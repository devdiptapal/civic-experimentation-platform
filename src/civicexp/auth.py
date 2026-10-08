"""Accounts, roles, and access keys for the hosted platform.

The library on its own has no user model: the ``actor`` written into an
audit entry is whatever the caller says it is. That is acceptable for a
script an analyst runs on their own machine, and it is the first thing the
threat model says an agency must fix before letting more than one person
near a pilot. This module fixes it for the hosted platform.

Three ideas carry the design:

**Roles are the approval roles.** The five reviewer roles that must sign
off before a pilot launches (:data:`civicexp.lifecycle.REQUIRED_APPROVALS`)
are the roles accounts hold. A sign-off as privacy reviewer can only come
from an account that holds that role, so the approval record names a real,
authenticated person rather than a typed string.

**Administration is not authority.** The ``admin`` role manages accounts,
keys, and settings, and holds no approval power of its own. The person who
can create accounts should not be able to approve a pilot by themselves; if
a small team needs one person in both seats, the grant is explicit and
lands in the workspace audit trail.

**Secrets are stored only as digests.** Passwords are hashed with
PBKDF2-HMAC-SHA256 at the iteration count OWASP recommends; API keys are
high-entropy random tokens stored as SHA-256 digests and shown once, at
creation. Neither file is useful to someone who copies it.

Everything here uses the standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import CivicExpError
from .lifecycle import REQUIRED_APPROVALS
from .storage import file_lock, read_json, write_json

__all__ = [
    "ROLES",
    "ROLE_DESCRIPTIONS",
    "PERMISSIONS",
    "SCOPES",
    "AuthError",
    "User",
    "UserStore",
    "ApiKey",
    "ApiKeyStore",
    "Session",
    "SessionStore",
    "LoginThrottle",
    "hash_password",
    "verify_password",
    "check_password_policy",
]


class AuthError(CivicExpError):
    """An account, key, or permission problem."""

    code = "AUTH_ERROR"


#: What each role is for, in the words shown to people choosing roles.
ROLE_DESCRIPTIONS: Mapping[str, str] = {
    "program_owner": (
        "Owns the service being changed. Approves the plan once every reviewer "
        "has signed off, and records the final decision."
    ),
    "operations_lead": (
        "Runs the service day to day. Starts, pauses, and resumes the pilot, "
        "and can roll it back."
    ),
    "privacy_reviewer": (
        "Confirms the pilot collects only what it needs and keeps it only as "
        "long as approved."
    ),
    "legal_policy_reviewer": (
        "Confirms the change does not alter eligibility, required documents, "
        "or policy."
    ),
    "evaluation_lead": (
        "Designs the measurement, and is the only role that can take a recorded "
        "interim look at results while a pilot runs."
    ),
    "admin": (
        "Manages accounts, access keys, and workspace settings. Holds no approval "
        "authority."
    ),
    "viewer": "Can read evaluations, readouts, and the evidence library.",
}
ROLES: tuple[str, ...] = tuple(ROLE_DESCRIPTIONS)

_REVIEWERS = frozenset(REQUIRED_APPROVALS)

#: Which roles may perform each action. Every action not listed here is
#: available to any signed-in account, which in practice means reading.
PERMISSIONS: Mapping[str, frozenset[str]] = {
    "experiment.create": frozenset({"program_owner", "evaluation_lead"}),
    "experiment.edit": frozenset({"program_owner", "evaluation_lead"}),
    "experiment.dry_run": frozenset({"program_owner", "evaluation_lead"}),
    "lifecycle.approve": frozenset({"program_owner"}),
    "lifecycle.reopen": frozenset({"program_owner", "evaluation_lead"}),
    "lifecycle.start": frozenset({"operations_lead", "program_owner"}),
    # Pausing is always available to anyone with a stake in the pilot. It is
    # never destructive, and a person who sees harm should not have to find
    # someone with more authority before stopping it.
    "lifecycle.pause": _REVIEWERS | {"admin"},
    "lifecycle.resume": frozenset({"operations_lead", "program_owner"}),
    "lifecycle.look": frozenset({"evaluation_lead"}),
    "lifecycle.complete": frozenset({"program_owner"}),
    "lifecycle.roll_back": frozenset({"program_owner", "operations_lead"}),
    "lifecycle.archive": frozenset({"program_owner", "admin"}),
    "case.publish": frozenset({"program_owner", "evaluation_lead"}),
    "library.import": frozenset({"evaluation_lead", "admin"}),
    "workspace.admin": frozenset({"admin"}),
}

#: What an API key may be used for.
SCOPES: Mapping[str, str] = {
    "collect": "Assign people to versions and record outcome events.",
    "read": "Read evaluation status, safety monitoring, and alerts.",
}

#: OWASP's 2023 recommendation for PBKDF2-HMAC-SHA256.
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 12
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Hash a password for storage, in a self-describing format."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash, in constant time."""
    try:
        scheme, iterations, salt_hex, digest_hex = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def check_password_policy(password: str, username: str = "") -> None:
    """Raise if a password is too weak to accept.

    Length is the requirement that matters; composition rules mostly
    produce predictable substitutions. A password containing the username
    is refused because it is the first thing anyone guesses.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"passwords need at least {MIN_PASSWORD_LENGTH} characters. A short "
            "phrase is easier to remember than a complex word and harder to guess.",
            code="AUTH_WEAK_PASSWORD",
        )
    if username and username.lower() in password.lower():
        raise AuthError(
            "the password may not contain the username", code="AUTH_WEAK_PASSWORD"
        )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    """A signed-in person, and what they are allowed to do."""

    username: str
    display_name: str
    roles: frozenset[str]
    password_hash: str = field(repr=False, default="")
    disabled: bool = False
    created: str = ""

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def can(self, permission: str) -> bool:
        if self.disabled:
            return False
        allowed = PERMISSIONS.get(permission)
        if allowed is None:
            return True
        return bool(self.roles & allowed)

    @property
    def reviewer_roles(self) -> tuple[str, ...]:
        """The approval roles this person can sign off as, in checklist order."""
        return tuple(r for r in REQUIRED_APPROVALS if r in self.roles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "roles": sorted(self.roles),
            "password_hash": self.password_hash,
            "disabled": self.disabled,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> User:
        return cls(
            username=str(data["username"]),
            display_name=str(data.get("display_name") or data["username"]),
            roles=frozenset(str(r) for r in data.get("roles") or ()),
            password_hash=str(data.get("password_hash", "")),
            disabled=bool(data.get("disabled", False)),
            created=str(data.get("created", "")),
        )


def _check_roles(roles: Iterable[str]) -> frozenset[str]:
    chosen = frozenset(roles)
    unknown = sorted(chosen - set(ROLES))
    if unknown:
        raise AuthError(
            f"unknown role(s) {unknown}; available roles are {', '.join(ROLES)}",
            code="AUTH_UNKNOWN_ROLE",
        )
    return chosen


class UserStore:
    """Accounts, kept in one JSON file holding password hashes only."""

    def __init__(self, path: str | Path, *, iterations: int = PBKDF2_ITERATIONS) -> None:
        self.path = Path(path)
        self.iterations = iterations
        self._dummy_hash: str | None = None

    def _timing_decoy(self) -> str:
        # Verifying against this when the username does not exist keeps a
        # failed login taking the same time whether or not the account
        # exists, so response timing cannot be used to list accounts. Built
        # on first use: hashing at this cost on every construction would
        # slow down every command that opens a workspace.
        if self._dummy_hash is None:
            self._dummy_hash = hash_password(secrets.token_hex(8), iterations=self.iterations)
        return self._dummy_hash

    def _load(self) -> dict[str, User]:
        data = read_json(self.path, default={"users": []})
        return {u["username"]: User.from_dict(u) for u in data.get("users", [])}

    def _save(self, users: Mapping[str, User]) -> None:
        write_json(
            self.path,
            {"users": [users[name].to_dict() for name in sorted(users)]},
            private=True,
        )

    def list(self) -> list[User]:
        return sorted(self._load().values(), key=lambda u: u.username)

    def get(self, username: str) -> User | None:
        return self._load().get(username)

    def __len__(self) -> int:
        return len(self._load())

    def add(
        self,
        username: str,
        *,
        password: str,
        roles: Iterable[str],
        display_name: str = "",
    ) -> User:
        username = username.strip().lower()
        if not _USERNAME_RE.match(username):
            raise AuthError(
                "usernames are 2-63 characters of lowercase letters, digits, '.', "
                "'_' or '-', starting with a letter or digit",
                code="AUTH_BAD_USERNAME",
            )
        check_password_policy(password, username)
        user = User(
            username=username,
            display_name=display_name.strip() or username,
            roles=_check_roles(roles),
            password_hash=hash_password(password, iterations=self.iterations),
            created=_now(),
        )
        with file_lock(self.path):
            users = self._load()
            if username in users:
                raise AuthError(f"an account named {username!r} already exists",
                                code="AUTH_DUPLICATE_USER")
            users[username] = user
            self._save(users)
        return user

    def _update(self, username: str, change: Callable[[User], User]) -> User:
        with file_lock(self.path):
            users = self._load()
            if username not in users:
                raise AuthError(f"no account named {username!r}", code="AUTH_NO_USER")
            users[username] = change(users[username])
            self._save(users)
            return users[username]

    def set_password(self, username: str, password: str) -> User:
        check_password_policy(password, username)
        encoded = hash_password(password, iterations=self.iterations)
        return self._update(username, lambda u: replace(u, password_hash=encoded))

    def set_roles(self, username: str, roles: Iterable[str]) -> User:
        chosen = _check_roles(roles)
        return self._update(username, lambda u: replace(u, roles=chosen))

    def set_display_name(self, username: str, display_name: str) -> User:
        name = display_name.strip() or username
        return self._update(username, lambda u: replace(u, display_name=name))

    def set_disabled(self, username: str, disabled: bool) -> User:
        return self._update(username, lambda u: replace(u, disabled=disabled))

    def authenticate(self, username: str, password: str) -> User | None:
        """The account for these credentials, or ``None``."""
        user = self.get(username.strip().lower())
        if user is None or user.disabled:
            verify_password(password, self._timing_decoy())
            return None
        return user if verify_password(password, user.password_hash) else None


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"^cxk_([0-9a-f]{12})_([A-Za-z0-9_-]{20,})$")


@dataclass(frozen=True)
class ApiKey:
    """A credential a live service uses to call the collection API."""

    key_id: str
    name: str
    scopes: frozenset[str]
    #: Experiment ids this key is limited to. Empty means every experiment.
    experiments: frozenset[str]
    secret_hash: str = field(repr=False)
    created: str = ""
    created_by: str = ""
    revoked: bool = False

    def allows(self, scope: str, experiment_id: str | None = None) -> bool:
        if self.revoked or scope not in self.scopes:
            return False
        if experiment_id is not None and self.experiments:
            return experiment_id in self.experiments
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "scopes": sorted(self.scopes),
            "experiments": sorted(self.experiments),
            "secret_hash": self.secret_hash,
            "created": self.created,
            "created_by": self.created_by,
            "revoked": self.revoked,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ApiKey:
        return cls(
            key_id=str(data["key_id"]),
            name=str(data.get("name", "")),
            scopes=frozenset(data.get("scopes") or ()),
            experiments=frozenset(data.get("experiments") or ()),
            secret_hash=str(data["secret_hash"]),
            created=str(data.get("created", "")),
            created_by=str(data.get("created_by", "")),
            revoked=bool(data.get("revoked", False)),
        )


class ApiKeyStore:
    """Collection keys, stored as digests and shown in full exactly once."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, ApiKey]:
        data = read_json(self.path, default={"keys": []})
        return {k["key_id"]: ApiKey.from_dict(k) for k in data.get("keys", [])}

    def _save(self, keys: Mapping[str, ApiKey]) -> None:
        write_json(
            self.path, {"keys": [keys[k].to_dict() for k in sorted(keys)]}, private=True
        )

    def list(self) -> list[ApiKey]:
        return sorted(self._load().values(), key=lambda k: (k.revoked, k.created, k.key_id))

    def get(self, key_id: str) -> ApiKey | None:
        return self._load().get(key_id)

    def create(
        self,
        name: str,
        *,
        scopes: Iterable[str],
        experiments: Iterable[str] = (),
        created_by: str = "",
    ) -> tuple[ApiKey, str]:
        """Create a key, returning it and the token to hand to the service."""
        chosen = frozenset(scopes)
        unknown = sorted(chosen - set(SCOPES))
        if not chosen or unknown:
            raise AuthError(
                f"a key needs at least one scope from {', '.join(SCOPES)}"
                + (f"; unknown: {unknown}" if unknown else ""),
                code="AUTH_BAD_SCOPE",
            )
        key_id = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        key = ApiKey(
            key_id=key_id,
            name=name.strip() or key_id,
            scopes=chosen,
            experiments=frozenset(experiments),
            secret_hash=hashlib.sha256(secret.encode("ascii")).hexdigest(),
            created=_now(),
            created_by=created_by,
        )
        with file_lock(self.path):
            keys = self._load()
            keys[key_id] = key
            self._save(keys)
        return key, f"cxk_{key_id}_{secret}"

    def verify(self, token: str) -> ApiKey | None:
        """The live key a bearer token belongs to, or ``None``."""
        match = _TOKEN_RE.match(token or "")
        if not match:
            return None
        key = self.get(match.group(1))
        if key is None or key.revoked:
            return None
        digest = hashlib.sha256(match.group(2).encode("ascii")).hexdigest()
        return key if hmac.compare_digest(digest, key.secret_hash) else None

    def revoke(self, key_id: str) -> ApiKey:
        with file_lock(self.path):
            keys = self._load()
            if key_id not in keys:
                raise AuthError(f"no key with id {key_id!r}", code="AUTH_NO_KEY")
            keys[key_id] = replace(keys[key_id], revoked=True)
            self._save(keys)
            return keys[key_id]


# ---------------------------------------------------------------------------
# Sessions and login throttling
# ---------------------------------------------------------------------------


@dataclass
class Session:
    session_id: str
    username: str
    csrf_token: str
    expires_at: float


class SessionStore:
    """Signed-in browser sessions, held in memory.

    Keeping them server-side means signing out, disabling an account, or
    changing a password ends the session immediately, which a signed cookie
    alone cannot do. A restart signs everyone out, which is the right
    failure direction for a tool that holds approval authority.
    """

    def __init__(
        self, *, ttl_seconds: int = 8 * 3600, clock: Callable[[], float] = time.time
    ) -> None:
        self.ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> Session:
        session = Session(
            session_id=secrets.token_urlsafe(32),
            username=username,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=self._clock() + self.ttl,
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at < self._clock():
                del self._sessions[session_id]
                return None
            return session

    def destroy(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def destroy_user(self, username: str) -> None:
        with self._lock:
            for sid in [s for s, v in self._sessions.items() if v.username == username]:
                del self._sessions[sid]


class LoginThrottle:
    """Refuse further attempts after repeated failures, for a while.

    Keyed separately by account and by client address, so one person
    mistyping cannot lock out a whole office behind one address, and one
    address cannot try every account.
    """

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: int = 300,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = self._clock() - self.window
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def retry_after(self, *keys: str) -> int:
        """Seconds until any of ``keys`` may try again; ``0`` if allowed now."""
        with self._lock:
            waits = []
            for key in keys:
                recent = self._recent(key)
                if len(recent) >= self.max_failures:
                    waits.append(int(recent[0] + self.window - self._clock()) + 1)
            return max(waits, default=0)

    def failure(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.setdefault(key, []).append(self._clock())

    def success(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)

"""A workspace: everything one agency team's evaluations need, in one folder.

The library modules each handle one concern. A workspace puts them together
the way an agency actually uses them -- several evaluations, several people
with different roles, data arriving from a live service -- and is what the
web application and the workspace commands operate on.

Layout
------
::

    workspace.json          name, jurisdiction, settings
    users.json              accounts (password hashes only; owner-readable)
    api-keys.json           collection keys (digests only; owner-readable)
    audit.jsonl             hash-chained record of administrative actions
    experiments/<id>/
        experiment.json     the plan reviewers sign off on
        audit.jsonl         its governance record -- the source of truth for
                            its state, sign-offs, and interim looks
        events.jsonl        live outcome events (only while approved)
        dry-run.jsonl       simulated events, never mixed with live ones
        final/              frozen when the decision is recorded: readout,
                            result, case summary, case record
    library/                published case records (see civicexp.library)

Everything is a plain file an agency can read without this software, back
up with the tools it already has, and delete for retention purposes with a
file operation.

Who may do what
---------------
Every action that changes an evaluation takes the :class:`~civicexp.auth.
User` performing it and checks their role here, so the web application and
the command line cannot disagree about the rules.

Results while a pilot runs
--------------------------
Harm is monitored continuously; benefit is read at the pre-registered time.
While an evaluation runs, guardrails, trust checks, and confirmed equity
harms are always visible, but the main result is **sealed**: it is shown
only through a recorded interim look (:meth:`ExperimentRecord.take_look`),
each of which is counted against the looks the plan declared. Watching a
live dashboard until the number looks good is the most common way an
experiment produces a false positive, and the significance threshold this
platform applies is only honest if the looks were counted.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .analysis import AnalysisResult, analyze
from .audit import AuditEntry, AuditLog
from .auth import PBKDF2_ITERATIONS, ApiKeyStore, AuthError, User, UserStore
from .collect import ENTRY_EVENT, Collector
from .config import ExperimentConfig, load_config
from .errors import AnalysisError, CivicExpError, ConfigError, LifecycleError
from .events import EventLog, parse_timestamp
from .library import CaseRecord, EvidenceLibrary, build_case_record
from .lifecycle import REQUIRED_APPROVALS, ExperimentLifecycle, State
from .report import render_case_summary, render_markdown
from .simulate import SimulationSpec, satisfying_attributes, segment_mix_for, simulate_pilot
from .storage import file_lock, read_json, tail_line, write_json

__all__ = [
    "WorkspaceError",
    "Workspace",
    "ExperimentRecord",
    "GovernanceStatus",
    "AnalysisOutcome",
    "DEFAULT_SETTINGS",
]

WORKSPACE_FORMAT = 1

DEFAULT_SETTINGS: Mapping[str, Any] = {
    #: One person may not provide two of the five sign-offs. Small teams can
    #: turn this off; the change is recorded in the workspace audit trail.
    "require_distinct_reviewers": True,
    #: A running pilot with no new events for this long raises an alert.
    #: A daily monitoring cadence plus a couple of hours' slack.
    "stale_data_hours": 26,
    #: Marks every case record published from this workspace as synthetic.
    #: Set for demonstration and training workspaces.
    "synthetic_data": False,
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_FILL_IN = "FILL IN"


class WorkspaceError(CivicExpError):
    code = "WORKSPACE_ERROR"


def _require(user: User, permission: str) -> None:
    if not user.can(permission):
        raise AuthError(
            f"{user.username} does not hold a role that allows this "
            f"({permission}).",
            code="AUTH_FORBIDDEN",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def unfinished_fields(raw: Any, path: str = "") -> list[str]:
    """Dotted paths in a plan that still contain a template's FILL IN marker.

    A plan with unanswered questions is not ready for review. Reviewers
    approving "FILL IN: the changed version" have approved nothing.
    """
    found: list[str] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if str(key).startswith("_"):
                continue
            found.extend(unfinished_fields(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            found.extend(unfinished_fields(value, f"{path}[{index}]"))
    elif isinstance(raw, str) and _FILL_IN in raw:
        found.append(path)
    return found


@dataclass(frozen=True)
class GovernanceStatus:
    """Whether an evaluation may collect data right now, and why not if not."""

    state: State | None
    audit_valid: bool
    audit_message: str
    #: ``None`` before approval, or when approval did not record a plan.
    plan_matches_approval: bool | None
    collecting: bool
    reason: str


@dataclass(frozen=True)
class AnalysisOutcome:
    """Results for every treatment arm, or why there are none yet."""

    results: tuple[AnalysisResult, ...]
    problem: str | None = None


def _result_detail(results: tuple[AnalysisResult, ...]) -> dict[str, Any]:
    """Flatten results into audit-entry fields, prefixed by arm if several."""
    detail: dict[str, Any] = {}
    for result in results:
        prefix = f"{result.treatment_variant}_" if len(results) > 1 else ""
        primary = result.primary
        low, high = primary.difference_interval
        change = getattr(primary, "absolute_difference", None)
        if change is None:
            change = primary.difference
        detail[f"{prefix}arm"] = result.treatment_variant
        detail[f"{prefix}decision"] = result.decision.value
        detail[f"{prefix}effect"] = round(change, 6)
        detail[f"{prefix}ci_low"] = round(low, 6)
        detail[f"{prefix}ci_high"] = round(high, 6)
    return detail


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_mtime_ns, info.st_size)


class Workspace:
    """One agency team's evaluations, accounts, keys, and published evidence."""

    SETTINGS_FILE = "workspace.json"

    def __init__(
        self,
        root: str | Path,
        *,
        password_iterations: int = PBKDF2_ITERATIONS,
        clock: Any = None,
    ) -> None:
        self.root = Path(root)
        #: Timestamps for new governance entries. Real time unless injected,
        #: which only tests and the demonstration workspace do.
        self.clock = clock
        if not (self.root / self.SETTINGS_FILE).exists():
            raise WorkspaceError(
                f"{self.root} is not a workspace. Create one with: "
                f"civicexp workspace init {self.root}",
                code="WORKSPACE_MISSING",
            )
        self.users = UserStore(self.root / "users.json", iterations=password_iterations)
        self.keys = ApiKeyStore(self.root / "api-keys.json")
        self.library = EvidenceLibrary(self.root / "library")
        self._records: dict[str, ExperimentRecord] = {}
        self._records_lock = threading.Lock()

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        name: str,
        jurisdiction: str = "",
        actor: str = "setup",
        password_iterations: int = PBKDF2_ITERATIONS,
        clock: Any = None,
        **settings: Any,
    ) -> Workspace:
        target = Path(root)
        if (target / cls.SETTINGS_FILE).exists():
            raise WorkspaceError(
                f"{target} is already a workspace", code="WORKSPACE_EXISTS"
            )
        target.mkdir(parents=True, exist_ok=True)
        (target / "experiments").mkdir(exist_ok=True)
        (target / "library").mkdir(exist_ok=True)
        data = {
            "format": WORKSPACE_FORMAT,
            "name": name.strip() or "Civic experimentation workspace",
            "jurisdiction": jurisdiction.strip(),
            "created": (clock() if clock else _now()).replace(microsecond=0).isoformat(),
            **DEFAULT_SETTINGS,
            **settings,
        }
        write_json(target / cls.SETTINGS_FILE, data)
        workspace = cls(target, password_iterations=password_iterations, clock=clock)
        workspace.record_admin("workspace:created", actor, name=data["name"])
        return workspace

    # -- settings and administration --------------------------------------

    @property
    def settings(self) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        data.update(read_json(self.root / self.SETTINGS_FILE, default={}))
        return data

    @property
    def name(self) -> str:
        return str(self.settings.get("name", ""))

    @property
    def jurisdiction(self) -> str:
        return str(self.settings.get("jurisdiction", ""))

    def update_settings(self, actor: User, **changes: Any) -> dict[str, Any]:
        _require(actor, "workspace.admin")
        allowed = {"name", "jurisdiction", *DEFAULT_SETTINGS}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise WorkspaceError(f"unknown setting(s): {unknown}", code="WORKSPACE_SETTING")
        path = self.root / self.SETTINGS_FILE
        with file_lock(path):
            data = read_json(path, default={})
            before = {k: data.get(k) for k in changes}
            data.update(changes)
            write_json(path, data)
        self.record_admin(
            "settings:changed",
            actor.username,
            **{f"{k}": f"{before[k]!r} -> {v!r}" for k, v in changes.items()},
        )
        return self.settings

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    def audit(self) -> AuditLog:
        return AuditLog(self.audit_path)

    def record_admin(self, action: str, actor: str, **detail: Any) -> AuditEntry:
        """Append to the workspace's own hash-chained administrative record."""
        with file_lock(self.audit_path):
            return AuditLog(self.audit_path, clock=self.clock).append(action, actor, **detail)

    def add_user(
        self,
        actor: str,
        username: str,
        *,
        password: str,
        roles: Any,
        display_name: str = "",
    ) -> User:
        user = self.users.add(
            username, password=password, roles=roles, display_name=display_name
        )
        self.record_admin(
            "user:created", actor, username=user.username, roles=",".join(sorted(user.roles))
        )
        return user

    def set_user_roles(self, actor: str, username: str, roles: Any) -> User:
        user = self.users.set_roles(username, roles)
        self.record_admin(
            "user:roles_changed", actor, username=username, roles=",".join(sorted(user.roles))
        )
        return user

    def set_user_disabled(self, actor: str, username: str, disabled: bool) -> User:
        user = self.users.set_disabled(username, disabled)
        self.record_admin(
            "user:disabled" if disabled else "user:enabled", actor, username=username
        )
        return user

    def reset_password(self, actor: str, username: str, password: str) -> User:
        user = self.users.set_password(username, password)
        self.record_admin("user:password_set", actor, username=username)
        return user

    def create_key(
        self, actor: str, name: str, *, scopes: Any, experiments: Any = ()
    ) -> tuple[Any, str]:
        key, token = self.keys.create(
            name, scopes=scopes, experiments=experiments, created_by=actor
        )
        self.record_admin(
            "key:created",
            actor,
            key_id=key.key_id,
            name=key.name,
            scopes=",".join(sorted(key.scopes)),
            experiments=",".join(sorted(key.experiments)) or "all",
        )
        return key, token

    def revoke_key(self, actor: str, key_id: str) -> Any:
        key = self.keys.revoke(key_id)
        self.record_admin("key:revoked", actor, key_id=key_id, name=key.name)
        return key

    # -- experiments -------------------------------------------------------

    @property
    def experiments_dir(self) -> Path:
        return self.root / "experiments"

    def experiment_ids(self) -> list[str]:
        if not self.experiments_dir.exists():
            return []
        return sorted(
            p.name
            for p in self.experiments_dir.iterdir()
            if p.is_dir() and (p / "experiment.json").exists()
        )

    def experiments(self) -> list[ExperimentRecord]:
        return [self.experiment(i) for i in self.experiment_ids()]

    def experiment(self, experiment_id: str) -> ExperimentRecord:
        if not _ID_RE.match(experiment_id) or not (
            self.experiments_dir / experiment_id / "experiment.json"
        ).exists():
            raise WorkspaceError(
                f"no evaluation with id {experiment_id!r} in this workspace",
                code="WORKSPACE_NOT_FOUND",
            )
        with self._records_lock:
            record = self._records.get(experiment_id)
            if record is None:
                record = self._records[experiment_id] = ExperimentRecord(self, experiment_id)
            return record

    def create_experiment(
        self, raw: Mapping[str, Any], user: User, *, workflow: str | None = None
    ) -> ExperimentRecord:
        """Add a new evaluation plan, in draft."""
        _require(user, "experiment.create")
        data = copy.deepcopy(dict(raw))
        data.pop("_template", None)
        if workflow:
            data.setdefault("scope", {})["workflow"] = workflow
        config = load_config(data)
        if not _ID_RE.match(config.experiment_id):
            raise ConfigError(
                "experiment.id may contain only letters, digits, '.', '_' and '-', "
                "and must start with a letter or digit",
                code="CONFIG_BAD_ID",
            )
        directory = self.experiments_dir / config.experiment_id
        if directory.exists():
            raise WorkspaceError(
                f"an evaluation with id {config.experiment_id!r} already exists; "
                "choose a different id",
                code="WORKSPACE_DUPLICATE",
            )
        directory.mkdir(parents=True)
        write_json(directory / "experiment.json", data)
        record = self.experiment(config.experiment_id)
        with record.mutate() as lifecycle:
            lifecycle.record(
                "config:created",
                user.username,
                config_digest=config.fingerprint,
                name=config.name,
            )
        return record

    def pending_sign_offs(self, user: User) -> list[tuple[ExperimentRecord, str]]:
        """Draft evaluations waiting on a sign-off this person could give."""
        waiting = []
        for record in self.experiments():
            try:
                lifecycle = record.lifecycle()
            except LifecycleError:
                continue
            if lifecycle.state is not State.DRAFT:
                continue
            for role in user.reviewer_roles:
                if role in lifecycle.missing_approvals or record.sign_off_is_stale(role):
                    waiting.append((record, role))
        return waiting


class ExperimentRecord:
    """One evaluation's files, governance, data, and results."""

    def __init__(self, workspace: Workspace, experiment_id: str) -> None:
        self.workspace = workspace
        self.id = experiment_id
        self.dir = workspace.experiments_dir / experiment_id
        self.config_path = self.dir / "experiment.json"
        self.audit_path = self.dir / "audit.jsonl"
        self.events_path = self.dir / "events.jsonl"
        self.dry_run_path = self.dir / "dry-run.jsonl"
        self.final_dir = self.dir / "final"
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[Any, Any]] = {}
        self._collector: Collector | None = None

    def _cached(self, name: str, key: Any, compute: Any) -> Any:
        with self._lock:
            hit = self._cache.get(name)
            if hit is not None and hit[0] == key:
                return hit[1]
        value = compute()
        with self._lock:
            self._cache[name] = (key, value)
        return value

    # -- the plan ----------------------------------------------------------

    def raw(self) -> dict[str, Any]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def config(self) -> ExperimentConfig:
        text = self.config_path.read_text(encoding="utf-8")
        return self._cached("config", text, lambda: load_config(json.loads(text)))

    def unfinished(self) -> list[str]:
        return unfinished_fields(self.raw())

    def revise(self, user: User, raw: Mapping[str, Any]) -> ExperimentConfig:
        """Replace the plan. Only in draft; existing sign-offs become stale."""
        _require(user, "experiment.edit")
        data = copy.deepcopy(dict(raw))
        config = load_config(data)
        if config.experiment_id != self.id:
            raise ConfigError(
                f"experiment.id must stay {self.id!r}; create a new evaluation to "
                "change it",
                code="CONFIG_ID_CHANGED",
            )
        with self.mutate() as lifecycle:
            if lifecycle.state is not State.DRAFT:
                raise LifecycleError(
                    f"the plan can only be changed in draft; this evaluation is "
                    f"{lifecycle.state.value!r}. Reopen it to draft first, which "
                    "means every reviewer signs off again.",
                    code="LIFECYCLE_PLAN_LOCKED",
                )
            write_json(self.config_path, data)
            lifecycle.record(
                "config:revised", user.username, config_digest=config.fingerprint
            )
        return config

    # -- governance --------------------------------------------------------

    def lifecycle(self) -> ExperimentLifecycle:
        """The evaluation's state, replayed from its audit trail."""
        return self._cached(
            "lifecycle",
            _stat(self.audit_path),
            lambda: ExperimentLifecycle.from_audit(self.id, self.audit_path),
        )

    def audit(self) -> AuditLog:
        return AuditLog(self.audit_path)

    @contextlib.contextmanager
    def mutate(self) -> Iterator[ExperimentLifecycle]:
        """A fresh lifecycle, under a lock, for exactly one change."""
        with self._lock, file_lock(self.audit_path):
            try:
                yield ExperimentLifecycle.from_audit(
                    self.id, self.audit_path, clock=self.workspace.clock
                )
            finally:
                # Even a refused action may have appended (a look recorded
                # before a later step failed), so never keep a stale view.
                self._cache.pop("lifecycle", None)
                self._cache.pop("governance", None)

    def governance(self) -> GovernanceStatus:
        key = (_stat(self.audit_path), self.config_path.read_text(encoding="utf-8"))
        return self._cached("governance", key, self._compute_governance)

    def _compute_governance(self) -> GovernanceStatus:
        try:
            lifecycle = ExperimentLifecycle.from_audit(self.id, self.audit_path)
        except LifecycleError as exc:
            return GovernanceStatus(
                None, False, exc.message, None, False,
                "its governance record failed verification, so collection is halted",
            )
        try:
            fingerprint = self.config().fingerprint
        except ConfigError:
            fingerprint = ""
        approved = lifecycle.approved_digest
        matches = None if approved is None else approved == fingerprint
        state = lifecycle.state
        if matches is False and state in (State.APPROVED, State.RUNNING, State.PAUSED):
            return GovernanceStatus(
                state, True, "chain intact", False, False,
                "the plan was changed after it was approved; it must be reopened "
                "and approved again before it collects anything",
            )
        reasons = {
            State.DRAFT: "it has not been approved yet",
            State.APPROVED: "it has been approved but not started",
            State.RUNNING: "collecting",
            State.PAUSED: "it is paused",
            State.COMPLETED: "it has been completed",
            State.ROLLED_BACK: "it was rolled back",
            State.ARCHIVED: "it has been archived",
        }
        return GovernanceStatus(
            state, True, "chain intact", matches, state is State.RUNNING, reasons[state]
        )

    def collecting(self) -> tuple[bool, str]:
        status = self.governance()
        return status.collecting, status.reason

    def collector(self) -> Collector:
        """The live collector, rebuilt if the plan file changes."""
        config = self.config()
        with self._lock:
            if self._collector is None or self._collector.config.fingerprint != config.fingerprint:
                self._collector = Collector(config, self.events_path, gate=self.collecting)
            return self._collector

    def sign_off_is_stale(self, role: str) -> bool:
        lifecycle = self.lifecycle()
        sign_off = lifecycle.sign_offs.get(role)
        return bool(
            sign_off and sign_off.config_digest
            and sign_off.config_digest != self.config().fingerprint
        )

    # -- lifecycle actions -------------------------------------------------

    def sign_off(self, user: User, role: str, note: str = "") -> None:
        if not user.has_role(role) or user.disabled:
            raise AuthError(
                f"{user.username} does not hold the {role} role, so cannot sign off "
                "for it",
                code="AUTH_FORBIDDEN",
            )
        unfinished = self.unfinished()
        if unfinished:
            raise LifecycleError(
                "the plan is not finished, so it is not ready for review. Still "
                f"marked FILL IN: {', '.join(unfinished)}",
                code="LIFECYCLE_PLAN_UNFINISHED",
            )
        fingerprint = self.config().fingerprint
        with self.mutate() as lifecycle:
            if self.workspace.settings.get("require_distinct_reviewers", True):
                for other_role, existing in lifecycle.sign_offs.items():
                    if other_role != role and existing.actor == user.username:
                        raise LifecycleError(
                            f"{user.username} has already signed off as {other_role}. "
                            "Each review role needs a different person, so that no "
                            "single person can approve a pilot alone.",
                            code="LIFECYCLE_SAME_REVIEWER",
                        )
            lifecycle.sign_off(role, user.username, note, config_digest=fingerprint)

    def approve(self, user: User) -> None:
        _require(user, "lifecycle.approve")
        fingerprint = self.config().fingerprint
        with self.mutate() as lifecycle:
            lifecycle.approve(user.username, config_digest=fingerprint)

    def reopen(self, user: User, reason: str) -> None:
        _require(user, "lifecycle.reopen")
        if not reason.strip():
            raise LifecycleError("reopening needs a reason", code="LIFECYCLE_NO_REASON")
        with self.mutate() as lifecycle:
            lifecycle.reopen(user.username, reason=reason.strip())

    def _require_plan_unchanged(self) -> None:
        status = self.governance()
        if status.plan_matches_approval is False:
            raise LifecycleError(
                "the plan on file is not the plan that was approved. Reopen the "
                "evaluation so reviewers can sign off on the current version.",
                code="LIFECYCLE_PLAN_CHANGED",
            )

    def start(self, user: User) -> None:
        _require(user, "lifecycle.start")
        self._require_plan_unchanged()
        with self.mutate() as lifecycle:
            lifecycle.start(user.username)

    def pause(self, user: User, reason: str) -> None:
        _require(user, "lifecycle.pause")
        with self.mutate() as lifecycle:
            lifecycle.pause(user.username, reason=reason.strip())

    def resume(self, user: User) -> None:
        _require(user, "lifecycle.resume")
        self._require_plan_unchanged()
        with self.mutate() as lifecycle:
            lifecycle.resume(user.username)

    def archive(self, user: User) -> None:
        _require(user, "lifecycle.archive")
        with self.mutate() as lifecycle:
            lifecycle.archive(user.username)

    def take_look(self, user: User) -> tuple[int, AnalysisOutcome]:
        """Unseal the current result once, on the record."""
        _require(user, "lifecycle.look")
        with self.mutate() as lifecycle:
            number = lifecycle.interim_looks + 1
            outcome = self.analyses("live", looks_taken=number)
            if not outcome.results:
                raise WorkspaceError(
                    f"there is nothing to look at yet: {outcome.problem}",
                    code="WORKSPACE_NO_RESULTS",
                )
            planned = self.config().decision.planned_looks
            lifecycle.record_look(
                user.username,
                planned_looks=planned,
                within_plan=number < planned,
                **_result_detail(outcome.results),
            )
        return number, outcome

    def looks(self) -> list[AuditEntry]:
        return [e for e in self.audit() if e.action == "analysis:interim_look"]

    def conclude(self, user: User, *, outcome: str, note: str) -> AnalysisOutcome:
        """Record the final decision and freeze the evidence it rests on.

        ``outcome`` is ``"complete"`` (the pilot ran its course) or
        ``"roll_back"`` (revert the tested version). The final analysis is
        the last look, and is counted as one.
        """
        if outcome not in ("complete", "roll_back"):
            raise WorkspaceError(f"unknown outcome {outcome!r}", code="WORKSPACE_OUTCOME")
        _require(user, "lifecycle.complete" if outcome == "complete" else "lifecycle.roll_back")
        with self.mutate() as lifecycle:
            if lifecycle.state not in (State.RUNNING, State.PAUSED):
                raise LifecycleError(
                    f"only a running or paused evaluation can be ended; this one is "
                    f"{lifecycle.state.value!r}",
                    code="LIFECYCLE_FORBIDDEN",
                )
            final = self.analyses("live", looks_taken=lifecycle.interim_looks + 1)
            detail = _result_detail(final.results)
            if outcome == "complete":
                decision = ", ".join(r.decision.value for r in final.results) or "no result"
                lifecycle.complete(user.username, summary=note.strip() or decision, **detail)
            else:
                lifecycle.roll_back(user.username, reason=note.strip(), **detail)
            self._freeze(lifecycle, final, user)
        return final

    def _freeze(
        self, lifecycle: ExperimentLifecycle, final: AnalysisOutcome, user: User
    ) -> None:
        """Write the decision record, so it survives the data's deletion."""
        if not final.results:
            return
        self.final_dir.mkdir(parents=True, exist_ok=True)
        settings = self.workspace.settings
        raw = self.raw()
        written: dict[str, str] = {}
        for result in final.results:
            suffix = f"-{result.treatment_variant}" if len(final.results) > 1 else ""
            stamp = self.workspace.clock() if self.workspace.clock else None
            files = {
                f"readout{suffix}.md": render_markdown(
                    result, audit=lifecycle.audit, generated_at=stamp
                ),
                f"result{suffix}.json": json.dumps(result.to_dict(), indent=2) + "\n",
                f"case-summary{suffix}.md": render_case_summary(
                    result,
                    jurisdiction=str(settings.get("jurisdiction", "")),
                    generated_at=stamp,
                ),
            }
            try:
                record = build_case_record(
                    result,
                    raw_config=raw,
                    jurisdiction=str(settings.get("jurisdiction", "")),
                    synthetic=bool(settings.get("synthetic_data")),
                    published=stamp,
                    platform_version=__version__,
                )
            except CivicExpError:
                record = None  # too small to publish; the readout is still kept
            if record is not None:
                files[f"case-record{suffix}.json"] = (
                    json.dumps(record, indent=2, sort_keys=True) + "\n"
                )
            for name, text in files.items():
                path = self.final_dir / name
                path.write_text(text, encoding="utf-8")
                written[name.replace(".", "_").replace("-", "_")] = _sha256(path)
        lifecycle.record("record:final_frozen", user.username, **written)

    def final_readouts(self) -> list[tuple[str, Path]]:
        """Frozen readouts, as (arm label, path)."""
        if not self.final_dir.exists():
            return []
        found = []
        for path in sorted(self.final_dir.glob("readout*.md")):
            arm = path.stem[len("readout-"):] if path.stem != "readout" else ""
            found.append((arm, path))
        return found

    def final_file(self, name: str) -> Path | None:
        path = self.final_dir / name
        return path if path.exists() and path.parent == self.final_dir else None

    def publish(self, user: User, *, arm: str = "") -> CaseRecord:
        """Add the frozen case record to the workspace's evidence library."""
        _require(user, "case.publish")
        lifecycle = self.lifecycle()
        if not lifecycle.state.is_terminal:
            raise LifecycleError(
                "results are published once the evaluation has ended",
                code="LIFECYCLE_FORBIDDEN",
            )
        suffix = f"-{arm}" if arm else ""
        record_path = self.final_dir / f"case-record{suffix}.json"
        if not record_path.exists():
            raise WorkspaceError(
                "there is no publishable case record for this evaluation. Either it "
                "ended without results, or an arm was below the disclosure threshold.",
                code="WORKSPACE_NOTHING_TO_PUBLISH",
            )
        data = json.loads(record_path.read_text(encoding="utf-8"))
        summary_path = self.final_dir / f"case-summary{suffix}.md"
        markdown = summary_path.read_text(encoding="utf-8") if summary_path.exists() else None
        case = self.workspace.library.add(data, markdown=markdown, replace=True)
        with self.mutate() as fresh:
            fresh.record("case:published", user.username, case_id=case.case_id)
        return case

    # -- data --------------------------------------------------------------

    def _path_for(self, which: str) -> Path:
        if which == "live":
            return self.events_path
        if which == "dry_run":
            return self.dry_run_path
        raise WorkspaceError(f"unknown event log {which!r}", code="WORKSPACE_LOG")

    def event_log(self, which: str = "live") -> EventLog | None:
        path = self._path_for(which)
        stat = _stat(path)
        if stat is None:
            return None
        config = self.config()

        def load() -> EventLog:
            return EventLog(
                config.experiment_id,
                path=path,
                privacy=config.privacy,
                allowed_variants=config.variants,
            )

        return self._cached(f"log:{which}", (stat, config.fingerprint), load)

    def last_event_at(self, which: str = "live") -> datetime | None:
        line = tail_line(self._path_for(which))
        if not line:
            return None
        try:
            return parse_timestamp(str(json.loads(line).get("timestamp")))
        except (CivicExpError, ValueError, TypeError):
            return None

    def enrollment(self, which: str = "live") -> dict[str, int]:
        log = self.event_log(which)
        counts = dict.fromkeys(self.config().variants, 0)
        if log is not None:
            counts.update(log.count_units_with_event(ENTRY_EVENT))
        return counts

    def analyses(
        self, which: str = "live", *, looks_taken: int | None = None
    ) -> AnalysisOutcome:
        """Analyze every treatment arm against control."""
        log = self.event_log(which)
        if log is None or not len(log):
            return AnalysisOutcome((), "no events have been recorded yet")
        config = self.config()

        def compute() -> AnalysisOutcome:
            results = []
            for arm in config.treatment_variants:
                try:
                    results.append(
                        analyze(config, log, treatment_variant=arm, looks_taken=looks_taken)
                    )
                except AnalysisError as exc:
                    return AnalysisOutcome((), exc.message)
            return AnalysisOutcome(tuple(results))

        key = (_stat(self._path_for(which)), config.fingerprint, looks_taken)
        return self._cached(f"analysis:{which}", key, compute)

    def visibility(self) -> str:
        """What results may be shown: ``pre-launch``, ``sealed`` or ``final``."""
        try:
            state = self.lifecycle().state
        except LifecycleError:
            return "sealed"
        if state in (State.DRAFT, State.APPROVED):
            return "pre-launch"
        if state in (State.RUNNING, State.PAUSED):
            return "sealed"
        return "final"

    def progress(self) -> dict[str, Any]:
        """How far the pilot is toward its planned size and duration."""
        config = self.config()
        enrolled = self.enrollment("live")
        expected = config.expected_units_per_group
        smallest = min(enrolled.values()) if enrolled else 0
        try:
            lifecycle = self.lifecycle()
            started = lifecycle.started_at
            ended = lifecycle.ended_at
        except LifecycleError:
            started = ended = None
        days = None
        if started:
            until = parse_timestamp(ended) if ended else _now()
            days = max(0, (until - parse_timestamp(started)).days)
        return {
            "enrolled": enrolled,
            "expected_per_group": expected,
            "fraction_of_target": (smallest / expected) if expected else None,
            "started_at": started,
            "ended_at": ended,
            "days_elapsed": days,
            "planned_days": config.planned_duration_days,
            "last_event_at": self.last_event_at("live"),
        }

    def dry_run(
        self,
        user: User,
        *,
        control_rate: float,
        treatment_rate: float,
        units: int = 4000,
        seed: int = 20260904,
    ) -> int:
        """Generate simulated data so the team can preview the readout."""
        _require(user, "experiment.dry_run")
        if self.visibility() != "pre-launch":
            raise LifecycleError(
                "dry runs are for planning, before a pilot starts",
                code="LIFECYCLE_FORBIDDEN",
            )
        if not (0 < control_rate < 1 and 0 < treatment_rate < 1):
            raise WorkspaceError(
                "rates must be proportions between 0 and 1", code="WORKSPACE_BAD_RATE"
            )
        units = max(100, min(int(units), 100_000))
        config = self.config()
        attributes = satisfying_attributes(config.eligibility)
        if not config.eligibility.is_eligible(attributes):
            raise WorkspaceError(
                "could not construct a synthetic person these eligibility rules "
                "include; run the dry run from the command line with explicit "
                "attributes instead",
                code="WORKSPACE_DRY_RUN",
            )
        control = config.control_variant
        rates = dict.fromkeys(config.treatment_variants, treatment_rate)
        rates[control] = control_rate
        flat = dict.fromkeys(config.variants, 0.05)
        spec = SimulationSpec(
            unit_attributes=attributes,
            segment_distribution=segment_mix_for(config, attributes),
            completion_rate=rates,
            error_rate=flat,
            support_rate=dict.fromkeys(config.variants, 0.06),
            median_seconds=dict.fromkeys(config.variants, 300.0),
            returned_rate=dict.fromkeys(config.variants, 0.08),
            median_processing_seconds=dict.fromkeys(config.variants, 3 * 86400.0),
            units=units,
            seed=seed,
        )
        with self._lock:
            self.dry_run_path.unlink(missing_ok=True)
            log = simulate_pilot(config, spec, path=str(self.dry_run_path))
        with self.mutate() as lifecycle:
            lifecycle.record(
                "dry_run:generated", user.username, events=len(log), units=units,
                control_rate=control_rate, treatment_rate=treatment_rate,
            )
        return len(log)

    # -- retention ---------------------------------------------------------

    def retention_due(self, now: datetime | None = None) -> datetime | None:
        """When this evaluation's event data falls due for deletion, if ever."""
        try:
            lifecycle = self.lifecycle()
        except LifecycleError:
            return None
        if not lifecycle.state.is_terminal:
            return None
        ended = lifecycle.ended_at or lifecycle.entered_at(State.ARCHIVED)
        if not ended:
            return None
        days = self.config().privacy.retention_days
        return parse_timestamp(ended) + timedelta(days=days)

    def apply_retention(
        self, actor: str = "retention", now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Delete event data past its retention date, recording what was deleted.

        The plan, the governance record, and the frozen readout are kept:
        they hold aggregates and decisions, not records about people.
        """
        due = self.retention_due()
        moment = now or _now()
        if due is None or moment < due:
            return None
        deleted: dict[str, Any] = {}
        with self._lock:
            for path in (self.events_path, self.dry_run_path):
                if path.exists():
                    with file_lock(path):
                        with path.open("rb") as handle:
                            lines = sum(1 for _ in handle)
                        deleted[path.name] = {"sha256": _sha256(path), "lines": lines}
                        path.unlink()
            self._cache.clear()
            self._collector = None
        if not deleted:
            return None
        with self.mutate() as lifecycle:
            lifecycle.record(
                "retention:events_deleted",
                actor,
                due=due.isoformat(),
                **{
                    f"{name.replace('.', '_').replace('-', '_')}_{k}": v
                    for name, info in deleted.items()
                    for k, v in info.items()
                },
            )
        return deleted


def reviewer_progress(lifecycle: ExperimentLifecycle) -> list[tuple[str, Any]]:
    """Each required role with its sign-off (or ``None``), in checklist order."""
    return [(role, lifecycle.sign_offs.get(role)) for role in REQUIRED_APPROVALS]

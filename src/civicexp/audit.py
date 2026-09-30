"""A tamper-evident audit trail for experiment decisions.

``GOVERNANCE.md`` commits to "auditability by design": pilot plans, metric
definitions, and decision rules documented before launch and archived after
completion. An audit log that can be silently edited does not deliver that
commitment, so entries here are hash-chained.

Each entry stores the SHA-256 digest of its own canonical content together
with the digest of the entry before it. Altering, removing, or reordering any
entry breaks the chain from that point forward, and :meth:`AuditLog.verify`
reports the first broken link.

This is tamper-*evidence*, not tamper-*proofing*. Someone with write access
to the file can recompute the whole chain. What it defends against is the
realistic failure mode -- a record quietly corrected after the fact, or a log
line dropped -- rather than a determined attacker with filesystem access. For
stronger guarantees the digest of the final entry should be published
somewhere the operator does not control, such as a git commit or a records
management system.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["AuditEntry", "AuditLog", "GENESIS_HASH"]

#: The previous-hash value recorded for the first entry in a chain.
GENESIS_HASH = "0" * 64


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class AuditEntry:
    """One immutable record of something that happened to an experiment."""

    sequence: int
    timestamp: str
    action: str
    actor: str
    detail: Mapping[str, Any]
    previous_hash: str
    entry_hash: str

    @staticmethod
    def compute_hash(
        sequence: int,
        timestamp: str,
        action: str,
        actor: str,
        detail: Mapping[str, Any],
        previous_hash: str,
    ) -> str:
        material = _canonical(
            {
                "sequence": sequence,
                "timestamp": timestamp,
                "action": action,
                "actor": actor,
                "detail": detail,
                "previous_hash": previous_hash,
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def recompute(self) -> str:
        return self.compute_hash(
            self.sequence,
            self.timestamp,
            self.action,
            self.actor,
            self.detail,
            self.previous_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "action": self.action,
            "actor": self.actor,
            "detail": dict(self.detail),
            "previous_hash": self.previous_hash,
            "entry_hash": self.entry_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AuditEntry:
        return cls(
            sequence=int(data["sequence"]),
            timestamp=str(data["timestamp"]),
            action=str(data["action"]),
            actor=str(data["actor"]),
            detail=dict(data.get("detail") or {}),
            previous_hash=str(data["previous_hash"]),
            entry_hash=str(data["entry_hash"]),
        )


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of checking an audit chain's integrity."""

    valid: bool
    entries_checked: int
    first_invalid_sequence: int | None = None
    message: str = "chain intact"

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.valid


class AuditLog:
    """An append-only, hash-chained record of experiment governance events."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        path:
            File to append entries to. In-memory only when omitted.
        clock:
            Returns the timestamp for each new entry. Injectable so that
            tests and the worked example can produce a byte-for-byte
            reproducible trail; production code should leave it unset and
            get real wall-clock time.
        """
        self.path = Path(path) if path is not None else None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: list[AuditEntry] = []
        if self.path is not None and self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self._entries.append(AuditEntry.from_dict(json.loads(line)))

    def append(
        self, action: str, actor: str, **detail: Any
    ) -> AuditEntry:
        """Record an action and link it to the chain."""
        sequence = len(self._entries)
        previous = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
        timestamp = self._clock().isoformat()
        entry_hash = AuditEntry.compute_hash(
            sequence, timestamp, action, actor, detail, previous
        )
        entry = AuditEntry(
            sequence=sequence,
            timestamp=timestamp,
            action=action,
            actor=actor,
            detail=detail,
            previous_hash=previous,
            entry_hash=entry_hash,
        )
        self._entries.append(entry)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
        return entry

    def verify(self) -> VerificationResult:
        """Check that every entry hashes correctly and links to its predecessor."""
        expected_previous = GENESIS_HASH
        for index, entry in enumerate(self._entries):
            if entry.sequence != index:
                return VerificationResult(
                    False,
                    index,
                    entry.sequence,
                    f"entry at position {index} claims sequence {entry.sequence}; "
                    "an entry was inserted, removed, or reordered",
                )
            if entry.previous_hash != expected_previous:
                return VerificationResult(
                    False,
                    index,
                    entry.sequence,
                    f"entry {entry.sequence} does not link to the previous entry; "
                    "the chain was broken at or before this point",
                )
            if entry.recompute() != entry.entry_hash:
                return VerificationResult(
                    False,
                    index,
                    entry.sequence,
                    f"entry {entry.sequence} ({entry.action!r}) does not match its "
                    "recorded hash; its contents were altered after it was written",
                )
            expected_previous = entry.entry_hash
        return VerificationResult(True, len(self._entries))

    @property
    def head(self) -> str:
        """Digest of the most recent entry.

        Publishing this value externally -- in a git commit, a ticket, or a
        records system -- is what upgrades the chain from "detects accidental
        corruption" to "detects deliberate rewriting".
        """
        return self._entries[-1].entry_hash if self._entries else GENESIS_HASH

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    def __iter__(self) -> Iterator[AuditEntry]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def to_markdown(self) -> str:
        """Render the trail as a table for inclusion in a pilot readout."""
        lines = [
            "| # | Timestamp (UTC) | Action | Actor | Detail |",
            "| --- | --- | --- | --- | --- |",
        ]
        for entry in self._entries:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(entry.detail.items())) or "—"
            lines.append(
                f"| {entry.sequence} | {entry.timestamp} | {entry.action} | "
                f"{entry.actor} | {detail} |"
            )
        result = self.verify()
        lines.append("")
        lines.append(
            f"Chain verification: **{'intact' if result.valid else 'FAILED'}** "
            f"({result.entries_checked} entries checked). Head digest: `{self.head[:16]}…`"
        )
        return "\n".join(lines)

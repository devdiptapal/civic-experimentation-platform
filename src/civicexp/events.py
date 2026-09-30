"""Component 2: recording the outcomes an agency agreed to measure.

Events are written append-only as JSON Lines. The format is deliberately
boring: an agency analyst can open the file in any tool, a records officer
can read it without the platform installed, and deletion for retention
purposes is a file operation rather than a database migration.

Every event is validated on the way in against:

* the taxonomy in ``docs/Metrics-Schema.md``;
* the experiment's privacy policy (field names and value shapes);
* the requirement that an outcome be attributable to an assigned variant.

Validation happens at write time rather than at analysis time on purpose. A
field that should never have been collected is a privacy incident the moment
it is written to disk, and discovering it during analysis is too late.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import EventError
from .privacy import PrivacyPolicy

__all__ = ["EVENT_TYPES", "Event", "EventLog", "parse_timestamp"]

#: The event taxonomy from ``docs/Metrics-Schema.md``.
EVENT_TYPES: tuple[str, ...] = (
    "experiment_assigned",
    "step_viewed",
    "step_completed",
    "submission_attempted",
    "submission_succeeded",
    "submission_failed",
    "support_contacted",
)

_PSEUDONYM_RE = re.compile(r"^[0-9a-f]{64}$")

#: Payload keys the platform sets itself and a caller may not override.
_RESERVED_PAYLOAD_KEYS = frozenset(
    {"event_type", "unit_pseudonym", "variant", "timestamp", "experiment_id"}
)


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, requiring an explicit timezone.

    Naive timestamps are rejected. A pilot that spans a daylight-saving
    transition, or an agency operating across time zones, will otherwise
    produce duration metrics that are silently wrong by an hour.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise EventError(
            f"timestamp {value!r} is not valid ISO-8601", code="EVENT_BAD_TIMESTAMP"
        ) from exc
    if parsed.tzinfo is None:
        raise EventError(
            f"timestamp {value!r} has no timezone offset. Use an explicit offset "
            "(for example 2026-03-14T09:30:00-07:00) so durations remain correct "
            "across daylight-saving changes.",
            code="EVENT_NAIVE_TIMESTAMP",
        )
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Event:
    """A single recorded outcome."""

    experiment_id: str
    event_type: str
    unit_pseudonym: str
    variant: str
    timestamp: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "event_type": self.event_type,
            "unit_pseudonym": self.unit_pseudonym,
            "variant": self.variant,
            "timestamp": self.timestamp.isoformat(),
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        missing = {
            "experiment_id",
            "event_type",
            "unit_pseudonym",
            "variant",
            "timestamp",
        } - set(data)
        if missing:
            raise EventError(
                f"event is missing required field(s): {', '.join(sorted(missing))}",
                code="EVENT_INCOMPLETE",
            )
        return cls(
            experiment_id=str(data["experiment_id"]),
            event_type=str(data["event_type"]),
            unit_pseudonym=str(data["unit_pseudonym"]),
            variant=str(data["variant"]),
            timestamp=parse_timestamp(str(data["timestamp"])),
            payload=dict(data.get("payload") or {}),
        )


class EventLog:
    """An append-only, validated store of outcome events for one experiment."""

    def __init__(
        self,
        experiment_id: str,
        *,
        path: str | os.PathLike[str] | None = None,
        privacy: PrivacyPolicy | None = None,
        allowed_variants: Iterable[str] | None = None,
    ) -> None:
        self.experiment_id = experiment_id
        self.path = Path(path) if path is not None else None
        self.privacy = privacy or PrivacyPolicy()
        self.allowed_variants = frozenset(allowed_variants) if allowed_variants else None
        self._events: list[Event] = []
        if self.path is not None and self.path.exists():
            self._events = list(self._read_file(self.path))

    # -- validation --------------------------------------------------------

    def validate(self, event: Event) -> None:
        """Check one event against taxonomy, privacy, and referential rules."""
        if event.experiment_id != self.experiment_id:
            raise EventError(
                f"event belongs to experiment {event.experiment_id!r} but this log is "
                f"for {self.experiment_id!r}",
                code="EVENT_WRONG_EXPERIMENT",
            )
        if event.event_type not in EVENT_TYPES:
            raise EventError(
                f"unknown event type {event.event_type!r}. The approved taxonomy is "
                f"{', '.join(EVENT_TYPES)}. Extending it is a governance change, not a "
                "code change: see docs/Metrics-Schema.md.",
                code="EVENT_UNKNOWN_TYPE",
            )
        if not _PSEUDONYM_RE.match(event.unit_pseudonym):
            raise EventError(
                "unit_pseudonym must be a 64-character hex digest produced by "
                "civicexp.privacy.pseudonymize. Raw identifiers must never reach the "
                "event log.",
                code="EVENT_RAW_IDENTIFIER",
            )
        if self.allowed_variants is not None and event.variant not in self.allowed_variants:
            raise EventError(
                f"variant {event.variant!r} is not one of the approved variants "
                f"{sorted(self.allowed_variants)!r}",
                code="EVENT_UNKNOWN_VARIANT",
            )
        overlap = _RESERVED_PAYLOAD_KEYS & set(event.payload)
        if overlap:
            raise EventError(
                f"payload may not redefine platform field(s): {sorted(overlap)!r}",
                code="EVENT_RESERVED_KEY",
            )
        self.privacy.check_payload(event.payload)

    # -- writing -----------------------------------------------------------

    def record(
        self,
        event_type: str,
        unit_pseudonym: str,
        variant: str,
        *,
        timestamp: datetime | str | None = None,
        payload: Mapping[str, Any] | None = None,
        **extra: Any,
    ) -> Event:
        """Validate and append one outcome event.

        Payload fields may be passed as keyword arguments for convenience, or
        in the explicit ``payload`` mapping. The mapping is the only way to
        record a field whose name collides with one of this method's own
        parameters -- and it is also the route by which such a name reaches
        the reserved-key check rather than failing as a Python ``TypeError``.
        """
        fields: dict[str, Any] = dict(payload or {})
        collisions = set(fields) & set(extra)
        if collisions:
            raise EventError(
                f"field(s) {sorted(collisions)!r} given both in payload and as "
                "keyword arguments",
                code="EVENT_DUPLICATE_FIELD",
            )
        fields.update(extra)

        if timestamp is None:
            ts = datetime.now(timezone.utc)
        elif isinstance(timestamp, str):
            ts = parse_timestamp(timestamp)
        elif timestamp.tzinfo is None:
            raise EventError(
                "timestamp must be timezone-aware", code="EVENT_NAIVE_TIMESTAMP"
            )
        else:
            ts = timestamp.astimezone(timezone.utc)

        event = Event(
            experiment_id=self.experiment_id,
            event_type=event_type,
            unit_pseudonym=unit_pseudonym,
            variant=variant,
            timestamp=ts,
            payload=fields,
        )
        self.validate(event)
        self._events.append(event)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        return event

    # -- reading -----------------------------------------------------------

    def _read_file(self, path: Path) -> Iterator[Event]:
        with path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EventError(
                        f"{path}:{lineno} is not valid JSON: {exc.msg}",
                        code="EVENT_CORRUPT_LINE",
                    ) from exc
                event = Event.from_dict(data)
                self.validate(event)
                yield event

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    # -- aggregation -------------------------------------------------------

    def units_by_variant(self) -> dict[str, set[str]]:
        """Distinct units seen in each variant."""
        out: dict[str, set[str]] = {}
        for event in self._events:
            out.setdefault(event.variant, set()).add(event.unit_pseudonym)
        return out

    def count_units_with_event(self, event_type: str) -> dict[str, int]:
        """Per variant, how many distinct units produced ``event_type``.

        Counting *units* rather than *events* is the default throughout this
        platform. A person who retries a failing upload six times is one
        person having a bad experience, not six data points, and rate metrics
        built on raw event counts overstate exactly the problems an agency
        most wants to detect.
        """
        seen: dict[str, set[str]] = {}
        for event in self._events:
            if event.event_type == event_type:
                seen.setdefault(event.variant, set()).add(event.unit_pseudonym)
        return {variant: len(units) for variant, units in seen.items()}

    def durations_by_variant(
        self, start_event: str, end_event: str
    ) -> dict[str, list[float]]:
        """Per-unit elapsed seconds between two events, grouped by variant.

        Uses each unit's earliest ``start_event`` and its earliest subsequent
        ``end_event``. Units that never reach the end event contribute
        nothing: an incomplete application has no completion time, and
        imputing one would bias the metric toward whichever arm abandons more.
        """
        firsts: dict[tuple[str, str], datetime] = {}
        ends: dict[tuple[str, str], datetime] = {}
        for event in self._events:
            key = (event.unit_pseudonym, event.variant)
            if event.event_type == start_event:
                earliest = firsts.get(key)
                if earliest is None or event.timestamp < earliest:
                    firsts[key] = event.timestamp
            elif event.event_type == end_event:
                earliest = ends.get(key)
                if earliest is None or event.timestamp < earliest:
                    ends[key] = event.timestamp

        out: dict[str, list[float]] = {}
        for key, start in firsts.items():
            end = ends.get(key)
            if end is None or end < start:
                continue
            out.setdefault(key[1], []).append((end - start).total_seconds())
        return out

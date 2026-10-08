"""The collection service: assignment and outcome recording for a live pilot.

:mod:`civicexp.assignment` and :mod:`civicexp.events` are the building
blocks; this module is what a running service actually calls. It closes the
gaps the Integration Guide otherwise has to warn engineers about:

* **The entry event is recorded once per person, automatically.** The
  first time an eligible person is assigned, their entry is written with
  the equity attributes the plan declared. Later calls return the same
  version and write nothing.
* **Only the declared equity attributes are kept.** A service passes every
  attribute eligibility needs; the ones that are not pre-registered
  segments are used for the eligibility decision and then discarded. Data
  minimization happens here, not in a policy document.
* **Outcomes must belong to someone in the pilot.** An outcome for a person
  who was never enrolled, or reported against the wrong version, is
  refused. Either one is a bug in the calling service, and accepting it
  would surface weeks later as a sample ratio mismatch.
* **Collection obeys the lifecycle.** When a pilot is not approved to
  collect -- paused, not yet started, finished, or with a governance record
  that fails verification -- assignment answers "serve the current version"
  and outcomes are refused.

The raw identifier a service sends is pseudonymized on arrival and never
written anywhere.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .assignment import Assigner
from .config import ExperimentConfig
from .errors import CivicExpError, EventError
from .events import Event, EventLog, append_event, parse_timestamp
from .privacy import pseudonymize
from .storage import file_lock

__all__ = ["AssignResult", "BatchReport", "Collector", "ENTRY_EVENT"]

#: The event that records a person entering the evaluation.
ENTRY_EVENT = "experiment_assigned"

#: How far in the future a client clock may be before an event is refused.
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class AssignResult:
    """The answer to "which version should this person see?"."""

    #: The arm this person belongs to, or ``None`` if they are not in the
    #: evaluation (ineligible, held back, or collection is not active).
    variant: str | None
    #: What the calling service should render. Always a real variant, so a
    #: service can use it without checking anything else.
    serve: str
    in_analysis: bool
    collecting: bool
    reason: str
    unit_pseudonym: str
    newly_enrolled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant": self.variant,
            "serve": self.serve,
            "in_analysis": self.in_analysis,
            "collecting": self.collecting,
            "reason": self.reason,
            "unit_pseudonym": self.unit_pseudonym,
            "newly_enrolled": self.newly_enrolled,
        }


@dataclass
class BatchReport:
    """Per-row outcome of a batch of events."""

    accepted: int = 0
    rejected: list[tuple[int, str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejected": [
                {"index": index, "code": code, "message": message}
                for index, code, message in self.rejected
            ],
        }


@dataclass
class _Enrollment:
    variant: str
    segments: dict[str, Any]


class Collector:
    """Assignment and outcome recording for one experiment's live event log.

    Thread-safe, and safe to run beside other processes appending to the
    same log (a batch import from the command line, say): appends take a
    file lock, and enrollments written by anyone are picked up by reading
    the log forward from where this collector last stopped.
    """

    def __init__(
        self,
        config: ExperimentConfig,
        events_path: str | Path,
        *,
        gate: Callable[[], tuple[bool, str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.events_path = Path(events_path)
        self._gate = gate or (lambda: (True, "collecting"))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.assigner = Assigner(
            salt=config.salt,
            variants=config.variants,
            eligibility=config.eligibility,
            enrolled_fraction=config.enrolled_fraction,
        )
        self._validator = EventLog(
            config.experiment_id,
            privacy=config.privacy,
            allowed_variants=config.variants,
        )
        self._lock = threading.RLock()
        self._enrolled: dict[str, _Enrollment] = {}
        self._offset = 0
        self.last_event_at: datetime | None = None
        #: Equity attributes are re-attached to the primary metric's
        #: denominator event when it is not the entry event, because that is
        #: where the equity review reads them from.
        self._denominator = config.primary_metric.denominator_event

    # -- reading the log forward -------------------------------------------

    def _refresh(self) -> None:
        """Fold any lines appended since the last read into the enrollment map."""
        try:
            size = self.events_path.stat().st_size
        except FileNotFoundError:
            return
        if size < self._offset:
            # The log was replaced or truncated (a retention deletion, say).
            self._enrolled.clear()
            self._offset = 0
        if size == self._offset:
            return
        with self.events_path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read(size - self._offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # only a partial line so far; wait for the rest
        for raw in chunk[: end + 1].splitlines():
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue  # analysis reports corrupt lines; collection keeps going
            if data.get("event_type") == ENTRY_EVENT:
                self._enrolled.setdefault(
                    str(data.get("unit_pseudonym")),
                    _Enrollment(str(data.get("variant")), dict(data.get("payload") or {})),
                )
            stamp = data.get("timestamp")
            if isinstance(stamp, str):
                try:
                    moment = parse_timestamp(stamp)
                except EventError:
                    continue
                if self.last_event_at is None or moment > self.last_event_at:
                    self.last_event_at = moment
        self._offset += end + 1

    def enrolled_counts(self) -> dict[str, int]:
        """People enrolled so far, per arm."""
        with self._lock:
            self._refresh()
            counts = dict.fromkeys(self.config.variants, 0)
            for enrollment in self._enrolled.values():
                counts[enrollment.variant] = counts.get(enrollment.variant, 0) + 1
            return counts

    # -- assignment ----------------------------------------------------------

    def assign(
        self,
        unit_id: str,
        attributes: Mapping[str, Any] | None = None,
        *,
        timestamp: datetime | str | None = None,
    ) -> AssignResult:
        """Decide which version a person sees, enrolling them on first sight."""
        attrs = dict(attributes or {})
        pseudonym = pseudonymize(str(unit_id), self.config.salt)
        control = self.config.control_variant

        collecting, why_not = self._gate()
        if not collecting:
            return AssignResult(None, control, False, False, why_not, pseudonym)

        with self._lock:
            self._refresh()
            known = self._enrolled.get(pseudonym)
        if known is not None:
            # Once in, always in the same arm: a person's experience does not
            # change because an attribute did.
            return AssignResult(
                known.variant, known.variant, True, True, "already enrolled", pseudonym
            )

        assignment = self.assigner.assign(str(unit_id), attrs)
        if not assignment.in_analysis:
            return AssignResult(None, control, False, True, assignment.reason, pseudonym)

        segments = {
            name: attrs[name]
            for name in self.config.segments
            if name in attrs and attrs[name] is not None
        }
        event = Event(
            experiment_id=self.config.experiment_id,
            event_type=ENTRY_EVENT,
            unit_pseudonym=pseudonym,
            variant=assignment.variant,
            timestamp=self._timestamp(timestamp),
            payload=segments,
        )
        self._validator.validate(event)

        with self._lock, file_lock(self.events_path):
            self._refresh()
            known = self._enrolled.get(pseudonym)
            if known is not None:  # enrolled by a concurrent caller meanwhile
                return AssignResult(
                    known.variant, known.variant, True, True, "already enrolled", pseudonym
                )
            append_event(self.events_path, event)
            self._enrolled[pseudonym] = _Enrollment(assignment.variant, segments)
            self._note_time(event.timestamp)
            self._offset = self.events_path.stat().st_size
        return AssignResult(
            assignment.variant,
            assignment.variant,
            True,
            True,
            assignment.reason,
            pseudonym,
            newly_enrolled=True,
        )

    # -- outcomes ------------------------------------------------------------

    def record(
        self,
        event_type: str,
        *,
        unit_id: str | None = None,
        unit_pseudonym: str | None = None,
        variant: str | None = None,
        timestamp: datetime | str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Event:
        """Record one outcome for a person already in the evaluation."""
        event = self.check(
            event_type,
            unit_id=unit_id,
            unit_pseudonym=unit_pseudonym,
            variant=variant,
            timestamp=timestamp,
            payload=payload,
        )
        with self._lock, file_lock(self.events_path):
            append_event(self.events_path, event)
            self._note_time(event.timestamp)
        return event

    def check(
        self,
        event_type: str,
        *,
        unit_id: str | None = None,
        unit_pseudonym: str | None = None,
        variant: str | None = None,
        timestamp: datetime | str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Event:
        """Everything :meth:`record` checks, without writing anything.

        Returns the event that would be recorded. Used to validate a batch
        import in full before any of it is committed.
        """
        collecting, why_not = self._gate()
        if not collecting:
            raise EventError(
                f"outcomes are not being recorded for {self.config.experiment_id!r}: "
                f"{why_not}",
                code="EVENT_NOT_COLLECTING",
            )
        if event_type == ENTRY_EVENT:
            raise EventError(
                "people enter the evaluation through assignment, which records the "
                "entry event itself; send the outcome events only",
                code="EVENT_USE_ASSIGN",
            )
        if unit_id:
            pseudonym = pseudonymize(str(unit_id), self.config.salt)
        elif unit_pseudonym:
            pseudonym = str(unit_pseudonym)
        else:
            raise EventError(
                "each event needs the person's unit_id (or the unit_pseudonym "
                "returned by assignment)",
                code="EVENT_NO_UNIT",
            )

        with self._lock:
            self._refresh()
            enrollment = self._enrolled.get(pseudonym)
        if enrollment is None:
            raise EventError(
                "this person was never enrolled in the evaluation, so their outcome "
                "cannot be attributed to a version. Call assignment first, and only "
                "send outcomes for people it placed in the evaluation.",
                code="EVENT_UNIT_NOT_ENROLLED",
            )
        if variant is not None and variant != enrollment.variant:
            raise EventError(
                f"the outcome was reported for version {variant!r}, but this person "
                f"was assigned {enrollment.variant!r}. The calling service is showing "
                "or reporting the wrong version.",
                code="EVENT_VARIANT_MISMATCH",
            )

        fields = dict(payload or {})
        if event_type == self._denominator:
            for name, value in enrollment.segments.items():
                fields.setdefault(name, value)
        event = Event(
            experiment_id=self.config.experiment_id,
            event_type=event_type,
            unit_pseudonym=pseudonym,
            variant=enrollment.variant,
            timestamp=self._timestamp(timestamp),
            payload=fields,
        )
        self._validator.validate(event)
        return event

    def record_many(self, items: Iterable[Mapping[str, Any]]) -> BatchReport:
        """Record a batch, reporting each row's fate rather than failing the lot."""
        report = BatchReport()
        for index, item in enumerate(items):
            try:
                if not isinstance(item, Mapping):
                    raise EventError("each item must be an object", code="EVENT_SHAPE")
                self.record(
                    str(item.get("event_type", "")),
                    unit_id=item.get("unit_id"),
                    unit_pseudonym=item.get("unit_pseudonym"),
                    variant=item.get("variant"),
                    timestamp=item.get("timestamp"),
                    payload=item.get("payload"),
                )
            except CivicExpError as exc:
                report.rejected.append((index, exc.code, exc.message))
            else:
                report.accepted += 1
        return report

    # -- helpers -------------------------------------------------------------

    def _timestamp(self, value: datetime | str | None) -> datetime:
        now = self._clock()
        if value is None or value == "":
            return now.astimezone(timezone.utc)
        if isinstance(value, str):
            moment = parse_timestamp(value)
        elif value.tzinfo is None:
            raise EventError("timestamp must be timezone-aware", code="EVENT_NAIVE_TIMESTAMP")
        else:
            moment = value.astimezone(timezone.utc)
        if moment > now + _CLOCK_SKEW:
            raise EventError(
                f"timestamp {moment.isoformat()} is in the future. Check the calling "
                "service's clock and time zone.",
                code="EVENT_FUTURE_TIMESTAMP",
            )
        return moment

    def _note_time(self, moment: datetime) -> None:
        if self.last_event_at is None or moment > self.last_event_at:
            self.last_event_at = moment

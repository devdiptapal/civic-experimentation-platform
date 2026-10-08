"""Batch mode: run an evaluation from spreadsheet exports, with no live integration.

Many county agencies cannot change a production system for a pilot, but
every one of them can export a spreadsheet. For service communications --
reminders, renewal notices, requests for missing information -- that is
enough to run a rigorous evaluation:

1. **Assign.** Export the people due a message, one row each, with an
   identifier column and the attributes eligibility and the equity review
   need. :func:`assign_batch` decides each person's version, enrolls them,
   and returns a list for the mail-merge or text-message vendor saying
   which version each person gets.
2. **Send.** The vendor sends each version as listed.
3. **Import outcomes.** Export what happened (identifier, outcome, when).
   :func:`import_outcomes` pseudonymizes each row, attributes it to the
   person's version, and records it -- refusing rows for anyone who was
   never enrolled.

The identifier column is read, pseudonymized, and dropped. The only file the
platform writes that contains it is the vendor list, which the agency needs
in order to send the messages and should handle exactly like the export it
came from.

:func:`profile_columns` helps with the first mapping: it lists each column,
how many distinct values it holds, and whether it looks like an identifier,
a timestamp, or a coded category -- without printing values from any column
that could identify someone.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .collect import Collector
from .errors import CivicExpError, EventError
from .events import EVENT_TYPES
from .privacy import PII_FIELD_PATTERNS

__all__ = [
    "AssignBatchReport",
    "ImportReport",
    "assign_batch",
    "coerce_value",
    "import_outcomes",
    "parse_event_map",
    "profile_columns",
    "read_csv_text",
    "resolve_timezone",
    "write_csv_text",
]

_PII = tuple(re.compile(p, re.IGNORECASE) for p in PII_FIELD_PATTERNS)
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")
#: Columns with at most this many distinct values are treated as coded
#: categories, whose values are safe to show while mapping.
_CATEGORY_LIMIT = 20


def read_csv_text(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse CSV text, tolerating the byte-order mark spreadsheet tools add."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows = [dict(row) for row in reader]
    return list(reader.fieldnames or []), rows


def write_csv_text(fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    return out.getvalue()


def coerce_value(text: Any) -> Any:
    """Turn a spreadsheet cell into the value an eligibility rule expects.

    Spreadsheets hold only text, but eligibility rules compare booleans and
    numbers: ``staff_assisted eq true`` must match a cell reading TRUE. An
    empty cell is absent, never false -- absence is not silently treated
    as a value anywhere in this platform.
    """
    if text is None:
        return None
    value = str(text).strip()
    if value == "":
        return None
    lowered = value.lower()
    if lowered in ("true", "yes", "y"):
        return True
    if lowered in ("false", "no", "n"):
        return False
    if re.fullmatch(r"-?\d+", value) and not (len(value) > 1 and value.lstrip("-").startswith("0")):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def resolve_timezone(name: str | None) -> Any:
    """A fixed offset like ``-08:00``, or an IANA zone like ``America/Los_Angeles``.

    A named zone is preferred for local timestamps, because it knows when
    daylight saving starts and ends; a fixed offset will be wrong by an
    hour for part of the year.
    """
    if not name:
        return None
    match = _OFFSET_RE.match(name.strip())
    if match:
        sign = -1 if match.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name.strip())
    except Exception as exc:  # ZoneInfoNotFoundError, or no tz database
        raise CivicExpError(
            f"unknown time zone {name!r}. Use a name like America/Los_Angeles, or a "
            "fixed offset like -08:00. On Windows, named zones need the tzdata package.",
            code="CSV_BAD_TIMEZONE",
        ) from exc


def parse_event_map(text: str | None) -> dict[str, str]:
    """``"Renewed=submission_succeeded,Called=support_contacted"`` as a mapping."""
    mapping: dict[str, str] = {}
    for part in (text or "").split(","):
        if not part.strip():
            continue
        label, sep, event = part.partition("=")
        if not sep or event.strip() not in EVENT_TYPES:
            raise CivicExpError(
                f"event map entry {part.strip()!r} must look like LABEL=event_type, "
                f"with event_type one of {', '.join(EVENT_TYPES)}",
                code="CSV_BAD_EVENT_MAP",
            )
        mapping[label.strip()] = event.strip()
    return mapping


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


@dataclass
class AssignBatchReport:
    rows: int = 0
    newly_enrolled: dict[str, int] = field(default_factory=dict)
    already_enrolled: int = 0
    not_in_evaluation: int = 0
    problems: list[tuple[int, str]] = field(default_factory=list)


def assign_batch(
    collector: Collector,
    rows: Iterable[Mapping[str, str]],
    *,
    id_column: str,
    attribute_columns: Sequence[str] | None = None,
) -> tuple[AssignBatchReport, list[dict[str, str]]]:
    """Assign every row, returning a report and the vendor list.

    The vendor list has the identifier, the version to send, and whether the
    person is in the evaluation. People who are not (ineligible, or held
    back by a ramp) get the current version, exactly as a live service
    would show them.
    """
    report = AssignBatchReport()
    output: list[dict[str, str]] = []
    for line, row in enumerate(rows, start=2):
        report.rows += 1
        unit_id = str(row.get(id_column) or "").strip()
        if not unit_id:
            report.problems.append((line, f"no value in the {id_column!r} column"))
            continue
        columns = attribute_columns or [c for c in row if c != id_column]
        attributes = {c: coerce_value(row.get(c)) for c in columns}
        attributes = {k: v for k, v in attributes.items() if v is not None}
        try:
            result = collector.assign(unit_id, attributes)
        except CivicExpError as exc:
            report.problems.append((line, exc.message))
            continue
        if not result.collecting:
            raise CivicExpError(
                f"this evaluation is not collecting ({result.reason}); nothing was "
                "assigned",
                code="CSV_NOT_COLLECTING",
            )
        if result.newly_enrolled and result.variant:
            report.newly_enrolled[result.variant] = report.newly_enrolled.get(result.variant, 0) + 1
        elif result.in_analysis:
            report.already_enrolled += 1
        else:
            report.not_in_evaluation += 1
        output.append({
            id_column: unit_id,
            "version": result.serve,
            "in_evaluation": "yes" if result.in_analysis else "no",
        })
    return report, output


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass
class ImportReport:
    rows: int = 0
    accepted: int = 0
    rejected: list[tuple[int, str, str]] = field(default_factory=list)
    unmapped: dict[str, int] = field(default_factory=dict)

    def rejected_by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, code, _ in self.rejected:
            counts[code] = counts.get(code, 0) + 1
        return counts


def _iso(text: str) -> str:
    """ISO text that Python 3.10's ``fromisoformat`` accepts."""
    if "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return text


def _timestamp(value: str, zone: Any) -> datetime:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(_iso(text))
    except ValueError as exc:
        raise EventError(
            f"{value!r} is not a date and time this tool can read; use ISO-8601, for "
            "example 2026-03-14T09:30:00",
            code="EVENT_BAD_TIMESTAMP",
        ) from exc
    if parsed.tzinfo is None:
        if zone is None:
            raise EventError(
                f"{value!r} has no time zone. Pass the zone the export uses, for "
                "example --timezone America/Los_Angeles.",
                code="EVENT_NAIVE_TIMESTAMP",
            )
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc)


def import_outcomes(
    collector: Collector,
    rows: Iterable[Mapping[str, str]],
    *,
    id_column: str,
    event_column: str,
    timestamp_column: str,
    event_map: Mapping[str, str] | None = None,
    zone: Any = None,
    dry_run: bool = False,
) -> ImportReport:
    """Record each outcome row against the person's assigned version.

    Rows whose outcome is not in the event map (and is not itself an event
    name) are counted as unmapped and skipped, so an export that carries
    unrelated activity can be imported as it is. With ``dry_run``, every row
    is checked and nothing is written.
    """
    report = ImportReport()
    mapping = dict(event_map or {})
    for line, row in enumerate(rows, start=2):
        report.rows += 1
        label = str(row.get(event_column) or "").strip()
        event_type = mapping.get(label, label if label in EVENT_TYPES else None)
        if event_type is None:
            report.unmapped[label] = report.unmapped.get(label, 0) + 1
            continue
        unit_id = str(row.get(id_column) or "").strip()
        try:
            if not unit_id:
                raise EventError(f"no value in the {id_column!r} column", code="EVENT_NO_UNIT")
            moment = _timestamp(str(row.get(timestamp_column) or ""), zone)
            if dry_run:
                collector.check(event_type, unit_id=unit_id, timestamp=moment)
            else:
                collector.record(event_type, unit_id=unit_id, timestamp=moment)
        except CivicExpError as exc:
            report.rejected.append((line, exc.code, exc.message))
        else:
            report.accepted += 1
    return report


# ---------------------------------------------------------------------------
# Profiling, to help map an unfamiliar export
# ---------------------------------------------------------------------------


def profile_columns(
    fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> list[dict[str, Any]]:
    """Describe each column well enough to map it, without exposing identifiers."""
    profiles = []
    for name in fieldnames:
        values = [str(r.get(name) or "").strip() for r in rows]
        present = [v for v in values if v]
        distinct = sorted(set(present))
        named_like_pii = any(p.search(name) for p in _PII)
        kind = "category"
        if present and all(_looks_like_time(v) for v in present[:50]):
            kind = "timestamp"
        elif named_like_pii or (len(distinct) > _CATEGORY_LIMIT and len(distinct) > 0.5 * len(present)):
            kind = "identifier"
        elif len(distinct) > _CATEGORY_LIMIT:
            kind = "free values"
        profiles.append({
            "column": name,
            "filled": len(present),
            "distinct": len(distinct),
            "kind": kind,
            "named_like_personal_data": named_like_pii,
            # Values only for small coded categories: an identifier, or a
            # column with many distinct values, could name someone.
            "values": distinct if kind == "category" else [],
        })
    return profiles


def _looks_like_time(value: str) -> bool:
    try:
        datetime.fromisoformat(_iso(value))
    except ValueError:
        return False
    return True

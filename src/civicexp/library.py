"""The evidence library: published results other agencies can find and reuse.

The point of shared infrastructure is that a result established in one
county does not have to be rediscovered in the next. The readout serves the
agency that ran the pilot; the case summary (:func:`civicexp.report.
render_case_summary`) serves a person in another agency reading it. This
module serves the *software* in another agency: a case record is the same
finding in a versioned, machine-readable form, so a library of them can be
searched, compared, and shown to a team at the moment it is planning the
same test.

Three rules keep the library trustworthy:

* **Negative results are published in exactly the same form as positive
  ones.** A change that did not work is evidence another agency can use,
  and a library of only successes is a biased one.
* **Nothing below the disclosure threshold is published.** A record is
  refused if either arm is smaller than the experiment's suppression
  threshold, and equity findings carry outcomes and effects, never the
  size of a group.
* **Comparability is explicit.** Each metric records whether it used a
  standard definition (:func:`civicexp.metrics.standard_metric_for`), so a
  reader knows whether two agencies' numbers measure the same thing.

Records imported from elsewhere are data, not instructions: they are
validated against the schema and rendered as plain text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import CivicExpError, PrivacyViolation
from .metrics import standard_metric_for
from .stats import MeanComparison, ProportionComparison
from .storage import atomic_write_text, file_lock

__all__ = [
    "CASE_SCHEMA",
    "LibraryError",
    "CaseRecord",
    "EvidenceLibrary",
    "build_case_record",
    "validate_case_record",
]

#: Version of the case record format. Bumped on any change a reader of an
#: older record would misinterpret.
CASE_SCHEMA = "civicexp.case/1"

_DECISIONS = ("promote", "iterate", "rollback", "inconclusive", "invalid")
_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")


class LibraryError(CivicExpError):
    code = "LIBRARY_ERROR"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:60] or "case"


def _comparison_fields(comparison: Any) -> dict[str, Any]:
    if isinstance(comparison, ProportionComparison):
        return {
            "control_rate": round(comparison.control_rate, 6),
            "treatment_rate": round(comparison.treatment_rate, 6),
            "difference": round(comparison.absolute_difference, 6),
            "ci": [round(v, 6) for v in comparison.difference_interval],
        }
    if isinstance(comparison, MeanComparison):
        return {
            "control_mean": round(comparison.control_mean, 3),
            "treatment_mean": round(comparison.treatment_mean, 3),
            "difference": round(comparison.difference, 3),
            "ci": [round(v, 3) for v in comparison.difference_interval],
        }
    return {}


def build_case_record(
    result: Any,
    *,
    raw_config: Mapping[str, Any] | None = None,
    jurisdiction: str = "",
    synthetic: bool = False,
    published: datetime | None = None,
    platform_version: str = "",
) -> dict[str, Any]:
    """Turn an analysis result into a publishable case record.

    Raises :class:`~civicexp.errors.PrivacyViolation` if either arm is below
    the disclosure threshold.
    """
    config = result.config
    primary = result.primary
    threshold = config.privacy.suppression_threshold
    sizes = (
        (primary.control_trials, primary.treatment_trials)
        if isinstance(primary, ProportionComparison)
        else (primary.control_n, primary.treatment_n)
    )
    if min(sizes) < threshold:
        raise PrivacyViolation(
            f"cannot publish: an arm has fewer than {threshold} people, the "
            "disclosure threshold this experiment was approved under",
            code="PRIVACY_SMALL_CELL",
        )

    raw = raw_config or {}
    scope = raw.get("scope") or {}
    variants = raw.get("variants") or {}
    metric = config.primary_metric
    stamp = (published or datetime.now(timezone.utc)).date().isoformat()
    record: dict[str, Any] = {
        "schema": CASE_SCHEMA,
        "case_id": f"{_slug(jurisdiction or 'unattributed')}--{_slug(config.experiment_id)}",
        "title": config.name,
        "jurisdiction": jurisdiction,
        "service_area": config.service_area,
        "workflow": scope.get("workflow"),
        "published": stamp,
        "synthetic": bool(synthetic),
        "platform_version": platform_version,
        "plan_fingerprint": config.fingerprint,
        "what_was_tested": config.description,
        "hypothesis": str((raw.get("hypothesis") or {}).get("statement", "")),
        "versions": {
            name: str((variants.get(name) or {}).get("description", ""))
            for name in (result.control_variant, result.treatment_variant)
        },
        "duration_days": config.planned_duration_days,
        "decision": result.decision.value,
        "decision_headline": result.decision.headline,
        "rationale": list(result.rationale),
        "primary": {
            "metric": metric.name,
            "standard_metric": standard_metric_for(metric),
            "definition": metric.definition,
            "target_direction": metric.target_direction,
            "minimum_effect_of_interest": metric.minimum_effect_of_interest,
            "confidence": config.decision.confidence,
            "control_n": sizes[0],
            "treatment_n": sizes[1],
            **_comparison_fields(primary),
        },
        "guardrails": [
            {
                "metric": g.name,
                "standard_metric": standard_metric_for(
                    next((s for s in config.guardrails if s.name == g.name), None)
                ),
                "kind": g.kind,
                "status": g.status,
                "tolerance": g.tolerance,
                **_comparison_fields(g.comparison),
            }
            for g in result.guardrails
        ],
        "equity": None,
        "trust_checks": [
            {"check": d.name, "result": d.severity.value, "summary": d.summary}
            for d in (result.diagnostics or ())
        ],
        "caveats": list(result.warnings),
    }
    if result.equity is not None and result.equity.findings:
        record["equity"] = {
            "summary": result.equity.summary_line(),
            "segments_declared": list(config.segments),
            "findings": [
                {
                    "segment": f.label,
                    "outcome": f.outcome.value,
                    "effect": None if f.effect is None else round(f.effect, 6),
                }
                for f in result.equity.findings
            ],
        }
    return record


def validate_case_record(data: Any) -> list[str]:
    """Every problem with a case record, or an empty list if it is usable."""
    if not isinstance(data, Mapping):
        return ["a case record must be a JSON object"]
    problems: list[str] = []
    if data.get("schema") != CASE_SCHEMA:
        problems.append(
            f"schema is {data.get('schema')!r}; this library reads {CASE_SCHEMA!r}"
        )
    for key in ("case_id", "title", "decision", "published"):
        if not isinstance(data.get(key), str) or not data.get(key):
            problems.append(f"{key} is required and must be text")
    case_id = data.get("case_id")
    if isinstance(case_id, str) and not _CASE_ID_RE.match(case_id):
        problems.append("case_id may contain only lowercase letters, digits, '.', '_', '-'")
    if data.get("decision") not in _DECISIONS:
        problems.append(f"decision must be one of {', '.join(_DECISIONS)}")
    primary = data.get("primary")
    if not isinstance(primary, Mapping) or not isinstance(primary.get("metric"), str):
        problems.append("primary must describe the main measure, including its name")
    if not isinstance(data.get("synthetic"), bool):
        problems.append("synthetic must be true or false")
    return problems


@dataclass(frozen=True)
class CaseRecord:
    """A validated case record, with convenient accessors for display."""

    data: Mapping[str, Any]

    @property
    def case_id(self) -> str:
        return str(self.data["case_id"])

    @property
    def title(self) -> str:
        return str(self.data["title"])

    @property
    def decision(self) -> str:
        return str(self.data["decision"])

    @property
    def synthetic(self) -> bool:
        return bool(self.data.get("synthetic"))

    @property
    def workflow(self) -> str | None:
        value = self.data.get("workflow")
        return str(value) if value else None

    @property
    def service_area(self) -> str:
        return str(self.data.get("service_area") or "")

    @property
    def jurisdiction(self) -> str:
        return str(self.data.get("jurisdiction") or "")

    def matches(self, text: str) -> bool:
        haystack = " ".join(
            str(self.data.get(k) or "")
            for k in ("title", "jurisdiction", "service_area", "workflow", "what_was_tested")
        ).lower()
        return all(term in haystack for term in text.lower().split())


class EvidenceLibrary:
    """A directory of case records, each with an optional Markdown summary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, case_id: str, suffix: str) -> Path:
        if not _CASE_ID_RE.match(case_id):
            raise LibraryError(f"invalid case id {case_id!r}", code="LIBRARY_BAD_ID")
        return self.root / f"{case_id}{suffix}"

    def list(self) -> list[CaseRecord]:
        if not self.root.exists():
            return []
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not validate_case_record(data):
                records.append(CaseRecord(data))
        records.sort(key=lambda r: (str(r.data.get("published", "")), r.case_id), reverse=True)
        return records

    def get(self, case_id: str) -> CaseRecord | None:
        path = self._path(case_id, ".json")
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return None if validate_case_record(data) else CaseRecord(data)

    def summary_markdown(self, case_id: str) -> str | None:
        path = self._path(case_id, ".md")
        return path.read_text(encoding="utf-8") if path.exists() else None

    def add(
        self,
        record: Mapping[str, Any],
        *,
        markdown: str | None = None,
        replace: bool = False,
    ) -> CaseRecord:
        problems = validate_case_record(record)
        if problems:
            raise LibraryError(
                "this is not a usable case record: " + "; ".join(problems),
                code="LIBRARY_INVALID_RECORD",
            )
        case_id = str(record["case_id"])
        json_path = self._path(case_id, ".json")
        with file_lock(json_path):
            if json_path.exists() and not replace:
                raise LibraryError(
                    f"a case with id {case_id!r} is already in the library",
                    code="LIBRARY_DUPLICATE",
                )
            atomic_write_text(json_path, json.dumps(record, indent=2, sort_keys=True) + "\n")
            md_path = self._path(case_id, ".md")
            if markdown is not None:
                atomic_write_text(md_path, markdown)
        return CaseRecord(dict(record))

    def import_json(self, text: str, *, replace: bool = False) -> CaseRecord:
        """Add a record received from another agency, as JSON text."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LibraryError(
                f"not valid JSON: {exc.msg} (line {exc.lineno})",
                code="LIBRARY_INVALID_RECORD",
            ) from exc
        return self.add(data, replace=replace)

    def search(
        self,
        text: str = "",
        *,
        decision: str = "",
        workflow: str = "",
        include_synthetic: bool = True,
    ) -> list[CaseRecord]:
        found = []
        for record in self.list():
            if text and not record.matches(text):
                continue
            if decision and record.decision != decision:
                continue
            if workflow and record.workflow != workflow:
                continue
            if not include_synthetic and record.synthetic:
                continue
            found.append(record)
        return found

    def evidence_for(
        self, *, workflow: str | None = None, service_area: str = ""
    ) -> list[CaseRecord]:
        """Published cases about the same workflow, for a team planning a test."""
        records = self.list()
        if workflow:
            return [r for r in records if r.workflow == workflow]
        if service_area:
            return [r for r in records if r.service_area == service_area]
        return []


def tally(records: Iterable[CaseRecord]) -> dict[str, int]:
    """How many records reached each decision."""
    counts: dict[str, int] = {}
    for record in records:
        counts[record.decision] = counts.get(record.decision, 0) + 1
    return counts

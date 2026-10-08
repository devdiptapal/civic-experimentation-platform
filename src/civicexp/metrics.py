"""A library of standard, pre-defined service metrics.

An agency should not have to invent a definition of "completion rate", and
two agencies measuring it differently cannot compare results -- which
defeats the point of shared infrastructure. This module holds one agreed
definition for each of the outcomes public-service pilots actually measure,
so that a configuration can reference a metric by name instead of restating
it:

.. code-block:: json

    "primary": { "use": "completion_rate", "minimum_effect_of_interest": 0.03 }

That expands to the full definition below. An agency that needs different
semantics can still write the metric out longhand; referencing the standard
one is a statement that the result is comparable with everyone else's.

The metrics here cover the service outcomes named in the project roadmap:
online abandonment, time to complete, successful first-time submission,
applications returned for missing information, and processing time.

Some outcomes are defined by something *not* happening -- abandonment is
entering and never submitting. Those use ``numerator_excludes``: count the
units that produced the numerator event and none of the excluded ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "StandardMetric",
    "STANDARD_METRICS",
    "expand_metric",
    "describe_library",
    "format_effect",
    "standard_metric_for",
]


@dataclass(frozen=True)
class StandardMetric:
    """A reusable metric definition with a fixed, documented meaning."""

    name: str
    definition: str
    kind: str
    numerator_event: str
    denominator_event: str
    target_direction: str
    #: Why this metric matters operationally, in plain language.
    why: str
    #: A sensible starting point for the smallest change worth acting on.
    #: Always a starting point for discussion, never a default that is
    #: silently applied -- the config must state its own.
    typical_effect_of_interest: float
    #: Units that produced any of these events are removed from the
    #: numerator. How "entered and did not submit" is expressed.
    numerator_excludes: tuple[str, ...] = ()

    def to_config(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "name": self.name,
            "definition": self.definition,
            "kind": self.kind,
            "numerator_event": self.numerator_event,
            "denominator_event": self.denominator_event,
            "target_direction": self.target_direction,
        }
        if self.numerator_excludes:
            base["numerator_excludes"] = list(self.numerator_excludes)
        base.update(overrides)
        return base


STANDARD_METRICS: Mapping[str, StandardMetric] = {
    "completion_rate": StandardMetric(
        name="completion_rate",
        definition=(
            "Share of people who entered the workflow and successfully submitted, "
            "counted once per person."
        ),
        kind="proportion",
        numerator_event="submission_succeeded",
        denominator_event="experiment_assigned",
        target_direction="increase",
        why=(
            "The headline measure of whether people can finish the task at all. "
            "Every person who does not complete is someone who needed the service "
            "and did not get it."
        ),
        typical_effect_of_interest=0.03,
    ),
    "abandonment_rate": StandardMetric(
        name="abandonment_rate",
        definition=(
            "Share of people who entered the workflow and never submitted "
            "successfully, counted once per person."
        ),
        kind="proportion",
        numerator_event="experiment_assigned",
        numerator_excludes=("submission_succeeded",),
        denominator_event="experiment_assigned",
        target_direction="decrease",
        why=(
            "The mirror of completion, stated the way program staff usually "
            "experience the problem. Use whichever framing the service owner "
            "thinks in; do not use both as separate metrics, because they are "
            "the same measurement."
        ),
        typical_effect_of_interest=0.03,
    ),
    "first_time_success_rate": StandardMetric(
        name="first_time_success_rate",
        definition=(
            "Share of people who attempted a submission and succeeded without "
            "any failed attempt, counted once per person."
        ),
        kind="proportion",
        numerator_event="submission_succeeded",
        numerator_excludes=("submission_failed",),
        denominator_event="submission_attempted",
        target_direction="increase",
        why=(
            "Distinguishes a service people can use from one they can eventually "
            "beat. Repeat attempts drive both applicant frustration and the "
            "rework burden on staff, and are an upstream cause of incorrect "
            "submissions."
        ),
        typical_effect_of_interest=0.05,
    ),
    "error_rate": StandardMetric(
        name="error_rate",
        definition=(
            "Share of people who hit a blocking error, counted once per person."
        ),
        kind="proportion",
        numerator_event="submission_failed",
        denominator_event="experiment_assigned",
        target_direction="decrease",
        why=(
            "The standard guardrail. A change that improves completion while "
            "increasing errors is usually pushing people through a flow that is "
            "failing them in a way the completion number does not show."
        ),
        typical_effect_of_interest=0.01,
    ),
    "support_contact_rate": StandardMetric(
        name="support_contact_rate",
        definition=(
            "Share of people who contacted support during the workflow, counted "
            "once per person."
        ),
        kind="proportion",
        numerator_event="support_contacted",
        denominator_event="experiment_assigned",
        target_direction="decrease",
        why=(
            "A direct measure of whether the service explains itself, and the one "
            "that converts most legibly into staff workload. A change that shifts "
            "burden from the applicant to the call centre has not reduced burden."
        ),
        typical_effect_of_interest=0.02,
    ),
    "time_to_complete": StandardMetric(
        name="time_to_complete",
        definition=(
            "Seconds from entering the workflow to a successful submission, for "
            "people who completed."
        ),
        kind="duration",
        numerator_event="submission_succeeded",
        denominator_event="experiment_assigned",
        target_direction="decrease",
        why=(
            "Applicant time is a real cost, usually unpaid and often taken from "
            "work hours. Measured only over people who completed, so read it "
            "alongside completion rather than on its own: a change that makes the "
            "slowest people give up will improve this number."
        ),
        typical_effect_of_interest=60.0,
    ),
    "returned_for_information_rate": StandardMetric(
        name="returned_for_information_rate",
        definition=(
            "Share of successful submissions later returned to the applicant for "
            "missing or incorrect information, counted once per person."
        ),
        kind="proportion",
        numerator_event="submission_returned",
        denominator_event="submission_succeeded",
        target_direction="decrease",
        why=(
            "A submission that comes back for missing information looks like a "
            "success at the front door and is a failure in the back office. It "
            "costs caseworker time, delays the applicant, and is a common route "
            "to a procedural denial for someone who was eligible."
        ),
        typical_effect_of_interest=0.02,
    ),
    "time_to_process": StandardMetric(
        name="time_to_process",
        definition=(
            "Seconds from a successful submission to the agency finishing "
            "processing it, for submissions processed within the pilot window. "
            "Records that processing finished, never what was decided."
        ),
        kind="duration",
        numerator_event="processing_completed",
        denominator_event="submission_succeeded",
        target_direction="decrease",
        why=(
            "Faster processing is faster access to the benefit. A front-end "
            "change that produces cleaner submissions should show up here; one "
            "that pushes incomplete applications through faster will show up "
            "as a longer back-office queue."
        ),
        typical_effect_of_interest=86400.0,
    ),
}


def format_effect(value: float, kind: str) -> str:
    """Render an effect size in the unit a program manager thinks in."""
    if kind != "duration":
        return f"{value * 100:.1f} percentage points"
    seconds = abs(value)
    if seconds >= 86400:
        return f"{value / 86400:.1f} days"
    if seconds >= 3600:
        return f"{value / 3600:.1f} hours"
    if seconds >= 120:
        return f"{value / 60:.1f} minutes"
    return f"{value:.0f} seconds"


def standard_metric_for(spec: Any) -> str | None:
    """The standard metric a config's metric is identical to, if any.

    A result is only comparable with another agency's when the definition
    matches exactly, so this checks every field that affects the number,
    not only the name.
    """
    standard = STANDARD_METRICS.get(getattr(spec, "name", ""))
    if standard is None:
        return None
    same = (
        spec.kind == standard.kind
        and spec.numerator_event == standard.numerator_event
        and spec.denominator_event == standard.denominator_event
        and tuple(getattr(spec, "numerator_excludes", ())) == standard.numerator_excludes
    )
    return standard.name if same else None


def expand_metric(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Expand a ``{"use": "<name>", ...}`` reference into a full definition.

    Any key given alongside ``use`` overrides the standard definition, so an
    agency can adopt the shared meaning while setting its own effect of
    interest. A spec without ``use`` is returned unchanged.
    """
    if "use" not in spec:
        return dict(spec)

    name = str(spec["use"])
    if name not in STANDARD_METRICS:
        available = ", ".join(sorted(STANDARD_METRICS))
        raise KeyError(
            f"unknown standard metric {name!r}; available metrics are {available}"
        )

    overrides = {k: v for k, v in spec.items() if k != "use"}
    return STANDARD_METRICS[name].to_config(**overrides)


def describe_library() -> str:
    """A plain-language listing, for ``civicexp metrics``."""
    lines = ["Standard metrics available via \"use\": in a configuration.", ""]
    for metric in STANDARD_METRICS.values():
        arrow = "higher is better" if metric.target_direction == "increase" else "lower is better"
        unit = "seconds" if metric.kind == "duration" else "proportion"
        lines.append(f"  {metric.name}  ({unit}, {arrow})")
        lines.append(f"    {metric.definition}")
        lines.append(f"    Why it matters: {metric.why}")
        if metric.kind == "duration":
            starting = format_effect(metric.typical_effect_of_interest, "duration")
        else:
            starting = f"{metric.typical_effect_of_interest:.0%}"
        lines.append(f"    Starting point for discussion: {starting}")
        lines.append("")
    lines.append(
        "Referencing a standard metric means your result is comparable with every"
    )
    lines.append("other agency that used the same one. Example:")
    lines.append('  "primary": {"use": "completion_rate", "minimum_effect_of_interest": 0.03}')
    return "\n".join(lines)

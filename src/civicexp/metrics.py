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
online abandonment, time to complete, and successful first-time submission.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["StandardMetric", "STANDARD_METRICS", "expand_metric", "describe_library"]


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

    def to_config(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "name": self.name,
            "definition": self.definition,
            "kind": self.kind,
            "numerator_event": self.numerator_event,
            "denominator_event": self.denominator_event,
            "target_direction": self.target_direction,
        }
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
            "Share of people who entered the workflow and never submitted, "
            "counted once per person."
        ),
        kind="proportion",
        numerator_event="step_viewed",
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
            "Share of people who submitted successfully without any failed attempt "
            "beforehand, counted once per person."
        ),
        kind="proportion",
        numerator_event="submission_succeeded",
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
}


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
            starting = f"{metric.typical_effect_of_interest:.0f} seconds"
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

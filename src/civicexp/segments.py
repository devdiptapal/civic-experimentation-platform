"""Did the change help everyone, or only some people?

``GOVERNANCE.md`` commits every pilot to "assess potential unequal impact
across participant groups". This module is where that commitment becomes a
computation rather than a promise.

An overall improvement can hide a group it made worse. A plain-language
rewrite that helps English speakers can leave a translated flow untouched or
worse, and the aggregate still looks like a win because that group is
smaller. For a public benefits workflow this is not a statistical footnote:
it is the difference between narrowing an access gap and widening one while
reporting success.

Two design choices make this trustworthy rather than a fishing expedition:

**Segments must be declared in the configuration before launch.** Searching
for a subgroup where the result looks good -- or bad -- after seeing the
data will always find one. Only pre-registered segments are analyzed.

**Every comparison is corrected for multiplicity.** Testing eight segments
at 5% each produces a spurious finding about a third of the time. Segment
results are corrected with Holm-Bonferroni across the family, and a segment
that does not survive correction is reported as something to watch, not as a
finding.

The output deliberately distinguishes three things an agency should treat
differently: a group the change *harmed*, a group it simply did not *reach*,
and a group where the pilot had too few people to tell.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from .stats import ProportionComparison, compare_proportions, holm_bonferroni

__all__ = ["SegmentFinding", "SegmentOutcome", "SegmentAnalysis", "analyze_segments"]


class SegmentOutcome(str, Enum):
    """What a segment's result means for the decision."""

    #: The change helped this group, by a margin that survives correction.
    HELPED = "helped"
    #: The change harmed this group. Overrides an overall win.
    HARMED = "harmed"
    #: No detectable difference for this group.
    NO_EFFECT = "no_effect"
    #: Too few people in this group to say anything.
    TOO_FEW = "too_few"
    #: Suggestive, but does not survive correction for multiple segments.
    WATCH = "watch"

    @property
    def is_equity_concern(self) -> bool:
        return self in (SegmentOutcome.HARMED, SegmentOutcome.WATCH)


@dataclass(frozen=True)
class SegmentFinding:
    """The result for one group within one segment dimension."""

    dimension: str
    value: str
    outcome: SegmentOutcome
    comparison: ProportionComparison | None
    control_n: int
    treatment_n: int
    note: str

    @property
    def effect(self) -> float | None:
        return None if self.comparison is None else self.comparison.absolute_difference

    @property
    def label(self) -> str:
        return f"{self.dimension}={self.value}"


@dataclass(frozen=True)
class SegmentAnalysis:
    """All pre-registered segment findings for one experiment."""

    findings: tuple[SegmentFinding, ...]
    overall_effect: float
    suppressed_count: int = 0

    @property
    def harmed(self) -> tuple[SegmentFinding, ...]:
        return tuple(f for f in self.findings if f.outcome is SegmentOutcome.HARMED)

    @property
    def watch(self) -> tuple[SegmentFinding, ...]:
        return tuple(f for f in self.findings if f.outcome is SegmentOutcome.WATCH)

    @property
    def not_reached(self) -> tuple[SegmentFinding, ...]:
        """Groups the change measurably failed to help, where it helped overall."""
        if self.overall_effect <= 0:
            return ()
        return tuple(
            f for f in self.findings if f.outcome is SegmentOutcome.NO_EFFECT
        )

    @property
    def has_equity_concern(self) -> bool:
        return bool(self.harmed or self.watch)

    def summary_line(self) -> str:
        if not self.findings:
            return "No segments were declared for equity review."
        if self.harmed:
            names = ", ".join(f.label for f in self.harmed)
            return f"Equity concern: the change harmed {names}."
        if self.watch:
            names = ", ".join(f.label for f in self.watch)
            return (
                f"Possible equity concern worth monitoring: {names}. Not confirmed "
                "after correcting for the number of groups examined."
            )
        if self.not_reached:
            names = ", ".join(f.label for f in self.not_reached)
            return (
                f"The change helped overall but showed no measurable benefit for "
                f"{names}. It did not harm them; it did not reach them."
            )
        return "No group was harmed, and no group was left behind by a detectable margin."


def analyze_segments(
    config,
    log,
    control: str,
    treatment: str,
    *,
    dimensions: Sequence[str] | None = None,
) -> SegmentAnalysis:
    """Evaluate the effect within each pre-registered segment.

    Segment values are read from the payload of the metric's denominator
    event, which is where an integrator records the coded attributes
    (preferred language, channel, region) captured at assignment time.
    """
    metric = config.primary_metric
    dimensions = list(dimensions if dimensions is not None else config.segments)
    if not dimensions:
        return SegmentAnalysis((), 0.0)

    # Overall effect, for the "helped overall but not this group" comparison.
    overall_num = log.count_units_with_event(metric.numerator_event)
    overall_den = log.count_units_with_event(metric.denominator_event)
    overall_effect = 0.0
    if overall_den.get(control) and overall_den.get(treatment):
        overall_effect = overall_num.get(treatment, 0) / overall_den[treatment] - (
            overall_num.get(control, 0) / overall_den[control]
        )

    # Bucket each unit by its declared segment values.
    entered: dict[str, tuple[str, Mapping[str, object]]] = {}
    for event in log:
        if event.event_type == metric.denominator_event:
            entered.setdefault(event.unit_pseudonym, (event.variant, event.payload))
    converted = {
        e.unit_pseudonym for e in log if e.event_type == metric.numerator_event
    }

    threshold = config.privacy.suppression_threshold
    confidence = config.decision.confidence
    minimum_effect = metric.minimum_effect_of_interest

    raw: list[tuple[str, str, int, int, int, int]] = []
    suppressed = 0
    for dimension in dimensions:
        tallies: dict[str, dict[str, list[int]]] = {}
        for pseudonym, (variant, payload) in entered.items():
            if variant not in (control, treatment):
                continue
            value = payload.get(dimension)
            if value is None:
                continue
            bucket = tallies.setdefault(str(value), {control: [0, 0], treatment: [0, 0]})
            bucket[variant][1] += 1
            if pseudonym in converted:
                bucket[variant][0] += 1

        for value in sorted(tallies):
            c_hits, c_total = tallies[value][control]
            t_hits, t_total = tallies[value][treatment]
            # Disclosure rule applies to segment cells too: a breakdown
            # covering a handful of people can identify them.
            if 0 < min(c_total, t_total) < threshold:
                suppressed += 1
                continue
            raw.append((dimension, value, c_hits, c_total, t_hits, t_total))

    # Compare every segment, collecting p-values for one family-wide correction.
    comparisons: list[ProportionComparison | None] = []
    p_values: list[float] = []
    p_index: list[int] = []
    for index, (_, _, c_hits, c_total, t_hits, t_total) in enumerate(raw):
        if c_total < 2 or t_total < 2:
            comparisons.append(None)
            continue
        comparison = compare_proportions(
            c_hits, c_total, t_hits, t_total, confidence=confidence
        )
        comparisons.append(comparison)
        p_index.append(index)
        p_values.append(comparison.p_value)

    rejected: list[bool] = []
    if p_values:
        rejected = holm_bonferroni(p_values, alpha=1.0 - confidence)
    survives = {p_index[i]: rejected[i] for i in range(len(p_index))} if rejected else {}

    findings: list[SegmentFinding] = []
    for index, (dimension, value, _c_hits, c_total, _t_hits, t_total) in enumerate(raw):
        comparison = comparisons[index]
        if comparison is None:
            findings.append(
                SegmentFinding(
                    dimension,
                    value,
                    SegmentOutcome.TOO_FEW,
                    None,
                    c_total,
                    t_total,
                    "Too few people in this group to compare.",
                )
            )
            continue

        effect = comparison.absolute_difference
        corrected = survives.get(index, False)
        low, high = comparison.difference_interval

        if effect < 0 and corrected:
            outcome = SegmentOutcome.HARMED
            note = (
                f"The change made this group worse by {abs(effect) * 100:.1f} "
                f"percentage points (range {abs(high) * 100:.1f} to "
                f"{abs(low) * 100:.1f}). This holds up after accounting for the "
                "number of groups examined."
            )
        elif effect < 0 and comparison.significant:
            outcome = SegmentOutcome.WATCH
            note = (
                f"This group looks worse by {abs(effect) * 100:.1f} percentage "
                "points, but the signal does not survive correction for the number "
                "of groups examined. Monitor rather than act."
            )
        elif effect > 0 and corrected and low >= minimum_effect:
            outcome = SegmentOutcome.HELPED
            note = (
                f"The change helped this group by {effect * 100:.1f} percentage "
                f"points (range {low * 100:.1f} to {high * 100:.1f})."
            )
        elif min(c_total, t_total) < 100:
            outcome = SegmentOutcome.TOO_FEW
            note = (
                f"Only {min(c_total, t_total)} people in the smaller arm of this "
                "group. The result is consistent with anything from "
                f"{low * 100:+.1f} to {high * 100:+.1f} points; this pilot cannot "
                "tell whether the change reached them."
            )
        else:
            outcome = SegmentOutcome.NO_EFFECT
            note = (
                f"No measurable difference for this group "
                f"({low * 100:+.1f} to {high * 100:+.1f} points)."
            )

        findings.append(
            SegmentFinding(
                dimension, value, outcome, comparison, c_total, t_total, note
            )
        )

    return SegmentAnalysis(tuple(findings), overall_effect, suppressed)

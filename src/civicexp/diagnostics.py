"""Checks that decide whether a pilot's results can be trusted at all.

These run *before* anyone reads the headline number, because a result from a
broken pilot is worse than no result: it carries the same authority and
points the wrong way.

Three checks are implemented.

**Sample ratio mismatch (SRM).** The single highest-value data-quality check
in controlled experimentation, and the one most often missing from
home-grown tools. If an experiment is configured 50/50 but the arms come
back 6,800/5,200, the split did not happen the way the config says. Some
units were lost, double-counted, or routed wrongly, and whatever caused that
is unlikely to have affected both arms evenly. The headline effect is then
measuring the bug, not the change. An SRM invalidates an experiment outright
-- it is not a warning to note in a limitations section.

**Novelty and primacy effects.** A change can move a metric simply because
it is *different*, with the effect fading as people get used to it. The
mirror case, primacy, is a change that looks bad at first because people
must relearn a familiar flow, then settles. Both produce a real,
statistically solid effect in a short pilot that does not survive rollout.
This check splits the pilot window in half and compares the effect in each,
so a fading or growing effect is visible before the agency commits.

**Completeness.** Whether the events needed to compute the declared metrics
actually arrived, in both arms, in plausible proportions.

References
----------
Fabijan, A., Gupchup, J., Gupta, S., et al. (2019). Diagnosing sample ratio
mismatch in online controlled experiments. *KDD '19*, 2156-2164.

Kohavi, R., Tang, D., & Xu, Y. (2020). *Trustworthy Online Controlled
Experiments*. Cambridge University Press. Chapters on novelty/primacy and
on trust-related guardrails.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .stats import compare_proportions, normal_cdf

__all__ = [
    "Severity",
    "Diagnostic",
    "DiagnosticReport",
    "chi_square_p_value",
    "check_sample_ratio",
    "check_novelty",
    "check_completeness",
    "run_diagnostics",
    "SRM_ALPHA",
]

#: Significance level for the sample-ratio test.
#:
#: Deliberately stricter than the usual 0.05. An SRM check runs on every
#: analysis, so at 0.05 roughly one pilot in twenty would be declared broken
#: for no reason, and a tool that cries wolf gets ignored -- which costs
#: more than the check is worth. 0.001 is the threshold in common industry
#: practice for the same reason.
SRM_ALPHA = 0.001


class Severity(str, Enum):
    """How much weight a diagnostic finding should carry."""

    OK = "ok"
    #: Worth stating in the limitations section; does not block a decision.
    NOTE = "note"
    #: The decision should not be acted on until a human has looked.
    WARNING = "warning"
    #: The experiment is not interpretable. No decision may be drawn.
    INVALID = "invalid"

    @property
    def blocks_decision(self) -> bool:
        return self is Severity.INVALID


@dataclass(frozen=True)
class Diagnostic:
    """One trustworthiness finding."""

    name: str
    severity: Severity
    summary: str
    detail: str = ""
    statistics: Mapping[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.severity is Severity.OK


@dataclass(frozen=True)
class DiagnosticReport:
    """All trustworthiness findings for one analysis."""

    diagnostics: tuple[Diagnostic, ...]

    @property
    def is_trustworthy(self) -> bool:
        """Whether any result may be acted on at all."""
        return not any(d.severity.blocks_decision for d in self.diagnostics)

    @property
    def blocking(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity.blocks_decision)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.severity is Severity.WARNING)

    def __iter__(self):
        return iter(self.diagnostics)

    def __len__(self) -> int:
        return len(self.diagnostics)


# ---------------------------------------------------------------------------
# Chi-square goodness of fit
# ---------------------------------------------------------------------------


def chi_square_p_value(statistic: float, degrees_of_freedom: int) -> float:
    """Upper-tail probability of a chi-square statistic.

    Implemented via the regularized upper incomplete gamma function, since
    this package carries no SciPy dependency. For one degree of freedom --
    the two-arm case, which is the common one -- it reduces to the exact
    normal-tail identity.
    """
    if statistic < 0:
        raise ValueError("chi-square statistic cannot be negative")
    if degrees_of_freedom < 1:
        raise ValueError("degrees of freedom must be at least 1")
    if statistic == 0:
        return 1.0
    if degrees_of_freedom == 1:
        # P(X > x) = 2 * (1 - Phi(sqrt(x))) exactly.
        return 2.0 * (1.0 - normal_cdf(math.sqrt(statistic)))
    return _regularized_upper_gamma(degrees_of_freedom / 2.0, statistic / 2.0)


def _regularized_upper_gamma(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x), via series or continued fraction."""
    if x < a + 1.0:
        # Series expansion for the lower function, then complement.
        term = 1.0 / a
        total = term
        n = a
        for _ in range(1000):
            n += 1.0
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        lower = total * math.exp(-x + a * math.log(x) - math.lgamma(a))
        return max(0.0, min(1.0, 1.0 - lower))

    # Lentz continued fraction for the upper function.
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return max(0.0, min(1.0, h * math.exp(-x + a * math.log(x) - math.lgamma(a))))


# ---------------------------------------------------------------------------
# Sample ratio mismatch
# ---------------------------------------------------------------------------


def check_sample_ratio(
    observed: Mapping[str, int],
    expected_shares: Mapping[str, float],
    *,
    alpha: float = SRM_ALPHA,
) -> Diagnostic:
    """Test whether the observed split matches the configured traffic shares.

    A failure here means the assignment or logging pipeline is broken. The
    correct response is to fix the pipeline and rerun the pilot, not to
    analyze the data that came back.
    """
    arms = [a for a in expected_shares if expected_shares[a] > 0]
    total = sum(observed.get(a, 0) for a in arms)

    if total == 0:
        return Diagnostic(
            "sample_ratio",
            Severity.INVALID,
            "No units were recorded in any arm.",
            "There is nothing to analyze.",
        )
    if len(arms) < 2:
        return Diagnostic(
            "sample_ratio",
            Severity.OK,
            "Only one arm is configured; no ratio to check.",
        )

    share_total = sum(expected_shares[a] for a in arms)
    statistic = 0.0
    for arm in arms:
        expected_count = total * expected_shares[arm] / share_total
        if expected_count <= 0:
            continue
        statistic += (observed.get(arm, 0) - expected_count) ** 2 / expected_count

    degrees = len(arms) - 1
    p_value = chi_square_p_value(statistic, degrees)
    stats = {"chi_square": statistic, "p_value": p_value, "degrees_of_freedom": degrees}

    detail_rows = []
    for arm in sorted(arms):
        expected_count = total * expected_shares[arm] / share_total
        actual = observed.get(arm, 0)
        detail_rows.append(
            f"{arm}: expected {expected_count:,.0f}, observed {actual:,} "
            f"({actual - expected_count:+,.0f})"
        )
    detail = "; ".join(detail_rows)

    if p_value < alpha:
        return Diagnostic(
            "sample_ratio",
            Severity.INVALID,
            f"Sample ratio mismatch: the observed split does not match the "
            f"configured one (p = {p_value:.2e}).",
            detail
            + ". Units were lost, duplicated, or routed incorrectly. Whatever caused "
            "this is unlikely to have affected both arms evenly, so the headline "
            "result may be measuring the fault rather than the change. Fix the "
            "assignment or logging pipeline and rerun; do not interpret these "
            "results.",
            stats,
        )
    if p_value < 0.05:
        return Diagnostic(
            "sample_ratio",
            Severity.NOTE,
            f"The split is a little uneven but within normal variation "
            f"(p = {p_value:.3f}).",
            detail + ". No action needed; noted for completeness.",
            stats,
        )
    return Diagnostic(
        "sample_ratio",
        Severity.OK,
        f"The observed split matches the configured one (p = {p_value:.2f}).",
        detail,
        stats,
    )


# ---------------------------------------------------------------------------
# Novelty and primacy
# ---------------------------------------------------------------------------


def check_novelty(
    first_half: tuple[int, int],
    second_half: tuple[int, int],
    *,
    confidence: float = 0.95,
) -> Diagnostic:
    """Compare the treatment effect early in the pilot against later on.

    Each argument is ``(control_successes, control_total)`` paired with the
    treatment counts -- see :func:`run_diagnostics` for how they are
    assembled. A large shift between halves suggests the measured effect is
    partly an artifact of the change being new.
    """
    (c1, n1, t1, m1) = first_half
    (c2, n2, t2, m2) = second_half

    if min(n1, m1, n2, m2) < 30:
        return Diagnostic(
            "novelty",
            Severity.NOTE,
            "Too few units per half to check whether the effect is fading.",
            "A pilot needs roughly 30 units per arm in each half of its window "
            "before early and late effects can be meaningfully compared.",
        )

    early = compare_proportions(c1, n1, t1, m1, confidence=confidence)
    late = compare_proportions(c2, n2, t2, m2, confidence=confidence)
    early_effect = early.absolute_difference
    late_effect = late.absolute_difference
    shift = late_effect - early_effect

    stats = {
        "early_effect": early_effect,
        "late_effect": late_effect,
        "shift": shift,
    }

    # Do the two halves' intervals overlap? Non-overlap is a conservative
    # signal that the effect genuinely changed over the window.
    disjoint = (
        early.difference_interval[0] > late.difference_interval[1]
        or late.difference_interval[0] > early.difference_interval[1]
    )

    def pct(value: float) -> str:
        return f"{value * 100:+.1f}"

    summary_numbers = (
        f"first half {pct(early_effect)} points, second half {pct(late_effect)} points"
    )

    if disjoint and abs(early_effect) > abs(late_effect):
        return Diagnostic(
            "novelty",
            Severity.WARNING,
            f"The effect faded over the pilot ({summary_numbers}).",
            "This is the signature of a novelty effect: people respond to the "
            "change because it is new, and the response decays. The effect "
            "measured over the whole window probably overstates what a permanent "
            "rollout would deliver. Consider extending the pilot until the effect "
            "stabilizes before deciding.",
            stats,
        )
    if disjoint and abs(late_effect) > abs(early_effect):
        return Diagnostic(
            "novelty",
            Severity.NOTE,
            f"The effect grew over the pilot ({summary_numbers}).",
            "This is consistent with a primacy effect: people needed time to adjust "
            "to a changed flow, and the benefit appeared once they had. If so, the "
            "whole-window figure understates the long-run effect. It can also mean "
            "something else changed mid-pilot, so check the operational log before "
            "assuming the larger number.",
            stats,
        )
    return Diagnostic(
        "novelty",
        Severity.OK,
        f"The effect was stable across the pilot ({summary_numbers}).",
        "No evidence that the result depends on the change being new.",
        stats,
    )


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


def check_completeness(
    counts_by_event: Mapping[str, Mapping[str, int]],
    required_events: Sequence[str],
    arms: Sequence[str],
) -> Diagnostic:
    """Check that the events the metrics depend on actually arrived."""
    missing: list[str] = []
    for event in required_events:
        per_arm = counts_by_event.get(event, {})
        absent = [arm for arm in arms if per_arm.get(arm, 0) == 0]
        if len(absent) == len(arms):
            missing.append(f"{event} (no units in any arm)")
        elif absent:
            missing.append(f"{event} (none in {', '.join(sorted(absent))})")

    if not missing:
        return Diagnostic(
            "completeness",
            Severity.OK,
            f"All {len(required_events)} required event types were recorded in "
            "both arms.",
        )
    return Diagnostic(
        "completeness",
        Severity.WARNING,
        f"{len(missing)} required event type(s) are missing or one-sided.",
        "; ".join(missing)
        + ". A metric computed from a missing event is not zero, it is unknown. "
        "Check the instrumentation before reading any number that depends on it.",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_diagnostics(config, log, control: str, treatment: str) -> DiagnosticReport:
    """Run every trustworthiness check for one comparison."""
    diagnostics: list[Diagnostic] = []
    metric = config.primary_metric

    # 1. Sample ratio, over the units actually enrolled.
    denominators = log.count_units_with_event(metric.denominator_event)
    observed = {arm: denominators.get(arm, 0) for arm in (control, treatment)}
    expected = {
        arm: config.variants[arm] for arm in (control, treatment) if arm in config.variants
    }
    diagnostics.append(check_sample_ratio(observed, expected))

    # 2. Completeness, over every event the config depends on.
    required = {metric.numerator_event, metric.denominator_event}
    for guardrail in config.guardrails:
        required.add(guardrail.numerator_event)
        if guardrail.denominator_event:
            required.add(guardrail.denominator_event)
    counts = {event: log.count_units_with_event(event) for event in sorted(required)}
    diagnostics.append(
        check_completeness(counts, sorted(required), [control, treatment])
    )

    # 3. Novelty, by splitting the observed window at its midpoint.
    halves = _split_counts(log, metric, control, treatment)
    if halves is None:
        diagnostics.append(
            Diagnostic(
                "novelty",
                Severity.NOTE,
                "Not enough of a time window to check for a fading effect.",
                "All recorded events fall at essentially the same moment.",
            )
        )
    else:
        diagnostics.append(check_novelty(*halves, confidence=config.decision.confidence))

    return DiagnosticReport(tuple(diagnostics))


def _split_counts(log, metric, control: str, treatment: str):
    """Split the pilot at its midpoint, returning counts for each half.

    A unit is placed in the half containing its *denominator* event -- the
    moment it entered the experiment -- so that a unit which enters late and
    converts after the midpoint is not counted as a conversion without an
    entry.
    """
    timestamps = [e.timestamp for e in log if e.event_type == metric.denominator_event]
    if len(timestamps) < 4:
        return None
    earliest, latest = min(timestamps), max(timestamps)
    if earliest == latest:
        return None
    midpoint = earliest + (latest - earliest) / 2

    entered: dict[str, tuple[str, datetime]] = {}
    for event in log:
        if event.event_type == metric.denominator_event:
            key = event.unit_pseudonym
            if key not in entered or event.timestamp < entered[key][1]:
                entered[key] = (event.variant, event.timestamp)

    converted = {
        e.unit_pseudonym for e in log if e.event_type == metric.numerator_event
    }

    tallies = {
        half: {arm: [0, 0] for arm in (control, treatment)} for half in ("early", "late")
    }
    for pseudonym, (variant, when) in entered.items():
        if variant not in (control, treatment):
            continue
        half = "early" if when < midpoint else "late"
        tallies[half][variant][1] += 1
        if pseudonym in converted:
            tallies[half][variant][0] += 1

    early = (
        tallies["early"][control][0],
        tallies["early"][control][1],
        tallies["early"][treatment][0],
        tallies["early"][treatment][1],
    )
    late = (
        tallies["late"][control][0],
        tallies["late"][control][1],
        tallies["late"][treatment][0],
        tallies["late"][treatment][1],
    )
    return early, late

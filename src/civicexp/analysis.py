"""Component 3: comparing the versions, and applying the pre-registered rule.

This module turns a config plus an event log into a decision. The decision
logic is deliberately separated from the statistics in :mod:`civicexp.stats`
so that the rule an agency signed off on is readable in one place, without
any distribution functions in the way.

The rule, in full:

1. **Any breached guardrail forces ROLLBACK.** A guardrail is breached when
   the confidence interval on its change lies entirely beyond the tolerance
   the agency set. Harm outranks benefit unconditionally; there is no
   trade-off in which a completion-rate win buys a tolerated increase in
   blocking errors.
2. **Otherwise, PROMOTE requires a practically significant improvement**, in
   the direction the agency named, with the whole confidence interval
   clearing the minimum effect of interest. Statistical significance alone is
   not enough.
3. **Otherwise, ITERATE.** This covers no detected difference, a detected
   difference too small to matter, and an inconclusive underpowered result --
   which are reported distinctly, because they call for different next steps.

Guardrails are tested as a family with a Holm-Bonferroni correction. Checking
five guardrails at alpha=0.05 each would raise the chance of a spurious harm
signal to roughly 23%, and a pilot halted by a false alarm costs an agency
real credibility for the next one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .config import ExperimentConfig, GuardrailSpec
from .diagnostics import DiagnosticReport, run_diagnostics
from .errors import AnalysisError
from .events import EventLog
from .segments import SegmentAnalysis, analyze_segments
from .stats import (
    MeanComparison,
    ProportionComparison,
    compare_means,
    compare_proportions,
    holm_bonferroni,
)

__all__ = ["Decision", "GuardrailResult", "AnalysisResult", "analyze"]


class Decision(str, Enum):
    """The pre-registered decision outcomes."""

    PROMOTE = "promote"
    ITERATE = "iterate"
    ROLLBACK = "rollback"
    INCONCLUSIVE = "inconclusive"
    #: The pilot is not interpretable -- a trust check failed. Distinct from
    #: INCONCLUSIVE, which means the pilot was sound but too small.
    INVALID = "invalid"

    @property
    def headline(self) -> str:
        return {
            Decision.PROMOTE: "Adopt the tested version",
            Decision.ITERATE: "Keep the current version and revise the test",
            Decision.ROLLBACK: "Stop and revert the tested version",
            Decision.INCONCLUSIVE: "No conclusion can be drawn yet",
            Decision.INVALID: "These results cannot be used",
        }[self]


@dataclass(frozen=True)
class GuardrailResult:
    """Whether one harm-detection metric stayed within tolerance."""

    name: str
    definition: str
    kind: str
    tolerance: float
    comparison: ProportionComparison | MeanComparison | None
    breached: bool
    watch: bool
    note: str

    @property
    def status(self) -> str:
        if self.breached:
            return "BREACHED"
        if self.watch:
            return "WATCH"
        if self.comparison is None:
            return "NO DATA"
        return "OK"


@dataclass(frozen=True)
class AnalysisResult:
    """Everything needed to write the readout and make the call."""

    config: ExperimentConfig
    control_variant: str
    treatment_variant: str
    primary: ProportionComparison | MeanComparison
    guardrails: tuple[GuardrailResult, ...]
    decision: Decision
    rationale: tuple[str, ...]
    diagnostics: DiagnosticReport | None = None
    equity: SegmentAnalysis | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def breached_guardrails(self) -> tuple[GuardrailResult, ...]:
        return tuple(g for g in self.guardrails if g.breached)

    def to_dict(self) -> dict[str, Any]:
        primary = self.primary
        payload: dict[str, Any] = {
            "experiment_id": self.config.experiment_id,
            "control_variant": self.control_variant,
            "treatment_variant": self.treatment_variant,
            "decision": self.decision.value,
            "rationale": list(self.rationale),
            "warnings": list(self.warnings),
            "trustworthy": self.diagnostics.is_trustworthy if self.diagnostics else None,
            "diagnostics": [
                {"name": d.name, "severity": d.severity.value, "summary": d.summary}
                for d in (self.diagnostics or ())
            ],
            "equity": {
                "summary": self.equity.summary_line(),
                "has_concern": self.equity.has_equity_concern,
                "findings": [
                    {
                        "segment": f.label,
                        "outcome": f.outcome.value,
                        "effect": f.effect,
                        "note": f.note,
                    }
                    for f in self.equity.findings
                ],
            }
            if self.equity
            else None,
            "guardrails": [
                {
                    "name": g.name,
                    "status": g.status,
                    "tolerance": g.tolerance,
                    "note": g.note,
                }
                for g in self.guardrails
            ],
        }
        if isinstance(primary, ProportionComparison):
            payload["primary"] = {
                "metric": self.config.primary_metric.name,
                "control_rate": primary.control_rate,
                "treatment_rate": primary.treatment_rate,
                "absolute_difference": primary.absolute_difference,
                "confidence_interval": list(primary.difference_interval),
                "p_value": primary.p_value,
                "significant": primary.significant,
                "control_n": primary.control_trials,
                "treatment_n": primary.treatment_trials,
            }
        else:
            payload["primary"] = {
                "metric": self.config.primary_metric.name,
                "control_mean": primary.control_mean,
                "treatment_mean": primary.treatment_mean,
                "difference": primary.difference,
                "confidence_interval": list(primary.difference_interval),
                "p_value": primary.p_value,
                "significant": primary.significant,
                "control_n": primary.control_n,
                "treatment_n": primary.treatment_n,
            }
        return payload


def _proportion_counts(
    log: EventLog, numerator_event: str, denominator_event: str, variant: str
) -> tuple[int, int]:
    numerators = log.count_units_with_event(numerator_event)
    denominators = log.count_units_with_event(denominator_event)
    return numerators.get(variant, 0), denominators.get(variant, 0)


def _evaluate_guardrail(
    spec: GuardrailSpec,
    log: EventLog,
    control: str,
    treatment: str,
    confidence: float,
    looks: int,
) -> tuple[GuardrailResult, float | None]:
    """Evaluate one guardrail; returns the result and its p-value if testable."""
    if spec.kind == "proportion":
        c_num, c_den = _proportion_counts(
            log, spec.numerator_event, spec.denominator_event, control
        )
        t_num, t_den = _proportion_counts(
            log, spec.numerator_event, spec.denominator_event, treatment
        )
        if c_den == 0 or t_den == 0:
            return (
                GuardrailResult(
                    name=spec.name,
                    definition=spec.definition,
                    kind=spec.kind,
                    tolerance=spec.max_tolerated_increase,
                    comparison=None,
                    breached=False,
                    watch=True,
                    note=(
                        "No denominator events were recorded for at least one arm, so "
                        "this guardrail could not be checked. Treat it as unverified, "
                        "not as passing."
                    ),
                ),
                None,
            )
        comparison = compare_proportions(
            c_num, c_den, t_num, t_den, confidence=confidence, looks=looks
        )
        low, high = comparison.difference_interval
        worsened_beyond_tolerance = low > spec.max_tolerated_increase
        watch = (
            not worsened_beyond_tolerance
            and high > spec.max_tolerated_increase
            and comparison.absolute_difference > 0
        )
        if worsened_beyond_tolerance:
            note = (
                f"Worsened by {comparison.absolute_difference * 100:.1f} percentage "
                f"points (95% CI {low * 100:.1f} to {high * 100:.1f}), and the whole "
                f"interval exceeds the {spec.max_tolerated_increase * 100:.1f} point "
                "tolerance."
            )
        elif watch:
            note = (
                f"Point estimate worsened by {comparison.absolute_difference * 100:.1f} "
                f"percentage points, and the interval reaches {high * 100:.1f}, above "
                f"the {spec.max_tolerated_increase * 100:.1f} point tolerance. Not a "
                "confirmed breach, but it warrants monitoring."
            )
        else:
            note = (
                f"Change of {comparison.absolute_difference * 100:+.1f} percentage "
                f"points (95% CI {low * 100:.1f} to {high * 100:.1f}), within the "
                f"{spec.max_tolerated_increase * 100:.1f} point tolerance."
            )
        return (
            GuardrailResult(
                name=spec.name,
                definition=spec.definition,
                kind=spec.kind,
                tolerance=spec.max_tolerated_increase,
                comparison=comparison,
                breached=worsened_beyond_tolerance,
                watch=watch,
                note=note,
            ),
            comparison.p_value,
        )

    # Duration guardrail, e.g. time-to-complete.
    durations = log.durations_by_variant(spec.denominator_event, spec.numerator_event)
    control_values = durations.get(control, [])
    treatment_values = durations.get(treatment, [])
    if len(control_values) < 2 or len(treatment_values) < 2:
        return (
            GuardrailResult(
                name=spec.name,
                definition=spec.definition,
                kind=spec.kind,
                tolerance=spec.max_tolerated_increase,
                comparison=None,
                breached=False,
                watch=True,
                note=(
                    "Fewer than two completed durations in at least one arm, so this "
                    "guardrail could not be checked."
                ),
            ),
            None,
        )
    comparison = compare_means(
        control_values, treatment_values, confidence=confidence, looks=looks
    )
    low, high = comparison.difference_interval
    breached = low > spec.max_tolerated_increase
    watch = not breached and high > spec.max_tolerated_increase and comparison.difference > 0
    note = (
        f"Change of {comparison.difference:+.1f} seconds "
        f"(95% CI {low:.1f} to {high:.1f}) against a tolerance of "
        f"{spec.max_tolerated_increase:.1f} seconds."
    )
    return (
        GuardrailResult(
            name=spec.name,
            definition=spec.definition,
            kind=spec.kind,
            tolerance=spec.max_tolerated_increase,
            comparison=comparison,
            breached=breached,
            watch=watch,
            note=note,
        ),
        comparison.p_value,
    )


def analyze(
    config: ExperimentConfig,
    log: EventLog,
    *,
    treatment_variant: str | None = None,
) -> AnalysisResult:
    """Compare one treatment arm against control and apply the decision rule."""
    control = config.control_variant
    candidates = config.treatment_variants
    if treatment_variant is None:
        if len(candidates) != 1:
            raise AnalysisError(
                f"this experiment has {len(candidates)} treatment arms "
                f"({list(candidates)!r}); name which one to compare against control",
                code="ANALYSIS_AMBIGUOUS_ARM",
            )
        treatment_variant = candidates[0]
    elif treatment_variant not in config.variants:
        raise AnalysisError(
            f"{treatment_variant!r} is not a declared variant of this experiment",
            code="ANALYSIS_UNKNOWN_ARM",
        )

    metric = config.primary_metric
    confidence = config.decision.confidence
    looks = config.decision.planned_looks
    warnings: list[str] = []

    if metric.kind != "proportion":
        raise AnalysisError(
            "only proportion primary metrics are supported in this release; "
            "duration metrics are available as guardrails",
            code="ANALYSIS_UNSUPPORTED_METRIC",
        )

    c_num, c_den = _proportion_counts(
        log, metric.numerator_event, metric.denominator_event, control
    )
    t_num, t_den = _proportion_counts(
        log, metric.numerator_event, metric.denominator_event, treatment_variant
    )
    if c_den == 0 or t_den == 0:
        raise AnalysisError(
            f"no {metric.denominator_event!r} events recorded for "
            f"{'control' if c_den == 0 else 'treatment'}; there is nothing to compare",
            code="ANALYSIS_NO_DATA",
        )

    primary = compare_proportions(
        c_num, c_den, t_num, t_den, confidence=confidence, looks=looks
    )
    warnings.extend(primary.warnings)

    # Trust checks, and the pre-registered equity review. Both run before the
    # decision rule, because a result from a broken pilot should never reach
    # it, and a change that harms a group is not a win whatever the aggregate
    # says.
    diagnostics = run_diagnostics(config, log, control, treatment_variant)
    equity = analyze_segments(config, log, control, treatment_variant)

    # Small-cell suppression applies to what gets published, and a comparison
    # resting on cells this small should not be published at all.
    threshold = config.privacy.suppression_threshold
    if min(c_den, t_den) < threshold:
        warnings.append(
            f"At least one arm has fewer than {threshold} units. Under the "
            "configured disclosure rule these results may not be published in "
            "detail, and should not drive a decision."
        )

    # -- guardrails, corrected as a family --------------------------------
    raw_results: list[GuardrailResult] = []
    p_values: list[float] = []
    p_index: list[int] = []
    for spec in config.guardrails:
        result, p_value = _evaluate_guardrail(
            spec, log, control, treatment_variant, confidence, looks
        )
        if p_value is not None:
            p_index.append(len(raw_results))
            p_values.append(p_value)
        raw_results.append(result)

    guardrails = list(raw_results)
    if p_values:
        family_alpha = 1.0 - confidence
        rejected = holm_bonferroni(p_values, alpha=family_alpha)
        for position, index in enumerate(p_index):
            result = guardrails[index]
            if result.breached and not rejected[position]:
                # The interval cleared the tolerance, but the change does not
                # survive correction for testing several guardrails at once.
                guardrails[index] = GuardrailResult(
                    name=result.name,
                    definition=result.definition,
                    kind=result.kind,
                    tolerance=result.tolerance,
                    comparison=result.comparison,
                    breached=False,
                    watch=True,
                    note=(
                        result.note
                        + " Downgraded from breach to watch: with "
                        f"{len(p_values)} guardrails tested together, this signal does "
                        "not survive a Holm-Bonferroni correction for multiple "
                        "comparisons."
                    ),
                )

    # -- apply the pre-registered rule ------------------------------------
    rationale: list[str] = []
    breached = [g for g in guardrails if g.breached]

    if not diagnostics.is_trustworthy:
        # A failed trust check outranks everything, including the guardrails.
        # The numbers below it are not measuring what they claim to.
        reasons = "; ".join(d.summary for d in diagnostics.blocking)
        return AnalysisResult(
            config=config,
            control_variant=control,
            treatment_variant=treatment_variant,
            primary=primary,
            guardrails=tuple(guardrails),
            decision=Decision.INVALID,
            rationale=(
                f"This pilot did not pass its data-quality checks: {reasons} "
                "No decision can be drawn from these results, in either direction. "
                "Fix the underlying problem and rerun.",
            ),
            diagnostics=diagnostics,
            equity=equity,
            warnings=tuple(warnings),
        )

    if breached:
        decision = Decision.ROLLBACK
        rationale.append(
            f"{len(breached)} guardrail(s) breached tolerance: "
            + "; ".join(g.name for g in breached)
            + ". A confirmed harm signal forces a rollback regardless of the primary "
            "metric."
        )
    else:
        wanted = metric.minimum_effect_of_interest
        signed_threshold = wanted if metric.target_direction == "increase" else -wanted
        practical = primary.meets_practical_threshold(signed_threshold)
        improved = (
            primary.absolute_difference > 0
            if metric.target_direction == "increase"
            else primary.absolute_difference < 0
        )
        if practical and primary.significant:
            decision = Decision.PROMOTE
            rationale.append(
                f"{metric.name} moved {primary.absolute_difference * 100:+.1f} "
                f"percentage points, and the entire confidence interval clears the "
                f"{wanted * 100:.1f} point improvement the program said was worth "
                "acting on."
            )
        elif primary.significant and improved:
            decision = Decision.ITERATE
            rationale.append(
                f"{metric.name} improved by {primary.absolute_difference * 100:.1f} "
                f"percentage points and the change is statistically detectable, but the "
                f"confidence interval does not clear the {wanted * 100:.1f} point bar "
                "the program set. The effect is real but smaller than the one that "
                "would justify the change."
            )
        elif primary.significant and not improved:
            decision = Decision.ITERATE
            rationale.append(
                f"{metric.name} moved {primary.absolute_difference * 100:+.1f} "
                "percentage points, in the opposite direction to the hypothesis. No "
                "guardrail was breached, so this is a failed test rather than a harm "
                "event."
            )
        else:
            low, high = primary.difference_interval
            if high - low > 2 * wanted:
                decision = Decision.INCONCLUSIVE
                rationale.append(
                    f"The confidence interval on {metric.name} runs from "
                    f"{low * 100:+.1f} to {high * 100:+.1f} percentage points, which is "
                    f"wide relative to the {wanted * 100:.1f} point effect of interest. "
                    "This pilot did not collect enough data to distinguish a "
                    "worthwhile improvement from no change."
                )
            else:
                decision = Decision.ITERATE
                rationale.append(
                    f"No detectable change in {metric.name}: the interval "
                    f"({low * 100:+.1f} to {high * 100:+.1f} percentage points) is "
                    "narrow enough to rule out an improvement of the size that would "
                    "matter. The tested change does not work as intended."
                )

    # An equity harm overrides a promotion. A change that improves the
    # average while measurably hurting a group has not improved the service;
    # it has redistributed who it fails.
    if equity.harmed and decision is not Decision.ROLLBACK:
        previous = decision
        decision = Decision.ROLLBACK
        names = ", ".join(f.label for f in equity.harmed)
        rationale.insert(
            0,
            f"Escalated from {previous.value} to rollback: the change measurably "
            f"harmed {names}. A change that makes the service worse for an "
            "identifiable group goes through the rollback and review path, "
            "whatever it did to the average.",
        )
    elif equity.has_equity_concern or equity.not_reached and decision is Decision.PROMOTE:
        rationale.append(equity.summary_line())

    for diagnostic in diagnostics:
        if diagnostic.severity.value in ("warning", "note") and diagnostic.name == "novelty":
            rationale.append(diagnostic.summary)

    watching = [g for g in guardrails if g.watch]
    if watching:
        rationale.append(
            f"{len(watching)} guardrail(s) warrant monitoring rather than action: "
            + "; ".join(g.name for g in watching)
            + "."
        )

    return AnalysisResult(
        config=config,
        control_variant=control,
        treatment_variant=treatment_variant,
        primary=primary,
        guardrails=tuple(guardrails),
        decision=decision,
        rationale=tuple(rationale),
        diagnostics=diagnostics,
        equity=equity,
        warnings=tuple(warnings),
    )

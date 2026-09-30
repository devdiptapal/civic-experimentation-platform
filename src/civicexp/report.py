"""Component 4: a standardized report that explains results in plain terms.

The audience for this output is a program manager, a policy reviewer, and an
oversight body -- not a data scientist. Three rules follow from that, and
they are the reason this module exists at all rather than printing the
contents of an :class:`~civicexp.analysis.AnalysisResult`:

**Say the decision first.** The recommendation and its reason appear before
any number. A reader who stops after the first paragraph should not be
missing anything that would change their action.

**Never print a bare point estimate.** Every effect is reported with its
confidence interval and, where it matters, with a sentence saying what the
uncertainty means for the decision. "Completion rose 4 points" invites a
false precision that "rose 4 points, somewhere between 1 and 7" does not.

**Do not use the word significant without qualifying it.** In program
settings it reads as "large", which is not what it means. The report says
"larger than the improvement the program said was worth acting on" or "too
small to be sure it is real", and reserves p-values for the technical
appendix.

The standardized structure also serves reuse across jurisdictions: a county
that reads three of these from elsewhere can compare them directly, which is
what turns a single pilot into transferable evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from .analysis import AnalysisResult, Decision, GuardrailResult
from .audit import AuditLog
from .stats import ProportionComparison

__all__ = ["render_markdown", "render_text_summary"]

_DECISION_GUIDANCE = {
    Decision.PROMOTE: (
        "The tested version performed better by a margin the program agreed in "
        "advance would be worth acting on, and no harm measure moved outside its "
        "agreed tolerance."
    ),
    Decision.ITERATE: (
        "The tested version did not earn adoption. No harm was confirmed, so the "
        "sensible next step is to revise the change or test a different one rather "
        "than to act on this result."
    ),
    Decision.ROLLBACK: (
        "At least one harm measure moved outside the tolerance the program set. "
        "The pre-agreed rule is to revert the tested version and review before any "
        "further testing."
    ),
    Decision.INCONCLUSIVE: (
        "This pilot cannot answer the question it was designed to answer. The "
        "result is neither a success nor a failure of the tested change; it is a "
        "signal that the pilot needs more data or a rescoped question."
    ),
}


def _pct(value: float, places: int = 1) -> str:
    return f"{value * 100:.{places}f}%"


def _signed_pct(value: float, places: int = 1) -> str:
    return f"{value * 100:+.{places}f} percentage points"


def _plain_uncertainty(comparison: ProportionComparison) -> str:
    low, high = comparison.difference_interval
    confidence = _pct(comparison.confidence, 0)
    if low > 0:
        return (
            f"We are {confidence} confident the tested version is better, by "
            f"somewhere between {low * 100:.1f} and {high * 100:.1f} percentage points."
        )
    if high < 0:
        return (
            f"We are {confidence} confident the tested version is worse, by "
            f"somewhere between {abs(high) * 100:.1f} and {abs(low) * 100:.1f} "
            "percentage points."
        )
    return (
        f"The range of plausible effects runs from {low * 100:.1f} to "
        f"{high * 100:.1f} percentage points. Because that range includes zero, this "
        "pilot cannot rule out that the two versions perform the same."
    )


def _guardrail_rows(guardrails: Iterable[GuardrailResult]) -> list[str]:
    rows = [
        "| Harm measure | Status | What happened |",
        "| --- | --- | --- |",
    ]
    for g in guardrails:
        rows.append(f"| {g.name} | **{g.status}** | {g.note} |")
    return rows


def render_markdown(
    result: AnalysisResult,
    *,
    audit: AuditLog | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Render the standardized pilot readout as Markdown."""
    config = result.config
    metric = config.primary_metric
    primary = result.primary
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []
    add = lines.append

    add(f"# Evaluation readout: {config.name}")
    add("")
    add(f"**Experiment ID:** `{config.experiment_id}`  ")
    add(f"**Service area:** {config.service_area or 'not recorded'}  ")
    add(f"**Owning team:** {config.owner_team or 'not recorded'}  ")
    add(f"**Generated:** {stamp}")
    add("")

    # -- 1. Recommendation -------------------------------------------------
    add("## 1. Recommendation")
    add("")
    add(f"### {result.decision.headline}")
    add("")
    add(_DECISION_GUIDANCE[result.decision])
    add("")
    for reason in result.rationale:
        add(f"- {reason}")
    add("")

    # -- 2. What was tested ------------------------------------------------
    add("## 2. What was tested")
    add("")
    add(config.description or "_No description was recorded in the configuration._")
    add("")
    add(
        f"People and transactions meeting the approved criteria were split between "
        f"**{result.control_variant}** (the current service) and "
        f"**{result.treatment_variant}** (the tested change). Assignment was made by a "
        "fixed rule tied to each unit's identifier, so the same person always saw the "
        "same version and the split can be re-checked afterwards."
    )
    add("")
    if config.eligibility.include or config.eligibility.exclude:
        add("**Who was included:**")
        add("")
        for rule in config.eligibility.include:
            add(f"- Included when {rule.describe()}")
        for rule in config.eligibility.exclude:
            add(f"- Excluded when {rule.describe()}")
        add("")
    if config.enrolled_fraction < 1.0:
        add(
            f"The evaluation was limited to {_pct(config.enrolled_fraction, 0)} of "
            "eligible traffic. Everyone else received the unchanged service and is not "
            "counted here."
        )
        add("")

    # -- 3. The main result ------------------------------------------------
    add("## 3. The main result")
    add("")
    add(f"**Measure:** {metric.name} — {metric.definition}")
    add("")
    if isinstance(primary, ProportionComparison):
        add("| Version | Units measured | Rate | Plausible range |")
        add("| --- | ---: | ---: | --- |")
        add(
            f"| {result.control_variant} (current) | {primary.control_trials:,} | "
            f"{_pct(primary.control_rate)} | {_pct(primary.control_interval[0])} to "
            f"{_pct(primary.control_interval[1])} |"
        )
        add(
            f"| {result.treatment_variant} (tested) | {primary.treatment_trials:,} | "
            f"{_pct(primary.treatment_rate)} | {_pct(primary.treatment_interval[0])} to "
            f"{_pct(primary.treatment_interval[1])} |"
        )
        add("")
        add(f"**Difference:** {_signed_pct(primary.absolute_difference)}")
        add("")
        add(_plain_uncertainty(primary))
        add("")
        add(
            f"Before launch, the program recorded that a change of at least "
            f"**{metric.minimum_effect_of_interest * 100:.1f} percentage points** "
            "would be worth acting on. That bar is what the recommendation above is "
            "measured against, not simply whether a difference exists."
        )
    else:
        low, high = primary.difference_interval
        add("| Version | Units measured | Average |")
        add("| --- | ---: | ---: |")
        add(
            f"| {result.control_variant} (current) | {primary.control_n:,} | "
            f"{primary.control_mean:.1f}s |"
        )
        add(
            f"| {result.treatment_variant} (tested) | {primary.treatment_n:,} | "
            f"{primary.treatment_mean:.1f}s |"
        )
        add("")
        add(
            f"**Difference:** {primary.difference:+.1f} seconds "
            f"(plausible range {low:+.1f} to {high:+.1f})"
        )
    add("")

    # -- 4. Harm measures --------------------------------------------------
    add("## 4. Checks for unintended harm")
    add("")
    if result.guardrails:
        add(
            "These measures were agreed in advance as things that must not get "
            "materially worse, whatever happens to the main measure."
        )
        add("")
        lines.extend(_guardrail_rows(result.guardrails))
    else:
        add(
            "_No harm measures were defined for this pilot. Results should be treated "
            "with corresponding caution: the evaluation had no way to detect a side "
            "effect of the change._"
        )
    add("")

    # -- 5. Limitations ----------------------------------------------------
    add("## 5. Limits of this evidence")
    add("")
    limitations = list(result.warnings)
    limitations.append(
        "This evaluation shows what happened for the people and transactions "
        "included during the pilot window. It does not establish that the same "
        "effect would appear for excluded groups, in another jurisdiction, or at a "
        "different time of year."
    )
    if config.planned_duration_days:
        limitations.append(
            f"The pilot ran for a planned {config.planned_duration_days} days. Effects "
            "that appear only after longer exposure, such as changes in repeat "
            "contact, are outside its reach."
        )
    note = config.power_note()
    if note:
        limitations.append(note)
    for item in limitations:
        add(f"- {item}")
    add("")

    # -- 6. Next steps -----------------------------------------------------
    add("## 6. Suggested next steps")
    add("")
    for step in _next_steps(result):
        add(f"- {step}")
    add("")

    # -- 7. Technical appendix --------------------------------------------
    add("## 7. Technical appendix")
    add("")
    add(
        "Rates are compared with a two-sided two-proportion z-test. Confidence "
        "intervals for each rate use the Wilson score method, and the interval on the "
        "difference uses Newcombe's hybrid score method, both of which hold their "
        "coverage at the small denominators common in county-scale pilots where the "
        "usual normal approximation does not."
    )
    add("")
    if isinstance(primary, ProportionComparison):
        add(f"- Significance threshold applied: α = {primary.alpha:.4f}")
        if config.decision.planned_looks > 1:
            add(
                f"- Corrected from α = {1 - primary.confidence:.3f} for "
                f"{config.decision.planned_looks} planned looks at the data (Šidák)"
            )
        add(f"- p-value on the primary measure: {primary.p_value:.4f}")
        add(
            f"- Counts: control {primary.control_successes:,}/{primary.control_trials:,}, "
            f"tested {primary.treatment_successes:,}/{primary.treatment_trials:,}"
        )
    if len([g for g in result.guardrails if g.comparison is not None]) > 1:
        add(
            f"- Guardrails were tested as a family of "
            f"{len([g for g in result.guardrails if g.comparison is not None])} with a "
            "Holm-Bonferroni correction, so a single guardrail moving by chance does "
            "not halt the pilot."
        )
    add(
        f"- Aggregates covering fewer than {config.privacy.suppression_threshold} units "
        "are suppressed under the configured disclosure rule."
    )
    add("")

    # -- 8. Audit ----------------------------------------------------------
    if audit is not None and len(audit):
        add("## 8. Decision and approval record")
        add("")
        add(audit.to_markdown())
        add("")

    add("---")
    add("")
    add(
        "_Generated by the Civic Experimentation Platform. This readout describes "
        "measured outcomes for a specific service change; it is not a legal, policy, "
        "or eligibility determination._"
    )
    add("")
    return "\n".join(lines)


def _next_steps(result: AnalysisResult) -> list[str]:
    metric = result.config.primary_metric
    if result.decision is Decision.PROMOTE:
        return [
            "Roll the tested version out to the full eligible population, keeping the "
            "harm measures under observation for at least one further cycle.",
            "Record the decision and this readout in the pilot's archive so the "
            "evidence is available to the next team that asks the same question.",
            "Publish a case summary so other jurisdictions running the same workflow "
            "can reuse the finding rather than repeat the pilot.",
        ]
    if result.decision is Decision.ROLLBACK:
        return [
            "Revert the tested version now, following the rollback process agreed "
            "before launch.",
            "Notify support staff that the change is being withdrawn and why.",
            "Review what the harm measure is telling you before designing any "
            "replacement test.",
        ]
    if result.decision is Decision.INCONCLUSIVE:
        return [
            "Do not act on this result in either direction.",
            "Decide whether the pilot can run longer or across a wider eligible "
            "population to reach the volume the question needs.",
            "If the necessary volume is not available, rescope to a larger, more "
            "detectable change rather than running the same pilot again.",
        ]
    return [
        "Keep the current version in place.",
        f"Revisit the assumption behind the change: {metric.name} did not move the way "
        "the hypothesis predicted.",
        "Document the negative result. A change that did not work is evidence other "
        "agencies can use, and is worth publishing for exactly that reason.",
    ]


def render_text_summary(result: AnalysisResult) -> str:
    """A short plain-text summary for a terminal, email, or ticket comment."""
    primary = result.primary
    lines = [
        f"{result.config.name} [{result.config.experiment_id}]",
        f"Decision: {result.decision.value.upper()} — {result.decision.headline}",
    ]
    if isinstance(primary, ProportionComparison):
        low, high = primary.difference_interval
        lines.append(
            f"{result.config.primary_metric.name}: "
            f"{_pct(primary.control_rate)} -> {_pct(primary.treatment_rate)} "
            f"({_signed_pct(primary.absolute_difference)}, "
            f"CI {low * 100:+.1f} to {high * 100:+.1f})"
        )
    breached = result.breached_guardrails
    if breached:
        lines.append("Guardrails breached: " + ", ".join(g.name for g in breached))
    else:
        lines.append(f"Guardrails: {len(result.guardrails)} checked, none breached")
    return "\n".join(lines)

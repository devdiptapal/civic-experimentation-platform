"""Statistical functions for comparing service-delivery outcomes.

Design constraints that drive the choices in this module:

1. **Standard library only.** Public agencies frequently cannot install
   SciPy/NumPy inside a reviewed environment. Every distribution function
   used here is implemented from scratch on top of :mod:`math`.
2. **Small denominators are normal.** A county workflow pilot may have a few
   hundred sessions, and guardrail events (blocking errors, support contacts)
   are rare. Wald/normal intervals are unreliable in exactly that regime, so
   proportions use the Wilson score interval and differences of proportions
   use Newcombe's hybrid score method.
3. **Practical significance is reported alongside statistical significance.**
   A result that is statistically detectable but smaller than the effect the
   agency said it cared about is reported as such, never as a win.
4. **Peeking is accounted for, not ignored.** Agencies monitor pilots daily.
   If a test is evaluated repeatedly, the caller declares how many looks are
   planned and the significance threshold is corrected.

References
----------
Wilson, E. B. (1927). Probable inference, the law of succession, and
statistical inference. *JASA* 22(158), 209-212.

Newcombe, R. G. (1998). Interval estimation for the difference between
independent proportions: comparison of eleven methods.
*Statistics in Medicine* 17(8), 873-890.

Holm, S. (1979). A simple sequentially rejective multiple test procedure.
*Scandinavian Journal of Statistics* 6(2), 65-70.

Welch, B. L. (1947). The generalization of "Student's" problem when several
different population variances are involved. *Biometrika* 34(1/2), 28-35.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import AnalysisError

__all__ = [
    "normal_cdf",
    "normal_quantile",
    "student_t_cdf",
    "wilson_interval",
    "ProportionComparison",
    "compare_proportions",
    "MeanComparison",
    "compare_means",
    "required_sample_size_per_group",
    "minimum_detectable_effect",
    "corrected_alpha",
    "holm_bonferroni",
]


# ---------------------------------------------------------------------------
# Distribution functions
# ---------------------------------------------------------------------------


def normal_cdf(x: float) -> float:
    """Cumulative distribution function of the standard normal."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def normal_quantile(p: float) -> float:
    """Inverse CDF (quantile function) of the standard normal.

    Uses bisection to bracket the root of ``normal_cdf(x) - p`` and then
    polishes with Newton-Raphson. Bisection is chosen over a rational
    approximation because it is obviously correct on inspection, and the cost
    (a few dozen iterations) is irrelevant at the call volumes here.
    """
    if not 0.0 < p < 1.0:
        raise AnalysisError(
            f"normal_quantile requires 0 < p < 1, got {p!r}",
            code="ANALYSIS_DOMAIN",
        )
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if normal_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-14:
            break
    x = (lo + hi) / 2.0
    # Newton polish: pdf is the derivative of the cdf.
    for _ in range(3):
        pdf = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
        if pdf < 1e-300:
            break
        x -= (normal_cdf(x) - p) / pdf
    return x


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion for the incomplete beta function.

    Modified Lentz algorithm (Numerical Recipes, section 6.4).
    """
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 301):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(a * math.log(x) + b * math.log1p(-x) - _log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(
        b * math.log1p(-x) + a * math.log(x) - _log_beta(b, a)
    ) * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    """CDF of Student's t distribution with ``df`` degrees of freedom."""
    if df <= 0:
        raise AnalysisError(
            f"degrees of freedom must be positive, got {df!r}",
            code="ANALYSIS_DOMAIN",
        )
    x = df / (df + t * t)
    tail = 0.5 * _regularized_incomplete_beta(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


# ---------------------------------------------------------------------------
# Multiplicity and repeated looks
# ---------------------------------------------------------------------------


def corrected_alpha(alpha: float, looks: int) -> float:
    """Significance level adjusted for ``looks`` interim analyses.

    Uses the Šidák correction, ``1 - (1 - alpha) ** (1 / looks)``. This is
    deliberately conservative relative to an alpha-spending function such as
    O'Brien-Fleming: it spends the same budget at every look rather than
    saving it for the end.

    That trade is intentional for this setting. Agency monitoring is driven by
    operational risk (a guardrail moving, a caseworker reporting a problem),
    not by a pre-registered interim schedule, so the number and timing of
    looks is not reliably known in advance. A correction that does not depend
    on look timing is the honest choice, and erring toward under-declaring
    wins is the right direction of error for a public program.
    """
    if not 0.0 < alpha < 1.0:
        raise AnalysisError(f"alpha must be in (0, 1), got {alpha!r}", code="ANALYSIS_DOMAIN")
    if looks < 1:
        raise AnalysisError(f"looks must be >= 1, got {looks!r}", code="ANALYSIS_DOMAIN")
    if looks == 1:
        # Short-circuit rather than computing 1 - (1 - alpha) ** 1, which is
        # alpha only up to floating-point error and would otherwise make an
        # uncorrected threshold very slightly *looser* than the one requested.
        return alpha
    return 1.0 - (1.0 - alpha) ** (1.0 / looks)


def holm_bonferroni(p_values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni step-down procedure.

    Returns a list of rejection decisions aligned with the input order.
    Controls the family-wise error rate across a set of related tests, which
    is what a guardrail panel is: several simultaneous checks for harm.
    """
    if not p_values:
        return []
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    decisions = [False] * len(p_values)
    n = len(p_values)
    for rank, idx in enumerate(order):
        threshold = alpha / (n - rank)
        if p_values[idx] <= threshold:
            decisions[idx] = True
        else:
            break  # step-down: once one fails, all larger p-values fail
    return decisions


# ---------------------------------------------------------------------------
# Proportions
# ---------------------------------------------------------------------------


def wilson_interval(successes: int, trials: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a single proportion.

    Unlike the normal-approximation (Wald) interval, this stays inside [0, 1],
    never collapses to zero width when no successes are observed, and holds
    its nominal coverage at the small denominators typical of county pilots.
    """
    if trials < 0 or successes < 0 or successes > trials:
        raise AnalysisError(
            f"invalid counts: {successes} successes of {trials} trials",
            code="ANALYSIS_DOMAIN",
        )
    if trials == 0:
        return (0.0, 1.0)
    z = normal_quantile(1.0 - (1.0 - confidence) / 2.0)
    p_hat = successes / trials
    denom = 1.0 + z * z / trials
    center = (p_hat + z * z / (2.0 * trials)) / denom
    margin = (
        z
        * math.sqrt(p_hat * (1.0 - p_hat) / trials + z * z / (4.0 * trials * trials))
        / denom
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass(frozen=True)
class ProportionComparison:
    """Result of comparing a rate between a control and a treatment group."""

    control_successes: int
    control_trials: int
    treatment_successes: int
    treatment_trials: int
    confidence: float
    alpha: float
    control_rate: float
    treatment_rate: float
    control_interval: tuple[float, float]
    treatment_interval: tuple[float, float]
    absolute_difference: float
    difference_interval: tuple[float, float]
    relative_difference: float | None
    p_value: float
    significant: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def direction(self) -> str:
        if self.absolute_difference > 0:
            return "increase"
        if self.absolute_difference < 0:
            return "decrease"
        return "no_change"

    def meets_practical_threshold(self, minimum_effect: float) -> bool:
        """Whether the *whole* confidence interval clears a practical bar.

        Requiring the near bound of the interval to clear the agency's stated
        minimum effect of interest is stricter than requiring the point
        estimate to clear it. It answers "are we confident the change is at
        least as large as the one we said would be worth acting on?" rather
        than "is the change probably not zero?".
        """
        low, high = self.difference_interval
        if minimum_effect >= 0:
            return low >= minimum_effect
        return high <= minimum_effect


def compare_proportions(
    control_successes: int,
    control_trials: int,
    treatment_successes: int,
    treatment_trials: int,
    *,
    confidence: float = 0.95,
    looks: int = 1,
    min_group_size: int = 30,
) -> ProportionComparison:
    """Compare two rates, e.g. completion rate in control vs. treatment.

    The confidence interval on the difference uses Newcombe's method 10
    (hybrid score), which composes the two Wilson intervals rather than
    assuming normality of the difference. The p-value is a two-sided
    two-proportion z-test with pooled variance, evaluated against an alpha
    corrected for the declared number of looks.
    """
    for name, s, n in (
        ("control", control_successes, control_trials),
        ("treatment", treatment_successes, treatment_trials),
    ):
        if n < 0 or s < 0 or s > n:
            raise AnalysisError(
                f"invalid {name} counts: {s} successes of {n} trials",
                code="ANALYSIS_DOMAIN",
            )
    if control_trials == 0 or treatment_trials == 0:
        raise AnalysisError(
            "both groups need at least one observation to be compared",
            code="ANALYSIS_NO_DATA",
        )

    warnings: list[str] = []
    if min(control_trials, treatment_trials) < min_group_size:
        warnings.append(
            f"Smallest group has {min(control_trials, treatment_trials)} observations "
            f"(below the {min_group_size} guidance threshold). Treat this comparison as "
            "directional only."
        )

    alpha = corrected_alpha(1.0 - confidence, looks)
    if looks > 1:
        warnings.append(
            f"Significance threshold tightened from {1.0 - confidence:.3f} to {alpha:.4f} "
            f"to account for {looks} planned looks at the data."
        )

    p_c = control_successes / control_trials
    p_t = treatment_successes / treatment_trials
    diff = p_t - p_c

    # Newcombe hybrid-score interval for the difference of proportions.
    interval_conf = 1.0 - alpha
    l1, u1 = wilson_interval(control_successes, control_trials, interval_conf)
    l2, u2 = wilson_interval(treatment_successes, treatment_trials, interval_conf)
    z = normal_quantile(1.0 - alpha / 2.0)
    lower = diff - z * math.sqrt(
        l2 * (1.0 - l2) / treatment_trials + u1 * (1.0 - u1) / control_trials
    )
    upper = diff + z * math.sqrt(
        u2 * (1.0 - u2) / treatment_trials + l1 * (1.0 - l1) / control_trials
    )
    diff_interval = (max(-1.0, lower), min(1.0, upper))

    # Two-proportion z-test, pooled variance under the null.
    pooled = (control_successes + treatment_successes) / (control_trials + treatment_trials)
    se = math.sqrt(pooled * (1.0 - pooled) * (1.0 / control_trials + 1.0 / treatment_trials))
    if se == 0.0:
        p_value = 1.0
        warnings.append(
            "Both groups produced identical rates with zero variance; no test was possible."
        )
    else:
        z_stat = diff / se
        p_value = 2.0 * (1.0 - normal_cdf(abs(z_stat)))

    relative: float | None = None
    if p_c > 0:
        relative = diff / p_c

    return ProportionComparison(
        control_successes=control_successes,
        control_trials=control_trials,
        treatment_successes=treatment_successes,
        treatment_trials=treatment_trials,
        confidence=confidence,
        alpha=alpha,
        control_rate=p_c,
        treatment_rate=p_t,
        control_interval=wilson_interval(control_successes, control_trials, confidence),
        treatment_interval=wilson_interval(treatment_successes, treatment_trials, confidence),
        absolute_difference=diff,
        difference_interval=diff_interval,
        relative_difference=relative,
        p_value=p_value,
        significant=p_value <= alpha,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Continuous outcomes (e.g. time-to-complete)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeanComparison:
    """Result of comparing a continuous outcome between two groups."""

    control_n: int
    treatment_n: int
    control_mean: float
    treatment_mean: float
    control_sd: float
    treatment_sd: float
    difference: float
    difference_interval: tuple[float, float]
    degrees_of_freedom: float
    p_value: float
    significant: bool
    alpha: float
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _mean_sd(values: Sequence[float]) -> tuple[float, float]:
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var)


def compare_means(
    control: Sequence[float],
    treatment: Sequence[float],
    *,
    confidence: float = 0.95,
    looks: int = 1,
) -> MeanComparison:
    """Welch's unequal-variance t-test with a confidence interval.

    Welch rather than Student because a treatment that changes a workflow
    routinely changes the *spread* of time-to-complete as well as its centre,
    and the equal-variance assumption is not defensible in that case.
    """
    if len(control) < 2 or len(treatment) < 2:
        raise AnalysisError(
            "each group needs at least two observations for a mean comparison",
            code="ANALYSIS_NO_DATA",
        )

    alpha = corrected_alpha(1.0 - confidence, looks)
    n1, n2 = len(control), len(treatment)
    m1, s1 = _mean_sd(control)
    m2, s2 = _mean_sd(treatment)
    v1, v2 = s1 * s1 / n1, s2 * s2 / n2
    se = math.sqrt(v1 + v2)

    warnings: list[str] = []
    if se == 0.0:
        return MeanComparison(
            control_n=n1,
            treatment_n=n2,
            control_mean=m1,
            treatment_mean=m2,
            control_sd=s1,
            treatment_sd=s2,
            difference=m2 - m1,
            difference_interval=(m2 - m1, m2 - m1),
            degrees_of_freedom=float(n1 + n2 - 2),
            p_value=1.0,
            significant=False,
            alpha=alpha,
            warnings=("Both groups had zero variance; no test was possible.",),
        )

    # Welch-Satterthwaite degrees of freedom.
    df = (v1 + v2) ** 2 / (v1 * v1 / (n1 - 1) + v2 * v2 / (n2 - 1))
    t_stat = (m2 - m1) / se
    p_value = 2.0 * (1.0 - student_t_cdf(abs(t_stat), df))

    # Critical t value for the interval, by bisection on the t CDF.
    target = 1.0 - alpha / 2.0
    lo, hi = 0.0, 1000.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if student_t_cdf(mid, df) < target:
            lo = mid
        else:
            hi = mid
    t_crit = (lo + hi) / 2.0

    if min(n1, n2) < 30:
        warnings.append(
            f"Smallest group has {min(n1, n2)} observations; the interval is wide and "
            "sensitive to outliers. Consider reporting the median alongside the mean."
        )

    diff = m2 - m1
    return MeanComparison(
        control_n=n1,
        treatment_n=n2,
        control_mean=m1,
        treatment_mean=m2,
        control_sd=s1,
        treatment_sd=s2,
        difference=diff,
        difference_interval=(diff - t_crit * se, diff + t_crit * se),
        degrees_of_freedom=df,
        p_value=p_value,
        significant=p_value <= alpha,
        alpha=alpha,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def required_sample_size_per_group(
    baseline_rate: float,
    minimum_detectable_effect: float,
    *,
    alpha: float = 0.05,
    power: float = 0.80,
    looks: int = 1,
) -> int:
    """Observations needed *per group* to detect an absolute change.

    ``minimum_detectable_effect`` is an absolute change in the rate: 0.03
    means three percentage points. The result is rounded up.
    """
    if not 0.0 < baseline_rate < 1.0:
        raise AnalysisError(
            f"baseline_rate must be in (0, 1), got {baseline_rate!r}", code="ANALYSIS_DOMAIN"
        )
    if minimum_detectable_effect == 0:
        raise AnalysisError(
            "minimum_detectable_effect must be non-zero", code="ANALYSIS_DOMAIN"
        )
    treated_rate = baseline_rate + minimum_detectable_effect
    if not 0.0 < treated_rate < 1.0:
        raise AnalysisError(
            f"baseline_rate + minimum_detectable_effect must stay in (0, 1); "
            f"got {treated_rate!r}",
            code="ANALYSIS_DOMAIN",
        )
    effective_alpha = corrected_alpha(alpha, looks)
    z_alpha = normal_quantile(1.0 - effective_alpha / 2.0)
    z_beta = normal_quantile(power)
    pooled = (baseline_rate + treated_rate) / 2.0
    numerator = (
        z_alpha * math.sqrt(2.0 * pooled * (1.0 - pooled))
        + z_beta
        * math.sqrt(
            baseline_rate * (1.0 - baseline_rate) + treated_rate * (1.0 - treated_rate)
        )
    ) ** 2
    return math.ceil(numerator / (minimum_detectable_effect**2))


def minimum_detectable_effect(
    baseline_rate: float,
    n_per_group: int,
    *,
    alpha: float = 0.05,
    power: float = 0.80,
    looks: int = 1,
) -> float:
    """Smallest absolute rate change detectable at a given per-group size.

    This is the honest companion to a sample-size calculation: given the
    traffic an agency actually has, what is the smallest improvement this
    pilot could ever find? If that number is larger than the improvement the
    program cares about, the pilot should be rescoped rather than run.
    """
    if n_per_group < 1:
        raise AnalysisError("n_per_group must be >= 1", code="ANALYSIS_DOMAIN")
    effective_alpha = corrected_alpha(alpha, looks)
    z_alpha = normal_quantile(1.0 - effective_alpha / 2.0)
    z_beta = normal_quantile(power)
    se = math.sqrt(2.0 * baseline_rate * (1.0 - baseline_rate) / n_per_group)
    return (z_alpha + z_beta) * se

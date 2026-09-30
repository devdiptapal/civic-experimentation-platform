# Statistical Methods

This document states exactly what the platform computes, why each method was
chosen over the obvious alternative, and where the choices are conservative.
It is written for an evaluator or methodologist reviewing whether results
from this platform can be trusted and compared across jurisdictions.

Every method below is implemented in [`stats.py`](../src/civicexp/stats.py)
on the Python standard library, and verified in
[`test_stats.py`](../tests/test_stats.py) against published reference values
rather than against the implementation's own output.

## The setting these choices are made for

Public-service pilots differ from commercial A/B tests in ways that change
the right statistical defaults:

* **Denominators are small.** A county workflow pilot may have a few hundred
  to a few thousand eligible transactions, not millions.
* **The events that matter most are rare.** Blocking errors and support
  contacts — the guardrails — often sit at 1–5%.
* **Monitoring is continuous and unscheduled.** Staff watch a pilot daily
  and react to operational events, not to a pre-registered interim schedule.
* **The cost of a false positive is asymmetric.** Rolling out a change that
  does not work wastes public money and erodes trust in the next evaluation.
  Failing to detect a small real improvement costs much less.
* **Participants did not opt in.** Someone applying for food assistance has
  no alternative provider and did not consent to be in a test.

Each choice below follows from one or more of these.

## Single proportions: Wilson score interval

**Used for:** the reported rate in each arm.

**Instead of:** the normal-approximation (Wald) interval, `p̂ ± z·√(p̂(1-p̂)/n)`.

**Why:** Wald's coverage is poor at small `n` and breaks down entirely at
extreme rates. With zero observed successes it returns `(0, 0)` — implying
certainty that the true rate is zero — and it can produce bounds outside
`[0, 1]`. Both failures occur precisely in the guardrail case this platform
cares most about: a rare adverse event in a modest sample.

The Wilson interval inverts the score test rather than the Wald test. It
stays within `[0, 1]`, remains non-degenerate at zero successes, and holds
approximately nominal coverage down to small `n`.

> Wilson, E. B. (1927). Probable inference, the law of succession, and
> statistical inference. *Journal of the American Statistical Association*
> 22(158), 209–212.

## Difference of proportions: Newcombe's hybrid score method

**Used for:** the confidence interval on the change between arms — the
number the decision rule is applied to.

**Instead of:** a Wald interval on the difference.

**Why:** the difference of two proportions each near a boundary is not
well approximated by a normal distribution. Newcombe's method 10 composes
the two Wilson intervals instead of assuming normality of the difference,
and in Newcombe's own comparison of eleven methods it was among the best
performing on coverage across the parameter space.

> Newcombe, R. G. (1998). Interval estimation for the difference between
> independent proportions: comparison of eleven methods. *Statistics in
> Medicine* 17(8), 873–890.

## Hypothesis test: two-sided two-proportion z-test

**Used for:** the p-value reported in the technical appendix.

Pooled variance under the null, two-sided, no continuity correction. This is
the conventional choice and is reported for completeness. **It is not what
drives the decision** — see the next two sections.

## Continuous outcomes: Welch's t-test

**Used for:** duration guardrails such as time-to-complete.

**Instead of:** Student's pooled-variance t-test.

**Why:** a change to a workflow routinely changes the *spread* of completion
times as well as the centre. A checklist that helps confident users and
confuses others widens the distribution while barely moving its mean. The
equal-variance assumption is not defensible, and Welch costs almost nothing
when variances happen to be equal.

Degrees of freedom use the Welch–Satterthwaite approximation. The t
distribution CDF is computed from the regularized incomplete beta function
via a modified Lentz continued fraction.

> Welch, B. L. (1947). The generalization of "Student's" problem when several
> different population variances are involved. *Biometrika* 34(1/2), 28–35.

## Repeated looks: Šidák correction

**Used for:** tightening α when `analysis.planned_looks > 1`.

α_corrected = 1 − (1 − α)^(1/k)

**Instead of:** an alpha-spending function such as O'Brien–Fleming or
Pocock.

**Why, and what it costs.** Group-sequential methods are more powerful:
they spend less α early and save it for the final analysis. They also
require the number and timing of looks to be fixed in advance, and they
assume looks occur at planned information fractions.

That assumption does not hold here. Agency monitoring is driven by
operational events — a guardrail moving, a caseworker reporting a problem,
a weekly standing meeting — not by a pre-registered interim schedule. A
correction whose validity depends on look timing would be applied outside
the conditions that make it valid.

The Šidák correction does not depend on when the looks happen. It is
conservative, and it costs power: detecting a 5-point change at 60%
baseline needs about 1,471 units per arm at one look and about 2,100 at
five. The platform states this cost plainly in the `power` command rather
than hiding it.

**This is a deliberate trade, and it is the right direction of error for a
public program.** Under-declaring a win wastes an opportunity; over-declaring
one rolls a change out to everyone.

## Multiple guardrails: Holm–Bonferroni

**Used for:** the guardrail panel, evaluated as a family.

**Why:** testing five guardrails at α = 0.05 each gives roughly a 23%
chance of at least one spurious harm signal. A pilot halted by a false
alarm costs the agency credibility for every evaluation that follows.

Holm's step-down procedure controls the family-wise error rate and is
uniformly more powerful than plain Bonferroni, with no additional
assumptions.

A guardrail whose interval clears its tolerance but which does not survive
the correction is reported as **WATCH** rather than **BREACHED**: visible to
the monitoring team, but not automatically triggering a rollback.

> Holm, S. (1979). A simple sequentially rejective multiple test procedure.
> *Scandinavian Journal of Statistics* 6(2), 65–70.

## The decision rule

Applied in order. The first matching condition wins.

**1. Any confirmed guardrail breach → `ROLLBACK`.**

A guardrail is breached when the *entire* confidence interval on its change
lies beyond the tolerance the agency set, and the signal survives the
family-wise correction. No improvement in the primary metric overrides this.

**2. Otherwise, `PROMOTE` requires practical significance.**

The whole confidence interval on the primary metric must clear the
`minimum_effect_of_interest`, in the direction the program named.

This is stricter than the usual bar, and the distinction matters. Consider
600/1000 versus 640/1000: a 4-point improvement, which looks like a win.
The 95% interval runs from −0.3 to +8.2 points. The data are consistent with
the change making things slightly worse. Requiring the interval to clear the
bar asks "are we confident the effect is at least as large as the one worth
acting on?" rather than "is it probably not exactly zero?".

**3. Otherwise, `INCONCLUSIVE` if the interval is wide relative to the bar.**

Specifically, if the interval width exceeds twice the minimum effect of
interest. This is reported separately from "no effect" because it calls for
a different action: an underpowered pilot should be extended or rescoped,
not treated as evidence that the change does not work. Reporting it as a
null result would be a false negative dressed up as a finding.

**4. Otherwise, `ITERATE`.** The change did not earn adoption, and no harm
was confirmed.

## Sample size and minimum detectable effect

Standard two-proportion power calculation, with α adjusted for planned
looks:

```
n per group = (z_α·√(2·p̄(1-p̄)) + z_β·√(p₁(1-p₁) + p₂(1-p₂)))² / δ²
```

The inverse — minimum detectable effect at a given `n` — is the more useful
direction in practice and is what `civicexp validate` reports. Given the
traffic an agency actually has, what is the smallest improvement this pilot
could ever find? If that number exceeds the improvement the program cares
about, the pilot should be rescoped **before** it runs, not explained away
afterwards.

Configurations that fail this check load with a warning rather than an
error: the agency, not the tool, owns that judgment.

## What the platform does not do

Stated plainly, because knowing a tool's limits is part of using it
responsibly.

* **No Bayesian analysis.** Frequentist intervals only.
* **No sequential testing with early stopping for efficacy.** A pilot runs
  its planned duration. Guardrails may stop it early; good news may not.
* **No covariate adjustment, CUPED, or variance reduction.** These would
  increase power but require pre-period data the platform does not assume
  an agency has.
* **No cluster-robust standard errors.** Assignment is at the unit level. If
  an agency assigns by office or by caseworker, the intervals here will be
  too narrow and the method does not apply.
* **No heterogeneous treatment effect estimation.** Segment analysis must be
  planned in advance and interpreted with multiplicity in mind; the platform
  does not automate it, because automating it invites exactly the fishing
  expedition that equity-motivated segmentation must avoid.
* **No adjustment for interference between units.** If one applicant's
  experience affects another's — plausible where people share a caseworker
  or a community organization — the independence assumption is violated and
  effects may be biased.
* **No multiplicity correction across treatment arms** for the primary
  metric. With more than two arms, correct for this yourself.

Each of these is a real limitation, not a to-do list item. A pilot design
that runs into one should be discussed with a methodologist rather than
forced through the tool.

## How the implementation is verified

[`test_stats.py`](../tests/test_stats.py) checks the distribution functions
against published critical values — `t(0.95, df=10) = 1.812461`,
`z(0.975) = 1.959964`, Wilson's interval for 5/50 — rather than against
this implementation's own output. A statistics library that agrees only
with itself is not verified.

[`test_end_to_end.py`](../tests/test_end_to_end.py) goes further and checks
the promises empirically over simulated pilots where the ground truth is
known:

* **False positive rate.** 400 pilots with genuinely identical arms; the
  share declared "detectable" must stay near the nominal 5%.
* **Interval coverage.** 400 pilots with a known 6-point true effect; the
  95% interval must contain it about 95% of the time.
* **Power.** At the sample size the tool says gives 80% power, roughly 80%
  of pilots must actually detect the effect.

These are the tests that would catch a method that is internally consistent
but does not deliver the error rates it advertises.

# Glossary

Plain-language terms used in this repository.

## A/B Test

A way to compare two versions of a service experience to learn which performs better on a defined metric.

## Aggregated Data

Data summarized across groups (for example, counts or rates) rather than individual records.

## Guardrail Metric

A metric used to detect harm or unintended effects during an experiment.

## Hypothesis

A clear, testable statement about what change is expected and why.

## Pilot

A limited, controlled trial used to test a service change before broader rollout.

## Primary Metric

The main outcome used to evaluate whether an experiment meets its objective.

## Rollback

A planned process to return to the previous service state if risks or problems appear.

## Sample Size

The number of observations needed to make a comparison meaningful for decision-making.

## Segmentation

Looking at results for specific groups (for example, language, region, channel) to check whether effects differ.

## Service Reliability

The ability of a public service system to remain available, understandable, and operational for participants and staff.

## Confidence Interval

A range of values consistent with the data. A 95% interval is built by a
method that captures the true value about 95% of the time. Reporting an
effect without one invites false precision: "completion rose 4 points" and
"rose 4 points, somewhere between -0.3 and +8.2" support very different
decisions.

## Practical Significance

Whether a change is large enough to be worth acting on, as opposed to merely
being detectable. With enough traffic almost any difference becomes
statistically detectable; practical significance asks whether it justifies
the cost of rolling the change out. This platform requires the whole
confidence interval to clear the agreed bar before recommending adoption.

## Pseudonymization

Replacing a direct identifier, such as a case number, with a stable
substitute that cannot be reversed without a secret key. It allows the same
person's actions to be linked together for analysis without storing who they
are.

## Statistical Power

The chance a pilot would detect an effect of a given size if one really
exists. A pilot with too little traffic to detect the improvement it cares
about will usually return "no difference" whether or not the change works,
which is why power is checked before launch rather than after.

## Inconclusive

A result where the data cannot distinguish a worthwhile improvement from no
change at all. Distinct from "no effect", and calling for a different
response: extend or rescope the pilot rather than concluding the change does
not work.

## Small-Cell Suppression

Withholding a statistic computed over very few people, because a breakdown
covering three individuals can identify them. This platform suppresses cells
below a configurable threshold, defaulting to eleven.

## Audit Trail

A record of who approved, started, paused, or ended an evaluation, and when.
This platform's trail is hash-chained, so altering an entry after the fact is
detectable.

## Guardrail Tolerance

The largest amount by which a harm measure may worsen before the pre-agreed
rule requires reverting the change. Fixing it in writing before launch is
what prevents it from being renegotiated once results arrive.

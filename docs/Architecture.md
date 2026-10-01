# Architecture

This document explains how the platform is put together and, more usefully,
*why* it is put together that way. Most of the design is driven by
constraints that are specific to public-sector deployment rather than by
general software preference.

## The five components

The platform implements the five components an agency needs to run one
controlled evaluation end to end.

| # | Component | Module | What it does |
| --- | --- | --- | --- |
| 1 | Assignment | [`assignment.py`](../src/civicexp/assignment.py) | Puts eligible units into approved comparison groups |
| 2 | Outcome recording | [`events.py`](../src/civicexp/events.py) | Validates and stores the outcomes the agency agreed to measure |
| 3 | Comparison | [`analysis.py`](../src/civicexp/analysis.py), [`stats.py`](../src/civicexp/stats.py) | Compares the versions and applies the pre-registered decision rule |
| 4 | Reporting | [`report.py`](../src/civicexp/report.py) | Produces a standardized readout in plain language |
| 5 | Administrative control | [`lifecycle.py`](../src/civicexp/lifecycle.py) | Approve, pause, resume, or end an evaluation |

Two further components answer the questions that come *before* "what was
the effect?":

| Component | Module | What it does |
| --- | --- | --- |
| Trust checks | [`diagnostics.py`](../src/civicexp/diagnostics.py) | Sample ratio mismatch, completeness, novelty — can these numbers be trusted at all |
| Equity review | [`segments.py`](../src/civicexp/segments.py) | Did the effect reach every pre-registered group |

Supporting modules: [`config.py`](../src/civicexp/config.py) validates the
one artifact an agency actually approves; [`privacy.py`](../src/civicexp/privacy.py)
enforces the data rules in code; [`audit.py`](../src/civicexp/audit.py)
keeps a tamper-evident decision trail; [`eligibility.py`](../src/civicexp/eligibility.py)
holds the declarative rule language; [`metrics.py`](../src/civicexp/metrics.py)
holds the standard metric definitions a config can reference by name;
[`wizard.py`](../src/civicexp/wizard.py) and
[`templates/`](../src/civicexp/templates/) turn a conversation into a valid
configuration; [`simulate.py`](../src/civicexp/simulate.py) generates
synthetic data for dry runs.

## How data flows

```text
                 experiment.json  (the approved artifact)
                         │
                   ┌─────▼─────┐
                   │  config   │  validates everything at once,
                   │           │  refuses placeholders, checks power
                   └─────┬─────┘
                         │
        ┌────────────────┼─────────────────┐
        │                │                 │
  ┌─────▼─────┐   ┌──────▼──────┐   ┌──────▼──────┐
  │ lifecycle │   │  assignment │   │   privacy   │
  │  approve  │──▶│  (stateless │◀──│ pseudonymize│
  │   pause   │   │   hashing)  │   │  screen PII │
  │    end    │   └──────┬──────┘   └──────┬──────┘
  └─────┬─────┘          │                 │
        │          variant decision        │
        │                │                 │
        │         ┌──────▼──────┐          │
        │         │   events    │◀─────────┘
        │         │ (append-only│  validated at write time
        │         │   JSONL)    │
        │         └──────┬──────┘
        │                │
        │         ┌──────▼──────┐
        │         │  analysis   │  stats + pre-registered decision rule
        │         └──────┬──────┘
        │                │
  ┌─────▼─────┐   ┌──────▼──────┐
  │   audit   │──▶│   report    │  plain-language readout
  │hash-chain │   └─────────────┘
  └───────────┘
```

## Design decisions and their reasons

### Assignment is stateless and deterministic

A unit's variant is `HMAC-SHA256(salt, "assign:" + unit_id)`, scaled into
`[0, 1)` and compared against the cumulative traffic shares. There is no
assignment table.

This matters for three reasons that are specific to this setting:

* **Consistency across sessions.** A person who starts an application on
  Monday and returns on Thursday sees the same version. With a database of
  assignments, a failed write or a cache miss silently shows them the other
  one, and their experience — not just the data — gets worse.
* **Post-hoc auditability.** An oversight reviewer with the config and the
  identifier list can recompute every assignment and confirm the analyzed
  groups are the groups the software actually built. That check is not
  available if assignment was a sequence of database writes.
* **No new store of personal records.** There is no assignment table to
  secure, retain, or breach. Reducing the number of systems holding
  applicant-linked rows is a privacy win before any policy is written.

Enrollment (whether a unit is in the evaluation at all) uses a *separate*
hash purpose from arm assignment. Without that separation, widening the
ramp from 20% to 60% would reshuffle everyone already enrolled.
[A test asserts this property directly](../tests/test_assignment.py).

### No runtime dependencies

The package imports only the standard library. The statistical distribution
functions — normal quantile, Student's t CDF via a continued-fraction
incomplete beta — are implemented in [`stats.py`](../src/civicexp/stats.py)
rather than taken from SciPy.

The reason is deployment reality, not preference. Adding a third-party
package to a reviewed agency environment can require its own security
review and procurement step, and each dependency is supply-chain risk
carried by every agency that deploys the platform. A tool that cannot be
installed does not improve any service.

CI enforces this: the first job runs the entire test suite *before*
installing anything, so the promise cannot quietly lapse.

### Validation happens at write time

Event payloads are screened for identifier-shaped field names, long free
text, and nested structures when they are recorded, not when they are
analyzed.

A field that should never have been collected is a privacy incident from
the moment it is written to disk. Finding it during analysis is too late —
the data already exists, already needs an incident report, and already has
to be deleted under whatever process the agency has for that.

### Counting units, not events

Rate metrics count *distinct units* that produced an event, never raw event
counts. A person who retries a failing upload six times is one person having
a bad experience.

Counting events would inflate exactly the measures an agency most wants to
watch: error rate and support-contact rate would both look worse in whichever
arm frustrates people into retrying, which is the opposite of the signal.

### The checks run in a deliberate order

`analyze()` evaluates in this sequence, and the order encodes what
outranks what:

1. **Trust checks.** A sample ratio mismatch returns `INVALID` immediately.
   Nothing below it is evaluated, because the numbers are not measuring the
   change.
2. **Guardrails.** A confirmed breach forces `ROLLBACK`.
3. **The primary metric**, against the practical bar.
4. **Equity.** A confirmed harm to a pre-registered group escalates to
   `ROLLBACK` from any other outcome.

`INVALID` and `INCONCLUSIVE` are deliberately distinct. Inconclusive means
the pilot was sound but too small — run it longer. Invalid means the pilot
was broken — fix it and start again. Collapsing them would let a broken
pipeline be mistaken for insufficient traffic.

### Harm outranks benefit, unconditionally

The decision rule has no trade-off in which a large improvement in the
primary metric purchases a tolerated increase in a guardrail. A confirmed
guardrail breach forces `ROLLBACK` regardless of what else happened.

This is a values choice, made explicit in code, and it reflects that the
people affected by a public benefits workflow did not opt into the
experiment and often have no alternative provider.

### Practical significance, not just statistical significance

`PROMOTE` requires the *entire* confidence interval to clear the minimum
effect the program said was worth acting on — not merely that the interval
excludes zero.

With enough traffic, almost any change is "statistically significant". The
question a program manager actually needs answered is "is this worth the
cost of rolling out?", and only the practical bar answers it.

### Guardrails are tested as a family

Checking five guardrails at α = 0.05 each would raise the chance of at
least one spurious harm signal to roughly 23%. The platform applies a
Holm-Bonferroni correction across the guardrail panel, so a single metric
wandering by chance does not halt a pilot.

A pilot stopped by a false alarm costs an agency real credibility for the
next one, which is a cost that does not appear in any statistics textbook.

### Repeated looks are paid for

Agencies monitor pilots daily. The config declares `planned_looks`, and the
significance threshold is tightened accordingly (Šidák). Looking at data
four times a week for four weeks and stopping when it looks good is a
reliable way to produce a finding that does not replicate.

The correction used is deliberately conservative; the reasoning is in
[Statistical-Methods.md](Statistical-Methods.md).

### The audit trail is hash-chained

Each entry stores the digest of its own content plus the digest of the
previous entry. Altering, deleting, or reordering any entry breaks the chain
from that point on.

This is tamper-*evidence*, not tamper-*proofing* — someone with write access
can recompute the whole chain. It defends against the realistic failure
mode: a record quietly corrected after the fact. For a stronger guarantee,
publish the head digest somewhere the operator does not control.

## Storage format

Events and audit entries are JSON Lines. Not a database, deliberately:

* an agency analyst can open the file with any tool they already have;
* a records officer can read it without this software installed;
* deletion for retention purposes is a file operation, not a migration;
* the format is trivially archivable to whatever the agency's records
  system accepts.

At pilot scale — thousands to low millions of events — this is entirely
adequate. An agency running continuous evaluation at higher volume would
replace the storage layer; the interface it would need to satisfy is the
aggregation methods on `EventLog`.

## What this is not

* Not a case-management system, and not a replacement for one.
* Not an eligibility engine. It makes no determination about any person's
  benefits, and nothing in it should ever be wired to one.
* Not production-hardened. It is a reference implementation for pilots and
  adaptation. See [SECURITY.md](../SECURITY.md) for what would need to be
  added before production use.
* Not a substitute for legal, policy, or procurement review in any
  jurisdiction.

## Extending it

The most likely extensions, in rough order of demand:

| Want | Where to start |
| --- | --- |
| A new outcome event type | `EVENT_TYPES` in `events.py`, and `docs/Metrics-Schema.md`. This is a governance change: it affects comparability with already-published results. |
| A continuous primary metric | `analyze()` in `analysis.py`; `compare_means` already exists and is used for duration guardrails. |
| More than one treatment arm | Already supported. Pass `--variant` to name the arm; note that comparing several arms needs a multiplicity correction the current code does not apply to the primary metric. |
| A different storage backend | Reimplement the aggregation methods on `EventLog`. |
| Stratified or blocked assignment | `Assigner.assign`; keep the deterministic-hash property or the audit guarantees are lost. |
| A web dashboard | Build on `AnalysisResult.to_dict()`, which is JSON-serializable by design. |
| A new workflow template | Add a JSON file to `src/civicexp/templates/` and register it in `wizard.TEMPLATES`. |
| A new standard metric | Add it to `metrics.STANDARD_METRICS`. This is a governance change: it affects comparability across agencies. |
| Assignment in another language | Reimplement the algorithm documented in [Integration-Guide.md](Integration-Guide.md) and check it against `tests/test_assignment.py::TestDocumentedAlgorithmContract`. |

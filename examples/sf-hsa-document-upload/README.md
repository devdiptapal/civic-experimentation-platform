# Worked example: plain-language document requirements

A complete, reproducible pilot demonstrating all five platform components
on synthetic data.

**Everything in this folder is generated.** Run `make example` (or
`python examples/sf-hsa-document-upload/run_example.py`) to reproduce every
file here byte for byte. CI fails if the committed artifacts drift from what
the code produces.

## The scenario

Applicants must upload proof of identity and proof of income to finish a
benefits application. Support staff report that people frequently upload the
wrong document type, get asked to resubmit, and some abandon the application
at that point.

**The tested change:** replace a dense paragraph of requirements with a short
checklist naming each accepted document and showing an example of each.

No eligibility rule, required document, or policy changes — only the
on-screen explanation of what to upload. This is deliberately the kind of
low-risk, reversible change that
[the deployment guide](../../docs/Pilot-Deployment-Guide.md) recommends for
a first pilot.

**The hypothesis:** more applicants will complete the upload step on their
first attempt, without increasing calls to the support line.

## Two scenarios, one configuration

Both scenarios use the same config and the same random seed. They differ in
exactly one underlying parameter: the blocking-error rate of the tested
version.

| | Completion | What else happened | Decision |
| --- | --- | --- | --- |
| [**Scenario A**](readout-scenario-a.md) | +7.0 points | nothing | **PROMOTE** |
| [**Scenario B**](readout-scenario-b.md) | +7.0 points | blocking errors rose 4.5 points | **ROLLBACK** |
| [**Scenario C**](readout-scenario-c.md) | +7.0 points | no benefit for Spanish speakers | **PROMOTE**, with the gap named |

**Scenarios B and C are the point of this example.** All three measure the
same improvement in completion — a +7 point gain whose entire confidence
interval clears the 3-point bar the program set. Most dashboards would
report all three as clear wins.

**B is reverted** because blocking errors rose past the 1-point tolerance
the agency agreed to before launch. Harm outranks benefit unconditionally,
and the rule was fixed in writing before anyone saw the data.

**C is the harder one.** No guardrail moves. Every headline number says
ship it. But the checklist was written in English and the translated flow
still shows the old paragraph, so the entire benefit goes to
English-preference applicants. The equity review — over groups named
*before* launch — reports that Spanish-preference applicants saw no
measurable change.

The recommendation stays PROMOTE, because the change helped many people and
harmed nobody. What differs is that the report says plainly who it did not
reach, so the agency ships the translation alongside it instead of
discovering the gap a year later. Reporting the average alone would have
widened an access gap and called it a success.

## Files

| File | What it is |
| --- | --- |
| [`experiment.json`](experiment.json) | The approved configuration — hypothesis, eligibility, metrics, guardrails, decision rule, privacy posture. This one artifact drives everything. |
| [`run_example.py`](run_example.py) | The script that generates everything else here |
| `events-scenario-a.jsonl` | ~57,000 synthetic outcome events, scenario A |
| `events-scenario-b.jsonl` | ~57,000 synthetic outcome events, scenario B |
| `events-scenario-c.jsonl` | ~57,000 synthetic outcome events, scenario C |
| `audit-log.jsonl` | The hash-chained governance record: sign-offs, approval, start, pause, resume, rollback, archive |
| [`readout-scenario-a.md`](readout-scenario-a.md) | The plain-language readout an agency would circulate |
| [`readout-scenario-b.md`](readout-scenario-b.md) | Same, for the rollback scenario |
| [`readout-scenario-c.md`](readout-scenario-c.md) | Same, for the equity-gap scenario |

## What the script demonstrates

Running it walks through the whole lifecycle, including the parts that are
supposed to fail:

1. **Pre-launch power check.** Confirms 6,000 units per group can detect the
   3-point change the program cares about. (At the 1,000 per group this
   example first used, validation reported the pilot as underpowered and
   said it would need about 5,719 — which is how the number was chosen.)

2. **Collection blocked before approval.** `require_collecting()` raises
   while the experiment is in draft.

3. **Approval refused with partial sign-off.** With only two of five
   reviewers recorded, `approve()` raises and names who is missing.

4. **Approval, start, pause, resume.** A mid-pilot pause for an unrelated
   outage, with the reason recorded in the audit trail.

5. **Assignment and outcome recording** for 14,000 synthetic applicants,
   with identifiers pseudonymized before anything is written and coded
   segment attributes recorded at entry.

6. **Trust checks** — sample ratio, completeness, and whether the effect
   faded over the pilot window — run before any result is reported.

7. **Analysis and the decision rule** applied to both scenarios.

8. **Rollback and archive**, driven by the decision rule rather than by
   operator preference.

9. **Tamper detection.** The script edits one approval record in a copy of
   the audit log and shows that verification fails and names the entry:

   ```
   Rewriting entry 2: the privacy reviewer's sign-off.
   After editing one approval record: valid=False
     entry 2 ('approval:sign_off') does not match its recorded hash;
     its contents were altered after it was written
   ```

## About the data

All data here is **synthetic**, generated by
[`simulate.py`](../../src/civicexp/simulate.py) from a fixed seed. No real
applicant data appears anywhere in this repository, and none should ever be
committed to it.

The generator models completion, blocking errors, and support contacts as
independent Bernoulli draws, and task durations as log-normal — service task
times are right-skewed, and a symmetric distribution would understate the
long tail that drives abandonment.

Because the truth is known by construction, the same generator is used in
[the property tests](../../tests/test_end_to_end.py) to check that the
platform's false-positive rate, interval coverage, and power match what it
claims.

## Adapting this for a real pilot

1. Copy `experiment.json` and edit it for your workflow.
2. **Generate a new salt** with `civicexp salt`. Never reuse this one, and
   treat the salt as a secret — see the
   [threat model](../../docs/Threat-Model.md#t5--pseudonym-reversal-by-guessing-identifiers).
3. Run `civicexp validate` and resolve every warning before going further.
4. Run `civicexp preview` against last month's identifiers to confirm the
   split before touching live traffic.
5. Simulate with your expected volumes and rates, and confirm the readout
   says what you need it to say — while there is still time to fix the
   instrumentation.
6. Complete the
   [approval checklist](../../docs/Experiment-Approval-Checklist.md) and
   record each sign-off through the lifecycle.
7. Do not commit your real config, event log, or audit log to a public
   repository.

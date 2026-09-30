# Pilot Deployment Guide

This guide is for agencies planning a small, low-risk pilot of a digital service improvement.

Use this guide with:
- `docs/Evaluation-Template.md`
- `docs/Experiment-Approval-Checklist.md`
- `PRIVACY.md`

**Tooling for each step.** The platform now implements most of what this
guide describes. Where a step has a command, it is shown inline. A complete
worked example following this guide start to finish is in
[`examples/sf-hsa-document-upload/`](../examples/sf-hsa-document-upload/).

| Step in this guide | Command |
| --- | --- |
| 5. Complete the evaluation plan | `civicexp validate experiment.json` |
| 5. Check the pilot can answer its question | `civicexp power --config experiment.json --available N` |
| 6. Confirm the split before launch | `civicexp preview experiment.json --units 10000` |
| 6. Dry-run the readout on synthetic data | `civicexp simulate` then `civicexp report` |
| 7. Record each reviewer sign-off | `ExperimentLifecycle.sign_off(...)` |
| 8. Monitor guardrails | `civicexp analyze` (exit code 2 on a breach) |
| 9. Produce the readout | `civicexp report experiment.json events.jsonl --out readout.md` |
| 10. Confirm the record was not altered | `civicexp verify audit-log.jsonl` |

**One warning before you start.** Generate your own assignment salt with
`civicexp salt`, and treat the config containing it as a secret. It keys the
HMAC that pseudonymizes applicant identifiers, and identifier spaces in
benefits systems are small enough to brute-force if the salt leaks. See
[Threat-Model.md](Threat-Model.md#t5--pseudonym-reversal-by-guessing-identifiers).

## 1. Define Pilot Scope and Outcome

Document:
- Service area (for example: form completion, eligibility steps, outbound communication)
- Pilot objective in one sentence
- What will change and what will stay the same
- Expected user and staff impact

Keep the first pilot narrow and operationally reversible.

## 2. Confirm Prerequisites

People and roles:
- Program owner
- Operations lead
- Privacy reviewer
- Legal/policy reviewer
- Analytics/evaluation lead
- Technical implementation lead

Operational readiness:
- Baseline process documented
- Support channels prepared for participant questions
- Rollback authority identified

## 3. Establish Data Rules Before Build

- List only required data fields.
- Default to non-PII metrics.
- Define who can access raw data.
- Define retention and deletion timeline.
- Confirm aggregation thresholds for reporting.

If any PII is required, document explicit justification and controls.

## 4. Select Experiment Type

Choose a low-risk experiment pattern:
- Content variation (for example, plain-language text alternatives)
- Sequence variation (ordering of steps)
- Reminder/communication timing variation

Avoid high-impact policy changes in initial pilots.

## 5. Complete the Evaluation Plan

Fill in `docs/Evaluation-Template.md` with:
- Hypothesis
- Primary metric
- Guardrails (harm-prevention metrics)
- Segmentation approach
- High-level sample size approach
- High-level analysis plan
- Decision rule

Approval should occur before launch.

## 6. Prepare Implementation and Instrumentation

- Implement variant routing logic.
- Confirm event logging for primary and guardrail metrics.
- Validate data capture in a non-production environment.
- Dry-run summary report outputs with synthetic or placeholder data.

## 7. Pre-Launch Checklist

Before launch, confirm:
- [ ] `docs/Experiment-Approval-Checklist.md` is complete
- [ ] Evaluation plan is signed off by required reviewers
- [ ] Rollback criteria are documented
- [ ] Communication plan exists for internal support teams
- [ ] Monitoring owner is assigned

## 8. Launch and Monitor

During pilot:
- Monitor primary and guardrail metrics on a defined cadence
- Track operational incidents and participant feedback
- Pause or rollback if guardrail thresholds are exceeded

## 9. Measurement Plan and Readout

At pilot close:
- Compare outcomes against baseline/control
- Report uncertainty and practical significance (not only directional changes)
- Document known limitations and potential confounders
- Record recommendations for next iteration

Use plain-language summaries for non-technical stakeholders.

## 10. Rollback and Contingency Plan

Define in advance:
- Trigger conditions for rollback
- Who can authorize rollback
- How quickly rollback can be executed
- How support staff and participants will be informed

Run at least one rollback simulation before launch when feasible.

## Suggested Pilot Artifacts

- Pilot charter (one page)
- Completed evaluation template
- Completed approval checklist
- Data handling note
- Launch decision log
- Final readout memo

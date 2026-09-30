# Standards and Policy Alignment

This document maps the platform's capabilities to the federal statutes,
memoranda, and executive actions that ask agencies to build evaluation
capacity and to make digital services measurable.

It is written for a program or policy reviewer assessing whether adopting
this tool helps meet an existing obligation. **It is a mapping, not a
compliance claim.** Nothing here is legal advice, and no software can make
an agency compliant with anything. Each agency remains responsible for its
own legal, policy, and procurement review.

## Summary table

| Authority | What it asks for | What the platform provides |
| --- | --- | --- |
| Foundations for Evidence-Based Policymaking Act (P.L. 115-435) | Agencies build capacity to evaluate programs and use evidence in decisions | A reusable evaluation method, pre-registered decision rules, and standardized readouts that make findings reusable rather than one-off |
| OMB M-19-23 (Evidence Act Phase 1 implementation) | Evaluation planning, evidence-building, and open data by default | Machine-readable configs and JSON results; a documented plan before launch; an auditable record afterwards |
| 21st Century Integrated Digital Experience Act (P.L. 115-336) | Modernize federal websites and digital services; make them accessible and data-informed | Measurement of completion, error, and time-to-complete for digital workflows; the evidence base for prioritizing which improvements to make |
| GPRA Modernization Act of 2010 (P.L. 111-352) | Set goals, measure performance, review progress | Defined primary and guardrail metrics with stated measurement windows and a documented review cadence |
| Payment Integrity Information Act of 2019 (P.L. 116-117) | Reduce improper payments; identify root causes | Measurement of error rates and first-time-correct submission, which are upstream drivers of improper payments caused by incorrect or incomplete applications |
| OPEN Government Data Act (Title II of P.L. 115-435) | Government data open and machine-readable by default | JSON Lines event logs, JSON analysis output, and Markdown readouts — no proprietary formats anywhere |

## Where each capability comes from

### Evidence Act: evaluation capacity that outlives one pilot

The Evidence Act asks agencies to build durable evaluation *capacity*, not
to commission individual studies. The distinction is the whole design
premise of this project.

The platform contributes capacity in three concrete ways:

* **The evaluation plan is executable.** The config in
  [`experiment.json`](../examples/sf-hsa-document-upload/experiment.json) is
  simultaneously the reviewed plan and the thing the software runs. The
  hypothesis, the primary metric, the guardrails, the decision rule, and
  the privacy posture cannot drift apart from what was approved, because
  there is only one artifact.
* **Decision rules are fixed before launch.** `minimum_effect_of_interest`
  and every guardrail tolerance are required fields. A config that leaves
  them as placeholder text does not load — see
  [`test_config.py`](../tests/test_config.py). This removes the most common
  way an evaluation becomes unfalsifiable: deciding what counts as success
  after seeing the results.
* **Findings are reusable across jurisdictions.** The readout format is
  standardized, so a county reading three of them from elsewhere can
  compare them directly. That is the step that turns a single pilot into
  transferable evidence.

### OMB M-19-23: open data and documented evidence-building

Every artifact the platform produces is open and machine-readable by
default: JSON Lines for events, JSON for analysis results, Markdown for
readouts. There is no database export step and no proprietary format.

`AnalysisResult.to_dict()` is JSON-serializable by contract, with a test
asserting it, so results can feed an agency's own evidence inventory
without bespoke integration work.

### 21st Century IDEA: measuring digital service quality

IDEA requires federal digital services to be accessible, mobile-friendly,
and improved based on data. The platform measures the outcomes that
correspond to the user-experience obligations:

* **Completion rate** — whether people can finish the task at all;
* **Error rate** — whether the service blocks them;
* **Time to complete** — whether it wastes their time;
* **Support contact rate** — whether the service is self-explanatory.

The eligibility rule language supports segmenting by channel, device type,
and preferred language, so an agency can check whether an improvement
reaches everyone or only some groups. The platform deliberately does not
automate segment discovery: see the note on heterogeneous effects in
[Statistical-Methods.md](Statistical-Methods.md).

### GPRA Modernization Act: measurable goals and regular review

Each experiment declares a primary metric with an exact definition and
measurement window, guardrails with numeric tolerances, and a monitoring
cadence. The lifecycle state machine records each review as an audit entry,
so "we reviewed progress quarterly" is a verifiable claim rather than an
assertion.

### Payment Integrity Information Act: upstream of improper payments

A significant share of improper payments in benefits programs originates in
applications that were incomplete, incorrect, or submitted with the wrong
supporting documents — not in fraud. Those are consequences of service
design, and they are measurable at the point where the design fails.

The worked example in this repository tests exactly such a change: a
plain-language list of accepted documents at the upload step, measured by
whether people succeed on the first attempt. Reducing wrong-document
submissions reduces both the rework burden on staff and the error rate that
propagates into payment integrity.

### OPEN Government Data Act: no proprietary formats

Stated above; the practical test is that every artifact in
[`examples/`](../examples/sf-hsa-document-upload/) can be read with a text
editor.

## Privacy and records obligations

These are not evaluation mandates, but they constrain any system touching
benefits data. The platform's posture:

| Concern | How the platform addresses it |
| --- | --- |
| Privacy Act of 1974 / system of records | The platform creates **no** new system of records by design: assignment is stateless, identifiers are pseudonymized with HMAC-SHA256 before storage, and no applicant-linked row is created |
| Data minimization | Field-name screening rejects identifier-shaped fields at write time unless explicitly allowlisted with a documented justification |
| Disclosure avoidance | Small-cell suppression, default threshold 11, applied to published aggregates |
| Records retention | `retention_days` is a required part of the config; event logs are plain files, so deletion is a file operation rather than a database migration |
| Accessibility (Section 508) | The platform produces Markdown and plain text, which are accessible by default. Accessibility of the *service being tested* is the agency's responsibility, and is a required item on the approval checklist |

## What alignment does not mean

* It does not mean an agency using this platform is compliant with any of
  these authorities. Compliance depends on the agency's own program,
  process, and legal review.
* It does not mean the platform has been assessed, certified, or authorized
  by any federal or state body. It has not.
* It does not constitute legal advice.
* It does not remove any obligation to conduct a privacy impact assessment,
  a records review, a security authorization, or procurement review before
  operational use.

## Related documents

* [PRIVACY.md](../PRIVACY.md) — the data-handling commitments
* [GOVERNANCE.md](../GOVERNANCE.md) — decision-making and experiment governance
* [Experiment-Approval-Checklist.md](Experiment-Approval-Checklist.md) — pre-launch review
* [Threat-Model.md](Threat-Model.md) — what the platform defends against and what it does not
* [Statistical-Methods.md](Statistical-Methods.md) — methodological choices and their limits

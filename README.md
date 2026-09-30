# Civic Experimentation Platform

**Open-source infrastructure for testing whether changes to digital public
services actually work.**

[![CI](https://github.com/devdiptapal/civic-experimentation-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/devdiptapal/civic-experimentation-platform/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Zero dependencies](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen.svg)](pyproject.toml)

Millions of people in the United States apply for Medicaid, SNAP,
unemployment insurance, and housing assistance through online forms every
year. Small design problems in those forms — a confusing question, an
unclear list of required documents, a badly timed reminder — cause people to
abandon applications, submit incorrect information, or wait longer than they
should.

Large technology companies answer questions like "does this change actually
help?" with experimentation infrastructure. Public agencies mostly do not
have it. The commercial tools are proprietary, priced for private-sector
budgets, and built around revenue goals rather than equitable access or
administrative burden.

This project is that infrastructure, built for public services and given
away.

```
$ civicexp analyze experiment.json events.jsonl

Document upload: plain-language requirements list [exp-doc-upload-001]
Decision: ROLLBACK — Stop and revert the tested version
completion_rate: 62.3% -> 69.6% (+7.3 percentage points, CI +5.2 to +9.5)
Guardrails breached: error_rate
```

That output is the point of the whole project. Completion went **up** by
7.3 points — a result most dashboards would report as a clear win. The
platform recommends reverting the change anyway, because blocking errors
rose past the tolerance the agency set before launch. Harm outranks benefit,
and the rule was fixed in writing before anyone saw the data.

---

## Try it in thirty seconds

No installation, no dependencies, no configuration.

```bash
git clone https://github.com/devdiptapal/civic-experimentation-platform.git
cd civic-experimentation-platform
python3 examples/sf-hsa-document-upload/run_example.py
```

This runs a complete pilot on synthetic data: approves it through five
reviewer sign-offs, refuses to start without them, generates ~49,000
outcome events for 12,000 applicants, analyzes two scenarios, writes the
plain-language readouts, and demonstrates that editing the audit log is
detectable.

To run the test suite (250 tests, still no dependencies):

```bash
make test
```

To install the command-line tool:

```bash
pip install -e .
civicexp --help
```

## What it does

Five components, which together are everything needed to run one controlled
evaluation end to end.

### 1. Assign eligible people to comparison groups

Deterministic, stateless, hash-based assignment. The same person always sees
the same version — across sessions, servers, and restarts — and there is no
assignment database to secure or breach. An auditor can recompute every
assignment afterwards from the config alone.

```bash
$ civicexp assign experiment.json applicant-99213 --attributes '{"channel":"web",...}'
Variant:    plain_language_checklist
Reason:     assigned to 'plain_language_checklist' (bucket 0.963186)
Pseudonym:  9ec39da0e069f4b9…
```

### 2. Record the outcomes the agency agreed to measure

Append-only JSON Lines, validated at write time against the approved event
taxonomy and the privacy policy. A raw case number **cannot** be recorded:
the log rejects any identifier that is not already pseudonymized.

### 3. Compare the versions

Wilson score intervals, Newcombe's method for the difference of proportions,
Welch's t-test for durations, Holm-Bonferroni across the guardrail panel, and
a significance threshold tightened for the number of times the agency plans
to look at the data. All implemented on the Python standard library.

### 4. Explain the result in plain language

A standardized readout written for a program manager, not a data scientist.
The recommendation comes first; no effect is ever reported without its
uncertainty; statistical jargon is confined to a technical appendix.

[See a full example readout →](examples/sf-hsa-document-upload/readout-scenario-b.md)

### 5. Approve, pause, or end the evaluation

An explicit state machine with a tamper-evident, hash-chained audit trail.
Nothing collects data without five recorded sign-offs. Pausing is always
available and never destructive. Editing the record afterwards is detectable:

```bash
$ civicexp verify audit-log.jsonl
AUDIT CHAIN INVALID: entry 2 ('approval:sign_off') does not match its
recorded hash; its contents were altered after it was written
```

## What makes it different

Most of the design follows from constraints specific to public services
rather than general software preference.

**It will not let you run a pilot that cannot answer its question.**
Validation computes the smallest effect your traffic could detect and tells
you before launch if that is larger than the effect you said you cared
about:

```
$ civicexp validate experiment.json
warning: Underpowered: with 1,000 units per group the smallest change this
pilot can reliably detect is about 7.2 percentage points, but the program
says it cares about 3.0. Detecting that would need roughly 5,719 units per
group.
```

**Harm outranks benefit, unconditionally.** There is no trade-off in which a
large completion win purchases a tolerated increase in blocking errors. The
people using a benefits workflow did not opt into the experiment and usually
have no alternative provider.

**Statistical significance is not enough.** Promotion requires the *entire*
confidence interval to clear the improvement the program said was worth
acting on. A 4-point gain whose interval runs from −0.3 to +8.2 is not a
win, and the platform says so.

**Underpowered is reported as inconclusive, not as "no effect."** These call
for different actions, and conflating them turns a false negative into a
finding.

**Privacy is enforced in code, not only in policy.** Identifiers are
pseudonymized with HMAC-SHA256 before storage. Identifier-shaped field names
are rejected at ingestion. Small cells are suppressed. A config whose
eligibility rules reference an SSN field does not load.

**Zero runtime dependencies.** Adding a package to a reviewed agency
environment can require its own security review, and every dependency is
supply-chain risk carried by every agency that deploys this. CI runs the
whole suite before installing anything, so the promise cannot lapse.

**Placeholders do not load.** `minimum_effect_of_interest: "placeholder"` is
a validation error. An experiment with no stated effect of interest cannot
produce a decision, and deciding what counts as success after seeing results
is the most common way an evaluation becomes unfalsifiable.

## Who this is for

- State and county agencies delivering benefits and social services
- Civic nonprofits and public-interest technology teams
- Digital service teams inside government
- Researchers and evaluators supporting service improvement

## Documentation

| Document | What it covers |
| --- | --- |
| [Architecture](docs/Architecture.md) | How the pieces fit together, and why each design choice was made |
| [Statistical Methods](docs/Statistical-Methods.md) | Every method, its alternative, and why this one — including what the platform deliberately does not do |
| [Threat Model](docs/Threat-Model.md) | What is defended against, what is not, and what an agency must add before production |
| [Standards Alignment](docs/Standards-Alignment.md) | Mapping to the Evidence Act, 21st Century IDEA, GPRA Modernization, PIIA, and the OPEN Government Data Act |
| [Pilot Deployment Guide](docs/Pilot-Deployment-Guide.md) | Step-by-step for an agency team planning a first pilot |
| [Evaluation Template](docs/Evaluation-Template.md) | The pre-launch evaluation plan |
| [Experiment Approval Checklist](docs/Experiment-Approval-Checklist.md) | Pre-launch review across program, privacy, legal, and operations |
| [Metrics Schema](docs/Metrics-Schema.md) | The event taxonomy and metric definitions |
| [Glossary](docs/Glossary.md) | Plain-language definitions |
| [Governance](GOVERNANCE.md) · [Privacy](PRIVACY.md) · [Security](SECURITY.md) | Project and experiment governance |

## The worked example

[`examples/sf-hsa-document-upload/`](examples/sf-hsa-document-upload/)
contains a complete, reproducible pilot: a plain-language checklist of
accepted documents replacing a dense requirements paragraph at the upload
step of a benefits application.

Two scenarios run from the same configuration and the same random seed,
differing only in the underlying blocking-error rate:

| | Completion | Blocking errors | Decision |
| --- | --- | --- | --- |
| [Scenario A](examples/sf-hsa-document-upload/readout-scenario-a.md) | 62.3% → 69.6% | unchanged | **PROMOTE** |
| [Scenario B](examples/sf-hsa-document-upload/readout-scenario-b.md) | 62.3% → 69.6% | +4.5 points | **ROLLBACK** |

Scenario B is the reason the project exists. A platform that reported only
the primary metric would have recommended shipping a change that was quietly
failing more applicants.

Everything in that folder is regenerated by `make example`, and CI fails if
the committed artifacts drift from what the code produces.

## Status and scope

**v0.2.0 — working reference implementation.**

**What is here:** a runnable platform with all five components, 250 tests,
CI across Python 3.10–3.13 on Linux, macOS, and Windows, a complete worked
example on synthetic data, and the governance documentation.

**What is not here:** live integrations with any agency system, any real
user data, production hardening, or an operational deployment. This is a
reference implementation intended for pilots and adaptation. See the
[threat model](docs/Threat-Model.md) for what an agency must add before
operational use.

**No real data appears anywhere in this repository, and none should ever be
committed to it.** Every example runs on seeded synthetic data generated by
[`simulate.py`](src/civicexp/simulate.py), and CI scans for
identifier-shaped strings on every push.

The platform makes no eligibility determinations, does not replace any
case-management system, and is not legal, policy, or compliance advice.

## Roadmap

See [ROADMAP.md](ROADMAP.md). In brief: pilot-ready release, then
multi-agency reuse, then replication and sustainability.

## Contributing

Contributions are welcome, including documentation-only ones. See
[CONTRIBUTING.md](CONTRIBUTING.md). Changes touching measurement, privacy,
or governance get extra review because they affect whether results from
different agencies remain comparable.

```bash
make test      # full suite, no dependencies required
make example   # regenerate the worked example
make lint      # ruff (needs: pip install -e '.[dev]')
make check     # test, then verify the committed example is current
```

## License

Apache License 2.0. See [LICENSE](LICENSE). The core software and
documentation are and will remain freely available without licensing fees.

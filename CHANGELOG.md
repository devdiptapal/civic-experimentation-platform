# Changelog

All notable changes to this project are documented in this file.

This project uses semantic-ish versioning while in `0.x`: minor versions may
change interfaces. Changes that affect **comparability of published results**
are called out explicitly, because they matter more than API breakage for a
project whose purpose is reusable evidence.

## v0.4.0 — The hosted platform

### Added
- **Web application and API** (`civicexp serve`), standard library only:
  accounts holding the five reviewer roles, sign-offs bound to the exact plan
  version, an alert inbox, sealed results with recorded interim looks,
  decisions frozen into a readout and case record, and administration.
- **Collection service** for live services: enrolls each person once, keeps
  only declared equity attributes, refuses outcomes for unenrolled people or
  the wrong version, and serves the current version whenever the pilot is not
  approved to collect. JavaScript client that fails open.
- **Workspace** layout with the audit trail as the source of truth for state
  (`ExperimentLifecycle.from_audit`); a tampered trail halts collection.
- **Monitoring** (`civicexp monitor`, chat webhooks), **retention
  enforcement** (`civicexp retention`), **evidence library** with a versioned
  case-record schema, **spreadsheet mode** (`assign-batch`, `import-csv`),
  and a populated synthetic **demo** (`civicexp demo`).
- Three workflow templates: eligibility screener, renewal reminder,
  missing-information notice. Events `submission_returned` and
  `processing_completed`; standard metrics `returned_for_information_rate`
  and `time_to_process`. Config validation rejects unknown event names.
- Dockerfile; docs/Product-Guide.md.

### Fixed — affects comparability
- `abandonment_rate` counted people who viewed a step; it now counts people
  who entered and never submitted (`numerator_excludes`).
- `first_time_success_rate` now excludes anyone with a failed attempt.
- Guardrails whose metric is better when higher (e.g. first-time success)
  were breached by improvement; guardrails now honor `target_direction`.
- For lower-is-better primary metrics, the equity review read benefit as
  harm and the readout called a worse result "better". Both fixed.
- A group helped by a margin that survives correction is reported as
  helped, not "not reached", even when below the service-wide bar.
- Templates are now declared package data, so a regular install includes them.

The committed worked example is unchanged byte for byte.

## v0.3.0 — Trust checks, equity review, and a guided setup

Three themes: results you can trust, results that account for everyone, and
a setup path that does not require writing JSON by hand.

### Added — can these results be trusted?

- **Sample ratio mismatch detection** (`civicexp.diagnostics`). A
  chi-square goodness-of-fit test on the observed split. A mismatch means
  the assignment or logging pipeline is broken, so it returns a new
  `INVALID` decision and blocks any interpretation rather than appearing as
  a footnote. Threshold α = 0.001, because this check runs on every
  analysis and a tool that cries wolf gets switched off.
- **Novelty and primacy detection.** Compares the effect in the first half
  of the pilot window against the second, so an effect that is fading — a
  response to the change being new rather than better — is visible before
  the agency commits to a rollout.
- **Completeness checks.** Confirms the events each declared metric depends
  on actually arrived, in both arms.
- `civicexp doctor` runs these on their own, without producing a decision.

### Added — did it work for everyone?

- **Pre-registered equity segment analysis** (`civicexp.segments`). Groups
  are declared in the configuration before launch, and every comparison is
  corrected across the family with Holm-Bonferroni.
- Results distinguish `HARMED`, `WATCH`, `NO_EFFECT` and `TOO_FEW`, because
  an agency should act differently on each. Reporting "no effect" for a
  group of forty people is a false negative dressed as a finding.
- **A confirmed harm escalates the decision to `ROLLBACK` from any other
  outcome.** A change that improves the average while measurably hurting an
  identifiable group has not improved the service.
- Segment cells below the disclosure threshold are suppressed before
  analysis, not after.
- `civicexp equity` reports the review on its own.

### Added — usable without writing JSON

- **`civicexp init`**, a nine-question interview that writes a complete,
  valid configuration. It runs the power check live, so a pilot too small
  to answer its own question is caught during planning rather than
  afterwards. It refuses to invent the minimum effect of interest, which is
  a program judgment.
- **Workflow templates** for the three workflows in the roadmap: document
  upload, appointment reminders, and digital intake (`civicexp templates`).
- **A standard metric library** (`civicexp metrics`), so a configuration can
  write `{"use": "completion_rate"}` instead of restating the definition.
  Covers the roadmap's named outcomes: abandonment, time to complete, and
  successful first-time submission. Referencing a standard metric is what
  keeps one agency's result comparable with another's.
- **`civicexp case-summary`** produces the short public document another
  jurisdiction can act on, separate from the internal readout and carrying
  no approval record.
- **[QUICKSTART.md](QUICKSTART.md)** — fifteen minutes from clone to a
  finished report.
- **[docs/Integration-Guide.md](docs/Integration-Guide.md)** — wiring it
  into a live service, including the fail-open pattern, concurrency, and
  the assignment algorithm specified precisely enough to reimplement in
  another language.

### Changed

- The readout gained two sections: *Did this work for everyone?* and *Can
  these results be trusted?*. Sections renumbered to 1–10.
- `analyze()` now returns `diagnostics` and `equity` alongside the existing
  fields, and both appear in `to_dict()`.
- `AuditLog` and `ExperimentLifecycle` accept an injectable `clock`, which
  is what makes every committed example artifact byte-for-byte
  reproducible.
- The worked example gained **Scenario C**: the change works on average but
  reaches only English-preference applicants. Same configuration, same
  headline gain, a materially different report.
- Configurations without declared equity segments now load with a warning.

### Fixed

- `Assigner` accepted an empty salt at construction and only failed later,
  at the first assignment.
- Report sections were numbered independently of the order they rendered
  in. A test now asserts the numbering is sequential.

### Notes on comparability

`INVALID` is a new decision outcome, distinct from `INCONCLUSIVE`.
Inconclusive means the pilot was sound but too small; invalid means it was
broken. Consumers of `to_dict()` should handle the new value.

Results produced before this release did not run trust checks or an equity
review. They remain valid as far as they go, but a v0.3 readout answers
questions a v0.2 one did not, and the two are not interchangeable as
evidence.

## v0.2.0 — Working reference implementation

The project moves from a documentation scaffold to runnable software. All
five platform components described in the roadmap are now implemented,
tested, and demonstrated end to end on synthetic data.

### Added — the platform

- **Assignment** (`civicexp.assignment`): deterministic, stateless
  HMAC-SHA256 bucketing; declarative eligibility criteria; enrollment
  ramping that does not reshuffle already-enrolled units.
- **Outcome recording** (`civicexp.events`): append-only JSON Lines,
  validated at write time against the event taxonomy and privacy policy;
  unit-level rather than event-level counting.
- **Comparison** (`civicexp.stats`, `civicexp.analysis`): Wilson score
  intervals, Newcombe's hybrid-score interval for differences, two-proportion
  z-test, Welch's t-test, Holm-Bonferroni across the guardrail panel, Šidák
  correction for repeated looks, sample-size and minimum-detectable-effect
  calculations. Implemented on the standard library.
- **Reporting** (`civicexp.report`): standardized plain-language readout;
  recommendation first, no effect reported without its uncertainty,
  statistical jargon confined to an appendix.
- **Administrative controls** (`civicexp.lifecycle`): approve / pause /
  resume / complete / roll back / archive state machine, gated on five
  recorded reviewer sign-offs.
- **Privacy enforcement** (`civicexp.privacy`): HMAC-SHA256 pseudonymization,
  identifier-shaped field-name screening, free-text and nested-payload
  rejection, small-cell suppression.
- **Tamper-evident audit trail** (`civicexp.audit`): hash-chained entries
  with verification that names the first broken link.
- **Configuration validation** (`civicexp.config`): reports every problem at
  once, rejects placeholder values, and warns about underpowered designs.
- **Command-line interface**: `validate`, `power`, `preview`, `assign`,
  `simulate`, `analyze`, `report`, `verify`, `salt`.

### Added — evidence that it works

- 329 tests, runnable with no third-party packages.
- Statistical functions verified against published reference values, not
  against their own output.
- Property tests over simulated pilots with known ground truth, checking
  that the false-positive rate, interval coverage, and power match what the
  platform claims.
- A complete worked example with two scenarios that reach opposite
  decisions from identical primary-metric results.
- CI across Python 3.10–3.13 on Linux, macOS, and Windows, including a job
  that runs the suite *before* installing anything, and a job that fails if
  the committed example artifacts drift from what the code produces.

### Added — documentation

- `docs/Architecture.md`, `docs/Statistical-Methods.md`,
  `docs/Threat-Model.md`, `docs/Standards-Alignment.md`.
- Issue and pull request templates, `CODEOWNERS`, Dependabot configuration.

### Changed

- `examples/sample-config.yml` now validates against the schema. The
  previous version used placeholder strings in fields that are now required
  to be numeric.
- `README.md` rewritten around what the software does.

### Notes on comparability

This is the first release that produces results, so nothing is broken yet.
From here on, changes to metric definitions, the event taxonomy, or the
decision rule will be flagged in this section, because they determine
whether a readout published by one agency can be compared with another.

## v0.1.0 — Initial public scaffold

- Documentation, governance, and policy scaffold created
- Pilot and evaluation templates added
- Privacy, security, and contribution guidance added

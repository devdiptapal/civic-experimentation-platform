# Changelog

All notable changes to this project are documented in this file.

This project uses semantic-ish versioning while in `0.x`: minor versions may
change interfaces. Changes that affect **comparability of published results**
are called out explicitly, because they matter more than API breakage for a
project whose purpose is reusable evidence.

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

- 250 tests, runnable with no third-party packages.
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

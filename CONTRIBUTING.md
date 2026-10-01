# Contributing

Thank you for helping improve this public-interest project.

This project is working software with a documentation set around it.
Contributions to either are welcome, and clear writing for public-sector
readers is a first-class contribution rather than a lesser one.

## Ways to Contribute

- Improve plain-English documentation for public-sector readers
- Propose updates to templates and checklists
- Describe a workflow you would like to evaluate, so the platform can
  support it (there is an issue template for this)
- Question a statistical or evaluation-design choice (likewise)
- Fix a bug, add a test, or improve an error message
- Add implementation notes for reuse across jurisdictions
- Report issues or ambiguities in governance/privacy/security guidance

## Running the Code

The test suite needs no third-party packages:

```bash
make test        # 329 tests, standard library only
make example     # regenerate the worked example
make lint        # ruff (needs: pip install -e '.[dev]')
make check       # test, then verify the committed example is current
```

If you change anything the worked example touches, run `make example` and
commit the regenerated files. CI fails if they drift.

## Before You Start

1. Read `README.md` for scope and boundaries.
2. Read `GOVERNANCE.md` for decision-making norms.
3. Read `CODE_OF_CONDUCT.md` for participation expectations.

## Opening an Issue

Use issues to propose:
- Documentation improvements
- Template changes
- New examples
- Clarifications to privacy/security guidance

When opening an issue, include:
- The problem you are trying to solve
- Who is affected (for example: program staff, evaluators, legal reviewers)
- Suggested change in plain language

## Submitting a Pull Request

1. Fork the repository and create a branch.
2. Keep changes focused and explain why they are needed.
3. Link related issues.
4. Use clear commit messages and plain-English PR descriptions.
5. Be responsive to review comments.

PR checklist:
- [ ] Change is scoped and clearly described
- [ ] Wording is plain English and avoids overclaiming
- [ ] No sensitive data or proprietary content included
- [ ] Related docs are updated when needed
- [ ] `make test` passes, and new behaviour has a test
- [ ] `make example` re-run if the worked example is affected

## Changes to Measurement, Privacy, or Governance

These get more review than their diff size suggests, because they affect
whether a readout published by one agency can be compared with another, or
whether the privacy posture still holds.

That includes:

- metric definitions and the event taxonomy (`docs/Metrics-Schema.md`)
- the statistical methods (`src/civicexp/stats.py`)
- the decision rule (`src/civicexp/analysis.py`)
- the privacy controls (`src/civicexp/privacy.py`)
- the approval gate (`src/civicexp/lifecycle.py`)

For these, please open an issue first describing the problem and the
trade-offs. If the change alters published results, say so explicitly: it
needs a note in `CHANGELOG.md` under "Notes on comparability".

## Two Rules That Are Not Negotiable

**No real data, ever.** No real applicant data, identifiers, agency
credentials, or production endpoints in code, tests, documentation, issues,
or pull requests. Every example uses synthetic data from
`civicexp.simulate`. CI scans for identifier-shaped strings on every push,
but the scan is a backstop, not a substitute for care.

**No new runtime dependencies.** The package imports only the standard
library, because adding a package to a reviewed agency environment can
require its own security review, and every dependency is supply-chain risk
carried by every agency that deploys this. If you believe a dependency is
genuinely necessary, open an issue making the case before writing the code.

## Documentation-First Contributions

At this stage, documentation improvements are first-class contributions.

Examples:
- Improving step clarity in the pilot guide
- Tightening definitions in the glossary
- Adding safer defaults to templates
- Clarifying measurement caveats

## Review and Merge Expectations

- Maintainers review for clarity, safety, and consistency with project scope.
- Significant governance/privacy/security changes may require longer review.
- Merges require at least one maintainer approval.

## Licensing

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License in this repository.

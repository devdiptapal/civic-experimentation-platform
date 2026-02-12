# Privacy

This project is designed for privacy-forward experimentation in public digital services.

It provides guidance and templates only. Each agency remains responsible for legal and policy compliance in its jurisdiction.

## Core Principles

1. Data minimization
Collect only what is needed to answer a defined experiment question.

2. No PII by default
Default templates should avoid personally identifiable information (PII). If PII is required for operations, document why and restrict access.

3. Purpose limitation
Use experiment data only for the approved pilot purpose.

4. Aggregation first
Prefer reporting in aggregate form. Avoid publishing small-cell or re-identifiable breakdowns.

5. Time-bounded retention
Define retention windows before launch and delete or archive data according to policy.

## Data Handling Guidance

- Use pseudonymous IDs where possible.
- Separate operational identifiers from analysis datasets.
- Apply least-privilege access controls.
- Log access to sensitive data stores.
- Avoid exporting row-level data unless explicitly approved.

## Retention Guidance (Template)

Each pilot should document:
- Data elements collected
- Legal or policy basis for collection
- Retention period (for example, `__` days)
- Deletion method and owner
- Exception process for legal holds

## Privacy Review Checklist

Before launch, confirm:

- [ ] Experiment objective is clearly documented.
- [ ] Required data fields are listed and justified.
- [ ] PII is excluded by default or explicitly justified.
- [ ] Access controls and roles are documented.
- [ ] Retention and deletion plan is approved.
- [ ] Aggregation thresholds for reporting are defined.
- [ ] Risk of re-identification is reviewed.
- [ ] Privacy contact/owner is named.

## Incident and Exception Handling

- Report potential privacy incidents immediately through the security process in `SECURITY.md`.
- Pause data collection for the affected pilot when required by agency policy.
- Document incident timeline, scope, and remediation actions.

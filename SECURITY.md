# Security Policy

## Scope

This repository currently provides documentation and templates. Security expectations still apply to:
- Repository access controls
- Dependency updates for any future tooling
- Safe handling of pilot documentation and configuration artifacts

## Reporting a Vulnerability

Please do **not** open public issues for suspected vulnerabilities.

Instead:
- Use GitHub private vulnerability reporting (Security Advisories), if enabled.
- Or contact maintainers through a private channel listed in the repository settings.

When reporting, include:
- Description of the issue
- Steps to reproduce
- Potential impact
- Suggested remediation, if known

Maintainers will:
- Acknowledge receipt within 5 business days (target)
- Triage severity and exposure
- Coordinate remediation and disclosure timing

## Dependency Hygiene

For any code added to this repository in the future:
- Pin or constrain dependency versions where practical.
- Run dependency update checks on a regular schedule.
- Remove unused packages promptly.
- Prefer well-maintained, widely reviewed libraries.

## Safe Defaults

- Do not commit credentials, secrets, tokens, or production endpoints.
- Do not include real user data in examples, tests, or docs.
- Use placeholder values in all sample configs.
- Restrict write permissions to trusted maintainers.
- Require pull request review for main branch changes.

## Security Maintenance Notes

As this project evolves beyond documentation-only content, maintainers should add:
- Threat modeling notes
- Runtime hardening guidance
- Logging and monitoring recommendations
- Incident response runbooks

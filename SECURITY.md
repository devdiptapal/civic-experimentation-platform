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

## Threat Model

A full threat model is maintained at [`docs/Threat-Model.md`](docs/Threat-Model.md).
It states what the platform defends against, what it explicitly does not,
and the operational controls an agency must add before production use.

Two items from it are worth repeating here because they are the easiest to
get wrong:

1. **The assignment salt in an experiment config is a secret.** It keys the
   HMAC that pseudonymizes applicant identifiers. Identifier spaces in
   benefits systems are small and guessable, so publishing a real pilot's
   config would make its pseudonyms reversible by brute force. Do not commit
   a real config to a public repository.
2. **The audit chain is tamper-evident, not tamper-proof.** Someone with
   write access to the file can recompute the whole chain. Publish the head
   digest (`civicexp verify`) somewhere the operator does not control at
   pilot close.

## Scope of the Current Implementation

The platform has **no runtime dependencies**, makes no network calls, opens
no ports, and has no server component. It runs inside an agency's own
environment. This removes a large share of the usual attack surface, and
shifts the remaining responsibility to the integrating system: see the
operational requirements in the threat model.

## Security Maintenance Notes

Still to be added as the project matures:
- Runtime hardening guidance for a hosted deployment
- Logging and monitoring recommendations
- Incident response runbooks

# Threat Model

What this platform defends against, what it does not, and what an agency
must add before operational use.

`SECURITY.md` notes that threat modeling should be added as the project
moves beyond documentation. This is that document.

## What is being protected

1. **Applicant privacy.** The platform touches identifiers for people
   applying for benefits. These are among the most sensitive identifiers a
   government system holds, and the people behind them did not opt in.
2. **Evidence integrity.** Results inform decisions affecting access to
   benefits. A result that has been altered — or that can be altered
   without trace — is worse than no result, because it carries unearned
   authority.
3. **Service availability.** The platform sits alongside a live service.
   It must not become a way to break one.

## Trust boundaries

```text
   Agency service                Platform                  Reviewers
  ┌──────────────┐          ┌─────────────────┐        ┌─────────────┐
  │  identifiers │─────────▶│ pseudonymize    │        │             │
  │  attributes  │  in-proc │ assign (pure fn)│        │  readouts   │
  │              │◀─────────│ variant name    │        │  audit log  │
  └──────────────┘          │                 │───────▶│             │
                            │  event log ─────┼──┐     └─────────────┘
                            │  audit log ─────┼──┤        aggregated
                            └─────────────────┘  │        only
                                                 ▼
                                          filesystem
                                       (agency-controlled)
```

The platform runs inside the agency's own environment. It makes no network
calls, opens no ports, and has no server component. The boundary that
matters most is the one where raw identifiers enter and only pseudonyms
leave.

## Threats addressed

### T1 — Raw identifiers written to the event log

**Risk:** a case number or SSN lands in a pilot dataset, creating a new
disclosure surface and a reportable incident.

**Mitigation:** `EventLog.validate` rejects any `unit_pseudonym` that is not
a 64-character hex digest, so a raw identifier cannot be recorded even by a
caller that passes one. Payload field names are screened against a
denylist. Validation also runs when an existing log is *read*, so a file
edited by hand is caught.

**Residual risk:** an identifier embedded in a value rather than a field
name — for example `{"step": "case-88213"}`. Long strings are rejected, but
a short one that happens to be an identifier would pass. Field screening is
a safety net, not a classifier.

### T2 — An experiment running without approval

**Risk:** a change is tested on live applicants before privacy or legal
review.

**Mitigation:** `ExperimentLifecycle.require_collecting()` raises unless the
experiment is in `RUNNING`, reachable only from `APPROVED`, which requires
all five reviewer sign-offs individually recorded. A single enforcement
point means no call site can forget to check.

**Residual risk:** the platform cannot compel a caller to invoke the guard.
An integration that bypasses the lifecycle entirely defeats it. Treat that
as a code-review item in the integrating system.

### T3 — Results altered after the fact

**Risk:** an inconvenient result is quietly amended, or a sign-off is
back-dated.

**Mitigation:** the audit log is hash-chained. Altering, removing, or
reordering an entry breaks the chain and `civicexp verify` names the first
broken link.

**Residual risk:** this is tamper-*evidence*, not tamper-*proofing*. Someone
with write access can recompute the entire chain. **Mitigation for that:**
publish the head digest somewhere the operator does not control — a git
commit, a ticket, or the agency's records system — at pilot close. Without
that external anchor the chain only detects careless tampering.

### T4 — Re-identification from published aggregates

**Risk:** a segment breakdown with three people in it identifies them.

**Mitigation:** small-cell suppression with a default threshold of 11,
applied to published aggregates. The analysis warns when any arm falls
below the threshold and states that the result should not drive a decision.

**Residual risk:** differencing attacks across multiple published reports
are not addressed. An agency publishing many overlapping breakdowns needs
disclosure review beyond a per-cell threshold.

### T5 — Pseudonym reversal by guessing identifiers

**Risk:** identifier spaces in benefits systems are small and highly
guessable — sequential case numbers, for instance. An attacker holding
published pseudonyms could confirm a guess by recomputing the digest.

**Mitigation:** HMAC-SHA256 keyed by the per-experiment salt, not a bare
hash. Without the salt, guessing is not verifiable.

**Residual risk:** the salt is stored in the config file. **An agency must
treat the config's salt as a secret**: restrict read access, and do not
commit a real pilot's config to a public repository. This is the platform's
sharpest operational footgun, and it is called out again in the deployment
guide.

### T6 — Assignment manipulated to produce a desired result

**Risk:** someone reruns assignment with a different salt until the split
favours the conclusion they want.

**Mitigation:** assignment is deterministic and recomputable. An auditor
with the config and the identifier list can reproduce every assignment
exactly. Changing the salt changes *all* assignments visibly, and the salt
is recorded in the approved config.

**Residual risk:** none technically, but it depends on someone actually
performing the check. The recomputation is the control; the platform only
makes it possible.

### T7 — Untrusted input in eligibility rules

**Risk:** a rule language that evaluates expressions becomes code execution
via a config file.

**Mitigation:** the rule language has ten enumerated operators, no
expression evaluation, no `eval`, and no imports. Rules are pure data.

### T8 — Supply chain compromise

**Risk:** a malicious dependency reaches an agency environment through this
package.

**Mitigation:** zero runtime dependencies. CI runs the full suite before
installing anything, so the property cannot silently lapse. PyYAML is
optional and needed only for YAML configs; JSON works without it.

## Threats NOT addressed

An agency must handle these itself before operational use.

| Threat | Why it is out of scope | What the agency must do |
| --- | --- | --- |
| Authentication and authorization | The platform has no user model. `actor` strings in the audit log are self-asserted. | Run it behind the agency's own authentication; map real identities to actors. |
| Encryption at rest | Event and audit logs are plain files. | Use encrypted storage and filesystem access controls. |
| Secure salt storage | The salt sits in the config file. | Manage configs as secrets; restrict read access; never publish a real pilot's config. |
| Availability and rate limiting | No server component. | Whatever hosts the integration is responsible. |
| Malicious insider with write access | Detectable but not preventable. | External anchoring of the audit head; separation of duties. |
| The service being tested | Entirely outside this platform. | The agency's own security review. |
| Denial of service through log growth | Logs grow unbounded. | Enforce the configured retention; monitor disk. |

## Operational requirements before production use

1. Run inside the agency's authenticated environment; do not expose it.
2. Store configs as secrets because of the salt.
3. Encrypt event and audit logs at rest and restrict access by role.
4. Anchor the audit head digest externally at pilot close.
5. Enforce the configured retention period with an actual scheduled job —
   the platform records the policy, it does not delete anything.
6. Complete a privacy impact assessment under the agency's own process.
7. Re-verify that no eligibility field or event payload carries an
   identifier the denylist does not know about.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md). Please do not open a public issue.

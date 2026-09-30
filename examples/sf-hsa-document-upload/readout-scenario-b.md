# Evaluation readout: Document upload: plain-language requirements list

**Experiment ID:** `exp-doc-upload-001`  
**Service area:** benefits-enrollment / document upload  
**Owning team:** agency-digital-services  
**Generated:** 2026-03-30 17:00 UTC

## 1. Recommendation

### Stop and revert the tested version

At least one harm measure moved outside the tolerance the program set. The pre-agreed rule is to revert the tested version and review before any further testing.

- 1 guardrail(s) breached tolerance: error_rate. A confirmed harm signal forces a rollback regardless of the primary metric.

## 2. What was tested

Applicants must upload proof-of-identity and proof-of-income documents to finish a benefits application. Support staff report that applicants frequently upload the wrong document type, are asked to resubmit, and some abandon the application at that point. The tested version replaces a dense paragraph of requirements with a short checklist naming each accepted document and showing an example of each. No eligibility rule, required document, or policy changes; only the on-screen explanation of what to upload.

People and transactions meeting the approved criteria were split between **control** (the current service) and **plain_language_checklist** (the tested change). Assignment was made by a fixed rule tied to each unit's identifier, so the same person always saw the same version and the split can be re-checked afterwards.

**Who was included:**

- Included when channel is 'web'
- Included when application_type is 'new'
- Excluded when staff_assisted is True
- Excluded when manual_review_flag is True

## 3. The main result

**Measure:** completion_rate — Share of applicants who reach the document upload step and successfully submit their documents, counted once per applicant.

| Version | Units measured | Rate | Plausible range |
| --- | ---: | ---: | --- |
| control (current) | 6,101 | 62.3% | 61.0% to 63.5% |
| plain_language_checklist (tested) | 5,899 | 69.6% | 68.4% to 70.8% |

**Difference:** +7.3 percentage points

We are 95% confident the tested version is better, by somewhere between 5.2 and 9.5 percentage points.

Before launch, the program recorded that a change of at least **3.0 percentage points** would be worth acting on. That bar is what the recommendation above is measured against, not simply whether a difference exists.

## 4. Checks for unintended harm

These measures were agreed in advance as things that must not get materially worse, whatever happens to the main measure.

| Harm measure | Status | What happened |
| --- | --- | --- |
| error_rate | **BREACHED** | Worsened by 4.5 percentage points (95% CI 3.3 to 5.7), and the whole interval exceeds the 1.0 point tolerance. |
| support_contact_rate | **OK** | Change of -0.0 percentage points (95% CI -1.1 to 1.1), within the 2.0 point tolerance. |
| time_to_complete | **OK** | Change of -18.4 seconds (95% CI -31.6 to -5.1) against a tolerance of 60.0 seconds. |

## 5. Limits of this evidence

- Significance threshold tightened from 0.050 to 0.0127 to account for 4 planned looks at the data.
- This evaluation shows what happened for the people and transactions included during the pilot window. It does not establish that the same effect would appear for excluded groups, in another jurisdiction, or at a different time of year.
- The pilot ran for a planned 28 days. Effects that appear only after longer exposure, such as changes in repeat contact, are outside its reach.
- Adequately powered: with 6,000 units per group this pilot can detect a change of about 3.0 percentage points, and the program cares about 3.0.

## 6. Suggested next steps

- Revert the tested version now, following the rollback process agreed before launch.
- Notify support staff that the change is being withdrawn and why.
- Review what the harm measure is telling you before designing any replacement test.

## 7. Technical appendix

Rates are compared with a two-sided two-proportion z-test. Confidence intervals for each rate use the Wilson score method, and the interval on the difference uses Newcombe's hybrid score method, both of which hold their coverage at the small denominators common in county-scale pilots where the usual normal approximation does not.

- Significance threshold applied: α = 0.0127
- Corrected from α = 0.050 for 4 planned looks at the data (Šidák)
- p-value on the primary measure: 0.0000
- Counts: control 3,799/6,101, tested 4,106/5,899
- Guardrails were tested as a family of 3 with a Holm-Bonferroni correction, so a single guardrail moving by chance does not halt the pilot.
- Aggregates covering fewer than 11 units are suppressed under the configured disclosure rule.

## 8. Decision and approval record

| # | Timestamp (UTC) | Action | Actor | Detail |
| --- | --- | --- | --- | --- |
| 0 | 2026-03-02T09:05:00+00:00 | approval:sign_off | a.rivera | experiment_id=exp-doc-upload-001, note=Scope is limited to on-screen text., role=program_owner |
| 1 | 2026-03-02T09:10:00+00:00 | approval:sign_off | m.chen | experiment_id=exp-doc-upload-001, note=Support team briefed., role=operations_lead |
| 2 | 2026-03-02T09:15:00+00:00 | approval:sign_off | s.okafor | experiment_id=exp-doc-upload-001, note=No PII collected; 90-day retention., role=privacy_reviewer |
| 3 | 2026-03-02T09:20:00+00:00 | approval:sign_off | d.whitfield | experiment_id=exp-doc-upload-001, note=No policy change., role=legal_policy_reviewer |
| 4 | 2026-03-02T09:25:00+00:00 | approval:sign_off | devdipta.pal | experiment_id=exp-doc-upload-001, note=Powered for a 3pt change., role=evaluation_lead |
| 5 | 2026-03-02T09:30:00+00:00 | state:draft->approved | a.rivera | approvals=evaluation_lead,legal_policy_reviewer,operations_lead,privacy_reviewer,program_owner, experiment_id=exp-doc-upload-001 |
| 6 | 2026-03-02T09:35:00+00:00 | state:approved->running | m.chen | experiment_id=exp-doc-upload-001 |
| 7 | 2026-03-02T09:40:00+00:00 | state:running->paused | m.chen | experiment_id=exp-doc-upload-001, reason=Unrelated upload-service outage; data would be biased. |
| 8 | 2026-03-02T09:45:00+00:00 | state:paused->running | m.chen | experiment_id=exp-doc-upload-001 |
| 9 | 2026-03-02T09:50:00+00:00 | state:running->rolled_back | m.chen | experiment_id=exp-doc-upload-001, reason=error_rate breached its 1.0 point tolerance under scenario B. |
| 10 | 2026-03-02T09:55:00+00:00 | state:rolled_back->archived | a.rivera | audit_head=7bb2c0d01e045b0351a01f118ac2b8336668767f10d77b595f9b9f41579c8076, experiment_id=exp-doc-upload-001 |

Chain verification: **intact** (11 entries checked). Head digest: `9b7cedbf68c6f518…`

---

_Generated by the Civic Experimentation Platform. This readout describes measured outcomes for a specific service change; it is not a legal, policy, or eligibility determination._

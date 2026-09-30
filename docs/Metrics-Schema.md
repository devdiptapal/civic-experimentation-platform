# Metrics Schema

## Purpose

This document defines a minimal, plain-language schema for experiment measurement artifacts in this repository.

It is a placeholder for future, versioned metric standards.

## Minimal event taxonomy (placeholder)

- `experiment_assigned`
- `step_viewed`
- `step_completed`
- `submission_attempted`
- `submission_succeeded`
- `submission_failed`
- `support_contacted`

For each event, future versions should define:
- Required fields
- Optional fields
- Allowed values
- Data quality rules

## Metric definitions

### Primary outcome metrics

- `completion_rate`: percent of started sessions that end in successful submission.
- `drop_off_rate`: percent of started sessions that do not reach submission.

### Guardrail metrics

- `error_rate`: percent of sessions with a blocking error.
- `support_contact_rate`: percent of sessions that generate support contact.
- `time_to_complete`: median time from first relevant step to successful submission.

### Notes

- Use aggregated reporting by default.
- Avoid PII in metric pipelines unless explicitly approved.
- Define metric windows and segment rules before pilot launch.

---

## Status: implemented

This schema is no longer a placeholder. The event taxonomy above is
enforced in code by [`civicexp.events`](../src/civicexp/events.py): an event
whose type is not in the list is rejected at write time, with an error
saying that extending the taxonomy is a governance change rather than a
code change.

### Required fields on every event

| Field | Type | Rule |
| --- | --- | --- |
| `experiment_id` | string | Must match the log's experiment |
| `event_type` | string | Must be one of the taxonomy above |
| `unit_pseudonym` | string | 64-character hex digest. A raw identifier is **rejected** |
| `variant` | string | Must be one of the experiment's declared variants |
| `timestamp` | string | ISO-8601 **with an explicit timezone offset**. Naive timestamps are rejected, because a pilot spanning a daylight-saving change would otherwise produce durations silently wrong by an hour |
| `payload` | object | Flat; see the rules below |

### Payload rules

Enforced by [`civicexp.privacy`](../src/civicexp/privacy.py):

- **Flat only.** Nested objects and arrays are rejected, so that every
  collected field is visible to privacy review.
- **No identifier-shaped field names**, unless explicitly allowlisted in
  `data_governance.approved_pii_fields` with a documented justification.
- **No long free text.** Strings over 120 characters are rejected: free text
  cannot be screened for identifiers by field name. Record a coded category
  instead.
- **No platform field names.** A payload may not redefine `event_type`,
  `unit_pseudonym`, `variant`, `timestamp`, or `experiment_id`.

### How metrics are computed

**Rates count distinct units, never raw events.** A person who retries a
failing upload six times is one person having a bad experience, not six data
points. Counting events would inflate exactly the measures an agency most
wants to watch — error rate and support-contact rate would look worst in
whichever arm frustrates people into retrying.

```
completion_rate = distinct units with `submission_succeeded`
                  ÷ distinct units with `experiment_assigned`
```

**Durations use each unit's earliest start and its earliest subsequent end
event.** Units that never reach the end event contribute nothing: an
incomplete application has no completion time, and imputing one would bias
the metric toward whichever arm abandons more.

### Changing this schema

Adding or redefining an event type changes whether results published before
the change can be compared with results after it. Treat it as a significant
decision under [GOVERNANCE.md](../GOVERNANCE.md): open an issue describing
the context and trade-offs, allow time for comment, and record the outcome
in [CHANGELOG.md](../CHANGELOG.md) under "Notes on comparability".

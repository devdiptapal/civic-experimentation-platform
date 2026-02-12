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

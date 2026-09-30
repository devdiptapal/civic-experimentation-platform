## What this changes

<!-- Plain English, for a reader who has not followed the discussion. -->

## Why

<!-- The problem this solves. Link the issue if there is one. -->

## Checklist

- [ ] Change is scoped and clearly described
- [ ] Wording is plain English and avoids overclaiming
- [ ] No real user data, credentials, or agency-specific endpoints included
- [ ] Tests added or updated, and `make test` passes
- [ ] Related documentation updated

## If this touches measurement, privacy, or governance

Changes in these areas affect whether results from different agencies can
be compared and whether the privacy posture still holds. Please also confirm:

- [ ] Metric definitions remain comparable with previously published results,
      or the change is called out in `CHANGELOG.md` as breaking comparability
- [ ] Privacy defaults are unchanged, or the change is justified here
- [ ] The decision rule still treats confirmed harm as overriding benefit
- [ ] `make example` was re-run and the regenerated artifacts are committed

# Integration Guide

How to connect this platform to a live service. Written for the engineer who
has to add it to an existing application.

The short version: **two calls.** One asks which version a person should
see, one records what happened. Everything else — the statistics, the
privacy screening, the decision rule — follows from those.

---

## The whole integration

```python
from civicexp import Assigner, EventLog, ExperimentLifecycle, load_config

config = load_config("experiment.json")

assigner = Assigner(
    salt=config.salt,
    variants=config.variants,
    eligibility=config.eligibility,
    enrolled_fraction=config.enrolled_fraction,
)
events = EventLog(
    config.experiment_id,
    path="/var/lib/civicexp/events.jsonl",
    privacy=config.privacy,
    allowed_variants=config.variants,
)


def upload_page(request):
    # 1. Which version does this person see?
    assignment = assigner.assign(
        request.applicant_id,
        {
            "channel": "web",
            "application_type": request.application_type,
            "staff_assisted": request.is_staff_assisted,
            "manual_review_flag": request.needs_manual_review,
        },
    )

    if not assignment.in_analysis:
        return render_current_page()      # ineligible or held back

    # 2. Record that they entered, with the attributes the equity review needs.
    events.record(
        "experiment_assigned",
        assignment.unit_pseudonym,
        assignment.variant,
        payload={
            "preferred_language": request.language_code,
            "device_type": request.device_type,
        },
    )

    if assignment.variant == "plain_language_checklist":
        return render_checklist_page()
    return render_current_page()


def on_upload_succeeded(request, assignment):
    events.record(
        "submission_succeeded", assignment.unit_pseudonym, assignment.variant
    )
```

That is the integration. The rest of this document is the detail that makes
it safe in production.

---

## Things that will bite you

### Pass the raw identifier, not a hash you made earlier

`assign()` takes the real identifier and pseudonymizes it internally with
the experiment's salt. If you hash it yourself first, assignment still
works, but your pseudonyms will not match the ones the platform computes
elsewhere, and an auditor will not be able to recompute your assignments.

The raw identifier never leaves the function: only
`assignment.unit_pseudonym` is ever written.

### Record the entry event exactly once per person

The denominator of every rate is "distinct people who entered". Recording
the entry event on every page load does not inflate the denominator — the
platform counts distinct units, not events — but it does bloat the log. Call
it once, when the person first reaches the step.

### Record segment attributes on the entry event

Equity analysis reads them from the payload of the metric's denominator
event, which is normally `experiment_assigned`. If you do not record them
there, the equity review silently has nothing to work with and reports no
groups. `civicexp validate` lists the segments your config declares; make
sure your integration records each one.

Use coded values (`"es"`, `"mobile"`), not free text. The privacy screen
rejects long strings precisely because they cannot be checked for
identifiers.

### Let the lifecycle gate collection

```python
lifecycle = ExperimentLifecycle(config.experiment_id, audit_path="audit.jsonl")

def upload_page(request):
    if not lifecycle.is_collecting:
        return render_current_page()
    ...
```

This is what makes "pause" mean something operationally. Without this check,
pausing changes a state field and nothing else.

### Fail open, always

If anything in the experimentation path raises, the person must still get a
working service. An evaluation is never worth a failed benefits application.

```python
def pick_variant(request):
    try:
        assignment = assigner.assign(request.applicant_id, attributes(request))
        if assignment.in_analysis:
            return assignment
    except Exception:
        logger.exception("experiment assignment failed; serving current version")
    return None    # caller renders the current page
```

Apply the same pattern to `events.record`. A dropped event costs you a
little statistical power. A 500 on the upload page costs someone their
application.

### Do not change the salt mid-pilot

Changing it reassigns everyone. The platform cannot detect that this
happened, because each assignment is individually correct — the population
simply gets reshuffled. Set the salt before launch, keep it in the config,
and leave it.

---

## Concurrency

`Assigner` is stateless and safe to share across threads and processes:
assignment is a pure function of the salt and the identifier. Two servers
that have never communicated will agree on every assignment.

`EventLog` appends to one file. Within a process it is not synchronized, so
guard it with a lock if you write from multiple threads. Across processes,
appends of lines shorter than `PIPE_BUF` are atomic on Linux in practice,
but if you are running several workers the cleaner answer is one log file
per worker, concatenated before analysis:

```bash
cat /var/lib/civicexp/events-*.jsonl > merged.jsonl
civicexp analyze experiment.json merged.jsonl
```

Deterministic assignment is what makes this safe: the arms are correct
however the events were split across files.

---

## Not writing Python?

The assignment rule is deliberately simple enough to reimplement, and
reimplementing it is a supported way to use this project. The rule is:

```
bucket = first 8 bytes of HMAC-SHA256(key = salt, message = "assign:" + unit_id)
         interpreted as a big-endian unsigned integer, divided by 2^64
```

Then walk the variants **in alphabetical order by name**, accumulating their
traffic shares, and take the first whose cumulative share exceeds the
bucket. Alphabetical order matters: it is what makes assignment independent
of the order keys happen to appear in the config file.

Pseudonyms are `HMAC-SHA256(key = salt, message = unit_id)`, hex-encoded.
Enrollment, when `enrolled_fraction < 1`, uses the same bucketing with the
prefix `"enroll:"` instead of `"assign:"`.

Verify any reimplementation against the Python one before trusting it:

```bash
civicexp assign experiment.json applicant-12345 --attributes '{...}' --json
```

Your service can then do assignment natively and write JSON Lines events in
the documented shape, using this package only for analysis and reporting.

---

## Analysis, scheduled

Analysis is offline and reads the event log. Nothing needs to run inside
your service.

```bash
# During the pilot, on the monitoring cadence in the config:
civicexp doctor  experiment.json events.jsonl    # is the data trustworthy?
civicexp analyze experiment.json events.jsonl    # exit code 2 if a guardrail breached

# At the end:
civicexp report       experiment.json events.jsonl --audit audit.jsonl --out readout.md
civicexp equity       experiment.json events.jsonl
civicexp case-summary experiment.json events.jsonl --jurisdiction "Example County"
```

The exit codes are built for this: `0` fine, `2` needs a human, `1` the tool
failed. A monitoring job can page on `2` without parsing any output.

Run `doctor` **before** `analyze` in any automated monitoring. A sample
ratio mismatch means the numbers are not measuring what they claim to, and
you want to know that before an alert fires on a guardrail that moved
because the pipeline is broken.

---

## Before production

From the [threat model](Threat-Model.md), the items that are the
integrator's responsibility rather than this platform's:

- Run inside your authenticated environment; this package has no user model
  and the `actor` strings in the audit log are self-asserted.
- Store the config as a secret, because of the salt.
- Encrypt the event and audit logs at rest; restrict access by role.
- Enforce the configured retention with an actual scheduled job. The
  platform records the policy; it deletes nothing.
- Publish the audit head digest (`civicexp verify`) somewhere you do not
  control, at pilot close.
- Complete your own privacy impact assessment.

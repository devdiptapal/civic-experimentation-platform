# Product Guide

The hosted platform: a web application and API, built on the Python
standard library. No third-party packages are installed.

## Try it (one minute)

```bash
pip install -e .
civicexp demo demo-workspace          # synthetic team, six evaluations
civicexp serve --workspace demo-workspace
```

Open http://127.0.0.1:8080 and sign in with a username and the password
`civicexp demo` printed. Sign in as different people to see each role.

## Set up for real

```bash
civicexp workspace init county-ws --name "County Human Services" --jurisdiction "Example County"
civicexp users add it.admin --roles admin --workspace county-ws
civicexp users add a.rivera --roles program_owner --workspace county-ws
# ...one person each for operations_lead, privacy_reviewer,
#    legal_policy_reviewer, evaluation_lead
civicexp serve --workspace county-ws
```

Docker: `docker build -t civicexp . && docker run -p 8080:8080 -v ws:/data civicexp`,
then create accounts with `docker exec <container> python -m civicexp users add ...`.
Run it behind your agency's TLS proxy; the container serves plain HTTP.

## How a pilot goes

1. **Plan** – pick a workflow template and answer the questions only your
   team can. The form checks live whether the pilot is large enough. A dry
   run on simulated data previews the readout.
2. **Review** – five people, one per role, sign off on that exact version
   of the plan. Editing the plan afterwards voids earlier sign-offs. The
   program owner then approves, and the plan is locked.
3. **Run** – the operations lead starts it. Anyone with a role can pause.
   Harm measures, data checks, and harm to declared groups are monitored
   continuously. The main result is **sealed**: only the evaluation lead can
   take a recorded look, counted against the looks the plan declared.
4. **Decide** – the program owner ends the pilot; the pre-agreed rule
   decides. The readout and case record are frozen at that moment.
5. **Share** – publish the case to the evidence library, whatever the
   outcome. Other agencies can import it and plan a replication from it.

## Connecting a service

Create a key (Administration page, or `civicexp keys create --name svc --experiment ID`),
then call:

```
POST /api/v1/experiments/{id}/assign   {"unit_id": "...", "attributes": {...}}
  -> {"serve": "control" | "<variant>", "in_analysis": ..., "collecting": ...}
POST /api/v1/experiments/{id}/events   {"unit_id": "...", "event_type": "submission_succeeded"}
POST /api/v1/experiments/{id}/events/batch   {"events": [...]}   (up to 1,000)
GET  /api/v1/experiments[/{id}]  and  /api/v1/alerts   (read scope)
```

Always render `serve`. Identifiers are pseudonymized on arrival and never
stored; only the declared equity attributes are kept. Outcomes for people
never enrolled, or reported against the wrong version, are refused. Errors are
JSON with a stable `code`. For web pages, `/static/civicexp-client.js` falls
back to the current version on any failure.

No integration? `civicexp assign-batch` turns a spreadsheet of people into a
send list for a mail or text vendor; `civicexp import-csv` records the
outcomes export (`--profile` describes an unfamiliar file without showing
identifiers).

## Operating it

- `civicexp monitor --webhook https://...` on a schedule: exit code 2 means a
  person is needed (harm breach, failed trust check, stalled data, plan
  changed after approval, governance record altered).
- `civicexp retention --apply` on a schedule deletes event data past its
  approved retention period, recording what was deleted. Decisions and
  readouts are kept.
- Back up the workspace folder. It holds password hashes and pilot data:
  keep it on encrypted storage and never commit it.

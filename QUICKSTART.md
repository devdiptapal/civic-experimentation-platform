# Quickstart

Fifteen minutes, start to finish, with no real data and nothing installed.

By the end you will have planned an evaluation, dry-run it on synthetic
data, and read the report your team would actually circulate. If the
platform is not right for your workflow, you will know that in fifteen
minutes too.

**You need:** Python 3.10 or newer. Nothing else — no packages, no account,
no network.

```bash
git clone https://github.com/devdiptapal/civic-experimentation-platform.git
cd civic-experimentation-platform
pip install -e .
```

(You can skip the install and run `python -m civicexp` from the `src`
directory instead, if adding packages needs approval where you work.)

---

## 1. See a finished evaluation first (2 minutes)

Before planning your own, watch a complete one run:

```bash
python examples/sf-hsa-document-upload/run_example.py
```

This runs three scenarios of the same pilot on synthetic data. Look at the
last few lines. The same measured improvement in completion produces three
different recommendations, because of what else happened:

| | Completion | What else | Recommendation |
| --- | --- | --- | --- |
| A | +7 points | nothing | **Adopt it** |
| B | +7 points | blocking errors rose | **Revert it** |
| C | +7 points | Spanish speakers saw no benefit | **Adopt, and fix the translation** |

Open [`readout-scenario-c.md`](examples/sf-hsa-document-upload/) to see what
a finished report looks like.

---

## 2. Plan your own evaluation (5 minutes)

```bash
civicexp init
```

Nine questions in plain language. It asks what you are changing, how many
people use the workflow, and what improvement would be worth acting on, then
writes a complete configuration.

Two of the questions matter more than the rest:

**"How big an improvement would be worth making this change permanent?"**
This is a program judgment, not a technical one, and the tool will not
answer it for you. Everything is reported against this number: a change
smaller than it will not be recommended for adoption even if it is real.

**"Roughly how many people go through this workflow each month?"** The
wizard uses this to tell you, immediately, whether your pilot can detect the
improvement you just said you cared about. If it cannot, it says so and
tells you how long you would need to run instead. Finding this out now costs
you a minute; finding out after the pilot costs you the pilot.

Prefer to edit a file directly? `civicexp templates` lists the ready-made
workflows and `civicexp init --template document-upload` copies one.

---

## 3. Check the plan (1 minute)

```bash
civicexp validate experiment.json
```

This refuses placeholder values, confirms the arms add up, checks the
privacy rules, and repeats the power verdict. Resolve every warning before
going further — warnings here are the problems that are cheap now and
expensive later.

---

## 4. Dry-run it before touching anyone real (5 minutes)

Confirm the split works, writing nothing:

```bash
civicexp preview experiment.json --attributes '{"channel":"web","application_type":"new"}'
```

Then generate a pilot's worth of synthetic outcomes and read the report it
would produce:

```bash
civicexp simulate experiment.json --out dry-run.jsonl \
  --attributes '{"channel":"web","application_type":"new"}'

civicexp report experiment.json dry-run.jsonl --out dry-run-readout.md
```

Open `dry-run-readout.md`. **This is the point of the dry run:** you are
seeing the document your team will receive, while there is still time to fix
the metric definitions, add a missing guardrail, or realise the report does
not answer the question your director will ask.

---

## 5. What happens next

The dry run is where the fifteen minutes end. Running on real traffic needs
three more things, and all of them are people rather than software:

1. **Wire it into your service.** About twenty lines — one call to pick the
   version, a few to record what happened. See the
   [Integration Guide](docs/Integration-Guide.md).
2. **Get it approved.** Work through the
   [approval checklist](docs/Experiment-Approval-Checklist.md) and record
   each sign-off. The platform refuses to collect data until all five
   reviewers have signed, and that is deliberate.
3. **Read the [Pilot Deployment Guide](docs/Pilot-Deployment-Guide.md)**,
   which covers monitoring, rollback, and the readout.

---

## The commands, in the order you will need them

| Command | When |
| --- | --- |
| `civicexp init` | Planning a new evaluation |
| `civicexp templates` · `civicexp metrics` | Seeing what is available |
| `civicexp validate` | Before every launch |
| `civicexp power --config … --available N` | Checking the pilot can answer its question |
| `civicexp preview` | Confirming the split, writing nothing |
| `civicexp simulate` · `civicexp report` | Dry-running the readout |
| `civicexp assign` | Which version one person gets |
| `civicexp doctor` | Is this pilot's data trustworthy? |
| `civicexp analyze` | What do the results say? |
| `civicexp equity` | Did it work for everyone? |
| `civicexp report` | The readout to circulate |
| `civicexp case-summary` | A public summary other agencies can reuse |
| `civicexp verify` | Has the approval record been altered? |

---

## One warning worth repeating

`civicexp init` generates an **assignment salt** and puts it in your config
file. That salt is what keeps applicant identifiers pseudonymous. Treat the
config as a secret: restrict access to it, and never commit a real pilot's
config to a public repository. See the
[threat model](docs/Threat-Model.md#t5--pseudonym-reversal-by-guessing-identifiers).

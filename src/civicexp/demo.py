"""A demonstration workspace: the whole product, populated, in one command.

``civicexp demo`` builds a workspace with a team of synthetic people and
six evaluations, one per workflow template, each at a different point in
its life -- so a program manager can click through planning, review, a
running pilot that has tripped a harm alert, a sealed result, a recorded
decision, a rollback, and the evidence library without anyone writing a
line of configuration.

Everything in it is synthetic. Data is generated through the same
collection service a live pilot uses, with timestamps spread over the past
few weeks, and the governance record is written with a clock set to match.
The workspace is marked as a demonstration, so every page says so and every
case it publishes is labelled synthetic.
"""

from __future__ import annotations

import random
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .analysis import analyze
from .auth import PBKDF2_ITERATIONS, User
from .config import load_config
from .library import build_case_record
from .lifecycle import REQUIRED_APPROVALS
from .report import render_case_summary
from .simulate import SimulationSpec, satisfying_attributes, segment_mix_for, simulate_pilot
from .wizard import build_config
from .workspace import ExperimentRecord, Workspace

__all__ = ["DEMO_PEOPLE", "build_demo"]

#: The synthetic team: username, display name, roles.
DEMO_PEOPLE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("it.admin", "Workspace administrator", ("admin",)),
    ("a.rivera", "A. Rivera", ("program_owner",)),
    ("m.chen", "M. Chen", ("operations_lead",)),
    ("s.okafor", "S. Okafor", ("privacy_reviewer",)),
    ("d.whitfield", "D. Whitfield", ("legal_policy_reviewer",)),
    ("j.park", "J. Park", ("evaluation_lead",)),
    ("viewer", "Program analyst (read only)", ("viewer",)),
)
_HOLDER = {roles[0]: name for name, _, roles in DEMO_PEOPLE}


class _Clock:
    """A movable clock, so the governance record matches the simulated weeks."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        self.moment += timedelta(seconds=37)
        return self.moment

    def at(self, moment: datetime) -> None:
        self.moment = moment


@dataclass(frozen=True)
class _Traffic:
    completion: Mapping[str, float]
    error: Mapping[str, float]
    support: Mapping[str, float]
    returned: float = 0.08
    processing_days: float = 4.0
    overrides: Mapping[str, Mapping[str, float]] | None = None


def _simulate(record: ExperimentRecord, traffic: _Traffic, *, units: int,
              start: datetime, end: datetime, now: datetime, seed: int) -> None:
    """Send synthetic people through the live collector, over a past window."""
    rng = random.Random(seed)
    config = record.config()
    base = satisfying_attributes(config.eligibility)
    mix = segment_mix_for(config, base)
    collector = record.collector()
    span = (end - start).total_seconds()

    def draw(distribution: Mapping[str, float]) -> str:
        pick, total = rng.random() * sum(distribution.values()), 0.0
        for value, weight in distribution.items():
            total += weight
            if pick < total:
                return value
        return next(iter(distribution))

    arrivals = sorted(start + timedelta(seconds=rng.random() * span) for _ in range(units))
    for index, arrived in enumerate(arrivals):
        segments = {name: draw(values) for name, values in mix.items()}
        unit = f"demo-{record.id}-{seed}-{index:06d}"
        result = collector.assign(unit, {**base, **segments}, timestamp=arrived)
        if not result.in_analysis:
            continue
        arm = result.variant

        def at(seconds: float, moment: datetime = arrived) -> datetime | None:
            when = moment + timedelta(seconds=seconds)
            return when if when < now else None

        def record_event(event_type: str, when: datetime | None) -> None:
            if when is not None:
                collector.record(event_type, unit_id=unit, timestamp=when)  # noqa: B023 - used within this iteration

        rate = traffic.completion[arm]
        for key, rates in (traffic.overrides or {}).items():
            dimension, _, value = key.partition("=")
            if segments.get(dimension) == value:
                rate = rates[arm]
        elapsed = 300 * min(rng.lognormvariate(0.0, 0.6), 12.0)
        record_event("step_viewed", at(rng.uniform(2, 20)))
        if rng.random() < traffic.error[arm]:
            record_event("submission_failed", at(elapsed * 0.7))
        if rng.random() < traffic.support[arm]:
            record_event("support_contacted", at(elapsed * 0.8))
        if rng.random() < rate:
            record_event("submission_attempted", at(elapsed * 0.95))
            record_event("step_completed", at(elapsed))
            submitted = at(elapsed)
            record_event("submission_succeeded", submitted)
            if submitted is not None:
                if rng.random() < traffic.returned:
                    record_event("submission_returned",
                                 at(rng.uniform(1, 5) * 86400, submitted))
                record_event("processing_completed",
                             at(traffic.processing_days * 86400
                                * min(rng.lognormvariate(0.0, 0.5), 6.0), submitted))


def _people(ws: Workspace) -> dict[str, User]:
    return {name: ws.users.get(name) for name, _, _ in DEMO_PEOPLE}


def _plan(ws: Workspace, people: dict[str, User], template: str, answers: dict[str, Any]
          ) -> ExperimentRecord:
    raw, _ = build_config(template, {
        "owner_team": "benefits-digital-services",
        "jurisdiction": "example-county",
        **answers,
    })
    return ws.create_experiment(raw, people["j.park"], workflow=template)


def _sign(record: ExperimentRecord, people: dict[str, User], roles: tuple[str, ...]) -> None:
    notes = {
        "program_owner": "Scope is on-screen wording only; reversible within a day.",
        "operations_lead": "Support line briefed; rollback rehearsed in staging.",
        "privacy_reviewer": "Coded attributes only; 90-day retention; threshold 11.",
        "legal_policy_reviewer": "No change to eligibility rules, documents, or deadlines.",
        "evaluation_lead": "Powered for the stated effect; looks fixed in the plan.",
    }
    for role in roles:
        record.sign_off(people[_HOLDER[role]], role, notes[role])


def build_demo(
    root: str | Path,
    *,
    password: str | None = None,
    password_iterations: int = PBKDF2_ITERATIONS,
    now: datetime | None = None,
) -> tuple[Workspace, str]:
    """Create and populate a demonstration workspace. Returns it and the password."""
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    clock = _Clock(now - timedelta(days=70))
    password = password or f"demo-{secrets.token_urlsafe(9)}"
    ws = Workspace.create(
        root,
        name="Example County Human Services",
        jurisdiction="Example County (synthetic)",
        actor="demo",
        password_iterations=password_iterations,
        clock=clock,
        synthetic_data=True,
    )
    for username, display, roles in DEMO_PEOPLE:
        ws.add_user("demo", username, password=password, roles=roles, display_name=display)
    people = _people(ws)
    days = lambda n: now - timedelta(days=n)  # noqa: E731

    # 1. Finished and published: the change worked, for everyone.
    clock.at(days(62))
    upload = _plan(ws, people, "document-upload", {
        "name": "Document upload: plain-language checklist",
        "description": "Applicants upload proof of identity and income. Today the page "
                       "shows a dense paragraph of requirements; the tested version shows "
                       "a checklist naming each accepted document with an example. No "
                       "eligibility rule, required document, or deadline changes.",
        "treatment_description": "Checklist of accepted documents, with an example of each",
        "hypothesis": "If applicants see a checklist of accepted documents, more of them "
                      "will complete the upload step, without more calls to the support line.",
        "baseline_rate": 0.62, "minimum_effect": 0.03, "monthly_volume": 9000,
        "duration_days": 28,
    })
    clock.at(days(58))
    _sign(upload, people, REQUIRED_APPROVALS)
    upload.approve(people["a.rivera"])
    clock.at(days(56))
    upload.start(people["m.chen"])
    upload_traffic = _Traffic({"control": 0.62, "treatment": 0.70},
                              {"control": 0.05, "treatment": 0.048},
                              {"control": 0.06, "treatment": 0.055})
    _simulate(upload, upload_traffic, units=4300, start=days(56), end=days(42),
              now=days(42), seed=11)
    clock.at(days(42))
    upload.take_look(people["j.park"])
    _simulate(upload, upload_traffic, units=4300, start=days(42), end=days(28),
              now=days(27), seed=12)
    clock.at(days(27))
    upload.conclude(people["a.rivera"], outcome="complete",
                    note="Pilot reached its planned size.")
    clock.at(days(26))
    upload.publish(people["j.park"])

    # 2. Rolled back: better on average, worse for Spanish-preference applicants.
    clock.at(days(50))
    screener = _plan(ws, people, "eligibility-screener", {
        "name": "Eligibility screener: shorter income questions",
        "description": "The screener asks five income questions. The tested version merges "
                       "them into two. The English text was rewritten; the Spanish "
                       "translation was machine-generated and not reviewed.",
        "treatment_description": "Two merged income questions",
        "hypothesis": "If the income questions are merged, more people will finish the "
                      "screener, without fewer of them going on to apply.",
        "baseline_rate": 0.70, "minimum_effect": 0.03, "monthly_volume": 12000,
        "duration_days": 21,
    })
    clock.at(days(46))
    _sign(screener, people, REQUIRED_APPROVALS)
    screener.approve(people["a.rivera"])
    clock.at(days(44))
    screener.start(people["m.chen"])
    _simulate(screener, _Traffic({"control": 0.70, "treatment": 0.75},
                                 {"control": 0.04, "treatment": 0.04},
                                 {"control": 0.05, "treatment": 0.05},
                                 overrides={"preferred_language=es":
                                            {"control": 0.70, "treatment": 0.57}}),
              units=8000, start=days(44), end=days(31), now=days(30), seed=23)
    clock.at(days(30))
    screener.conclude(people["m.chen"], outcome="roll_back",
                      note="Equity review: Spanish-preference applicants were measurably "
                           "harmed. Translation to be reviewed before any retest.")
    clock.at(days(29))
    screener.publish(people["j.park"])

    # 3. Running, and tripping a harm alert.
    clock.at(days(20))
    renewal = _plan(ws, people, "renewal-reminder", {
        "name": "Renewal reminder: send three weeks earlier",
        "description": "Renewal reminders go out 10 days before the deadline. The tested "
                       "version sends the same reminder 31 days before. The deadline, the "
                       "documents required, and the renewal process do not change.",
        "treatment_description": "Same reminder, sent 31 days before the deadline",
        "hypothesis": "If reminders arrive earlier, more people will renew on time, without "
                      "more calls to the support line.",
        "baseline_rate": 0.60, "minimum_effect": 0.03, "monthly_volume": 9000,
        "duration_days": 45,
    })
    clock.at(days(16))
    _sign(renewal, people, REQUIRED_APPROVALS)
    renewal.approve(people["a.rivera"])
    clock.at(days(14))
    renewal.start(people["m.chen"])
    _simulate(renewal, _Traffic({"control": 0.60, "treatment": 0.62},
                                {"control": 0.03, "treatment": 0.03},
                                {"control": 0.05, "treatment": 0.12}),
              units=4000, start=days(14), end=now - timedelta(hours=2), now=now, seed=37)

    # 4. Running healthily, result sealed, one interim look on the record.
    clock.at(days(30))
    intake = _plan(ws, people, "digital-intake", {
        "name": "Digital intake: one question per page",
        "description": "The intake form shows twelve questions on one page. The tested "
                       "version shows one question per page with a progress indicator. "
                       "The questions and required fields do not change.",
        "treatment_description": "One question per page, with progress shown",
        "hypothesis": "If the form asks one question at a time, more people will finish it, "
                      "without more incomplete submissions.",
        "baseline_rate": 0.55, "minimum_effect": 0.03, "monthly_volume": 10000,
        "duration_days": 35,
    })
    clock.at(days(25))
    _sign(intake, people, REQUIRED_APPROVALS)
    intake.approve(people["a.rivera"])
    clock.at(days(21))
    intake.start(people["m.chen"])
    intake_traffic = _Traffic({"control": 0.55, "treatment": 0.59},
                              {"control": 0.04, "treatment": 0.04},
                              {"control": 0.05, "treatment": 0.05})
    _simulate(intake, intake_traffic, units=4000, start=days(21), end=days(7),
              now=days(7), seed=41)
    clock.at(days(7))
    intake.take_look(people["j.park"])
    _simulate(intake, intake_traffic, units=2000, start=days(7),
              end=now - timedelta(hours=1), now=now, seed=42)

    # 5. In review, with a dry run to look at.
    clock.at(days(4))
    notice = _plan(ws, people, "missing-information-notice", {
        "name": "Missing-information notice: name the document",
        "description": "When an application is missing a document, the notice cites the "
                       "regulation. The tested version names the specific document, shows "
                       "how to upload it, and states one deadline. What is required does "
                       "not change.",
        "treatment_description": "Notice naming the missing document and how to send it",
        "hypothesis": "If the notice names the document and how to send it, more applicants "
                      "will provide it, and processing will finish sooner.",
        "baseline_rate": 0.50, "minimum_effect": 0.04, "monthly_volume": 3000,
        "duration_days": 42,
    })
    clock.at(days(3))
    notice.dry_run(people["j.park"], control_rate=0.50, treatment_rate=0.57)
    clock.at(days(2))
    _sign(notice, people, ("program_owner", "privacy_reviewer", "evaluation_lead"))

    # 6. Approved and waiting for the service to be connected.
    clock.at(days(9))
    reminder = _plan(ws, people, "appointment-reminder", {
        "name": "Interview reminder: what to bring",
        "description": "The interview reminder text gives the time only. The tested version "
                       "adds the three documents to bring. The appointment process does not "
                       "change.",
        "treatment_description": "Reminder listing the three documents to bring",
        "hypothesis": "If the reminder lists what to bring, more people will attend with "
                      "their documents, without more calls to reschedule.",
        "baseline_rate": 0.70, "minimum_effect": 0.04, "monthly_volume": 6000,
        "duration_days": 30,
    })
    clock.at(days(5))
    _sign(reminder, people, REQUIRED_APPROVALS)
    clock.at(days(1))
    reminder.approve(people["a.rivera"])

    # Evidence from elsewhere: a case another county published.
    _neighbor_case(ws, now)
    return ws, password


def _neighbor_case(ws: Workspace, now: datetime) -> None:
    """A synthetic case from a different jurisdiction, as an import would arrive."""
    raw, _ = build_config("renewal-reminder", {
        "name": "Renewal reminder: name the documents needed",
        "description": "The renewal reminder listed the deadline only. The tested version "
                       "also named the documents to send and linked to the upload page.",
        "treatment_description": "Reminder naming the documents to send",
        "hypothesis": "If the reminder names the documents, more people renew on time.",
        "jurisdiction": "neighboring-county",
        "baseline_rate": 0.58, "minimum_effect": 0.03, "monthly_volume": 9000,
        "duration_days": 45,
    })
    config = load_config(raw)
    attributes = satisfying_attributes(config.eligibility)
    spec = SimulationSpec(
        unit_attributes=attributes,
        segment_distribution=segment_mix_for(config, attributes),
        completion_rate={"control": 0.58, "treatment": 0.65},
        error_rate={"control": 0.03, "treatment": 0.03},
        support_rate={"control": 0.05, "treatment": 0.045},
        returned_rate={"control": 0.10, "treatment": 0.07},
        units=8000, seed=53,
        start=now - timedelta(days=150),
    )
    result = analyze(config, simulate_pilot(config, spec))
    published = now - timedelta(days=90)
    record = build_case_record(
        result, raw_config=raw, jurisdiction="Neighboring County (synthetic)",
        synthetic=True, published=published, platform_version=__version__,
    )
    ws.library.add(record, markdown=render_case_summary(
        result, jurisdiction="Neighboring County (synthetic)", generated_at=published))

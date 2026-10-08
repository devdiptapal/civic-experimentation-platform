"""Shared fixtures for tests of the hosted product: workspaces, people, traffic."""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from civicexp.lifecycle import REQUIRED_APPROVALS
from civicexp.wizard import build_config
from civicexp.workspace import Workspace

#: Cheap hashing for tests. Production uses the OWASP iteration count.
FAST = 1000
PASSWORD = "correct horse battery staple"

#: One person per role, so the distinct-reviewer rule can be satisfied.
PEOPLE = {
    "admin": ("admin",),
    "owner": ("program_owner",),
    "ops": ("operations_lead",),
    "privacy": ("privacy_reviewer",),
    "legal": ("legal_policy_reviewer",),
    "lead": ("evaluation_lead",),
    "watcher": ("viewer",),
}
ROLE_HOLDER = {roles[0]: name for name, roles in PEOPLE.items()}

ANSWERS = {
    "name": "Upload checklist",
    "description": "Replace the requirements paragraph with a checklist.",
    "hypothesis": "A checklist helps people upload the right document.",
    "treatment_description": "Checklist of accepted documents",
    "owner_team": "digital-services",
    "jurisdiction": "example-county",
    "baseline_rate": 0.62,
    "minimum_effect": 0.03,
    "monthly_volume": 12000,
    "duration_days": 28,
}

ELIGIBLE = {
    "channel": "web",
    "application_type": "new",
    "staff_assisted": False,
    "manual_review_flag": False,
}


class WorkspaceFixture:
    """A temporary workspace with one account per role."""

    def __init__(self, **settings):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.ws = Workspace.create(
            self.root, name="Test workspace", jurisdiction="Example County",
            password_iterations=FAST, **settings,
        )
        for name, roles in PEOPLE.items():
            self.ws.add_user("setup", name, password=PASSWORD, roles=roles,
                             display_name=name.title())

    def cleanup(self):
        self._tmp.cleanup()

    def user(self, name):
        return self.ws.users.get(name)

    def holder(self, role):
        return self.user(ROLE_HOLDER[role])

    def new_experiment(self, template="document-upload", **overrides):
        answers = dict(ANSWERS, **overrides)
        raw, _ = build_config(template, answers)
        return self.ws.create_experiment(raw, self.user("lead"), workflow=template)

    def approved(self, record):
        for role in REQUIRED_APPROVALS:
            record.sign_off(self.holder(role), role, "ok")
        record.approve(self.user("owner"))
        return record

    def running(self, record):
        self.approved(record)
        record.start(self.user("ops"))
        return record


def drive_traffic(record, units, *, completion, error=None, seed=7,
                  segment=None):
    """Send synthetic people through the live collector.

    ``completion`` and ``error`` map variant to rate. ``segment`` maps a
    preferred_language value to a completion override per variant.
    """
    rng = random.Random(seed)
    collector = record.collector()
    for i in range(units):
        language = "es" if rng.random() < 0.25 else "en"
        result = collector.assign(
            f"person-{seed}-{i}", dict(ELIGIBLE, preferred_language=language,
                                       device_type="mobile")
        )
        if not result.in_analysis:
            continue
        rate = completion[result.variant]
        if segment and language in segment:
            rate = segment[language][result.variant]
        if error and rng.random() < error[result.variant]:
            collector.record("submission_failed", unit_id=f"person-{seed}-{i}")
        if rng.random() < rate:
            collector.record("submission_attempted", unit_id=f"person-{seed}-{i}")
            collector.record("submission_succeeded", unit_id=f"person-{seed}-{i}",
                             variant=result.variant)
    return collector

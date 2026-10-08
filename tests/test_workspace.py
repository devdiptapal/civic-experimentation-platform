"""Tests for the hosted product's core: workspaces, review, collection, results.

These exercise the rules the product layer adds on top of the library --
who may do what, what the live service may record, and when a result may be
seen -- against a real workspace on disk.
"""

from __future__ import annotations

import json
import os
import stat
import unittest
from datetime import datetime, timedelta, timezone

from civicexp.auth import AuthError
from civicexp.collect import Collector
from civicexp.errors import ConfigError, EventError, LifecycleError
from civicexp.lifecycle import REQUIRED_APPROVALS, State
from civicexp.monitor import check_experiment, check_workspace
from civicexp.workspace import Workspace, WorkspaceError, unfinished_fields
from tests.helpers import ELIGIBLE, FAST, PASSWORD, WorkspaceFixture, drive_traffic


class FixtureCase(unittest.TestCase):
    settings: dict = {}

    def setUp(self):
        self.fx = WorkspaceFixture(**self.settings)
        self.addCleanup(self.fx.cleanup)
        self.ws = self.fx.ws


class TestWorkspaceSetup(FixtureCase):
    def test_opening_a_folder_that_is_not_a_workspace_says_how_to_make_one(self):
        with self.assertRaises(WorkspaceError) as ctx:
            Workspace(self.fx.root / "nope")
        self.assertEqual(ctx.exception.code, "WORKSPACE_MISSING")
        self.assertIn("civicexp workspace init", ctx.exception.message)

    def test_cannot_create_over_an_existing_workspace(self):
        with self.assertRaises(WorkspaceError):
            Workspace.create(self.fx.root, name="again", password_iterations=FAST)

    def test_account_file_holds_hashes_only_and_is_private(self):
        text = (self.fx.root / "users.json").read_text()
        self.assertNotIn(PASSWORD, text)
        self.assertIn("pbkdf2_sha256$", text)
        if os.name == "posix":
            mode = stat.S_IMODE((self.fx.root / "users.json").stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_administrative_actions_are_on_the_record(self):
        actions = [e.action for e in self.ws.audit()]
        self.assertEqual(actions[0], "workspace:created")
        self.assertEqual(actions.count("user:created"), 7)
        self.assertTrue(self.ws.audit().verify().valid)

    def test_only_admins_change_settings(self):
        with self.assertRaises(AuthError):
            self.ws.update_settings(self.fx.user("owner"), require_distinct_reviewers=False)
        self.ws.update_settings(self.fx.user("admin"), require_distinct_reviewers=False)
        self.assertFalse(self.ws.settings["require_distinct_reviewers"])
        self.assertEqual(self.ws.audit().entries[-1].action, "settings:changed")


class TestPlanAndReview(FixtureCase):
    def test_creating_a_plan_needs_a_planning_role(self):
        for name in ("admin", "watcher", "ops"):
            with self.subTest(user=name), self.assertRaises(AuthError):
                self.fx.ws.create_experiment({}, self.fx.user(name))
        record = self.fx.new_experiment()
        self.assertIs(record.lifecycle().state, State.DRAFT)
        self.assertEqual(record.audit().entries[0].action, "config:created")

    def test_duplicate_ids_are_refused(self):
        self.fx.new_experiment()
        with self.assertRaises(WorkspaceError) as ctx:
            self.fx.new_experiment()
        self.assertEqual(ctx.exception.code, "WORKSPACE_DUPLICATE")

    def test_sign_off_needs_the_role(self):
        record = self.fx.new_experiment()
        with self.assertRaises(AuthError):
            record.sign_off(self.fx.user("ops"), "privacy_reviewer")

    def test_one_person_cannot_fill_two_review_seats(self):
        record = self.fx.new_experiment()
        both = self.ws.add_user("setup", "both", password=PASSWORD,
                                roles=["privacy_reviewer", "legal_policy_reviewer"])
        record.sign_off(both, "privacy_reviewer")
        with self.assertRaises(LifecycleError) as ctx:
            record.sign_off(both, "legal_policy_reviewer")
        self.assertEqual(ctx.exception.code, "LIFECYCLE_SAME_REVIEWER")

    def test_unfinished_plans_cannot_be_reviewed(self):
        record = self.fx.new_experiment(hypothesis="", treatment_description="")
        self.assertIn("hypothesis.statement", record.unfinished())
        with self.assertRaises(LifecycleError) as ctx:
            record.sign_off(self.fx.holder("program_owner"), "program_owner")
        self.assertEqual(ctx.exception.code, "LIFECYCLE_PLAN_UNFINISHED")

    def test_revising_the_plan_makes_earlier_sign_offs_stale(self):
        record = self.fx.new_experiment()
        for role in REQUIRED_APPROVALS:
            record.sign_off(self.fx.holder(role), role)
        raw = record.raw()
        raw["metrics"]["primary"]["minimum_effect_of_interest"] = 0.04
        record.revise(self.fx.user("lead"), raw)
        self.assertTrue(record.sign_off_is_stale("privacy_reviewer"))
        with self.assertRaises(LifecycleError) as ctx:
            record.approve(self.fx.user("owner"))
        self.assertEqual(ctx.exception.code, "LIFECYCLE_STALE_SIGNOFF")
        for role in REQUIRED_APPROVALS:
            record.sign_off(self.fx.holder(role), role)
        record.approve(self.fx.user("owner"))
        self.assertIs(record.lifecycle().state, State.APPROVED)

    def test_approved_plans_are_locked(self):
        record = self.fx.approved(self.fx.new_experiment())
        with self.assertRaises(LifecycleError) as ctx:
            record.revise(self.fx.user("lead"), record.raw())
        self.assertEqual(ctx.exception.code, "LIFECYCLE_PLAN_LOCKED")

    def test_a_plan_edited_on_disk_after_approval_stops_collection(self):
        record = self.fx.running(self.fx.new_experiment())
        self.assertTrue(record.collecting()[0])
        raw = record.raw()
        raw["metrics"]["primary"]["minimum_effect_of_interest"] = 0.01
        record.config_path.write_text(json.dumps(raw))
        collecting, reason = record.collecting()
        self.assertFalse(collecting)
        self.assertIn("changed after it was approved", reason)
        alerts = check_experiment(record)
        self.assertIn("plan_changed", [a.kind for a in alerts])

    def test_a_tampered_governance_record_halts_collection(self):
        record = self.fx.running(self.fx.new_experiment())
        lines = record.audit_path.read_text().splitlines()
        lines[2] = lines[2].replace('"ok"', '"edited"')
        record.audit_path.write_text("\n".join(lines) + "\n")
        self.assertFalse(record.collecting()[0])
        self.assertEqual(check_experiment(record)[0].kind, "audit_broken")

    def test_roles_are_enforced_on_each_transition(self):
        record = self.fx.approved(self.fx.new_experiment())
        with self.assertRaises(AuthError):
            record.start(self.fx.user("privacy"))
        record.start(self.fx.user("ops"))
        # Anyone with a stake may pause; resuming needs operations or the owner.
        record.pause(self.fx.user("privacy"), "saw something odd")
        with self.assertRaises(AuthError):
            record.resume(self.fx.user("privacy"))
        with self.assertRaises(AuthError):
            record.pause(self.fx.user("watcher"), "no")
        record.resume(self.fx.user("ops"))


class TestCollection(FixtureCase):
    def setUp(self):
        super().setUp()
        self.record = self.fx.new_experiment()
        self.collector = self.record.collector()

    def test_nothing_is_collected_before_start(self):
        result = self.collector.assign("a-1", ELIGIBLE)
        self.assertFalse(result.collecting)
        self.assertEqual(result.serve, "control")
        self.assertIsNone(result.variant)
        self.assertFalse(self.record.events_path.exists())

    def test_enrolls_once_and_keeps_only_declared_attributes(self):
        self.fx.running(self.record)
        attrs = dict(ELIGIBLE, preferred_language="es", device_type="mobile")
        first = self.collector.assign("applicant-1", attrs)
        again = self.collector.assign("applicant-1", attrs)
        self.assertTrue(first.newly_enrolled)
        self.assertFalse(again.newly_enrolled)
        self.assertEqual(first.variant, again.variant)
        lines = self.record.events_path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])["payload"]
        self.assertEqual(payload, {"preferred_language": "es", "device_type": "mobile"})
        self.assertNotIn("applicant-1", self.record.events_path.read_text())

    def test_ineligible_people_get_the_current_version_and_leave_no_trace(self):
        self.fx.running(self.record)
        result = self.collector.assign("x", dict(ELIGIBLE, staff_assisted=True))
        self.assertFalse(result.in_analysis)
        self.assertEqual(result.serve, "control")
        self.assertFalse(self.record.events_path.exists())

    def test_outcomes_must_belong_to_an_enrolled_person_and_version(self):
        self.fx.running(self.record)
        with self.assertRaises(EventError) as ctx:
            self.collector.record("submission_succeeded", unit_id="stranger")
        self.assertEqual(ctx.exception.code, "EVENT_UNIT_NOT_ENROLLED")
        assigned = self.collector.assign("p", ELIGIBLE)
        wrong = "control" if assigned.variant != "control" else "treatment"
        with self.assertRaises(EventError) as ctx:
            self.collector.record("submission_succeeded", unit_id="p", variant=wrong)
        self.assertEqual(ctx.exception.code, "EVENT_VARIANT_MISMATCH")
        with self.assertRaises(EventError) as ctx:
            self.collector.record("experiment_assigned", unit_id="p")
        self.assertEqual(ctx.exception.code, "EVENT_USE_ASSIGN")
        event = self.collector.record("submission_succeeded",
                                      unit_pseudonym=assigned.unit_pseudonym)
        self.assertEqual(event.variant, assigned.variant)

    def test_future_timestamps_are_refused(self):
        self.fx.running(self.record)
        self.collector.assign("p", ELIGIBLE)
        later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        with self.assertRaises(EventError) as ctx:
            self.collector.record("submission_succeeded", unit_id="p", timestamp=later)
        self.assertEqual(ctx.exception.code, "EVENT_FUTURE_TIMESTAMP")

    def test_pausing_stops_exposure_and_recording(self):
        self.fx.running(self.record)
        self.collector.assign("p", ELIGIBLE)
        self.record.pause(self.fx.user("ops"), "outage")
        result = self.collector.assign("p", ELIGIBLE)
        self.assertEqual(result.serve, "control")
        self.assertFalse(result.collecting)
        with self.assertRaises(EventError) as ctx:
            self.collector.record("submission_succeeded", unit_id="p")
        self.assertEqual(ctx.exception.code, "EVENT_NOT_COLLECTING")

    def test_a_second_writer_sees_enrollments_made_by_the_first(self):
        self.fx.running(self.record)
        self.collector.assign("p", ELIGIBLE)
        other = Collector(self.record.config(), self.record.events_path)
        other.record("submission_succeeded", unit_id="p")
        self.assertEqual(sum(other.enrolled_counts().values()), 1)

    def test_segment_values_follow_people_to_the_denominator_event(self):
        # A plan whose main measure counts from submission_attempted reads
        # equity attributes there, so the collector carries them over.
        raw = self.record.raw()
        raw["experiment"]["id"] = "exp-attempt-based"
        raw["metrics"]["primary"] = {
            "use": "first_time_success_rate", "minimum_effect_of_interest": 0.05
        }
        record = self.ws.create_experiment(raw, self.fx.user("lead"))
        self.fx.running(record)
        collector = record.collector()
        collector.assign("p", dict(ELIGIBLE, preferred_language="zh"))
        event = collector.record("submission_attempted", unit_id="p")
        self.assertEqual(event.payload["preferred_language"], "zh")


class TestResultsAndDecision(FixtureCase):
    def setUp(self):
        super().setUp()
        self.record = self.fx.running(self.fx.new_experiment())
        drive_traffic(self.record, 3000, completion={"control": 0.6, "treatment": 0.7},
                      error={"control": 0.05, "treatment": 0.05})

    def test_results_are_sealed_while_running(self):
        self.assertEqual(self.record.visibility(), "sealed")

    def test_only_the_evaluation_lead_can_look_and_each_look_is_counted(self):
        with self.assertRaises(AuthError):
            self.record.take_look(self.fx.user("owner"))
        number, outcome = self.record.take_look(self.fx.user("lead"))
        self.assertEqual(number, 1)
        self.assertTrue(outcome.results)
        entry = self.record.looks()[0]
        self.assertEqual(entry.detail["look"], 1)
        self.assertIn(entry.detail["decision"], ("promote", "iterate", "inconclusive"))
        self.assertTrue(entry.detail["within_plan"])

    def test_concluding_freezes_the_record(self):
        final = self.record.conclude(self.fx.user("owner"), outcome="complete", note="")
        self.assertTrue(final.results)
        self.assertEqual(self.record.visibility(), "final")
        readout = (self.record.final_dir / "readout.md").read_text()
        self.assertIn("Decision and approval record", readout)
        self.assertIn("state:running->completed", readout)
        self.assertTrue((self.record.final_dir / "case-record.json").exists())
        actions = [e.action for e in self.record.audit()]
        self.assertEqual(actions[-1], "record:final_frozen")
        completion = next(e for e in self.record.audit() if e.action.endswith("->completed"))
        self.assertIn("decision", completion.detail)

    def test_looks_beyond_the_plan_are_disclosed_in_the_final_readout(self):
        planned = self.record.config().decision.planned_looks
        for _ in range(planned):
            self.record.take_look(self.fx.user("lead"))
        self.assertFalse(self.record.looks()[-1].detail["within_plan"])
        self.record.conclude(self.fx.user("owner"), outcome="complete", note="")
        readout = (self.record.final_dir / "readout.md").read_text()
        self.assertIn(f"examined {planned + 1} times", readout)

    def test_publishing_puts_the_case_in_the_library(self):
        with self.assertRaises(LifecycleError):
            self.record.publish(self.fx.user("owner"))
        self.record.conclude(self.fx.user("owner"), outcome="complete", note="done")
        case = self.record.publish(self.fx.user("lead"))
        self.assertEqual(self.ws.library.get(case.case_id).title, "Upload checklist")
        self.assertFalse(case.synthetic)
        self.assertTrue(self.ws.library.summary_markdown(case.case_id))

    def test_retention_deletes_events_but_keeps_the_decision(self):
        self.record.conclude(self.fx.user("owner"), outcome="complete", note="")
        self.assertIsNone(self.record.apply_retention())
        later = datetime.now(timezone.utc) + timedelta(days=91)
        self.assertIn("retention_due", [a.kind for a in check_experiment(self.record, now=later)])
        deleted = self.record.apply_retention(now=later)
        self.assertIn("events.jsonl", deleted)
        self.assertFalse(self.record.events_path.exists())
        self.assertTrue((self.record.final_dir / "readout.md").exists())
        self.assertEqual(self.record.audit().entries[-1].action, "retention:events_deleted")
        self.assertTrue(self.record.audit().verify().valid)


class TestMonitoring(FixtureCase):
    def test_a_breached_guardrail_is_critical_and_ranked_first(self):
        draft = self.fx.new_experiment(name="Another plan")
        breaching = self.fx.running(self.fx.new_experiment())
        drive_traffic(breaching, 3000, completion={"control": 0.6, "treatment": 0.7},
                      error={"control": 0.04, "treatment": 0.16})
        alerts = check_workspace(self.ws)
        self.assertEqual(alerts[0].severity, "critical")
        self.assertEqual(alerts[0].kind, "guardrail_breach")
        self.assertIn("error_rate", alerts[0].detail)
        kinds = {(a.experiment_id, a.kind) for a in alerts}
        self.assertIn((draft.id, "awaiting_sign_off"), kinds)

    def test_alerts_never_reveal_a_sealed_result(self):
        record = self.fx.running(self.fx.new_experiment())
        drive_traffic(record, 3000, completion={"control": 0.5, "treatment": 0.7})
        text = " ".join(f"{a.title} {a.detail}" for a in check_experiment(record))
        self.assertNotIn("completion_rate", text)
        self.assertNotIn("promote", text.lower())

    def test_silence_from_a_running_pilot_is_noticed(self):
        record = self.fx.running(self.fx.new_experiment())
        later = datetime.now(timezone.utc) + timedelta(hours=30)
        self.assertIn("no_data", [a.kind for a in check_experiment(record, now=later)])
        drive_traffic(record, 50, completion={"control": 0.5, "treatment": 0.5})
        self.assertIn("stale_data", [a.kind for a in check_experiment(record, now=later)])


class TestDryRun(FixtureCase):
    def test_dry_runs_are_kept_apart_from_live_data(self):
        record = self.fx.new_experiment()
        events = record.dry_run(self.fx.user("lead"), control_rate=0.6, treatment_rate=0.7)
        self.assertGreater(events, 0)
        self.assertTrue(record.dry_run_path.exists())
        self.assertFalse(record.events_path.exists())
        outcome = record.analyses("dry_run")
        self.assertTrue(outcome.results, outcome.problem)
        self.assertIsNotNone(outcome.results[0].equity)
        self.fx.running(record)
        with self.assertRaises(LifecycleError):
            record.dry_run(self.fx.user("lead"), control_rate=0.6, treatment_rate=0.7)

    def test_needs_a_planning_role(self):
        record = self.fx.new_experiment()
        with self.assertRaises(AuthError):
            record.dry_run(self.fx.user("ops"), control_rate=0.6, treatment_rate=0.7)


class TestSyntheticWorkspace(FixtureCase):
    settings = {"synthetic_data": True}

    def test_records_from_a_demonstration_workspace_are_marked_synthetic(self):
        record = self.fx.running(self.fx.new_experiment())
        drive_traffic(record, 2000, completion={"control": 0.6, "treatment": 0.7})
        record.conclude(self.fx.user("owner"), outcome="complete", note="")
        self.assertTrue(record.publish(self.fx.user("owner")).synthetic)


class TestUnfinishedFields(unittest.TestCase):
    def test_finds_nested_markers_and_ignores_template_headers(self):
        raw = {"_template": {"x": "FILL IN"}, "a": {"b": ["ok", "FILL IN: this"]}}
        self.assertEqual(unfinished_fields(raw), ["a.b[1]"])

    def test_creating_with_an_invalid_plan_reports_config_errors(self):
        fx = WorkspaceFixture()
        self.addCleanup(fx.cleanup)
        with self.assertRaises(ConfigError):
            fx.ws.create_experiment({"experiment": {"id": "x", "name": "x"}}, fx.user("lead"))

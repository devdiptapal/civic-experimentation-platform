"""Tests for administrative controls and the audit trail."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from civicexp.audit import GENESIS_HASH, AuditLog
from civicexp.errors import LifecycleError
from civicexp.lifecycle import REQUIRED_APPROVALS, ExperimentLifecycle, State


def fully_signed(lifecycle: ExperimentLifecycle) -> ExperimentLifecycle:
    for role in REQUIRED_APPROVALS:
        lifecycle.sign_off(role, f"{role}-user")
    return lifecycle


class TestApprovalGate(unittest.TestCase):
    def test_starts_in_draft_and_not_collecting(self):
        lifecycle = ExperimentLifecycle("exp-1")
        self.assertIs(lifecycle.state, State.DRAFT)
        self.assertFalse(lifecycle.is_collecting)

    def test_cannot_approve_without_every_sign_off(self):
        lifecycle = ExperimentLifecycle("exp-1")
        lifecycle.sign_off("program_owner", "a")
        with self.assertRaises(LifecycleError) as ctx:
            lifecycle.approve("a")
        self.assertEqual(ctx.exception.code, "LIFECYCLE_MISSING_APPROVAL")
        self.assertIn("privacy_reviewer", ctx.exception.message)

    def test_missing_approvals_are_listed(self):
        lifecycle = ExperimentLifecycle("exp-1")
        self.assertEqual(set(lifecycle.missing_approvals), set(REQUIRED_APPROVALS))
        lifecycle.sign_off("privacy_reviewer", "s")
        self.assertNotIn("privacy_reviewer", lifecycle.missing_approvals)

    def test_approves_once_fully_signed(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        self.assertIs(lifecycle.state, State.APPROVED)

    def test_unknown_reviewer_role_is_rejected(self):
        with self.assertRaises(LifecycleError) as ctx:
            ExperimentLifecycle("exp-1").sign_off("chief_vibes_officer", "a")
        self.assertEqual(ctx.exception.code, "LIFECYCLE_UNKNOWN_ROLE")

    def test_sign_off_after_draft_is_rejected(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        with self.assertRaises(LifecycleError) as ctx:
            lifecycle.sign_off("program_owner", "b")
        self.assertEqual(ctx.exception.code, "LIFECYCLE_SIGNOFF_LATE")

    def test_required_roles_match_the_published_checklist(self):
        self.assertEqual(
            set(REQUIRED_APPROVALS),
            {
                "program_owner",
                "operations_lead",
                "privacy_reviewer",
                "legal_policy_reviewer",
                "evaluation_lead",
            },
        )


class TestCollectionGuard(unittest.TestCase):
    def test_draft_blocks_collection(self):
        with self.assertRaises(LifecycleError) as ctx:
            ExperimentLifecycle("exp-1").require_collecting()
        self.assertEqual(ctx.exception.code, "LIFECYCLE_NOT_COLLECTING")
        self.assertIn("not been approved", ctx.exception.message)

    def test_running_permits_collection(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        lifecycle.start("a")
        lifecycle.require_collecting()  # must not raise
        self.assertTrue(lifecycle.is_collecting)

    def test_pause_stops_collection_everywhere(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        lifecycle.start("a")
        lifecycle.pause("a", reason="outage")
        self.assertFalse(lifecycle.is_collecting)
        with self.assertRaises(LifecycleError) as ctx:
            lifecycle.require_collecting()
        self.assertIn("paused", ctx.exception.message)

    def test_resume_restores_collection(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        lifecycle.start("a")
        lifecycle.pause("a", reason="outage")
        lifecycle.resume("a")
        self.assertTrue(lifecycle.is_collecting)

    def test_each_blocked_state_explains_itself(self):
        for setup, fragment in (
            (lambda lc: None, "not been approved"),
            (lambda lc: (fully_signed(lc), lc.approve("a")), "not started"),
        ):
            lifecycle = ExperimentLifecycle("exp-1")
            setup(lifecycle)
            with self.assertRaises(LifecycleError) as ctx:
                lifecycle.require_collecting()
            self.assertIn(fragment, ctx.exception.message)


class TestTransitions(unittest.TestCase):
    def running(self) -> ExperimentLifecycle:
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        lifecycle.start("a")
        return lifecycle

    def test_cannot_start_from_draft(self):
        with self.assertRaises(LifecycleError) as ctx:
            ExperimentLifecycle("exp-1").start("a")
        self.assertEqual(ctx.exception.code, "LIFECYCLE_FORBIDDEN")

    def test_pause_requires_a_reason(self):
        with self.assertRaises(LifecycleError) as ctx:
            self.running().pause("a", reason="")
        self.assertEqual(ctx.exception.code, "LIFECYCLE_NO_REASON")

    def test_rollback_requires_a_reason(self):
        with self.assertRaises(LifecycleError):
            self.running().roll_back("a", reason="")

    def test_terminal_states_cannot_be_reopened(self):
        lifecycle = self.running()
        lifecycle.complete("a")
        for action in (
            lambda: lifecycle.start("a"),
            lambda: lifecycle.resume("a"),
            lambda: lifecycle.pause("a", reason="x"),
            lambda: lifecycle.roll_back("a", reason="x"),
        ):
            with self.assertRaises(LifecycleError):
                action()

    def test_archived_is_fully_terminal(self):
        lifecycle = self.running()
        lifecycle.roll_back("a", reason="guardrail breached")
        lifecycle.archive("a")
        self.assertTrue(lifecycle.state.is_terminal)
        with self.assertRaises(LifecycleError):
            lifecycle.archive("a")

    def test_approved_can_be_reopened_to_draft(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        lifecycle.reopen("a", reason="scope changed")
        self.assertIs(lifecycle.state, State.DRAFT)

    def test_can_end_directly_from_paused(self):
        lifecycle = self.running()
        lifecycle.pause("a", reason="outage")
        lifecycle.roll_back("a", reason="not resuming")
        self.assertIs(lifecycle.state, State.ROLLED_BACK)


class TestAuditChain(unittest.TestCase):
    def test_empty_chain_is_valid(self):
        log = AuditLog()
        self.assertTrue(log.verify())
        self.assertEqual(log.head, GENESIS_HASH)

    def test_entries_link_to_their_predecessor(self):
        log = AuditLog()
        first = log.append("a", "actor")
        second = log.append("b", "actor")
        self.assertEqual(first.previous_hash, GENESIS_HASH)
        self.assertEqual(second.previous_hash, first.entry_hash)
        self.assertTrue(log.verify())

    def test_head_tracks_the_last_entry(self):
        log = AuditLog()
        log.append("a", "actor")
        last = log.append("b", "actor")
        self.assertEqual(log.head, last.entry_hash)

    def test_altered_content_breaks_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            log = AuditLog(path)
            log.append("approval:sign_off", "a", role="privacy_reviewer")
            log.append("state:draft->approved", "a")

            lines = path.read_text().splitlines()
            lines[0] = lines[0].replace("privacy_reviewer", "program_owner")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = AuditLog(path).verify()
            self.assertFalse(result.valid)
            self.assertEqual(result.first_invalid_sequence, 0)
            self.assertIn("altered", result.message)

    def test_deleted_entry_breaks_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            log = AuditLog(path)
            for i in range(4):
                log.append(f"action-{i}", "a")

            lines = path.read_text().splitlines()
            del lines[1]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = AuditLog(path).verify()
            self.assertFalse(result.valid)

    def test_reordered_entries_break_the_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            log = AuditLog(path)
            for i in range(3):
                log.append(f"action-{i}", "a")

            lines = path.read_text().splitlines()
            lines[0], lines[1] = lines[1], lines[0]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            self.assertFalse(AuditLog(path).verify().valid)

    def test_appended_forgery_without_the_chain_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            log = AuditLog(path)
            log.append("real", "a")

            forged = {
                "sequence": 1,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "action": "state:draft->approved",
                "actor": "attacker",
                "detail": {},
                "previous_hash": "0" * 64,
                "entry_hash": "f" * 64,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(forged) + "\n")

            result = AuditLog(path).verify()
            self.assertFalse(result.valid)
            self.assertEqual(result.first_invalid_sequence, 1)

    def test_chain_survives_reload_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            first = AuditLog(path)
            first.append("a", "actor")
            second = AuditLog(path)
            second.append("b", "actor")
            self.assertTrue(AuditLog(path).verify())
            self.assertEqual(len(AuditLog(path)), 2)

    def test_injected_clock_makes_the_trail_reproducible(self):
        # This is what lets the committed worked example be byte-for-byte
        # reproducible, and therefore what lets CI detect drift between the
        # code and the evidence checked in beside it.
        from datetime import datetime, timedelta, timezone

        def fixed():
            moment = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)

            def tick():
                nonlocal moment
                moment += timedelta(minutes=5)
                return moment

            return tick

        def build():
            log = AuditLog(clock=fixed())
            log.append("state:draft->approved", "a.rivera")
            log.append("state:approved->running", "m.chen")
            return [e.to_dict() for e in log]

        self.assertEqual(build(), build())
        self.assertEqual(build()[0]["timestamp"], "2026-03-02T09:05:00+00:00")

    def test_default_clock_is_wall_time(self):
        from datetime import datetime, timezone

        before = datetime.now(timezone.utc)
        log = AuditLog()
        entry = log.append("state:draft->approved", "a")
        self.assertGreaterEqual(datetime.fromisoformat(entry.timestamp), before)

    def test_markdown_includes_verification_status(self):
        log = AuditLog()
        log.append("state:draft->approved", "a.rivera", reason="ready")
        markdown = log.to_markdown()
        self.assertIn("a.rivera", markdown)
        self.assertIn("intact", markdown)


class TestLifecycleAuditIntegration(unittest.TestCase):
    def test_every_transition_is_recorded(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        lifecycle.start("a")
        lifecycle.pause("a", reason="outage")
        lifecycle.resume("a")
        lifecycle.complete("a")

        actions = [e.action for e in lifecycle.audit]
        self.assertEqual(actions.count("approval:sign_off"), len(REQUIRED_APPROVALS))
        self.assertIn("state:running->paused", actions)
        self.assertIn("state:paused->running", actions)
        self.assertIn("state:running->completed", actions)
        self.assertTrue(lifecycle.audit.verify())

    def test_pause_reason_is_preserved_in_the_record(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        lifecycle.start("a")
        lifecycle.pause("m.chen", reason="upload service outage")
        self.assertEqual(lifecycle.audit.entries[-1].detail["reason"], "upload service outage")

    def test_status_summary_reports_chain_health(self):
        lifecycle = fully_signed(ExperimentLifecycle("exp-1"))
        lifecycle.approve("a")
        summary = lifecycle.status_summary()
        self.assertEqual(summary["state"], "approved")
        self.assertTrue(summary["audit_chain_valid"])
        self.assertEqual(summary["approvals_missing"], [])

    def test_failed_transitions_are_not_recorded(self):
        lifecycle = ExperimentLifecycle("exp-1")
        before = len(lifecycle.audit)
        with self.assertRaises(LifecycleError):
            lifecycle.start("a")
        self.assertEqual(len(lifecycle.audit), before)


if __name__ == "__main__":
    unittest.main()

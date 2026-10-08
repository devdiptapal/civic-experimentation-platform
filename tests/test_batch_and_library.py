"""Tests for batch mode (spreadsheet exports), accounts, and the evidence library."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from civicexp.auth import (
    ApiKeyStore,
    AuthError,
    LoginThrottle,
    SessionStore,
    UserStore,
    hash_password,
    verify_password,
)
from civicexp.collect import Collector
from civicexp.config import load_config
from civicexp.csvio import (
    assign_batch,
    coerce_value,
    import_outcomes,
    parse_event_map,
    profile_columns,
    read_csv_text,
    resolve_timezone,
    write_csv_text,
)
from civicexp.errors import CivicExpError
from civicexp.library import CASE_SCHEMA, EvidenceLibrary, LibraryError, validate_case_record
from civicexp.wizard import build_config
from tests.helpers import ANSWERS, FAST, PASSWORD

PEOPLE_CSV = """﻿case_ref,renewal_due,contact_method_on_file,opted_out_of_messages,preferred_language,channel
R-0001,TRUE,TRUE,FALSE,es,sms
R-0002,TRUE,TRUE,FALSE,en,email
R-0003,TRUE,FALSE,FALSE,en,sms
R-0004,TRUE,TRUE,TRUE,en,sms
R-0005,TRUE,TRUE,FALSE,zh,mail
"""


class BatchCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        raw, _ = build_config("renewal-reminder", dict(ANSWERS, name="Renewal test"))
        self.config = load_config(raw)
        self.events = Path(self._tmp.name) / "events.jsonl"
        self.collector = Collector(self.config, self.events)


class TestAssignBatch(BatchCase):
    def test_produces_a_vendor_list_and_enrolls_eligible_people(self):
        _, rows = read_csv_text(PEOPLE_CSV)
        report, out = assign_batch(self.collector, rows, id_column="case_ref")
        self.assertEqual(report.rows, 5)
        # R-0003 has no contact method; R-0004 opted out.
        self.assertEqual(report.not_in_evaluation, 2)
        self.assertEqual(sum(report.newly_enrolled.values()), 3)
        by_id = {r["case_ref"]: r for r in out}
        self.assertEqual(by_id["R-0004"]["version"], "control")
        self.assertEqual(by_id["R-0004"]["in_evaluation"], "no")
        # The identifier never reaches the event log.
        self.assertNotIn("R-0001", self.events.read_text())
        # Only declared equity attributes are kept from the spreadsheet.
        payloads = [json.loads(line)["payload"] for line in self.events.read_text().splitlines()]
        self.assertTrue(all(set(p) <= {"preferred_language", "channel"} for p in payloads))

    def test_running_twice_does_not_enroll_twice(self):
        _, rows = read_csv_text(PEOPLE_CSV)
        assign_batch(self.collector, rows, id_column="case_ref")
        report, _ = assign_batch(self.collector, rows, id_column="case_ref")
        self.assertEqual(report.already_enrolled, 3)
        self.assertEqual(len(self.events.read_text().splitlines()), 3)

    def test_vendor_list_round_trips_as_csv(self):
        _, rows = read_csv_text(PEOPLE_CSV)
        _, out = assign_batch(self.collector, rows, id_column="case_ref")
        fields, back = read_csv_text(write_csv_text(["case_ref", "version", "in_evaluation"], out))
        self.assertEqual(fields, ["case_ref", "version", "in_evaluation"])
        self.assertEqual(len(back), 5)


class TestImportOutcomes(BatchCase):
    def setUp(self):
        super().setUp()
        _, rows = read_csv_text(PEOPLE_CSV)
        assign_batch(self.collector, rows, id_column="case_ref")

    def test_maps_labels_localizes_times_and_skips_unrelated_rows(self):
        outcomes = (
            "case_ref,what,when\n"
            "R-0001,Renewal received,2026-03-10 14:00:00\n"
            "R-0002,Called helpline,2026-03-11T09:15:00\n"
            "R-0002,Address changed,2026-03-11T09:20:00\n"
            "R-0003,Renewal received,2026-03-12T10:00:00\n"
        )
        _, rows = read_csv_text(outcomes)
        report = import_outcomes(
            self.collector, rows, id_column="case_ref", event_column="what",
            timestamp_column="when",
            event_map=parse_event_map(
                "Renewal received=submission_succeeded,Called helpline=support_contacted"
            ),
            zone=resolve_timezone("-08:00"),
        )
        self.assertEqual(report.accepted, 2)
        self.assertEqual(report.unmapped, {"Address changed": 1})
        # R-0003 was never enrolled (no contact method on file).
        self.assertEqual(report.rejected_by_code(), {"EVENT_UNIT_NOT_ENROLLED": 1})
        last = json.loads(self.events.read_text().splitlines()[-1])
        self.assertEqual(last["timestamp"], "2026-03-11T17:15:00+00:00")

    def test_a_dry_run_checks_everything_and_writes_nothing(self):
        before = self.events.read_text()
        _, rows = read_csv_text("case_ref,what,when\nR-0001,submission_succeeded,2026-03-10T14:00:00Z\n")
        report = import_outcomes(self.collector, rows, id_column="case_ref",
                                 event_column="what", timestamp_column="when", dry_run=True)
        self.assertEqual(report.accepted, 1)
        self.assertEqual(self.events.read_text(), before)

    def test_local_times_without_a_zone_are_refused(self):
        _, rows = read_csv_text("case_ref,what,when\nR-0001,submission_succeeded,2026-03-10 14:00\n")
        report = import_outcomes(self.collector, rows, id_column="case_ref",
                                 event_column="what", timestamp_column="when")
        self.assertEqual(report.rejected_by_code(), {"EVENT_NAIVE_TIMESTAMP": 1})

    def test_named_zones_know_about_daylight_saving(self):
        try:
            zone = resolve_timezone("America/Los_Angeles")
        except CivicExpError:
            self.skipTest("no time zone database on this system")
        _, rows = read_csv_text(
            "case_ref,what,when\n"
            "R-0001,support_contacted,2026-03-07T12:00:00\n"   # before the change: -08:00
            "R-0002,support_contacted,2026-03-09T12:00:00\n"   # after the change: -07:00
        )
        import_outcomes(self.collector, rows, id_column="case_ref", event_column="what",
                        timestamp_column="when", zone=zone)
        stamps = [json.loads(x)["timestamp"] for x in self.events.read_text().splitlines()[-2:]]
        self.assertEqual(stamps, ["2026-03-07T20:00:00+00:00", "2026-03-09T19:00:00+00:00"])


class TestSpreadsheetHelpers(unittest.TestCase):
    def test_cells_become_the_values_rules_compare(self):
        self.assertIs(coerce_value("TRUE"), True)
        self.assertIs(coerce_value("no"), False)
        self.assertEqual(coerce_value("42"), 42)
        self.assertEqual(coerce_value("2.5"), 2.5)
        self.assertIsNone(coerce_value("  "))
        # Leading zeros mean a code, not a number.
        self.assertEqual(coerce_value("0042"), "0042")

    def test_bad_event_maps_and_zones_are_explained(self):
        with self.assertRaises(CivicExpError):
            parse_event_map("Renewed=renewed")
        with self.assertRaises(CivicExpError):
            resolve_timezone("Mars/Olympus_Mons")

    def test_profile_never_shows_values_from_identifier_columns(self):
        rows = [{"case_number": f"C{i:05d}", "lang": "en" if i % 2 else "es",
                 "when": "2026-03-01T10:00:00"} for i in range(60)]
        profiles = {p["column"]: p for p in profile_columns(["case_number", "lang", "when"], rows)}
        self.assertEqual(profiles["case_number"]["kind"], "identifier")
        self.assertEqual(profiles["case_number"]["values"], [])
        self.assertEqual(profiles["lang"]["values"], ["en", "es"])
        self.assertEqual(profiles["when"]["kind"], "timestamp")


class TestAccounts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.users = UserStore(Path(self._tmp.name) / "users.json", iterations=FAST)

    def test_password_hashes_verify_and_reject(self):
        encoded = hash_password("a long enough phrase", iterations=FAST)
        self.assertTrue(verify_password("a long enough phrase", encoded))
        self.assertFalse(verify_password("a long enough phrasE", encoded))
        self.assertFalse(verify_password("x", "garbage"))

    def test_weak_passwords_and_bad_names_are_refused(self):
        with self.assertRaises(AuthError):
            self.users.add("dana", password="short", roles=["viewer"])
        with self.assertRaises(AuthError):
            self.users.add("dana", password="dana-is-my-password", roles=["viewer"])
        with self.assertRaises(AuthError):
            self.users.add("Not Valid!", password=PASSWORD, roles=["viewer"])
        with self.assertRaises(AuthError):
            self.users.add("dana", password=PASSWORD, roles=["supreme_leader"])

    def test_authenticate(self):
        self.users.add("dana", password=PASSWORD, roles=["viewer"])
        self.assertEqual(self.users.authenticate("DANA", PASSWORD).username, "dana")
        self.assertIsNone(self.users.authenticate("dana", "wrong password here"))
        self.assertIsNone(self.users.authenticate("nobody", PASSWORD))
        self.users.set_disabled("dana", True)
        self.assertIsNone(self.users.authenticate("dana", PASSWORD))

    def test_admin_holds_no_approval_authority(self):
        admin = self.users.add("it", password=PASSWORD, roles=["admin"])
        self.assertTrue(admin.can("workspace.admin"))
        for permission in ("lifecycle.approve", "lifecycle.start", "lifecycle.complete",
                           "lifecycle.look", "experiment.create"):
            self.assertFalse(admin.can(permission), permission)
        self.assertTrue(admin.can("lifecycle.pause"))

    def test_keys_are_shown_once_stored_as_digests_and_scoped(self):
        keys = ApiKeyStore(Path(self._tmp.name) / "keys.json")
        key, token = keys.create("upload page", scopes=["collect"], experiments=["exp-1"])
        self.assertNotIn(token.split("_", 2)[2], (Path(self._tmp.name) / "keys.json").read_text())
        found = keys.verify(token)
        self.assertEqual(found.key_id, key.key_id)
        self.assertTrue(found.allows("collect", "exp-1"))
        self.assertFalse(found.allows("collect", "exp-2"))
        self.assertFalse(found.allows("read", "exp-1"))
        self.assertIsNone(keys.verify(token[:-1] + ("A" if token[-1] != "A" else "B")))
        keys.revoke(key.key_id)
        self.assertIsNone(keys.verify(token))

    def test_sessions_expire_and_can_be_ended_per_user(self):
        now = [1000.0]
        sessions = SessionStore(ttl_seconds=60, clock=lambda: now[0])
        one = sessions.create("dana")
        two = sessions.create("dana")
        self.assertIsNotNone(sessions.get(one.session_id))
        sessions.destroy_user("dana")
        self.assertIsNone(sessions.get(two.session_id))
        three = sessions.create("dana")
        now[0] += 61
        self.assertIsNone(sessions.get(three.session_id))

    def test_throttle_locks_out_after_repeated_failures(self):
        now = [time.time()]
        throttle = LoginThrottle(max_failures=3, window_seconds=60, clock=lambda: now[0])
        for _ in range(3):
            throttle.failure("user:dana", "ip:1")
        self.assertGreater(throttle.retry_after("user:dana"), 0)
        self.assertEqual(throttle.retry_after("user:other"), 0)
        now[0] += 61
        self.assertEqual(throttle.retry_after("user:dana"), 0)


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.library = EvidenceLibrary(Path(self._tmp.name) / "library")

    def record(self, **changes):
        base = {
            "schema": CASE_SCHEMA, "case_id": "example-county--exp-1",
            "title": "Upload checklist", "decision": "promote",
            "published": "2026-04-01", "synthetic": True,
            "workflow": "document-upload", "service_area": "benefits",
            "primary": {"metric": "completion_rate"},
        }
        base.update(changes)
        return base

    def test_validation_lists_every_problem(self):
        problems = validate_case_record({"schema": "other", "decision": "win"})
        self.assertGreaterEqual(len(problems), 4)
        self.assertEqual(validate_case_record(self.record()), [])

    def test_imports_search_and_refuses_duplicates(self):
        self.library.import_json(json.dumps(self.record()))
        self.library.add(self.record(case_id="other--exp-2", decision="rollback",
                                     title="Renewal reminder timing",
                                     workflow="renewal-reminder", synthetic=False))
        with self.assertRaises(LibraryError):
            self.library.add(self.record())
        self.assertEqual(len(self.library.search("checklist")), 1)
        self.assertEqual(len(self.library.search(decision="rollback")), 1)
        self.assertEqual(len(self.library.search(include_synthetic=False)), 1)
        self.assertEqual(len(self.library.evidence_for(workflow="document-upload")), 1)

    def test_unsafe_ids_cannot_escape_the_library_folder(self):
        with self.assertRaises(LibraryError):
            self.library.add(self.record(case_id="../../etc/passwd"))
        with self.assertRaises(LibraryError):
            self.library.get("../secrets")

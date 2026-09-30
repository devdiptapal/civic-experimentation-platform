"""Tests for outcome recording."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from civicexp.errors import EventError, PrivacyViolation
from civicexp.events import EVENT_TYPES, EventLog, parse_timestamp
from civicexp.privacy import pseudonymize

PSEUDO_A = pseudonymize("unit-a", "salt")
PSEUDO_B = pseudonymize("unit-b", "salt")
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def make_log(**kwargs) -> EventLog:
    params = {"allowed_variants": ["control", "treatment"]}
    params.update(kwargs)
    return EventLog("exp-1", **params)


class TestTimestamps(unittest.TestCase):
    def test_parses_offset_timestamps(self):
        parsed = parse_timestamp("2026-03-14T09:30:00-07:00")
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.hour, 16)

    def test_rejects_naive_timestamps(self):
        # A pilot spanning a DST change would otherwise produce durations
        # that are silently wrong by an hour.
        with self.assertRaises(EventError) as ctx:
            parse_timestamp("2026-03-14T09:30:00")
        self.assertEqual(ctx.exception.code, "EVENT_NAIVE_TIMESTAMP")

    def test_rejects_unparseable_timestamps(self):
        with self.assertRaises(EventError):
            parse_timestamp("last Tuesday")


class TestValidation(unittest.TestCase):
    def test_records_a_valid_event(self):
        log = make_log()
        event = log.record("step_viewed", PSEUDO_A, "control", timestamp=T0, step="upload")
        self.assertEqual(len(log), 1)
        self.assertEqual(event.payload["step"], "upload")

    def test_rejects_unknown_event_type(self):
        with self.assertRaises(EventError) as ctx:
            make_log().record("user_rage_quit", PSEUDO_A, "control")
        self.assertEqual(ctx.exception.code, "EVENT_UNKNOWN_TYPE")
        self.assertIn("governance change", ctx.exception.message)

    def test_rejects_raw_identifier_in_place_of_pseudonym(self):
        # The single most important check here: a raw case number must never
        # reach the event log, even by mistake.
        with self.assertRaises(EventError) as ctx:
            make_log().record("step_viewed", "case-number-88213", "control")
        self.assertEqual(ctx.exception.code, "EVENT_RAW_IDENTIFIER")

    def test_rejects_unknown_variant(self):
        with self.assertRaises(EventError) as ctx:
            make_log().record("step_viewed", PSEUDO_A, "variant_z")
        self.assertEqual(ctx.exception.code, "EVENT_UNKNOWN_VARIANT")

    def test_rejects_pii_in_payload(self):
        with self.assertRaises(PrivacyViolation):
            make_log().record("step_viewed", PSEUDO_A, "control", applicant_email="a@b.c")

    def test_rejects_payload_overriding_platform_fields(self):
        # Passed through the explicit mapping, because `variant=` would
        # collide with the method's own parameter.
        with self.assertRaises(EventError) as ctx:
            make_log().record(
                "step_viewed", PSEUDO_A, "control", payload={"variant": "other"}
            )
        self.assertEqual(ctx.exception.code, "EVENT_RESERVED_KEY")

    def test_rejects_a_field_given_twice(self):
        with self.assertRaises(EventError) as ctx:
            make_log().record(
                "step_viewed", PSEUDO_A, "control", payload={"step": "a"}, step="b"
            )
        self.assertEqual(ctx.exception.code, "EVENT_DUPLICATE_FIELD")

    def test_payload_mapping_and_keywords_both_work(self):
        log = make_log()
        event = log.record(
            "step_viewed", PSEUDO_A, "control", timestamp=T0,
            payload={"step": "upload"}, attempt=2,
        )
        self.assertEqual(event.payload, {"step": "upload", "attempt": 2})

    def test_rejects_event_for_a_different_experiment(self):
        log = make_log()
        from civicexp.events import Event

        other = Event("exp-2", "step_viewed", PSEUDO_A, "control", T0)
        with self.assertRaises(EventError) as ctx:
            log.validate(other)
        self.assertEqual(ctx.exception.code, "EVENT_WRONG_EXPERIMENT")

    def test_every_declared_event_type_is_accepted(self):
        log = make_log()
        for event_type in EVENT_TYPES:
            log.record(event_type, PSEUDO_A, "control", timestamp=T0)
        self.assertEqual(len(log), len(EVENT_TYPES))


class TestAggregation(unittest.TestCase):
    def setUp(self):
        self.log = make_log()
        # unit A: assigned, succeeded (after three failed attempts)
        self.log.record("experiment_assigned", PSEUDO_A, "control", timestamp=T0)
        for i in range(3):
            self.log.record(
                "submission_failed", PSEUDO_A, "control",
                timestamp=T0 + timedelta(seconds=10 * i + 10),
            )
        self.log.record(
            "submission_succeeded", PSEUDO_A, "control", timestamp=T0 + timedelta(seconds=100)
        )
        # unit B: assigned, never succeeded
        self.log.record("experiment_assigned", PSEUDO_B, "control", timestamp=T0)

    def test_counts_distinct_units_not_raw_events(self):
        # Unit A failed three times. That is one applicant having a bad
        # experience, not three, and counting events would triple-count it.
        counts = self.log.count_units_with_event("submission_failed")
        self.assertEqual(counts["control"], 1)

    def test_denominator_counts_assigned_units(self):
        self.assertEqual(self.log.count_units_with_event("experiment_assigned")["control"], 2)

    def test_units_by_variant(self):
        self.assertEqual(len(self.log.units_by_variant()["control"]), 2)

    def test_durations_only_include_completed_units(self):
        durations = self.log.durations_by_variant("experiment_assigned", "submission_succeeded")
        # Unit B never completed, so it contributes no duration. Imputing one
        # would bias the metric toward whichever arm abandons more.
        self.assertEqual(durations["control"], [100.0])

    def test_missing_event_type_yields_no_counts(self):
        self.assertEqual(self.log.count_units_with_event("support_contacted"), {})


class TestPersistence(unittest.TestCase):
    def test_round_trips_through_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            first = make_log(path=path)
            first.record("experiment_assigned", PSEUDO_A, "control", timestamp=T0)
            first.record("submission_succeeded", PSEUDO_A, "control", timestamp=T0)

            reopened = make_log(path=path)
            self.assertEqual(len(reopened), 2)
            self.assertEqual(reopened.events[0].event_type, "experiment_assigned")

    def test_appends_rather_than_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            make_log(path=path).record("step_viewed", PSEUDO_A, "control", timestamp=T0)
            make_log(path=path).record("step_viewed", PSEUDO_B, "control", timestamp=T0)
            self.assertEqual(len(make_log(path=path)), 2)

    def test_file_is_valid_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            log = make_log(path=path)
            log.record("step_viewed", PSEUDO_A, "control", timestamp=T0, step="upload")
            for line in path.read_text().splitlines():
                parsed = json.loads(line)
                self.assertIn("event_type", parsed)
                self.assertIn("timestamp", parsed)

    def test_corrupt_line_is_reported_with_its_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text('{"not": "an event"\n', encoding="utf-8")
            with self.assertRaises(EventError) as ctx:
                make_log(path=path)
            self.assertEqual(ctx.exception.code, "EVENT_CORRUPT_LINE")

    def test_stored_events_are_revalidated_on_read(self):
        # A file edited by hand to insert a raw identifier must be caught.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "experiment_id": "exp-1",
                        "event_type": "step_viewed",
                        "unit_pseudonym": "case-88213",
                        "variant": "control",
                        "timestamp": T0.isoformat(),
                        "payload": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(EventError) as ctx:
                make_log(path=path)
            self.assertEqual(ctx.exception.code, "EVENT_RAW_IDENTIFIER")


if __name__ == "__main__":
    unittest.main()

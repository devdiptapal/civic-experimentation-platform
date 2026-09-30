"""Tests for the privacy controls.

These are the tests that matter most for public trust: they assert that the
commitments written in PRIVACY.md are enforced by the code, not merely
described by it.
"""

from __future__ import annotations

import unittest

from civicexp.errors import PrivacyViolation
from civicexp.privacy import DEFAULT_SUPPRESSION_THRESHOLD, PrivacyPolicy


class TestFieldScreening(unittest.TestCase):
    def setUp(self):
        self.policy = PrivacyPolicy()

    def test_rejects_direct_identifiers(self):
        for field in (
            "ssn",
            "social_security_number",
            "dob",
            "date_of_birth",
            "email",
            "email_address",
            "phone_number",
            "home_address",
            "street_address",
            "zipcode",
            "zip_code",
            "case_number",
            "client_id",
            "medicaid_id",
            "applicant_id",
            "ip_address",
            "device_id",
            "first_name",
            "last_name",
            "passport_number",
            "alien_number",
        ):
            with self.subTest(field=field), self.assertRaises(PrivacyViolation):
                self.policy.check_field_names([field])

    def test_screening_is_case_insensitive(self):
        with self.assertRaises(PrivacyViolation):
            self.policy.check_field_names(["SSN"])
        with self.assertRaises(PrivacyViolation):
            self.policy.check_field_names(["Email_Address"])

    def test_allows_non_identifying_operational_fields(self):
        self.policy.check_field_names(
            [
                "channel",
                "step",
                "application_type",
                "preferred_language",
                "device_type",
                "region",
                "failure_kind",
                "attempt_number",
                "staff_assisted",
            ]
        )

    def test_allowlisted_field_is_permitted(self):
        policy = PrivacyPolicy(approved_pii_fields=frozenset({"case_number"}))
        policy.check_field_names(["case_number"])
        with self.assertRaises(PrivacyViolation):
            policy.check_field_names(["case_number", "ssn"])

    def test_error_names_the_offending_field_and_the_remedy(self):
        with self.assertRaises(PrivacyViolation) as ctx:
            self.policy.check_field_names(["applicant_email"])
        message = ctx.exception.message
        self.assertIn("applicant_email", message)
        self.assertIn("approved_pii_fields", message)

    def test_all_offending_fields_are_reported_together(self):
        with self.assertRaises(PrivacyViolation) as ctx:
            self.policy.check_field_names(["ssn", "channel", "phone"])
        self.assertIn("ssn", ctx.exception.message)
        self.assertIn("phone", ctx.exception.message)


class TestPayloadScreening(unittest.TestCase):
    def setUp(self):
        self.policy = PrivacyPolicy()

    def test_accepts_a_flat_coded_payload(self):
        self.policy.check_payload({"step": "upload", "attempt": 2, "ok": True})

    def test_rejects_long_free_text(self):
        # Free text cannot be screened by field name, so it is refused outright.
        with self.assertRaises(PrivacyViolation) as ctx:
            self.policy.check_payload({"reason": "x" * 200})
        self.assertEqual(ctx.exception.code, "PRIVACY_FREE_TEXT")

    def test_accepts_short_coded_strings(self):
        self.policy.check_payload({"failure_kind": "blocking_error"})

    def test_rejects_nested_structures(self):
        for value in ({"a": 1}, [1, 2, 3], (1, 2)):
            with self.subTest(value=value):
                with self.assertRaises(PrivacyViolation) as ctx:
                    self.policy.check_payload({"detail": value})
                self.assertEqual(ctx.exception.code, "PRIVACY_NESTED")


class TestSuppression(unittest.TestCase):
    def test_default_threshold_is_eleven(self):
        self.assertEqual(DEFAULT_SUPPRESSION_THRESHOLD, 11)

    def test_small_cells_are_suppressed(self):
        policy = PrivacyPolicy()
        self.assertTrue(policy.suppress(1))
        self.assertTrue(policy.suppress(10))
        self.assertFalse(policy.suppress(11))
        self.assertFalse(policy.suppress(500))

    def test_empty_cells_are_not_suppressed(self):
        # A true zero discloses nothing about any individual, and hiding it
        # would make reports harder to read for no privacy gain.
        self.assertFalse(PrivacyPolicy().suppress(0))

    def test_apply_suppression_replaces_the_value(self):
        policy = PrivacyPolicy()
        self.assertEqual(policy.apply_suppression(0.42, 500), 0.42)
        self.assertIn("suppressed", str(policy.apply_suppression(0.42, 3)))

    def test_threshold_is_configurable(self):
        policy = PrivacyPolicy(suppression_threshold=25)
        self.assertTrue(policy.suppress(20))
        self.assertFalse(policy.suppress(25))


if __name__ == "__main__":
    unittest.main()

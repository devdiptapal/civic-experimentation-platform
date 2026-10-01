"""Tests for assignment, eligibility, and pseudonymization."""

from __future__ import annotations

import math
import unittest

from civicexp.assignment import NOT_ELIGIBLE, NOT_ENROLLED, Assigner, bucket_of
from civicexp.eligibility import EligibilityCriteria, Rule
from civicexp.errors import AssignmentError, ConfigError, PrivacyViolation
from civicexp.privacy import pseudonymize


def make_assigner(**kwargs) -> Assigner:
    params = {"salt": "test-salt-0123456789", "variants": {"control": 0.5, "treatment": 0.5}}
    params.update(kwargs)
    return Assigner(**params)


class TestBucketing(unittest.TestCase):
    def test_bucket_is_in_unit_interval(self):
        for i in range(1000):
            self.assertTrue(0.0 <= bucket_of(f"unit-{i}", "salt-value") < 1.0)

    def test_bucket_is_deterministic(self):
        self.assertEqual(bucket_of("unit-1", "s"), bucket_of("unit-1", "s"))

    def test_different_salts_give_different_buckets(self):
        self.assertNotEqual(bucket_of("unit-1", "salt-a"), bucket_of("unit-1", "salt-b"))

    def test_different_purposes_give_independent_draws(self):
        # Enrollment and arm assignment must not be correlated, or ramping
        # the enrolled fraction would reshuffle the arms.
        self.assertNotEqual(
            bucket_of("unit-1", "s", purpose="assign"),
            bucket_of("unit-1", "s", purpose="enroll"),
        )

    def test_empty_inputs_are_rejected(self):
        with self.assertRaises(AssignmentError):
            bucket_of("", "salt")
        with self.assertRaises(AssignmentError):
            bucket_of("unit", "")


class TestAssignmentDistribution(unittest.TestCase):
    def test_even_split_is_uniform(self):
        assigner = make_assigner()
        counts = assigner.balance_report([f"u-{i}" for i in range(100_000)])
        expected = 50_000
        chi_square = sum(
            (counts[name] - expected) ** 2 / expected for name in ("control", "treatment")
        )
        # 1 degree of freedom, critical value 10.83 at p=0.001.
        self.assertLess(chi_square, 10.83, f"split looks non-uniform: {counts}")

    def test_uneven_split_respects_configured_shares(self):
        assigner = make_assigner(variants={"control": 0.9, "treatment": 0.1})
        counts = assigner.balance_report([f"u-{i}" for i in range(50_000)])
        self.assertAlmostEqual(counts["treatment"] / 50_000, 0.10, delta=0.01)

    def test_three_arms_each_get_their_share(self):
        assigner = make_assigner(
            variants={"control": 0.34, "arm_b": 0.33, "arm_c": 0.33}
        )
        counts = assigner.balance_report([f"u-{i}" for i in range(60_000)])
        for name, share in (("control", 0.34), ("arm_b", 0.33), ("arm_c", 0.33)):
            self.assertAlmostEqual(counts[name] / 60_000, share, delta=0.01)

    def test_every_unit_lands_in_exactly_one_arm(self):
        assigner = make_assigner()
        counts = assigner.balance_report([f"u-{i}" for i in range(5000)])
        self.assertEqual(sum(counts.values()), 5000)


class TestAssignmentStability(unittest.TestCase):
    def test_same_unit_always_gets_the_same_variant(self):
        assigner = make_assigner()
        first = assigner.assign("applicant-42")
        for _ in range(50):
            self.assertEqual(assigner.assign("applicant-42").variant, first.variant)

    def test_assignment_survives_rebuilding_the_assigner(self):
        # The property that lets an auditor recompute assignments later.
        a = make_assigner().assign("applicant-42").variant
        b = make_assigner().assign("applicant-42").variant
        self.assertEqual(a, b)

    def test_assignment_is_independent_of_variant_declaration_order(self):
        forward = Assigner(salt="s-0123456789abcdef", variants={"control": 0.5, "treatment": 0.5})
        reverse = Assigner(salt="s-0123456789abcdef", variants={"treatment": 0.5, "control": 0.5})
        for i in range(500):
            unit = f"u-{i}"
            self.assertEqual(forward.assign(unit).variant, reverse.assign(unit).variant)

    def test_changing_the_salt_reshuffles_assignments(self):
        a = Assigner(salt="salt-one-0123456", variants={"control": 0.5, "treatment": 0.5})
        b = Assigner(salt="salt-two-0123456", variants={"control": 0.5, "treatment": 0.5})
        moved = sum(
            a.assign(f"u-{i}").variant != b.assign(f"u-{i}").variant for i in range(1000)
        )
        self.assertGreater(moved, 400)


class TestAssignerValidation(unittest.TestCase):
    def test_shares_must_sum_to_one(self):
        with self.assertRaises(AssignmentError) as ctx:
            make_assigner(variants={"control": 0.5, "treatment": 0.4})
        self.assertEqual(ctx.exception.code, "ASSIGNMENT_SHARES_SUM")

    def test_single_variant_is_rejected(self):
        with self.assertRaises(AssignmentError) as ctx:
            make_assigner(variants={"control": 1.0})
        self.assertEqual(ctx.exception.code, "ASSIGNMENT_NO_COMPARISON")

    def test_reserved_variant_names_are_rejected(self):
        with self.assertRaises(AssignmentError) as ctx:
            make_assigner(variants={"control": 0.5, NOT_ELIGIBLE: 0.5})
        self.assertEqual(ctx.exception.code, "ASSIGNMENT_RESERVED_NAME")

    def test_missing_salt_is_rejected(self):
        with self.assertRaises(AssignmentError):
            make_assigner(salt="")

    def test_bad_enrolled_fraction_is_rejected(self):
        for bad in (0.0, -0.1, 1.5):
            with self.assertRaises(AssignmentError):
                make_assigner(enrolled_fraction=bad)


class TestEnrollmentRamp(unittest.TestCase):
    def test_ramp_holds_back_roughly_the_right_share(self):
        assigner = make_assigner(enrolled_fraction=0.20)
        counts = assigner.balance_report([f"u-{i}" for i in range(20_000)])
        self.assertAlmostEqual(counts[NOT_ENROLLED] / 20_000, 0.80, delta=0.02)

    def test_held_back_units_are_excluded_from_analysis(self):
        assigner = make_assigner(enrolled_fraction=0.10)
        held = [a for a in (assigner.assign(f"u-{i}") for i in range(500))
                if a.variant == NOT_ENROLLED]
        self.assertTrue(held)
        for assignment in held:
            self.assertTrue(assignment.eligible)
            self.assertFalse(assignment.enrolled)
            self.assertFalse(assignment.in_analysis)

    def test_widening_the_ramp_only_adds_units(self):
        # Because enrollment and arm assignment are independent draws,
        # increasing the ramp must not move anyone already enrolled.
        narrow = make_assigner(enrolled_fraction=0.20)
        wide = make_assigner(enrolled_fraction=0.60)
        for i in range(2000):
            unit = f"u-{i}"
            before = narrow.assign(unit)
            after = wide.assign(unit)
            if before.enrolled:
                self.assertTrue(after.enrolled)
                self.assertEqual(before.variant, after.variant)


class TestEligibility(unittest.TestCase):
    def test_include_and_exclude_rules(self):
        criteria = EligibilityCriteria(
            include=(Rule("channel", "in", ["web", "mobile"]),),
            exclude=(Rule("staff_assisted", "eq", True),),
        )
        self.assertTrue(criteria.is_eligible({"channel": "web", "staff_assisted": False}))
        self.assertFalse(criteria.is_eligible({"channel": "phone"}))
        self.assertFalse(criteria.is_eligible({"channel": "web", "staff_assisted": True}))

    def test_exclusions_win_over_inclusions(self):
        criteria = EligibilityCriteria(
            include=(Rule("channel", "eq", "web"),),
            exclude=(Rule("channel", "eq", "web"),),
        )
        self.assertFalse(criteria.is_eligible({"channel": "web"}))

    def test_missing_attribute_never_silently_matches(self):
        criteria = EligibilityCriteria(include=(Rule("channel", "eq", "web"),))
        self.assertFalse(criteria.is_eligible({}))

    def test_exists_and_missing_operators(self):
        self.assertTrue(Rule("x", "exists").matches({"x": None}))
        self.assertFalse(Rule("x", "exists").matches({}))
        self.assertTrue(Rule("x", "missing").matches({}))

    def test_numeric_operators(self):
        self.assertTrue(Rule("age_days", "gte", 30).matches({"age_days": 30}))
        self.assertFalse(Rule("age_days", "gt", 30).matches({"age_days": 30}))
        self.assertTrue(Rule("age_days", "lt", 30).matches({"age_days": 29}))

    def test_numeric_operator_on_non_numeric_value_is_an_error(self):
        with self.assertRaises(ConfigError):
            Rule("age_days", "gt", 30).matches({"age_days": "thirty"})

    def test_booleans_are_not_treated_as_numbers(self):
        with self.assertRaises(ConfigError):
            Rule("flag", "gt", 0).matches({"flag": True})

    def test_unknown_operator_is_rejected_at_construction(self):
        with self.assertRaises(ConfigError):
            Rule("x", "regex_match", ".*")

    def test_list_operators_require_a_list(self):
        with self.assertRaises(ConfigError):
            Rule("x", "in", "web")

    def test_describe_works_for_every_operator(self):
        # Regression: describe() once evaluated every branch eagerly and
        # crashed on non-list values for the `in` branch.
        rules = [
            Rule("a", "eq", True),
            Rule("a", "ne", 3),
            Rule("a", "in", ["x", "y"]),
            Rule("a", "not_in", [1]),
            Rule("a", "gt", 1),
            Rule("a", "gte", 1),
            Rule("a", "lt", 1),
            Rule("a", "lte", 1),
            Rule("a", "exists"),
            Rule("a", "missing"),
        ]
        for rule in rules:
            self.assertIsInstance(rule.describe(), str)
            self.assertTrue(rule.describe())

    def test_explain_gives_a_usable_reason(self):
        criteria = EligibilityCriteria(exclude=(Rule("staff_assisted", "eq", True),))
        self.assertIn("staff_assisted", criteria.explain({"staff_assisted": True}))
        self.assertEqual(criteria.explain({"staff_assisted": False}), "eligible")

    def test_round_trips_through_a_dict(self):
        original = Rule("channel", "in", ["web"])
        self.assertEqual(Rule.from_dict(original.to_dict()), original)

    def test_ineligible_units_are_marked_and_not_analyzed(self):
        assigner = make_assigner(
            eligibility=EligibilityCriteria(include=(Rule("channel", "eq", "web"),))
        )
        result = assigner.assign("u-1", {"channel": "phone"})
        self.assertEqual(result.variant, NOT_ELIGIBLE)
        self.assertFalse(result.in_analysis)
        self.assertTrue(math.isnan(result.bucket))


class TestPseudonymization(unittest.TestCase):
    def test_pseudonym_is_stable_and_hex(self):
        first = pseudonymize("applicant-1", "salt")
        self.assertEqual(first, pseudonymize("applicant-1", "salt"))
        self.assertEqual(len(first), 64)
        int(first, 16)  # raises if not hex

    def test_pseudonym_differs_across_salts(self):
        self.assertNotEqual(pseudonymize("a", "salt-1"), pseudonymize("a", "salt-2"))

    def test_pseudonym_does_not_contain_the_identifier(self):
        self.assertNotIn("applicant-1", pseudonymize("applicant-1", "salt"))

    def test_empty_inputs_are_rejected(self):
        with self.assertRaises(PrivacyViolation):
            pseudonymize("", "salt")
        with self.assertRaises(PrivacyViolation):
            pseudonymize("unit", "")

    def test_assignment_exposes_only_the_pseudonym(self):
        result = make_assigner().assign("applicant-secret-12345")
        self.assertNotIn("secret", str(result.to_dict()))


if __name__ == "__main__":
    unittest.main()


class TestDocumentedAlgorithmContract(unittest.TestCase):
    """Lock the algorithm published in docs/Integration-Guide.md.

    That document tells other teams how to reimplement assignment in another
    language. If these tests ever fail, either the code changed and the
    documentation is now wrong, or someone's reimplementation is about to
    silently disagree with this one about who is in which arm.
    """

    SALT = "9f2c4a7e1b8d6350fae24c9071b5d8e3"

    def reference_bucket(self, unit_id: str, purpose: str = "assign") -> float:
        """The algorithm exactly as the integration guide describes it."""
        import hashlib
        import hmac

        digest = hmac.new(
            self.SALT.encode("utf-8"),
            f"{purpose}:{unit_id}".encode(),
            hashlib.sha256,
        ).digest()
        return int.from_bytes(digest[:8], "big") / 2.0**64

    def test_bucket_matches_the_documented_formula(self):
        for i in range(200):
            unit = f"applicant-{i}"
            self.assertEqual(bucket_of(unit, self.SALT), self.reference_bucket(unit))

    def test_enrollment_uses_the_documented_prefix(self):
        for i in range(50):
            unit = f"applicant-{i}"
            self.assertEqual(
                bucket_of(unit, self.SALT, purpose="enroll"),
                self.reference_bucket(unit, "enroll"),
            )

    def test_pseudonym_is_unprefixed_hmac(self):
        import hashlib
        import hmac

        for i in range(50):
            unit = f"applicant-{i}"
            expected = hmac.new(
                self.SALT.encode("utf-8"), unit.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            self.assertEqual(pseudonymize(unit, self.SALT), expected)

    def test_variants_are_walked_in_alphabetical_order(self):
        # The guide tells reimplementers to sort by name. If this changed,
        # every non-Python integration would disagree about the arms.
        assigner = Assigner(
            salt=self.SALT,
            variants={"zebra": 0.5, "alpha": 0.5},
        )
        self.assertEqual([name for name, _ in assigner.variants], ["alpha", "zebra"])
        for i in range(200):
            unit = f"applicant-{i}"
            bucket = self.reference_bucket(unit)
            expected = "alpha" if bucket < 0.5 else "zebra"
            self.assertEqual(assigner.assign(unit).variant, expected)

    def test_known_vectors_are_stable_across_releases(self):
        # Pinned outputs. A change here is a breaking change to every live
        # pilot, because it moves people between arms mid-flight.
        assigner = Assigner(
            salt=self.SALT, variants={"control": 0.5, "treatment": 0.5}
        )
        for unit, expected in (
            ("applicant-1", "treatment"),      # bucket 0.889423
            ("applicant-2", "control"),        # bucket 0.029970
            ("applicant-3", "control"),        # bucket 0.156657
            ("applicant-99213", "treatment"),  # bucket 0.963186
        ):
            with self.subTest(unit=unit):
                self.assertEqual(assigner.assign(unit).variant, expected)

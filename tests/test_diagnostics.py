"""Tests for the trust checks."""

from __future__ import annotations

import unittest

from civicexp.diagnostics import (
    SRM_ALPHA,
    Severity,
    check_completeness,
    check_novelty,
    check_sample_ratio,
    chi_square_p_value,
)


class TestChiSquare(unittest.TestCase):
    def test_matches_published_critical_values(self):
        # Standard table: the 0.05 critical value at 1, 2, 3 and 10 df.
        for statistic, df in ((3.8415, 1), (5.9915, 2), (7.8147, 3), (18.307, 10)):
            with self.subTest(df=df):
                self.assertAlmostEqual(chi_square_p_value(statistic, df), 0.05, places=4)

    def test_matches_stricter_critical_values(self):
        self.assertAlmostEqual(chi_square_p_value(10.828, 1), 0.001, places=5)
        self.assertAlmostEqual(chi_square_p_value(6.635, 1), 0.01, places=4)
        self.assertAlmostEqual(chi_square_p_value(13.816, 2), 0.001, places=4)

    def test_zero_statistic_is_certain(self):
        self.assertEqual(chi_square_p_value(0.0, 1), 1.0)

    def test_p_value_decreases_as_statistic_grows(self):
        values = [chi_square_p_value(x, 2) for x in (0.5, 2.0, 6.0, 20.0)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_always_a_probability(self):
        for df in (1, 2, 5, 30):
            for statistic in (0.01, 1.0, 50.0, 500.0):
                value = chi_square_p_value(statistic, df)
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            chi_square_p_value(-1.0, 1)
        with self.assertRaises(ValueError):
            chi_square_p_value(1.0, 0)


class TestSampleRatio(unittest.TestCase):
    def test_even_split_passes(self):
        result = check_sample_ratio({"c": 5000, "t": 5000}, {"c": 0.5, "t": 0.5})
        self.assertIs(result.severity, Severity.OK)

    def test_small_imbalance_passes(self):
        # 6101/5899 is ordinary sampling variation, not a fault.
        result = check_sample_ratio({"c": 6101, "t": 5899}, {"c": 0.5, "t": 0.5})
        self.assertIs(result.severity, Severity.OK)

    def test_large_imbalance_is_invalid(self):
        # The signature of a routing or logging bug.
        result = check_sample_ratio({"c": 6800, "t": 5200}, {"c": 0.5, "t": 0.5})
        self.assertIs(result.severity, Severity.INVALID)
        self.assertTrue(result.severity.blocks_decision)
        self.assertIn("rerun", result.detail)

    def test_uneven_configured_split_is_respected(self):
        # 90/10 configured and 90/10 observed is correct, not a mismatch.
        result = check_sample_ratio({"c": 9000, "t": 1000}, {"c": 0.9, "t": 0.1})
        self.assertIs(result.severity, Severity.OK)

    def test_uneven_configured_split_still_detects_a_fault(self):
        result = check_sample_ratio({"c": 5000, "t": 5000}, {"c": 0.9, "t": 0.1})
        self.assertIs(result.severity, Severity.INVALID)

    def test_three_arms(self):
        ok = check_sample_ratio(
            {"a": 3333, "b": 3333, "c": 3334}, {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}
        )
        self.assertIs(ok.severity, Severity.OK)
        broken = check_sample_ratio(
            {"a": 5000, "b": 3000, "c": 2000}, {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}
        )
        self.assertIs(broken.severity, Severity.INVALID)

    def test_no_data_is_invalid(self):
        result = check_sample_ratio({"c": 0, "t": 0}, {"c": 0.5, "t": 0.5})
        self.assertIs(result.severity, Severity.INVALID)

    def test_detail_names_the_gap(self):
        result = check_sample_ratio({"c": 6800, "t": 5200}, {"c": 0.5, "t": 0.5})
        self.assertIn("expected 6,000", result.detail)
        self.assertIn("observed 6,800", result.detail)

    def test_threshold_is_strict_to_avoid_crying_wolf(self):
        # An SRM check runs on every analysis, so a 0.05 threshold would
        # declare one healthy pilot in twenty broken.
        self.assertEqual(SRM_ALPHA, 0.001)

    def test_borderline_imbalance_is_only_a_note(self):
        found = None
        for treatment in range(5700, 5900):
            diagnostic = check_sample_ratio(
                {"c": 12000 - treatment, "t": treatment}, {"c": 0.5, "t": 0.5}
            )
            if diagnostic.severity is Severity.NOTE:
                found = diagnostic
                break
        self.assertIsNotNone(found, "expected some split to land in the NOTE band")
        self.assertFalse(found.severity.blocks_decision)


class TestNovelty(unittest.TestCase):
    def test_stable_effect_passes(self):
        half = (600, 1000, 700, 1000)
        result = check_novelty(half, half)
        self.assertIs(result.severity, Severity.OK)

    def test_fading_effect_warns(self):
        early = (600, 1000, 780, 1000)  # +18 points
        late = (600, 1000, 610, 1000)  # +1 point
        result = check_novelty(early, late)
        self.assertIs(result.severity, Severity.WARNING)
        self.assertIn("faded", result.summary)
        self.assertIn("novelty", result.detail)

    def test_growing_effect_is_a_note_not_a_warning(self):
        early = (600, 1000, 610, 1000)
        late = (600, 1000, 780, 1000)
        result = check_novelty(early, late)
        self.assertIs(result.severity, Severity.NOTE)
        self.assertIn("grew", result.summary)
        self.assertIn("primacy", result.detail)

    def test_too_small_to_judge(self):
        result = check_novelty((5, 10, 6, 10), (5, 10, 6, 10))
        self.assertIs(result.severity, Severity.NOTE)
        self.assertIn("Too few", result.summary)

    def test_never_blocks_a_decision(self):
        # A fading effect is a reason to think, not a reason to discard data.
        result = check_novelty((600, 1000, 780, 1000), (600, 1000, 610, 1000))
        self.assertFalse(result.severity.blocks_decision)


class TestCompleteness(unittest.TestCase):
    def test_all_present_passes(self):
        counts = {
            "experiment_assigned": {"c": 100, "t": 100},
            "submission_succeeded": {"c": 60, "t": 70},
        }
        result = check_completeness(
            counts, ["experiment_assigned", "submission_succeeded"], ["c", "t"]
        )
        self.assertIs(result.severity, Severity.OK)

    def test_event_missing_everywhere_warns(self):
        counts = {"experiment_assigned": {"c": 100, "t": 100}}
        result = check_completeness(
            counts, ["experiment_assigned", "support_contacted"], ["c", "t"]
        )
        self.assertIs(result.severity, Severity.WARNING)
        self.assertIn("support_contacted", result.detail)

    def test_one_sided_event_warns(self):
        counts = {
            "experiment_assigned": {"c": 100, "t": 100},
            "submission_failed": {"c": 5, "t": 0},
        }
        result = check_completeness(
            counts, ["experiment_assigned", "submission_failed"], ["c", "t"]
        )
        self.assertIs(result.severity, Severity.WARNING)
        self.assertIn("none in t", result.detail)

    def test_explains_that_missing_is_not_zero(self):
        result = check_completeness({}, ["submission_failed"], ["c", "t"])
        self.assertIn("not zero, it is unknown", result.detail)


if __name__ == "__main__":
    unittest.main()

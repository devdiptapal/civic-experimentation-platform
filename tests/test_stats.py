"""Tests for the statistical functions.

Where a published reference value exists, the test asserts against that
value rather than against this implementation's own output. A statistics
library that only agrees with itself is not verified.
"""

from __future__ import annotations

import unittest

from civicexp.errors import AnalysisError
from civicexp.stats import (
    compare_means,
    compare_proportions,
    corrected_alpha,
    holm_bonferroni,
    minimum_detectable_effect,
    normal_cdf,
    normal_quantile,
    required_sample_size_per_group,
    student_t_cdf,
    wilson_interval,
)


class TestDistributions(unittest.TestCase):
    def test_normal_cdf_known_values(self):
        self.assertAlmostEqual(normal_cdf(0.0), 0.5, places=12)
        self.assertAlmostEqual(normal_cdf(1.0), 0.8413447461, places=9)
        self.assertAlmostEqual(normal_cdf(1.959963985), 0.975, places=9)
        self.assertAlmostEqual(normal_cdf(-2.575829304), 0.005, places=9)

    def test_normal_quantile_matches_published_critical_values(self):
        self.assertAlmostEqual(normal_quantile(0.975), 1.959963985, places=8)
        self.assertAlmostEqual(normal_quantile(0.995), 2.575829304, places=8)
        self.assertAlmostEqual(normal_quantile(0.80), 0.8416212336, places=8)
        self.assertAlmostEqual(normal_quantile(0.50), 0.0, places=10)

    def test_normal_quantile_inverts_cdf(self):
        for p in (0.001, 0.05, 0.25, 0.5, 0.75, 0.95, 0.999):
            self.assertAlmostEqual(normal_cdf(normal_quantile(p)), p, places=10)

    def test_normal_quantile_rejects_out_of_domain(self):
        for bad in (0.0, 1.0, -0.5, 1.5):
            with self.assertRaises(AnalysisError):
                normal_quantile(bad)

    def test_student_t_cdf_matches_published_tables(self):
        # t(0.95, df=10) = 1.812461; t(0.975, df=10) = 2.228139
        self.assertAlmostEqual(student_t_cdf(1.812461, 10), 0.95, places=6)
        self.assertAlmostEqual(student_t_cdf(2.228139, 10), 0.975, places=6)
        # t(0.975, df=30) = 2.042272
        self.assertAlmostEqual(student_t_cdf(2.042272, 30), 0.975, places=6)
        self.assertAlmostEqual(student_t_cdf(0.0, 5), 0.5, places=12)

    def test_student_t_is_symmetric(self):
        for t in (0.3, 1.0, 2.5, 4.0):
            self.assertAlmostEqual(
                student_t_cdf(-t, 12), 1.0 - student_t_cdf(t, 12), places=10
            )

    def test_student_t_approaches_normal_at_high_df(self):
        self.assertAlmostEqual(student_t_cdf(1.96, 100000), normal_cdf(1.96), places=4)


class TestWilsonInterval(unittest.TestCase):
    def test_matches_published_value(self):
        # Wilson 95% interval for 5/50 is (0.0435, 0.2136).
        low, high = wilson_interval(5, 50)
        self.assertAlmostEqual(low, 0.0435, places=4)
        self.assertAlmostEqual(high, 0.2136, places=4)

    def test_zero_successes_gives_non_degenerate_interval(self):
        # The reason for preferring Wilson: Wald would return (0, 0) here,
        # implying certainty that the rate is zero.
        low, high = wilson_interval(0, 20)
        self.assertEqual(low, 0.0)
        self.assertGreater(high, 0.0)
        self.assertLess(high, 0.20)

    def test_all_successes_stays_within_bounds(self):
        low, high = wilson_interval(20, 20)
        self.assertLess(low, 1.0)
        self.assertEqual(high, 1.0)

    def test_interval_always_inside_unit_range(self):
        for successes, trials in ((0, 1), (1, 1), (1, 3), (99, 100), (7, 13)):
            low, high = wilson_interval(successes, trials)
            self.assertGreaterEqual(low, 0.0)
            self.assertLessEqual(high, 1.0)
            self.assertLessEqual(low, high)

    def test_no_trials_returns_full_range(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_rejects_impossible_counts(self):
        with self.assertRaises(AnalysisError):
            wilson_interval(10, 5)


class TestCompareProportions(unittest.TestCase):
    def test_detects_a_real_difference(self):
        result = compare_proportions(600, 1000, 700, 1000)
        self.assertAlmostEqual(result.control_rate, 0.60)
        self.assertAlmostEqual(result.treatment_rate, 0.70)
        self.assertAlmostEqual(result.absolute_difference, 0.10)
        self.assertTrue(result.significant)
        self.assertLess(result.p_value, 0.001)
        self.assertEqual(result.direction, "increase")

    def test_identical_rates_are_not_significant(self):
        result = compare_proportions(500, 1000, 500, 1000)
        self.assertEqual(result.absolute_difference, 0.0)
        self.assertFalse(result.significant)
        self.assertAlmostEqual(result.p_value, 1.0, places=9)
        self.assertEqual(result.direction, "no_change")

    def test_difference_interval_brackets_the_estimate(self):
        result = compare_proportions(600, 1000, 700, 1000)
        low, high = result.difference_interval
        self.assertLess(low, result.absolute_difference)
        self.assertGreater(high, result.absolute_difference)
        self.assertGreater(low, 0.0)  # a clear win excludes zero

    def test_relative_difference(self):
        result = compare_proportions(500, 1000, 600, 1000)
        self.assertAlmostEqual(result.relative_difference, 0.20, places=9)

    def test_practical_threshold_uses_the_interval_not_the_estimate(self):
        # +4 points estimated, but the interval runs from -0.3 to +8.2, so
        # even a 0.1-point bar is not cleared. A point estimate above the bar
        # is not evidence that the true effect is above the bar; this
        # distinction is what the whole decision rule rests on.
        result = compare_proportions(600, 1000, 640, 1000)
        self.assertAlmostEqual(result.absolute_difference, 0.04)
        self.assertLess(result.difference_interval[0], 0.0)
        self.assertFalse(result.meets_practical_threshold(0.03))
        self.assertFalse(result.meets_practical_threshold(0.001))

    def test_practical_threshold_clears_a_bar_the_interval_beats(self):
        # +10 points with the whole interval above +3: clears a 3-point bar,
        # but not a 15-point one.
        result = compare_proportions(600, 1000, 700, 1000)
        self.assertGreater(result.difference_interval[0], 0.03)
        self.assertTrue(result.meets_practical_threshold(0.03))
        self.assertFalse(result.meets_practical_threshold(0.15))

    def test_practical_threshold_handles_decrease_direction(self):
        result = compare_proportions(700, 2000, 500, 2000)
        self.assertLess(result.absolute_difference, 0)
        self.assertTrue(result.meets_practical_threshold(-0.05))
        self.assertFalse(result.meets_practical_threshold(-0.50))

    def test_warns_on_small_groups(self):
        result = compare_proportions(3, 10, 6, 10)
        self.assertTrue(any("below the 30" in w for w in result.warnings))

    def test_more_looks_tighten_the_threshold(self):
        one = compare_proportions(600, 1000, 645, 1000, looks=1)
        many = compare_proportions(600, 1000, 645, 1000, looks=10)
        self.assertLess(many.alpha, one.alpha)
        self.assertEqual(one.p_value, many.p_value)
        # Same data, same p-value, stricter bar: peeking costs you power.
        self.assertTrue(one.significant)
        self.assertFalse(many.significant)

    def test_empty_group_is_rejected(self):
        with self.assertRaises(AnalysisError):
            compare_proportions(0, 0, 5, 10)

    def test_impossible_counts_are_rejected(self):
        with self.assertRaises(AnalysisError):
            compare_proportions(11, 10, 5, 10)


class TestCompareMeans(unittest.TestCase):
    def test_detects_a_shift_in_means(self):
        control = [100.0 + i for i in range(50)]
        treatment = [130.0 + i for i in range(50)]
        result = compare_means(control, treatment)
        self.assertAlmostEqual(result.difference, 30.0, places=6)
        self.assertTrue(result.significant)

    def test_identical_samples_are_not_significant(self):
        values = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
        result = compare_means(values, list(values))
        self.assertAlmostEqual(result.difference, 0.0, places=12)
        self.assertFalse(result.significant)

    def test_zero_variance_is_handled_without_dividing_by_zero(self):
        result = compare_means([5.0] * 10, [5.0] * 10)
        self.assertEqual(result.p_value, 1.0)
        self.assertFalse(result.significant)

    def test_welch_handles_unequal_variances(self):
        control = [10.0, 10.5, 9.5, 10.2, 9.8] * 6
        treatment = [1.0, 30.0, 5.0, 25.0, 12.0] * 6
        result = compare_means(control, treatment)
        # Welch-Satterthwaite df is below the pooled n1+n2-2 when variances differ.
        self.assertLess(result.degrees_of_freedom, len(control) + len(treatment) - 2)

    def test_requires_two_observations_per_group(self):
        with self.assertRaises(AnalysisError):
            compare_means([1.0], [2.0, 3.0])


class TestMultiplicity(unittest.TestCase):
    def test_sidak_correction_shrinks_alpha(self):
        self.assertAlmostEqual(corrected_alpha(0.05, 1), 0.05, places=12)
        self.assertLess(corrected_alpha(0.05, 4), 0.05)
        self.assertAlmostEqual(corrected_alpha(0.05, 4), 0.012741455, places=8)

    def test_correction_is_conservative_relative_to_uncorrected(self):
        for looks in range(1, 20):
            self.assertLessEqual(corrected_alpha(0.05, looks), 0.05)

    def test_corrected_alpha_rejects_bad_input(self):
        with self.assertRaises(AnalysisError):
            corrected_alpha(0.0, 1)
        with self.assertRaises(AnalysisError):
            corrected_alpha(0.05, 0)

    def test_holm_step_down_stops_at_first_failure(self):
        # Sorted: 0.01 vs 0.05/3=0.0167 rejects; 0.03 vs 0.05/2=0.025 fails,
        # so 0.04 is not tested either.
        self.assertEqual(holm_bonferroni([0.01, 0.04, 0.03], 0.05), [True, False, False])

    def test_holm_rejects_all_when_all_are_small(self):
        self.assertEqual(holm_bonferroni([0.001, 0.002, 0.003], 0.05), [True, True, True])

    def test_holm_is_at_least_as_powerful_as_bonferroni(self):
        p_values = [0.001, 0.02, 0.03, 0.04]
        holm = holm_bonferroni(p_values, 0.05)
        bonferroni = [p <= 0.05 / len(p_values) for p in p_values]
        for h, b in zip(holm, bonferroni, strict=True):
            self.assertTrue(h or not b)

    def test_holm_handles_empty_input(self):
        self.assertEqual(holm_bonferroni([]), [])


class TestPlanning(unittest.TestCase):
    def test_sample_size_matches_standard_calculation(self):
        # 60% baseline, +5 points, alpha 0.05, power 0.80 -> ~1471 per group.
        n = required_sample_size_per_group(0.60, 0.05)
        self.assertGreater(n, 1400)
        self.assertLess(n, 1550)

    def test_smaller_effects_need_larger_samples(self):
        big = required_sample_size_per_group(0.60, 0.10)
        small = required_sample_size_per_group(0.60, 0.02)
        self.assertGreater(small, big * 10)

    def test_more_looks_require_more_units(self):
        one = required_sample_size_per_group(0.60, 0.05, looks=1)
        five = required_sample_size_per_group(0.60, 0.05, looks=5)
        self.assertGreater(five, one)

    def test_mde_and_sample_size_are_consistent(self):
        # The MDE at the sample size required for an effect should be close
        # to that effect; these are inverses of one another.
        effect = 0.05
        n = required_sample_size_per_group(0.60, effect)
        detectable = minimum_detectable_effect(0.60, n)
        self.assertAlmostEqual(detectable, effect, delta=0.002)

    def test_mde_shrinks_as_sample_grows(self):
        self.assertGreater(
            minimum_detectable_effect(0.5, 100), minimum_detectable_effect(0.5, 10000)
        )

    def test_rejects_effect_pushing_rate_out_of_bounds(self):
        with self.assertRaises(AnalysisError):
            required_sample_size_per_group(0.98, 0.05)

    def test_rejects_zero_effect(self):
        with self.assertRaises(AnalysisError):
            required_sample_size_per_group(0.60, 0.0)


if __name__ == "__main__":
    unittest.main()

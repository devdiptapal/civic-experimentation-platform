"""Tests for the standard metric library and metric definitions in configs.

A standard metric is a promise of comparability across agencies, so these
check that each one computes what its definition says -- not merely that it
loads.
"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from civicexp.analysis import analyze
from civicexp.config import config_fingerprint, load_config
from civicexp.errors import ConfigError
from civicexp.events import EventLog
from civicexp.metrics import STANDARD_METRICS, format_effect, standard_metric_for
from civicexp.privacy import pseudonymize
from tests.test_config import VALID

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def config_using(primary, guardrails=None):
    data = copy.deepcopy(VALID)
    data["metrics"]["primary"] = primary
    if guardrails is not None:
        data["metrics"]["guardrails"] = guardrails
    return load_config(data)


def unit(log, label, variant, *events):
    pseudonym = pseudonymize(label, "salt")
    for offset, event in enumerate(("experiment_assigned",) + events):
        log.record(event, pseudonym, variant, timestamp=T0 + timedelta(seconds=offset))


class TestAbandonment(unittest.TestCase):
    def test_counts_people_who_entered_and_never_submitted(self):
        config = config_using({"use": "abandonment_rate", "minimum_effect_of_interest": 0.03})
        log = EventLog(config.experiment_id, allowed_variants=config.variants)
        # Control: 3 of 4 abandon. Every one of them viewed a step, which is
        # what the v0.3 definition wrongly counted as abandonment.
        unit(log, "c1", "control", "step_viewed", "submission_succeeded")
        unit(log, "c2", "control", "step_viewed")
        unit(log, "c3", "control", "step_viewed")
        unit(log, "c4", "control", "step_viewed")
        # Treatment: 1 of 4 abandons.
        unit(log, "t1", "treatment", "step_viewed", "submission_succeeded")
        unit(log, "t2", "treatment", "step_viewed", "submission_succeeded")
        unit(log, "t3", "treatment", "step_viewed", "submission_succeeded")
        unit(log, "t4", "treatment", "step_viewed")

        result = analyze(config, log)
        self.assertAlmostEqual(result.primary.control_rate, 0.75)
        self.assertAlmostEqual(result.primary.treatment_rate, 0.25)


class TestFirstTimeSuccess(unittest.TestCase):
    def test_a_failed_attempt_disqualifies_a_later_success(self):
        config = config_using(
            {"name": "completion_rate", "definition": "d",
             "numerator_event": "submission_succeeded",
             "denominator_event": "experiment_assigned",
             "minimum_effect_of_interest": 0.03},
            guardrails=[{"use": "first_time_success_rate", "max_tolerated_increase": 0.5}],
        )
        log = EventLog(config.experiment_id, allowed_variants=config.variants)
        unit(log, "c1", "control", "submission_attempted", "submission_succeeded")
        unit(log, "c2", "control", "submission_attempted", "submission_failed",
             "submission_attempted", "submission_succeeded")
        unit(log, "t1", "treatment", "submission_attempted", "submission_succeeded")
        unit(log, "t2", "treatment", "submission_attempted", "submission_succeeded")

        counts = log.count_units_meeting(
            "submission_succeeded", excluding=("submission_failed",)
        )
        self.assertEqual(counts, {"control": 1, "treatment": 2})
        self.assertEqual(log.units_meeting("submission_succeeded"), {
            pseudonymize(label, "salt") for label in ("c1", "c2", "t1", "t2")
        })
        guardrail = analyze(config, log).guardrails[0]
        self.assertAlmostEqual(guardrail.comparison.control_rate, 0.5)


class TestEventNamesAreValidated(unittest.TestCase):
    def test_misspelled_event_is_an_error_not_a_silent_empty_metric(self):
        data = copy.deepcopy(VALID)
        data["metrics"]["primary"]["numerator_event"] = "submision_succeeded"
        with self.assertRaises(ConfigError) as ctx:
            load_config(data)
        self.assertIn("submision_succeeded", ctx.exception.message)
        self.assertIn("taxonomy", ctx.exception.message)

    def test_unknown_excluded_event_is_an_error(self):
        data = copy.deepcopy(VALID)
        data["metrics"]["primary"]["numerator_excludes"] = ["nope"]
        with self.assertRaises(ConfigError):
            load_config(data)

    def test_excludes_on_a_duration_metric_is_an_error(self):
        data = copy.deepcopy(VALID)
        data["metrics"]["guardrails"] = [{
            "use": "time_to_complete", "max_tolerated_increase": 60,
            "numerator_excludes": ["submission_failed"],
        }]
        with self.assertRaises(ConfigError):
            load_config(data)

    def test_every_standard_metric_loads_in_a_config(self):
        for name, metric in STANDARD_METRICS.items():
            spec = {"use": name}
            if metric.kind == "proportion":
                spec["minimum_effect_of_interest"] = metric.typical_effect_of_interest
                config = config_using(spec)
                self.assertEqual(standard_metric_for(config.primary_metric), name)
            else:
                spec["max_tolerated_increase"] = metric.typical_effect_of_interest
                config = config_using(VALID["metrics"]["primary"], guardrails=[spec])
                self.assertEqual(standard_metric_for(config.guardrails[0]), name)


class TestComparabilityMarker(unittest.TestCase):
    def test_a_redefined_metric_is_not_reported_as_standard(self):
        config = config_using({
            "name": "completion_rate", "definition": "ours",
            "numerator_event": "step_completed",
            "denominator_event": "experiment_assigned",
            "minimum_effect_of_interest": 0.03,
        })
        self.assertIsNone(standard_metric_for(config.primary_metric))


class TestFormatting(unittest.TestCase):
    def test_durations_use_the_unit_people_think_in(self):
        self.assertEqual(format_effect(45, "duration"), "45 seconds")
        self.assertEqual(format_effect(600, "duration"), "10.0 minutes")
        self.assertEqual(format_effect(7200, "duration"), "2.0 hours")
        self.assertEqual(format_effect(86400 * 2, "duration"), "2.0 days")
        self.assertEqual(format_effect(0.031, "proportion"), "3.1 percentage points")


class TestFingerprint(unittest.TestCase):
    def test_key_order_does_not_change_it(self):
        reordered = dict(reversed(list(copy.deepcopy(VALID).items())))
        self.assertEqual(config_fingerprint(VALID), config_fingerprint(reordered))
        self.assertEqual(load_config(VALID).fingerprint, config_fingerprint(VALID))

    def test_any_value_change_does(self):
        changed = copy.deepcopy(VALID)
        changed["metrics"]["primary"]["minimum_effect_of_interest"] = 0.02
        self.assertNotEqual(config_fingerprint(VALID), config_fingerprint(changed))


class TestLooksTaken(unittest.TestCase):
    def setUp(self):
        self.config = load_config(VALID)
        self.log = EventLog(self.config.experiment_id, allowed_variants=self.config.variants)
        for i in range(40):
            unit(self.log, f"c{i}", "control", *(["submission_succeeded"] if i % 2 else []))
            unit(self.log, f"t{i}", "treatment", *(["submission_succeeded"] if i % 3 else []))

    def test_extra_looks_are_disclosed(self):
        result = analyze(self.config, self.log, looks_taken=3)
        self.assertTrue(any("examined 3 times" in w for w in result.warnings))

    def test_planned_looks_are_not_flagged(self):
        result = analyze(self.config, self.log, looks_taken=1)
        self.assertFalse(any("examined" in w for w in result.warnings))


class TestDirection(unittest.TestCase):
    """Which way is "better" must be honored everywhere, not assumed upward."""

    def _log(self, config, control, treatment, segment_value="en"):
        log = EventLog(config.experiment_id, allowed_variants=config.variants)
        for variant, (hits, total) in (("control", control), ("treatment", treatment)):
            for i in range(total):
                pseudonym = pseudonymize(f"{variant}-{segment_value}-{i}", "salt")
                log.record("experiment_assigned", pseudonym, variant, timestamp=T0,
                           preferred_language=segment_value)
                log.record("submission_attempted", pseudonym, variant,
                           timestamp=T0 + timedelta(seconds=1))
                if i < hits:
                    log.record("submission_succeeded", pseudonym, variant,
                               timestamp=T0 + timedelta(seconds=2))
        return log

    def test_a_success_rate_guardrail_is_breached_by_falling_not_rising(self):
        config = config_using(
            {"use": "completion_rate", "minimum_effect_of_interest": 0.03},
            guardrails=[{"use": "first_time_success_rate", "max_tolerated_increase": 0.02}],
        )
        improved = analyze(config, self._log(config, (600, 1000), (800, 1000)))
        self.assertFalse(improved.guardrails[0].breached)
        self.assertEqual(improved.guardrails[0].status, "OK")

        worsened = analyze(config, self._log(config, (800, 1000), (600, 1000)))
        self.assertTrue(worsened.guardrails[0].breached)
        self.assertIn("Worsened by 20.0", worsened.guardrails[0].note)

    def test_a_lower_is_better_primary_does_not_read_benefit_as_harm(self):
        config = config_using(
            {"use": "abandonment_rate", "minimum_effect_of_interest": 0.03}
        )
        # Abandonment falls from 40% to 20%: a large benefit.
        result = analyze(config, self._log(config, (600, 1000), (800, 1000)))
        self.assertEqual(result.decision.value, "promote")
        self.assertFalse(result.equity.harmed)
        self.assertEqual(result.equity.findings[0].outcome.value, "helped")

    def test_the_readout_calls_a_fall_in_abandonment_better(self):
        from civicexp.report import render_markdown

        config = config_using(
            {"use": "abandonment_rate", "minimum_effect_of_interest": 0.03}
        )
        result = analyze(config, self._log(config, (600, 1000), (800, 1000)))
        text = render_markdown(result)
        self.assertIn("confident the tested version is better", text)
        self.assertNotIn("confident the tested version is worse", text)

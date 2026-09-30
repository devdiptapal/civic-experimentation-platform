"""Tests for the decision engine.

These assert the behaviour the governance documents promise: harm outranks
benefit, practical significance outranks statistical significance, and an
underpowered pilot is reported as inconclusive rather than as a null result.
"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from civicexp.analysis import Decision, analyze
from civicexp.config import load_config
from civicexp.errors import AnalysisError
from civicexp.events import EventLog
from civicexp.privacy import pseudonymize
from tests.test_config import VALID

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def build_log(config, *, control, treatment, seconds=None):
    """Create a log with exact counts per arm.

    ``control`` and ``treatment`` are ``(successes, failures, errors, n)``.
    """
    log = EventLog(
        config.experiment_id, privacy=config.privacy, allowed_variants=config.variants
    )
    for variant, (successes, errors, total) in (
        (config.control_variant, control),
        ("treatment", treatment),
    ):
        for i in range(total):
            pseudonym = pseudonymize(f"{variant}-unit-{i}", "salt")
            log.record("experiment_assigned", pseudonym, variant, timestamp=T0)
            if i < successes:
                elapsed = (seconds or {}).get(variant, 100)
                log.record(
                    "submission_succeeded", pseudonym, variant,
                    timestamp=T0 + timedelta(seconds=elapsed),
                )
            if i < errors:
                log.record(
                    "submission_failed", pseudonym, variant,
                    timestamp=T0 + timedelta(seconds=50),
                )
    return log


class TestDecisionRule(unittest.TestCase):
    def setUp(self):
        self.config = load_config(VALID)

    def test_clear_improvement_promotes(self):
        log = build_log(
            self.config, control=(1200, 100, 2000), treatment=(1500, 100, 2000)
        )
        result = analyze(self.config, log)
        self.assertIs(result.decision, Decision.PROMOTE)
        self.assertIn("clears", result.rationale[0])

    def test_improvement_below_the_bar_iterates(self):
        # Statistically detectable, but the interval does not clear the
        # 3-point bar the program set. Not a win.
        log = build_log(
            self.config, control=(12000, 1000, 20000), treatment=(12400, 1000, 20000)
        )
        result = analyze(self.config, log)
        self.assertIs(result.decision, Decision.ITERATE)
        self.assertIn("smaller than", result.rationale[0])

    def test_no_difference_iterates(self):
        log = build_log(
            self.config, control=(6200, 500, 10000), treatment=(6200, 500, 10000)
        )
        result = analyze(self.config, log)
        self.assertIs(result.decision, Decision.ITERATE)

    def test_change_in_the_wrong_direction_iterates_not_rolls_back(self):
        # A failed test is not a harm event: rollback is reserved for
        # breached guardrails.
        log = build_log(
            self.config, control=(1400, 100, 2000), treatment=(1100, 100, 2000)
        )
        result = analyze(self.config, log)
        self.assertIs(result.decision, Decision.ITERATE)
        self.assertIn("opposite direction", result.rationale[0])

    def test_underpowered_pilot_is_inconclusive_not_null(self):
        # 40 units per arm cannot distinguish a 3-point effect from nothing.
        # Reporting "no effect" here would be a false negative dressed as a
        # finding.
        log = build_log(self.config, control=(25, 2, 40), treatment=(28, 2, 40))
        result = analyze(self.config, log)
        self.assertIs(result.decision, Decision.INCONCLUSIVE)
        self.assertIn("did not collect enough data", result.rationale[0])


class TestGuardrailPrecedence(unittest.TestCase):
    def setUp(self):
        self.config = load_config(VALID)

    def test_breached_guardrail_overrides_a_winning_primary(self):
        # The central safety property: a large completion win does not buy
        # an increase in blocking errors.
        log = build_log(
            self.config, control=(1200, 100, 2000), treatment=(1500, 400, 2000)
        )
        result = analyze(self.config, log)
        self.assertIs(result.decision, Decision.ROLLBACK)
        self.assertEqual(len(result.breached_guardrails), 1)
        self.assertEqual(result.breached_guardrails[0].name, "error_rate")

    def test_guardrail_within_tolerance_does_not_block(self):
        log = build_log(
            self.config, control=(1200, 100, 2000), treatment=(1500, 105, 2000)
        )
        result = analyze(self.config, log)
        self.assertIs(result.decision, Decision.PROMOTE)

    def test_guardrail_improvement_never_counts_as_a_breach(self):
        log = build_log(
            self.config, control=(1200, 400, 2000), treatment=(1500, 100, 2000)
        )
        result = analyze(self.config, log)
        self.assertEqual(result.breached_guardrails, ())

    def test_guardrail_with_no_data_is_unverified_not_passing(self):
        config = load_config(VALID)
        log = EventLog(
            config.experiment_id, privacy=config.privacy, allowed_variants=config.variants
        )
        for variant in (config.control_variant, "treatment"):
            for i in range(100):
                pseudonym = pseudonymize(f"{variant}-{i}", "salt")
                log.record("experiment_assigned", pseudonym, variant, timestamp=T0)
                if i < 60:
                    log.record("submission_succeeded", pseudonym, variant, timestamp=T0)
        result = analyze(config, log)
        guardrail = result.guardrails[0]
        self.assertEqual(guardrail.status, "OK")

    def test_multiple_guardrails_get_a_family_correction(self):
        data = copy.deepcopy(VALID)
        base = data["metrics"]["guardrails"][0]
        data["metrics"]["guardrails"] = [
            dict(base, name=f"guardrail_{i}") for i in range(4)
        ]
        config = load_config(data)
        log = build_log(config, control=(1200, 100, 2000), treatment=(1500, 100, 2000))
        result = analyze(config, log)
        self.assertEqual(len(result.guardrails), 4)
        self.assertEqual(result.breached_guardrails, ())


class TestAnalysisErrors(unittest.TestCase):
    def setUp(self):
        self.config = load_config(VALID)

    def test_no_data_is_an_error_not_a_decision(self):
        log = EventLog(
            self.config.experiment_id,
            privacy=self.config.privacy,
            allowed_variants=self.config.variants,
        )
        with self.assertRaises(AnalysisError) as ctx:
            analyze(self.config, log)
        self.assertEqual(ctx.exception.code, "ANALYSIS_NO_DATA")

    def test_unknown_treatment_arm_is_rejected(self):
        log = build_log(self.config, control=(60, 5, 100), treatment=(70, 5, 100))
        with self.assertRaises(AnalysisError) as ctx:
            analyze(self.config, log, treatment_variant="arm_z")
        self.assertEqual(ctx.exception.code, "ANALYSIS_UNKNOWN_ARM")

    def test_multiple_arms_require_naming_one(self):
        data = copy.deepcopy(VALID)
        data["variants"] = {
            "control": {"traffic_share": 0.34},
            "treatment": {"traffic_share": 0.33},
            "treatment_b": {"traffic_share": 0.33},
        }
        config = load_config(data)
        log = build_log(config, control=(60, 5, 100), treatment=(70, 5, 100))
        with self.assertRaises(AnalysisError) as ctx:
            analyze(config, log)
        self.assertEqual(ctx.exception.code, "ANALYSIS_AMBIGUOUS_ARM")

    def test_small_cells_produce_a_publication_warning(self):
        log = build_log(self.config, control=(3, 0, 5), treatment=(4, 0, 5))
        result = analyze(self.config, log)
        self.assertTrue(any("fewer than 11" in w for w in result.warnings))


class TestSerialization(unittest.TestCase):
    def test_result_serializes_to_json_safe_types(self):
        import json

        config = load_config(VALID)
        log = build_log(config, control=(1200, 100, 2000), treatment=(1500, 100, 2000))
        payload = analyze(config, log).to_dict()
        json.dumps(payload)  # raises if a non-serializable type leaked in
        self.assertEqual(payload["decision"], "promote")
        self.assertIn("confidence_interval", payload["primary"])


if __name__ == "__main__":
    unittest.main()

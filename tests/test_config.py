"""Tests for configuration loading and validation."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from civicexp.config import load_config
from civicexp.errors import ConfigError

VALID = {
    "experiment": {"id": "exp-1", "name": "Test", "owner_team": "team"},
    "scope": {"service_area": "benefits"},
    "assignment": {"salt": "0123456789abcdef0123", "control_variant": "control"},
    "variants": {
        "control": {"traffic_share": 0.5},
        "treatment": {"traffic_share": 0.5},
    },
    "eligibility": {"include": [{"field": "channel", "operator": "eq", "value": "web"}]},
    "metrics": {
        "primary": {
            "name": "completion_rate",
            "definition": "Share completing",
            "numerator_event": "submission_succeeded",
            "denominator_event": "experiment_assigned",
            "target_direction": "increase",
            "minimum_effect_of_interest": 0.03,
        },
        "guardrails": [
            {
                "name": "error_rate",
                "definition": "Share hitting an error",
                "numerator_event": "submission_failed",
                "denominator_event": "experiment_assigned",
                "max_tolerated_increase": 0.01,
            }
        ],
    },
    "segments": ["preferred_language"],
    "sample_size": {"baseline_rate": 0.62, "expected_units_per_group": 6000},
    "analysis": {"confidence": 0.95, "power": 0.8, "planned_looks": 1},
    "data_governance": {"retention_days": 90, "suppression_threshold": 11},
}


def config_with(**changes):
    data = copy.deepcopy(VALID)
    for dotted, value in changes.items():
        keys = dotted.split("__")
        node = data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        if value is None:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    return data


class TestValidConfig(unittest.TestCase):
    def test_loads_a_valid_config(self):
        config = load_config(VALID)
        self.assertEqual(config.experiment_id, "exp-1")
        self.assertEqual(config.control_variant, "control")
        self.assertEqual(config.treatment_variants, ("treatment",))
        self.assertEqual(len(config.guardrails), 1)
        self.assertEqual(config.privacy.retention_days, 90)

    def test_parses_eligibility_rules(self):
        config = load_config(VALID)
        self.assertTrue(config.eligibility.is_eligible({"channel": "web"}))
        self.assertFalse(config.eligibility.is_eligible({"channel": "phone"}))


class TestErrorReporting(unittest.TestCase):
    def test_reports_all_problems_at_once(self):
        # A reviewer fixing a config should need one round trip, not several.
        broken = config_with(
            experiment__id="",
            assignment__salt="",
            metrics__primary=None,
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(broken)
        message = ctx.exception.message
        self.assertIn("experiment.id", message)
        self.assertIn("assignment.salt", message)
        self.assertIn("metrics.primary", message)

    def test_placeholder_salt_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(config_with(assignment__salt="placeholder-value"))
        self.assertIn("placeholder", ctx.exception.message)
        self.assertIn("secrets.token_hex", ctx.exception.message)

    def test_shares_must_sum_to_one(self):
        broken = config_with(variants={"control": {"traffic_share": 0.5},
                                       "treatment": {"traffic_share": 0.3}})
        with self.assertRaises(ConfigError) as ctx:
            load_config(broken)
        self.assertIn("sum to 1.0", ctx.exception.message)

    def test_control_variant_must_exist(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(config_with(assignment__control_variant="baseline"))
        self.assertIn("control_variant", ctx.exception.message)

    def test_placeholder_effect_size_is_rejected(self):
        # The original scaffold used the string "placeholder" here. An
        # experiment with no numeric effect of interest cannot produce a
        # decision, so it must not load.
        broken = config_with(metrics__primary__minimum_effect_of_interest="placeholder")
        with self.assertRaises(ConfigError) as ctx:
            load_config(broken)
        self.assertIn("minimum_effect_of_interest", ctx.exception.message)

    def test_placeholder_guardrail_threshold_is_rejected(self):
        broken = copy.deepcopy(VALID)
        broken["metrics"]["guardrails"][0]["max_tolerated_increase"] = (
            "must_not_increase_by_more_than_placeholder"
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(broken)
        self.assertIn("cannot stop anything", ctx.exception.message)

    def test_negative_effect_of_interest_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(config_with(metrics__primary__minimum_effect_of_interest=-0.03))

    def test_eligibility_on_a_pii_field_is_rejected(self):
        broken = config_with(
            eligibility={"include": [{"field": "ssn", "operator": "exists"}]}
        )
        with self.assertRaises(ConfigError) as ctx:
            load_config(broken)
        self.assertIn("personal", ctx.exception.message.lower())

    def test_unknown_eligibility_operator_is_rejected(self):
        broken = config_with(
            eligibility={"include": [{"field": "x", "operator": "matches", "value": "a"}]}
        )
        with self.assertRaises(ConfigError):
            load_config(broken)

    def test_missing_experiment_section_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config({"variants": {}})


class TestWarnings(unittest.TestCase):
    def test_underpowered_config_loads_but_warns(self):
        # Valid, but unwise. The agency, not the tool, owns that judgment.
        config = load_config(config_with(sample_size__expected_units_per_group=200))
        self.assertTrue(any("Underpowered" in w for w in config.warnings))

    def test_adequately_powered_config_says_so(self):
        note = load_config(VALID).power_note()
        self.assertIn("Adequately powered", note)

    def test_missing_guardrails_warns(self):
        config = load_config(config_with(metrics__guardrails=[]))
        self.assertTrue(any("guardrail" in w.lower() for w in config.warnings))

    def test_allowlisted_pii_warns(self):
        config = load_config(
            config_with(data_governance__approved_pii_fields=["case_number"])
        )
        self.assertTrue(any("approved PII" in w for w in config.warnings))

    def test_long_retention_warns(self):
        config = load_config(config_with(data_governance__retention_days=1000))
        self.assertTrue(any("retention" in w.lower() for w in config.warnings))

    def test_short_salt_warns_but_loads(self):
        config = load_config(config_with(assignment__salt="abc123"))
        self.assertTrue(any("salt" in w for w in config.warnings))

    def test_missing_sizing_information_warns(self):
        config = load_config(config_with(sample_size__baseline_rate=None))
        self.assertTrue(any("power" in w.lower() for w in config.warnings))
        self.assertIsNone(config.power_note())


class TestFileLoading(unittest.TestCase):
    def test_loads_from_a_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiment.json"
            path.write_text(json.dumps(VALID), encoding="utf-8")
            self.assertEqual(load_config(path).experiment_id, "exp-1")

    def test_malformed_json_is_reported_with_a_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiment.json"
            path.write_text('{"experiment": ', encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertEqual(ctx.exception.code, "CONFIG_UNPARSEABLE")

    def test_yaml_loads_when_pyyaml_is_available(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experiment.yml"
            path.write_text(yaml.safe_dump(VALID), encoding="utf-8")
            self.assertEqual(load_config(path).experiment_id, "exp-1")

    def test_sample_yaml_config_is_valid(self):
        # A sample configuration the validator rejects teaches the wrong
        # shape, so the shipped YAML sample must load cleanly.
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        sample = Path(__file__).resolve().parents[1] / "examples" / "sample-config.yml"
        config = load_config(sample)
        self.assertEqual(config.warnings, ())
        self.assertEqual(len(config.guardrails), 3)

    def test_shipped_example_is_valid(self):
        example = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "sf-hsa-document-upload"
            / "experiment.json"
        )
        config = load_config(example)
        self.assertEqual(config.experiment_id, "exp-doc-upload-001")
        self.assertEqual(len(config.guardrails), 3)
        # The shipped example must be a well-designed pilot, not merely a
        # loadable one.
        self.assertEqual(config.warnings, ())
        self.assertIn("Adequately powered", config.power_note())


if __name__ == "__main__":
    unittest.main()

"""Tests for the setup wizard, templates, and the metric library.

The roadmap target these serve is "usable with less than two hours of
onboarding". The tests that matter are therefore less about correctness of
output than about whether the wizard refuses to let someone build a pilot
that cannot work.
"""

from __future__ import annotations

import unittest

from civicexp.config import load_config
from civicexp.metrics import STANDARD_METRICS, expand_metric
from civicexp.wizard import TEMPLATES, list_templates, load_template, run_wizard

# Answers for a well-sized document-upload pilot. Templates are listed
# alphabetically, so document-upload is option 3.
GOOD_ANSWERS = [
    "3",
    "Document upload checklist",
    "Replace the requirements paragraph with a checklist.",
    "digital-services",
    "example-county",
    "62",
    "3",
    "13000",
    "28",
    "y",
]


def drive(answers):
    """Run the wizard against scripted answers; returns (config, output)."""
    it = iter(answers)
    out: list[str] = []
    config = run_wizard(prompt=lambda _q: next(it), echo=out.append)
    return config, "\n".join(out)


class TestTemplates(unittest.TestCase):
    def test_every_template_loads(self):
        for name in TEMPLATES:
            with self.subTest(template=name):
                self.assertIn("experiment", load_template(name))

    def test_every_template_is_a_valid_config_once_filled(self):
        # The placeholders are intentional, but the structure must be sound
        # apart from them, or the template teaches the wrong shape.
        import secrets

        for name in TEMPLATES:
            with self.subTest(template=name):
                data = load_template(name)
                data.pop("_template")
                data["assignment"]["salt"] = secrets.token_hex(16)
                config = load_config(data)
                self.assertTrue(config.experiment_id)
                self.assertGreaterEqual(len(config.guardrails), 1)
                self.assertTrue(config.segments)

    def test_templates_cover_the_named_workflows(self):
        self.assertEqual(
            set(TEMPLATES),
            {"document-upload", "appointment-reminder", "digital-intake"},
        )

    def test_each_template_describes_itself(self):
        for name, header in list_templates():
            with self.subTest(template=name):
                for key in ("title", "summary", "good_for", "typical_change"):
                    self.assertTrue(header.get(key), f"{name} missing {key}")

    def test_unknown_template_is_rejected(self):
        with self.assertRaises(KeyError):
            load_template("nonexistent")


class TestMetricLibrary(unittest.TestCase):
    def test_use_reference_expands(self):
        expanded = expand_metric({"use": "completion_rate"})
        self.assertEqual(expanded["numerator_event"], "submission_succeeded")
        self.assertEqual(expanded["target_direction"], "increase")

    def test_overrides_win_over_the_standard_definition(self):
        expanded = expand_metric(
            {"use": "completion_rate", "minimum_effect_of_interest": 0.07}
        )
        self.assertEqual(expanded["minimum_effect_of_interest"], 0.07)

    def test_spec_without_use_passes_through(self):
        original = {"name": "custom", "numerator_event": "x"}
        self.assertEqual(expand_metric(original), original)

    def test_unknown_metric_names_the_alternatives(self):
        with self.assertRaises(KeyError) as ctx:
            expand_metric({"use": "made_up"})
        self.assertIn("completion_rate", ctx.exception.args[0])

    def test_library_covers_the_roadmap_outcomes(self):
        for name in (
            "completion_rate",
            "abandonment_rate",
            "time_to_complete",
            "first_time_success_rate",
        ):
            self.assertIn(name, STANDARD_METRICS)

    def test_every_metric_explains_why_it_matters(self):
        for name, metric in STANDARD_METRICS.items():
            with self.subTest(metric=name):
                self.assertGreater(len(metric.why), 40)
                self.assertIn(metric.target_direction, ("increase", "decrease"))


class TestWizard(unittest.TestCase):
    def test_produces_a_valid_configuration(self):
        config, _ = drive(GOOD_ANSWERS)
        loaded = load_config(config)
        self.assertEqual(loaded.primary_metric.name, "completion_rate")
        self.assertEqual(loaded.warnings, ())

    def test_generates_its_own_salt(self):
        # Never asked for, never a placeholder.
        config, _ = drive(GOOD_ANSWERS)
        self.assertEqual(len(config["assignment"]["salt"]), 32)
        int(config["assignment"]["salt"], 16)

    def test_two_runs_produce_different_salts(self):
        first, _ = drive(GOOD_ANSWERS)
        second, _ = drive(GOOD_ANSWERS)
        self.assertNotEqual(
            first["assignment"]["salt"], second["assignment"]["salt"]
        )

    def test_warns_when_the_pilot_is_too_small(self):
        answers = list(GOOD_ANSWERS)
        answers[7] = "800"  # monthly volume
        config, output = drive(answers)
        self.assertIn("too small to answer the question", output)
        self.assertIn("run for about", output)
        # Still writes the config: the agency decides, not the tool.
        self.assertTrue(load_config(config).warnings)

    def test_confirms_when_the_pilot_is_adequately_powered(self):
        _, output = drive(GOOD_ANSWERS)
        self.assertIn("That works", output)

    def test_requires_an_effect_of_interest(self):
        # Pressing enter at the effect question must not silently accept a
        # default: it is a program judgment, not a technical one.
        # Empty at the effect question, then a real answer.
        answers = GOOD_ANSWERS[:6] + ["", "3"] + GOOD_ANSWERS[7:]
        it = iter(answers)
        out: list[str] = []
        config = run_wizard(prompt=lambda _q: next(it), echo=out.append)
        self.assertEqual(config["metrics"]["primary"]["minimum_effect_of_interest"], 0.03)
        self.assertIn("Please enter a percentage", "\n".join(out))

    def test_accepts_percentages_in_several_formats(self):
        for written, expected in (("62", 0.62), ("62%", 0.62), ("0.62", 0.62)):
            with self.subTest(written=written):
                answers = list(GOOD_ANSWERS)
                answers[5] = written
                config, _ = drive(answers)
                self.assertAlmostEqual(
                    config["sample_size"]["baseline_rate"], expected, places=4
                )

    def test_declares_equity_segments(self):
        config, output = drive(GOOD_ANSWERS)
        self.assertTrue(config["segments"])
        self.assertIn("hide a group the change made worse", output)

    def test_custom_segments_can_be_supplied(self):
        answers = list(GOOD_ANSWERS)
        answers[-1] = "n"
        answers.append("preferred_language, region")
        config, _ = drive(answers)
        self.assertEqual(config["segments"], ["preferred_language", "region"])

    def test_explains_what_the_effect_of_interest_means(self):
        _, output = drive(GOOD_ANSWERS)
        self.assertIn("judgment rather than a technical one", output)

    def test_tells_the_user_what_is_left_to_fill_in(self):
        _, output = drive(GOOD_ANSWERS)
        self.assertIn("Still to fill in by hand", output)
        self.assertIn("eligibility", output)

    def test_reminds_the_user_the_salt_is_a_secret(self):
        _, output = drive(GOOD_ANSWERS)
        self.assertIn("secret", output)

    def test_rejects_nonsense_then_accepts_a_number(self):
        answers = GOOD_ANSWERS[:5] + ["about sixty percent", "62"] + GOOD_ANSWERS[6:]
        it = iter(answers)
        out: list[str] = []
        config = run_wizard(prompt=lambda _q: next(it), echo=out.append)
        self.assertAlmostEqual(config["sample_size"]["baseline_rate"], 0.62, places=4)
        self.assertIn("Please enter a percentage", "\n".join(out))


if __name__ == "__main__":
    unittest.main()

"""Tests for the equity review.

The behaviour these lock in is the project's central equity claim: that an
overall improvement which leaves a group behind, or harms one, is reported
as such rather than disappearing into the average.
"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from civicexp.analysis import Decision, analyze
from civicexp.config import load_config
from civicexp.events import EventLog
from civicexp.privacy import pseudonymize
from civicexp.segments import SegmentOutcome, analyze_segments
from tests.test_config import VALID

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
TREATMENT = "treatment"


def config_with_segments(dimensions=("preferred_language",)):
    data = copy.deepcopy(VALID)
    data["segments"] = list(dimensions)
    return load_config(data)


def build_log(config, groups):
    """Build a log from {(segment_value, variant): (successes, total)}."""
    log = EventLog(
        config.experiment_id, privacy=config.privacy, allowed_variants=config.variants
    )
    counter = 0
    for (value, variant), (successes, total) in groups.items():
        for i in range(total):
            counter += 1
            pseudonym = pseudonymize(f"unit-{counter}", "salt")
            log.record(
                "experiment_assigned", pseudonym, variant, timestamp=T0,
                payload={"preferred_language": value},
            )
            if i < successes:
                log.record("submission_succeeded", pseudonym, variant, timestamp=T0)
    return log


class TestEquityDetection(unittest.TestCase):
    def test_uniform_benefit_flags_nothing(self):
        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 1000), ("en", TREATMENT): (700, 1000),
            ("es", "control"): (600, 1000), ("es", TREATMENT): (700, 1000),
        })
        equity = analyze_segments(config, log, "control", TREATMENT)
        self.assertFalse(equity.has_equity_concern)
        self.assertEqual(
            {f.outcome for f in equity.findings}, {SegmentOutcome.HELPED}
        )

    def test_group_left_behind_is_reported(self):
        # The headline improves, but one group sees nothing.
        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 2000), ("en", TREATMENT): (760, 2000),
            ("es", "control"): (600, 1000), ("es", TREATMENT): (600, 1000),
        })
        equity = analyze_segments(config, log, "control", TREATMENT)
        by_value = {f.value: f.outcome for f in equity.findings}
        self.assertIs(by_value["en"], SegmentOutcome.HELPED)
        self.assertIs(by_value["es"], SegmentOutcome.NO_EFFECT)
        self.assertTrue(equity.not_reached)
        self.assertIn("did not reach them", equity.summary_line())

    def test_harmed_group_is_reported(self):
        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 2000), ("en", TREATMENT): (800, 2000),
            ("es", "control"): (600, 1000), ("es", TREATMENT): (420, 1000),
        })
        equity = analyze_segments(config, log, "control", TREATMENT)
        self.assertTrue(equity.has_equity_concern)
        self.assertEqual([f.value for f in equity.harmed], ["es"])
        self.assertIn("harmed", equity.summary_line())

    def test_tiny_group_is_reported_as_unknown_not_as_no_effect(self):
        # Saying "no effect" about 40 people would be a false negative
        # dressed up as a finding.
        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 1000), ("en", TREATMENT): (700, 1000),
            ("es", "control"): (24, 40), ("es", TREATMENT): (26, 40),
        })
        equity = analyze_segments(config, log, "control", TREATMENT)
        by_value = {f.value: f.outcome for f in equity.findings}
        self.assertIs(by_value["es"], SegmentOutcome.TOO_FEW)

    def test_cells_below_the_disclosure_threshold_are_withheld(self):
        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 1000), ("en", TREATMENT): (700, 1000),
            ("zh", "control"): (3, 5), ("zh", TREATMENT): (4, 5),
        })
        equity = analyze_segments(config, log, "control", TREATMENT)
        self.assertEqual(equity.suppressed_count, 1)
        self.assertNotIn("zh", [f.value for f in equity.findings])

    def test_no_declared_segments_yields_no_findings(self):
        data = copy.deepcopy(VALID)
        data["segments"] = []
        config = load_config(data)
        log = build_log(config, {
            ("en", "control"): (600, 1000), ("en", TREATMENT): (700, 1000),
        })
        equity = analyze_segments(config, log, "control", TREATMENT)
        self.assertEqual(equity.findings, ())
        self.assertIn("No segments were declared", equity.summary_line())

    def test_multiplicity_correction_is_applied(self):
        # Eight groups with no real differences should not manufacture a
        # confirmed harm finding.
        config = config_with_segments()
        groups = {}
        for index in range(8):
            groups[(f"g{index}", "control")] = (300, 500)
            groups[(f"g{index}", TREATMENT)] = (300, 500)
        log = build_log(config, groups)
        equity = analyze_segments(config, log, "control", TREATMENT)
        self.assertEqual(equity.harmed, ())


class TestEquityDrivesTheDecision(unittest.TestCase):
    def test_harm_escalates_a_promotion_to_rollback(self):
        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 3000), ("en", TREATMENT): (900, 3000),
            ("es", "control"): (300, 500), ("es", TREATMENT): (180, 500),
        })
        result = analyze(config, log)
        self.assertIs(result.decision, Decision.ROLLBACK)
        self.assertIn("harmed", result.rationale[0])
        self.assertIn("preferred_language=es", result.rationale[0])

    def test_group_left_behind_is_noted_but_still_promotes(self):
        # Not reaching a group is not the same as harming it, and should not
        # block a change that helps everyone else.
        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 3000), ("en", TREATMENT): (900, 3000),
            ("es", "control"): (300, 500), ("es", TREATMENT): (300, 500),
        })
        result = analyze(config, log)
        self.assertIs(result.decision, Decision.PROMOTE)
        self.assertTrue(
            any("did not reach them" in r for r in result.rationale),
            result.rationale,
        )

    def test_equity_appears_in_the_serialized_result(self):
        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 1000), ("en", TREATMENT): (700, 1000),
        })
        payload = analyze(config, log).to_dict()
        self.assertIn("equity", payload)
        self.assertIn("findings", payload["equity"])

    def test_equity_appears_in_the_report(self):
        from civicexp.report import render_markdown

        config = config_with_segments()
        log = build_log(config, {
            ("en", "control"): (600, 1000), ("en", TREATMENT): (700, 1000),
            ("es", "control"): (600, 1000), ("es", TREATMENT): (700, 1000),
        })
        markdown = render_markdown(analyze(config, log))
        self.assertIn("## 5. Did this work for everyone?", markdown)
        self.assertIn("preferred_language=es", markdown)
        self.assertIn("**before** the pilot ran", markdown)


if __name__ == "__main__":
    unittest.main()

"""End-to-end tests covering all five components together.

These are the tests that would catch a regression in how the pieces
interact, as opposed to a regression inside any one of them. Two of them
are property tests over the statistics: run many simulated pilots where the
truth is known, and check the platform's error rates against what its own
confidence level promises.
"""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from civicexp import (
    Decision,
    ExperimentLifecycle,
    analyze,
    load_config,
    render_markdown,
)
from civicexp.errors import LifecycleError
from civicexp.lifecycle import REQUIRED_APPROVALS
from civicexp.simulate import SimulationSpec, simulate_pilot
from civicexp.stats import compare_proportions

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "sf-hsa-document-upload"
ELIGIBLE = {
    "channel": "web",
    "application_type": "new",
    "staff_assisted": False,
    "manual_review_flag": False,
}


def spec(treatment_rate=0.70, treatment_error=0.048, units=8000, seed=1):
    return SimulationSpec(
        unit_attributes=ELIGIBLE,
        completion_rate={"control": 0.62, "plain_language_checklist": treatment_rate},
        error_rate={"control": 0.05, "plain_language_checklist": treatment_error},
        support_rate={"control": 0.06, "plain_language_checklist": 0.06},
        median_seconds={"control": 300.0, "plain_language_checklist": 285.0},
        units=units,
        seed=seed,
    )


class TestFullPipeline(unittest.TestCase):
    def setUp(self):
        self.config = load_config(EXAMPLE_DIR / "experiment.json")

    def test_governed_pilot_runs_start_to_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            lifecycle = ExperimentLifecycle(
                self.config.experiment_id, audit_path=Path(tmp) / "audit.jsonl"
            )
            # Collection is refused before approval.
            with self.assertRaises(LifecycleError):
                lifecycle.require_collecting()

            for role in REQUIRED_APPROVALS:
                lifecycle.sign_off(role, f"{role}-user")
            lifecycle.approve("a.rivera")
            lifecycle.start("m.chen")
            lifecycle.require_collecting()

            log = simulate_pilot(self.config, spec(), path=str(Path(tmp) / "events.jsonl"))
            result = analyze(self.config, log)
            markdown = render_markdown(result, audit=lifecycle.audit)

            lifecycle.complete("m.chen", summary=result.decision.value)
            lifecycle.archive("a.rivera")

            self.assertIs(result.decision, Decision.PROMOTE)
            self.assertIn("## 1. Recommendation", markdown)
            self.assertTrue(lifecycle.audit.verify())

    def test_harmful_change_is_rolled_back_despite_a_winning_primary(self):
        log = simulate_pilot(self.config, spec(treatment_rate=0.70, treatment_error=0.12))
        result = analyze(self.config, log)
        self.assertIs(result.decision, Decision.ROLLBACK)
        self.assertGreater(result.primary.absolute_difference, 0.03)
        self.assertIn("error_rate", [g.name for g in result.breached_guardrails])

    def test_no_real_effect_does_not_promote(self):
        log = simulate_pilot(self.config, spec(treatment_rate=0.62))
        self.assertIsNot(analyze(self.config, log).decision, Decision.PROMOTE)

    def test_pipeline_is_reproducible_from_the_same_seed(self):
        first = analyze(self.config, simulate_pilot(self.config, spec(seed=7)))
        second = analyze(self.config, simulate_pilot(self.config, spec(seed=7)))
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_no_raw_identifier_reaches_the_event_log(self):
        log = simulate_pilot(self.config, spec(units=500))
        for event in log:
            self.assertEqual(len(event.unit_pseudonym), 64)
            self.assertNotIn("synthetic-unit", event.unit_pseudonym)

    def test_report_never_contains_a_pseudonym(self):
        # Aggregated reporting: individual-level identifiers, even
        # pseudonymous ones, must not appear in a published readout.
        log = simulate_pilot(self.config, spec(units=2000))
        markdown = render_markdown(analyze(self.config, log))
        for event in list(log)[:50]:
            self.assertNotIn(event.unit_pseudonym, markdown)

    def test_simulation_with_wrong_attributes_fails_loudly(self):
        from civicexp.errors import ConfigError

        empty = SimulationSpec(
            unit_attributes={},  # does not satisfy the eligibility criteria
            completion_rate={"control": 0.6, "plain_language_checklist": 0.6},
            units=100,
        )
        with self.assertRaises(ConfigError) as ctx:
            simulate_pilot(self.config, empty)
        self.assertEqual(ctx.exception.code, "CONFIG_SIMULATION_EMPTY")


class TestStatisticalProperties(unittest.TestCase):
    """Property tests: does the platform deliver the error rates it claims?"""

    def test_false_positive_rate_is_at_or_below_nominal(self):
        # 400 pilots where the two arms are genuinely identical. At a 95%
        # confidence level, no more than about 5% should show a
        # "statistically detectable" difference. A materially higher rate
        # would mean the platform manufactures findings from noise.
        rng = random.Random(4242)
        trials, false_positives = 400, 0
        for _ in range(trials):
            n, rate = 1200, 0.62
            control = sum(rng.random() < rate for _ in range(n))
            treatment = sum(rng.random() < rate for _ in range(n))
            if compare_proportions(control, n, treatment, n).significant:
                false_positives += 1
        rate = false_positives / trials
        self.assertLess(rate, 0.09, f"false positive rate {rate:.3f} exceeds nominal 0.05")

    def test_confidence_intervals_cover_the_true_effect(self):
        # The coverage guarantee, checked directly: a 95% interval should
        # contain the true difference about 95% of the time.
        rng = random.Random(99)
        trials, covered = 400, 0
        p_control, p_treatment = 0.62, 0.68
        true_difference = p_treatment - p_control
        for _ in range(trials):
            n = 1500
            control = sum(rng.random() < p_control for _ in range(n))
            treatment = sum(rng.random() < p_treatment for _ in range(n))
            low, high = compare_proportions(control, n, treatment, n).difference_interval
            if low <= true_difference <= high:
                covered += 1
        coverage = covered / trials
        self.assertGreater(coverage, 0.90, f"coverage {coverage:.3f} is below nominal 0.95")

    def test_power_calculation_is_honest(self):
        # If the tool says n units per group gives 80% power, then across
        # many pilots at that size roughly 80% should detect the effect.
        from civicexp.stats import required_sample_size_per_group

        rng = random.Random(7)
        baseline, effect = 0.62, 0.05
        n = required_sample_size_per_group(baseline, effect)
        trials, detected = 200, 0
        for _ in range(trials):
            control = sum(rng.random() < baseline for _ in range(n))
            treatment = sum(rng.random() < baseline + effect for _ in range(n))
            if compare_proportions(control, n, treatment, n).significant:
                detected += 1
        power = detected / trials
        self.assertGreater(power, 0.70, f"observed power {power:.2f} far below the 0.80 claim")


class TestShippedArtifacts(unittest.TestCase):
    """The committed example must stay in step with the code."""

    def test_example_events_reproduce_the_committed_decisions(self):
        config = load_config(EXAMPLE_DIR / "experiment.json")
        from civicexp.events import EventLog

        expected = {"a": Decision.PROMOTE, "b": Decision.ROLLBACK}
        for key, decision in expected.items():
            path = EXAMPLE_DIR / f"events-scenario-{key}.jsonl"
            if not path.exists():
                self.skipTest(f"{path.name} has not been generated")
            log = EventLog(
                config.experiment_id,
                path=path,
                privacy=config.privacy,
                allowed_variants=config.variants,
            )
            self.assertIs(analyze(config, log).decision, decision, f"scenario {key}")

    def test_committed_readouts_match_a_fresh_render(self):
        config = load_config(EXAMPLE_DIR / "experiment.json")
        from datetime import datetime, timezone

        from civicexp.audit import AuditLog
        from civicexp.events import EventLog

        audit_path = EXAMPLE_DIR / "audit-log.jsonl"
        if not audit_path.exists():
            self.skipTest("example has not been generated")
        audit = AuditLog(audit_path)
        self.assertTrue(audit.verify(), "committed audit log fails verification")

        for key in ("a", "b"):
            readout = EXAMPLE_DIR / f"readout-scenario-{key}.md"
            events = EXAMPLE_DIR / f"events-scenario-{key}.jsonl"
            if not readout.exists() or not events.exists():
                self.skipTest("example has not been generated")
            log = EventLog(
                config.experiment_id,
                path=events,
                privacy=config.privacy,
                allowed_variants=config.variants,
            )
            rendered = render_markdown(
                analyze(config, log),
                audit=audit,
                generated_at=datetime(2026, 3, 30, 17, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(
                rendered,
                readout.read_text(encoding="utf-8"),
                f"{readout.name} is stale; re-run examples/sf-hsa-document-upload/"
                "run_example.py",
            )


if __name__ == "__main__":
    unittest.main()

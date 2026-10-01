#!/usr/bin/env python3
"""Run the worked example end to end and write the artifacts in this folder.

This script exercises all five platform components in the order an agency
would actually use them, and it is the script that generates every file
checked in beside it. Re-running it reproduces them byte for byte, because
the simulation is seeded and the analysis is deterministic.

    python examples/sf-hsa-document-upload/run_example.py

Two scenarios are generated from the *same* configuration and the same seed,
differing only in the underlying blocking-error rate of the tested version:

* **Scenario A** -- the change helps and nothing else moves. The rule
  promotes it.
* **Scenario B** -- the change helps by the same amount, but blocking errors
  rise past the agreed tolerance. The rule rolls it back anyway.

Scenario B is the point of the example. A platform that only reports the
primary metric would have recommended shipping a change that was quietly
failing more applicants.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from civicexp import (  # noqa: E402
    AuditLog,
    Decision,
    ExperimentLifecycle,
    analyze,
    load_config,
    render_markdown,
    render_text_summary,
)
from civicexp.errors import LifecycleError  # noqa: E402
from civicexp.simulate import SimulationSpec, simulate_pilot  # noqa: E402

# Attributes shared by every synthetic applicant, chosen to satisfy the
# eligibility criteria in experiment.json.
ELIGIBLE = {
    "channel": "web",
    "application_type": "new",
    "staff_assisted": False,
    "manual_review_flag": False,
}

# Coded, non-identifying attributes recorded at assignment time. These are
# what the equity review groups by.
SEGMENT_MIX = {
    "preferred_language": {"en": 0.72, "es": 0.21, "zh": 0.07},
    "device_type": {"mobile": 0.58, "desktop": 0.42},
}

TREATMENT = "plain_language_checklist"

SCENARIOS = {
    "a": {
        "label": "Scenario A: the change works",
        "treatment_error": 0.048,
        "overrides": {},
        "note": "Blocking errors are unchanged and every group benefits.",
    },
    "b": {
        "label": "Scenario B: the change works, but causes harm",
        "treatment_error": 0.095,
        "overrides": {},
        "note": "Blocking errors nearly double.",
    },
    "c": {
        "label": "Scenario C: the change works on average, for some people",
        "treatment_error": 0.048,
        # The checklist was written in English. The translated flow still
        # shows the old paragraph, so Spanish-preference applicants see no
        # benefit at all -- and the aggregate still looks like a clear win.
        "overrides": {"preferred_language=es": {"control": 0.62, TREATMENT: 0.62}},
        "note": "The English flow improves; the Spanish flow is untouched.",
    },
}


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def fixed_clock():
    """A deterministic clock for the audit trail.

    Real deployments leave this unset and get wall-clock time. The example
    pins it so that every artifact committed in this folder -- the audit
    log and the readouts that embed it -- is byte-for-byte reproducible,
    which is what lets CI detect when they drift from what the code
    actually produces.
    """
    moment = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)

    def tick() -> datetime:
        nonlocal moment
        moment += timedelta(minutes=5)
        return moment

    return tick


def governance_walkthrough(config) -> ExperimentLifecycle:
    """Component 5: approve the evaluation, with an auditable record."""
    audit_path = HERE / "audit-log.jsonl"
    audit_path.unlink(missing_ok=True)
    lifecycle = ExperimentLifecycle(
        config.experiment_id, audit_path=audit_path, clock=fixed_clock()
    )

    print(f"State: {lifecycle.state.value}")

    # Nothing may collect data before approval.
    try:
        lifecycle.require_collecting()
    except LifecycleError as exc:
        print(f"Collection blocked, as expected: {exc.message}")

    # Launching without every sign-off is refused, not merely warned about.
    lifecycle.sign_off("program_owner", "a.rivera", "Scope is limited to on-screen text.")
    lifecycle.sign_off("operations_lead", "m.chen", "Support team briefed.")
    try:
        lifecycle.approve("a.rivera")
    except LifecycleError as exc:
        print(f"Approval refused, as expected: {exc.message}")

    lifecycle.sign_off("privacy_reviewer", "s.okafor", "No PII collected; 90-day retention.")
    lifecycle.sign_off("legal_policy_reviewer", "d.whitfield", "No policy change.")
    lifecycle.sign_off("evaluation_lead", "devdipta.pal", "Powered for a 3pt change.")

    lifecycle.approve("a.rivera")
    lifecycle.start("m.chen")
    print(f"State: {lifecycle.state.value} (collecting: {lifecycle.is_collecting})")

    # A pause mid-pilot, as would happen during an unrelated outage.
    lifecycle.pause("m.chen", reason="Unrelated upload-service outage; data would be biased.")
    print(f"State: {lifecycle.state.value} — {lifecycle.audit.entries[-1].detail['reason']}")
    lifecycle.resume("m.chen")
    print(f"State: {lifecycle.state.value}")
    return lifecycle


def main() -> int:
    config = load_config(HERE / "experiment.json")

    banner("Pre-launch: is this pilot worth running?")
    print(f"Experiment: {config.name}")
    print(f"Primary:    {config.primary_metric.name}")
    print(f"Worth acting on: {config.primary_metric.minimum_effect_of_interest:.1%}")
    print(f"Power check: {config.power_note()}")
    for warning in config.warnings:
        print(f"warning: {warning}")

    banner("Components 5: approve, pause, and resume the evaluation")
    lifecycle = governance_walkthrough(config)

    results = {}
    for key, scenario in SCENARIOS.items():
        banner(f"{scenario['label']} — {scenario['note']}")
        events_path = HERE / f"events-scenario-{key}.jsonl"
        events_path.unlink(missing_ok=True)

        # Components 1 and 2: assign eligible units, record their outcomes.
        spec = SimulationSpec(
            unit_attributes=ELIGIBLE,
            segment_distribution=SEGMENT_MIX,
            completion_rate={"control": 0.62, TREATMENT: 0.70},
            error_rate={"control": 0.05, TREATMENT: scenario["treatment_error"]},
            support_rate={"control": 0.06, TREATMENT: 0.06},
            median_seconds={"control": 300.0, TREATMENT: 285.0},
            completion_overrides=scenario["overrides"],
            units=14000,
            seed=20260904,
        )
        log = simulate_pilot(config, spec, path=str(events_path))
        print(f"Recorded {len(log):,} outcome events for 14,000 synthetic applicants.")

        # Component 3: compare the versions and apply the pre-registered rule.
        result = analyze(config, log)
        results[key] = result
        print()
        print(render_text_summary(result))
        for diagnostic in result.diagnostics:
            print(f"  [{diagnostic.severity.value:>8}] {diagnostic.name}: {diagnostic.summary}")
        for guardrail in result.guardrails:
            print(f"  [{guardrail.status:>8}] {guardrail.name}: {guardrail.note}")
        print(f"  equity: {result.equity.summary_line()}")

    banner("Closing the evaluation")
    # The decision rule, not the operator's preference, ends the pilot.
    decision = results["b"].decision
    if decision is Decision.ROLLBACK:
        lifecycle.roll_back(
            "m.chen",
            reason="error_rate breached its 1.0 point tolerance under scenario B.",
        )
    else:
        lifecycle.complete("m.chen", summary=decision.value)
    lifecycle.archive("a.rivera")

    # Component 4: the readouts an agency circulates. Written last, so that
    # the approval and decision record each one embeds is the completed
    # trail -- which is also what a readout attached to a closed pilot
    # should contain.
    for key, result in results.items():
        report_path = HERE / f"readout-scenario-{key}.md"
        report_path.write_text(
            render_markdown(
                result,
                audit=lifecycle.audit,
                # Fixed so the committed report is byte-for-byte reproducible.
                generated_at=datetime(2026, 3, 30, 17, 0, tzinfo=timezone.utc),
            ),
            encoding="utf-8",
        )
        print(f"Wrote {report_path.name}")

    verification = lifecycle.audit.verify()
    print(f"Final state:  {lifecycle.state.value}")
    print(f"Audit entries: {len(lifecycle.audit)}")
    print(f"Chain intact:  {verification.valid} ({verification.message})")
    print(f"Head digest:   {lifecycle.audit.head}")

    # Demonstrate that tampering is detectable.
    banner("Tamper check")
    raw = (HERE / "audit-log.jsonl").read_text().splitlines()
    altered = list(raw)
    target = next(i for i, line in enumerate(raw) if '"privacy_reviewer"' in line)
    altered[target] = altered[target].replace('"privacy_reviewer"', '"program_owner"')
    assert altered != raw, "tamper demo must actually change a line"
    print(f"Rewriting entry {target}: the privacy reviewer's sign-off.")
    scratch = HERE / ".tampered-audit.jsonl"
    scratch.write_text("\n".join(altered) + "\n", encoding="utf-8")
    tampered = AuditLog(scratch).verify()
    print(f"After editing one approval record: valid={tampered.valid}")
    print(f"  {tampered.message}")
    scratch.unlink()

    summary = " | ".join(
        f"{key.upper()} -> {result.decision.value.upper()}"
        for key, result in results.items()
    )
    banner(summary)
    print(
        "One configuration, three scenarios, three different answers.\n\n"
        "A and B measure the same improvement in completion. B is rolled back\n"
        "because blocking errors breached the agreed tolerance.\n\n"
        "C is the one most tools would get wrong. Completion rises, no guardrail\n"
        "moves, and every headline number says ship it -- but the benefit reaches\n"
        "English-preference applicants only, because the checklist was never\n"
        "translated. Reporting only the average would have widened an access gap\n"
        "and called it a success."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

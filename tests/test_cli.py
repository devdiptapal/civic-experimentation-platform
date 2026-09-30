"""Tests for the command-line interface, including exit-code contracts."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from civicexp.cli import EXIT_ATTENTION, EXIT_ERROR, EXIT_OK, main
from tests.test_config import VALID

ELIGIBLE = json.dumps({"channel": "web"})
EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "sf-hsa-document-upload"
    / "experiment.json"
)
EXAMPLE_ATTRS = json.dumps(
    {
        "channel": "web",
        "application_type": "new",
        "staff_assisted": False,
        "manual_review_flag": False,
    }
)


def run(*argv):
    """Run the CLI, capturing stdout, stderr, and the exit code."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CLITestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config_path = self.tmp / "experiment.json"
        self.config_path.write_text(json.dumps(VALID), encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)


class TestValidate(CLITestCase):
    def test_valid_config_exits_zero(self):
        code, out, _ = run("validate", str(self.config_path))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Configuration is valid", out)

    def test_config_with_warnings_exits_with_attention(self):
        data = json.loads(self.config_path.read_text())
        data["sample_size"]["expected_units_per_group"] = 50
        self.config_path.write_text(json.dumps(data), encoding="utf-8")
        code, _, err = run("validate", str(self.config_path))
        self.assertEqual(code, EXIT_ATTENTION)
        self.assertIn("Underpowered", err)

    def test_invalid_config_exits_with_error(self):
        self.config_path.write_text('{"experiment": {}}', encoding="utf-8")
        code, _, err = run("validate", str(self.config_path))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("error:", err)

    def test_missing_file_exits_with_error(self):
        code, _, err = run("validate", str(self.tmp / "nope.json"))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("not found", err)

    def test_shipped_example_validates_cleanly(self):
        code, out, _ = run("validate", str(EXAMPLE))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Adequately powered", out)


class TestPower(CLITestCase):
    def test_standalone_calculation(self):
        code, out, _ = run("power", "--baseline", "0.6", "--effect", "0.05")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Units needed per group", out)

    def test_reads_parameters_from_a_config(self):
        code, out, _ = run("power", "--config", str(self.config_path))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("62.0%", out)

    def test_flags_an_underpowered_plan(self):
        code, out, _ = run(
            "power", "--baseline", "0.6", "--effect", "0.03", "--available", "100"
        )
        self.assertEqual(code, EXIT_ATTENTION)
        self.assertIn("underpowered", out)

    def test_confirms_an_adequately_powered_plan(self):
        code, out, _ = run(
            "power", "--baseline", "0.6", "--effect", "0.05", "--available", "5000"
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("adequately powered", out)

    def test_missing_parameters_are_reported(self):
        code, _, err = run("power")
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("baseline", err)


class TestPreviewAndAssign(CLITestCase):
    def test_preview_shows_the_split(self):
        code, out, _ = run(
            "preview", str(self.config_path), "--units", "2000", "--attributes", ELIGIBLE
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("control", out)
        self.assertIn("treatment", out)

    def test_preview_without_attributes_explains_the_zero_split(self):
        code, _, err = run("preview", str(self.config_path), "--units", "100")
        self.assertEqual(code, EXIT_ATTENTION)
        self.assertIn("ineligible", err)
        self.assertIn("--attributes", err)

    def test_assign_is_stable_across_invocations(self):
        first = run("assign", str(self.config_path), "u-1", "--attributes", ELIGIBLE)[1]
        second = run("assign", str(self.config_path), "u-1", "--attributes", ELIGIBLE)[1]
        self.assertEqual(first, second)

    def test_assign_json_output_carries_no_raw_identifier(self):
        code, out, _ = run(
            "assign", str(self.config_path), "applicant-secret-1",
            "--attributes", ELIGIBLE, "--json",
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertNotIn("secret", json.dumps(payload))
        self.assertEqual(len(payload["unit_pseudonym"]), 64)

    def test_assign_explains_an_exclusion(self):
        attrs = json.dumps({"channel": "phone"})
        code, out, _ = run("assign", str(self.config_path), "u-1", "--attributes", attrs)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("not_eligible", out)


class TestSimulateAnalyzeReport(CLITestCase):
    def simulate(self, **overrides):
        events = self.tmp / "events.jsonl"
        argv = [
            "simulate", str(EXAMPLE), "--out", str(events),
            "--units", "4000", "--attributes", EXAMPLE_ATTRS,
        ]
        for key, value in overrides.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        code, _, _ = run(*argv)
        self.assertEqual(code, EXIT_OK)
        return events

    def test_simulate_writes_events(self):
        events = self.simulate()
        self.assertTrue(events.exists())
        self.assertGreater(len(events.read_text().splitlines()), 1000)

    def test_analyze_reports_a_decision(self):
        events = self.simulate(control_rate=0.62, treatment_rate=0.75)
        code, out, _ = run("analyze", str(EXAMPLE), str(events))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("PROMOTE", out)

    def test_breached_guardrail_exits_with_attention(self):
        # The exit-code contract that lets monitoring distinguish a breach
        # from a broken run.
        events = self.simulate(
            control_rate=0.62, treatment_rate=0.75,
            control_error=0.03, treatment_error=0.15,
        )
        code, out, _ = run("analyze", str(EXAMPLE), str(events))
        self.assertEqual(code, EXIT_ATTENTION)
        self.assertIn("ROLLBACK", out)

    def test_analyze_json_is_machine_readable(self):
        events = self.simulate()
        code, out, _ = run("analyze", str(EXAMPLE), str(events), "--json")
        payload = json.loads(out)
        self.assertIn("decision", payload)
        self.assertIn("guardrails", payload)

    def test_report_writes_markdown_to_a_file(self):
        events = self.simulate()
        report = self.tmp / "readout.md"
        code, _, _ = run("report", str(EXAMPLE), str(events), "--out", str(report))
        self.assertIn(code, (EXIT_OK, EXIT_ATTENTION))
        self.assertIn("## 1. Recommendation", report.read_text())

    def test_analyze_on_an_empty_log_errors(self):
        empty = self.tmp / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        code, _, err = run("analyze", str(EXAMPLE), str(empty))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("no events", err)


class TestVerifyAndSalt(CLITestCase):
    def test_verify_accepts_an_intact_chain(self):
        from civicexp.audit import AuditLog

        path = self.tmp / "audit.jsonl"
        log = AuditLog(path)
        log.append("state:draft->approved", "a")
        code, out, _ = run("verify", str(path))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("intact", out)

    def test_verify_rejects_a_tampered_chain(self):
        from civicexp.audit import AuditLog

        path = self.tmp / "audit.jsonl"
        log = AuditLog(path)
        log.append("approval:sign_off", "a", role="privacy_reviewer")
        log.append("state:draft->approved", "a")
        path.write_text(
            path.read_text().replace("privacy_reviewer", "program_owner"), encoding="utf-8"
        )
        code, _, err = run("verify", str(path))
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("INVALID", err)

    def test_salt_generates_distinct_hex_values(self):
        first = run("salt")[1].strip()
        second = run("salt")[1].strip()
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 32)
        int(first, 16)

    def test_generated_salt_is_accepted_by_the_validator(self):
        # Round trip: the salt command must not produce something the
        # placeholder check rejects.
        from civicexp.config import load_config

        data = json.loads(self.config_path.read_text())
        data["assignment"]["salt"] = run("salt")[1].strip()
        self.assertTrue(load_config(data).salt)


class TestParser(unittest.TestCase):
    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_missing_subcommand_is_an_error(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            main([])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()

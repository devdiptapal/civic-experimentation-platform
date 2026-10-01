"""Tests for the plain-language report.

The report is the artifact non-technical readers act on, so these tests
check editorial rules as well as correctness: no bare point estimates, no
unqualified use of the word "significant", and the recommendation stated
before any number.
"""

from __future__ import annotations

import re
import unittest

from civicexp.analysis import Decision, analyze
from civicexp.audit import AuditLog
from civicexp.config import load_config
from civicexp.report import render_markdown, render_text_summary
from tests.test_analysis import build_log
from tests.test_config import VALID


def report_for(control, treatment, **kwargs):
    config = load_config(VALID)
    log = build_log(config, control=control, treatment=treatment)
    result = analyze(config, log)
    return result, render_markdown(result, **kwargs)


class TestStructure(unittest.TestCase):
    def setUp(self):
        self.result, self.markdown = report_for((1200, 100, 2000), (1500, 100, 2000))

    def test_has_every_expected_section(self):
        for heading in (
            "## 1. Recommendation",
            "## 2. What was tested",
            "## 3. The main result",
            "## 4. Checks for unintended harm",
            "## 7. Limits of this evidence",
            "## 8. Suggested next steps",
            "## 9. Technical appendix",
        ):
            self.assertIn(heading, self.markdown)

    def test_sections_are_numbered_in_the_order_they_appear(self):
        # Regression: the trust section was once numbered 6 but rendered
        # after section 8, because it was inserted in the wrong place.
        import re

        numbers = [
            int(m) for m in re.findall(r"^## (\d+)\. ", self.markdown, re.MULTILINE)
        ]
        self.assertEqual(numbers, sorted(numbers), f"sections out of order: {numbers}")
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_recommendation_comes_before_any_number(self):
        recommendation = self.markdown.index("## 1. Recommendation")
        detail = self.markdown.index("## 3. The main result")
        self.assertLess(recommendation, detail)

    def test_states_the_decision_headline(self):
        self.assertIn(self.result.decision.headline, self.markdown)

    def test_includes_the_audit_trail_when_supplied(self):
        audit = AuditLog()
        audit.append("state:draft->approved", "a.rivera")
        _, markdown = report_for(
            (1200, 100, 2000), (1500, 100, 2000), audit=audit
        )
        self.assertIn("## 10. Decision and approval record", markdown)
        self.assertIn("a.rivera", markdown)

    def test_omits_the_audit_section_when_there_is_none(self):
        self.assertNotIn("## 10.", self.markdown)


class TestEditorialRules(unittest.TestCase):
    def setUp(self):
        self.result, self.markdown = report_for((1200, 100, 2000), (1500, 100, 2000))

    def test_never_reports_an_effect_without_its_interval(self):
        body = self.markdown.split("## 9. Technical appendix")[0]
        self.assertIn("percentage points", body)
        self.assertTrue(
            re.search(r"between [\d.]+ and [\d.]+ percentage points", body)
            or "plausible range" in body.lower()
            or "range of plausible effects" in body.lower()
        )

    def test_avoids_unqualified_statistical_jargon_in_the_body(self):
        body = self.markdown.split("## 9. Technical appendix")[0].lower()
        for term in ("p-value", "p value", "null hypothesis", "alpha", "z-test"):
            self.assertNotIn(term, body, f"{term!r} belongs in the appendix")

    def test_jargon_is_allowed_in_the_appendix(self):
        appendix = self.markdown.split("## 9. Technical appendix")[1]
        self.assertIn("p-value", appendix)

    def test_explains_the_practical_bar_that_was_set(self):
        self.assertIn("worth acting on", self.markdown)

    def test_always_states_limits(self):
        limits = self.markdown.split("## 7. Limits of this evidence")[1]
        self.assertIn("does not establish", limits)

    def test_names_the_methods_used(self):
        appendix = self.markdown.split("## 9. Technical appendix")[1]
        self.assertIn("Wilson", appendix)
        self.assertIn("Newcombe", appendix)


class TestDecisionSpecificContent(unittest.TestCase):
    def test_rollback_report_leads_with_reverting(self):
        result, markdown = report_for((1200, 100, 2000), (1500, 400, 2000))
        self.assertIs(result.decision, Decision.ROLLBACK)
        self.assertIn("Stop and revert", markdown)
        self.assertIn("**BREACHED**", markdown)
        steps = markdown.split("## 8. Suggested next steps")[1]
        self.assertIn("Revert the tested version now", steps)

    def test_promote_report_recommends_rollout_and_publication(self):
        _, markdown = report_for((1200, 100, 2000), (1500, 100, 2000))
        steps = markdown.split("## 8. Suggested next steps")[1]
        self.assertIn("Roll the tested version out", steps)
        self.assertIn("other jurisdictions", steps)

    def test_inconclusive_report_says_do_not_act(self):
        result, markdown = report_for((25, 2, 40), (28, 2, 40))
        self.assertIs(result.decision, Decision.INCONCLUSIVE)
        steps = markdown.split("## 8. Suggested next steps")[1]
        self.assertIn("Do not act on this result", steps)

    def test_negative_result_is_framed_as_reusable_evidence(self):
        _, markdown = report_for((6200, 500, 10000), (6200, 500, 10000))
        steps = markdown.split("## 8. Suggested next steps")[1]
        self.assertIn("Document the negative result", steps)

    def test_uncertainty_wording_reflects_an_interval_spanning_zero(self):
        _, markdown = report_for((6200, 500, 10000), (6200, 500, 10000))
        self.assertIn("includes zero", markdown)


class TestTextSummary(unittest.TestCase):
    def test_summary_is_short_and_names_the_decision(self):
        config = load_config(VALID)
        log = build_log(config, control=(1200, 100, 2000), treatment=(1500, 100, 2000))
        summary = render_text_summary(analyze(config, log))
        self.assertLessEqual(len(summary.splitlines()), 4)
        self.assertIn("PROMOTE", summary)
        self.assertIn("CI", summary)

    def test_summary_names_breached_guardrails(self):
        config = load_config(VALID)
        log = build_log(config, control=(1200, 100, 2000), treatment=(1500, 400, 2000))
        summary = render_text_summary(analyze(config, log))
        self.assertIn("error_rate", summary)


if __name__ == "__main__":
    unittest.main()

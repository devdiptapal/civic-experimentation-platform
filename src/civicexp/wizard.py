"""An interview that produces a valid experiment configuration.

The project roadmap sets a target of being usable with **less than two hours
of onboarding** by staff who are not data scientists. Hand-authoring a JSON
configuration does not meet that bar: it requires knowing the event
taxonomy, the metric definitions, and what a minimum detectable effect is
before writing the first line.

This module asks for the handful of things only the agency knows -- what is
being changed, how many people go through the workflow, what must not get
worse -- and derives everything else. The output is a configuration that
passes ``civicexp validate``, and the questions double as the evaluation
planning conversation the pilot needs to have anyway.

Three decisions keep the interview honest rather than merely short:

* **It refuses to invent the effect of interest.** The smallest improvement
  worth acting on is a program judgment, not a technical default. The wizard
  explains what the number means, offers the standard metric's starting
  point for discussion, and requires an answer.
* **It runs the power check during the interview**, not afterwards. If the
  traffic the agency has cannot detect the effect it says it cares about,
  the right time to find out is while the plan is still a conversation.
* **It never writes a salt the user supplies.** One is generated.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .metrics import STANDARD_METRICS
from .stats import minimum_detectable_effect, required_sample_size_per_group

__all__ = ["TEMPLATES", "load_template", "list_templates", "run_wizard"]

TEMPLATES: Mapping[str, str] = {
    "document-upload": "document_upload.json",
    "appointment-reminder": "appointment_reminder.json",
    "digital-intake": "digital_intake.json",
}

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def load_template(name: str) -> dict[str, Any]:
    """Load a workflow template by its short name."""
    if name not in TEMPLATES:
        available = ", ".join(sorted(TEMPLATES))
        raise KeyError(f"unknown template {name!r}; available: {available}")
    return json.loads((_TEMPLATE_DIR / TEMPLATES[name]).read_text(encoding="utf-8"))


def list_templates() -> list[tuple[str, dict[str, str]]]:
    """Every template's short name and its descriptive header."""
    out = []
    for name in sorted(TEMPLATES):
        out.append((name, load_template(name)["_template"]))
    return out


# ---------------------------------------------------------------------------
# Interview primitives
# ---------------------------------------------------------------------------


class _Asker:
    """Prompting that tolerates the ways people actually answer."""

    def __init__(self, prompt: Callable[[str], str], echo: Callable[[str], None]):
        self._prompt = prompt
        self._echo = echo

    def say(self, text: str = "") -> None:
        self._echo(text)

    def text(self, question: str, *, default: str | None = None, help: str = "") -> str:
        if help:
            self._echo(f"  {help}")
        suffix = f" [{default}]" if default else ""
        while True:
            answer = self._prompt(f"{question}{suffix}: ").strip()
            if answer:
                return answer
            if default is not None:
                return default
            self._echo("  An answer is needed here.")

    def number(
        self,
        question: str,
        *,
        default: float | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        help: str = "",
    ) -> float:
        if help:
            self._echo(f"  {help}")
        suffix = f" [{default}]" if default is not None else ""
        while True:
            raw = self._prompt(f"{question}{suffix}: ").strip().rstrip("%")
            if not raw and default is not None:
                return default
            try:
                value = float(raw)
            except ValueError:
                self._echo("  Please enter a number.")
                continue
            if minimum is not None and value < minimum:
                self._echo(f"  Needs to be at least {minimum}.")
                continue
            if maximum is not None and value > maximum:
                self._echo(f"  Needs to be at most {maximum}.")
                continue
            return value

    def percent(self, question: str, *, default: float | None = None, help: str = "") -> float:
        """Accept 62, 62%, or 0.62 and return a proportion."""
        if help:
            self._echo(f"  {help}")
        shown = f" [{default * 100:g}%]" if default is not None else ""
        while True:
            raw = self._prompt(f"{question}{shown}: ").strip().rstrip("%")
            if not raw and default is not None:
                return default
            try:
                value = float(raw)
            except ValueError:
                self._echo("  Please enter a percentage, for example 62 or 62%.")
                continue
            proportion = value / 100.0 if value > 1 else value
            if not 0 < proportion < 1:
                self._echo("  Needs to be between 0 and 100 percent.")
                continue
            return proportion

    def choose(self, question: str, options: list[tuple[str, str]]) -> str:
        self._echo(f"{question}")
        for index, (key, description) in enumerate(options, start=1):
            self._echo(f"  {index}. {key} — {description}")
        while True:
            raw = self._prompt("Choose a number: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            for key, _ in options:
                if raw.lower() == key.lower():
                    return key
            self._echo(f"  Please choose 1 to {len(options)}.")

    def yes_no(self, question: str, *, default: bool = True) -> bool:
        suffix = " [Y/n]" if default else " [y/N]"
        while True:
            raw = self._prompt(f"{question}{suffix}: ").strip().lower()
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            self._echo("  Please answer yes or no.")


# ---------------------------------------------------------------------------
# The interview
# ---------------------------------------------------------------------------


def run_wizard(
    *,
    prompt: Callable[[str], str] = input,
    echo: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Interview the user and return a complete experiment configuration."""
    ask = _Asker(prompt, echo)

    ask.say()
    ask.say("=" * 70)
    ask.say("  Set up an evaluation")
    ask.say("=" * 70)
    ask.say()
    ask.say("Nine questions. Nothing is sent anywhere, and nothing runs until")
    ask.say("your reviewers have signed off. You can edit the result afterwards.")
    ask.say()

    # 1. Which workflow.
    options = [(name, header["summary"]) for name, header in list_templates()]
    template_name = ask.choose("1. Which part of the service are you changing?", options)
    config = load_template(template_name)
    header = config.pop("_template")
    ask.say()
    ask.say(f"  Using the {header['title'].lower()} template.")
    ask.say(f"  Good for: {header['good_for']}")
    ask.say()

    # 2. Name and description.
    name = ask.text(
        "2. What would you call this evaluation?",
        help="A short name your team will recognize in a list.",
    )
    config["experiment"]["name"] = name
    config["experiment"]["id"] = _slug(name)
    ask.say()

    description = ask.text(
        "3. In one or two sentences, what is changing?",
        help="What people see today, what they will see instead. Plain language.",
    )
    config["experiment"]["description"] = description
    ask.say()

    team = ask.text("4. Which team owns this workflow?", default="digital-services")
    config["experiment"]["owner_team"] = team
    jurisdiction = ask.text("5. Which agency or jurisdiction?", default="our-agency")
    config["scope"]["jurisdictions"] = [jurisdiction]
    ask.say()

    # 6. Baseline.
    metric_name = config["metrics"]["primary"].get("use", "completion_rate")
    standard = STANDARD_METRICS.get(metric_name)
    if standard:
        ask.say(f"Your main measure is {standard.name}:")
        ask.say(f"  {standard.definition}")
        ask.say()
    baseline = ask.percent(
        "6. Out of everyone who starts, what share finishes today?",
        default=config["sample_size"]["baseline_rate"],
        help="Your current rate. A rough figure from last month is fine.",
    )
    config["sample_size"]["baseline_rate"] = round(baseline, 4)
    ask.say()

    # 7. The effect of interest -- never defaulted silently.
    ask.say("7. How big an improvement would be worth making this change permanent?")
    ask.say()
    ask.say("  This is the most important answer here, and it is a program")
    ask.say("  judgment rather than a technical one. Results are reported against")
    ask.say("  this number: a change smaller than it will not be recommended for")
    ask.say("  adoption even if it is real. Think about the staff time, the")
    ask.say("  retraining, and the risk of changing a live service, and ask what")
    ask.say("  improvement would be worth all of that.")
    ask.say()
    if standard:
        ask.say(
            f"  Teams often start the discussion around "
            f"{standard.typical_effect_of_interest:.0%} for this measure. That is a"
        )
        ask.say("  starting point for your conversation, not a recommendation.")
    effect = ask.percent(
        "   Smallest improvement worth acting on (percentage points)",
        help="For example, 3 means going from 62% to 65%.",
    )
    config["metrics"]["primary"]["minimum_effect_of_interest"] = round(effect, 4)
    ask.say()

    # 8. Volume, with the power check run live.
    monthly = ask.number(
        "8. Roughly how many people go through this workflow each month?",
        minimum=1,
        help="A rough figure is fine. This decides whether the pilot can work.",
    )
    duration = ask.number(
        "   How many days do you want to run for?",
        default=28,
        minimum=1,
        help="Four weeks is typical. Covering whole weeks avoids weekday effects.",
    )
    per_group = int(monthly * (duration / 30.0) / 2)
    config["sample_size"]["expected_units_per_group"] = max(per_group, 1)
    config["sample_size"]["planned_duration_days"] = int(duration)
    ask.say()

    _report_power(ask, baseline, effect, per_group, duration, monthly)

    # 9. Equity segments.
    ask.say()
    ask.say("9. Which groups should be checked separately for unequal impact?")
    ask.say()
    ask.say("  An overall improvement can hide a group the change made worse.")
    ask.say("  These must be chosen now, before any results exist: looking for a")
    ask.say("  subgroup afterwards will always find one.")
    ask.say()
    segments = list(config.get("segments", []))
    ask.say(f"  Suggested: {', '.join(segments)}")
    if not ask.yes_no("   Use these?", default=True):
        raw = ask.text(
            "   Which attributes instead? (comma separated)",
            help="Coded, non-identifying attributes you already record.",
        )
        segments = [part.strip() for part in raw.split(",") if part.strip()]
    config["segments"] = segments

    # Generated, never asked for.
    config["assignment"]["salt"] = secrets.token_hex(16)

    ask.say()
    ask.say("=" * 70)
    ask.say("  Done")
    ask.say("=" * 70)
    ask.say()
    ask.say("An assignment salt was generated for you. Treat the config file as a")
    ask.say("secret: that salt is what keeps applicant identifiers pseudonymous.")
    ask.say()
    ask.say("Still to fill in by hand, because only your team can answer them:")
    ask.say("  - variants.treatment.description — what the new version actually says")
    ask.say("  - eligibility — confirm the include/exclude rules match your records")
    ask.say("  - hypothesis.statement — what you expect and why")
    ask.say()
    return config


def _report_power(ask: _Asker, baseline, effect, per_group, duration, monthly) -> None:
    """Tell the user now whether their pilot can answer its question."""
    if per_group < 1:
        ask.say("  Not enough volume to evaluate this workflow at all.")
        return

    detectable = minimum_detectable_effect(baseline, per_group, looks=4)
    ask.say(f"  That gives about {per_group:,} people per group over {int(duration)} days.")
    ask.say()
    if detectable <= effect:
        ask.say(f"  This pilot can detect a change of about {detectable:.1%},")
        ask.say(f"  and you said you care about {effect:.1%}. That works.")
        return

    needed = required_sample_size_per_group(baseline, effect, looks=4)
    days_needed = int(needed * 2 / (monthly / 30.0)) if monthly else 0
    ask.say("  WARNING: this pilot is too small to answer the question.")
    ask.say()
    ask.say(f"  The smallest change it could reliably detect is about {detectable:.1%},")
    ask.say(f"  but you said you care about {effect:.1%}. Run as planned and the")
    ask.say("  likely result is 'no conclusion', after spending the full effort.")
    ask.say()
    ask.say("  Options:")
    ask.say(f"    - run for about {days_needed} days instead of {int(duration)}")
    ask.say(f"    - widen who is eligible ({needed * 2:,} people total are needed)")
    ask.say("    - test a bolder change that would produce a larger effect")
    ask.say()
    ask.say("  The config will be written either way, and 'civicexp validate'")
    ask.say("  will keep reminding you until it is resolved.")


def _slug(name: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in name)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return f"exp-{cleaned.strip('-')[:40]}"

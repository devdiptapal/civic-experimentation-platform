"""Detect: alerts that tell a team when an evaluation needs a person.

A dashboard shows what happened to whoever opens it. A pilot that is
quietly harming applicants needs someone to be told, which is what this
module is for: it checks every evaluation in a workspace and returns what
needs attention, most serious first, each with the action to take.

It runs inside the web application (the inbox) and on its own from a
scheduler (``civicexp monitor``), which can post to a chat webhook and exits
with status 2 when anything needs a person -- so a cron job can page on the
exit code without parsing anything.

Alerts never reveal a sealed result. While a pilot runs, they report harm
(guardrails, trust checks, confirmed equity harms), data problems, and
governance problems. Whether the change is *working* waits for a recorded
look or the end of the pilot.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import CivicExpError, ConfigError, LifecycleError
from .lifecycle import State

__all__ = ["Alert", "SEVERITIES", "check_experiment", "check_workspace", "format_alerts",
           "post_webhook"]

#: Severity levels, most serious first.
SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")

_ROLE_WORDS = {
    "program_owner": "the program owner",
    "operations_lead": "the operations lead",
    "privacy_reviewer": "the privacy reviewer",
    "legal_policy_reviewer": "the legal and policy reviewer",
    "evaluation_lead": "the evaluation lead",
}


@dataclass(frozen=True)
class Alert:
    """One thing that needs a person, and what they should do about it."""

    experiment_id: str
    experiment_name: str
    severity: str
    kind: str
    title: str
    detail: str
    action: str

    @property
    def rank(self) -> int:
        return SEVERITIES.index(self.severity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "severity": self.severity,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "action": self.action,
        }


def check_experiment(record: Any, *, now: datetime | None = None,
                     stale_hours: float = 26) -> list[Alert]:
    """Everything about one evaluation that needs attention."""
    now = now or datetime.now(timezone.utc)
    alerts: list[Alert] = []
    try:
        name = record.config().name
    except ConfigError as exc:
        return [Alert(record.id, record.id, "critical", "plan_invalid",
                      "The plan on file no longer loads",
                      exc.message,
                      "Restore the approved plan from your records. Collection stops "
                      "until the plan loads again.")]

    def add(severity: str, kind: str, title: str, detail: str, action: str) -> None:
        alerts.append(Alert(record.id, name, severity, kind, title, detail, action))

    status = record.governance()
    if not status.audit_valid:
        add("critical", "audit_broken", "Governance record failed verification",
            status.audit_message,
            "Collection has been halted. Compare the record with your backup and the "
            "last published head digest, and do not resume until the cause is known.")
        return alerts
    state = status.state
    if status.plan_matches_approval is False and state in (
        State.APPROVED, State.RUNNING, State.PAUSED
    ):
        add("critical", "plan_changed", "The plan changed after approval",
            "The configuration on file is not the one the reviewers approved, so "
            "collection is halted.",
            "Reopen the evaluation so the reviewers can sign off on the current plan, "
            "or restore the approved version.")

    lifecycle = record.lifecycle()
    if state is State.DRAFT:
        unfinished = record.unfinished()
        missing = list(lifecycle.missing_approvals)
        stale = [r for r in lifecycle.sign_offs if record.sign_off_is_stale(r)]
        if unfinished:
            add("info", "plan_unfinished", "The plan is not finished",
                f"{len(unfinished)} field(s) still say FILL IN: {', '.join(unfinished)}.",
                "Complete the plan so reviewers can sign off on it.")
        elif missing or stale:
            waiting = sorted(set(missing) | set(stale))
            add("info", "awaiting_sign_off", "Waiting for sign-off",
                f"Still needed from: {', '.join(_ROLE_WORDS.get(r, r) for r in waiting)}.",
                "Each reviewer opens the plan and records their sign-off.")
        else:
            add("info", "ready_to_approve", "Ready for approval",
                "Every reviewer has signed off on the current plan.",
                "The program owner approves the plan.")
    elif state is State.APPROVED and status.plan_matches_approval is not False:
        add("info", "ready_to_start", "Approved and ready to start",
            "The plan is approved. Nothing is collected until it is started.",
            "The operations lead starts the pilot once the service is ready.")

    if state in (State.RUNNING, State.PAUSED):
        alerts.extend(_running_alerts(record, name, state, lifecycle, now, stale_hours))

    if state is not None and state.is_terminal:
        due = record.retention_due()
        if due is not None and now >= due and (
            record.events_path.exists() or record.dry_run_path.exists()
        ):
            add("warning", "retention_due", "Event data is past its retention period",
                f"The approved retention period ended on {due.date().isoformat()}.",
                "Run the retention job (civicexp retention --apply) to delete the event "
                "data. The plan, decision record, and readout are kept.")
        published = any(e.action == "case:published" for e in record.audit())
        if state is not State.ARCHIVED and not published and record.final_file("case-record.json"):
            add("info", "unpublished", "Results not yet published",
                "The case summary for this evaluation has not been added to the "
                "evidence library.",
                "Publish it, whatever the outcome. A change that did not work is "
                "evidence other agencies can use.")
    return alerts


def _running_alerts(record: Any, name: str, state: State, lifecycle: Any,
                    now: datetime, stale_hours: float) -> list[Alert]:
    alerts: list[Alert] = []

    def add(severity: str, kind: str, title: str, detail: str, action: str) -> None:
        alerts.append(Alert(record.id, name, severity, kind, title, detail, action))

    outcome = record.analyses("live")
    for result in outcome.results:
        arm = f" ({result.treatment_variant})" if len(outcome.results) > 1 else ""
        diagnostics = result.diagnostics
        if diagnostics is not None and not diagnostics.is_trustworthy:
            add("critical", "trust_failure", f"Results cannot be trusted{arm}",
                " ".join(d.summary for d in diagnostics.blocking),
                "Pause the pilot and check the assignment and logging pipeline. "
                "Results from a broken pilot point the wrong way with full authority.")
        breached = result.breached_guardrails
        if breached:
            add("critical", "guardrail_breach",
                f"Harm measure breached its tolerance{arm}",
                "; ".join(f"{g.name}: {g.note}" for g in breached),
                "Pause now and review. The rule agreed before launch is to roll back.")
        if result.equity is not None and result.equity.harmed:
            add("critical", "equity_harm", f"A group is being harmed{arm}",
                result.equity.summary_line(),
                "Pause and review. A change that harms an identifiable group goes "
                "through the rollback path whatever it does on average.")
        watching = [g for g in result.guardrails if g.watch and not g.breached]
        if watching:
            add("warning", "guardrail_watch", f"Harm measure to watch{arm}",
                "; ".join(f"{g.name}: {g.note}" for g in watching),
                "Not a confirmed breach. Check it again at the next monitoring point.")
        for diagnostic in diagnostics or ():
            if diagnostic.severity.value == "warning":
                add("warning", f"check_{diagnostic.name}", f"Data check: {diagnostic.name}{arm}",
                    diagnostic.summary, diagnostic.detail or "Review before relying on results.")

    if state is State.RUNNING:
        last = record.last_event_at("live")
        started = lifecycle.started_at
        threshold = timedelta(hours=stale_hours)
        if last is None and started:
            began = datetime.fromisoformat(started)
            if now - began > threshold:
                add("warning", "no_data", "No data has arrived",
                    f"The pilot started {_ago(now - began)} ago and nothing has been "
                    "recorded.",
                    "Check that the service is calling the collection API with a valid "
                    "key. People may be seeing the tested version with nothing measured.")
        elif last is not None and now - last > threshold:
            add("warning", "stale_data", "Data has stopped arriving",
                f"The last event was recorded {_ago(now - last)} ago.",
                "Check the integration. If the service is down, pause the pilot so the "
                "gap is on the record.")
    else:
        reason = next((str(e.detail.get("reason", "")) for e in reversed(record.audit().entries)
                       if e.action.endswith("->paused")), "")
        add("info", "paused", "Paused", reason or "The pilot is paused.",
            "Resume it, or record the final decision if it should not continue.")

    progress = record.progress()
    fraction = progress.get("fraction_of_target")
    days, planned = progress.get("days_elapsed"), progress.get("planned_days")
    if (fraction is not None and fraction >= 1) or (days and planned and days >= planned):
        add("info", "ready_for_decision", "Reached its planned size or length",
            "The pilot has the data it was planned to collect.",
            "Record the final decision. Running past the plan adds looks the "
            "significance threshold was not set for.")
    return alerts


def _ago(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 48:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.0f} days"


def check_workspace(workspace: Any, *, now: datetime | None = None) -> list[Alert]:
    """Every alert across a workspace, most serious first."""
    stale = float(workspace.settings.get("stale_data_hours", 26))
    alerts: list[Alert] = []
    for record in workspace.experiments():
        try:
            alerts.extend(check_experiment(record, now=now, stale_hours=stale))
        except (CivicExpError, LifecycleError) as exc:  # pragma: no cover - defensive
            alerts.append(Alert(record.id, record.id, "critical", "check_failed",
                                "Could not check this evaluation", str(exc),
                                "Investigate before relying on it."))
    alerts.sort(key=lambda a: (a.rank, a.experiment_name, a.kind))
    return alerts


def format_alerts(alerts: Iterable[Alert]) -> str:
    """Plain text, for a terminal, an email, or a chat message."""
    lines = []
    for alert in alerts:
        lines.append(f"[{alert.severity.upper()}] {alert.experiment_name}: {alert.title}")
        if alert.detail:
            lines.append(f"    {alert.detail}")
        lines.append(f"    Next: {alert.action}")
    return "\n".join(lines) if lines else "Nothing needs attention."


def post_webhook(url: str, alerts: list[Alert], *, workspace_name: str = "",
                 timeout: float = 10.0) -> int:
    """Post alerts to a chat webhook, returning the HTTP status.

    The body carries a ``text`` field, which Slack, Microsoft Teams
    (incoming webhooks), Google Chat, and Mattermost all display, plus the
    structured alerts for anything that wants them.
    """
    if not url.startswith("https://"):
        raise ValueError("webhook URLs must use https")
    heading = f"Civic experimentation: {len(alerts)} item(s) need attention"
    if workspace_name:
        heading += f" in {workspace_name}"
    body = json.dumps({
        "text": heading + "\n\n" + format_alerts(alerts),
        "alerts": [a.to_dict() for a in alerts],
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return int(response.status)

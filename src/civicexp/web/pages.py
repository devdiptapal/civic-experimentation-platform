"""The pages people use: plan, review, run, decide, and share.

The interface follows the order a pilot actually happens in, and each
evaluation's overview always answers three questions first: what state is
this in, what needs to happen next, and who can do it. Everything that
changes an evaluation goes through :mod:`civicexp.workspace`, which checks
the person's role, so this module never decides who may do what.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from ..analysis import AnalysisResult
from ..auth import ROLE_DESCRIPTIONS, ROLES, SCOPES, AuthError, check_password_policy
from ..errors import CivicExpError, ConfigError
from ..events import parse_timestamp
from ..library import tally
from ..lifecycle import REQUIRED_APPROVALS, State
from ..metrics import STANDARD_METRICS, standard_metric_for
from ..monitor import check_experiment, check_workspace
from ..report import render_markdown
from ..wizard import TEMPLATES, assess_power, build_config, list_templates, load_template
from ..workspace import ExperimentRecord, WorkspaceError
from .app import App, HTTPError, Request, Response
from .charts import Interval, format_value, interval_chart, meter
from .html import (
    Markup,
    button_form,
    csrf_field,
    decision_chip,
    field,
    hidden,
    join,
    render,
    severity_badge,
    state_chip,
    status_chip,
    table,
)
from .markdown import markdown_to_html

ROLE_NAMES = {
    "program_owner": "Program owner",
    "operations_lead": "Operations lead",
    "privacy_reviewer": "Privacy reviewer",
    "legal_policy_reviewer": "Legal and policy reviewer",
    "evaluation_lead": "Evaluation lead",
    "admin": "Administrator",
    "viewer": "Viewer",
}

#: What each reviewer confirms, from docs/Experiment-Approval-Checklist.md.
REVIEW_QUESTIONS = {
    "program_owner": (
        "The objective is clearly stated, and the scope is narrow, specific, and reversible.",
        "The expected effect on applicants and staff is described.",
        "The smallest improvement worth acting on is a number the program stands behind.",
    ),
    "legal_policy_reviewer": (
        "No eligibility rule, required document, or policy changes; only the experience does.",
        "Possible unequal impacts and accessibility have been reviewed, and the groups "
        "to check are declared.",
        "No group is excluded without a documented reason, and a support path exists.",
    ),
    "privacy_reviewer": (
        "Only the fields the pilot needs are collected; no personal identifiers are stored.",
        "The retention period and the small-cell disclosure threshold are right for "
        "this data.",
        "Re-identification risk from published results has been considered.",
    ),
    "evaluation_lead": (
        "The hypothesis, main measure, and harm measures with tolerances are defined.",
        "The pilot is large enough to detect the effect the program cares about.",
        "The decision rule and the number of interim looks are fixed before launch.",
    ),
    "operations_lead": (
        "Someone owns implementation and monitoring, and escalation contacts are known.",
        "The rollback trigger and process are documented and rehearsed.",
        "Support staff know the pilot is running and what is being tested.",
    ),
}

ACTION_LABELS = {
    "config:created": "Plan created",
    "config:revised": "Plan revised",
    "approval:sign_off": "Signed off",
    "analysis:interim_look": "Interim look taken",
    "record:final_frozen": "Decision record frozen",
    "case:published": "Published to the evidence library",
    "dry_run:generated": "Dry run generated",
    "retention:events_deleted": "Event data deleted under the retention policy",
}
TRANSITION_LABELS = {
    "approved": "Approved", "running": "Started", "paused": "Paused",
    "completed": "Completed", "rolled_back": "Rolled back", "archived": "Archived",
    "draft": "Reopened to draft",
}


def register(app: App) -> None:
    routes = [
        ("GET", "/login", login_form, "public"),
        ("POST", "/login", login_submit, "public"),
        ("POST", "/logout", logout, "session"),
        ("GET", "/", inbox, "session"),
        ("GET", "/experiments", experiments_list, "session"),
        ("GET", "/experiments/new", new_choose, "session"),
        ("GET", "/experiments/power", power_check, "session"),
        ("POST", "/experiments/import", import_plan, "session"),
        ("GET", r"/experiments/new/(?P<template>[a-z-]+)", new_form, "session"),
        ("POST", r"/experiments/new/(?P<template>[a-z-]+)", new_submit, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)", overview, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/plan", plan_page, "session"),
        ("POST", r"/experiments/(?P<id>[^/]+)/plan", plan_save, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/plan\.json", plan_download, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/review", review_page, "session"),
        ("POST", r"/experiments/(?P<id>[^/]+)/sign-off", sign_off, "session"),
        ("POST", r"/experiments/(?P<id>[^/]+)/actions/(?P<action>[a-z-]+)", action, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/conclude", conclude_preview, "session"),
        ("POST", r"/experiments/(?P<id>[^/]+)/conclude", conclude_submit, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/results", results_page, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/files/(?P<name>[A-Za-z0-9._-]+)", final_download,
         "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/activity", activity_page, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/audit\.jsonl", audit_download, "session"),
        ("GET", r"/experiments/(?P<id>[^/]+)/connect", connect_page, "session"),
        ("GET", "/library", library_page, "session"),
        ("POST", "/library/import", library_import, "session"),
        ("GET", r"/library/(?P<case>[a-z0-9._-]+)\.json", library_download, "session"),
        ("GET", r"/library/(?P<case>[a-z0-9._-]+)", library_case, "session"),
        ("GET", "/admin", admin_page, "session"),
        ("POST", "/admin/settings", admin_settings, "session"),
        ("POST", "/admin/users", admin_add_user, "session"),
        ("POST", r"/admin/users/(?P<username>[a-z0-9._-]+)", admin_update_user, "session"),
        ("POST", "/admin/keys", admin_create_key, "session"),
        ("POST", r"/admin/keys/(?P<key>[0-9a-f]+)/revoke", admin_revoke_key, "session"),
        ("GET", "/account", account_page, "session"),
        ("POST", "/account/password", account_password, "session"),
        ("GET", "/help", help_page, "session"),
    ]
    for method, pattern, handler, auth in routes:
        app.route(method, pattern, auth=auth)(handler)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _need(request: Request, permission: str) -> None:
    if request.user is None or not request.user.can(permission):
        raise HTTPError(403, "Your role does not allow this. Ask someone who holds the "
                        "right role, or an administrator if your roles are wrong.",
                        code="AUTH_FORBIDDEN")


def _when(value: Any) -> str:
    if not value:
        return "—"
    try:
        moment = value if isinstance(value, datetime) else parse_timestamp(str(value))
    except CivicExpError:
        return str(value)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ago(value: datetime | None) -> str:
    if value is None:
        return "never"
    seconds = (datetime.now(timezone.utc) - value).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} hours ago"
    return f"{seconds / 86400:.0f} days ago"


def _role(role: str) -> str:
    return ROLE_NAMES.get(role, role.replace("_", " "))


def _record(app: App, request: Request) -> ExperimentRecord:
    return app.workspace.experiment(request.params["id"])


def _percent_input(text: str, label: str) -> float:
    """Read a form value given in percent (62, 62%, 3.5) as a proportion."""
    cleaned = text.strip().rstrip("%").strip()
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{label}: enter a number of percent, for example 62") from exc
    return value / 100.0


def _transition_label(action: str) -> str:
    if action.startswith("state:"):
        target = action.split("->")[-1]
        return TRANSITION_LABELS.get(target, action)
    return ACTION_LABELS.get(action, action)


# ---------------------------------------------------------------------------
# Sign in and out
# ---------------------------------------------------------------------------

_LOGIN_COOKIE = "cx_login"


def _login_body(token: str, nxt: str, error: str = "", username: str = "") -> Markup:
    err = render('<div class="error-summary" role="alert"><p>{e}</p></div>', e=error) if error else Markup("")
    return render(
        '<div class="narrow"><h1>Sign in</h1>{err}'
        '<form method="post" action="/login" class="stack">{tok}{next}{user}{pw}'
        '<button class="btn btn-primary" type="submit">Sign in</button></form>'
        '<p class="help">Accounts are created by your workspace administrator. If you '
        "have forgotten your password, ask them to set a new one.</p></div>",
        err=err, tok=hidden("_login", token), next=hidden("next", nxt),
        user=field("username", "Username", value=username, required=True,
                   attrs={"autocomplete": "username", "autocapitalize": "none"}),
        pw=field("password", "Password", kind="password", required=True,
                 attrs={"autocomplete": "current-password"}),
    )


def login_form(app: App, request: Request) -> Response:
    token = secrets.token_urlsafe(24)
    response = app.page(request, "Sign in", _login_body(token, request.arg("next", "/")))
    response.headers.append(
        ("Set-Cookie", f"{_LOGIN_COOKIE}={token}; Path=/login; HttpOnly; SameSite=Strict"
         + ("; Secure" if app.secure_cookies else ""))
    )
    return response


def login_submit(app: App, request: Request) -> Response:
    expected = request.cookies.get(_LOGIN_COOKIE, "")
    supplied = request.value("_login")
    nxt = request.value("next", "/") or "/"
    username = request.value("username").strip().lower()
    token = secrets.token_urlsafe(24)

    def retry(message: str, status: int = 200) -> Response:
        response = app.page(request, "Sign in", _login_body(token, nxt, message, username),
                            status=status)
        response.headers.append(
            ("Set-Cookie", f"{_LOGIN_COOKIE}={token}; Path=/login; HttpOnly; SameSite=Strict"
             + ("; Secure" if app.secure_cookies else ""))
        )
        return response

    if not expected or not secrets.compare_digest(expected, supplied):
        return retry("The sign-in form expired. Please try again.", 400)
    keys = (f"user:{username}", f"ip:{app.client_key(request)}")
    wait = app.throttle.retry_after(*keys)
    if wait:
        return retry(f"Too many unsuccessful attempts. Try again in {wait // 60 + 1} "
                     "minute(s).", 429)
    user = app.workspace.users.authenticate(username, request.value("password"))
    if user is None:
        app.throttle.failure(*keys)
        return retry("That username and password do not match an active account.", 401)
    app.throttle.success(*keys)
    return app.sign_in(user, nxt if nxt != "/login" else "/")


def logout(app: App, request: Request) -> Response:
    return app.sign_out(request)


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


def _alert_list(alerts: list[Any], *, show_experiment: bool = True) -> Markup:
    if not alerts:
        return Markup('<p class="empty">Nothing needs attention.</p>')
    items = []
    for alert in alerts:
        where = render('<a href="/experiments/{id}">{name}</a> · ', id=alert.experiment_id,
                       name=alert.experiment_name) if show_experiment else Markup("")
        items.append(render(
            '<li class="alert alert-{sev}">{badge}<div class="alert-body">'
            '<p class="alert-title">{where}{title}</p><p class="alert-detail">{detail}</p>'
            '<p class="alert-action"><strong>Next:</strong> {action}</p></div></li>',
            sev=alert.severity, badge=severity_badge(alert.severity), where=where,
            title=alert.title, detail=alert.detail, action=alert.action,
        ))
    return render('<ul class="alerts">{i}</ul>', i=join(items))


def inbox(app: App, request: Request) -> Response:
    ws = app.workspace
    user = request.user
    records = ws.experiments()
    counts = {"running": 0, "review": 0, "done": 0}
    for record in records:
        state = record.governance().state
        if state in (State.RUNNING, State.PAUSED):
            counts["running"] += 1
        elif state in (State.DRAFT, State.APPROVED):
            counts["review"] += 1
        elif state is not None:
            counts["done"] += 1
    published = len(ws.library.list())
    alerts = check_workspace(ws)
    pending = ws.pending_sign_offs(user)

    tiles = join(
        render('<div class="tile"><p class="tile-label">{l}</p><p class="tile-value">{v}</p>'
               '<p class="tile-note">{n}</p></div>', l=label, v=f"{value:,}", n=note)
        for label, value, note in (
            ("Running", counts["running"], "collecting or paused"),
            ("In planning or review", counts["review"], "draft or approved"),
            ("Finished", counts["done"], "completed, rolled back, archived"),
            ("Published cases", published, "in the evidence library"),
        )
    )
    waiting = Markup("")
    if pending:
        waiting = render(
            '<section class="card"><h2>Waiting on you</h2><ul class="plain">{items}</ul></section>',
            items=join(
                render('<li><a href="/experiments/{id}/review">{name}</a> needs your sign-off '
                       "as {role}.</li>", id=rec.id, name=rec.config().name, role=_role(role))
                for rec, role in pending
            ),
        )
    getting_started = Markup("")
    if len(records) < 2:
        steps = [
            ("Give your team accounts, one per review role",
             len(ws.users) >= 6, "/admin" if user.can("workspace.admin") else ""),
            ("Plan a first evaluation from a workflow template",
             bool(records), "/experiments/new" if user.can("experiment.create") else ""),
            ("Preview the readout with a dry run on simulated data",
             any(r.dry_run_path.exists() for r in records), ""),
            ("Collect the five reviewer sign-offs and approve", any(
                r.governance().state not in (State.DRAFT, None) for r in records), ""),
            ("Connect the service, or plan a spreadsheet batch", bool(ws.keys.list()), ""),
        ]
        getting_started = render(
            '<section class="card"><h2>Getting started</h2><p>Most teams reach a running '
            "pilot within two weeks. These are the steps, in order.</p>"
            '<ol class="checklist">{items}</ol></section>',
            items=join(
                render('<li class="{c}"><span class="check" aria-hidden="true">{g}</span>'
                       "{t}{state}{link}</li>",
                       c="done" if done else "", g="✓" if done else "○", t=text,
                       state=Markup('<span class="visually-hidden"> (done)</span>') if done
                       else Markup(""),
                       link=render(' — <a href="{h}">start</a>', h=href) if href and not done
                       else Markup(""))
                for text, done, href in steps
            ),
        )
    body = render(
        '<h1>Inbox</h1><div class="tiles">{tiles}</div>{waiting}'
        '<section class="card"><h2>Needs attention</h2>'
        '<p class="section-note">Most serious first. Harm and data problems are checked '
        "continuously; whether a change is working is not shown until a recorded look "
        "or the end of the pilot.</p>{alerts}</section>{start}",
        tiles=tiles, waiting=waiting, alerts=_alert_list(alerts), start=getting_started,
    )
    return app.page(request, "Inbox", body, section="inbox")


# ---------------------------------------------------------------------------
# Evaluations list and creation
# ---------------------------------------------------------------------------


def experiments_list(app: App, request: Request) -> Response:
    rows = []
    for record in app.workspace.experiments():
        try:
            config = record.config()
        except ConfigError:
            rows.append((record.id, "—", state_chip(None), "—", "—", "—"))
            continue
        status = record.governance()
        progress = record.progress()
        fraction = progress["fraction_of_target"]
        visibility = record.visibility()
        if visibility == "final":
            result_file = record.final_file("result.json")
            result = decision_chip(json.loads(result_file.read_text())["decision"]) \
                if result_file else Markup("No result")
        elif visibility == "sealed":
            result = Markup('<span class="muted">Sealed until decision</span>')
        else:
            result = Markup('<span class="muted">Not started</span>')
        rows.append((
            render('<a href="/experiments/{id}">{name}</a><br><span class="muted small">{id}'
                   "</span>", id=record.id, name=config.name),
            config.service_area or "—",
            state_chip(status.state.value if status.state else None),
            f"{fraction:.0%} of planned size" if fraction is not None and status.state not in
            (State.DRAFT, State.APPROVED) else "—",
            _ago(progress["last_event_at"]) if progress["last_event_at"] else "—",
            result,
        ))
    create = Markup("")
    if request.user.can("experiment.create"):
        create = Markup('<a class="btn btn-primary" href="/experiments/new">New evaluation</a>')
    body = render(
        '<div class="page-title"><h1>Evaluations</h1>{create}</div>{table}',
        create=create,
        table=table(["Evaluation", "Service area", "State", "Progress", "Last data", "Result"],
                    rows, empty="No evaluations yet. Start one from a workflow template."),
    )
    return app.page(request, "Evaluations", body, section="experiments")


def new_choose(app: App, request: Request) -> Response:
    _need(request, "experiment.create")
    cards = []
    for name, header in list_templates():
        evidence = app.workspace.library.evidence_for(workflow=name)
        counts = tally(evidence)
        evidence_text = (
            f"{len(evidence)} published case(s): "
            + ", ".join(f"{n} {d}" for d, n in sorted(counts.items()))
            if evidence else "No published cases yet — yours could be the first."
        )
        cards.append(render(
            '<article class="template-card"><h2><a href="/experiments/new/{n}">{t}</a></h2>'
            "<p>{s}</p><dl><dt>Good for</dt><dd>{g}</dd><dt>A typical change</dt><dd>{c}</dd>"
            '<dt>Evidence</dt><dd>{e}</dd></dl><a class="btn btn-secondary" '
            'href="/experiments/new/{n}">Plan with this template</a></article>',
            n=name, t=header["title"], s=header["summary"], g=header["good_for"],
            c=header["typical_change"], e=evidence_text,
        ))
    body = render(
        "<h1>Plan a new evaluation</h1><p class=\"lede\">Choose the part of the service you "
        "are changing. The template fills in the measures, harm checks, and privacy "
        "settings that suit it; you answer the questions only your team can.</p>"
        '<div class="template-grid">{cards}</div>'
        '<details class="card"><summary>Import an existing plan (JSON)</summary>'
        '<form method="post" action="/experiments/import" class="stack">{csrf}{text}'
        '<button class="btn btn-secondary" type="submit">Import plan</button></form></details>',
        cards=join(cards), csrf=csrf_field(request.session.csrf_token),
        text=field("plan", "Plan JSON", kind="textarea", rows=10,
                   help="A configuration written by civicexp init or exported from another "
                        "workspace. It is validated in full before anything is saved."),
    )
    return app.page(request, "New evaluation", body, section="experiments")


def _form_defaults(app: App, template: str, request: Request) -> dict[str, Any]:
    data = load_template(template)
    defaults: dict[str, Any] = {
        "name": "",
        "description": "",
        "hypothesis": "",
        "treatment_description": "",
        "owner_team": "",
        "jurisdiction": app.workspace.jurisdiction,
        "baseline_rate": f"{data['sample_size']['baseline_rate'] * 100:g}",
        "minimum_effect": "",
        "monthly_volume": "",
        "duration_days": str(data["sample_size"].get("planned_duration_days", 28)),
        "segments": ", ".join(data.get("segments", [])),
    }
    case_id = request.arg("from_case")
    if case_id:
        case = app.workspace.library.get(case_id)
        if case is not None:
            defaults["name"] = f"Replication: {case.title}"[:80]
            defaults["description"] = str(case.data.get("what_was_tested") or "")
            defaults["hypothesis"] = str(case.data.get("hypothesis") or "")
            mei = (case.data.get("primary") or {}).get("minimum_effect_of_interest")
            if isinstance(mei, (int, float)):
                defaults["minimum_effect"] = f"{mei * 100:g}"
    return defaults


def _new_form_body(app: App, request: Request, template: str, values: dict[str, Any],
                   errors: list[str]) -> Markup:
    header = load_template(template)["_template"]
    primary = load_template(template)["metrics"]["primary"]
    metric = STANDARD_METRICS.get(primary.get("use", ""))
    measure = metric.definition if metric else primary.get("definition", "")
    starting = (f" Teams often start the discussion around "
                f"{metric.typical_effect_of_interest * 100:g} points — a starting point, "
                "not a recommendation." if metric else "")
    error_html = Markup("")
    if errors:
        error_html = render(
            '<div class="error-summary" role="alert" tabindex="-1"><h2>Fix these before '
            "continuing</h2><ul>{items}</ul></div>",
            items=join(render("<li>{e}</li>", e=e) for e in errors),
        )
    v = values
    return render(
        '<p class="breadcrumb"><a href="/experiments/new">New evaluation</a> / {title}</p>'
        "<h1>{title}</h1>{errors}"
        '<form method="post" action="/experiments/new/{tpl}" class="stack wizard">{csrf}'
        "<fieldset><legend>1. What is changing</legend>{name}{description}{treatment}{hypothesis}"
        "{team}{jurisdiction}</fieldset>"
        "<fieldset><legend>2. What counts as success</legend>"
        '<p class="help">Main measure: {measure}</p>{baseline}{effect}</fieldset>'
        "<fieldset><legend>3. Can this pilot answer its question?</legend>{volume}{days}"
        '<div id="power-check" class="power-check" aria-live="polite" data-template="{tpl}">'
        '<p class="muted">Enter the numbers above to check whether the pilot is large '
        "enough.</p></div></fieldset>"
        "<fieldset><legend>4. Who to check for unequal impact</legend>{segments}</fieldset>"
        '<div class="form-actions"><button class="btn btn-primary" type="submit">'
        "Create the plan</button><span class=\"help\">Nothing runs until five reviewers have "
        "signed off and the plan is approved.</span></div></form>",
        title=header["title"], errors=error_html, tpl=template,
        csrf=csrf_field(request.session.csrf_token), measure=measure,
        name=field("name", "Name of this evaluation", value=v["name"], required=True,
                   help="A short name your team will recognize in a list."),
        description=field("description", "What people see today, and what will change",
                          kind="textarea", value=v["description"], required=True,
                          help="Plain language. Say explicitly what is not changing: "
                               "eligibility rules, required documents, deadlines."),
        treatment=field("treatment_description", "The new version, in one line",
                        value=v["treatment_description"], required=True),
        hypothesis=field("hypothesis", "What you expect, and why", kind="textarea", rows=3,
                         value=v["hypothesis"], required=True,
                         help="If we <change>, then <outcome> will improve, without "
                              "<what must not get worse>."),
        team=field("owner_team", "Team that owns this workflow", value=v["owner_team"]),
        jurisdiction=field("jurisdiction", "Agency or jurisdiction", value=v["jurisdiction"]),
        baseline=field("baseline_rate", "Today's rate, in percent", value=v["baseline_rate"],
                       required=True, kind="number",
                       attrs={"step": "any", "min": "0.1", "max": "99.9", "inputmode": "decimal"},
                       help="A rough figure from last month is fine, for example 62."),
        effect=field("minimum_effect", "Smallest improvement worth making permanent, in "
                     "percentage points", value=v["minimum_effect"], required=True,
                     kind="number", attrs={"step": "any", "min": "0.1", "max": "50",
                                           "inputmode": "decimal"},
                     help="A program judgment, not a technical one. For example 3 means "
                          "going from 62% to 65%. Results are measured against this number."
                          + starting),
        volume=field("monthly_volume", "People going through this workflow each month",
                     value=v["monthly_volume"], required=True, kind="number",
                     attrs={"min": "1", "step": "1", "inputmode": "numeric"}),
        days=field("duration_days", "Days to run for", value=v["duration_days"],
                   kind="number", attrs={"min": "7", "max": "365", "step": "1"},
                   help="Whole weeks avoid weekday effects. Four weeks is typical."),
        segments=field("segments", "Groups to check separately (comma separated)",
                       value=v["segments"],
                       help="Coded attributes your service already records, chosen now, "
                            "before any results exist. Looking for a group afterwards "
                            "always finds one."),
    )


def new_form(app: App, request: Request) -> Response:
    _need(request, "experiment.create")
    template = request.params["template"]
    if template not in TEMPLATES:
        raise HTTPError(404, "There is no template with that name.")
    body = _new_form_body(app, request, template, _form_defaults(app, template, request), [])
    return app.page(request, "New evaluation", body, section="experiments")


def new_submit(app: App, request: Request) -> Response:
    _need(request, "experiment.create")
    template = request.params["template"]
    if template not in TEMPLATES:
        raise HTTPError(404, "There is no template with that name.")
    keys = ("name", "description", "hypothesis", "treatment_description", "owner_team",
            "jurisdiction", "baseline_rate", "minimum_effect", "monthly_volume",
            "duration_days", "segments")
    values = {k: request.value(k).strip() for k in keys}
    errors: list[str] = []
    for key, label in (("name", "Name"), ("description", "What is changing"),
                       ("treatment_description", "The new version"),
                       ("hypothesis", "What you expect"),
                       ("minimum_effect", "Smallest improvement worth making permanent")):
        if not values[key]:
            errors.append(f"{label} is required.")
    answers: dict[str, Any] = {k: values[k] for k in keys if values[k]}
    try:
        answers["baseline_rate"] = _percent_input(values["baseline_rate"], "Today's rate")
        if values["minimum_effect"]:
            answers["minimum_effect"] = _percent_input(values["minimum_effect"],
                                                       "Smallest improvement")
        answers["monthly_volume"] = float(values["monthly_volume"] or 0)
        answers["duration_days"] = int(float(values["duration_days"] or 28))
    except ValueError as exc:
        errors.append(str(exc))
    if not errors:
        try:
            raw, assessment = build_config(template, answers)
            record = app.workspace.create_experiment(raw, request.user, workflow=template)
        except (ValueError, ConfigError, WorkspaceError) as exc:
            errors.append(getattr(exc, "message", str(exc)))
        else:
            app.flash(request, "success", "Plan created. Next: read it through on the Plan "
                      "tab, preview a readout with a dry run, then ask reviewers to sign off.")
            if not assessment.adequate:
                app.flash(request, "warning", assessment.message(answers["duration_days"]))
            return Response.redirect(f"/experiments/{record.id}")
    body = _new_form_body(app, request, template, values, errors)
    return app.page(request, "New evaluation", body, section="experiments", status=422)


def power_check(app: App, request: Request) -> Response:
    try:
        baseline = _percent_input(request.arg("baseline"), "baseline")
        effect = _percent_input(request.arg("effect"), "effect")
        monthly = float(request.arg("monthly"))
        days = float(request.arg("days") or 28)
        if not (0 < baseline < 1 and 0 < effect < 1 and monthly > 0 and days > 0):
            raise ValueError("out of range")
    except ValueError:
        return Response.json({"ready": False})
    assessment = assess_power(baseline, effect, monthly, days)
    return Response.json({
        "ready": True,
        "adequate": assessment.adequate,
        "message": assessment.message(int(days)),
        "per_group": assessment.per_group,
    })


def import_plan(app: App, request: Request) -> Response:
    _need(request, "experiment.create")
    try:
        raw = json.loads(request.value("plan"))
        if not isinstance(raw, dict):
            raise ValueError("the plan must be a JSON object")
        record = app.workspace.create_experiment(raw, request.user)
    except (ValueError, CivicExpError) as exc:
        app.flash(request, "error", f"Not imported: {getattr(exc, 'message', exc)}")
        return Response.redirect("/experiments/new")
    app.flash(request, "success", "Plan imported in draft.")
    return Response.redirect(f"/experiments/{record.id}")


# ---------------------------------------------------------------------------
# Evaluation pages: shared header
# ---------------------------------------------------------------------------

_TABS = (
    ("overview", "Overview", ""),
    ("plan", "Plan", "/plan"),
    ("review", "Review", "/review"),
    ("results", "Results", "/results"),
    ("activity", "Activity", "/activity"),
    ("connect", "Connect", "/connect"),
)


def _header(request: Request, record: ExperimentRecord, active: str) -> Markup:
    config = record.config()
    status = record.governance()
    tabs = join(
        render('<li><a href="/experiments/{id}{suffix}"{cur}>{label}</a></li>',
               id=record.id, suffix=suffix, label=label,
               cur=Markup(' aria-current="page"') if key == active else Markup(""))
        for key, label, suffix in _TABS
    )
    problems = Markup("")
    if not status.audit_valid:
        problems = render(
            '<div class="banner banner-critical" role="alert"><strong>Governance record '
            "failed verification.</strong> {m} Collection is halted.</div>",
            m=status.audit_message)
    elif status.plan_matches_approval is False and status.state in (
        State.APPROVED, State.RUNNING, State.PAUSED
    ):
        problems = Markup(
            '<div class="banner banner-critical" role="alert"><strong>The plan changed '
            "after approval.</strong> The configuration on file is not the one reviewers "
            "approved, so nothing is being collected.</div>")
    meta = [record.id]
    if config.service_area:
        meta.append(config.service_area)
    if config.owner_team:
        meta.append(f"owned by {config.owner_team}")
    return render(
        '<p class="breadcrumb"><a href="/experiments">Evaluations</a></p>'
        '<div class="page-title"><div><h1>{name}</h1><p class="meta">{meta}</p></div>'
        "<div>{chip}</div></div>{problems}"
        '<nav class="tabs" aria-label="Evaluation sections"><ul>{tabs}</ul></nav>',
        name=config.name, meta=" · ".join(meta),
        chip=state_chip(status.state.value if status.state else None),
        problems=problems, tabs=tabs,
    )


def _next_step(request: Request, record: ExperimentRecord) -> Markup:
    """What needs to happen next, who can do it, and the button if it is you."""
    user, csrf = request.user, request.session.csrf_token
    base = f"/experiments/{record.id}"
    try:
        lifecycle = record.lifecycle()
    except CivicExpError:
        return Markup("")
    state = lifecycle.state
    buttons: list[Markup] = []
    if state is State.DRAFT:
        missing = list(lifecycle.missing_approvals)
        stale = [r for r in lifecycle.sign_offs if record.sign_off_is_stale(r)]
        unfinished = record.unfinished()
        if unfinished:
            text = (f"Finish the plan: {len(unfinished)} field(s) still say FILL IN. "
                    "Reviewers cannot sign off on an unfinished plan.")
            if user.can("experiment.edit"):
                buttons.append(render('<a class="btn btn-primary" href="{b}/plan#edit">Edit the '
                                      "plan</a>", b=base))
        elif missing or stale:
            done = len(REQUIRED_APPROVALS) - len(set(missing) | set(stale))
            text = (f"Collect reviewer sign-offs ({done} of {len(REQUIRED_APPROVALS)} "
                    "done). Each review role is a different person.")
            buttons.append(render('<a class="btn btn-primary" href="{b}/review">Go to review</a>',
                                  b=base))
        else:
            text = "Every reviewer has signed off. The program owner approves the plan."
            if user.can("lifecycle.approve"):
                buttons.append(button_form(f"{base}/actions/approve", "Approve the plan", csrf,
                                           style="primary"))
        if user.can("experiment.dry_run"):
            buttons.append(render('<a class="btn btn-secondary" href="{b}/results#dry-run">'
                                  "Preview with a dry run</a>", b=base))
    elif state is State.APPROVED:
        text = ("Approved. Connect the service (or prepare the spreadsheet batch), then the "
                "operations lead starts the pilot. Nothing is collected until then.")
        if user.can("lifecycle.start"):
            buttons.append(button_form(f"{base}/actions/start", "Start the pilot", csrf,
                                       style="primary",
                                       confirm="Start collecting? People will begin seeing "
                                               "the tested version."))
        buttons.append(render('<a class="btn btn-secondary" href="{b}/connect">Connect the '
                              "service</a>", b=base))
        if user.can("lifecycle.reopen"):
            buttons.append(render('<a class="btn btn-quiet" href="{b}/review#reopen">Reopen to '
                                  "draft</a>", b=base))
    elif state in (State.RUNNING, State.PAUSED):
        progress = record.progress()
        fraction = progress["fraction_of_target"]
        size = f" It has reached {fraction:.0%} of its planned size." if fraction is not None else ""
        if state is State.RUNNING:
            text = "Collecting. Harm and data quality are monitored continuously." + size
        else:
            text = "Paused: nobody sees the tested version and nothing is collected." + size
        if state is State.RUNNING and user.can("lifecycle.pause"):
            buttons.append(render(
                '<form method="post" action="{b}/actions/pause" class="inline-form pause-form">'
                '{csrf}<label for="pause-reason" class="visually-hidden">Reason for pausing'
                '</label><input id="pause-reason" name="reason" required '
                'placeholder="Reason for pausing"><button class="btn btn-warning" '
                'type="submit">Pause</button></form>', b=base, csrf=csrf_field(csrf)))
        if state is State.PAUSED and user.can("lifecycle.resume"):
            buttons.append(button_form(f"{base}/actions/resume", "Resume", csrf, style="primary"))
        if user.can("lifecycle.look"):
            buttons.append(button_form(
                f"{base}/actions/look", "Take a recorded look", csrf,
                confirm="This unseals the current result once and counts against the "
                        "interim looks the plan declared. Continue?"))
        if user.can("lifecycle.complete"):
            buttons.append(render('<a class="btn btn-primary" href="{b}/conclude?outcome=complete">'
                                  "End and record the decision</a>", b=base))
        if user.can("lifecycle.roll_back"):
            buttons.append(render('<a class="btn btn-danger" href="{b}/conclude?outcome=roll_back">'
                                  "Roll back</a>", b=base))
    else:
        published = any(e.action == "case:published" for e in record.audit())
        if state is State.ARCHIVED:
            text = "Archived. The record is closed."
        elif published:
            text = "Finished and published. Archive it when the record can be closed."
        else:
            text = ("Finished. Publish the case summary — whatever the outcome — so other "
                    "agencies can reuse the evidence.")
            if user.can("case.publish") and record.final_file("case-record.json"):
                buttons.append(button_form(f"{base}/actions/publish", "Publish to the evidence "
                                           "library", csrf, style="primary"))
        if state is not State.ARCHIVED and user.can("lifecycle.archive"):
            buttons.append(button_form(f"{base}/actions/archive", "Archive", csrf,
                                       confirm="Archiving closes the record permanently."))
    return render(
        '<section class="next-step"><h2>Next step</h2><p>{t}</p>'
        '<div class="actions">{b}</div></section>',
        t=text, b=join(buttons),
    )


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def _primary_section(result: AnalysisResult, *, heading: str) -> Markup:
    config = result.config
    metric = config.primary_metric
    primary = result.primary
    low, high = primary.difference_interval
    wanted = metric.minimum_effect_of_interest
    threshold = wanted if metric.target_direction == "increase" else -wanted
    chart = interval_chart(
        [Interval(f"{result.treatment_variant} vs {result.control_variant}",
                  primary.absolute_difference, low, high)],
        unit="pts", title=f"Change in {metric.name}",
        threshold=threshold, threshold_label=f"Bar set before launch {format_value(threshold, 'pts')}",
    )
    rows = [
        (f"{result.control_variant} (current)", f"{primary.control_trials:,}",
         f"{primary.control_rate:.1%}"),
        (f"{result.treatment_variant} (tested)", f"{primary.treatment_trials:,}",
         f"{primary.treatment_rate:.1%}"),
    ]
    standard = standard_metric_for(metric)
    comparability = (
        render('<span class="tag">Standard definition: {s}</span>', s=standard)
        if standard else Markup('<span class="tag tag-muted">Custom definition</span>')
    )
    return render(
        '<section class="card"><h2>{heading}</h2>'
        '<div class="decision decision-{d}"><p class="decision-chip">{chip}</p>'
        '<p class="decision-headline">{headline}</p></div>'
        '<ul class="rationale">{why}</ul>'
        '<h3>{metric} {comp}</h3><p class="muted">{definition}</p>{chart}'
        '{table}<p class="viz-note">The dot is the measured change; the line is the range of '
        "plausible changes. The tested version is adopted only if the whole line clears the "
        "bar set before launch.</p></section>",
        heading=heading, d=result.decision.value, chip=decision_chip(result.decision.value),
        headline=result.decision.headline,
        why=join(render("<li>{r}</li>", r=r) for r in result.rationale),
        metric=metric.name, comp=comparability, definition=metric.definition, chart=chart,
        table=table(["Version", "People", "Rate"], rows, numeric=(1, 2)),
    )


def _safety_section(results: tuple[AnalysisResult, ...], *, sealed: bool) -> Markup:
    parts = []
    for result in results:
        arm = f" — {result.treatment_variant}" if len(results) > 1 else ""
        guardrails = table(
            ["Harm measure", "Status", "What happened"],
            [(g.name, status_chip(g.status), g.note) for g in result.guardrails],
            empty="No harm measures were defined, so this pilot cannot detect a side effect.",
        )
        checks = []
        names = {"sample_ratio": "The split matched the plan",
                 "completeness": "Every measurement arrived",
                 "novelty": "The effect held up over time"}
        for diagnostic in result.diagnostics or ():
            if sealed and diagnostic.name == "novelty":
                continue  # its numbers describe the main result, which is sealed
            checks.append((names.get(diagnostic.name, diagnostic.name),
                           status_chip(diagnostic.severity.value), diagnostic.summary))
        if sealed:
            checks.append(("The effect held up over time", status_chip("NO DATA"),
                           "Checked when the result is unsealed: its numbers describe the "
                           "main result."))
        equity = Markup("")
        if result.equity is not None and result.equity.findings:
            findings = result.equity.findings
            if sealed:
                harmed = [f for f in findings if f.outcome.value == "harmed"]
                equity = render(
                    "<h3>Groups declared for equity review</h3><p>{t}</p>",
                    t=("A confirmed harm to " + ", ".join(f.label for f in harmed) + ". "
                       "Pause and review." if harmed else
                       f"No confirmed harm to any of the {len(findings)} declared group(s). "
                       "Whether each group benefited is shown when the result is unsealed."))
            else:
                metric = result.config.primary_metric
                wanted = metric.minimum_effect_of_interest
                threshold = wanted if metric.target_direction == "increase" else -wanted
                rows = [Interval(f.label, f.comparison.absolute_difference,
                                 *f.comparison.difference_interval)
                        for f in findings if f.comparison is not None]
                equity = render(
                    "<h3>Did it work for everyone?</h3><p><strong>{s}</strong></p>{chart}{t}",
                    s=result.equity.summary_line(),
                    chart=interval_chart(rows, unit="pts", title="Change by group",
                                         threshold=threshold,
                                         threshold_label="Bar set before launch"),
                    t=table(["Group", "People (current / tested)", "Result", "What it means"],
                            [(f.label, f"{f.control_n:,} / {f.treatment_n:,}",
                              status_chip(f.outcome.value), f.note) for f in findings],
                            numeric=(1,)),
                )
        parts.append(render(
            "<h3>Harm measures{arm}</h3>{g}<h3>What we checked{arm}</h3>{c}{e}",
            arm=arm, g=guardrails,
            c=table(["Check", "Result", "Finding"], checks), e=equity,
        ))
    return render('<section class="card"><h2>Safety and trust</h2>'
                  '<p class="section-note">Agreed before launch: harm outranks benefit, and a '
                  "failed trust check means no decision can be drawn.</p>{p}</section>",
                  p=join(parts))


def _sealed_section(request: Request, record: ExperimentRecord) -> Markup:
    looks = record.looks()
    planned = record.config().decision.planned_looks
    interim_allowed = max(planned - 1, 0)
    latest = Markup("")
    if looks:
        entry = looks[-1]
        detail = entry.detail
        lines = []
        for key, value in detail.items():
            if key.endswith("decision"):
                prefix = key[: -len("decision")]
                lo, hi = detail.get(f"{prefix}ci_low"), detail.get(f"{prefix}ci_high")
                lines.append(render(
                    "<li>{arm}: {chip} measured change {eff}, plausible range {lo} to {hi}</li>",
                    arm=detail.get(f"{prefix}arm", ""), chip=decision_chip(str(value)),
                    eff=format_value(float(detail.get(f"{prefix}effect", 0)), "pts"),
                    lo=format_value(float(lo), "pts"), hi=format_value(float(hi), "pts")))
        latest = render(
            "<h3>Latest recorded look</h3><p>Look {n}, taken by {who} on {when}.{extra}</p>"
            "<ul>{lines}</ul><p class=\"help\">An interim result is what the rule would say "
            "if the pilot ended now. The decision is recorded only when the pilot ends.</p>",
            n=detail.get("look"), who=entry.actor, when=_when(entry.timestamp),
            extra=" This look was beyond the number the plan declared." if detail.get(
                "within_plan") is False else "",
            lines=join(lines))
    return render(
        '<section class="card sealed"><h2><span aria-hidden="true">🔒</span> Main result '
        "sealed</h2><p>Whether the change is working is not shown while the pilot runs. "
        "Watching a live number until it looks good is the most common way a pilot reports "
        "a change that does not work. The plan declared {planned} look(s) at the data, "
        "including the final one, and the significance threshold is tightened to match.</p>"
        "<p><strong>{used} of {allowed} interim look(s) used.</strong> The evaluation lead "
        "can take a recorded look; each one goes on the record.</p>{latest}</section>",
        planned=planned, used=len(looks), allowed=interim_allowed, latest=latest,
    )


def _progress_section(record: ExperimentRecord) -> Markup:
    progress = record.progress()
    expected = progress["expected_per_group"]
    rows = []
    for arm, count in sorted(progress["enrolled"].items()):
        fraction = count / expected if expected else None
        label = f"{count:,} of {expected:,} planned" if expected else f"{count:,}"
        rows.append(render(
            '<div class="meter-row"><span class="meter-label">{arm}</span>{m}'
            '<span class="meter-value">{label}</span></div>',
            arm=arm, m=meter(fraction, label=f"{arm}: {label}"), label=label))
    days = progress["days_elapsed"]
    planned = progress["planned_days"]
    timing = (f"Day {days} of {planned} planned." if days is not None and planned else
              f"Day {days}." if days is not None else "Not started.")
    last = progress["last_event_at"]
    return render(
        '<section class="card"><h2>Progress</h2><div class="meters">{rows}</div>'
        "<p>{timing} Last data received: {last}.</p></section>",
        rows=join(rows), timing=timing,
        last=f"{_ago(last)} ({_when(last)})" if last else "none yet",
    )


def _evidence_section(record: ExperimentRecord) -> Markup:
    config = record.config()
    status = record.governance()
    audit = record.audit()
    verification = audit.verify()
    log = record.event_log("live")
    window = "no events"
    if log is not None and len(log):
        stamps = [e.timestamp for e in log]
        window = f"{_when(min(stamps))} to {_when(max(stamps))} ({len(log):,} events)"
    matches = {True: "matches what reviewers approved", False: "DOES NOT match the approved "
               "plan", None: "not yet approved"}[status.plan_matches_approval]
    rows = [
        ("Plan version", render("<code>{f}</code> — {m}", f=config.fingerprint[:12], m=matches)),
        ("Governance record", render("{v}, {n} entries, head <code>{h}</code>",
                                     v="intact" if verification.valid else "FAILED",
                                     n=len(audit), h=audit.head[:12])),
        ("Live data window", window),
        ("Privacy", f"No identifiers stored; groups under "
                    f"{config.privacy.suppression_threshold} people withheld; data kept "
                    f"{config.privacy.retention_days} days after the pilot ends"),
    ]
    return render('<section class="card"><h2>Evidence behind this page</h2>{t}</section>',
                  t=table(["", ""], rows))


def overview(app: App, request: Request) -> Response:
    record = _record(app, request)
    visibility = record.visibility()
    sections: list[Markup] = [_next_step(request, record)]
    alerts = [a for a in check_experiment(record) if a.severity != "info"]
    if alerts:
        sections.append(render('<section class="card"><h2>Needs attention</h2>{a}</section>',
                               a=_alert_list(alerts, show_experiment=False)))
    if visibility == "final":
        outcome = record.analyses("live")
        if outcome.results:
            for result in outcome.results:
                sections.append(_primary_section(result, heading="Result"))
            sections.append(_safety_section(outcome.results, sealed=False))
        else:
            sections.append(Markup(
                '<section class="card"><h2>Result</h2><p>The live data is no longer held '
                '(or never arrived). The frozen readout on the Results tab is the '
                "record.</p></section>"))
        sections.append(_progress_section(record))
    elif visibility == "sealed":
        sections.append(_sealed_section(request, record))
        outcome = record.analyses("live")
        if outcome.results:
            sections.append(_safety_section(outcome.results, sealed=True))
        else:
            sections.append(render('<section class="card"><h2>Safety and trust</h2>'
                                   "<p>{p}</p></section>",
                                   p=f"Nothing to check yet: {outcome.problem}."))
        sections.append(_progress_section(record))
    else:
        config = record.config()
        note = config.power_note() or "Add a baseline rate and expected volume to check power."
        sections.append(render(
            '<section class="card"><h2>Before launch</h2><p>{n}</p>'
            '<p>Preview what the readout will look like with a <a href="/experiments/{id}/'
            'results#dry-run">dry run on simulated data</a>. No real person is involved.</p>'
            "</section>", n=note, id=record.id))
    sections.append(_evidence_section(record))
    body = _header(request, record, "overview") + join(sections)
    return app.page(request, record.config().name, body, section="experiments")


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def plan_page(app: App, request: Request) -> Response:
    record = _record(app, request)
    raw = record.raw()
    config = record.config()
    state = record.governance().state
    metric = config.primary_metric
    variants = raw.get("variants") or {}
    unfinished = record.unfinished()
    warn = Markup("")
    if unfinished:
        warn = render('<div class="banner banner-warning" role="note"><strong>Unfinished.'
                      "</strong> Still marked FILL IN: {f}.</div>", f=", ".join(unfinished))
    eligibility = [f"Included when {r.describe()}" for r in config.eligibility.include]
    eligibility += [f"Excluded when {r.describe()}" for r in config.eligibility.exclude]
    standard = standard_metric_for(metric)
    guardrails = [
        (g.name, g.definition,
         f"{'rises' if g.target_direction == 'decrease' else 'falls'} by more than "
         + (format_value(g.max_tolerated_increase, 'pts', signed=False) if g.kind == "proportion"
            else format_value(g.max_tolerated_increase, 'seconds', signed=False)),
         standard_metric_for(g) or "custom")
        for g in config.guardrails
    ]
    sections = render(
        '<section class="card"><h2>Hypothesis</h2><p>{hyp}</p><h3>What is changing</h3>'
        "<p>{desc}</p>{versions}</section>"
        '<section class="card"><h2>Who is included</h2><ul>{elig}</ul><p>{ramp}</p></section>'
        '<section class="card"><h2>What counts as success</h2><p><strong>{mname}</strong> — '
        "{mdef} {std}</p><p>Adopt only if the whole plausible range clears a change of "
        "<strong>{bar}</strong> in the {dir} direction.</p></section>"
        '<section class="card"><h2>What must not get worse</h2>{guards}</section>'
        '<section class="card"><h2>Groups checked for unequal impact</h2><p>{seg}</p></section>'
        '<section class="card"><h2>Size, looks, and confidence</h2><p>{power}</p>'
        "<p>{looks} look(s) at the data, including the final one, at {conf} confidence. "
        "Planned for {days} days.</p></section>"
        '<section class="card"><h2>Privacy</h2><ul><li>Identifiers are pseudonymized with '
        "this plan's secret key before anything is stored; the key is hidden here.</li>"
        "<li>Event data is deleted {ret} days after the pilot ends.</li>"
        "<li>Results about groups smaller than {sup} people are withheld.</li>"
        "<li>Approved personal-data fields: {pii}.</li></ul></section>",
        hyp=str((raw.get("hypothesis") or {}).get("statement", "Not recorded.")),
        desc=config.description or "Not recorded.",
        versions=table(["Version", "Share", "Description"],
                       [(name, f"{share:.0%}", (variants.get(name) or {}).get("description", ""))
                        for name, share in sorted(config.variants.items())], numeric=(1,)),
        elig=join(render("<li>{e}</li>", e=e) for e in eligibility) or Markup("<li>Everyone</li>"),
        ramp=(f"Limited to {config.enrolled_fraction:.0%} of eligible traffic."
              if config.enrolled_fraction < 1 else "All eligible traffic is included."),
        mname=metric.name, mdef=metric.definition,
        std=render('<span class="tag">Standard definition</span>') if standard else
        Markup('<span class="tag tag-muted">Custom definition — not directly comparable with '
               "other agencies</span>"),
        bar=format_value(metric.minimum_effect_of_interest, "pts", signed=False),
        dir="higher" if metric.target_direction == "increase" else "lower",
        guards=table(["Measure", "Definition", "Breached if it", "Definition source"], guardrails,
                     empty="None defined. Without harm measures this pilot cannot detect harm."),
        seg=", ".join(config.segments) or "None declared.",
        power=config.power_note() or "Power cannot be checked: no baseline or volume recorded.",
        looks=config.decision.planned_looks, conf=f"{config.decision.confidence:.0%}",
        days=config.planned_duration_days or "an unspecified number of",
        ret=config.privacy.retention_days, sup=config.privacy.suppression_threshold,
        pii=", ".join(sorted(config.privacy.approved_pii_fields)) or "none",
    )
    warnings = Markup("")
    if config.warnings:
        warnings = render('<section class="card"><h2>Validation notes</h2><ul>{w}</ul></section>',
                          w=join(render("<li>{w}</li>", w=w) for w in config.warnings))
    editor = Markup("")
    can_edit = request.user.can("experiment.edit")
    if can_edit and state is State.DRAFT:
        editor = render(
            '<section class="card" id="edit"><h2>Edit the plan</h2><p class="help">The whole '
            "plan, as JSON. Saving validates it in full and records a new plan version; any "
            "sign-offs already given will need to be given again.</p>"
            '<form method="post" action="/experiments/{id}/plan" class="stack">{csrf}{f}'
            '<button class="btn btn-primary" type="submit">Save the plan</button></form></section>',
            id=record.id, csrf=csrf_field(request.session.csrf_token),
            f=field("plan", "Plan JSON", kind="textarea", rows=24,
                    value=json.dumps(raw, indent=2), attrs={"spellcheck": "false",
                                                            "class": "code"}))
    download = Markup("")
    if can_edit or request.user.can("workspace.admin"):
        download = render('<p><a href="/experiments/{id}/plan.json">Download the plan</a> '
                          "(contains the secret key — handle it as a secret)</p>", id=record.id)
    body = _header(request, record, "plan") + warn + sections + warnings + editor + download
    return app.page(request, f"Plan · {config.name}", body, section="experiments")


def plan_save(app: App, request: Request) -> Response:
    record = _record(app, request)
    try:
        raw = json.loads(request.value("plan"))
        if not isinstance(raw, dict):
            raise ValueError("the plan must be a JSON object")
        record.revise(request.user, raw)
    except (ValueError, CivicExpError) as exc:
        app.flash(request, "error", f"Not saved: {getattr(exc, 'message', exc)}")
        return Response.redirect(f"/experiments/{record.id}/plan#edit")
    app.flash(request, "success", "Plan saved as a new version. Reviewers who signed off "
              "earlier need to sign off again.")
    return Response.redirect(f"/experiments/{record.id}/plan")


def plan_download(app: App, request: Request) -> Response:
    if not (request.user.can("experiment.edit") or request.user.can("workspace.admin")):
        _need(request, "experiment.edit")
    record = _record(app, request)
    return Response.download(record.config_path.read_bytes(), f"{record.id}.json",
                             "application/json")


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def review_page(app: App, request: Request) -> Response:
    record = _record(app, request)
    lifecycle = record.lifecycle()
    fingerprint = record.config().fingerprint
    csrf = request.session.csrf_token
    rows = []
    for role in REQUIRED_APPROVALS:
        sign_off = lifecycle.sign_offs.get(role)
        if sign_off is None:
            status = Markup('<span class="chip chip-neutral"><span class="chip-glyph" '
                            'aria-hidden="true">○</span>Waiting</span>')
            detail = "—"
        elif record.sign_off_is_stale(role):
            status = Markup('<span class="chip chip-warning"><span class="chip-glyph" '
                            'aria-hidden="true">▲</span>Plan changed since</span>')
            detail = f"{sign_off.actor} signed an earlier version on {_when(sign_off.timestamp)}"
        else:
            status = Markup('<span class="chip chip-good"><span class="chip-glyph" '
                            'aria-hidden="true">✓</span>Signed off</span>')
            detail = render("{who}, {when}{note}", who=sign_off.actor,
                            when=_when(sign_off.timestamp),
                            note=f" — “{sign_off.note}”" if sign_off.note else "")
        rows.append((_role(role), status, detail))

    forms = []
    if lifecycle.state is State.DRAFT:
        for role in request.user.reviewer_roles:
            current = lifecycle.sign_offs.get(role)
            if current is not None and not record.sign_off_is_stale(role):
                continue
            questions = join(render("<li>{q}</li>", q=q) for q in REVIEW_QUESTIONS[role])
            forms.append(render(
                '<section class="card"><h2>Your review as {name}</h2>'
                "<p>Read the <a href=\"/experiments/{id}/plan\">plan</a>, then confirm:</p>"
                '<ul class="review-questions">{q}</ul>'
                '<form method="post" action="/experiments/{id}/sign-off" class="stack">{csrf}'
                "{role}{note}{confirm}"
                '<button class="btn btn-primary" type="submit">Sign off as {name}</button></form>'
                "</section>",
                name=_role(role), id=record.id, q=questions, csrf=csrf_field(csrf),
                role=hidden("role", role),
                note=field("note", "Note for the record (optional)", kind="textarea", rows=2,
                           help="Conditions, context, or what you checked. Stored with your "
                                "sign-off in the governance record."),
                confirm=field("confirm", f"I have reviewed plan version {fingerprint[:12]} "
                              "against these points", kind="checkbox", required=True),
            ))
    approve = Markup("")
    if lifecycle.state is State.DRAFT and not lifecycle.missing_approvals and not any(
        record.sign_off_is_stale(r) for r in lifecycle.sign_offs
    ):
        approve = render(
            '<section class="card"><h2>Approval</h2><p>All five reviewers have signed off on '
            "the current plan.</p>{b}</section>",
            b=button_form(f"/experiments/{record.id}/actions/approve", "Approve the plan", csrf,
                          style="primary") if request.user.can("lifecycle.approve")
            else Markup("<p>The program owner approves it.</p>"))
    reopen = Markup("")
    if lifecycle.state is State.APPROVED and request.user.can("lifecycle.reopen"):
        reopen = render(
            '<section class="card" id="reopen"><h2>Reopen to draft</h2><p>Reopening lets the '
            "plan be changed; every reviewer then signs off again.</p>"
            '<form method="post" action="/experiments/{id}/actions/reopen" class="stack">'
            '{csrf}{reason}<button class="btn btn-secondary" type="submit">Reopen</button>'
            "</form></section>",
            id=record.id, csrf=csrf_field(csrf),
            reason=field("reason", "Reason", required=True))
    intro = render(
        '<section class="card"><h2>Sign-offs</h2><p>Five people, one per role, each confirm '
        "the plan is ready from their side. A sign-off applies to one version of the plan: "
        "editing the plan afterwards means signing off again.</p>{t}</section>",
        t=table(["Role", "Status", "Detail"], rows))
    body = _header(request, record, "review") + intro + join(forms) + approve + reopen
    return app.page(request, f"Review · {record.config().name}", body, section="experiments")


def sign_off(app: App, request: Request) -> Response:
    record = _record(app, request)
    if request.value("confirm") != "1":
        app.flash(request, "error", "Tick the confirmation to sign off.")
        return Response.redirect(f"/experiments/{record.id}/review")
    role = request.value("role")
    record.sign_off(request.user, role, request.value("note").strip())
    app.flash(request, "success", f"Signed off as {_role(role)}.")
    return Response.redirect(f"/experiments/{record.id}/review")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def action(app: App, request: Request) -> Response:
    record = _record(app, request)
    name = request.params["action"]
    user = request.user
    target = f"/experiments/{record.id}"
    if name == "approve":
        record.approve(user)
        app.flash(request, "success", "Approved. The plan is now locked; the operations lead "
                  "can start the pilot when the service is ready.")
    elif name == "start":
        record.start(user)
        app.flash(request, "success", "Started. The service can now assign people and record "
                  "outcomes.")
    elif name == "pause":
        reason = request.value("reason").strip()
        if not reason:
            app.flash(request, "error", "Pausing needs a reason, so the record shows why.")
            return Response.redirect(target)
        record.pause(user, reason)
        app.flash(request, "success", "Paused. Everyone now sees the current version and "
                  "nothing is collected.")
    elif name == "resume":
        record.resume(user)
        app.flash(request, "success", "Resumed.")
    elif name == "reopen":
        record.reopen(user, request.value("reason"))
        app.flash(request, "success", "Reopened to draft. Reviewers sign off again after "
                  "any change.")
    elif name == "look":
        number, _ = record.take_look(user)
        app.flash(request, "success", f"Look {number} recorded.")
    elif name == "archive":
        record.archive(user)
        app.flash(request, "success", "Archived.")
    elif name == "publish":
        case = record.publish(user)
        app.flash(request, "success", "Published to the evidence library.")
        return Response.redirect(f"/library/{case.case_id}")
    elif name == "dry-run":
        try:
            control = _percent_input(request.value("control_rate"), "Current rate")
            treatment = _percent_input(request.value("treatment_rate"), "Tested rate")
            units = int(request.value("units") or 4000)
        except ValueError as exc:
            app.flash(request, "error", str(exc))
            return Response.redirect(f"{target}/results#dry-run")
        events = record.dry_run(user, control_rate=control, treatment_rate=treatment,
                                units=units)
        app.flash(request, "success", f"Dry run generated: {events:,} simulated events.")
        return Response.redirect(f"{target}/results#dry-run")
    else:
        raise HTTPError(404, "Unknown action.")
    return Response.redirect(target)


def conclude_preview(app: App, request: Request) -> Response:
    record = _record(app, request)
    outcome = request.arg("outcome", "complete")
    if outcome not in ("complete", "roll_back"):
        raise HTTPError(404, "Unknown outcome.")
    _need(request, "lifecycle.complete" if outcome == "complete" else "lifecycle.roll_back")
    lifecycle = record.lifecycle()
    if lifecycle.state not in (State.RUNNING, State.PAUSED):
        raise HTTPError(409, "Only a running or paused evaluation can be ended.",
                        code="LIFECYCLE_FORBIDDEN")
    progress = record.progress()
    fraction = progress["fraction_of_target"]
    planned = record.config().decision.planned_looks
    final_look = lifecycle.interim_looks + 1
    notes = []
    if fraction is not None and fraction < 1:
        notes.append(f"The pilot has reached {fraction:.0%} of its planned size. Ending now "
                     "makes an inconclusive result more likely.")
    if final_look > planned:
        notes.append(f"This will be look {final_look}, but the plan declared {planned}. The "
                     "readout will say so.")
    safety = check_experiment(record)
    harm = [a for a in safety if a.severity == "critical"]
    if outcome == "roll_back":
        title = "Roll back the tested version"
        explain = ("Rolling back ends the pilot and records that the tested version is "
                   "withdrawn. Collection stops immediately; the service shows everyone the "
                   "current version. The final readout is generated from the data collected.")
        note_field = field("note", "Reason for rolling back", kind="textarea", rows=3,
                           required=True, value="; ".join(a.title for a in harm))
        button = '<button class="btn btn-danger" type="submit">Roll back and record</button>'
    else:
        title = "End the pilot and record the decision"
        explain = ("The decision is made by the rule agreed before launch, applied to all the "
                   "data collected. You will see the result once it is recorded — not "
                   "before, so that seeing it cannot change whether the pilot ends.")
        note_field = field("note", "Note for the record (optional)", kind="textarea", rows=2)
        button = ('<button class="btn btn-primary" type="submit">End the pilot and record '
                  "the decision</button>")
    body = render(
        "{header}<section class=\"card\"><h2>{title}</h2><p>{explain}</p>{notes}{harm}"
        '<form method="post" action="/experiments/{id}/conclude" class="stack">{csrf}'
        '{outcome}{note}<div class="form-actions">{button}<a class="btn btn-quiet" '
        'href="/experiments/{id}">Cancel</a></div></form></section>',
        header=_header(request, record, "overview"), title=title, explain=explain,
        notes=join(render('<p class="banner banner-warning">{n}</p>', n=n) for n in notes),
        harm=render("<h3>Open safety alerts</h3>{a}", a=_alert_list(harm, show_experiment=False))
        if harm else Markup(""),
        id=record.id, csrf=csrf_field(request.session.csrf_token),
        outcome=hidden("outcome", outcome), note=note_field, button=Markup(button),
    )
    return app.page(request, title, body, section="experiments")


def conclude_submit(app: App, request: Request) -> Response:
    record = _record(app, request)
    outcome = request.value("outcome")
    note = request.value("note")
    if outcome == "roll_back" and not note.strip():
        app.flash(request, "error", "Rolling back needs a reason for the record.")
        return Response.redirect(f"/experiments/{record.id}/conclude?outcome=roll_back")
    final = record.conclude(request.user, outcome=outcome, note=note)
    if final.results:
        summary = "; ".join(f"{r.decision.value.upper()} — {r.decision.headline}"
                            for r in final.results)
        app.flash(request, "success", f"Decision recorded: {summary}.")
    else:
        app.flash(request, "warning", f"Ended without a result: {final.problem}.")
    return Response.redirect(f"/experiments/{record.id}/results")


# ---------------------------------------------------------------------------
# Results and files
# ---------------------------------------------------------------------------


def _dry_run_section(request: Request, record: ExperimentRecord) -> Markup:
    can = request.user.can("experiment.dry_run")
    form = Markup("")
    if can:
        config = record.config()
        baseline = config.baseline_rate or 0.6
        effect = config.primary_metric.minimum_effect_of_interest
        form = render(
            '<form method="post" action="/experiments/{id}/actions/dry-run" class="row-form">'
            "{csrf}{c}{t}{u}<button class=\"btn btn-secondary\" type=\"submit\">Generate"
            "</button></form>",
            id=record.id, csrf=csrf_field(request.session.csrf_token),
            c=field("control_rate", "Current rate, %", value=f"{baseline * 100:g}",
                    kind="number", attrs={"step": "any"}),
            t=field("treatment_rate", "Tested rate, %",
                    value=f"{(baseline + effect * 1.5) * 100:g}", kind="number",
                    attrs={"step": "any"}),
            u=field("units", "Synthetic people", value="4000", kind="number",
                    attrs={"min": "100", "max": "100000"}))
    outcome = record.analyses("dry_run")
    readouts = Markup("")
    if outcome.results:
        readouts = join(
            render('<div class="readout dry-run">{md}</div>',
                   md=markdown_to_html(render_markdown(result)))
            for result in outcome.results)
    return render(
        '<section class="card" id="dry-run"><h2>Dry run on simulated data</h2>'
        "<p>Generates synthetic people who meet the eligibility rules and runs the real "
        "analysis, so the team can check the measures produce sensible numbers and see the "
        "readout before launch. Simulated data never mixes with live data.</p>{form}"
        "{banner}{readouts}</section>",
        form=form, readouts=readouts,
        banner=Markup('<p class="banner banner-info"><strong>Simulated data.</strong> This '
                      "readout describes synthetic people, not your service.</p>")
        if outcome.results else Markup(""),
    )


def results_page(app: App, request: Request) -> Response:
    record = _record(app, request)
    visibility = record.visibility()
    parts: list[Markup] = []
    if visibility == "final":
        readouts = record.final_readouts()
        if not readouts:
            parts.append(Markup('<section class="card"><p>This evaluation ended without '
                                "results to report.</p></section>"))
        for arm, path in readouts:
            suffix = f"-{arm}" if arm else ""
            links = [render('<a href="/experiments/{id}/files/{n}">Download readout</a>',
                            id=record.id, n=path.name)]
            for name in (f"case-summary{suffix}.md", f"case-record{suffix}.json",
                         f"result{suffix}.json"):
                if record.final_file(name):
                    links.append(render('<a href="/experiments/{id}/files/{n}">{n}</a>',
                                        id=record.id, n=name))
            parts.append(render(
                '<section class="card"><p class="file-links">{links}</p>'
                '<div class="readout">{md}</div></section>',
                links=join(links, " · "),
                md=markdown_to_html(path.read_text(encoding="utf-8"))))
    elif visibility == "sealed":
        parts.append(_sealed_section(request, record))
    else:
        parts.append(_dry_run_section(request, record))
    body = _header(request, record, "results") + join(parts)
    return app.page(request, f"Results · {record.config().name}", body, section="experiments")


def final_download(app: App, request: Request) -> Response:
    record = _record(app, request)
    path = record.final_file(request.params["name"])
    if path is None:
        raise HTTPError(404, "There is no such file for this evaluation.")
    kind = "application/json" if path.suffix == ".json" else "text/markdown; charset=utf-8"
    return Response.download(path.read_bytes(), f"{record.id}-{path.name}", kind)


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def activity_page(app: App, request: Request) -> Response:
    record = _record(app, request)
    audit = record.audit()
    verification = audit.verify()
    rows = []
    for entry in reversed(audit.entries):
        detail = {k: v for k, v in entry.detail.items() if k != "experiment_id"}
        text = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(detail.items()))
        rows.append((str(entry.sequence), _when(entry.timestamp),
                     _transition_label(entry.action), entry.actor, text or "—"))
    status = (render("<p>{chip} The record is intact: {n} entries, each linked to the one "
                     "before by its hash. Head digest <code>{h}</code>. Publishing this digest "
                     "somewhere outside this system at pilot close makes later rewriting "
                     "detectable.</p>", chip=status_chip("ok"), n=len(audit), h=audit.head)
              if verification.valid else
              render('<p class="banner banner-critical">{chip} {m}</p>',
                     chip=status_chip("invalid"), m=verification.message))
    body = _header(request, record, "activity") + render(
        '<section class="card"><h2>Governance record</h2>{s}'
        '<p><a href="/experiments/{id}/audit.jsonl">Download the record</a> '
        "(verify it anywhere with <code>civicexp verify</code>)</p>{t}</section>",
        s=status, id=record.id,
        t=table(["#", "When", "What", "Who", "Detail"], rows, numeric=(0,)))
    return app.page(request, f"Activity · {record.config().name}", body, section="experiments")


def audit_download(app: App, request: Request) -> Response:
    record = _record(app, request)
    data = record.audit_path.read_bytes() if record.audit_path.exists() else b""
    return Response.download(data, f"{record.id}-audit.jsonl", "application/jsonl")


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


def _base_url(app: App, request: Request) -> str:
    host = request.headers.get("Host", "localhost")
    scheme = "https" if app.secure_cookies else "http"
    if app.trust_proxy:
        scheme = request.headers.get("X-Forwarded-Proto", scheme)
        host = request.headers.get("X-Forwarded-Host", host)
    return f"{scheme}://{host}"


def connect_page(app: App, request: Request) -> Response:
    record = _record(app, request)
    config = record.config()
    base = _base_url(app, request)
    attributes = {r.field: r.value if r.operator == "eq" else "…"
                  for r in config.eligibility.include}
    for name in config.segments:
        attributes.setdefault(name, "…")
    assign_body = json.dumps({"unit_id": "<the person's case or application number>",
                              "attributes": attributes}, indent=2)
    keys = [k for k in app.workspace.keys.list()
            if not k.revoked and (not k.experiments or record.id in k.experiments)]
    key_rows = [(k.name, k.key_id, ", ".join(sorted(k.scopes)),
                 "this evaluation" if k.experiments else "every evaluation") for k in keys]
    create = Markup("")
    if request.user.can("workspace.admin"):
        create = render(
            '<form method="post" action="/admin/keys" class="row-form">{csrf}'
            '{exp}{scope}{name}<button class="btn btn-secondary" type="submit">Create a '
            "collection key for this evaluation</button></form>",
            csrf=csrf_field(request.session.csrf_token), exp=hidden("experiment", record.id),
            scope=hidden("scopes", "collect"),
            name=field("name", "Key name", value=f"{record.id} service"))
    body = _header(request, record, "connect") + render(
        '<section class="card"><h2>Connect a live service</h2>'
        "<p>Two calls. Ask which version a person should see; then record what happened. "
        "The service sends the person's identifier; it is pseudonymized on arrival and never "
        "stored. Only the groups declared for equity review are kept from the attributes.</p>"
        "<h3>1. Which version?</h3><pre><code>POST {base}/api/v1/experiments/{id}/assign\n"
        "Authorization: Bearer &lt;collection key&gt;\nContent-Type: application/json\n\n{ab}"
        "</code></pre><p>The answer's <code>serve</code> field is always safe to render. When "
        "the pilot is not running, it is the current version.</p>"
        "<h3>2. What happened?</h3><pre><code>POST {base}/api/v1/experiments/{id}/events\n\n"
        '{{"unit_id": "&lt;same identifier&gt;", "event_type": "submission_succeeded"}}'
        "</code></pre><p>Outcomes are accepted only for people the pilot enrolled, against "
        "the version they were assigned.</p>"
        "<h3>From a web page</h3><p>Load <code>{base}/static/civicexp-client.js</code>. It "
        "fails open: if this platform is slow or down, the person gets the current "
        "version.</p><pre><code>const cx = new CivicExp({{ baseUrl: \"{base}\", apiKey: KEY, "
        'experimentId: "{id}" }});\nconst {{ serve }} = await cx.assign(caseNumber, '
        "attributes);\n// …later\ncx.record(caseNumber, \"submission_succeeded\");"
        "</code></pre><p class=\"help\">Calling from your own server keeps the key private. "
        "A key used in a browser is visible to anyone, so give it the collect scope only and "
        "limit it to this evaluation.</p></section>"
        '<section class="card"><h2>No live integration? Use spreadsheets</h2>'
        "<p>For reminders, notices, and other messages sent in batches:</p><pre><code>"
        "civicexp assign-batch --workspace WORKSPACE --experiment {id} people.csv "
        "--id-column case_number --out send-list.csv\n"
        "civicexp import-csv --workspace WORKSPACE --experiment {id} outcomes.csv "
        "--id-column case_number --event-column outcome --timestamp-column date "
        "--timezone America/Los_Angeles --event-map \"Renewed=submission_succeeded\""
        "</code></pre></section>"
        '<section class="card"><h2>Keys that can reach this evaluation</h2>{keys}{create}'
        "</section>",
        base=base, id=record.id, ab=assign_body,
        keys=table(["Name", "Key id", "Scopes", "Limited to"], key_rows,
                   empty="No active keys. An administrator creates them."),
        create=create)
    return app.page(request, f"Connect · {config.name}", body, section="experiments")


# ---------------------------------------------------------------------------
# Evidence library
# ---------------------------------------------------------------------------


def library_page(app: App, request: Request) -> Response:
    library = app.workspace.library
    text = request.arg("q")
    decision = request.arg("decision")
    workflow = request.arg("workflow")
    include = request.arg("synthetic", "1") == "1"
    records = library.search(text, decision=decision, workflow=workflow,
                             include_synthetic=include)
    cards = []
    for case in records:
        primary = case.data.get("primary") or {}
        effect = primary.get("difference")
        ci = primary.get("ci") or [None, None]
        measure = Markup("")
        if isinstance(effect, (int, float)) and None not in ci:
            measure = render("<p>{m}: {e} (plausible range {lo} to {hi})</p>",
                             m=primary.get("metric", ""), e=format_value(effect, "pts"),
                             lo=format_value(ci[0], "pts"), hi=format_value(ci[1], "pts"))
        cards.append(render(
            '<article class="case-card"><h2><a href="/library/{id}">{title}</a></h2>'
            '<p class="meta">{who} · {wf} · published {date}</p><p>{chip}{syn}</p>{m}</article>',
            id=case.case_id, title=case.title, who=case.jurisdiction or "Unattributed",
            wf=case.workflow or case.service_area or "workflow not recorded",
            date=case.data.get("published", ""), chip=decision_chip(case.decision),
            syn=Markup(' <span class="tag tag-muted">Synthetic</span>') if case.synthetic
            else Markup(""), m=measure))
    workflows = [("", "Any workflow")] + [(name, name) for name in TEMPLATES]
    decisions = [("", "Any outcome"), ("promote", "Adopted"), ("iterate", "Revise"),
                 ("rollback", "Rolled back"), ("inconclusive", "Inconclusive"),
                 ("invalid", "Invalid")]
    importer = Markup("")
    if request.user.can("library.import"):
        importer = render(
            '<details class="card"><summary>Add a case record from another agency</summary>'
            '<form method="post" action="/library/import" enctype="multipart/form-data" '
            'class="stack">{csrf}<div class="field"><label for="f-file">Case record file '
            '(.json)</label><input type="file" id="f-file" name="file" accept=".json,'
            'application/json"></div>{text}<button class="btn btn-secondary" type="submit">'
            "Add to the library</button></form></details>",
            csrf=csrf_field(request.session.csrf_token),
            text=field("record", "…or paste it", kind="textarea", rows=6))
    body = render(
        '<h1>Evidence library</h1><p class="lede">What other evaluations found, in one '
        "format, positive and negative results alike. Use it before planning a test: someone "
        "may already have answered your question.</p>"
        '<form method="get" action="/library" class="filters" role="search">{q}{wf}{dec}'
        '{syn}<button class="btn btn-secondary" type="submit">Filter</button></form>'
        '<p class="muted">{n} case(s)</p><div class="case-grid">{cards}</div>{importer}',
        q=field("q", "Search", value=text), wf=field("workflow", "Workflow", kind="select",
                                                     value=workflow, options=workflows),
        dec=field("decision", "Outcome", kind="select", value=decision, options=decisions),
        syn=field("synthetic", "Include synthetic demonstration cases", kind="select",
                  value="1" if include else "0", options=[("1", "Yes"), ("0", "No")]),
        n=len(records), cards=join(cards) or Markup('<p class="empty">No cases match.</p>'),
        importer=importer)
    return app.page(request, "Evidence library", body, section="library")


def library_case(app: App, request: Request) -> Response:
    library = app.workspace.library
    case = library.get(request.params["case"])
    if case is None:
        raise HTTPError(404, "There is no case with that id in this library.")
    summary = library.summary_markdown(case.case_id)
    content = markdown_to_html(summary) if summary else render(
        "<h2>What was tested</h2><p>{w}</p><h2>Outcome</h2><p>{d}</p><ul>{r}</ul>",
        w=case.data.get("what_was_tested", ""), d=case.data.get("decision_headline", ""),
        r=join(render("<li>{x}</li>", x=x) for x in case.data.get("rationale") or []))
    replicate = Markup("")
    if case.workflow in TEMPLATES and request.user.can("experiment.create"):
        replicate = render('<a class="btn btn-primary" href="/experiments/new/{wf}?from_case={id}">'
                           "Plan a replication</a>", wf=case.workflow, id=case.case_id)
    body = render(
        '<p class="breadcrumb"><a href="/library">Evidence library</a></p>'
        '<div class="page-title"><div><h1>{title}</h1><p class="meta">{who} · published '
        "{date}</p></div><div>{chip}{syn}</div></div>"
        '<p class="actions">{rep} <a class="btn btn-secondary" href="/library/{id}.json">'
        "Download the case record</a></p>"
        '<section class="card"><div class="readout">{content}</div></section>',
        title=case.title, who=case.jurisdiction or "Unattributed",
        date=case.data.get("published", ""), chip=decision_chip(case.decision),
        syn=Markup(' <span class="tag tag-muted">Synthetic</span>') if case.synthetic
        else Markup(""), rep=replicate, id=case.case_id, content=content)
    return app.page(request, case.title, body, section="library")


def library_download(app: App, request: Request) -> Response:
    case = app.workspace.library.get(request.params["case"])
    if case is None:
        raise HTTPError(404, "There is no case with that id in this library.")
    return Response.download(json.dumps(dict(case.data), indent=2, sort_keys=True) + "\n",
                             f"{case.case_id}.json", "application/json")


def library_import(app: App, request: Request) -> Response:
    _need(request, "library.import")
    upload = request.files().get("file")
    text = upload[1].decode("utf-8", "replace") if upload and upload[1] else request.value("record")
    try:
        case = app.workspace.library.import_json(text)
    except CivicExpError as exc:
        app.flash(request, "error", f"Not added: {exc.message}")
        return Response.redirect("/library")
    app.workspace.record_admin("library:imported", request.user.username, case_id=case.case_id)
    app.flash(request, "success", "Added to the evidence library.")
    return Response.redirect(f"/library/{case.case_id}")


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------


def _role_checkboxes(prefix: str, selected: frozenset[str] | set[str]) -> Markup:
    return render(
        '<fieldset class="roles"><legend>Roles</legend>{boxes}</fieldset>',
        boxes=join(
            render('<div class="field-check"><input type="checkbox" id="{p}-{r}" name="roles" '
                   'value="{r}"{c}><label for="{p}-{r}">{n}<span class="help"> — {d}</span>'
                   "</label></div>",
                   p=prefix, r=role, n=_role(role), d=ROLE_DESCRIPTIONS[role],
                   c=Markup(" checked") if role in selected else Markup(""))
            for role in ROLES))


def admin_page(app: App, request: Request, *, new_token: str = "") -> Response:
    _need(request, "workspace.admin")
    ws = app.workspace
    settings = ws.settings
    csrf = request.session.csrf_token
    token_html = Markup("")
    if new_token:
        token_html = render(
            '<div class="banner banner-info" role="alert"><p><strong>Copy this key now.</strong> '
            "It is shown once and stored only as a digest.</p><pre><code>{t}</code></pre>"
            '<button type="button" class="btn btn-secondary" data-copy="{t}">Copy</button></div>',
            t=new_token)
    user_rows = []
    for user in ws.users.list():
        user_rows.append(render(
            '<details class="user-row"><summary><strong>{name}</strong> ({u}) — {roles}{dis}'
            "</summary>"
            '<form method="post" action="/admin/users/{u}" class="stack">{csrf}{roles_form}'
            '{dn}{pw}<div class="form-actions"><button class="btn btn-secondary" name="op" '
            'value="save" type="submit">Save</button><button class="btn btn-quiet" name="op" '
            'value="{toggle}" type="submit">{toggle_label}</button></div></form></details>',
            name=user.display_name, u=user.username,
            roles=", ".join(_role(r) for r in sorted(user.roles)) or "no roles",
            dis=" — disabled" if user.disabled else "", csrf=csrf_field(csrf),
            roles_form=_role_checkboxes(f"u-{user.username}", user.roles),
            dn=field(f"display_name_{user.username}", "Display name", value=user.display_name),
            pw=field(f"password_{user.username}", "Set a new password (optional)",
                     kind="password", attrs={"autocomplete": "new-password"}),
            toggle="enable" if user.disabled else "disable",
            toggle_label="Enable account" if user.disabled else "Disable account"))
    key_rows = []
    for key in ws.keys.list():
        key_rows.append((
            key.name, key.key_id, ", ".join(sorted(key.scopes)),
            ", ".join(sorted(key.experiments)) or "every evaluation",
            f"{key.created_by}, {_when(key.created)}",
            Markup("Revoked") if key.revoked else button_form(
                f"/admin/keys/{key.key_id}/revoke", "Revoke", csrf, style="quiet",
                confirm="Revoke this key? Services using it will stop being able to collect.")))
    experiments = [("", "Every evaluation")] + [(r.id, r.id) for r in ws.experiments()]
    audit_rows = [(_when(e.timestamp), _transition_label(e.action), e.actor,
                   ", ".join(f"{k}: {v}" for k, v in sorted(e.detail.items())))
                  for e in list(reversed(ws.audit().entries))[:40]]
    body = render(
        "<h1>Administration</h1>{token}"
        '<section class="card"><h2>Workspace</h2><form method="post" action="/admin/settings" '
        'class="stack">{csrf}{name}{jur}{distinct}{stale}{synthetic}<button class="btn '
        'btn-primary" type="submit">Save settings</button></form></section>'
        '<section class="card"><h2>People</h2><p class="help">One person per review role keeps '
        "any single person from approving a pilot alone. Administrators manage accounts and "
        "keys but hold no approval authority.</p>{users}"
        '<details class="card-inner"><summary>Add a person</summary>'
        '<form method="post" action="/admin/users" class="stack">{csrf}{nu}{nd}{np}{nroles}'
        '<button class="btn btn-primary" type="submit">Add</button></form></details></section>'
        '<section class="card"><h2>Collection keys</h2><p class="help">Live services use a key '
        "to assign people and record outcomes. Keys are shown once, stored as digests, and can "
        "be limited to one evaluation.</p>{keys}"
        '<details class="card-inner"><summary>Create a key</summary>'
        '<form method="post" action="/admin/keys" class="stack">{csrf}{kn}{ks}{ke}'
        '<button class="btn btn-primary" type="submit">Create key</button></form></details>'
        "</section>"
        '<section class="card"><h2>Administrative record</h2><p class="help">Who created '
        "accounts, changed roles, and issued keys. Hash-chained like every governance record."
        "</p>{audit}</section>",
        token=token_html, csrf=csrf_field(csrf),
        name=field("name", "Workspace name", value=settings.get("name", "")),
        jur=field("jurisdiction", "Agency or jurisdiction",
                  value=settings.get("jurisdiction", ""),
                  help="Shown on case records this workspace publishes."),
        distinct=field("require_distinct_reviewers", "Require a different person for each "
                       "review role", kind="checkbox",
                       value=settings.get("require_distinct_reviewers")),
        stale=field("stale_data_hours", "Alert when a running pilot receives no data for "
                    "this many hours", value=settings.get("stale_data_hours"), kind="number",
                    attrs={"min": "1", "step": "1"}),
        synthetic=field("synthetic_data", "Demonstration workspace: mark everything published "
                        "here as synthetic", kind="checkbox", value=settings.get("synthetic_data")),
        users=join(user_rows),
        nu=field("username", "Username", required=True, attrs={"autocapitalize": "none"}),
        nd=field("display_name", "Name"),
        np=field("password", "Temporary password", kind="password", required=True,
                 help="At least 12 characters. Ask them to change it after signing in.",
                 attrs={"autocomplete": "new-password"}),
        nroles=_role_checkboxes("new", set()),
        keys=table(["Name", "Id", "Scopes", "Limited to", "Created", ""], key_rows,
                   empty="No keys yet."),
        kn=field("name", "Name", required=True, help="Which service will use it."),
        ks=render('<fieldset class="roles"><legend>Scopes</legend>{b}</fieldset>',
                  b=join(render('<div class="field-check"><input type="checkbox" id="s-{s}" '
                                'name="scopes" value="{s}"{c}><label for="s-{s}">{s} — {d}'
                                "</label></div>", s=s, d=d,
                                c=Markup(" checked") if s == "collect" else Markup(""))
                         for s, d in SCOPES.items())),
        ke=field("experiment", "Limit to", kind="select", options=experiments),
        audit=table(["When", "What", "Who", "Detail"], audit_rows))
    return app.page(request, "Administration", body, section="admin")


def admin_settings(app: App, request: Request) -> Response:
    _need(request, "workspace.admin")
    try:
        stale = float(request.value("stale_data_hours") or 26)
    except ValueError:
        stale = 26.0
    app.workspace.update_settings(
        request.user,
        name=request.value("name").strip(),
        jurisdiction=request.value("jurisdiction").strip(),
        require_distinct_reviewers=request.value("require_distinct_reviewers") == "1",
        stale_data_hours=max(stale, 1.0),
        synthetic_data=request.value("synthetic_data") == "1",
    )
    app.flash(request, "success", "Settings saved.")
    return Response.redirect("/admin")


def admin_add_user(app: App, request: Request) -> Response:
    _need(request, "workspace.admin")
    try:
        app.workspace.add_user(
            request.user.username, request.value("username"),
            password=request.value("password"), roles=request.values("roles"),
            display_name=request.value("display_name"))
    except CivicExpError as exc:
        app.flash(request, "error", exc.message)
    else:
        app.flash(request, "success", "Account created.")
    return Response.redirect("/admin")


def admin_update_user(app: App, request: Request) -> Response:
    _need(request, "workspace.admin")
    ws = app.workspace
    username = request.params["username"]
    op = request.value("op")
    actor = request.user.username
    try:
        if op in ("disable", "enable"):
            if username == actor and op == "disable":
                raise AuthError("you cannot disable your own account", code="AUTH_SELF")
            ws.set_user_disabled(actor, username, op == "disable")
            if op == "disable":
                app.sessions.destroy_user(username)
        else:
            roles = request.values("roles")
            if username == actor and "admin" not in roles:
                raise AuthError("you cannot remove your own administrator role",
                                code="AUTH_SELF")
            ws.set_user_roles(actor, username, roles)
            ws.users.set_display_name(username, request.value(f"display_name_{username}"))
            password = request.value(f"password_{username}")
            if password:
                ws.reset_password(actor, username, password)
                app.sessions.destroy_user(username)
    except CivicExpError as exc:
        app.flash(request, "error", exc.message)
    else:
        app.flash(request, "success", f"Updated {username}.")
    return Response.redirect("/admin")


def admin_create_key(app: App, request: Request) -> Response:
    _need(request, "workspace.admin")
    experiment = request.value("experiment")
    try:
        _, token = app.workspace.create_key(
            request.user.username, request.value("name"), scopes=request.values("scopes"),
            experiments=[experiment] if experiment else [])
    except CivicExpError as exc:
        app.flash(request, "error", exc.message)
        return Response.redirect("/admin")
    return admin_page(app, request, new_token=token)


def admin_revoke_key(app: App, request: Request) -> Response:
    _need(request, "workspace.admin")
    app.workspace.revoke_key(request.user.username, request.params["key"])
    app.flash(request, "success", "Key revoked.")
    return Response.redirect("/admin")


# ---------------------------------------------------------------------------
# Account and guide
# ---------------------------------------------------------------------------


def account_page(app: App, request: Request) -> Response:
    user = request.user
    body = render(
        '<div class="narrow"><h1>Your account</h1><p>{name} ({u})</p>'
        "<h2>Your roles</h2><ul>{roles}</ul>"
        '<h2>Change your password</h2><form method="post" action="/account/password" '
        'class="stack">{csrf}{cur}{new}{again}<button class="btn btn-primary" type="submit">'
        "Change password</button></form></div>",
        name=user.display_name, u=user.username,
        roles=join(render("<li><strong>{r}</strong> — {d}</li>", r=_role(r),
                          d=ROLE_DESCRIPTIONS[r]) for r in sorted(user.roles))
        or Markup("<li>No roles. Ask an administrator.</li>"),
        csrf=csrf_field(request.session.csrf_token),
        cur=field("current", "Current password", kind="password", required=True,
                  attrs={"autocomplete": "current-password"}),
        new=field("new", "New password", kind="password", required=True,
                  help="At least 12 characters. A short phrase works well.",
                  attrs={"autocomplete": "new-password"}),
        again=field("again", "New password again", kind="password", required=True,
                    attrs={"autocomplete": "new-password"}))
    return app.page(request, "Your account", body)


def account_password(app: App, request: Request) -> Response:
    user = request.user
    ws = app.workspace
    if ws.users.authenticate(user.username, request.value("current")) is None:
        app.flash(request, "error", "Your current password was not correct.")
        return Response.redirect("/account")
    new = request.value("new")
    if new != request.value("again"):
        app.flash(request, "error", "The two new passwords did not match.")
        return Response.redirect("/account")
    try:
        check_password_policy(new, user.username)
        ws.reset_password(user.username, user.username, new)
    except CivicExpError as exc:
        app.flash(request, "error", exc.message)
        return Response.redirect("/account")
    app.sessions.destroy_user(user.username)
    response = app.sign_in(user)
    return response


def help_page(app: App, request: Request) -> Response:
    roles = table(["Role", "What it is for"],
                  [(_role(r), ROLE_DESCRIPTIONS[r]) for r in ROLES])
    body = render(
        '<div class="guide"><h1>How this works</h1>'
        '<p class="lede">An evaluation compares the current version of a service with a '
        "changed one, for people assigned at random, and decides by a rule agreed before "
        "anyone sees the results.</p>"
        '<ol class="steps">'
        "<li><h2>Plan</h2><p>Pick the workflow and answer the questions only your team can: "
        "what changes, what success means as a number, what must not get worse, and which "
        "groups to check. The plan checks itself for whether the pilot is large enough.</p></li>"
        "<li><h2>Review</h2><p>Five people — program owner, operations, privacy, legal and "
        "policy, evaluation — each sign off on that exact version of the plan. Then the "
        "program owner approves it, and it is locked.</p></li>"
        "<li><h2>Run</h2><p>The operations lead starts the pilot. The service asks which "
        "version each person sees and reports what happened. Anyone with a role can pause at "
        "any time. Harm measures, data quality, and harm to any declared group are checked "
        "continuously and raise alerts.</p></li>"
        "<li><h2>Decide</h2><p>The main result stays sealed while the pilot runs. The "
        "evaluation lead can take a recorded look, counted against the plan. When the pilot "
        "ends, the rule decides: <strong>adopt</strong> only if the whole plausible range "
        "clears the bar set before launch and no harm measure was breached; "
        "<strong>roll back</strong> if harm was confirmed, for anyone; otherwise "
        "<strong>revise</strong> or <strong>inconclusive</strong>.</p></li>"
        "<li><h2>Share</h2><p>Publish the case summary to the evidence library, whatever the "
        "outcome, so the next agency can build on it.</p></li></ol>"
        "<h2>Roles</h2>{roles}"
        "<h2>Words used here</h2><dl class=\"glossary\">"
        "<dt>Plausible range</dt><dd>The confidence interval: the range of effects consistent "
        "with the data. A result is only as certain as its range is narrow.</dd>"
        "<dt>Bar set before launch</dt><dd>The smallest improvement the program said would be "
        "worth making the change permanent.</dd>"
        "<dt>Harm measure</dt><dd>A guardrail: something that must not get worse by more than "
        "an agreed tolerance, whatever happens to the main measure.</dd>"
        "<dt>Sealed</dt><dd>Not shown while the pilot runs, so that nobody stops it the moment "
        "the number looks good.</dd>"
        "<dt>Split matched the plan</dt><dd>The sample-ratio check. If the groups are not the "
        "sizes the plan set, something in the pipeline is broken and no result can be "
        "trusted.</dd>"
        "<dt>Inconclusive</dt><dd>The pilot was too small to tell. Not the same as \"no "
        "effect\".</dd></dl>"
        '<p>Full documentation: <a href="https://github.com/devdiptapal/civic-experimentation-'
        'platform/tree/main/docs">the project documentation</a>.</p></div>',
        roles=roles)
    return app.page(request, "Guide", body, section="help")

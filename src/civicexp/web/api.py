"""The collection API a live service calls, and a read API for monitoring.

Every endpoint takes a bearer key (see :class:`civicexp.auth.ApiKey`). The
``collect`` scope can assign people and record outcomes; the ``read`` scope
can see status, safety monitoring, and alerts. Neither can see a sealed
result: the read API applies the same rule as the pages.

Errors are JSON with a stable ``code``, so a calling service can fail open
on anything unexpected without parsing prose. The contract, in full, is in
``docs/API.md``.
"""

from __future__ import annotations

from typing import Any

from ..lifecycle import State
from ..monitor import check_workspace
from .app import App, HTTPError, Request, Response

#: Most events accepted in one batch request.
MAX_BATCH = 1000
_MAX_ID = 200


def register(app: App) -> None:
    app.route("GET", "/healthz", auth="public")(health)
    app.route("POST", r"/api/v1/experiments/(?P<id>[^/]+)/assign", auth="api")(assign)
    app.route("POST", r"/api/v1/experiments/(?P<id>[^/]+)/events", auth="api")(event)
    app.route("POST", r"/api/v1/experiments/(?P<id>[^/]+)/events/batch", auth="api")(batch)
    app.route("GET", "/api/v1/experiments", auth="api")(list_experiments)
    app.route("GET", r"/api/v1/experiments/(?P<id>[^/]+)", auth="api")(experiment_status)
    app.route("GET", "/api/v1/alerts", auth="api")(alerts)


def _allow(request: Request, scope: str, experiment_id: str | None = None) -> None:
    key = request.api_key
    if key is None or not key.allows(scope, experiment_id):
        raise HTTPError(403, f"This key does not have the {scope!r} scope"
                        + (f" for {experiment_id!r}." if experiment_id else "."),
                        code="API_FORBIDDEN")


def _body(request: Request) -> dict[str, Any]:
    data = request.json()
    if not isinstance(data, dict):
        raise HTTPError(400, "The request body must be a JSON object.", code="API_BAD_JSON")
    return data


def _unit(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > _MAX_ID:
        raise HTTPError(422, f"unit_id must be 1 to {_MAX_ID} characters.",
                        code="API_BAD_UNIT")
    return text


def health(app: App, request: Request) -> Response:
    return Response.json({"status": "ok", "version": app.version})


def assign(app: App, request: Request) -> Response:
    experiment_id = request.params["id"]
    _allow(request, "collect", experiment_id)
    data = _body(request)
    unit_id = _unit(data.get("unit_id"))
    if unit_id is None:
        raise HTTPError(422, "unit_id is required.", code="API_BAD_UNIT")
    attributes = data.get("attributes") or {}
    if not isinstance(attributes, dict):
        raise HTTPError(422, "attributes must be an object.", code="API_BAD_ATTRIBUTES")
    record = app.workspace.experiment(experiment_id)
    result = record.collector().assign(unit_id, attributes)
    return Response.json(result.to_dict())


def event(app: App, request: Request) -> Response:
    experiment_id = request.params["id"]
    _allow(request, "collect", experiment_id)
    data = _body(request)
    record = app.workspace.experiment(experiment_id)
    recorded = record.collector().record(
        str(data.get("event_type", "")),
        unit_id=_unit(data.get("unit_id")),
        unit_pseudonym=data.get("unit_pseudonym"),
        variant=data.get("variant"),
        timestamp=data.get("timestamp"),
        payload=data.get("payload") if isinstance(data.get("payload"), dict) else None,
    )
    return Response.json(
        {"recorded": True, "event_type": recorded.event_type, "variant": recorded.variant,
         "timestamp": recorded.timestamp.isoformat()},
        201,
    )


def batch(app: App, request: Request) -> Response:
    experiment_id = request.params["id"]
    _allow(request, "collect", experiment_id)
    items = _body(request).get("events")
    if not isinstance(items, list):
        raise HTTPError(422, "Send {\"events\": [...]}.", code="API_BAD_BATCH")
    if len(items) > MAX_BATCH:
        raise HTTPError(413, f"At most {MAX_BATCH} events per batch.", code="API_BATCH_TOO_LARGE")
    for item in items:
        if isinstance(item, dict) and item.get("unit_id") is not None:
            item["unit_id"] = _unit(item["unit_id"])
    report = app.workspace.experiment(experiment_id).collector().record_many(items)
    return Response.json(report.to_dict(), 200 if not report.rejected else 207)


def _status(record: Any) -> dict[str, Any]:
    config = record.config()
    governance = record.governance()
    progress = record.progress()
    payload: dict[str, Any] = {
        "experiment_id": record.id,
        "name": config.name,
        "state": governance.state.value if governance.state else None,
        "collecting": governance.collecting,
        "reason": governance.reason,
        "plan_fingerprint": config.fingerprint,
        "plan_matches_approval": governance.plan_matches_approval,
        "results": record.visibility(),
        "progress": {
            "enrolled": progress["enrolled"],
            "expected_per_group": progress["expected_per_group"],
            "days_elapsed": progress["days_elapsed"],
            "planned_days": progress["planned_days"],
            "last_event_at": progress["last_event_at"].isoformat()
            if progress["last_event_at"] else None,
        },
    }
    if governance.state in (State.RUNNING, State.PAUSED, State.COMPLETED,
                            State.ROLLED_BACK, State.ARCHIVED):
        outcome = record.analyses("live")
        sealed = record.visibility() == "sealed"
        arms = []
        for result in outcome.results:
            arm: dict[str, Any] = {
                "arm": result.treatment_variant,
                "guardrails": [{"name": g.name, "status": g.status} for g in result.guardrails],
                "trust_checks": [
                    {"check": d.name, "result": d.severity.value}
                    for d in (result.diagnostics or ())
                    if not (sealed and d.name == "novelty")
                ],
                "equity_harm": [f.label for f in result.equity.harmed] if result.equity else [],
            }
            if not sealed:
                arm["decision"] = result.decision.value
                arm["primary"] = result.to_dict()["primary"]
            arms.append(arm)
        payload["monitoring"] = arms
        payload["monitoring_note"] = outcome.problem
    return payload


def list_experiments(app: App, request: Request) -> Response:
    _allow(request, "read")
    key = request.api_key
    rows = []
    for record in app.workspace.experiments():
        if key.experiments and record.id not in key.experiments:
            continue
        governance = record.governance()
        rows.append({
            "experiment_id": record.id,
            "name": record.config().name,
            "state": governance.state.value if governance.state else None,
            "collecting": governance.collecting,
        })
    return Response.json({"experiments": rows})


def experiment_status(app: App, request: Request) -> Response:
    experiment_id = request.params["id"]
    _allow(request, "read", experiment_id)
    return Response.json(_status(app.workspace.experiment(experiment_id)))


def alerts(app: App, request: Request) -> Response:
    _allow(request, "read")
    key = request.api_key
    found = [
        a.to_dict() for a in check_workspace(app.workspace)
        if not key.experiments or a.experiment_id in key.experiments
    ]
    return Response.json({"alerts": found})

"""Command-line interface.

The CLI is the surface most agency staff will actually touch, so it is built
around the questions a pilot team asks in order:

* ``validate`` -- is this configuration safe and answerable? (before launch)
* ``power``    -- can the traffic we have detect the change we care about?
* ``preview``  -- what split would this produce on identifiers we already have?
* ``assign``   -- which version does this person see?
* ``simulate`` -- what will the readout look like, on synthetic data?
* ``analyze``  -- what do the results say?
* ``report``   -- give me the readout to circulate.
* ``verify``   -- has the audit trail been altered?

The hosted platform adds commands that work on a *workspace* -- the folder
holding a team's evaluations, accounts, and keys:

* ``workspace init``, ``users``, ``keys`` -- set one up and give people access
* ``serve``    -- run the web application and collection API
* ``monitor``  -- check every evaluation; exit 2 if anything needs a person
* ``retention``-- delete event data past its approved retention period
* ``demo``     -- build a populated, synthetic workspace to explore
* ``assign-batch``, ``import-csv`` -- run a pilot from spreadsheet exports

Every command exits non-zero on failure so it can be wired into a pipeline,
and prints errors with the stable error codes from :mod:`civicexp.errors`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .analysis import Decision, analyze
from .assignment import NOT_ELIGIBLE, Assigner
from .audit import AuditLog
from .config import ExperimentConfig, load_config
from .diagnostics import Severity, run_diagnostics
from .errors import CivicExpError
from .events import EventLog
from .metrics import describe_library
from .report import render_case_summary, render_markdown, render_text_summary
from .segments import analyze_segments
from .simulate import SimulationSpec, simulate_pilot
from .stats import minimum_detectable_effect, required_sample_size_per_group
from .wizard import list_templates, load_template, run_wizard

#: Exit codes. ``2`` is reserved for "the tool worked, the answer is bad news",
#: so CI and monitoring can distinguish a broken run from a breached guardrail.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ATTENTION = 2


def _load(path: str) -> ExperimentConfig:
    config = load_config(path)
    for warning in config.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return config


def _open_log(config: ExperimentConfig, events_path: str) -> EventLog:
    return EventLog(
        config.experiment_id,
        path=events_path,
        privacy=config.privacy,
        allowed_variants=config.variants,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Interview the user and write a ready-to-review configuration."""
    out = Path(args.out)
    if out.exists() and not args.force:
        print(
            f"error: {out} already exists. Choose another path with --out, or "
            "pass --force to overwrite it.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if args.template:
        try:
            config = load_template(args.template)
        except KeyError as exc:
            print(f"error: {exc.args[0]}", file=sys.stderr)
            return EXIT_ERROR
        config.pop("_template", None)
        config["assignment"]["salt"] = secrets.token_hex(16)
        print(f"Wrote the {args.template} template to {out}.")
        print("Fill in every FILL IN marker, then run: civicexp validate", out)
    else:
        try:
            config = run_wizard()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled. Nothing was written.", file=sys.stderr)
            return EXIT_ERROR

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    if not args.template:
        print(f"Wrote {out}")
        print()
        print("Next:")
        print(f"  1. Fill in the remaining details in {out}")
        print(f"  2. civicexp validate {out}")
        print(f"  3. civicexp simulate {out} --out dry-run.jsonl   (dry run)")
        print(f"  4. civicexp report {out} dry-run.jsonl           (see the readout)")
    return EXIT_OK


def cmd_templates(args: argparse.Namespace) -> int:
    print("Workflow templates. Start one with: civicexp init --template <name>\n")
    for name, header in list_templates():
        print(f"  {name}")
        print(f"    {header['summary']}")
        print(f"    Good for:      {header['good_for']}")
        print(f"    Typical change: {header['typical_change']}")
        print()
    print("Or run 'civicexp init' with no template for a guided setup.")
    return EXIT_OK


def cmd_metrics(args: argparse.Namespace) -> int:
    print(describe_library())
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run the trust checks on their own, without producing a decision."""
    config = _load(args.config)
    log = _open_log(config, args.events)
    if not len(log):
        print(f"error: no events found in {args.events}", file=sys.stderr)
        return EXIT_ERROR

    treatments = config.treatment_variants
    treatment = args.variant or (treatments[0] if len(treatments) == 1 else None)
    if treatment is None:
        print(
            f"error: name the arm to check with --variant ({', '.join(treatments)})",
            file=sys.stderr,
        )
        return EXIT_ERROR

    report = run_diagnostics(config, log, config.control_variant, treatment)
    print(f"Data-quality checks for {config.experiment_id}\n")
    for diagnostic in report:
        print(f"  [{diagnostic.severity.value.upper():>7}] {diagnostic.name}")
        print(f"            {diagnostic.summary}")
        if diagnostic.detail and diagnostic.severity is not Severity.OK:
            print(f"            {diagnostic.detail}")
        print()

    if report.is_trustworthy:
        print("VERDICT: these results can be interpreted.")
        return EXIT_ATTENTION if report.warnings else EXIT_OK
    print("VERDICT: these results are NOT interpretable. Fix the cause and rerun.")
    return EXIT_ERROR


def cmd_equity(args: argparse.Namespace) -> int:
    """Report the pre-registered equity review on its own."""
    config = _load(args.config)
    log = _open_log(config, args.events)
    if not config.segments:
        print(
            "error: this experiment declares no segments, so there is no equity "
            "review to run. Add a 'segments' list to the configuration before "
            "launch.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    treatments = config.treatment_variants
    treatment = args.variant or (treatments[0] if len(treatments) == 1 else None)
    if treatment is None:
        print(f"error: name the arm with --variant ({', '.join(treatments)})", file=sys.stderr)
        return EXIT_ERROR

    equity = analyze_segments(config, log, config.control_variant, treatment)
    print(f"Equity review for {config.experiment_id}")
    print(f"Groups declared before launch: {', '.join(config.segments)}\n")
    width = max((len(f.label) for f in equity.findings), default=10)
    for finding in equity.findings:
        print(f"  {finding.label:<{width}}  {finding.outcome.value.upper():<10} "
              f"n={finding.control_n:,}/{finding.treatment_n:,}")
        print(f"  {'':<{width}}  {finding.note}")
        print()
    if equity.suppressed_count:
        print(f"  ({equity.suppressed_count} group(s) withheld: too few people to report)\n")
    print(equity.summary_line())
    return EXIT_ATTENTION if equity.has_equity_concern else EXIT_OK


def cmd_case_summary(args: argparse.Namespace) -> int:
    """Write the public, reusable case summary."""
    config = _load(args.config)
    log = _open_log(config, args.events)
    if not len(log):
        print(f"error: no events found in {args.events}", file=sys.stderr)
        return EXIT_ERROR
    result = analyze(config, log, treatment_variant=args.variant)
    markdown = render_case_summary(result, jurisdiction=args.jurisdiction or "")
    if args.record:
        from . import __version__ as version
        from .library import build_case_record

        raw = json.loads(Path(args.config).read_text(encoding="utf-8")) if str(
            args.config).endswith(".json") else None
        record = build_case_record(
            result, raw_config=raw, jurisdiction=args.jurisdiction or "",
            synthetic=args.synthetic, platform_version=version,
        )
        Path(args.record).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")
        print(f"Wrote case record to {args.record}", file=sys.stderr)
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"Wrote case summary to {args.out}")
    else:
        print(markdown)
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args.config)
    print(f"Configuration is valid: {config.name} [{config.experiment_id}]")
    print(f"  Variants:    {', '.join(f'{k} {v:.0%}' for k, v in sorted(config.variants.items()))}")
    print(f"  Control arm: {config.control_variant}")
    print(f"  Primary:     {config.primary_metric.name} "
          f"({config.primary_metric.target_direction}, "
          f"min effect {config.primary_metric.minimum_effect_of_interest:.1%})")
    print(f"  Guardrails:  {len(config.guardrails)}")
    print(f"  Equity:      {', '.join(config.segments) if config.segments else 'NO GROUPS DECLARED'}")
    print(f"  Retention:   {config.privacy.retention_days} days")
    print(f"  Suppression: cells under {config.privacy.suppression_threshold} units")
    note = config.power_note()
    if note:
        print(f"  Power:       {note}")
    if config.warnings:
        print(f"\n{len(config.warnings)} warning(s) reported above.")
        return EXIT_ATTENTION
    return EXIT_OK


def cmd_power(args: argparse.Namespace) -> int:
    if args.config:
        config = _load(args.config)
        baseline = args.baseline if args.baseline is not None else config.baseline_rate
        effect = (
            args.effect
            if args.effect is not None
            else config.primary_metric.minimum_effect_of_interest
        )
        alpha = 1.0 - config.decision.confidence
        power = config.decision.power
        looks = config.decision.planned_looks
    else:
        baseline, effect = args.baseline, args.effect
        alpha, power, looks = args.alpha, args.power, args.looks

    if baseline is None or effect is None:
        print(
            "error: need a baseline rate and an effect size, either from --config "
            "or from --baseline and --effect",
            file=sys.stderr,
        )
        return EXIT_ERROR

    needed = required_sample_size_per_group(
        baseline, effect, alpha=alpha, power=power, looks=looks
    )
    print(f"Baseline rate:              {baseline:.1%}")
    print(f"Effect worth acting on:     {effect:+.1%} (absolute)")
    print(f"Confidence / power:         {1 - alpha:.0%} / {power:.0%}")
    if looks > 1:
        print(f"Planned looks at the data:  {looks} (significance threshold tightened)")
    print(f"\nUnits needed per group:     {needed:,}")
    print(f"Units needed in total:      {needed * 2:,}")

    if args.available:
        detectable = minimum_detectable_effect(
            baseline, args.available, alpha=alpha, power=power, looks=looks
        )
        print(f"\nWith {args.available:,} units per group available:")
        print(f"  Smallest detectable change: {detectable:.1%}")
        if detectable > effect:
            print(
                f"  VERDICT: underpowered. This pilot could not reliably detect the "
                f"{effect:.1%} change the program cares about."
            )
            return EXIT_ATTENTION
        print("  VERDICT: adequately powered.")
    return EXIT_OK


def cmd_preview(args: argparse.Namespace) -> int:
    config = _load(args.config)
    assigner = Assigner(
        salt=config.salt,
        variants=config.variants,
        eligibility=config.eligibility,
        enrolled_fraction=config.enrolled_fraction,
    )
    attributes = json.loads(args.attributes) if args.attributes else {}
    unit_ids = [f"{args.prefix}{i}" for i in range(args.units)]
    counts = assigner.balance_report(unit_ids, attributes)
    total = sum(counts.values())
    width = max(len(name) for name in counts)

    print(f"Dry-run split over {total:,} synthetic identifiers:\n")
    for name, count in sorted(counts.items()):
        share = count / total if total else 0.0
        expected = config.variants.get(name)
        marker = f"  (configured {expected:.1%})" if expected is not None else ""
        print(f"  {name:<{width}}  {count:>8,}  {share:>7.2%}{marker}")

    if counts.get(NOT_ELIGIBLE) == total and total:
        fields = sorted(config.eligibility.referenced_fields())
        print(
            "\nEvery unit was ineligible because no attributes were supplied and the "
            f"criteria test {fields}. This is the correct behaviour for a unit whose "
            "attributes are unknown. To inspect the traffic split itself, pass "
            "attributes that satisfy the criteria, for example:\n"
            f"  --attributes '{{\"{fields[0]}\": \"...\"}}'",
            file=sys.stderr,
        )
        return EXIT_ATTENTION

    print(
        "\nThis uses synthetic identifiers only. Run it against a list of real "
        "identifiers to confirm the split before launch; no data is written."
    )
    return EXIT_OK


def cmd_assign(args: argparse.Namespace) -> int:
    config = _load(args.config)
    attributes = json.loads(args.attributes) if args.attributes else {}
    assigner = Assigner(
        salt=config.salt,
        variants=config.variants,
        eligibility=config.eligibility,
        enrolled_fraction=config.enrolled_fraction,
    )
    result = assigner.assign(args.unit_id, attributes)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"Variant:    {result.variant}")
        print(f"Reason:     {result.reason}")
        print(f"Pseudonym:  {result.unit_pseudonym[:16]}…")
        print(f"In analysis: {'yes' if result.in_analysis else 'no'}")
    return EXIT_OK


def cmd_simulate(args: argparse.Namespace) -> int:
    config = _load(args.config)
    control = config.control_variant
    treatments = config.treatment_variants
    if not treatments:
        print("error: configuration has no treatment arm", file=sys.stderr)
        return EXIT_ERROR
    treatment = treatments[0]

    spec = SimulationSpec(
        unit_attributes=json.loads(args.attributes) if args.attributes else {},
        completion_rate={control: args.control_rate, treatment: args.treatment_rate},
        error_rate={control: args.control_error, treatment: args.treatment_error},
        support_rate={control: 0.06, treatment: 0.06},
        median_seconds={control: 300.0, treatment: 285.0},
        units=args.units,
        seed=args.seed,
    )
    out = Path(args.out)
    if out.exists():
        out.unlink()
    log = simulate_pilot(config, spec, path=str(out))
    print(f"Wrote {len(log):,} synthetic events for {args.units:,} units to {out}")
    print(
        "These are generated records. No real applicant data is used anywhere in "
        "this project."
    )
    return EXIT_OK


def cmd_analyze(args: argparse.Namespace) -> int:
    config = _load(args.config)
    log = _open_log(config, args.events)
    if not len(log):
        print(f"error: no events found in {args.events}", file=sys.stderr)
        return EXIT_ERROR
    result = analyze(config, log, treatment_variant=args.variant)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(render_text_summary(result))
        for warning in result.warnings:
            print(f"  note: {warning}")
    return EXIT_ATTENTION if result.decision is Decision.ROLLBACK else EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    config = _load(args.config)
    log = _open_log(config, args.events)
    if not len(log):
        print(f"error: no events found in {args.events}", file=sys.stderr)
        return EXIT_ERROR
    result = analyze(config, log, treatment_variant=args.variant)
    audit = AuditLog(args.audit) if args.audit else None
    markdown = render_markdown(result, audit=audit)
    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"Wrote readout to {args.out}")
    else:
        print(markdown)
    return EXIT_ATTENTION if result.decision is Decision.ROLLBACK else EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    audit = AuditLog(args.audit)
    result = audit.verify()
    if result.valid:
        print(f"Audit chain intact: {result.entries_checked} entries verified.")
        print(f"Head digest: {audit.head}")
        return EXIT_OK
    print(f"AUDIT CHAIN INVALID: {result.message}", file=sys.stderr)
    print(f"First problem at entry {result.first_invalid_sequence}.", file=sys.stderr)
    return EXIT_ERROR


def cmd_salt(args: argparse.Namespace) -> int:
    print(secrets.token_hex(args.bytes))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Workspace commands
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE = "civicexp-workspace"


def _actor() -> str:
    try:
        return f"command-line:{getpass.getuser()}"
    except Exception:  # pragma: no cover - no login name available
        return "command-line"


def _workspace(args: argparse.Namespace):
    from .workspace import Workspace

    return Workspace(args.workspace)


def _read_password(args: argparse.Namespace, *, prompt: str = "Password") -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(f"{prompt}: ")
    if getpass.getpass(f"{prompt} again: ") != first:
        raise CivicExpError("the two passwords did not match", code="AUTH_MISMATCH")
    return first


def _split(text: str | None) -> list[str]:
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def cmd_workspace_init(args: argparse.Namespace) -> int:
    from .workspace import Workspace

    ws = Workspace.create(args.path, name=args.name, jurisdiction=args.jurisdiction or "",
                          actor=_actor())
    print(f"Created workspace '{ws.name}' in {args.path}")
    print()
    print("Next:")
    print(f"  1. civicexp users add <your-name> --roles admin --workspace {args.path}")
    print("  2. add one person per review role (program_owner, operations_lead,")
    print("     privacy_reviewer, legal_policy_reviewer, evaluation_lead)")
    print(f"  3. civicexp serve --workspace {args.path}")
    print()
    print("The workspace holds password hashes and pilot data. Keep it on encrypted")
    print("storage, back it up, and never commit it to a repository.")
    return EXIT_OK


def cmd_users(args: argparse.Namespace) -> int:
    from .auth import ROLES

    ws = _workspace(args)
    actor = _actor()
    if args.users_command == "list":
        users = ws.users.list()
        if not users:
            print("No accounts yet. Add one with: civicexp users add <name> --roles admin")
        for user in users:
            flag = "  (disabled)" if user.disabled else ""
            print(f"  {user.username:<20} {', '.join(sorted(user.roles)) or 'no roles'}{flag}")
        return EXIT_OK
    if args.users_command == "roles-help":
        from .auth import ROLE_DESCRIPTIONS

        for role in ROLES:
            print(f"  {role:<22} {ROLE_DESCRIPTIONS[role]}")
        return EXIT_OK
    if args.users_command == "add":
        password = _read_password(args)
        user = ws.add_user(actor, args.username, password=password,
                           roles=_split(args.roles), display_name=args.name or "")
        print(f"Created {user.username} with roles: {', '.join(sorted(user.roles))}")
        return EXIT_OK
    if args.users_command == "roles":
        user = ws.set_user_roles(actor, args.username, _split(args.roles))
        print(f"{user.username} now holds: {', '.join(sorted(user.roles)) or 'no roles'}")
        return EXIT_OK
    if args.users_command == "passwd":
        ws.reset_password(actor, args.username, _read_password(args, prompt="New password"))
        print(f"Password set for {args.username}. Their existing sessions end when the "
              "server next checks them.")
        return EXIT_OK
    if args.users_command in ("disable", "enable"):
        ws.set_user_disabled(actor, args.username, args.users_command == "disable")
        print(f"{args.username} {args.users_command}d.")
        return EXIT_OK
    return EXIT_ERROR  # pragma: no cover - argparse enforces the choices


def cmd_keys(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if args.keys_command == "list":
        keys = ws.keys.list()
        if not keys:
            print("No keys yet.")
        for key in keys:
            scope = ", ".join(sorted(key.scopes))
            where = ", ".join(sorted(key.experiments)) or "every evaluation"
            state = "revoked" if key.revoked else "active"
            print(f"  {key.key_id}  {key.name:<28} {scope:<14} {where}  [{state}]")
        return EXIT_OK
    if args.keys_command == "create":
        experiments = [args.experiment] if args.experiment else []
        if args.experiment:
            ws.experiment(args.experiment)  # fail now if it does not exist
        key, token = ws.create_key(_actor(), args.name, scopes=_split(args.scopes),
                                   experiments=experiments)
        print(f"Created key {key.key_id} ({', '.join(sorted(key.scopes))}).")
        print("Copy it now; it is stored only as a digest and will not be shown again:")
        print()
        print(f"  {token}")
        return EXIT_OK
    if args.keys_command == "revoke":
        ws.revoke_key(_actor(), args.key_id)
        print(f"Revoked {args.key_id}.")
        return EXIT_OK
    return EXIT_ERROR  # pragma: no cover


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    if args.init_if_missing and not (Path(args.workspace) / "workspace.json").exists():
        from .workspace import Workspace

        Workspace.create(args.workspace, name=os.environ.get(
            "CIVICEXP_WORKSPACE_NAME", "Civic experimentation workspace"), actor=_actor())
        print(f"Created a new workspace in {args.workspace}.")
    _workspace(args)  # fail with a clear message before binding a port
    serve(args.workspace, host=args.host, port=args.port,
          secure_cookies=args.secure_cookies, trust_proxy=args.trust_proxy)
    return EXIT_OK


def cmd_monitor(args: argparse.Namespace) -> int:
    from .monitor import check_workspace, format_alerts, post_webhook

    ws = _workspace(args)
    alerts = check_workspace(ws)
    if not args.include_info:
        alerts = [a for a in alerts if a.severity != "info"]
    if args.json:
        print(json.dumps({"alerts": [a.to_dict() for a in alerts]}, indent=2))
    else:
        print(format_alerts(alerts))
    if args.webhook and alerts:
        status = post_webhook(args.webhook, alerts, workspace_name=ws.name)
        print(f"Posted {len(alerts)} alert(s) to the webhook (HTTP {status}).", file=sys.stderr)
    needs_person = any(a.severity in ("critical", "warning") for a in alerts)
    return EXIT_ATTENTION if needs_person else EXIT_OK


def cmd_retention(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    due_any = False
    for record in ws.experiments():
        due = record.retention_due()
        if due is None:
            continue
        holding = record.events_path.exists() or record.dry_run_path.exists()
        from datetime import datetime, timezone

        if datetime.now(timezone.utc) < due or not holding:
            continue
        due_any = True
        if args.apply:
            deleted = record.apply_retention(_actor())
            names = ", ".join(sorted(deleted or {}))
            print(f"  {record.id}: deleted {names} (due {due.date()})")
        else:
            print(f"  {record.id}: event data due for deletion since {due.date()}")
    if not due_any:
        print("Nothing is past its retention period.")
        return EXIT_OK
    if not args.apply:
        print("\nRun again with --apply to delete. The plan, the decision record, and the "
              "readout are kept.")
        return EXIT_ATTENTION
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    import shutil

    from .demo import DEMO_PEOPLE, build_demo

    target = Path(args.path)
    if target.exists() and any(target.iterdir()):
        if not args.force:
            print(f"error: {target} is not empty. Choose another path, or pass --force to "
                  "replace it.", file=sys.stderr)
            return EXIT_ERROR
        if not (target / "workspace.json").exists():
            print(f"error: {target} is not a workspace; refusing to delete it.",
                  file=sys.stderr)
            return EXIT_ERROR
        shutil.rmtree(target)
    print("Building a demonstration workspace with synthetic data. This takes about a "
          "minute...")
    ws, password = build_demo(target, password=args.password)
    print(f"\nCreated '{ws.name}' in {target}. Every record in it is synthetic.\n")
    print("Sign in as any of these people (all share one password):\n")
    for username, _, roles in DEMO_PEOPLE:
        print(f"  {username:<12} {', '.join(roles)}")
    print(f"\n  password: {password}\n")
    print(f"Start it with:  civicexp serve --workspace {target}")
    return EXIT_OK


def _collector_for(args: argparse.Namespace):
    """The collector for a workspace evaluation, or for a config + events file."""
    from .collect import Collector

    if args.workspace and args.experiment:
        return _workspace(args).experiment(args.experiment).collector()
    if not (args.config and args.events):
        raise CivicExpError(
            "say where to record: either --workspace and --experiment, or --config and "
            "--events",
            code="CSV_NO_TARGET",
        )
    return Collector(_load(args.config), args.events)


def cmd_assign_batch(args: argparse.Namespace) -> int:
    from .csvio import assign_batch, read_csv_text, write_csv_text

    collector = _collector_for(args)
    fields, rows = read_csv_text(Path(args.csv).read_text(encoding="utf-8"))
    if args.id_column not in fields:
        print(f"error: no column named {args.id_column!r}; columns are {fields}",
              file=sys.stderr)
        return EXIT_ERROR
    report, out = assign_batch(collector, rows, id_column=args.id_column,
                               attribute_columns=_split(args.attribute_columns) or None)
    Path(args.out).write_text(
        write_csv_text([args.id_column, "version", "in_evaluation"], out), encoding="utf-8")
    newly = ", ".join(f"{k} {v:,}" for k, v in sorted(report.newly_enrolled.items())) or "none"
    print(f"Read {report.rows:,} rows.")
    print(f"  Newly enrolled:     {newly}")
    print(f"  Already enrolled:   {report.already_enrolled:,}")
    print(f"  Not in evaluation:  {report.not_in_evaluation:,} (they get the current version)")
    for line, problem in report.problems[:20]:
        print(f"  line {line}: {problem}", file=sys.stderr)
    print(f"Wrote the send list to {args.out}. It contains identifiers: handle it like the "
          "export it came from.")
    return EXIT_ATTENTION if report.problems else EXIT_OK


def cmd_import_csv(args: argparse.Namespace) -> int:
    from .csvio import (
        import_outcomes,
        parse_event_map,
        profile_columns,
        read_csv_text,
        resolve_timezone,
    )

    fields, rows = read_csv_text(Path(args.csv).read_text(encoding="utf-8"))
    if args.profile:
        print(f"{len(rows):,} rows, {len(fields)} columns:\n")
        for column in profile_columns(fields, rows):
            values = f"  values: {', '.join(column['values'])}" if column["values"] else ""
            flag = "  (named like personal data)" if column["named_like_personal_data"] else ""
            print(f"  {column['column']:<24} {column['kind']:<12} "
                  f"{column['distinct']:>6,} distinct{flag}{values}")
        return EXIT_OK
    missing = [c for c in (args.id_column, args.event_column, args.timestamp_column)
               if c not in fields]
    if missing:
        print(f"error: missing column(s) {missing}; columns are {fields}. Run with --profile "
              "to see what the file holds.", file=sys.stderr)
        return EXIT_ERROR
    collector = _collector_for(args)
    report = import_outcomes(
        collector, rows, id_column=args.id_column, event_column=args.event_column,
        timestamp_column=args.timestamp_column, event_map=parse_event_map(args.event_map),
        zone=resolve_timezone(args.timezone), dry_run=args.dry_run,
    )
    verb = "Would record" if args.dry_run else "Recorded"
    print(f"Read {report.rows:,} rows. {verb} {report.accepted:,}.")
    for label, count in sorted(report.unmapped.items()):
        print(f"  skipped {count:,} row(s) with outcome {label!r} (not in the event map)")
    for code, count in sorted(report.rejected_by_code().items()):
        print(f"  rejected {count:,} row(s): {code}")
    for line, code, message in report.rejected[:10]:
        print(f"  line {line}: [{code}] {message}", file=sys.stderr)
    return EXIT_ATTENTION if report.rejected else EXIT_OK


def cmd_library(args: argparse.Namespace) -> int:
    ws = _workspace(args)
    if args.library_command == "list":
        records = ws.library.list()
        if not records:
            print("The evidence library is empty.")
        for case in records:
            tag = " [synthetic]" if case.synthetic else ""
            print(f"  {case.decision.upper():<12} {case.title} — "
                  f"{case.jurisdiction or 'unattributed'}{tag}")
            print(f"  {'':<12} {case.case_id}")
        return EXIT_OK
    if args.library_command == "import":
        case = ws.library.import_json(Path(args.file).read_text(encoding="utf-8"),
                                      replace=args.replace)
        ws.record_admin("library:imported", _actor(), case_id=case.case_id)
        print(f"Added {case.case_id}.")
        return EXIT_OK
    return EXIT_ERROR  # pragma: no cover


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="civicexp",
        description=(
            "Run a controlled evaluation of a digital public service change, with "
            "governance, privacy, and plain-language reporting built in."
        ),
        epilog="Documentation: https://github.com/devdiptapal/civic-experimentation-platform",
    )
    parser.add_argument("--version", action="version", version=f"civicexp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="set up a new evaluation by answering questions")
    p.add_argument("--out", default="experiment.json", help="where to write the config")
    p.add_argument(
        "--template",
        help="skip the questions and copy a template verbatim "
        "(see: civicexp templates)",
    )
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("templates", help="list the workflow templates available")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("metrics", help="list the standard metric definitions")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("doctor", help="run data-quality checks on a pilot's events")
    p.add_argument("config")
    p.add_argument("events")
    p.add_argument("--variant")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("equity", help="report the pre-registered equity review")
    p.add_argument("config")
    p.add_argument("events")
    p.add_argument("--variant")
    p.set_defaults(func=cmd_equity)

    p = sub.add_parser(
        "case-summary", help="write a public summary other jurisdictions can reuse"
    )
    p.add_argument("config")
    p.add_argument("events")
    p.add_argument("--variant")
    p.add_argument("--jurisdiction", help="your agency or county, for the header")
    p.add_argument("--out")
    p.add_argument("--record", help="also write the machine-readable case record (JSON) "
                   "that an evidence library imports")
    p.add_argument("--synthetic", action="store_true",
                   help="mark the case record as describing synthetic data")
    p.set_defaults(func=cmd_case_summary)

    p = sub.add_parser("validate", help="check an experiment configuration before launch")
    p.add_argument("config", help="path to the experiment config (YAML or JSON)")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("power", help="sample size and detectable-effect calculations")
    p.add_argument("--config", help="read parameters from an experiment config")
    p.add_argument("--baseline", type=float, help="current rate, e.g. 0.62")
    p.add_argument("--effect", type=float, help="absolute change worth acting on, e.g. 0.03")
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--power", type=float, default=0.80)
    p.add_argument("--looks", type=int, default=1, help="planned interim analyses")
    p.add_argument("--available", type=int, help="units per group actually available")
    p.set_defaults(func=cmd_power)

    p = sub.add_parser("preview", help="dry-run the traffic split, writing nothing")
    p.add_argument("config")
    p.add_argument("--units", type=int, default=10000)
    p.add_argument("--prefix", default="preview-unit-")
    p.add_argument(
        "--attributes",
        help="JSON object of attributes applied to every synthetic unit, so that "
        "eligibility criteria can be satisfied",
    )
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("assign", help="show which version one unit would receive")
    p.add_argument("config")
    p.add_argument("unit_id")
    p.add_argument("--attributes", help="JSON object of unit attributes")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_assign)

    p = sub.add_parser("simulate", help="generate synthetic pilot data for a dry run")
    p.add_argument("config")
    p.add_argument("--out", default="events.jsonl")
    p.add_argument("--units", type=int, default=2000)
    p.add_argument(
        "--attributes",
        help="JSON object of attributes applied to every synthetic unit",
    )
    p.add_argument("--control-rate", type=float, default=0.62)
    p.add_argument("--treatment-rate", type=float, default=0.68)
    p.add_argument("--control-error", type=float, default=0.05)
    p.add_argument("--treatment-error", type=float, default=0.05)
    p.add_argument("--seed", type=int, default=20260904)
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("analyze", help="compare the arms and apply the decision rule")
    p.add_argument("config")
    p.add_argument("events", help="path to the JSON Lines event log")
    p.add_argument("--variant", help="treatment arm to compare (if more than one)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("report", help="write the standardized plain-language readout")
    p.add_argument("config")
    p.add_argument("events")
    p.add_argument("--variant")
    p.add_argument("--audit", help="audit log to append as the decision record")
    p.add_argument("--out", help="write to a file instead of standard output")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("verify", help="check an audit trail for tampering")
    p.add_argument("audit")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("salt", help="generate a random assignment salt")
    p.add_argument("--bytes", type=int, default=16)
    p.set_defaults(func=cmd_salt)

    # -- the hosted platform --------------------------------------------------
    default_ws = os.environ.get("CIVICEXP_WORKSPACE", DEFAULT_WORKSPACE)

    def workspace_arg(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--workspace", default=default_ws,
            help=f"workspace folder (default: $CIVICEXP_WORKSPACE or {DEFAULT_WORKSPACE})",
        )

    p = sub.add_parser("workspace", help="create a workspace for the hosted platform")
    wsub = p.add_subparsers(dest="workspace_command", required=True)
    q = wsub.add_parser("init", help="create a new, empty workspace")
    q.add_argument("path")
    q.add_argument("--name", required=True, help="e.g. 'County Human Services Agency'")
    q.add_argument("--jurisdiction", help="shown on published case records")
    q.set_defaults(func=cmd_workspace_init)

    p = sub.add_parser("users", help="manage accounts and roles in a workspace")
    usub = p.add_subparsers(dest="users_command", required=True)
    q = usub.add_parser("list", help="list accounts")
    workspace_arg(q)
    q = usub.add_parser("roles-help", help="explain each role")
    workspace_arg(q)
    q = usub.add_parser("add", help="create an account")
    q.add_argument("username")
    q.add_argument("--roles", required=True, help="comma separated; see users roles-help")
    q.add_argument("--name", help="display name")
    q.add_argument("--password-stdin", action="store_true",
                   help="read the password from standard input instead of prompting")
    workspace_arg(q)
    q = usub.add_parser("roles", help="replace an account's roles")
    q.add_argument("username")
    q.add_argument("--roles", required=True)
    workspace_arg(q)
    q = usub.add_parser("passwd", help="set an account's password")
    q.add_argument("username")
    q.add_argument("--password-stdin", action="store_true")
    workspace_arg(q)
    for name in ("disable", "enable"):
        q = usub.add_parser(name, help=f"{name} an account")
        q.add_argument("username")
        workspace_arg(q)
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("keys", help="manage collection API keys")
    ksub = p.add_subparsers(dest="keys_command", required=True)
    q = ksub.add_parser("list")
    workspace_arg(q)
    q = ksub.add_parser("create", help="create a key; it is shown once")
    q.add_argument("--name", required=True, help="which service will use it")
    q.add_argument("--scopes", default="collect", help="collect, read, or collect,read")
    q.add_argument("--experiment", help="limit the key to one evaluation")
    workspace_arg(q)
    q = ksub.add_parser("revoke")
    q.add_argument("key_id")
    workspace_arg(q)
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("serve", help="run the web application and collection API")
    workspace_arg(p)
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default: this machine only)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--secure-cookies", action="store_true",
                   help="mark session cookies Secure; use when served over HTTPS")
    p.add_argument("--trust-proxy", action="store_true",
                   help="trust X-Forwarded-For/-Proto from a reverse proxy in front")
    p.add_argument("--init-if-missing", action="store_true",
                   help="create an empty workspace if none exists (for containers)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("monitor", help="check every evaluation; exit 2 if a person is needed")
    workspace_arg(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--webhook", help="https URL of a chat webhook to post alerts to")
    p.add_argument("--include-info", action="store_true",
                   help="also report informational items (waiting sign-offs, etc.)")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("retention", help="delete event data past its retention period")
    workspace_arg(p)
    p.add_argument("--apply", action="store_true", help="actually delete (default: report)")
    p.set_defaults(func=cmd_retention)

    p = sub.add_parser("demo", help="build a populated demonstration workspace")
    p.add_argument("path", nargs="?", default="demo-workspace")
    p.add_argument("--force", action="store_true", help="replace an existing demo workspace")
    p.add_argument("--password", help="password for every demo account (default: random)")
    p.set_defaults(func=cmd_demo)

    def target_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--workspace", help="workspace folder (with --experiment)")
        parser.add_argument("--experiment", help="evaluation id in the workspace")
        parser.add_argument("--config", help="experiment config (with --events), "
                            "instead of a workspace")
        parser.add_argument("--events", help="event log to append to (with --config)")

    p = sub.add_parser("assign-batch", help="assign everyone in a spreadsheet and write a "
                       "send list")
    p.add_argument("csv", help="one row per person, with an identifier column")
    p.add_argument("--id-column", required=True)
    p.add_argument("--attribute-columns", help="columns to use for eligibility and equity "
                   "(default: every column except the identifier)")
    p.add_argument("--out", required=True, help="where to write the send list (CSV)")
    target_args(p)
    p.set_defaults(func=cmd_assign_batch)

    p = sub.add_parser("import-csv", help="record outcomes from a spreadsheet export")
    p.add_argument("csv")
    p.add_argument("--profile", action="store_true",
                   help="describe the file's columns (without showing identifiers) and stop")
    p.add_argument("--id-column")
    p.add_argument("--event-column")
    p.add_argument("--timestamp-column")
    p.add_argument("--event-map", help="LABEL=event_type,... for the export's own outcome "
                   "labels")
    p.add_argument("--timezone", help="zone of timestamps without one, e.g. "
                   "America/Los_Angeles")
    p.add_argument("--dry-run", action="store_true", help="check every row; record nothing")
    target_args(p)
    p.set_defaults(func=cmd_import_csv)

    p = sub.add_parser("library", help="the workspace's evidence library")
    lsub = p.add_subparsers(dest="library_command", required=True)
    q = lsub.add_parser("list")
    workspace_arg(q)
    q = lsub.add_parser("import", help="add a case record from another agency")
    q.add_argument("file")
    q.add_argument("--replace", action="store_true")
    workspace_arg(q)
    p.set_defaults(func=cmd_library)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CivicExpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"error: file not found: {exc.filename}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:  # pragma: no cover - piping into head, etc.
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

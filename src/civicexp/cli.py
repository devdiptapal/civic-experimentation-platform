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

Every command exits non-zero on failure so it can be wired into a pipeline,
and prints errors with the stable error codes from :mod:`civicexp.errors`.
"""

from __future__ import annotations

import argparse
import json
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

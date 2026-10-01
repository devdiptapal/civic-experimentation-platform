"""Loading and validating the one artifact an agency actually approves.

An experiment configuration is the contract between the people who reviewed
a pilot and the software that runs it. Everything downstream -- who is
eligible, which arms exist, what counts as success, what triggers a rollback
-- is read from here, so this module is strict on purpose.

Two behaviours are worth calling out:

* **All errors are reported at once.** Validation collects every problem it
  finds rather than stopping at the first. A reviewer fixing a config should
  need one round trip, not seven.
* **Warnings are distinct from errors.** A config that is valid but unwise
  (an underpowered pilot, a rollback rule that can never fire) loads
  successfully and reports warnings, because the agency, not the tool, owns
  that judgment.

Configs may be written as YAML or JSON. YAML requires PyYAML; JSON works with
no third-party dependency at all, which matters in environments where adding
a package requires its own review.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .eligibility import EligibilityCriteria
from .errors import ConfigError
from .metrics import expand_metric
from .privacy import DEFAULT_SUPPRESSION_THRESHOLD, PrivacyPolicy
from .stats import minimum_detectable_effect, required_sample_size_per_group

__all__ = [
    "MetricSpec",
    "GuardrailSpec",
    "DecisionRule",
    "ExperimentConfig",
    "load_config",
]

_DIRECTIONS = ("increase", "decrease")
_METRIC_KINDS = ("proportion", "duration")


@dataclass(frozen=True)
class MetricSpec:
    """The primary outcome, defined precisely enough to be recomputed."""

    name: str
    definition: str
    kind: str
    target_direction: str
    numerator_event: str
    denominator_event: str
    minimum_effect_of_interest: float

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], errors: list[str]) -> MetricSpec | None:
        if not isinstance(data, Mapping):
            errors.append("metrics.primary must be a mapping")
            return None
        try:
            # Expand a {"use": "<standard metric>"} reference, if present.
            data = expand_metric(data)
        except KeyError as exc:
            errors.append(f"metrics.primary: {exc.args[0]}")
            return None
        missing = {
            "name",
            "definition",
            "numerator_event",
            "denominator_event",
            "minimum_effect_of_interest",
        } - set(data)
        if missing:
            errors.append(
                f"metrics.primary is missing: {', '.join(sorted(missing))}"
            )
            return None
        direction = str(data.get("target_direction", "increase"))
        if direction not in _DIRECTIONS:
            errors.append(
                f"metrics.primary.target_direction must be one of {_DIRECTIONS}, "
                f"got {direction!r}"
            )
            return None
        kind = str(data.get("kind", "proportion"))
        if kind not in _METRIC_KINDS:
            errors.append(f"metrics.primary.kind must be one of {_METRIC_KINDS}")
            return None
        try:
            mei = float(data["minimum_effect_of_interest"])
        except (TypeError, ValueError):
            errors.append(
                "metrics.primary.minimum_effect_of_interest must be a number "
                "(an absolute change, so 0.03 means three percentage points). "
                "Placeholder text is not accepted: an experiment without a stated "
                "effect of interest cannot produce a decision."
            )
            return None
        if mei <= 0:
            errors.append(
                "metrics.primary.minimum_effect_of_interest must be positive; "
                "express the desired direction with target_direction"
            )
            return None
        return cls(
            name=str(data["name"]),
            definition=str(data["definition"]),
            kind=kind,
            target_direction=direction,
            numerator_event=str(data["numerator_event"]),
            denominator_event=str(data["denominator_event"]),
            minimum_effect_of_interest=mei,
        )


@dataclass(frozen=True)
class GuardrailSpec:
    """A metric that must not get materially worse while the pilot runs."""

    name: str
    definition: str
    kind: str
    numerator_event: str
    denominator_event: str
    #: Largest tolerable worsening, as an absolute change for proportions or
    #: seconds for durations. Always expressed as a positive number.
    max_tolerated_increase: float

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], index: int, errors: list[str]
    ) -> GuardrailSpec | None:
        if not isinstance(data, Mapping):
            errors.append(f"metrics.guardrails[{index}] must be a mapping")
            return None
        try:
            data = expand_metric(data)
        except KeyError as exc:
            errors.append(f"metrics.guardrails[{index}]: {exc.args[0]}")
            return None
        kind = str(data.get("kind", "proportion"))
        if kind not in _METRIC_KINDS:
            errors.append(
                f"metrics.guardrails[{index}].kind must be one of {_METRIC_KINDS}"
            )
            return None
        required = {"name", "definition", "numerator_event", "max_tolerated_increase"}
        if kind == "proportion":
            required.add("denominator_event")
        missing = required - set(data)
        if missing:
            errors.append(
                f"metrics.guardrails[{index}] is missing: {', '.join(sorted(missing))}"
            )
            return None
        try:
            tolerance = float(data["max_tolerated_increase"])
        except (TypeError, ValueError):
            errors.append(
                f"metrics.guardrails[{index}].max_tolerated_increase must be a number. "
                "A guardrail without a numeric threshold cannot stop anything, which "
                "defeats its purpose."
            )
            return None
        if tolerance < 0:
            errors.append(
                f"metrics.guardrails[{index}].max_tolerated_increase must be "
                "non-negative; it describes how much worse the metric may get"
            )
            return None
        return cls(
            name=str(data["name"]),
            definition=str(data["definition"]),
            kind=kind,
            numerator_event=str(data["numerator_event"]),
            denominator_event=str(data.get("denominator_event", "")),
            max_tolerated_increase=tolerance,
        )


@dataclass(frozen=True)
class DecisionRule:
    """The promote / iterate / rollback rule, fixed before launch."""

    confidence: float = 0.95
    power: float = 0.80
    planned_looks: int = 1
    #: Require the confidence interval to clear the minimum effect of
    #: interest, not merely to exclude zero.
    require_practical_significance: bool = True

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, errors: list[str]) -> DecisionRule:
        data = data or {}
        confidence = float(data.get("confidence", 0.95))
        if not 0.5 < confidence < 1.0:
            errors.append(f"analysis.confidence must be in (0.5, 1.0), got {confidence}")
            confidence = 0.95
        power = float(data.get("power", 0.80))
        if not 0.5 <= power < 1.0:
            errors.append(f"analysis.power must be in [0.5, 1.0), got {power}")
            power = 0.80
        looks = int(data.get("planned_looks", 1))
        if looks < 1:
            errors.append("analysis.planned_looks must be at least 1")
            looks = 1
        return cls(
            confidence=confidence,
            power=power,
            planned_looks=looks,
            require_practical_significance=bool(
                data.get("require_practical_significance", True)
            ),
        )


@dataclass(frozen=True)
class ExperimentConfig:
    """A fully validated experiment definition."""

    experiment_id: str
    name: str
    description: str
    owner_team: str
    service_area: str
    salt: str
    variants: Mapping[str, float]
    control_variant: str
    enrolled_fraction: float
    eligibility: EligibilityCriteria
    primary_metric: MetricSpec
    guardrails: tuple[GuardrailSpec, ...]
    segments: tuple[str, ...]
    decision: DecisionRule
    privacy: PrivacyPolicy
    baseline_rate: float | None = None
    expected_units_per_group: int | None = None
    planned_duration_days: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def treatment_variants(self) -> tuple[str, ...]:
        return tuple(sorted(v for v in self.variants if v != self.control_variant))

    def power_note(self) -> str | None:
        """A plain-language note on whether this pilot can answer its question.

        This is the check that most often should stop a pilot before it
        starts, and the one teams skip. If the traffic available cannot
        detect the improvement the program cares about, running the pilot
        produces an inconclusive result at full operational cost.
        """
        if self.baseline_rate is None or self.expected_units_per_group is None:
            return None
        detectable = minimum_detectable_effect(
            self.baseline_rate,
            self.expected_units_per_group,
            alpha=1.0 - self.decision.confidence,
            power=self.decision.power,
            looks=self.decision.planned_looks,
        )
        wanted = self.primary_metric.minimum_effect_of_interest
        needed = required_sample_size_per_group(
            self.baseline_rate,
            wanted,
            alpha=1.0 - self.decision.confidence,
            power=self.decision.power,
            looks=self.decision.planned_looks,
        )
        if detectable <= wanted:
            return (
                f"Adequately powered: with {self.expected_units_per_group:,} units per "
                f"group this pilot can detect a change of about "
                f"{detectable * 100:.1f} percentage points, and the program cares about "
                f"{wanted * 100:.1f}."
            )
        return (
            f"Underpowered: with {self.expected_units_per_group:,} units per group the "
            f"smallest change this pilot can reliably detect is about "
            f"{detectable * 100:.1f} percentage points, but the program says it cares "
            f"about {wanted * 100:.1f}. Detecting that would need roughly "
            f"{needed:,} units per group. Consider extending the pilot, widening "
            f"eligibility, or testing a larger change."
        )


def _load_raw(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ConfigError(
                f"{path} is YAML, but PyYAML is not installed. Either install it "
                "(pip install 'civicexp[yaml]') or supply the same configuration as "
                "JSON, which this package reads with no third-party dependency.",
                code="CONFIG_NO_YAML",
            ) from exc
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path} is not valid JSON: {exc.msg} (line {exc.lineno})",
                code="CONFIG_UNPARSEABLE",
            ) from exc
    if not isinstance(data, Mapping):
        raise ConfigError(
            f"{path} must contain a mapping at the top level", code="CONFIG_SHAPE"
        )
    return data


def load_config(source: str | os.PathLike[str] | Mapping[str, Any]) -> ExperimentConfig:
    """Load and validate an experiment configuration.

    Raises :class:`~civicexp.errors.ConfigError` listing *every* problem found.
    """
    raw = _load_raw(Path(source)) if not isinstance(source, Mapping) else source

    errors: list[str] = []
    warnings: list[str] = []

    experiment = raw.get("experiment")
    if not isinstance(experiment, Mapping):
        raise ConfigError(
            "configuration must contain an 'experiment' section with at least "
            "an id and a name",
            code="CONFIG_SHAPE",
        )

    experiment_id = str(experiment.get("id", "")).strip()
    if not experiment_id:
        errors.append("experiment.id is required")

    name = str(experiment.get("name", "")).strip()
    if not name:
        errors.append("experiment.name is required")

    # -- assignment --------------------------------------------------------
    assignment = raw.get("assignment") or {}
    salt = str(assignment.get("salt", "")).strip()
    if not salt:
        errors.append(
            "assignment.salt is required. It must be set once before launch and "
            "never changed while the pilot runs, because changing it reshuffles "
            "every unit between arms."
        )
    elif len(salt) < 16:
        warnings.append(
            f"assignment.salt is only {len(salt)} characters. A longer, "
            "randomly generated salt makes pseudonyms harder to attack by guessing "
            "identifiers."
        )
    if "placeholder" in salt.lower() or salt.lower() in ("changeme", "salt", "test"):
        errors.append(
            f"assignment.salt is still a placeholder value ({salt!r}). Generate a "
            "real one, for example with: python -c \"import secrets; "
            'print(secrets.token_hex(16))"'
        )

    raw_variants = raw.get("variants")
    variants: dict[str, float] = {}
    control_variant = str(assignment.get("control_variant", "control"))
    if not isinstance(raw_variants, Mapping) or not raw_variants:
        errors.append("a 'variants' section with at least two arms is required")
    else:
        for vname, vdata in raw_variants.items():
            if isinstance(vdata, Mapping) and "traffic_share" in vdata:
                try:
                    variants[str(vname)] = float(vdata["traffic_share"])
                except (TypeError, ValueError):
                    errors.append(f"variants.{vname}.traffic_share must be a number")
            else:
                errors.append(f"variants.{vname} needs a numeric traffic_share")
        if variants:
            total = sum(variants.values())
            if abs(total - 1.0) > 1e-9:
                errors.append(
                    f"variant traffic_share values must sum to 1.0, got {total:.4f}"
                )
            if len(variants) < 2:
                errors.append("at least two variants are required to make a comparison")
            if control_variant not in variants:
                errors.append(
                    f"assignment.control_variant is {control_variant!r}, which is not "
                    f"one of the declared variants {sorted(variants)!r}"
                )

    enrolled_fraction = float(assignment.get("enrolled_fraction", 1.0))
    if not 0.0 < enrolled_fraction <= 1.0:
        errors.append("assignment.enrolled_fraction must be in (0, 1]")
        enrolled_fraction = 1.0

    # -- eligibility -------------------------------------------------------
    try:
        eligibility = EligibilityCriteria.from_dict(raw.get("eligibility"))
    except ConfigError as exc:
        errors.append(exc.message)
        eligibility = EligibilityCriteria()

    # -- metrics -----------------------------------------------------------
    metrics = raw.get("metrics") or {}
    primary = MetricSpec.from_dict(metrics.get("primary") or {}, errors)

    guardrails: list[GuardrailSpec] = []
    for index, gdata in enumerate(metrics.get("guardrails") or []):
        spec = GuardrailSpec.from_dict(gdata, index, errors)
        if spec is not None:
            guardrails.append(spec)
    if not guardrails:
        warnings.append(
            "No guardrail metrics are defined. Every change that helps one measure "
            "can hurt another; without guardrails this pilot cannot detect harm it "
            "is causing."
        )

    # -- equity segments ---------------------------------------------------
    # Pre-registering these is what separates an equity review from a fishing
    # expedition: searching for a subgroup after seeing the data will always
    # find one.
    segments: list[str] = []
    raw_segments = raw.get("segments") or []
    if not isinstance(raw_segments, (list, tuple)):
        errors.append("segments must be a list of attribute names")
    else:
        for item in raw_segments:
            if not isinstance(item, str):
                errors.append(f"segments entries must be strings, got {item!r}")
            else:
                segments.append(item)
    if not segments:
        warnings.append(
            "No segments are declared for equity review. An overall improvement "
            "can hide a group the change made worse; without declared segments "
            "this pilot cannot detect that. Consider preferred_language, channel, "
            "or device_type."
        )

    # -- analysis and decision --------------------------------------------
    analysis = raw.get("analysis") or {}
    decision = DecisionRule.from_dict(analysis, errors)

    # -- privacy -----------------------------------------------------------
    governance = raw.get("data_governance") or {}
    approved_pii = frozenset(str(f) for f in (governance.get("approved_pii_fields") or []))
    if approved_pii:
        warnings.append(
            f"{len(approved_pii)} field(s) are allowlisted as approved PII: "
            f"{sorted(approved_pii)}. Each one needs a documented justification in the "
            "approval record."
        )
    try:
        retention_days = int(governance.get("retention_days", 90))
    except (TypeError, ValueError):
        errors.append("data_governance.retention_days must be a whole number of days")
        retention_days = 90
    if retention_days > 365:
        warnings.append(
            f"data_governance.retention_days is {retention_days}. Retention beyond a "
            "year should be justified against the stated purpose of the pilot."
        )
    privacy = PrivacyPolicy(
        approved_pii_fields=approved_pii,
        suppression_threshold=int(
            governance.get("suppression_threshold", DEFAULT_SUPPRESSION_THRESHOLD)
        ),
        retention_days=retention_days,
        allow_row_level_export=bool(governance.get("allow_row_level_export", False)),
    )

    # Eligibility fields are read from live agency records, so they are
    # subject to the same screening as anything else the platform touches.
    try:
        privacy.check_field_names(eligibility.referenced_fields())
    except Exception as exc:  # PrivacyViolation
        errors.append(
            f"eligibility rules reference fields that look like personal "
            f"identifiers: {exc}"
        )

    # -- sizing ------------------------------------------------------------
    sizing = raw.get("sample_size") or {}

    def _optional_number(key: str, cast: Any) -> Any:
        value = sizing.get(key)
        if value is None or isinstance(value, str):
            return None
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    baseline_rate = _optional_number("baseline_rate", float)
    expected_units = _optional_number("expected_units_per_group", int)
    duration_days = _optional_number("planned_duration_days", int)
    if baseline_rate is not None and not 0.0 < baseline_rate < 1.0:
        errors.append("sample_size.baseline_rate must be a proportion between 0 and 1")
        baseline_rate = None
    if baseline_rate is None or expected_units is None:
        warnings.append(
            "sample_size.baseline_rate and sample_size.expected_units_per_group are "
            "not both set, so this configuration cannot be checked for statistical "
            "power before launch."
        )

    if errors:
        bullets = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(
            f"{len(errors)} problem(s) found in the experiment configuration:\n{bullets}",
            code="CONFIG_INVALID",
        )

    assert primary is not None  # guaranteed: a None primary appends an error above

    config = ExperimentConfig(
        experiment_id=experiment_id,
        name=name,
        description=str(experiment.get("description", "")),
        owner_team=str(experiment.get("owner_team", "")),
        service_area=str((raw.get("scope") or {}).get("service_area", "")),
        salt=salt,
        variants=dict(variants),
        control_variant=control_variant,
        enrolled_fraction=enrolled_fraction,
        eligibility=eligibility,
        primary_metric=primary,
        guardrails=tuple(guardrails),
        segments=tuple(segments),
        decision=decision,
        privacy=privacy,
        baseline_rate=baseline_rate,
        expected_units_per_group=expected_units,
        planned_duration_days=duration_days,
        warnings=tuple(warnings),
    )

    note = config.power_note()
    if note and note.startswith("Underpowered"):
        object.__setattr__(config, "warnings", config.warnings + (note,))
    return config

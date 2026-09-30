"""A small, safe predicate language for describing who is in an experiment.

Eligibility rules decide which people or transactions a public agency has
approved for inclusion in an evaluation. Getting this wrong is not a
statistical problem, it is a fairness problem, so the rule language here is
deliberately tiny and entirely declarative:

* no expression evaluation, no ``eval``, no lambdas, no imports from config;
* every operator is enumerated below and total over its inputs;
* rules serialize cleanly to YAML/JSON so that the exact inclusion criteria
  reviewed by program and legal staff are the ones the software runs.

Example
-------
>>> rule = Rule("channel", "in", ["web", "mobile"])
>>> rule.matches({"channel": "web"})
True
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError

__all__ = ["Rule", "EligibilityCriteria", "OPERATORS"]

OPERATORS: tuple[str, ...] = (
    "eq",
    "ne",
    "in",
    "not_in",
    "gt",
    "gte",
    "lt",
    "lte",
    "exists",
    "missing",
)


def _as_number(value: Any, field: str, op: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"operator {op!r} on field {field!r} needs a number, got {value!r}",
            code="CONFIG_RULE_TYPE",
        )
    return float(value)


@dataclass(frozen=True)
class Rule:
    """A single declarative condition on a unit's attributes."""

    field: str
    operator: str
    value: Any = None

    def __post_init__(self) -> None:
        if self.operator not in OPERATORS:
            raise ConfigError(
                f"unknown eligibility operator {self.operator!r}; "
                f"supported operators are {', '.join(OPERATORS)}",
                code="CONFIG_RULE_OPERATOR",
            )
        if self.operator in ("in", "not_in") and not isinstance(self.value, (list, tuple)):
            raise ConfigError(
                f"operator {self.operator!r} on field {self.field!r} needs a list value",
                code="CONFIG_RULE_TYPE",
            )

    def matches(self, attributes: Mapping[str, Any]) -> bool:
        """Evaluate this rule against a unit's attributes."""
        present = self.field in attributes
        if self.operator == "exists":
            return present
        if self.operator == "missing":
            return not present
        if not present:
            # A rule about an absent attribute is not satisfied. Absence is
            # never silently treated as a match: an agency that wants to
            # include records with missing data must say so with `missing`.
            return False

        actual = attributes[self.field]
        if self.operator == "eq":
            return bool(actual == self.value)
        if self.operator == "ne":
            return bool(actual != self.value)
        if self.operator == "in":
            return actual in self.value
        if self.operator == "not_in":
            return actual not in self.value

        left = _as_number(actual, self.field, self.operator)
        right = _as_number(self.value, self.field, self.operator)
        if self.operator == "gt":
            return left > right
        if self.operator == "gte":
            return left >= right
        if self.operator == "lt":
            return left < right
        return left <= right  # "lte"

    def describe(self) -> str:
        """A plain-language rendering for approval records and reports."""
        if self.operator == "exists":
            return f"{self.field} is recorded"
        if self.operator == "missing":
            return f"{self.field} is not recorded"
        if self.operator in ("in", "not_in"):
            joined = ", ".join(repr(v) for v in self.value)
            negation = "" if self.operator == "in" else "not "
            return f"{self.field} is {negation}one of [{joined}]"
        wording = {
            "eq": "is",
            "ne": "is not",
            "gt": "is greater than",
            "gte": "is at least",
            "lt": "is less than",
            "lte": "is at most",
        }[self.operator]
        return f"{self.field} {wording} {self.value!r}"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"field": self.field, "operator": self.operator}
        if self.operator not in ("exists", "missing"):
            out["value"] = self.value
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Rule:
        if not isinstance(data, Mapping):
            raise ConfigError(
                f"eligibility rule must be a mapping, got {type(data).__name__}",
                code="CONFIG_RULE_SHAPE",
            )
        missing = {"field", "operator"} - set(data)
        if missing:
            raise ConfigError(
                f"eligibility rule is missing required key(s): {', '.join(sorted(missing))}",
                code="CONFIG_RULE_SHAPE",
            )
        return cls(str(data["field"]), str(data["operator"]), data.get("value"))


@dataclass(frozen=True)
class EligibilityCriteria:
    """Include and exclude rule sets.

    A unit is eligible when it matches **every** include rule and **no**
    exclude rule. Exclusions are evaluated last and always win, so that a
    protective carve-out (for example, staff-assisted sessions, or applicants
    already flagged for manual review) cannot be overridden by a broad
    inclusion rule added later.
    """

    include: tuple[Rule, ...] = ()
    exclude: tuple[Rule, ...] = ()

    def is_eligible(self, attributes: Mapping[str, Any]) -> bool:
        if any(rule.matches(attributes) for rule in self.exclude):
            return False
        return all(rule.matches(attributes) for rule in self.include)

    def explain(self, attributes: Mapping[str, Any]) -> str:
        """Why a specific unit was or was not eligible.

        Operators need this to answer "why was this applicant not in the
        test?" without reading code or opening a database.
        """
        for rule in self.exclude:
            if rule.matches(attributes):
                return f"excluded because {rule.describe()}"
        unmet = [rule for rule in self.include if not rule.matches(attributes)]
        if unmet:
            return "not eligible because it is not the case that " + " and ".join(
                rule.describe() for rule in unmet
            )
        return "eligible"

    def referenced_fields(self) -> set[str]:
        return {r.field for r in self.include} | {r.field for r in self.exclude}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> EligibilityCriteria:
        if not data:
            return cls()
        return cls(
            include=tuple(Rule.from_dict(r) for r in data.get("include", []) or []),
            exclude=tuple(Rule.from_dict(r) for r in data.get("exclude", []) or []),
        )

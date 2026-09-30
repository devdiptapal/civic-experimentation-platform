"""Component 1: assigning eligible units to approved comparison groups.

The assignment function is deterministic and stateless. Given the same
experiment salt, unit identifier, and variant configuration, it always
returns the same variant -- on any machine, in any process, in any order,
with no database lookup and no coordination between servers.

That property is doing real work in a public-service setting:

* **A person's experience does not change mid-application.** Someone who
  starts a benefits application on Monday and returns on Thursday sees the
  same version, even if the agency's servers restarted in between.
* **Results can be audited after the fact.** An oversight reviewer can
  recompute every assignment from the config and the identifier list, and
  confirm that the analyzed groups are the groups the software actually
  created.
* **No assignment database is required.** There is no new store of
  per-person records to secure, retain, or breach.

The mechanism is standard hash-based bucketing: HMAC-SHA256 over the unit
identifier keyed by the experiment salt, the leading 8 bytes read as an
unsigned integer, scaled to ``[0, 1)``, then compared against the cumulative
traffic shares.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .eligibility import EligibilityCriteria
from .errors import AssignmentError
from .privacy import pseudonymize

__all__ = ["Assignment", "NOT_ELIGIBLE", "NOT_ENROLLED", "Assigner", "bucket_of"]

#: Returned when a unit does not meet the approved eligibility criteria.
NOT_ELIGIBLE = "not_eligible"
#: Returned when a unit is eligible but falls outside the enrolled fraction.
#:
#: Agencies routinely ramp an evaluation to a slice of traffic before
#: widening it. Units held back this way are neither control nor treatment;
#: they receive the unchanged service and are excluded from analysis.
NOT_ENROLLED = "not_enrolled"

_RESERVED = frozenset({NOT_ELIGIBLE, NOT_ENROLLED})


def bucket_of(unit_id: str, salt: str, *, purpose: str = "assign") -> float:
    """Map a unit identifier to a stable, uniform value in ``[0, 1)``.

    ``purpose`` separates independent random draws made from the same
    identifier and salt. Assignment and enrollment use different purposes so
    that the decision "is this unit in the evaluation at all?" is statistically
    independent of "which arm does it land in?" -- otherwise ramping the
    enrolled fraction up or down would systematically reshuffle the arms.
    """
    if not unit_id:
        raise AssignmentError("unit_id must be a non-empty string", code="ASSIGNMENT_NO_UNIT")
    if not salt:
        raise AssignmentError(
            "experiment salt must be set before assignment", code="ASSIGNMENT_NO_SALT"
        )
    digest = hmac.new(
        salt.encode("utf-8"), f"{purpose}:{unit_id}".encode(), hashlib.sha256
    ).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64


@dataclass(frozen=True)
class Assignment:
    """The outcome of an assignment decision, with its justification."""

    unit_pseudonym: str
    variant: str
    bucket: float
    eligible: bool
    enrolled: bool
    reason: str

    @property
    def in_analysis(self) -> bool:
        """Whether this unit contributes to the comparison."""
        return self.eligible and self.enrolled

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_pseudonym": self.unit_pseudonym,
            "variant": self.variant,
            "bucket": round(self.bucket, 12),
            "eligible": self.eligible,
            "enrolled": self.enrolled,
            "reason": self.reason,
        }


class Assigner:
    """Assigns eligible units to variants according to an approved config.

    Parameters
    ----------
    salt:
        Per-experiment secret. Changing it re-randomizes every assignment,
        so it must be fixed before launch and never rotated mid-pilot.
    variants:
        Mapping of variant name to traffic share. Shares must sum to 1.0.
    eligibility:
        Approved inclusion and exclusion criteria.
    enrolled_fraction:
        Share of *eligible* traffic actually entered into the evaluation.
    """

    def __init__(
        self,
        *,
        salt: str,
        variants: Mapping[str, float],
        eligibility: EligibilityCriteria | None = None,
        enrolled_fraction: float = 1.0,
    ) -> None:
        if not salt:
            # Fail here rather than at the first assign() call: a misconfigured
            # experiment should be rejected when it is built, not part-way
            # through a live traffic stream.
            raise AssignmentError(
                "a per-experiment salt is required to construct an Assigner",
                code="ASSIGNMENT_NO_SALT",
            )
        if not variants:
            raise AssignmentError(
                "at least one variant is required", code="ASSIGNMENT_NO_VARIANTS"
            )
        if len(variants) < 2:
            raise AssignmentError(
                "an experiment needs at least two variants to compare; "
                f"got only {list(variants)!r}",
                code="ASSIGNMENT_NO_COMPARISON",
            )
        reserved = _RESERVED & set(variants)
        if reserved:
            raise AssignmentError(
                f"variant name(s) {sorted(reserved)!r} are reserved by the platform",
                code="ASSIGNMENT_RESERVED_NAME",
            )
        for name, share in variants.items():
            if not 0.0 <= share <= 1.0:
                raise AssignmentError(
                    f"traffic share for {name!r} must be between 0 and 1, got {share!r}",
                    code="ASSIGNMENT_BAD_SHARE",
                )
        total = sum(variants.values())
        if abs(total - 1.0) > 1e-9:
            raise AssignmentError(
                f"variant traffic shares must sum to 1.0, got {total:.6f}. "
                "Every eligible, enrolled unit must land in exactly one arm.",
                code="ASSIGNMENT_SHARES_SUM",
            )
        if not 0.0 < enrolled_fraction <= 1.0:
            raise AssignmentError(
                f"enrolled_fraction must be in (0, 1], got {enrolled_fraction!r}",
                code="ASSIGNMENT_BAD_FRACTION",
            )

        self.salt = salt
        # Sorted for a stable bucket layout independent of dict insertion
        # order, so that assignments are reproducible across config rewrites
        # that only reorder keys.
        self.variants: tuple[tuple[str, float], ...] = tuple(sorted(variants.items()))
        self.eligibility = eligibility or EligibilityCriteria()
        self.enrolled_fraction = enrolled_fraction

    def assign(self, unit_id: str, attributes: Mapping[str, Any] | None = None) -> Assignment:
        """Decide the variant for one unit, with a reason for the decision."""
        attrs = dict(attributes or {})
        pseudonym = pseudonymize(unit_id, self.salt)

        if not self.eligibility.is_eligible(attrs):
            return Assignment(
                unit_pseudonym=pseudonym,
                variant=NOT_ELIGIBLE,
                bucket=float("nan"),
                eligible=False,
                enrolled=False,
                reason=self.eligibility.explain(attrs),
            )

        if self.enrolled_fraction < 1.0:
            enrollment_bucket = bucket_of(unit_id, self.salt, purpose="enroll")
            if enrollment_bucket >= self.enrolled_fraction:
                return Assignment(
                    unit_pseudonym=pseudonym,
                    variant=NOT_ENROLLED,
                    bucket=enrollment_bucket,
                    eligible=True,
                    enrolled=False,
                    reason=(
                        "eligible but held back: the evaluation is ramped to "
                        f"{self.enrolled_fraction:.0%} of eligible traffic"
                    ),
                )

        bucket = bucket_of(unit_id, self.salt, purpose="assign")
        cumulative = 0.0
        for name, share in self.variants:
            cumulative += share
            if bucket < cumulative:
                return Assignment(
                    unit_pseudonym=pseudonym,
                    variant=name,
                    bucket=bucket,
                    eligible=True,
                    enrolled=True,
                    reason=f"assigned to {name!r} (bucket {bucket:.6f})",
                )

        # Floating-point cumulative sums can fall a few ULPs short of 1.0.
        # The last variant absorbs that remainder rather than the caller
        # receiving an error for a unit that is genuinely eligible.
        name = self.variants[-1][0]
        return Assignment(
            unit_pseudonym=pseudonym,
            variant=name,
            bucket=bucket,
            eligible=True,
            enrolled=True,
            reason=f"assigned to {name!r} (bucket {bucket:.6f}, rounding remainder)",
        )

    def assign_many(
        self, units: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> list[Assignment]:
        """Assign a batch of ``(unit_id, attributes)`` pairs."""
        return [self.assign(unit_id, attrs) for unit_id, attrs in units]

    def balance_report(
        self,
        unit_ids: Sequence[str],
        attributes: Mapping[str, Any] | None = None,
    ) -> dict[str, int]:
        """Counts per variant for a set of identifiers.

        Useful as a pre-launch dry run: an agency can confirm the split looks
        right on last month's identifiers before any live traffic is touched.

        ``attributes`` is applied to every identifier. Supply a set that
        satisfies the eligibility criteria to inspect the traffic split
        itself; omit it to confirm that the criteria exclude units whose
        attributes are unknown, which they should.
        """
        counts: dict[str, int] = {name: 0 for name, _ in self.variants}
        counts[NOT_ENROLLED] = 0
        counts[NOT_ELIGIBLE] = 0
        attrs = dict(attributes or {})
        for unit_id in unit_ids:
            result = self.assign(unit_id, attrs)
            counts[result.variant] = counts.get(result.variant, 0) + 1
        return counts

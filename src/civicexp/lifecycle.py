"""Component 5: administrative controls to approve, pause, or end an evaluation.

The signed endeavor plan describes "administrative controls that allow an
agency to approve, pause, or end an evaluation". This module implements those
controls as an explicit state machine with three properties that matter for
public-sector use:

**Nothing runs without recorded approval.** The transition into ``RUNNING``
is only available from ``APPROVED``, and approval requires the named set of
reviewer roles from ``docs/Experiment-Approval-Checklist.md`` to have signed
off individually. A missing privacy sign-off is a blocked launch, not a
warning in a log.

**Pausing is always available and never destructive.** A ``RUNNING``
experiment can be paused by any authorized role at any time without
coordination, and resumed later. Assignments already made are unaffected,
because assignment is deterministic and stateless.

**Ending is terminal and typed.** An evaluation ends as ``COMPLETED`` or
``ROLLED_BACK``, and neither can be reopened. A rollback additionally
requires a stated reason, which lands in the audit trail.

Every transition is written to a hash-chained :class:`~civicexp.audit.AuditLog`.

**The audit trail is the state.** :meth:`ExperimentLifecycle.from_audit`
rebuilds an evaluation's state, sign-offs, and interim looks by replaying
its trail, through the same code path a live transition uses. There is no
second copy of "what state is this in" that could disagree with the
record, and a trail that fails verification is refused rather than
loaded.

**Approval is bound to a specific plan.** When a sign-off carries the
configuration's fingerprint (:func:`civicexp.config.config_fingerprint`),
approval checks that every reviewer signed off on the plan as it now
stands. A plan edited after the privacy reviewer read it needs the privacy
reviewer to read it again.

State diagram
-------------
::

    DRAFT ──approve──> APPROVED ──start──> RUNNING ⇄ PAUSED
      │                    │                 │         │
      │                    └──archive──┐     │         │
      └──archive─────────────────────┐ │     │         │
                                     v v     v         v
                                  ARCHIVED   COMPLETED / ROLLED_BACK
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .audit import AuditEntry, AuditLog
from .errors import LifecycleError

__all__ = ["State", "REQUIRED_APPROVALS", "ExperimentLifecycle"]


class State(str, Enum):
    """Administrative state of an evaluation."""

    DRAFT = "draft"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    ARCHIVED = "archived"

    @property
    def is_terminal(self) -> bool:
        return self in (State.COMPLETED, State.ROLLED_BACK, State.ARCHIVED)

    @property
    def is_collecting(self) -> bool:
        """Whether assignment and outcome collection should be active."""
        return self is State.RUNNING


#: Reviewer roles that must sign off before an evaluation may launch.
#: Mirrors the approval record in ``docs/Experiment-Approval-Checklist.md``.
REQUIRED_APPROVALS: tuple[str, ...] = (
    "program_owner",
    "operations_lead",
    "privacy_reviewer",
    "legal_policy_reviewer",
    "evaluation_lead",
)

_TRANSITIONS: dict[State, frozenset[State]] = {
    State.DRAFT: frozenset({State.APPROVED, State.ARCHIVED}),
    State.APPROVED: frozenset({State.RUNNING, State.DRAFT, State.ARCHIVED}),
    State.RUNNING: frozenset({State.PAUSED, State.COMPLETED, State.ROLLED_BACK}),
    State.PAUSED: frozenset({State.RUNNING, State.COMPLETED, State.ROLLED_BACK}),
    State.COMPLETED: frozenset({State.ARCHIVED}),
    State.ROLLED_BACK: frozenset({State.ARCHIVED}),
    State.ARCHIVED: frozenset(),
}


#: Audit action prefixes that only the lifecycle itself may write. A
#: free-form annotation using one of these could be replayed as a
#: transition or approval that never happened.
_RESERVED_PREFIXES = ("state:", "approval:", "analysis:")


@dataclass(frozen=True)
class SignOff:
    """One reviewer's recorded approval."""

    role: str
    actor: str
    timestamp: str
    note: str = ""
    #: Fingerprint of the plan this reviewer approved, when recorded.
    config_digest: str = ""


class ExperimentLifecycle:
    """Administrative state and approval record for one evaluation."""

    def __init__(
        self,
        experiment_id: str,
        *,
        audit_path: str | os.PathLike[str] | None = None,
        required_approvals: Iterable[str] | None = None,
        state: State = State.DRAFT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.experiment_id = experiment_id
        self.audit = AuditLog(audit_path, clock=clock)
        self.required_approvals = tuple(
            required_approvals if required_approvals is not None else REQUIRED_APPROVALS
        )
        self._state = state
        self._sign_offs: dict[str, SignOff] = {}
        self._approved_digest: str | None = None
        self._interim_looks = 0
        self._entered_at: dict[State, str] = {}

    @classmethod
    def from_audit(
        cls,
        experiment_id: str,
        audit_path: str | os.PathLike[str],
        *,
        required_approvals: Iterable[str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> ExperimentLifecycle:
        """Rebuild an evaluation's administrative state from its audit trail.

        Raises :class:`~civicexp.errors.LifecycleError` if the trail fails
        verification, or records something the state machine would not have
        allowed. A governance record that cannot be trusted must not be the
        basis for collecting data from the public.
        """
        lifecycle = cls(
            experiment_id,
            audit_path=audit_path,
            required_approvals=required_approvals,
            clock=clock,
        )
        verification = lifecycle.audit.verify()
        if not verification.valid:
            raise LifecycleError(
                f"the audit trail for {experiment_id!r} failed verification: "
                f"{verification.message}. The state it records cannot be trusted, "
                "so it was not loaded.",
                code="LIFECYCLE_AUDIT_INVALID",
            )
        for entry in lifecycle.audit:
            lifecycle._apply(entry, replaying=True)
        return lifecycle

    def _apply(self, entry: AuditEntry, *, replaying: bool = False) -> None:
        """Fold one audit entry into the in-memory state.

        Live transitions call this after appending, and replay calls it for
        each stored entry, so the two cannot drift apart.
        """
        recorded_for = entry.detail.get("experiment_id")
        if recorded_for is not None and recorded_for != self.experiment_id:
            raise LifecycleError(
                f"audit entry {entry.sequence} belongs to experiment "
                f"{recorded_for!r}, not {self.experiment_id!r}",
                code="LIFECYCLE_AUDIT_INVALID",
            )
        if entry.action == "approval:sign_off":
            role = str(entry.detail.get("role", ""))
            self._sign_offs[role] = SignOff(
                role=role,
                actor=entry.actor,
                timestamp=entry.timestamp,
                note=str(entry.detail.get("note", "")),
                config_digest=str(entry.detail.get("config_digest", "")),
            )
        elif entry.action.startswith("state:"):
            source, _, target = entry.action[len("state:"):].partition("->")
            try:
                source_state, target_state = State(source), State(target)
            except ValueError as exc:
                raise LifecycleError(
                    f"audit entry {entry.sequence} records an unknown state "
                    f"({entry.action!r})",
                    code="LIFECYCLE_AUDIT_INVALID",
                ) from exc
            if replaying and (
                source_state is not self._state
                or target_state not in _TRANSITIONS[source_state]
            ):
                raise LifecycleError(
                    f"audit entry {entry.sequence} records {entry.action!r}, but the "
                    f"trail up to that point leaves the experiment in "
                    f"{self._state.value!r}. The trail is internally inconsistent.",
                    code="LIFECYCLE_AUDIT_INVALID",
                )
            self._state = target_state
            self._entered_at.setdefault(target_state, entry.timestamp)
            if target_state is State.APPROVED:
                self._approved_digest = entry.detail.get("config_digest") or None
            elif target_state is State.DRAFT:
                self._approved_digest = None
        elif entry.action == "analysis:interim_look":
            self._interim_looks += 1

    # -- inspection --------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def sign_offs(self) -> Mapping[str, SignOff]:
        return dict(self._sign_offs)

    @property
    def missing_approvals(self) -> tuple[str, ...]:
        return tuple(r for r in self.required_approvals if r not in self._sign_offs)

    @property
    def is_collecting(self) -> bool:
        """Whether the platform should be assigning units and recording outcomes."""
        return self._state.is_collecting

    @property
    def approved_digest(self) -> str | None:
        """Plan fingerprint recorded at approval, if one was recorded."""
        return self._approved_digest

    @property
    def interim_looks(self) -> int:
        """How many recorded interim looks at the results have been taken."""
        return self._interim_looks

    def entered_at(self, state: State) -> str | None:
        """ISO timestamp of the first time the evaluation entered ``state``."""
        return self._entered_at.get(state)

    @property
    def started_at(self) -> str | None:
        return self.entered_at(State.RUNNING)

    @property
    def ended_at(self) -> str | None:
        return self.entered_at(State.COMPLETED) or self.entered_at(State.ROLLED_BACK)

    def can_transition_to(self, target: State) -> bool:
        return target in _TRANSITIONS[self._state]

    # -- transitions -------------------------------------------------------

    def _transition(self, target: State, actor: str, **detail: Any) -> None:
        if not self.can_transition_to(target):
            allowed = sorted(s.value for s in _TRANSITIONS[self._state])
            raise LifecycleError(
                f"cannot move experiment {self.experiment_id!r} from "
                f"{self._state.value!r} to {target.value!r}; "
                f"allowed next states are {allowed or ['(none, terminal)']}",
                code="LIFECYCLE_FORBIDDEN",
            )
        entry = self.audit.append(
            f"state:{self._state.value}->{target.value}",
            actor,
            experiment_id=self.experiment_id,
            **detail,
        )
        self._apply(entry)

    def sign_off(
        self,
        role: str,
        actor: str,
        note: str = "",
        *,
        config_digest: str | None = None,
    ) -> SignOff:
        """Record one reviewer's approval.

        Pass ``config_digest`` to bind the sign-off to the exact plan the
        reviewer read; :meth:`approve` can then refuse a plan that changed
        afterwards.
        """
        if self._state is not State.DRAFT:
            raise LifecycleError(
                f"sign-offs are recorded while an experiment is in 'draft'; "
                f"{self.experiment_id!r} is in {self._state.value!r}. Reopen it to "
                "draft to change the approval record.",
                code="LIFECYCLE_SIGNOFF_LATE",
            )
        if role not in self.required_approvals:
            raise LifecycleError(
                f"{role!r} is not a required reviewer role for this experiment; "
                f"expected one of {list(self.required_approvals)!r}",
                code="LIFECYCLE_UNKNOWN_ROLE",
            )
        extra = {"config_digest": config_digest} if config_digest else {}
        entry = self.audit.append(
            "approval:sign_off",
            actor,
            experiment_id=self.experiment_id,
            role=role,
            note=note,
            **extra,
        )
        self._apply(entry)
        return self._sign_offs[role]

    def approve(self, actor: str, *, config_digest: str | None = None) -> None:
        """Move a fully signed-off draft to ``APPROVED``.

        With ``config_digest``, every required sign-off must have been made
        against that same plan.
        """
        missing = self.missing_approvals
        if missing:
            raise LifecycleError(
                f"cannot approve {self.experiment_id!r}: missing sign-off from "
                f"{', '.join(missing)}. See docs/Experiment-Approval-Checklist.md.",
                code="LIFECYCLE_MISSING_APPROVAL",
            )
        extra: dict[str, str] = {}
        if config_digest:
            stale = sorted(
                role
                for role in self.required_approvals
                if self._sign_offs[role].config_digest != config_digest
            )
            if stale:
                raise LifecycleError(
                    f"cannot approve {self.experiment_id!r}: the plan changed after "
                    f"{', '.join(stale)} signed off. Each of them needs to review the "
                    "current version and sign off again.",
                    code="LIFECYCLE_STALE_SIGNOFF",
                )
            extra["config_digest"] = config_digest
        self._transition(
            State.APPROVED,
            actor,
            approvals=",".join(sorted(self._sign_offs)),
            **extra,
        )

    def reopen(self, actor: str, reason: str) -> None:
        """Return an approved (not yet started) experiment to draft."""
        self._transition(State.DRAFT, actor, reason=reason)

    def start(self, actor: str) -> None:
        """Begin collecting assignments and outcomes."""
        self._transition(State.RUNNING, actor)

    def pause(self, actor: str, reason: str) -> None:
        """Halt collection without ending the evaluation."""
        if not reason:
            raise LifecycleError(
                "pausing requires a reason so that monitoring staff and later "
                "reviewers can see why collection stopped",
                code="LIFECYCLE_NO_REASON",
            )
        self._transition(State.PAUSED, actor, reason=reason)

    def resume(self, actor: str) -> None:
        """Resume collection after a pause."""
        self._transition(State.RUNNING, actor)

    def complete(self, actor: str, summary: str = "", **detail: Any) -> None:
        """End the evaluation normally.

        Extra keyword arguments are recorded with the transition, which is
        how the decision and the result it rested on enter the record.
        """
        self._transition(State.COMPLETED, actor, summary=summary, **detail)

    def roll_back(self, actor: str, reason: str, **detail: Any) -> None:
        """End the evaluation and revert the tested change."""
        if not reason:
            raise LifecycleError(
                "rollback requires a stated reason for the audit record",
                code="LIFECYCLE_NO_REASON",
            )
        self._transition(State.ROLLED_BACK, actor, reason=reason, **detail)

    def record_look(self, actor: str, **detail: Any) -> int:
        """Record an interim look at the results, returning its number.

        Every look at a running pilot's results is another chance to see a
        false positive, which is why the plan declares how many there will
        be and the significance threshold is tightened to match. Recording
        each one is what lets the final readout say whether the plan was
        kept.
        """
        if self._state not in (State.RUNNING, State.PAUSED):
            raise LifecycleError(
                f"interim looks are taken while an evaluation is running or paused; "
                f"{self.experiment_id!r} is {self._state.value!r}",
                code="LIFECYCLE_FORBIDDEN",
            )
        entry = self.audit.append(
            "analysis:interim_look",
            actor,
            experiment_id=self.experiment_id,
            look=self._interim_looks + 1,
            **detail,
        )
        self._apply(entry)
        return self._interim_looks

    def record(self, action: str, actor: str, **detail: Any) -> AuditEntry:
        """Add an entry to the record that is not a state change.

        For events worth keeping in the same tamper-evident trail -- a plan
        revised, a readout frozen, data deleted under the retention policy.
        Action names that the lifecycle replays are refused, so an
        annotation can never be mistaken for a transition.
        """
        if action.startswith(_RESERVED_PREFIXES):
            raise LifecycleError(
                f"{action!r} uses a prefix reserved for lifecycle transitions",
                code="LIFECYCLE_RESERVED_ACTION",
            )
        return self.audit.append(
            action, actor, experiment_id=self.experiment_id, **detail
        )

    def archive(self, actor: str) -> None:
        """Close the record permanently."""
        self._transition(State.ARCHIVED, actor, audit_head=self.audit.head)

    # -- guards ------------------------------------------------------------

    def require_collecting(self) -> None:
        """Raise unless the evaluation is authorized to collect right now.

        Call this before assigning a unit or recording an outcome. It is the
        single enforcement point that makes "pause" mean something: a paused
        evaluation stops taking data everywhere, without each call site
        needing to remember to check.
        """
        if self._state.is_collecting:
            return
        guidance = {
            State.DRAFT: "it has not been approved yet",
            State.APPROVED: "it has been approved but not started",
            State.PAUSED: "it is paused",
            State.COMPLETED: "it has been completed",
            State.ROLLED_BACK: "it was rolled back",
            State.ARCHIVED: "it has been archived",
        }[self._state]
        raise LifecycleError(
            f"experiment {self.experiment_id!r} is not collecting data because "
            f"{guidance}",
            code="LIFECYCLE_NOT_COLLECTING",
        )

    def status_summary(self) -> dict[str, Any]:
        verification = self.audit.verify()
        return {
            "experiment_id": self.experiment_id,
            "state": self._state.value,
            "collecting": self.is_collecting,
            "approvals_recorded": sorted(self._sign_offs),
            "approvals_missing": list(self.missing_approvals),
            "interim_looks": self._interim_looks,
            "approved_config_digest": self._approved_digest,
            "audit_entries": len(self.audit),
            "audit_chain_valid": verification.valid,
            "audit_head": self.audit.head,
        }

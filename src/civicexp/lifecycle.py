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

from .audit import AuditLog
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


@dataclass(frozen=True)
class SignOff:
    """One reviewer's recorded approval."""

    role: str
    actor: str
    timestamp: str
    note: str = ""


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
        previous = self._state
        self._state = target
        self.audit.append(
            f"state:{previous.value}->{target.value}",
            actor,
            experiment_id=self.experiment_id,
            **detail,
        )

    def sign_off(self, role: str, actor: str, note: str = "") -> SignOff:
        """Record one reviewer's approval."""
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
        entry = self.audit.append(
            "approval:sign_off",
            actor,
            experiment_id=self.experiment_id,
            role=role,
            note=note,
        )
        record = SignOff(role=role, actor=actor, timestamp=entry.timestamp, note=note)
        self._sign_offs[role] = record
        return record

    def approve(self, actor: str) -> None:
        """Move a fully signed-off draft to ``APPROVED``."""
        missing = self.missing_approvals
        if missing:
            raise LifecycleError(
                f"cannot approve {self.experiment_id!r}: missing sign-off from "
                f"{', '.join(missing)}. See docs/Experiment-Approval-Checklist.md.",
                code="LIFECYCLE_MISSING_APPROVAL",
            )
        self._transition(
            State.APPROVED,
            actor,
            approvals=",".join(sorted(self._sign_offs)),
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

    def complete(self, actor: str, summary: str = "") -> None:
        """End the evaluation normally."""
        self._transition(State.COMPLETED, actor, summary=summary)

    def roll_back(self, actor: str, reason: str) -> None:
        """End the evaluation and revert the tested change."""
        if not reason:
            raise LifecycleError(
                "rollback requires a stated reason for the audit record",
                code="LIFECYCLE_NO_REASON",
            )
        self._transition(State.ROLLED_BACK, actor, reason=reason)

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
            "audit_entries": len(self.audit),
            "audit_chain_valid": verification.valid,
            "audit_head": self.audit.head,
        }

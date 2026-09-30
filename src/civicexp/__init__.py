"""Civic Experimentation Platform: a reference implementation.

An open-source toolkit that lets a public agency test whether a change to a
digital service actually improves outcomes, with governance, privacy, and
plain-language reporting built in rather than bolted on.

The package implements the five components an agency needs to run one
controlled evaluation end to end:

===========================================  ============================
Component                                    Module
===========================================  ============================
1. Assign eligible units to groups           :mod:`civicexp.assignment`
2. Record agreed-upon outcomes               :mod:`civicexp.events`
3. Compare the results                       :mod:`civicexp.analysis`
4. Explain results in understandable terms   :mod:`civicexp.report`
5. Approve, pause, or end an evaluation      :mod:`civicexp.lifecycle`
===========================================  ============================

Supporting modules: :mod:`civicexp.config` (the approved artifact that
drives everything), :mod:`civicexp.privacy` (controls enforced in code),
:mod:`civicexp.audit` (tamper-evident decision trail),
:mod:`civicexp.stats` (dependency-free statistical functions), and
:mod:`civicexp.simulate` (synthetic data for dry runs).

Quick start
-----------
>>> from civicexp import Assigner
>>> assigner = Assigner(
...     salt="example-salt-value",
...     variants={"control": 0.5, "treatment": 0.5},
... )
>>> assigner.assign("applicant-1001").variant == assigner.assign("applicant-1001").variant
True

Scope
-----
This is a reference implementation intended for pilots and adaptation. It is
not a production benefits system, it makes no eligibility determinations, and
it does not replace an agency's case-management software.
"""

from __future__ import annotations

__version__ = "0.2.0"

from .analysis import AnalysisResult, Decision, analyze
from .assignment import NOT_ELIGIBLE, NOT_ENROLLED, Assigner, Assignment
from .audit import AuditLog
from .config import ExperimentConfig, load_config
from .eligibility import EligibilityCriteria, Rule
from .errors import (
    AnalysisError,
    AssignmentError,
    CivicExpError,
    ConfigError,
    EventError,
    LifecycleError,
    PrivacyViolation,
)
from .events import EVENT_TYPES, Event, EventLog
from .lifecycle import REQUIRED_APPROVALS, ExperimentLifecycle, State
from .privacy import PrivacyPolicy, pseudonymize
from .report import render_markdown, render_text_summary

__all__ = [
    "__version__",
    # Component 1: assignment
    "Assigner",
    "Assignment",
    "NOT_ELIGIBLE",
    "NOT_ENROLLED",
    "EligibilityCriteria",
    "Rule",
    # Component 2: outcome recording
    "Event",
    "EventLog",
    "EVENT_TYPES",
    # Component 3: comparison
    "analyze",
    "AnalysisResult",
    "Decision",
    # Component 4: reporting
    "render_markdown",
    "render_text_summary",
    # Component 5: administrative controls
    "ExperimentLifecycle",
    "State",
    "REQUIRED_APPROVALS",
    "AuditLog",
    # Supporting
    "ExperimentConfig",
    "load_config",
    "PrivacyPolicy",
    "pseudonymize",
    # Errors
    "CivicExpError",
    "ConfigError",
    "PrivacyViolation",
    "LifecycleError",
    "AssignmentError",
    "EventError",
    "AnalysisError",
]

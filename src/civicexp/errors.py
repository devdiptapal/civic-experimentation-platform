"""Exception hierarchy for the civic experimentation platform.

Errors carry a stable machine-readable ``code`` so that agency operators,
logs, and CI output can be triaged without parsing English prose.
"""

from __future__ import annotations


class CivicExpError(Exception):
    """Base class for every error raised by this package."""

    code = "CIVICEXP_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class ConfigError(CivicExpError):
    """The experiment configuration is malformed or internally inconsistent."""

    code = "CONFIG_INVALID"


class PrivacyViolation(CivicExpError):
    """An operation would record or publish data the privacy rules forbid."""

    code = "PRIVACY_VIOLATION"


class LifecycleError(CivicExpError):
    """An administrative state transition is not permitted."""

    code = "LIFECYCLE_FORBIDDEN"


class AssignmentError(CivicExpError):
    """A unit could not be assigned to a variant."""

    code = "ASSIGNMENT_FAILED"


class EventError(CivicExpError):
    """An outcome event failed schema or data-quality validation."""

    code = "EVENT_INVALID"


class AnalysisError(CivicExpError):
    """An analysis was requested that the available data cannot support."""

    code = "ANALYSIS_UNSUPPORTED"

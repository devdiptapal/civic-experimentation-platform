"""Privacy controls enforced in code, not only in policy documents.

``PRIVACY.md`` in this repository commits to data minimization, no PII by
default, aggregation-first reporting, and bounded retention. This module is
where those commitments are made mechanical, so that a pilot cannot quietly
drift away from the reviewed posture between the approval meeting and the
production run.

Three controls are implemented:

* **Pseudonymization.** Unit identifiers are hashed with a per-experiment
  salt before anything is written to disk.
* **A field denylist.** Attribute and event payload keys that look like
  direct identifiers are rejected at ingestion unless the field was named in
  the experiment's approved PII allowlist.
* **Small-cell suppression.** Aggregates computed over fewer than a
  configured number of units are withheld from reports.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import PrivacyViolation

__all__ = [
    "DEFAULT_SUPPRESSION_THRESHOLD",
    "PII_FIELD_PATTERNS",
    "PrivacyPolicy",
    "pseudonymize",
]

#: Default minimum cell size before an aggregate may be published.
#:
#: Eleven is the convention used by several federal health and human-services
#: data programs. It is a default, not a legal standard: agencies should set
#: this to whatever their own disclosure-review policy requires.
DEFAULT_SUPPRESSION_THRESHOLD = 11

#: Key-name patterns treated as direct or near-direct identifiers.
#:
#: This is a safety net against accidental capture, not a classifier. It
#: matches on field *names*, so it cannot catch an identifier hidden inside a
#: free-text field -- which is why free text is rejected outright below.
PII_FIELD_PATTERNS: tuple[str, ...] = (
    r"ssn",
    r"social_security",
    r"\bdob\b",
    r"date_of_birth",
    r"birth_date",
    r"first_name",
    r"last_name",
    r"full_name",
    r"^name$",
    r"email",
    r"phone",
    r"mobile",
    r"street",
    r"address",
    r"zip_?code",
    r"postal",
    r"case_number",
    r"case_id",
    r"client_id",
    r"applicant_id",
    r"member_id",
    r"medicaid_id",
    r"ebt",
    r"account_number",
    r"routing",
    r"license",
    r"passport",
    r"alien_number",
    r"\ba_?number\b",
    r"ip_address",
    r"device_id",
    r"latitude",
    r"longitude",
    r"geo_?point",
    r"free_?text",
    r"comment",
    r"note",
)

_COMPILED_PII = tuple(re.compile(p, re.IGNORECASE) for p in PII_FIELD_PATTERNS)


def pseudonymize(unit_id: str, salt: str) -> str:
    """Return a stable, salted pseudonym for a unit identifier.

    HMAC-SHA256 is used rather than a bare hash so that the salt acts as a
    key: without it, an attacker holding the published pseudonyms cannot
    confirm a guessed identifier by recomputing the digest. Identifier spaces
    in benefits systems are small and highly guessable (sequential case
    numbers, for example), so this distinction matters in practice.

    The pseudonym is stable for a given ``(unit_id, salt)`` pair, which is
    what makes assignment reproducible, and differs across experiments
    because each experiment carries its own salt.
    """
    if not unit_id:
        raise PrivacyViolation("unit_id must be a non-empty string", code="PRIVACY_EMPTY_ID")
    if not salt:
        raise PrivacyViolation(
            "a per-experiment salt is required to pseudonymize identifiers",
            code="PRIVACY_NO_SALT",
        )
    digest = hmac.new(salt.encode("utf-8"), unit_id.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


@dataclass(frozen=True)
class PrivacyPolicy:
    """The privacy posture a single experiment was approved to operate under."""

    #: Field names explicitly approved for collection despite matching the
    #: denylist. Every entry here should correspond to a documented
    #: justification in the experiment's approval record.
    approved_pii_fields: frozenset[str] = frozenset()
    #: Minimum cell size for published aggregates.
    suppression_threshold: int = DEFAULT_SUPPRESSION_THRESHOLD
    #: Days after which pilot data should be deleted or archived.
    retention_days: int = 90
    #: Whether row-level export is permitted at all.
    allow_row_level_export: bool = False

    def check_field_names(self, fields: Iterable[str]) -> None:
        """Raise if any field name looks like an unapproved identifier."""
        offending = []
        for name in fields:
            if name in self.approved_pii_fields:
                continue
            for pattern in _COMPILED_PII:
                if pattern.search(name):
                    offending.append((name, pattern.pattern))
                    break
        if offending:
            detail = ", ".join(f"{n!r} (matched /{p}/)" for n, p in offending)
            raise PrivacyViolation(
                "Field names look like personal identifiers and were not in the "
                f"approved PII allowlist: {detail}. Either remove these fields or add "
                "them to data_governance.approved_pii_fields with a documented "
                "justification in the approval record.",
                code="PRIVACY_UNAPPROVED_FIELD",
            )

    def check_payload(self, payload: Mapping[str, Any]) -> None:
        """Validate both the field names and the shape of their values."""
        self.check_field_names(payload.keys())
        for key, value in payload.items():
            if key in self.approved_pii_fields:
                continue
            if isinstance(value, str) and len(value) > 120:
                raise PrivacyViolation(
                    f"Field {key!r} carries a {len(value)}-character string. Long free "
                    "text cannot be screened for identifiers by field name, so it is "
                    "rejected. Record a coded category instead.",
                    code="PRIVACY_FREE_TEXT",
                )
            if isinstance(value, (dict, list, tuple, set)):
                raise PrivacyViolation(
                    f"Field {key!r} is a nested structure. Event payloads must be flat "
                    "so that every collected field is visible to privacy review.",
                    code="PRIVACY_NESTED",
                )

    def suppress(self, count: int) -> bool:
        """Whether an aggregate over ``count`` units must be withheld."""
        return 0 < count < self.suppression_threshold

    def apply_suppression(self, value: Any, count: int) -> Any:
        """Return ``value``, or a suppression marker if the cell is too small."""
        if self.suppress(count):
            return f"suppressed (n < {self.suppression_threshold})"
        return value

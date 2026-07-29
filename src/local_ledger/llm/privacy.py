"""Privacy gate: per-call consent, aggregates-only cloud payload, redaction, egress audit.

Design principle: it must be *structurally impossible* for a raw Transaction to
leave the machine. ``build_cloud_payload`` accepts only the typed ``CloudPayload``
shape (category names + integer cents); a raw Transaction raises
``PrivacyViolation``. Cloud access requires a single-use consent token issued
only after the user previews the exact outbound payload.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable

from pydantic import BaseModel, ValidationError
from sqlmodel import Session

from local_ledger.models import AuditLog, Transaction

# Single-use, session-scoped consent tokens (in-process; not persisted).
_consent_tokens: set[str] = set()


class PrivacyViolation(Exception):
    pass


class CategoryAggregate(BaseModel):
    name: str
    income_cents: int = 0
    spent_cents: int = 0
    budgeted_cents: int = 0


class CloudPayload(BaseModel):
    month: str
    total_income_cents: int = 0
    total_spent_cents: int = 0
    total_budgeted_cents: int = 0
    categories: list[CategoryAggregate] = []


_FORBIDDEN_KEYS = {
    "payee",
    "payee_id",
    "payee_name",
    "account",
    "account_id",
    "account_name",
    "notes",
    "imported_id",
    "raw_txns",
    "transactions",
    "transferred_id",
}


def build_cloud_payload(data: object) -> CloudPayload:
    if isinstance(data, CloudPayload):
        return data
    if isinstance(data, Transaction) or not isinstance(data, dict):
        raise PrivacyViolation(
            "cloud payload must be a CloudPayload or aggregate dict; raw transactions are forbidden"
        )
    leaked = _FORBIDDEN_KEYS & set(data.keys())
    if leaked:
        raise PrivacyViolation(f"forbidden raw-data keys in payload: {sorted(leaked)}")
    try:
        return CloudPayload.model_validate(data)
    except ValidationError as exc:
        raise PrivacyViolation(f"payload did not match aggregate shape: {exc}") from exc


def preview_payload(data: dict) -> dict:
    """Return a JSON-serializable preview of exactly what would be sent."""
    payload = build_cloud_payload(data)
    return json.loads(payload.model_dump_json())


# --- consent tokens ---


def grant_consent(*, provider: str, payload_preview: dict) -> str:
    _ = provider  # provider name is recorded at egress; token itself is opaque
    _ = payload_preview
    token = uuid.uuid4().hex
    _consent_tokens.add(token)
    return token


def assert_consent(token: str | None) -> None:
    if not token or token not in _consent_tokens:
        raise ConsentDenied("no valid per-call consent token")
    _consent_tokens.discard(token)  # single-use


class ConsentDenied(Exception):
    pass


# --- redaction (belt-and-braces for any residual free text) ---

_REDACTORS: list[tuple[str, Callable[[str], str]]] = [
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", lambda s: "[REDACTED_EMAIL]"),
    (r"\b[A-Z]{2}\d{2}(?:[ ]?\w){11,30}\b", lambda s: "[REDACTED_IBAN]"),
    (r"\b\d{3}-\d{2}-\d{4}\b", lambda s: "[REDACTED_SSN]"),
    (r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", lambda s: "[REDACTED_PHONE]"),
    (r"\b(?:\d[ -]*?){13,19}\b", lambda s: "[REDACTED_CARD]"),
]


def redact_text(text: str) -> str:
    out = text
    for pattern, repl in _REDACTORS:
        out = re.sub(pattern, repl, out)
    return out


# --- egress audit ---


def record_egress(session: Session, *, provider: str, payload: CloudPayload | dict) -> AuditLog:
    if isinstance(payload, dict):
        payload = build_cloud_payload(payload)
    entry = AuditLog(
        entity_type="cloud_egress",
        entity_id=None,
        action="egress",
        actor=f"llm:cloud:{provider}",
        after_json=payload.model_dump_json(),
    )
    session.add(entry)
    session.commit()
    return entry

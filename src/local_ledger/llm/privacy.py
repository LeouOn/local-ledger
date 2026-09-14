"""Privacy gate: per-call consent, aggregates-only cloud payload, redaction, egress audit.

Design principle: it must be *structurally impossible* for a raw Transaction to
leave the machine. ``build_cloud_payload`` accepts only the typed ``CloudPayload``
shape (category names + integer cents); a raw Transaction raises
``PrivacyViolation``. Cloud access requires a single-use consent token issued
only after the user previews the exact outbound payload; the token is bound to
that specific provider and payload, so it cannot authorize a different
destination or a modified payload.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable

from pydantic import BaseModel, ValidationError
from sqlmodel import Session

from local_ledger.models import AuditLog, Transaction

# Single-use, session-scoped consent tokens bound to (provider, payload digest)
# (in-process; not persisted).
_consent_tokens: dict[str, dict[str, str]] = {}


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


def _canonical_json(payload: dict | CloudPayload) -> str:
    if isinstance(payload, CloudPayload):
        payload = json.loads(payload.model_dump_json())
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def request_digest(payload: dict | CloudPayload) -> str:
    """Digest of the exact outbound payload a consent token authorizes."""
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def grant_consent(*, provider: str, payload_preview: dict | CloudPayload) -> str:
    """Issue a single-use token bound to one destination and one exact payload."""
    if isinstance(payload_preview, dict):
        leaked = _FORBIDDEN_KEYS & set(payload_preview)
        if leaked:
            raise PrivacyViolation(
                f"cannot consent to send forbidden raw-data keys: {sorted(leaked)}"
            )
    token = uuid.uuid4().hex
    _consent_tokens[token] = {"provider": provider, "digest": request_digest(payload_preview)}
    return token


def assert_consent(
    token: str | None,
    *,
    provider: str,
    payload: dict | CloudPayload,
) -> None:
    """Consume a consent token; it must match the destination and payload in flight.

    ``provider`` and ``payload`` are mandatory: a token must never be accepted
    without checking what it authorizes. Rejections do not consume the token.
    """
    if not provider or payload is None:
        raise ConsentDenied("consent must be verified against an explicit provider and payload")
    record = _consent_tokens.get(token) if token else None
    if record is None:
        raise ConsentDenied("no valid per-call consent token")
    if provider != record["provider"]:
        raise ConsentDenied(
            f"consent token was issued for provider {record['provider']!r}, not {provider!r}"
        )
    if request_digest(payload) != record["digest"]:
        raise ConsentDenied("consent token does not authorize this payload")
    _consent_tokens.pop(token)  # single-use; consumed only on success


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


def egress_attempt_auditor(
    session: Session,
    *,
    provider: str,
    model: str | None,
    request: dict,
) -> Callable[[str, str | None], None]:
    """Return an audit callback recording EVERY cloud transmission attempt.

    Auditing happens at the request boundary, not after the fact: each send
    (including structured-output retries and failed transmissions such as
    timeouts) writes one row with the verbatim request body, the attempt
    number, and the outcome. Nothing is dropped when parsing, interpretation,
    or saving later fails.
    """
    state = {"attempt": 0}

    def audit(outcome: str, error: str | None = None) -> None:
        state["attempt"] += 1
        actor = f"llm:cloud:{provider}" + (f":{model}" if model else "")
        entry = AuditLog(
            entity_type="cloud_egress",
            entity_id=None,
            action="egress",
            actor=actor,
            after_json=json.dumps(
                {
                    "attempt": state["attempt"],
                    "outcome": outcome,
                    "error": error,
                    "request": request,
                },
                sort_keys=True,
            ),
        )
        session.add(entry)
        session.commit()

    return audit

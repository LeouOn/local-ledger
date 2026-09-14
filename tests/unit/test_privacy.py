"""Privacy gate tests: consent, aggregates-only payload, redaction, egress audit."""

from __future__ import annotations

import json

import pytest
from openai import OpenAI
from sqlmodel import Session, select

from local_ledger.db import init_db
from local_ledger.ledger import audit  # noqa: F401  - ensures audit module importable
from local_ledger.llm import privacy, providers
from local_ledger.models import AuditLog, ProviderSettings, Transaction


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)


@pytest.fixture()
def settings(session: Session) -> ProviderSettings:
    s = providers.load_settings(session)
    providers.save_settings(
        session,
        cloud_base_url="https://api.openai.com/v1",
        cloud_model="gpt-4o-mini",
        cloud_api_key_env="LOCAL_LEDGER_TEST_KEY",
    )
    return s


# --- consent ---


def test_get_cloud_client_blocked_without_consent(settings: ProviderSettings) -> None:
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(
            consent_token=None, settings=settings, payload={"month": "2026-01"}
        )


def test_consent_grants_single_use_access(
    settings: ProviderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_LEDGER_TEST_KEY", "sk-test-key-value")
    payload = privacy.preview_payload({"month": "2026-01", "total_spent_cents": 100})
    token = privacy.grant_consent(
        provider=providers.cloud_destination(settings), payload_preview=payload
    )

    client = providers.get_cloud_client(consent_token=token, settings=settings, payload=payload)
    assert isinstance(client, OpenAI)
    assert client.max_retries == 0  # No transmissions hidden from the audit wrapper.

    # single-use: a second call with the same token is blocked
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(consent_token=token, settings=settings, payload=payload)


def test_consent_denied_when_destination_changes(
    settings: ProviderSettings, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repointing cloud_base_url after granting must invalidate the token."""
    monkeypatch.setenv("LOCAL_LEDGER_TEST_KEY", "sk-test-key-value")
    payload = privacy.preview_payload({"month": "2026-01", "total_spent_cents": 100})
    token = privacy.grant_consent(
        provider=providers.cloud_destination(settings), payload_preview=payload
    )

    providers.save_settings(session, cloud_base_url="https://api.anthropic.com/v1")
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(consent_token=token, settings=settings, payload=payload)

    # Rejected attempts must not consume the token: the original endpoint works.
    providers.save_settings(session, cloud_base_url="https://api.openai.com/v1")
    client = providers.get_cloud_client(consent_token=token, settings=settings, payload=payload)
    assert isinstance(client, OpenAI)


def test_consent_denied_when_cloud_model_changes(
    settings: ProviderSettings, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swapping the cloud model after granting must invalidate the token."""
    monkeypatch.setenv("LOCAL_LEDGER_TEST_KEY", "sk-test-key-value")
    payload = privacy.preview_payload({"month": "2026-01", "total_spent_cents": 100})
    token = privacy.grant_consent(
        provider=providers.cloud_destination(settings), payload_preview=payload
    )

    providers.save_settings(session, cloud_model="gpt-4o")
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(consent_token=token, settings=settings, payload=payload)

    # Rejected attempts must not consume the token: the original model works.
    providers.save_settings(session, cloud_model="gpt-4o-mini")
    client = providers.get_cloud_client(consent_token=token, settings=settings, payload=payload)
    assert isinstance(client, OpenAI)


def test_consent_denied_for_different_payload(
    settings: ProviderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token must authorize exactly the payload the user previewed."""
    monkeypatch.setenv("LOCAL_LEDGER_TEST_KEY", "sk-test-key-value")
    payload = privacy.preview_payload({"month": "2026-01", "total_spent_cents": 100})
    token = privacy.grant_consent(
        provider=providers.cloud_destination(settings), payload_preview=payload
    )

    altered = dict(payload, total_spent_cents=999)
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(consent_token=token, settings=settings, payload=altered)

    # Rejected attempt must not consume the token: the exact payload works.
    client = providers.get_cloud_client(consent_token=token, settings=settings, payload=payload)
    assert isinstance(client, OpenAI)


def test_assert_consent_requires_explicit_bindings() -> None:
    """No code path may accept a token without checking what it authorizes."""
    payload = {"month": "2026-01", "total_spent_cents": 100}
    token = privacy.grant_consent(provider="openai", payload_preview=payload)

    with pytest.raises(privacy.ConsentDenied):
        privacy.assert_consent(token, provider="openai", payload=None)  # type: ignore[arg-type]
    with pytest.raises(privacy.ConsentDenied):
        privacy.assert_consent(token, provider="", payload=payload)

    # The rejected attempts did not consume the token.
    privacy.assert_consent(token, provider="openai", payload=payload)
    with pytest.raises(privacy.ConsentDenied):
        privacy.assert_consent(token, provider="openai", payload=payload)


def test_consent_digest_is_canonical_across_key_order() -> None:
    a = {"month": "2026-01", "total_spent_cents": 100, "categories": []}
    b = {"categories": [], "total_spent_cents": 100, "month": "2026-01"}
    assert privacy.request_digest(a) == privacy.request_digest(b)


def test_grant_consent_rejects_forbidden_keys() -> None:
    with pytest.raises(privacy.PrivacyViolation):
        privacy.grant_consent(
            provider="openai",
            payload_preview={"month": "2026-01", "total_spent_cents": 1, "payee": "Dr. Smith"},
        )


def test_get_cloud_client_blocked_when_cloud_not_configured(session: Session) -> None:
    s = providers.load_settings(session)  # defaults: no cloud url/key env
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(
            consent_token="anything", settings=s, payload={"month": "2026-01"}
        )


# --- aggregates-only payload (raw Transaction rejected) ---


def test_build_cloud_payload_accepts_dict() -> None:
    payload = privacy.build_cloud_payload(
        {
            "month": "2026-01",
            "total_income_cents": 200000,
            "total_spent_cents": 6500,
            "categories": [{"name": "Food", "spent_cents": 6500}],
        }
    )
    assert payload.month == "2026-01"
    dumped = payload.model_dump()
    # no raw-transaction fields leak
    for forbidden in ("payee", "payee_id", "account", "account_id", "notes", "imported_id"):
        assert forbidden not in dumped
        assert forbidden not in str(payload.categories)


def test_build_cloud_payload_rejects_raw_transaction() -> None:
    tx = Transaction(account_id=1, date="2026-01-01", amount_cents=-500)
    with pytest.raises(privacy.PrivacyViolation):
        privacy.build_cloud_payload(tx)  # type: ignore[arg-type]


def test_build_cloud_payload_rejects_dict_with_forbidden_keys() -> None:
    with pytest.raises(privacy.PrivacyViolation):
        privacy.build_cloud_payload(
            {"month": "2026-01", "total_spent_cents": 1, "payee": "Dr. Smith", "raw_txns": []}
        )


# --- redaction ---


@pytest.mark.parametrize(
    "raw,kind",
    [
        ("contact me at john@example.com", "EMAIL"),
        ("call 555-867-5309 now", "PHONE"),
        ("card 4111111111111111 expires", "CARD"),
        ("iban GB29 NWBK 6016 1331 9268 19", "IBAN"),
        ("ssn 123-45-6789", "SSN"),
    ],
)
def test_redact_matrix(raw: str, kind: str) -> None:
    out = privacy.redact_text(raw)
    assert f"[REDACTED_{kind}]" in out
    # original sensitive substring is gone
    assert "john@example.com" not in out
    assert "4111111111111111" not in out


# --- egress audit ---


def test_record_egress_writes_audit_row(session: Session) -> None:
    payload = privacy.build_cloud_payload(
        {"month": "2026-01", "total_income_cents": 0, "total_spent_cents": 100, "categories": []}
    )
    privacy.record_egress(session, provider="openai", payload=payload)

    rows = list(session.exec(select(AuditLog)).all())
    assert len(rows) == 1
    assert rows[0].actor == "llm:cloud:openai"
    assert rows[0].action == "egress"
    assert "2026-01" in (rows[0].after_json or "")


def test_egress_attempt_auditor_records_attempts_and_outcomes(session: Session) -> None:
    """Every transmission attempt — retries and failures included — is audited
    with the verbatim request, attempt number, and outcome."""
    request = {"model": "gpt-4o-mini", "messages": [], "response_format": {"type": "json_object"}}
    recorder = privacy.egress_attempt_auditor(
        session, provider="api.openai.com/v1", model="gpt-4o-mini", request=request
    )
    recorder("ok")
    recorder("error", "TimeoutError('request timed out')")

    rows = list(session.exec(select(AuditLog)).all())
    assert len(rows) == 2
    assert rows[0].actor == "llm:cloud:api.openai.com/v1:gpt-4o-mini"
    first = json.loads(rows[0].after_json or "{}")
    second = json.loads(rows[1].after_json or "{}")
    assert first["attempt"] == 1
    assert first["outcome"] == "ok"
    assert first["request"] == request
    assert second["attempt"] == 2
    assert second["outcome"] == "error"
    assert "timed out" in (second["error"] or "")


# --- preview ---


def test_preview_payload_returns_jsonable_dict() -> None:
    preview = privacy.preview_payload({"month": "2026-01", "total_spent_cents": 100})
    assert preview["month"] == "2026-01"
    # preview is what the user sees before consenting; must be JSON-serializable
    import json

    json.dumps(preview)

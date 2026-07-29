"""Privacy gate tests: consent, aggregates-only payload, redaction, egress audit."""

from __future__ import annotations

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
        providers.get_cloud_client(consent_token=None, settings=settings)


def test_consent_grants_single_use_access(
    settings: ProviderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_LEDGER_TEST_KEY", "sk-test-key-value")
    payload = privacy.preview_payload({"month": "2026-01", "total_spent_cents": 100})
    token = privacy.grant_consent(provider="openai", payload_preview=payload)

    client = providers.get_cloud_client(consent_token=token, settings=settings)
    assert isinstance(client, OpenAI)

    # single-use: a second call with the same token is blocked
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(consent_token=token, settings=settings)


def test_get_cloud_client_blocked_when_cloud_not_configured(session: Session) -> None:
    s = providers.load_settings(session)  # defaults: no cloud url/key env
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(consent_token="anything", settings=s)


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


# --- preview ---


def test_preview_payload_returns_jsonable_dict() -> None:
    preview = privacy.preview_payload({"month": "2026-01", "total_spent_cents": 100})
    assert preview["month"] == "2026-01"
    # preview is what the user sees before consenting; must be JSON-serializable
    import json

    json.dumps(preview)

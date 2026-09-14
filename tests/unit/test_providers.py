"""Provider abstraction tests (no sockets: probes are injected)."""

from __future__ import annotations

import pytest
from openai import OpenAI
from sqlmodel import Session

from local_ledger.db import init_db
from local_ledger.llm import providers
from local_ledger.llm.ollama import LLMUnavailable, OllamaState
from local_ledger.models import ProviderSettings


@pytest.fixture()
def settings(engine, session: Session) -> ProviderSettings:  # type: ignore[no-untyped-def]
    init_db(engine)
    return providers.load_settings(session)


@pytest.fixture(autouse=True)
def _schema(engine) -> None:  # type: ignore[no-untyped-def]
    init_db(engine)


def _detector(state: OllamaState):
    def _d(*_a, **_k):
        return state
    return _d


def test_get_local_client_raises_when_not_ready(settings: ProviderSettings) -> None:
    with pytest.raises(LLMUnavailable) as exc:
        providers.get_local_client(settings, state_detector=_detector(OllamaState.NOT_RUNNING))
    assert exc.value.state == OllamaState.NOT_RUNNING


def test_get_local_client_returns_openai_when_ready(settings: ProviderSettings) -> None:
    client = providers.get_local_client(settings, state_detector=_detector(OllamaState.READY))
    assert isinstance(client, OpenAI)
    assert str(client.base_url).rstrip("/").endswith("v1")


def test_local_client_refuses_non_loopback_url(session: Session) -> None:
    """The consent-free local path must never point off-machine."""
    s = providers.save_settings(session, local_base_url="http://192.168.1.5:11434/v1")
    with pytest.raises(providers.NonLocalEndpointError):
        providers.get_local_client(s, state_detector=_detector(OllamaState.READY))
    with pytest.raises(providers.NonLocalEndpointError):
        providers.require_local_ready(s, state_detector=_detector(OllamaState.READY))


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:9000/v1", "http://[::ffff:127.0.0.1]:1/v1"],
)
def test_local_endpoint_accepts_loopback_variants(base_url: str) -> None:
    providers.assert_local_endpoint(base_url)  # must not raise


@pytest.mark.parametrize("base_url", ["http://example.com:11434/v1", "http://0.0.0.0:8000/v1", "not a url", ""])
def test_local_endpoint_refuses_non_loopback(base_url: str) -> None:
    with pytest.raises(providers.NonLocalEndpointError):
        providers.assert_local_endpoint(base_url)


def test_all_local_profiles_use_loopback_endpoints() -> None:
    from local_ledger.llm.local_server import PROFILES

    for profile in PROFILES.values():
        providers.assert_local_endpoint(profile.base_url)


def test_cloud_destination_includes_endpoint_and_model(session: Session) -> None:
    providers.save_settings(
        session, cloud_base_url="https://api.openai.com/v1", cloud_model="gpt-4o-mini"
    )
    a = providers.cloud_destination(providers.load_settings(session))
    providers.save_settings(session, cloud_model="gpt-4o")
    b = providers.cloud_destination(providers.load_settings(session))
    providers.save_settings(session, cloud_base_url="https://api.anthropic.com/v1")
    c = providers.cloud_destination(providers.load_settings(session))
    assert len({a, b, c}) == 3


def test_get_cloud_client_blocked_by_default(settings: ProviderSettings) -> None:
    with pytest.raises(providers.CloudCallBlocked):
        providers.get_cloud_client(
            consent_token=None, settings=settings, payload={"month": "2026-01"}
        )


def test_list_local_models_injectable(settings: ProviderSettings) -> None:
    models = providers.list_local_models(settings, lister=lambda _url, _t: ["llama3.3:8b", "phi4:latest"])
    assert models == ["llama3.3:8b", "phi4:latest"]


def test_load_settings_returns_singleton_defaults(session: Session) -> None:
    s1 = providers.load_settings(session)
    s2 = providers.load_settings(session)
    assert s1.id == s2.id == 1
    assert s1.local_base_url == "http://localhost:11434/v1"
    assert s1.evaluation_default_provider == "local"
    assert s1.local_profile == "ollama"
    assert s1.local_autostart is False


def test_save_settings_updates_fields(session: Session) -> None:
    providers.save_settings(session, local_model="phi4:latest", cloud_base_url="https://api.openai.com/v1")
    row = session.get(ProviderSettings, 1)
    assert row.local_model == "phi4:latest"
    assert row.cloud_base_url == "https://api.openai.com/v1"


def test_settings_never_stores_api_key_value(session: Session) -> None:
    """Only the env-var NAME is stored, never the key itself."""
    providers.save_settings(session, cloud_api_key_env="OPENAI_API_KEY")
    row = session.get(ProviderSettings, 1)
    assert row.cloud_api_key_env == "OPENAI_API_KEY"
    # no field holds a raw key
    dump = row.model_dump()
    assert not any("sk-" in str(v) for v in dump.values())

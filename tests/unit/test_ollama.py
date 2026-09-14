"""Local server degradation contract tests.

detect_state() accepts injectable probe callables so unit tests never open a
socket (the unit-suite socket guard would otherwise block them). Probing is
endpoint-first: a healthy server is adopted regardless of the local binary.
"""

from __future__ import annotations

import pytest

from local_ledger.llm.ollama import (
    DEGRADED_BEHAVIOR,
    LLMUnavailable,
    OllamaState,
    degraded_message,
    detect_state,
    guidance_for,
)


def _probes(*, binary=True, endpoint=True, models=("llama3.3:8b",)):
    return {
        "probe_binary": lambda: binary,
        "probe_endpoint": lambda _url, _timeout: endpoint,
        "list_models": lambda _url, _timeout: models,
    }


def test_state_not_installed() -> None:
    assert (
        detect_state("http://localhost:11434", "llama3.3:8b", **_probes(binary=False, endpoint=False))
        == OllamaState.NOT_INSTALLED
    )


def test_state_not_running() -> None:
    assert (
        detect_state("http://localhost:11434", "llama3.3:8b", **_probes(endpoint=False))
        == OllamaState.NOT_RUNNING
    )


def test_state_model_missing() -> None:
    assert (
        detect_state("http://localhost:11434", "llama3.3:8b", **_probes(models=("phi4:latest",)))
        == OllamaState.MODEL_MISSING
    )


def test_state_ready() -> None:
    assert detect_state("http://localhost:11434", "llama3.3:8b", **_probes()) == OllamaState.READY


def test_state_ready_when_binary_missing_but_endpoint_healthy() -> None:
    """A healthy, compatible server is reused even with no binary installed."""
    assert (
        detect_state(
            "http://127.0.0.1:8000", "glm-5.3-flash", **_probes(binary=False, models=("glm-5.3-flash",))
        )
        == OllamaState.READY
    )


def test_state_model_missing_on_empty_library() -> None:
    """An empty model library means the configured model is definitely absent."""
    assert detect_state("http://localhost:11434", "llama3.3:8b", **_probes(models=())) == (
        OllamaState.MODEL_MISSING
    )


def test_state_ready_when_model_discovery_unsupported() -> None:
    """No usable model listing: endpoint health is the best available signal."""
    assert detect_state("http://localhost:11434", "llama3.3:8b", **_probes(models=None)) == (
        OllamaState.READY
    )


def test_llm_unavailable_carries_state_and_guidance() -> None:
    err = LLMUnavailable(OllamaState.NOT_RUNNING, "run `ollama serve`")
    assert err.state == OllamaState.NOT_RUNNING
    assert "ollama serve" in err.guidance
    assert "ollama serve" in str(err)


@pytest.mark.parametrize("state", list(OllamaState))
def test_every_state_has_guidance(state: OllamaState) -> None:
    """Each state maps to a non-empty human instruction."""
    from local_ledger.llm.ollama import GUIDANCE

    assert GUIDANCE[state].strip()


@pytest.mark.parametrize("profile", ["ollama", "ds4", "glm"])
@pytest.mark.parametrize("state", list(OllamaState))
def test_guidance_is_provider_appropriate(profile: str, state: OllamaState) -> None:
    """Every profile has non-empty guidance, and non-Ollama profiles never
    tell the user to install Ollama."""
    msg = guidance_for(state, profile)
    assert msg.strip()
    if profile != "ollama":
        assert "Ollama" not in msg or state == OllamaState.READY


def test_guidance_unknown_profile_falls_back_to_default() -> None:
    assert guidance_for(OllamaState.NOT_RUNNING, "nope") == guidance_for(
        OllamaState.NOT_RUNNING, "ollama"
    )


def test_degraded_message_per_feature() -> None:
    msg = degraded_message(OllamaState.NOT_RUNNING, "paste_parse")
    assert msg
    assert isinstance(msg, str)


def test_degraded_behavior_covers_all_states_and_features() -> None:
    features = {"paste_parse", "categorize", "evaluate"}
    for state in OllamaState:
        assert state in DEGRADED_BEHAVIOR
        for f in features:
            assert f in DEGRADED_BEHAVIOR[state], f"{state} missing {f}"
            assert DEGRADED_BEHAVIOR[state][f].strip()

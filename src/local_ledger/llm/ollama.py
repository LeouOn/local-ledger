"""Ollama degradation contract.

Probes the local Ollama runtime and reports one of four states so every LLM
feature can degrade to a deterministic message instead of raising a raw
``ConnectionError``. Probes are injectable so the unit suite (which blocks
sockets) can exercise every state without a real Ollama.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from collections.abc import Callable
from enum import StrEnum

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.3:8b"


class OllamaState(StrEnum):
    NOT_INSTALLED = "NOT_INSTALLED"
    NOT_RUNNING = "NOT_RUNNING"
    MODEL_MISSING = "MODEL_MISSING"
    READY = "READY"


GUIDANCE: dict[OllamaState, str] = {
    OllamaState.NOT_INSTALLED: "Install Ollama from https://ollama.com and restart the app.",
    OllamaState.NOT_RUNNING: "Start the local model server: run `ollama serve`.",
    OllamaState.MODEL_MISSING: "Pull the model: run `ollama pull <model>` (set in Settings).",
    OllamaState.READY: "Local model is ready.",
}

# Per-feature degraded UX messages, keyed by state then feature.
DEGRADED_BEHAVIOR: dict[OllamaState, dict[str, str]] = {
    OllamaState.NOT_INSTALLED: {
        "paste_parse": "Paste-parse is unavailable: install Ollama to enable local parsing.",
        "categorize": "Auto-categorization is unavailable: install Ollama to enable it.",
        "evaluate": "LLM budget evaluation is unavailable: install Ollama or connect a cloud provider in Settings.",
    },
    OllamaState.NOT_RUNNING: {
        "paste_parse": "Paste-parse is unavailable: start Ollama (`ollama serve`).",
        "categorize": "Auto-categorization is unavailable: start Ollama.",
        "evaluate": "LLM budget evaluation is unavailable: start Ollama or use a cloud provider.",
    },
    OllamaState.MODEL_MISSING: {
        "paste_parse": "Paste-parse is unavailable: pull the configured model in Settings.",
        "categorize": "Auto-categorization is unavailable: pull the configured model.",
        "evaluate": "LLM budget evaluation is unavailable: pull the configured model.",
    },
    OllamaState.READY: {
        "paste_parse": "Ready.",
        "categorize": "Ready.",
        "evaluate": "Ready.",
    },
}


def degraded_message(state: OllamaState, feature: str) -> str:
    return DEGRADED_BEHAVIOR[state][feature]


class LLMUnavailable(Exception):
    def __init__(self, state: OllamaState, guidance: str) -> None:
        self.state = state
        self.guidance = guidance
        super().__init__(f"{state.value}: {guidance}")


def _default_probe_binary() -> bool:
    return shutil.which("ollama") is not None


def _default_probe_endpoint(base_url: str, timeout: float) -> bool:
    try:
        urllib.request.urlopen(f"{base_url}/api/tags", timeout=timeout).close()
    except OSError:
        return False
    return True


def _default_list_models(base_url: str, timeout: float) -> list[str]:
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except OSError:
        return []
    return [m.get("name", "") for m in payload.get("models", [])]


def detect_state(
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 1.0,
    *,
    probe_binary: Callable[[], bool] = _default_probe_binary,
    probe_endpoint: Callable[[str, float], bool] = _default_probe_endpoint,
    list_models: Callable[[str, float], list[str]] = _default_list_models,
) -> OllamaState:
    if not probe_binary():
        return OllamaState.NOT_INSTALLED
    if not probe_endpoint(base_url, timeout):
        return OllamaState.NOT_RUNNING
    if model not in list_models(base_url, timeout):
        return OllamaState.MODEL_MISSING
    return OllamaState.READY

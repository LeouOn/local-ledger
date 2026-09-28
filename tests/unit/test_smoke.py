"""Unit tests for the reproducible smoke runner."""

from __future__ import annotations

from unittest.mock import MagicMock

from local_ledger.llm.ollama import OllamaState
from local_ledger.llm.smoke import main, run_smoke


def test_smoke_deferred_when_model_not_ready() -> None:
    result = run_smoke(
        "ollama",
        state_detector=lambda _root, _model: OllamaState.NOT_RUNNING,
    )
    assert result.verdict == "DEFERRED"
    assert result.profile == "ollama"
    assert result.model == "llama3.3:8b"
    assert "NOT_RUNNING" in result.detail


def test_smoke_pass_when_model_ready_and_replies() -> None:
    fake_client = MagicMock()
    fake_choice = MagicMock()
    fake_choice.message.content = "OK"
    fake_client.chat.completions.create.return_value = MagicMock(choices=[fake_choice])

    result = run_smoke(
        "ds4",
        state_detector=lambda _root, _model: OllamaState.READY,
        client_factory=lambda _base, _to: fake_client,
    )
    assert result.verdict == "PASS"
    assert result.profile == "ds4"
    assert result.model == "deepseek-v4-flash"
    assert result.latency_ms is not None
    assert result.latency_ms >= 0
    assert result.output == "OK"


def test_smoke_failed_on_remote_endpoint() -> None:
    result = run_smoke(
        "ollama",
        endpoint="https://api.openai.com/v1",
        state_detector=lambda _root, _model: OllamaState.READY,
    )
    assert result.verdict == "FAILED"
    assert "loopback" in result.detail.lower()


def test_smoke_failed_on_request_error() -> None:
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = TimeoutError("Connection timed out")

    result = run_smoke(
        "ollama",
        state_detector=lambda _root, _model: OllamaState.READY,
        client_factory=lambda _base, _to: fake_client,
    )
    assert result.verdict == "FAILED"
    assert "Connection timed out" in result.detail


def test_smoke_cli_main_deferred(capsys) -> None:
    code = main(["--profile", "ds4"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Profile:   ds4" in captured.out
    assert "Verdict:   DEFERRED" in captured.out

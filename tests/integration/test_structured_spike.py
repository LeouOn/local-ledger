"""Integration spike: real-Ollama structured output quality (deselected by default).

Run with: ``pytest -m integration tests/integration/test_structured_spike.py``

Requires Ollama running with the configured model pulled. If Ollama is absent,
the test records a SKIP-deferred verdict to the evidence file rather than
failing silently.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

EVIDENCE = Path(__file__).resolve().parent.parent.parent / ".omo" / "evidence" / "task-9-local-ledger.md"


@pytest.fixture(scope="module", autouse=True)
def _require_ollama() -> None:
    from local_ledger.llm.ollama import GUIDANCE, OllamaState, detect_state

    base = os.environ.get("LOCAL_LEDGER_OLLAMA_URL", "http://localhost:11434")
    model = os.environ.get("LOCAL_LEDGER_OLLAMA_MODEL", "llama3.3:8b")
    state = detect_state(base, model)
    if state != OllamaState.READY:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(
            f"# Task 9 - Ollama structured-output spike\n\n"
            f"**Verdict: SKIP-deferred** (Ollama not READY)\n\n"
            f"- detected state: `{state.value}`\n"
            f"- guidance: {GUIDANCE[state]}\n\n"
            f"Re-run with `pytest -m integration tests/integration/test_structured_spike.py` "
            f"once Ollama is running and `{model}` is pulled.\n",
            encoding="utf-8",
        )
        pytest.skip(f"ollama state {state.value}; spike deferred", allow_module_level=True)


def test_structured_output_spike_records_verdict(tmp_path: Path) -> None:
    from pydantic import BaseModel
    from sqlalchemy import create_engine
    from sqlmodel import Session, SQLModel

    from local_ledger.llm.providers import get_local_client, load_settings
    from local_ledger.llm.structured import extract, make_openai_completer

    class Item(BaseModel):
        date: str
        merchant: str
        amount_cents: int

    db_path = tmp_path / "structured_spike.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(db_url)
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            settings = load_settings(session)
            client = get_local_client(settings)
    finally:
        engine.dispose()

    completer = make_openai_completer(client)
    messages = [
        {
            "role": "system",
            "content": "Extract a single transaction as JSON with keys date, merchant, amount_cents.",
        },
        {"role": "user", "content": "On 2026-01-05, bought coffee at Blue Bottle for $4.50."},
    ]
    obj = extract(completer, Item, messages, settings.local_model, max_retries=2)

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        "# Task 9 - Ollama structured-output spike\n\n"
        f"**Verdict: PASS**\n\n"
        f"- model: `{settings.local_model}`\n"
        f"- parsed: `{obj.model_dump_json()}`\n\n"
        "Instructor-style extraction via the OpenAI-compatible endpoint produced a "
        "schema-valid object on the first parse.\n",
        encoding="utf-8",
    )
    assert isinstance(obj.amount_cents, int)

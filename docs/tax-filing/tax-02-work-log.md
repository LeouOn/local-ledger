# TAX-02 work log — reproducible local runtime and evaluation harness

Agent B, started September 28, 2026. Status: **completed**.

This log records the goals, scope, and implementation plan for TAX-02.
The acceptance criteria are documented in [tasks.md](tasks.md#tax-02--make-local-runtime-and-evaluation-harness-reproducible).

## Goals

1. **Resolve seven test failures in `test_local_server.py`.** The existing tests fail on any environment where `~/Projects/ds4/run-ds4f.sh` is absent because `LocalServerManager` does not allow injecting executable/which resolution or faking the launcher hermetically.
2. **Configurable and discoverable launcher paths.** Model launcher locations must not assume a hardcoded home directory (`~/Projects/ds4`). Support configuration via environment variables (`LOCAL_LEDGER_DS4_HOME`, `DS4_HOME`) and PATH discovery, while retaining existing uncommitted runtime improvements (SSD streaming, expert cache, KV cache, env overrides like `CTX=16384`, GLM listed model `glm-5.2`).
3. **Hermetic unit testing.** Server manager unit tests must inject filesystem/launcher checks so fake launchers can test delayed startup, timeout, server reuse, single-flight startup, wrong served model, and missing executable without requiring installed model scripts or real processes.
4. **Isolate integration evaluations and spikes.** `tests/integration/test_eval_quality.py` and `test_structured_spike.py` must use isolated temporary or in-memory databases rather than mutating `DEFAULT_DB_URL` (`data/ledger.db`). Never mutate user provider preferences in the application DB as a test side effect.
5. **Deterministic scoring and reproducible smoke command.** Replace placeholder unscored results with explicit scored/unscored/failed/skipped reporting. Provide a real smoke command/runner that uses synthetic input, records profile, model ID, endpoint, timeout, and latency, and yields a deferred result when unavailable.
6. **Accurate runtime documentation.** Update `README.md` to reflect cross-platform runtime commands, available profiles (Ollama, DS4, GLM), and realistic model memory guidance.

## Owned paths

- `src/local_ledger/llm/local_server.py`
- `src/local_ledger/llm/providers.py`
- `src/local_ledger/llm/smoke.py`
- `app_pages/settings.py` (if launcher path or status display is updated)
- `tests/unit/test_local_server.py`
- `tests/integration/test_eval_quality.py`
- `tests/integration/test_structured_spike.py`
- `tests/integration/test_smoke.py`
- `README.md`
- `docs/tax-filing/tax-02-work-log.md`
- `docs/tax-filing/tasks.md`

## Implementation steps

1. In `src/local_ledger/llm/local_server.py`:
   - Introduce `get_ds4_home()` checking `LOCAL_LEDGER_DS4_HOME` / `DS4_HOME` before falling back to `~/Projects/ds4`.
   - Update `resolve_launcher` to support path discovery and injectable `which` callable.
   - Inject `which` (default `shutil.which`) into `LocalServerManager.__init__` and pass it to `resolve_launcher`.
   - Retain uncommitted launch arguments and `launch_env`.
2. In `tests/unit/test_local_server.py`:
   - Inject hermetic launcher check in `make_manager` so tests verify startup, adoption, slow loading, exit reporting, and timeout without relying on filesystem scripts.
   - Verify all 16 tests in `test_local_server.py` pass.
3. In `tests/integration/test_eval_quality.py` and `test_structured_spike.py`:
   - Isolate database connection to a temporary SQLite database (no mutation of `data/ledger.db`).
   - Implement explicit scoring based on rubric criteria (valid JSON, structure, non-empty content).
   - Ensure provider settings in default DB are untouched.
4. Implement `src/local_ledger/llm/smoke.py` and `tests/integration/test_smoke.py`:
   - Synthetic smoke test measuring endpoint latency, model ID, profile, timeout.
   - Yields clean deferred result if server/model is not ready.
5. In `README.md`:
   - Update instructions to be cross-platform, include DS4 and GLM profiles, and correct memory/model details.

## Test evidence

- `pytest tests/unit/test_local_server.py tests/unit/test_smoke.py tests/unit/test_rubric_scoring.py -v`:
  **26 passed in 2.91s**.
  - All 7 previous failures in `test_local_server.py` resolved hermetically.
  - Delayed startup, exit reporting, bounded timeout, single-flight locking, process alive reuse, and launcher missing behavior verified.
  - Launcher configuration (`LOCAL_LEDGER_DS4_HOME`, `DS4_HOME`), PATH discovery, and custom `which` injection covered by dedicated unit tests.
  - Hermetic smoke tests verify PASS, DEFERRED (when not ready), FAILED (on remote endpoint or request timeout), and CLI exit code.
  - Rubric scoring logic verified for high-quality and low-quality evaluations.
- `pytest -m integration tests/integration/ -v`:
  - `test_real_local_model_smoke` yields `SKIP-deferred` when model is absent, recording latency and endpoint info cleanly to `.omo/evidence/local-runtime-smoke.md`.
  - `test_eval_quality_spike_records_verdict` and `test_structured_output_spike_records_verdict` use isolated temporary SQLite databases (`tmp_path`).
  - Zero side effects on `DEFAULT_DB_URL` (`data/ledger.db`) or user provider preferences.
- `ruff check src/local_ledger/llm/ tests/unit/test_local_server.py tests/unit/test_smoke.py tests/unit/test_rubric_scoring.py tests/integration/test_smoke.py tests/integration/test_eval_quality.py tests/integration/test_structured_spike.py`:
  **All checks passed!**


# Local Ledger

A local-first personal budgeting app that uses a local language model (with an
optional, explicitly-consented cloud path) to evaluate your income and spending,
score budgets against monthly targets, flag anomalies, and propose changes —
while remembering its own suggestions so you can see whether they helped.

## What it does

- **Import** bank/credit-card CSVs with auto-suggested column mapping, or paste a
  statement and let the local LLM parse it into transactions you confirm.
  Imported rows are staged first: the Import page lists the batch, you accept or
  reject the rows you want, and only accepted rows are written. Committing twice
  writes once, and a rejected row is never written.
- **Budget** per category with optional month-to-month carryover (supports both
  envelope and category-budgeting styles).
- **Evaluate** your budget: deterministic anomaly detection (overspend, MoM
  drift, outlier transactions) runs always; an on-demand LLM interpretation adds
  a written summary and suggested actions.
- **Explore what-ifs** — adjust any category with a slider and see the projected
  savings impact instantly.
- **Track** recommendations over time — each suggestion is stored append-only,
  and you mark whether you followed it.
- **Auto-categorize** imported transactions deterministically (rules engine) and
  via LLM (dry-run default, low-confidence and unknown-category proposals routed
  to a review queue; nothing is created silently).
- **Detect recurring transactions** — subscriptions and regular bills grouped by
  payee, amount, and interval with a confidence score.
- **Save toward goals** — set a target amount and date; the app tracks progress,
  projects achievement, and flags when you're behind schedule.
- **Chat about your budget** — a persistent sidebar assistant answers questions
  using only your aggregate data (never raw transactions).

## Privacy posture

- Runs on your machine. Bound to `127.0.0.1` by default (`.streamlit/config.toml`).
  If a non-localhost address is detected at startup, the app refuses to serve
  any data (error page + `st.stop`). The import-time guard also catches the
  `STREAMLIT_SERVER_ADDRESS` env var.
- Local LLM (Ollama) by default; cloud is opt-in **per call** with a one-use
  consent token you issue after previewing the exact payload.
- The only thing that ever leaves the machine is **category names plus numeric
  totals** — never raw transactions, payees, account numbers, or notes.
- Every cloud egress is written to a local audit log (`auditlog` table).

## Requirements

- Python **3.11 or 3.12** (not 3.13/3.14 — some deps lack wheels yet)
- [uv](https://docs.astral.sh/uv/) (recommended) or plain `pip` + `venv`
- [Ollama](https://ollama.com) (optional; the app degrades gracefully without it)

## Quickstart

```bash
# 1. create an isolated environment with Python 3.12
uv venv --python 3.12 .venv            # or: python3.12 -m venv .venv

# 2. install the app + dev tooling
uv pip install -e ".[dev]"             # or: source .venv/bin/activate && pip install -e ".[dev]"

# 3. run the app (bound to 127.0.0.1, schema auto-migrates on first launch)
streamlit run app.py
```

Then open the printed `http://localhost:8501` URL.

### Tax document pilot

For 2025 federal/California review, use the
[local tax pilot guide](docs/tax-filing/local-pilot-guide.md). It explains isolated
storage, local model settings, document proposals and verification, working-paper
views, TaxAct comparisons and current limits. TaxAct computes and files the return.

### First-run guide

1. **Budgets page** → create your first account (e.g. "Checking") and a few
   categories (Food, Rent, Salary, …).
2. **Import page** → upload a CSV export from your bank. The mapping is
   auto-suggested; review it, then stage and commit. Or paste a statement and
   click **Parse with local LLM** (requires Ollama running), then confirm.
3. **Budgets page** → set monthly allocations per category (toggle carryover for
   envelope-style rollover).
4. **Insights page** → click **Evaluate my budget** for deterministic findings
   plus an optional LLM interpretation.

## Optional: enable local LLM profiles

Local Ledger supports multiple local model profiles selectable in **Settings**:

### 1. Ollama (default)

```bash
ollama serve                       # start the local model server
ollama pull llama3.1:8b            # pull an 8B model (fits in 16 GB RAM; note llama3.3 is 70B only)
```

For smaller machines (8 GB RAM), `ollama pull phi4` or `ollama pull qwen2.5:7b` are supported.
Select the model in **Settings**.

### 2. DwarfStar (DeepSeek V4 Flash & GLM 5.3 Flash)

For local ROCm / Vulkan inference engines:
- **Profile `ds4`**: DeepSeek V4 Flash (`deepseek-v4-flash`), launched via `run-ds4f.sh server`.
- **Profile `glm`**: GLM 5.3 Flash (listed as `glm-5.2` by the engine), launched via `run-glm.sh server`.
- **Launcher directory**: Configured via `LOCAL_LEDGER_DS4_HOME` (or `DS4_HOME`, defaulting to `~/Projects/ds4` or PATH discovery).
- Single-flight autostart can be enabled in **Settings** ("Start local server when needed").

### Verify local runtime with the smoke runner

Check local model availability, endpoint latency, and profile health without touching the database:

```bash
python -m local_ledger.llm.smoke --profile ollama
python -m local_ledger.llm.smoke --profile ds4
python -m local_ledger.llm.smoke --profile glm
```

Returns `PASS` (with latency measurement), `DEFERRED` (if server or model is not ready), or `FAILED`.

## Local LLM degradation contract

The app probes the configured server at startup and on every page; each LLM feature shows a
deterministic message (never a raw exception) when it's unavailable:

| State | What you see |
|---|---|
| `NOT_INSTALLED` | Profile-specific installation guidance (Ollama or DwarfStar) |
| `NOT_RUNNING` | "Start the local model server..." or autostart prompt |
| `MODEL_MISSING` | Guidance to pull or select the configured model |
| `READY` | Features work normally |

The ledger, budgets, and deterministic evaluation always work — only the LLM
features degrade.

## Optional: cloud provider (opt-in, per call)

In **Settings**, configure a cloud base URL, model, and the **name** of the env
var holding your API key (the key itself is never stored). On the **Insights**
page, choosing the cloud provider requires issuing a one-use consent token from
**Settings** after you preview the exact aggregates-only payload that will be
sent.

```bash
# example: point the app at your OpenAI key via the named env var
export LOCAL_LEDGER_OPENAI_KEY="sk-..."    # on Windows: setx LOCAL_LEDGER_OPENAI_KEY "sk-..."
# then set "API key ENV var name" = LOCAL_LEDGER_OPENAI_KEY in Settings
```

## Tests

```bash
pytest tests/unit -q                 # fast unit test suite
pytest -m integration -q             # real local runtime spikes (optional, reports DEFERRED if unavailable)
ruff check src tests app.py app_pages
```

Integration tests (LLM quality spike, structured-output spike, runtime smoke) require
a running local server and are deselected by default; they record a SKIP-deferred verdict
if the server is absent.

## Migration policy

The app runs `alembic upgrade head` at startup against `data/ledger.db`, so
schema changes apply automatically. To regenerate the migration after a model
change:

```bash
alembic revision --autogenerate -m "describe change"
```

## Scope boundaries

Not included: bank aggregation/Plaid/sync, multi-user or hosting, double-entry
accounting, multi-currency, XLS import, or sending raw tax documents to the cloud.
Tax PDFs and investment/rental input auditing are available through the tax pilot;
this does not implement a tax calculation engine or electronic filing.

The full design and decision trail lives in `.omo/plans/local-ledger.md`.

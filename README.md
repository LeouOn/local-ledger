# Local Ledger

A local-first personal budgeting app that uses a local language model (with an
optional, explicitly-consented cloud path) to evaluate your income and spending,
score budgets against monthly targets, flag anomalies, and propose changes —
while remembering its own suggestions so you can see whether they helped.

## What it does

- **Import** bank/credit-card CSVs with auto-suggested column mapping, or paste a
  statement and let the local LLM parse it into transactions you confirm.
- **Budget** per category with optional month-to-month carryover (supports both
  envelope and category-budgeting styles).
- **Evaluate** your budget: deterministic anomaly detection (overspend, MoM
  drift) runs always; an on-demand LLM interpretation adds a written summary and
  suggested actions.
- **Track** recommendations over time — each suggestion is stored append-only,
  and you mark whether you followed it.
- **Auto-categorize** imported transactions (dry-run default, low-confidence and
  unknown-category proposals routed to a review queue; nothing is created
  silently).

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
uv venv --python 3.12 .venv            # or: py -3.12 -m venv .venv

# 2. install the app + dev tooling
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
# or with pip:  .venv\Scripts\activate && pip install -e ".[dev]"

# 3. run the app (bound to 127.0.0.1, schema auto-migrates on first launch)
streamlit run app.py
```

Then open the printed `http://localhost:8501` URL.

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

## Optional: enable the local LLM

```bash
ollama serve                       # start the local model server
ollama pull llama3.3:8b            # pull a model (8B fits in 16 GB RAM)
```

For an 8 GB RAM machine, `ollama pull phi4` is a smaller alternative. Set the
model in **Settings**.

## Ollama degradation contract

The app probes Ollama at startup and on every page; each LLM feature shows a
deterministic message (never a raw exception) when it's unavailable:

| State | What you see |
|---|---|
| `NOT_INSTALLED` | "Install Ollama from https://ollama.com …" |
| `NOT_RUNNING` | "Start the local model server: run `ollama serve`." |
| `MODEL_MISSING` | "Pull the model: run `ollama pull <model>`." |
| `READY` | features work normally |

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
setx LOCAL_LEDGER_OPENAI_KEY "sk-..."
# then set "API key ENV var name" = LOCAL_LEDGER_OPENAI_KEY in Settings
```

## Tests

```bash
.venv\Scripts\python.exe -m pytest tests/unit -q      # fast gate (~15s)
.venv\Scripts\python.exe -m pytest tests/unit -q      # includes perf (10K-row seed)
.venv\Scripts\python.exe -m pytest -m integration -q  # real-Ollama spikes (optional)
.venv\Scripts\ruff check src tests app.py app_pages
```

Integration tests (LLM quality spike, structured-output spike) require Ollama
running and are deselected by default; they record a SKIP-deferred verdict if
Ollama is absent.

## Migration policy

The app runs `alembic upgrade head` at startup against `data/ledger.db`, so
schema changes apply automatically. To regenerate the migration after a model
change:

```bash
.venv\Scripts\alembic revision --autogenerate -m "describe change"
```

## Scope boundaries (v1)

Not included: bank aggregation/Plaid/sync, multi-user or hosting, double-entry
accounting, investments, recurring-transaction auto-detection, multi-currency,
in-app chat, PDF/XLS import, or sending anything but aggregates to the cloud.

The full design and decision trail lives in `.omo/plans/local-ledger.md`.

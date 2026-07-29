# Local Ledger

A local-first personal budgeting app that uses a local language model (with an
optional, explicitly-consented cloud path) to evaluate your income and spending,
score budgets against monthly targets, flag anomalies, and propose changes —
while remembering its own suggestions so you can see whether they helped.

> **Status:** under construction. See `.omo/plans/local-ledger.md` for the full
> work plan.

## Privacy posture

- Runs on your machine, bound to `127.0.0.1` only.
- Local LLM (Ollama) by default; cloud is opt-in **per call**.
- The only thing that ever leaves the machine is category names plus numeric
  totals — never raw transactions, payees, or account numbers.
- Every cloud egress is written to a local audit log.

## Requirements

- Python 3.11 or 3.12
- [Ollama](https://ollama.com) (optional; app degrades gracefully if absent)

## Quickstart

(Full quickstart lands in the final README, todo 30.)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
streamlit run app.py
```

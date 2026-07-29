---
slug: local-ledger
status: drafting
intent: clear
pending-action: write .omo/plans/local-ledger.md
approach: Streamlit local web app (Python) with a local-first hybrid LLM layer (OpenAI-compatible abstraction: Ollama local default, cloud API opt-in with privacy gate), SQLite local ledger, CSV import + manual entry + LLM tool-calling to parse pasted budgets/statements, TDD with pytest.
---

# Draft: local-ledger

## Components (topology ledger)
<!-- Locked 2026-07-29 with user. id | outcome | status | evidence -->
- C1 data-layer | Local SQLite ledger: accounts, transactions, categories, income/expense classification, recurring items | active | (plan) src/ledger/
- C2 ingest | CSV bank-statement import w/ column mapping + manual entry + LLM tool-calling parse of pasted statements/budgets | active | (plan) src/ingest/
- C3 llm-layer | Provider abstraction (OpenAI-compatible) over local (Ollama) + cloud APIs; structured output; privacy gate | active | (plan) src/llm/
- C4 analysis | Budget evaluation, month-over-month trends, category drift, recommendations that persist and are re-evaluated over time | active | (plan) src/analysis/
- C5 ui | Streamlit dashboard: transactions, budgets, trends, LLM insights/chat, import wizard | active | (plan) app.py + src/ui/
- C6 privacy | Local-only storage; explicit per-call opt-in before any data leaves machine; redaction for cloud calls | active | (plan) src/llm/privacy.py

## Open assumptions (announced defaults)
<!-- Record any default you adopt instead of asking, so the user can veto it at the gate. -->
<!-- assumption | adopted default | rationale | reversible? -->
- Storage engine | SQLite + SQLModel | local single-user, zero-config; research: Actual Budget uses SQLite same way | yes
- LLM client | openai-python SDK, base_url swap (Ollama :11434/v1 default) | one code path for local+cloud; research: OpenAI-compat is universal contract | yes
- Structured output | Instructor + Pydantic, mode=JSON + Ollama format/response_format (GBNF grammar) | research: grammar-constrained = 100% schema validity even on small models | yes
- Local runtime | Ollama assumed pre-installed; app detects or guides install | bundling inference is out of scope for a Streamlit app | yes
- Default local model | llama3.3:8b (16GB RAM) / phi-4-mini (8GB) configurable | research: 7-8B Q4 handles categorization/anomaly; 3-4B fast path | yes
- Redaction for cloud calls | regex + placeholder mapping table (no Presidio/spaCy) | Presidio deps too heavy for v1; mapping table is the community pattern | yes
- OFX/QFX import | OUT (CSV only) | user chose CSV+manual+paste; OFX adds parser surface | yes
- Budget model | category budgets with carryover flag (supports envelope + category styles) | research: one schema serves both philosophies | yes
- LLM math | LLM never computes totals; SQL/pandas computes, LLM interprets | research: hallucinated arithmetic is a known failure mode | yes

## Findings (cited - path:lines)
- Full research report: .omo/ulw-research/2026-07-29-budgeting-app-research/SYNTHESIS.md (35+ sources, 20 verified claims)
- Data model: Actual-Budget-style simple ledger, integer cents, imported_id dedup + fuzzy fallback (SYNTHESIS.md §2.1, lines 68-85)
- Budget model: category_budgets(category_id, month, allocated, carryover) serves envelope + category styles (§2.2, lines 87-105)
- Trend API shape: per-month {toBudget, totalIncome, totalSpent, fromLastMonth} + per-category {budgeted, spent, balance, carryover} (§2.3, lines 107-119)
- LLM-on-ledger dominant 2026 pattern: sidecar + deterministic totals + audit-tagged LLM writes + redaction before cloud (§4-5)
- Known failure modes: prompt-only JSON ~5% invalid; LLM arithmetic hallucination; context truncation on large transaction dumps -> aggregate before sending (librarian report bg_c5cad85e §3-4)
- Bank aggregation without paid APIs is effectively closed (GoCardless BAD shut mid-2025) -> CSV/manual/paste is the right call (SYNTHESIS.md §1.2)

## Decisions (with rationale)
- Platform: Streamlit local web app (user-chosen; matches installed developing-with-streamlit skill; fastest path for pandas+LLM)
- LLM strategy: hybrid, local-first (user-chosen; Ollama default, cloud opt-in per call with privacy gate)
- Data input: CSV import + manual entry + LLM tool-calling for copy-pasted budgets/statements (user-chosen, tool-calling addition is user's explicit request)
- Tests: TDD with pytest (user-chosen)
- Storage: SQLite via SQLModel/SQLAlchemy (default; local single-user, zero-config, reversible)
- LLM client: openai-python SDK pointed at Ollama's OpenAI-compatible endpoint + any cloud OpenAI-compatible API (default; one code path for all providers)

## Scope IN
- Local ledger with accounts/categories/transactions; income vs expense classification
- CSV import wizard with column mapping; manual CRUD; paste-text -> LLM tool-call -> structured transactions (user confirms before save)
- LLM auto-categorization of imported uncategorized transactions (dry-run default, low-confidence flagging, review queue, audit tag actor=llm:<provider>) [Metis A3 - explicit IN]
- Monthly budget targets per category; actual-vs-budget; month-over-month trends; carryover computed-on-read (never persisted mutable)
- Transfer pairs (atomic create, cascade delete, transferred_id link)
- Fuzzy dedup fallback (account+amount+date±7d+payee) with review queue [Metis A1]
- LLM analysis: budget evaluation, spending anomalies, recommendations; append-only recommendation store tracked over time (followed is user-set only)
- Provider settings UI (local endpoint + model, cloud key); privacy gate before any cloud call
- Streamlit UI: dashboard, transactions, budgets, import wizard, paste review, insights
- Alembic migrations from day 1; audit_log for ALL writes (actor: user|llm:<provider>|import:<source>)

## Scope OUT (Must NOT have)
- No bank aggregation / Plaid / automatic sync (incl. SimpleFIN, Enable Banking, Teller, Yapily)
- No multi-user, auth, or cloud hosting; single-user localhost (bind 127.0.0.1, refuse otherwise)
- No double-entry accounting; simple ledger
- No investment/portfolio tracking
- No automatic execution of LLM-suggested budget changes (suggestions only; user applies)
- No sending data to cloud without explicit per-call opt-in; cloud payload = category names + numeric aggregates ONLY (never raw transaction text)
- No recurring/schedule auto-detection (v2) [Metis creep 1]
- No MCP server / HTTP API for external agents; LLM accesses ledger in-process [Metis creep 2]
- No free-form chat interface; LLM invoked only via explicit user actions (evaluate, parse, categorize) [Metis creep 4]
- No PDF/XLS import; paste-parse accepts plain UTF-8 text only [Metis creep 6]
- No multi-currency; single currency, exactly 2 decimal places [Metis creep 3 + assumption 6]
- No Presidio/spaCy/transformers/langchain/chromadb deps (grep-enforced)

## Open questions
- None blocking. All four owner-decisions answered by user 2026-07-29; remaining items are announced defaults (veto-able at the gate).

## Metis gap analysis (2026-07-29, mandatory pre-todo review)
- Session: Metis subagent, ~20 findings (5 contradictions, 8 missing constraints, 6 creep risks, 8 assumptions, 17 ACs)
- Folded in: fuzzy dedup (A1), transfer pairs (A2), auto-categorize explicit IN (A3), carryover-on-read (A4), wave reorder provider-before-LLM (A5), Ollama degradation contract (B1), Alembic (B2), cents-parse safety (B3), naive-date invariant (B4), WAL+concurrency (B5), 4K token cap (B6), audit_log all writes (B7), append-only recommendations (B8), cloud-aggregates-only payload (assumption 3 fix), LLM eval quality spike (assumption 4), Streamlit perf spike (assumption 5), VCR cassettes + integration marks (assumption 7), localhost bind invariant (assumption 8), scope-OUT additions (creep 1-4, 6)
- Status: all findings folded into plan todos/scope; none deferred silently

## Approval gate
status: approved 2026-07-29 (user: "I approve let's implement" - approval authorizes plan writing ONLY; execution belongs to a worker session the user starts)
pending action: write .omo/plans/local-ledger.md (scaffold + Metis + append todos)
approach: Streamlit + SQLModel/SQLite ledger; OpenAI-compatible LLM layer (Ollama default, cloud opt-in); Instructor/Pydantic structured output; CSV+manual+LLM-paste ingest; category budgets w/ carryover; deterministic totals + LLM interpretation; TDD pytest.
<!-- When exploration is exhausted and unknowns are answered, set status: awaiting-approval. -->
<!-- That durable record is the loop guard: on a later turn read it and resume at the gate instead of re-running exploration. -->

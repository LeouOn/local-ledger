# local-ledger - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->
<!-- Plain English for a non-engineer: NO file paths, NO todo numbers, NO wave/agent/tool names. -->

**What you'll get:** A private budgeting app that runs on your own computer and uses a local language model (with a cloud option behind an explicit consent dialog) to read your income and spending, score your budget against monthly targets, flag anomalies, and propose changes — while remembering its own suggestions so you can see whether they actually helped over time.

**Why this approach:** Local-first because the data is your full financial life — so the LLM works on your machine by default (Ollama), the cloud is gated behind a per-call consent preview, and the only thing that ever leaves the machine is category names plus numeric totals (never raw transactions, payees, or account numbers). Structured output is grammar-constrained and arithmetic is done in SQL, so the model can't corrupt your numbers or produce invalid shapes.

**What it will NOT do:** Connect to your bank (no Plaid/sync); auto-apply its own suggestions; do double-entry accounting; handle multiple currencies; sync across devices; send anything to the cloud without your explicit per-call click; or do recurring-transaction auto-detection in this version. It also won't help with investments or portfolios.

**Effort:** Large (~30 distinct todos across 6 waves, TDD throughout).
**Risk:** Medium — the biggest unknown is whether a 7-8B local model is good enough at budget evaluation; this is gated by a real-Ollama quality spike that picks the default provider (local vs. cloud-consent) based on a rubric and can be re-run anytime.
**Decisions to sanity-check:** (1) auto-categorization of imported transactions is IN scope (dry-run default, review queue, audit tags) because that's the single highest-value LLM feature; (2) cloud payload is restricted to category names + numeric totals — no raw transaction text ever leaves; (3) single currency, 2-decimal places; (4) recurring/schedule auto-detection deferred to v2; (5) no in-app chat — LLM only runs on explicit buttons.

Your next move: When you're ready, start execution in a worker session (e.g. `/start-work`) — that loads the plan, picks a worktree, and runs the 30 todos with evidence. Full execution detail follows below.

---

> TL;DR (machine): Large effort, Medium risk; 30 todos, 6 waves; Streamlit + SQLModel/SQLite + Alembic ledger; OpenAI-compatible LLM layer (Ollama default, cloud opt-in aggregates-only); Instructor + Pydantic structured output with token-capped aggregates-only payloads; TDD pytest; localhost-only bind.

## Scope
### Must have
1. **Ledger (SQLite + SQLModel + Alembic):** accounts, payees, categories + groups, transactions (integer cents, naive `YYYY-MM-DD` dates, `imported_id`, `transferred_id`), transfer pairs (atomic create, cascade delete), category budgets with carryover **computed on read** (never persisted mutable), month summaries + month-over-month deltas computed in SQL/pandas only.
2. **Dedup:** `imported_id` exact match first; fuzzy fallback on (account, amount, date ±7d, normalized payee) routed to a review queue — never auto-merged, never silently duplicated.
3. **Ingest:** CSV import wizard (column mapping persisted per account, ambiguous amount formats REJECTED not guessed), manual entry/edit, paste-text → LLM tool-call → structured transactions shown in an editable review grid → user confirms → single atomic commit.
4. **LLM layer:** one OpenAI-compatible client (`openai` SDK, `base_url` swap; Ollama `http://localhost:11434/v1` default; cloud providers opt-in only), Instructor + Pydantic structured output with a manual-JSON-parse fallback, VCR cassettes for tests, and a 4-state Ollama degradation contract (not-installed / not-running / model-missing / ready) consumed by every LLM feature.
5. **Auto-categorization:** LLM proposes categories for imported uncategorized transactions; dry-run default; low-confidence rows flagged to a review queue; every LLM write audit-tagged `actor=llm:<provider>:<model>`.
6. **Analysis:** deterministic budget evaluation + anomaly detection runs with the LLM patched to raise (proves no LLM in arithmetic path); LLM interpretation invoked only by explicit user action; prompts carry **aggregates only** and are hard-capped at ≤4096 tokens (tiktoken-verified); recommendations are **append-only** (timestamp + provider + prompt-hash) and `followed` is user-set only; month-over-month follow-up evaluation ("you were advised X; actual was Y").
7. **Privacy gate:** local-only default; every cloud call requires per-call opt-in; cloud payload = category names + numeric aggregates ONLY (no raw transaction text, no payees, no account numbers); placeholder substitution for any residual free text; user previews exact payload before send; every egress written to local audit log.
8. **UI (Streamlit):** dashboard (trends, budget-vs-actual), transactions (1000-row capable, filters, CRUD, transfer form, dedup + categorization review queues), budgets (allocations + carryover), import wizard + paste review, insights (evaluate button, recommendation history/diff, followed toggles), provider settings + consent dialog + Ollama-status banner. Bound to 127.0.0.1 only.
9. **TDD:** pytest-first for every unit; agent-executed QA per todo with evidence files.

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No bank aggregation / Plaid / SimpleFIN / Enable Banking / Teller / any sync.
- No multi-user, auth, or hosting; never bind non-localhost (startup refuses `0.0.0.0`).
- No double-entry accounting, no investments/portfolio tracking.
- No recurring/schedule auto-detection (v2). No MCP server or HTTP API — ledger access is in-process only.
- No free-form chat UI; LLM runs only on explicit user actions (evaluate / parse / categorize).
- No PDF/XLS/XLSX import; paste-parse is plain UTF-8 text only.
- No multi-currency: single currency, exactly 2 decimal places.
- No auto-applying LLM suggestions; no LLM-set `followed` flags; no LLM arithmetic.
- No raw transaction text to cloud, ever — aggregates only.
- Forbidden deps (grep-enforced in todo 29): `ofxparse`, `plaid-python`, `simplefin`, `yapily`, `presidio`, `spacy`, `transformers`, `langchain`, `chromadb`, `torch`.

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: **TDD** + pytest (unit under `tests/unit/`, real-Ollama tests marked `@pytest.mark.integration` skipped by default, VCR cassettes under `tests/cassettes/` for LLM happy-path replay). Run: `python -m pytest tests/unit -q` (fast gate) and `python -m pytest -q` (full).
- Evidence: `.omo/evidence/task-<N>-local-ledger.<ext>` (outside ulw-loop); each todo's QA writes its command output there.
- LLM QA: unit tests replay cassettes; a pre-release integration pass against real Ollama is a distinct todo (19) with a rubric, not a mock.

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave. Fewer than 3 (except the final) means you under-split.

- **Wave 1 — foundation (7):** 1 scaffold+git → 2 test infra, 3 models+Alembic → 4 ledger service, 5 dedup, 6 trends/carryover, 7 Ollama degradation contract. (1 first; 2,3 parallel after 1; 4-7 parallel after 3.)
- **Wave 2 — LLM plumbing + parsing primitives (4):** 8 provider abstraction (after 3,7) → 9 structured output, 10 privacy gate parallel; 11 amount/date parsing parallel with everything.
- **Wave 3 — ingest + payloads (5):** 12 CSV parser (after 11) → 13 import service (after 4,5,12); 14 paste-parse (after 9,11), 15 auto-categorize (after 9,13), 16 analysis payload builder (after 6,8) — 14,15,16 parallel.
- **Wave 4 — analysis (4):** 17 evaluate service (after 9,10,16) → 18 recommendation store; 19 LLM quality spike (after 16), 26 concurrency stress (after 4) — parallel with 17/18.
- **Wave 5 — UI (6):** 20 app shell+settings+consent (after 7) → 21 dashboard, 22 transactions+review queues, 23 budgets, 24 import wizard+paste review, 25 insights — parallel after their service deps.
- **Wave 6 — hardening (4):** 27 perf validation, 29 security/dependency invariants (parallel) → 28 end-to-end QA → 30 README final.

Executor note: load the `developing-with-streamlit` skill before any Wave-5 todo.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 | - | 2-30 | - |
| 2 | 1 | 3-30 (test conventions) | 3 |
| 3 | 1,2 | 4,5,6,8 | 2 |
| 4 | 3 | 13,22,26 | 5,6,7 |
| 5 | 3 | 13 | 4,6,7 |
| 6 | 3 | 16,17,21,23 | 4,5,7 |
| 7 | 1 | 8,9,20 | 4,5,6 |
| 8 | 3,7 | 9,10,14,15,16,17 | 11 |
| 9 | 8 | 14,15,17 | 10,11 |
| 10 | 8 | 17,25 | 9,11 |
| 11 | 1,2 | 12,14 | 8,9,10 |
| 12 | 11 | 13,24 | 14,16 |
| 13 | 4,5,12 | 15,24 | 14,16 |
| 14 | 4,9,11 | 24 | 13,15,16 |
| 15 | 9,13 | 22 | 14,16 |
| 16 | 6,8 | 17,19,27 | 13,14,15 |
| 17 | 9,10,16 | 18,25 | 19,26 |
| 18 | 17 | 25 | 19,26 |
| 19 | 16 | 25 (default config) | 17,18,26 |
| 20 | 7 | 21-25 | - |
| 21 | 6,20 | 27,28 | 22,23,24,25 |
| 22 | 4,15,20 | 27,28 | 21,23,24,25 |
| 23 | 6,20 | 28 | 21,22,24,25 |
| 24 | 13,14,20 | 28 | 21,22,23,25 |
| 25 | 17,18,19,20 | 28 | 21,22,23,24 |
| 26 | 4 | - | 17,18,19 |
| 27 | 16,21,22 | 28 | 29 |
| 28 | 21-25,27 | 30 | 29 |
| 29 | 1-25 | - | 27,28 |
| 30 | 28 | - | - |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->
- [ ] 1. Project scaffold + git init + localhost-only config
  What to do / Must NOT do: `git init` in C:\Users\llama\OneDrive\proj\local-ledger; create pyproject.toml (project `local-ledger`, requires-python >=3.11, src layout `src/local_ledger/`; deps: streamlit, sqlmodel, sqlalchemy, alembic, pandas, openai, instructor, pydantic>=2, tiktoken; dev: pytest, vcrpy, ruff); `.streamlit/config.toml` with `server.address="127.0.0.1"`, `server.headless=true`; `.gitignore` (data/, .omo/evidence/, .venv/, __pycache__); `app.py` stub containing a startup guard that raises SystemExit if the effective bind address is not 127.0.0.1/localhost; README.md skeleton; ruff config. Must NOT: install any forbidden dep (see Must NOT have); no business logic.
  Parallelization: Wave 1 | Blocked by: - | Blocks: 2-30
  References: plan Scope sections above; .omo/drafts/local-ledger.md (Decisions); research: .omo/ulw-research/2026-07-29-budgeting-app-research/SYNTHESIS.md
  Acceptance criteria (agent-executable): `git log --oneline` non-empty; `pip install -e ".[dev]"` succeeds; `python -c "import local_ledger"` exit 0; `ruff check src` clean; `grep -n "127.0.0.1" .streamlit/config.toml` matches.
  QA scenarios: happy: fresh venv install + import smoke, output to .omo/evidence/task-1-local-ledger.txt. failure: `streamlit run app.py --server.address 0.0.0.0` → SystemExit with refusal message, captured in same evidence file.
  Commit: Y | chore(scaffold): project skeleton, tooling, localhost-only streamlit config

- [ ] 2. Test infrastructure (fixtures, factories, VCR, network isolation)
  What to do / Must NOT do: tests/conftest.py (tmp-path SQLite engine + session fixtures; socket-blocking fixture autouse for tests/unit); tests/factories.py (make_account/make_category/make_payee/make_transaction with integer cents and YYYY-MM-DD strings); pytest.ini options: markers `integration`, addopts `-m "not integration"`; vcrpy config with cassette_library_dir=tests/cassettes. Must NOT: any network access from unit tests.
  Parallelization: Wave 1 | Blocked by: 1 | Blocks: 3-30
  References: pyproject.toml (todo 1); vcrpy docs https://vcrpy.readthedocs.io/
  Acceptance criteria: `python -m pytest tests/unit -q` ≥3 passing sanity tests; a deliberately network-attempting unit test FAILS (proves isolation); `python -m pytest -q --collect-only` deselects integration-marked tests by default.
  QA scenarios: happy: factory-built entity persists via session fixture (evidence task-2). failure: unit test opening a socket raises the isolation error (evidence task-2).
  Commit: Y | test(infra): fixtures, factories, VCR cassettes, network isolation

- [ ] 3. Domain models + Alembic initial migration
  What to do / Must NOT do: src/local_ledger/models.py (SQLModel): Account(id,name,type,created_at); Payee(id,name,normalized_name); CategoryGroup(id,name); Category(id,group_id,name,is_income); Transaction(id,account_id FK,date:str,amount_cents:int,payee_id,category_id NULL,notes,imported_id NULL,transferred_id NULL,is_reconciled,created_by:str,created_at) with CHECK amount_cents<>0 and date-format validator; CategoryBudget(id,category_id,month:str 'YYYY-MM',allocated_cents:int,carryover_enabled:bool); Recommendation(id,created_at,provider,model,prompt_hash,month,body,followed:bool NULL); AuditLog(id,entity_type,entity_id,action,actor,before_json,after_json,created_at); ProviderSettings(single row: local_base_url,local_model,cloud_base_url,cloud_model,cloud_api_key_env,evaluation_default_provider); ImportPrefs(account_id,mapping_json); StagedTransaction(id,import_batch,account_id,date,amount_cents,payee_raw,suggested_category_id,confidence,dedup_status,review_state). alembic init + autogenerate initial migration; db.py `init_db()` runs `alembic upgrade head`. Must NOT: any float money field; any mutable carryover column; datetime.now() in business fields.
  Parallelization: Wave 1 | Blocked by: 1,2 | Blocks: 4,5,6,8
  References: SYNTHESIS.md §5.2 schema sketch; drafts decisions (integer cents, naive dates)
  Acceptance criteria: `alembic upgrade head` on empty DB creates all tables; `alembic downgrade base` clean; unit: float amount raises ValidationError; `date="2026-13-45"` raises; `amount_cents=0` raises IntegrityError.
  QA scenarios: happy: factory entity graph persists (task-3-happy.txt). failure: amount_cents=12.5 → typed validation error (task-3-failure.txt).
  Commit: Y | feat(ledger): domain models + initial alembic migration

- [ ] 4. Ledger service layer + transfer pairs + audit + WAL write path
  What to do / Must NOT do: src/local_ledger/db.py: engine with `PRAGMA journal_mode=WAL`; write helper with retry on "database is locked" (backoff 50/100/200ms, then raise). src/local_ledger/ledger/service.py: CRUD for accounts/payees/categories/transactions; `create_transfer(from_id,to_id,amount_cents,date)` inserts BOTH rows atomically with cross-set transferred_id; `delete_transaction` cascades the pair; EVERY mutation writes one AuditLog row (actor param, default "user", before/after JSON). src/local_ledger/ledger/audit.py helper. Must NOT: any LLM import in ledger/ (grep-enforced); any session.write bypassing audit.
  Parallelization: Wave 1 | Blocked by: 3 | Blocks: 13,22,26
  References: models.py (todo 3); SYNTHESIS.md §2.1 transferred_id pattern; Metis A2/B5
  Acceptance criteria: unit: transfer pair atomic (inject failure between inserts → 0 rows); delete cascades; each mutation yields exactly 1 audit row; `grep -riE "openai|ollama|instructor" src/local_ledger/ledger/` → 0 hits.
  QA scenarios: happy: create/edit/delete round-trip with complete audit trail (task-4-happy.txt). failure: hold a write lock in thread A, write from thread B → bounded retries then typed error, zero corruption (task-4-failure.txt).
  Commit: Y | feat(ledger): service layer, atomic transfer pairs, audit log, WAL writes

- [ ] 5. Dedup engine (imported_id + fuzzy fallback + review queue)
  What to do / Must NOT do: src/local_ledger/ledger/dedup.py: classify(candidate_txn, account_id) → EXACT (same imported_id → skip), FUZZY (same account, equal abs amount_cents, date within ±N days [N configurable, default 7], equal normalized payee → review queue), NEW. normalize_payee: lowercase, strip punctuation/whitespace. Fuzzy NEVER auto-merges. Must NOT: auto-delete or auto-merge anything.
  Parallelization: Wave 1 | Blocked by: 3 | Blocks: 13
  References: SYNTHESIS.md §1.1/§2.1 (Actual ±7d fuzzy pattern); Metis A1/AC1
  Acceptance criteria: deterministic fixture of 100 known overlapping pairs → ≥95% routed FUZZY, 0 auto-merged; same imported_id → EXACT skip without queue entry.
  QA scenarios: happy: same CSV staged twice → second pass all EXACT (task-5-happy.txt). failure: same payee/date, amount off by 1 cent → classified NEW, not FUZZY (task-5-failure.txt).
  Commit: Y | feat(ingest): dedup engine with fuzzy review queue

- [ ] 6. Trends + carryover engine (deterministic, on-read)
  What to do / Must NOT do: src/local_ledger/ledger/trends.py: `month_summary(month)` → income_cents, spent_cents, budgeted_cents, per-category {budgeted,spent,balance,carryover}; `mom_deltas(month,n)`; carryover computed ON READ by chaining (allocated − spent) over prior months for carryover_enabled categories; SQL/pandas integer ops only; naive YYYY-MM-DD string comparisons only. Must NOT: persist carryover; any LLM call; datetime.now() (accept `today` injectable).
  Parallelization: Wave 1 | Blocked by: 3 | Blocks: 16,17,21,23
  References: SYNTHESIS.md §2.2/§2.3/§3.3; Metis A4/AC9/B4
  Acceptance criteria: seeded 3-month ledger → carryover chain equals (alloc−spent) sums; back-dated insert in month M−2 changes month M carryover on next read (proves on-read); `grep -riE "openai|ollama|instructor" src/local_ledger/ledger/trends.py` → 0.
  QA scenarios: happy: fixture ledger → expected summary numbers (task-6-happy.txt). failure: month with no budget rows → carryover=0, no exception (task-6-failure.txt).
  Commit: Y | feat(analysis): month summaries, MoM deltas, on-read carryover

- [ ] 7. Ollama degradation contract
  What to do / Must NOT do: src/local_ledger/llm/ollama.py: `detect_state(timeout=1.0)` → enum NOT_INSTALLED / NOT_RUNNING / MODEL_MISSING / READY (probe binary/port 11434/models list); `LLMUnavailable(state, guidance)` typed exception; DEGRADED_BEHAVIOR mapping table {state → per-feature message} consumed later by UI/services. Must NOT: raise raw ConnectionError to callers; block >1s on probe.
  Parallelization: Wave 1 | Blocked by: 1 | Blocks: 8,9,20
  References: drafts decisions (Ollama assumed pre-installed); Metis B1/AC5; Ollama API docs https://github.com/ollama/ollama/blob/main/docs/api.md
  Acceptance criteria: unit with monkeypatched socket → all 4 states detected; guidance strings non-empty for each state; probe completes ≤1s when port dead.
  QA scenarios: happy: mocked server+model → READY (task-7-happy.txt). failure: connection refused → NOT_RUNNING with "run `ollama serve`" guidance (task-7-failure.txt).
  Commit: Y | feat(llm): ollama state detection + degradation contract

- [ ] 8. Provider abstraction (OpenAI-compatible, consent-gated cloud)
  What to do / Must NOT do: src/local_ledger/llm/providers.py: `get_local_client()` → openai.OpenAI(base_url=settings.local_base_url, api_key="ollama"), pre-checks detect_state (raises LLMUnavailable); `get_cloud_client(consent_token)` → raises CloudCallBlocked unconditionally in this todo (consent issuance lands in todo 10); settings CRUD for ProviderSettings; `list_local_models()`. Must NOT: hardcode URLs/keys; read cloud key from env var named in settings, never store key in DB.
  Parallelization: Wave 2 | Blocked by: 3,7 | Blocks: 9,10,14,15,16,17
  References: ollama OpenAI-compat https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx; librarian report (OpenAI-compat universal contract)
  Acceptance criteria: unit (no network): factory builds correct base_url from settings; `get_cloud_client(None)` → CloudCallBlocked; local probe failure → LLMUnavailable (not ConnectionError).
  QA scenarios: happy: mock server chat completion via cassette (task-8-happy.txt + cassette). failure: cloud without consent token → CloudCallBlocked (task-8-failure.txt).
  Commit: Y | feat(llm): openai-compatible provider abstraction + settings

- [ ] 9. Structured output helpers + Ollama Instructor spike
  What to do / Must NOT do: src/local_ledger/llm/structured.py: `extract(schema, messages, provider)` via instructor.from_openai(mode=instructor.Mode.JSON), max_retries=2; fallback path plain completion + json.loads + pydantic validate; typed StructuredOutputError after exhaustion. SPIKE (integration-marked): real Ollama + llama3.3:8b, 3-field schema, 10 sequential calls; record failures/retries/verdict to evidence; if Instructor path unreliable, make fallback the default and record decision in the evidence file.
  Parallelization: Wave 2 | Blocked by: 8 | Blocks: 14,15,17
  References: Ollama structured outputs https://docs.ollama.com/capabilities/structured-outputs; Instructor https://github.com/567-Labs/instructor; SYNTHESIS.md §4.5 #1 (native Ollama JSON bug → use OpenAI-compat endpoint); Metis assumption 1
  Acceptance criteria: unit via cassette: valid replay → typed object; garbage-JSON cassette → ≤2 retries → StructuredOutputError, never raw JSONDecodeError; evidence file .omo/evidence/task-9-local-ledger.md exists with spike verdict (or SKIP-deferred note if Ollama absent).
  QA scenarios: happy: cassette typed success (task-9-happy.txt). failure: garbage-JSON cassette → typed error after bounded retries (task-9-failure.txt).
  Commit: Y | feat(llm): instructor structured outputs + fallback + ollama spike

- [ ] 10. Privacy gate (consent, aggregates-only cloud payload, redaction, egress audit)
  What to do / Must NOT do: src/local_ledger/llm/privacy.py: `grant_consent(provider, payload_preview)` → single-use session token; `assert_consent(token)`; wire into providers.get_cloud_client (replaces todo-8 stub); `build_cloud_payload(aggregates)` accepting ONLY the typed aggregates object (payees/dates/accounts structurally impossible); placeholder mapping for any residual free text + regex pass (email/phone/card/IBAN/SSN); `preview_payload()` returns exact outbound dict; every cloud send writes AuditLog(actor="llm:cloud:<provider>", after_json=payload). Must NOT: ever include raw transactions, payee names, account identifiers, or per-transaction dates in a cloud payload.
  Parallelization: Wave 2 | Blocked by: 8 | Blocks: 17,25
  References: librarian privacy report (redact early, placeholder > deletion, show-what-leaves UX); drafts decisions (aggregates-only); Metis AC3/AC4/assumption 3
  Acceptance criteria: unit: builder given a raw Transaction object → PrivacyViolation type error; no consent → CloudCallBlocked; consent → proceeds + 1 egress audit row; regex matrix (email/phone/SSN/IBAN/card) tokenized in residual text.
  QA scenarios: happy: consent→send→audit row present (task-10-happy.txt). failure: send attempt without consent → blocked + 0 audit rows (task-10-failure.txt).
  Commit: Y | feat(privacy): consent gate, aggregates-only payload, redaction, egress audit

- [ ] 11. Strict amount/date parsing primitives
  What to do / Must NOT do: src/local_ledger/ingest/amounts.py: `parse_amount_to_cents(s, locale: "us"|"eu", cents_mode=False)`; `detect_format(samples)` → suggested locale or None (ambiguous → caller must ask user); `parse_date(s, fmt)` → normalized "YYYY-MM-DD" from an explicit allowed-format list. Must NOT: silently coerce ambiguous formats; never return 0 for empty string.
  Parallelization: Wave 2 | Blocked by: 1,2 | Blocks: 12,14
  References: Metis B3/AC11 (exact test matrix)
  Acceptance criteria: matrix all green: "123.45"(us)→12345; "1,234.56"(us)→123456; "-123.45"(us)→-12345; "1.234,56"(eu)→123456; "1.234,56"(us)→AmbiguousAmountError; "12345"→error unless cents_mode=True; ""→error (not 0); detect_format on mixed column → None.
  QA scenarios: happy: full matrix (task-11-happy.txt). failure: EU string under us locale → error, not silently wrong value (task-11-failure.txt).
  Commit: Y | feat(ingest): strict amount/date parsing primitives

- [ ] 12. CSV parser + column mapping + per-account prefs
  What to do / Must NOT do: src/local_ledger/ingest/csv_import.py: csv.Sniffer dialect detection; header row detection; parse with mapping {date, amount OR debit/credit pair, payee, notes} using todo-11 primitives; row-level error collection (row number + reason; bad rows never silently dropped, never block good rows from staging); ImportPrefs save/auto-suggest per account. Fixtures: tests/fixtures/bank_a_us.csv, bank_b_eu.csv, bank_c_debit_credit.csv (create with realistic columns).
  Parallelization: Wave 3 | Blocked by: 11 | Blocks: 13,24
  References: SYNTHESIS.md §1.3 (Actual import UX); amounts.py (todo 11); Metis AC10
  Acceptance criteria: 3 fixtures parse 100% of valid rows; second import for same account auto-suggests prior mapping (assert suggested == saved); a CSV with 1 bad row → 1 error entry with line number, others parsed.
  QA scenarios: happy: 3 fixtures end-to-parse (task-12-happy.txt). failure: bad-amount row isolated with line number, nothing committed (task-12-failure.txt).
  Commit: Y | feat(ingest): csv parser, column mapping, per-account prefs

- [ ] 13. Import service (stage → dedup → confirm → atomic commit)
  What to do / Must NOT do: src/local_ledger/ingest/importer.py: `stage(parsed_rows, account_id, batch_label)` → StagedTransaction rows classified via dedup (todo 5); `commit(staged_ids, actor=f"import:{batch_label}")` → single DB transaction writing Transactions + AuditLog rows; rollback → zero partial rows; FUZZY rows excluded from commit until review-state resolved via service call.
  Parallelization: Wave 3 | Blocked by: 4,5,12 | Blocks: 15,24
  References: service.py (todo 4); dedup.py (todo 5); Metis AC1
  Acceptance criteria: failure injected at row 40/50 → 0 committed + staged rows intact; re-import same file → 0 new transactions; fuzzy candidates appear in review queue, not in ledger.
  QA scenarios: happy: 50-row fixture → 50 committed + 50 audit rows (task-13-happy.txt). failure: mid-commit exception → 0 rows, DB unchanged (task-13-failure.txt).
  Commit: Y | feat(ingest): stage-confirm-commit import pipeline

- [ ] 14. Paste-parse service (LLM tool-calling, confirm-before-commit)
  What to do / Must NOT do: src/local_ledger/llm/paste_parse.py: `parse_pasted_text(text, provider)` → ParsedTransactionBatch via structured.extract (todo 9); schema fields: date, payee, amount_cents, suggested_category, confidence; input >20K chars → chunked by line groups; NEVER writes to ledger; `commit_proposals(accepted, actor="user")` reuses importer.commit path (todo 13) atomically. Must NOT: commit without explicit accepted list; accept PDF/binary (plain str only).
  Parallelization: Wave 3 | Blocked by: 4,9,11 | Blocks: 24
  References: structured.py (todo 9); importer.py (todo 13); user's paste feature request; Metis AC6
  Acceptance criteria: cassette: 10-transaction pasted statement → ≤10 proposals, all fields populated, `SELECT count(*) FROM transactions` unchanged by parse; confirm 7 → exactly 7 committed, 1 DB transaction.
  QA scenarios: happy: cassette parse → subset confirm (task-14-happy.txt). failure: LLM returns 0 valid rows → typed ParseFailure, 0 writes (task-14-failure.txt).
  Commit: Y | feat(llm): paste-to-transactions parse + confirm-before-commit

- [ ] 15. Auto-categorization service (dry-run default, confidence review queue)
  What to do / Must NOT do: src/local_ledger/llm/categorize.py: `propose_categories(account_id, month, dry_run=True)` → proposals {transaction_id, category_id, confidence} via structured.extract with context = category list + ≤20 labeled (payee→category) examples from the local ledger; confidence <0.7 → review queue; `apply_proposals(accepted)` writes with actor `llm:<provider>:<model>` audit rows; batches ≤50. Must NOT: create new categories silently (unknown category name → proposal rejected to queue); write in dry-run.
  Parallelization: Wave 3 | Blocked by: 9,13 | Blocks: 22
  References: SYNTHESIS.md §4.1/§4.4/§4.5 (actual-ai pattern: dry-run default, low-confidence flag, audit tags); Metis A3
  Acceptance criteria: cassette: 20 uncategorized → proposals with confidences; dry-run → 0 category writes; apply → audit actor startswith "llm:"; unknown-category proposal → queued, no category created.
  QA scenarios: happy: dry-run then apply accepted subset (task-15-happy.txt). failure: LLM emits unknown category → review queue, ledger unchanged (task-15-failure.txt).
  Commit: Y | feat(llm): auto-categorization with dry-run + review queue

- [ ] 16. Analysis payload builder (aggregates only, ≤4096 tokens)
  What to do / Must NOT do: src/local_ledger/analysis/payloads.py: `build_evaluation_payload(month, n_months=6)` → typed Aggregates object from trends.py: per-category {budgeted,spent,balance,carryover}, income, MoM deltas, top movers; `to_prompt(payload)`; tiktoken count hard-assert ≤4096; same builder feeds local path and (via privacy.build_cloud_payload) cloud path. Must NOT: include raw transactions/payees/accounts; bypass the typed object.
  Parallelization: Wave 3 | Blocked by: 6,8 | Blocks: 17,19,27
  References: trends.py (todo 6); privacy.py (todo 10); Metis B6/AC14
  Acceptance criteria: seeded 10K-tx/36-month ledger → tiktoken(prompt) ≤4096; unit asserts zero payee/account strings present in prompt; builder rejects a raw Transaction list with TypeError.
  QA scenarios: happy: small ledger → schema-valid payload (task-16-happy.txt). failure: monkeypatched trends returning raw rows → builder raises (task-16-failure.txt).
  Commit: Y | feat(analysis): aggregates-only payload builder with token cap

- [ ] 17. Evaluate service (deterministic first, LLM interpretation on demand)
  What to do / Must NOT do: src/local_ledger/analysis/evaluate.py: `deterministic_evaluate(month)` → anomalies: overspend >110% of budget, category drift >50% MoM, single transaction >3σ vs category mean (pandas); `llm_interpret(payload, provider, consent_token=None)` → typed BudgetEvaluation {summary, key_findings[], suggested_actions[]} via structured.extract. Must NOT: any LLM call inside deterministic paths; any arithmetic inside llm_interpret.
  Parallelization: Wave 4 | Blocked by: 9,10,16 | Blocks: 18,25
  References: payloads.py (todo 16); Metis AC2/AC7
  Acceptance criteria: deterministic_evaluate passes with the LLM client patched to raise on ANY call; cassette: llm_interpret → typed BudgetEvaluation; cloud path without consent → CloudCallBlocked.
  QA scenarios: happy: seeded ledger → exact expected anomaly list (task-17-happy.txt). failure: LLMUnavailable → deterministic result still returned, interpretation section typed-degraded (task-17-failure.txt).
  Commit: Y | feat(analysis): deterministic evaluation + on-demand llm interpretation

- [ ] 18. Recommendation store (append-only) + follow-up evaluation
  What to do / Must NOT do: src/local_ledger/analysis/recommendations.py: `save(evaluation, provider, model, prompt_hash, month)` insert-only; `set_followed(id, bool)` (only mutator, user actor audit); `followup_report(current_month)` → per prior suggestion: restated advice + measured actual delta from trends.py; `diff(month_a, month_b)`. Must NOT: any update path for body; any LLM-callable path to set_followed.
  Parallelization: Wave 4 | Blocked by: 17 | Blocks: 25
  References: recommendations model (todo 3); trends.py (todo 6); Metis B8/AC8
  Acceptance criteria: generate month M and M+1 → both rows, neither mutated; followed defaults NULL; service module exposes no body-update function (inspect-based test); followup numbers equal trends.py output exactly.
  QA scenarios: happy: M → M+1 → followup adherence math correct (task-18-happy.txt). failure: attempt LLM-actor set_followed via service → no such code path; direct attempt blocked by actor guard (task-18-failure.txt).
  Commit: Y | feat(analysis): append-only recommendations + follow-up evaluation

- [ ] 19. LLM evaluation-quality spike (integration, sets provider default)
  What to do / Must NOT do: tests/integration/test_eval_quality.py (marked integration): requires real Ollama + llama3.3:8b; 3 seeded sample ledgers (fixtures); run llm_interpret; score against tests/integration/rubric.md (specificity, correctness vs deterministic numbers, actionability); write verdict + scores to evidence; if pass-rate <70% → ProviderSettings.evaluation_default_provider = "cloud-consent" else "local". Must NOT: run in default pytest; silently skip without a SKIP verdict in evidence.
  Parallelization: Wave 4 | Blocked by: 16 | Blocks: 25 (default config)
  References: rubric (create in this todo); Metis assumption 4; librarian model-size guidance
  Acceptance criteria: .omo/evidence/task-19-local-ledger.md contains verdict + per-ledger rubric scores (or explicit Ollama-absent SKIP-deferred note); settings default matches verdict.
  QA scenarios: happy: rubric scored, verdict recorded. failure: Ollama absent → evidence file records SKIP-deferred (not a silent pass). Evidence: task-19-local-ledger.md.
  Commit: Y | test(analysis): llm evaluation quality spike + provider default verdict

- [ ] 20. Streamlit app shell + settings page + consent dialog (LOAD developing-with-streamlit SKILL FIRST)
  What to do / Must NOT do: app.py multipage entry; startup: bind-address guard (todo 1), init_db(), Ollama state probe; shared ui/chrome.py rendering the degradation banner per todo-7 mapping on every page; pages/6_Settings.py: provider form (local url/model + list_local_models picker, cloud url/model/key-env-name), save→ProviderSettings; ui/consent.py dialog component: shows exact payload preview + provider, grants single-use token only on explicit click.
  Parallelization: Wave 5 | Blocked by: 7 | Blocks: 21-25
  References: ollama.py (todo 7); providers.py (todo 8); privacy.py (todo 10); streamlit AppTest docs https://docs.streamlit.io/develop/api-reference/app-testing
  Acceptance criteria: AppTest: boot with Ollama down → banner rendered, no exception, <3s; settings form round-trips; consent dialog yields token only after click (not on render).
  QA scenarios: happy: AppTest full boot + settings round-trip (task-20-happy.txt). failure: AppTest with bind patched to 0.0.0.0 → SystemExit (task-20-failure.txt).
  Commit: Y | feat(ui): app shell, ollama banner, settings, consent dialog

- [ ] 21. Dashboard page (deterministic only)
  What to do / Must NOT do: pages/1_Dashboard.py: month selector; KPI cards (income/spent/budgeted/net); per-category budget-vs-actual bar chart; 6-month trend line; anomaly cards from deterministic_evaluate. Must NOT: any LLM call/import on this page.
  Parallelization: Wave 5 | Blocked by: 6,20 | Blocks: 27,28
  References: trends.py (todo 6); evaluate.py (todo 17); developing-with-streamlit skill (charts/caching patterns)
  Acceptance criteria: AppTest with 1000-tx fixture DB: renders without exception; `grep -iE "llm|openai|ollama" pages/1_Dashboard.py` → 0 hits; empty-DB run → empty-state, no exception.
  QA scenarios: happy: seeded render (task-21-happy.txt). failure: empty DB → graceful empty state (task-21-failure.txt).
  Commit: Y | feat(ui): dashboard with deterministic trends + anomalies

- [ ] 22. Transactions page + review queues + transfer form
  What to do / Must NOT do: pages/2_Transactions.py: st.dataframe (NOT st.table) with filters (month/category/account/uncategorized-only) + 100-row pagination; manual add/edit form (validated via todo-11 primitives); transfer form → service.create_transfer; dedup review queue UI (accept/reject FUZZY staged rows); categorization review queue UI (accept/edit/reject proposals). Must NOT: load full table unpaginated; bypass service layer.
  Parallelization: Wave 5 | Blocked by: 4,15,20 | Blocks: 27,28
  References: service.py (todo 4); categorize.py (todo 15); importer.py (todo 13)
  Acceptance criteria: AppTest: 1000-tx fixture → page runs, filter applies (row count changes), transfer form creates linked pair (service-level assert), queue accept writes audit row.
  QA scenarios: happy: CRUD + transfer + queue accept round-trip (task-22-happy.txt). failure: amount input "abc" → inline validation error, 0 writes (task-22-failure.txt).
  Commit: Y | feat(ui): transactions, transfer form, dedup + categorization queues

- [ ] 23. Budgets page (allocations + carryover)
  What to do / Must NOT do: pages/3_Budgets.py: st.data_editor for per-category monthly allocations (batch save in one DB transaction); per-category carryover_enabled toggle; balance column (allocated − spent + carryover) rendered from trends.py. Must NOT: allow negative allocations; write carryover as stored value.
  Parallelization: Wave 5 | Blocked by: 6,20 | Blocks: 28
  References: trends.py (todo 6); CategoryBudget model (todo 3)
  Acceptance criteria: AppTest: edit 3 allocations → persisted as integer cents; toggling carryover changes displayed balance per on-read computation.
  QA scenarios: happy: edit+save+recompute (task-23-happy.txt). failure: negative allocation → rejected with message, 0 writes (task-23-failure.txt).
  Commit: Y | feat(ui): budget allocation editor with carryover toggle

- [ ] 24. Import wizard + paste review page
  What to do / Must NOT do: pages/4_Import.py, two tabs. CSV tab: upload → auto-suggest mapping from ImportPrefs → editable mapping selectors → staged preview with dedup badges (EXACT/FUZZY/NEW) → confirm → commit summary. Paste tab: plain-text textarea only → provider selector (local default; cloud opens consent dialog with payload preview) → parse → st.data_editor proposals (edit/reject rows) → confirm → atomic commit. Must NOT: accept file upload in paste tab; commit unreviewed proposals.
  Parallelization: Wave 5 | Blocked by: 13,14,20 | Blocks: 28
  References: importer.py (todo 13); paste_parse.py (todo 14); consent.py (todo 20); Metis assumption 2 (editor UX validated here)
  Acceptance criteria: AppTest: fixture CSV end-to-end → committed count correct; paste flow with cassette → confirm 7/10 → exactly 7 committed; 20-row editor frame renders <1s (timer assert).
  QA scenarios: happy: both tabs end-to-end (task-24-happy.txt). failure: paste with LLM unavailable → degraded message + manual-entry fallback, 0 writes (task-24-failure.txt).
  Commit: Y | feat(ui): csv import wizard + paste review flow

- [ ] 25. Insights page (evaluate, history, follow-ups)
  What to do / Must NOT do: pages/5_Insights.py: "Evaluate my budget" button → deterministic results always rendered; LLM interpretation via provider default from todo 19 (local default; cloud → consent dialog first); saved via recommendations.save; history list with month-over-month diff; followed toggle (user only); follow-up report section ("suggested X; actual: Y"). Must NOT: free-form chat input; LLM call on page load (button only).
  Parallelization: Wave 5 | Blocked by: 17,18,19,20 | Blocks: 28
  References: evaluate.py (todo 17); recommendations.py (todo 18); consent.py (todo 20)
  Acceptance criteria: AppTest with cassette: evaluate → recommendations count +1; toggle followed → persisted with user audit; follow-up figures match trends.py output.
  QA scenarios: happy: evaluate→store→toggle→followup (task-25-happy.txt). failure: evaluate with LLM down → deterministic results shown, degraded banner, 0 stored (task-25-failure.txt).
  Commit: Y | feat(ui): insights, recommendation history, follow-up tracking

- [ ] 26. Concurrency stress (WAL verification)
  What to do / Must NOT do: tests/integration/test_concurrency.py: 2 threads × 5 sequential inserts via service layer with 50ms jitter → all 10 land, 0 "database is locked"; assert `PRAGMA journal_mode` returns wal. Env flag LL_WAL=off to prove the test has teeth.
  Parallelization: Wave 4 | Blocked by: 4 | Blocks: -
  References: db.py (todo 4); Metis B5/AC15
  Acceptance criteria: 10 consecutive stress runs green; WAL pragma asserted.
  QA scenarios: happy: stress passes ×10 (task-26-happy.txt). failure: LL_WAL=off run detects lock errors → test fails (proves measurement real), then revert (task-26-failure.txt).
  Commit: Y | test(db): sqlite WAL concurrency stress

- [ ] 27. Performance validation (dashboard, filter, payload)
  What to do / Must NOT do: tests/integration/test_perf.py: seed 10K-tx/36-month ledger; dashboard AppTest render <3s; transactions filter interaction <1s (or pagination path asserted); payload build <2s and ≤4096 tokens; write results table to evidence.
  Parallelization: Wave 6 | Blocked by: 16,21,22 | Blocks: 28
  References: dashboards/payloads from todos 16,21,22; Metis assumption 5/AC14
  Acceptance criteria: all three thresholds green in evidence file; thresholds temporarily set to 1ms → tests fail (teeth check), then restored.
  QA scenarios: happy: thresholds pass (task-27-happy.txt). failure: 1ms-threshold run fails, proving measurement (task-27-failure.txt).
  Commit: Y | test(perf): dashboard/filter/payload budgets validated

- [ ] 28. End-to-end user journey QA
  What to do / Must NOT do: tests/e2e/test_journey.py (Streamlit AppTest; Playwright only if already available): empty DB → import 50-tx fixture CSV via wizard → set 3 category budgets → evaluate budget → view recommendation → mark followed. <60s total. Final DB assertions: ≥50 transactions, 3 budget rows, 1 recommendation followed=true, audit rows present for every write.
  Parallelization: Wave 6 | Blocked by: 21-25,27 | Blocks: 30
  References: Metis AC16; pages from todos 21-25
  Acceptance criteria: journey green end-to-end; exact DB assertions pass.
  QA scenarios: happy: full journey (task-28-happy.txt). failure: deliberately break budget-save step → journey fails AT the named step with AppTest dump saved (task-28-failure.txt), then fix.
  Commit: Y | test(e2e): full user journey

- [ ] 29. Security + scope invariant checks
  What to do / Must NOT do: scripts/check_invariants.py (wired as a pytest): forbidden-dep grep over `pip freeze` (ofxparse, plaid-python, simplefin, yapily, presidio, spacy, transformers, langchain, chromadb, torch) → 0; `grep -rn "0.0.0.0" src/ app.py` → only inside the refusal guard; `grep -rn "datetime.now(" src/local_ledger/` → 0; grep privacy payload path for Transaction model import → 0; re-assert bind refusal.
  Parallelization: Wave 6 | Blocked by: 1-25 | Blocks: -
  References: plan Must NOT have; Metis AC13/AC17
  Acceptance criteria: all invariants green; each check proven failable (toggle one violation → named failure → revert).
  QA scenarios: happy: clean tree passes (task-29-happy.txt). failure: install a forbidden dep in venv → check fails naming it; uninstall after (task-29-failure.txt).
  Commit: Y | test(security): scope + privacy invariants enforced

- [ ] 30. README final + fresh-venv quickstart verification
  What to do / Must NOT do: README.md: overview, privacy posture (local-first, aggregates-only cloud, audit log), requirements (Python ≥3.11; Ollama optional), quickstart (`python -m venv .venv` / `pip install -e .` / `streamlit run app.py`), first-run guide (import CSV → set budgets → evaluate), degradation behavior table (4 Ollama states), migration policy (alembic auto-upgrade at startup), scope boundaries. Then execute every README command in a CLEAN venv and capture output.
  Parallelization: Wave 6 | Blocked by: 28 | Blocks: -
  References: all prior todos; drafts decisions
  Acceptance criteria: every README command runs clean in fresh venv (evidence log); every path referenced in README exists (script check).
  QA scenarios: happy: fresh-venv quickstart boot (task-30-happy.txt). failure: quickstart without Ollama → app boots with banner; observed behavior matches README degradation table (task-30-failure.txt).
  Commit: Y | docs(readme): quickstart, privacy posture, degradation contract

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Plan compliance audit
- [ ] F2. Code quality review
- [ ] F3. Real manual QA
- [ ] F4. Scope fidelity

## Commit strategy
- `git init` in todo 1; one conventional commit per todo immediately after its QA passes.
- Commit types: `feat|fix|test|docs|chore`; scopes: `scaffold|ledger|ingest|llm|privacy|analysis|ui|db|perf|security|readme`.
- Never force-push; never amend published commits.
- Commit message references the todo number (e.g. `feat(ingest): … (#5)`).
- Tracking: `.omo/evidence/` is gitignored; `.omo/plans/` and `.omo/drafts/` are committed (durable plan + resume record).
- Executor run loop: load the `using-git-worktrees` skill before starting; work on a branch off `main`; squash-merge per todo if simpler for review, else keep individual commits.

## Success criteria
- All 30 todos QA-green with evidence files in `.omo/evidence/`.
- Final verification wave F1-F4 each APPROVED with documented verdicts.
- `task-16` evidence shows ≤4096-token payload for a 10K-tx/36-month ledger.
- `task-26` evidence shows 10 consecutive concurrency runs green on WAL.
- `task-27` evidence shows dashboard/filter/payload thresholds met.
- `task-28` evidence shows full user journey <60s with exact DB assertions.
- `task-29` evidence shows all scope/privacy invariants green and each provably failable.
- `task-30` evidence shows README quickstart runs clean in a fresh venv.
- `task-19` either records a rubric verdict and matching default, or records an explicit SKIP-deferred verdict (Ollama absent).
- User can run `streamlit run app.py` on their machine and (a) import a CSV, (b) set budgets, (c) click Evaluate, (d) see a recommendation, (e) mark it followed — with the app behaving per the degradation contract when Ollama is off.

# Try a tax document with a local model

Start with one small, text-based 2025 1099-INT or 1099-DIV. This trial covers
extraction, manual verification and a TaxAct comparison. It does not qualify
the model or complete a return.

## Start an isolated pilot

Use this prepared workspace: the tax changes are still uncommitted and will
not appear in a new checkout of main automatically.

```bash
cd /home/yl/proj/local-ledger
uv sync --extra dev

pilot_root="$HOME/.local/share/local-ledger/tax-pilot"
mkdir -p "$pilot_root"
chmod 700 "$pilot_root"
export LOCAL_LEDGER_DB_URL="sqlite:///$pilot_root/ledger.db"
export LOCAL_LEDGER_TAX_DIR="$pilot_root/documents"
export LOCAL_LEDGER_TAX_EXPORT_DIR="$pilot_root/exports"

.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1
```

Open **http://127.0.0.1:8501**. The app migrates the pilot database at startup.
This pilot has separate documents, settings and exports from your normal ledger.
Reuse the same directory to resume; stop the server with Ctrl-C. Keep the
directory until debugging is complete: it contains originals and review history.

## Configure your existing local model

For your installed **official GLM**, select **DwarfStar (GLM 5.3 Flash official)**
(`glmo`) in Settings. Its endpoint is `http://127.0.0.1:8000/v1` and its model is
`glm-5.3-flash-chat`, confirmed in the running server's model listing. The chat
variant avoids the reasoning mode for this structured extraction trial.

The October 1 trial started it as a user service:

```bash
systemctl --user status local-ledger-glmo-pilot
# Stop when finished:
systemctl --user stop local-ledger-glmo-pilot
```

To start it later in a terminal, use `/home/yl/ds4/run-glm-official.sh` **without
arguments**. The installed launcher forwards `server` to the engine, which
rejects it; the no-argument default selects server mode successfully. The app
profile uses this workaround and discovers `~/ds4` as well as `~/Projects/ds4`.
All DwarfStar profiles share port 8000: stop one manually before switching.

In **Settings → Local model server**, choose the matching profile:

- **Ollama:** start your server if needed. `ollama list` shows installed model
  names; use `http://127.0.0.1:11434/v1` and the exact installed name.
- **DwarfStar ds4/glm/glmo:** select the matching profile and existing launcher.
  Settings supports starting a configured launcher. Confirm its endpoint and
  served model ID.
- For another compatible local server, edit the endpoint and model fields.
  Its endpoint must be loopback and its readiness probe must succeed.

Save local settings and check for **READY**. Use the model actually served,
rather than assuming the default name is installed. Cloud settings are not
needed. Local generation refuses remote hosts, ignores environment proxies and
does not follow redirects.

To check connectivity with a synthetic prompt, replace the model placeholder:

```bash
.venv/bin/python -m local_ledger.llm.smoke \
  --profile ollama --endpoint http://127.0.0.1:11434/v1 --model YOUR_INSTALLED_MODEL
```

For ds4/glm, use that profile and the actual endpoint/model from Settings.
The smoke runner does not read saved settings. PASS establishes connectivity,
not extraction accuracy.

## Review one document

1. **Tax Intake:** select 2025 and Federal. In **Review scope**, choose
   **Documents only** for this document-only trial and save. Confirm the property
   list only if you have checked it; unresolved scope can remain a visible gap
   while you test extraction.
2. **Upload Evidence:** upload the PDF with its document category. For a corrected
   form, explicitly select the original it replaces.
3. **Document Inspector:** select the saved document. Download/open the original
   and compare the page text with it. Originals are hash-verified before display
   read-back or model extraction.
4. Click **Extract proposals with local model**. The report shows saved proposal
   counts, skipped pages, unsupported fields and rejected citations. Fields are
   never automatically verified. A scan without text requires manual entry.
5. **Verification Queue:** check field name, box, amount and page/snippet against
   the original. Verify correct proposals; reject incorrect ones; enter missing
   fields manually. A document with active proposed/verified fields has extraction
   disabled to avoid another proposal set over its existing review. Rejected
   history stays saved.
6. **Tax Investments:** inspect typed working-paper figures and questions and
   compare them with the corresponding figures in TaxAct.
7. **TaxAct Comparison:** record the amounts you entered in TaxAct. Investigate
   differences and export a **draft** while experimenting. Final exports remain
   gated by scope, evidence, questions and comparison coverage.

The intake KPI counts verified fields, rather than adding unrelated amounts into
one “Verified Total.” Use working papers for meaningful totals. Keep the year and
jurisdiction consistent across pages: Federal and California are separate
workspaces, and changing the selector does not copy evidence.

## Current limits

- Your shared-partition model is
  `/mnt/winshare/LM-Studio-Models/agentionai/Signal-3.8-Flash-Next-GGUF/Signal-3.8-Flash-Next-AP-IQ4_XS.gguf`
  (84.24 GiB). Its GGUF metadata identifies a **Signal fine-tune of Qwen3.8**,
  architecture `qwen4exp`. The installed DwarfStar
  `/home/yl/ds4/docs/QWEN38_FLASH_NEXT.md` explicitly excludes ROCm for this
  architecture; CPU is a correctness reference, not a general inference backend.
  No alternate llama.cpp/LM Studio executable was found in PATH or the checked
  home directories. Qwen inference has **not** been attempted or validated here.
  Using it requires an installed runtime supporting both this architecture and
  its quantization on this machine.
- No OCR or vision: scans need manual entry.
- Automatic proposals cover scalar fields. Repeating 1099-B lots need structured
  investment-sale records; the extraction button does not create them.
- Rental proposals do not automatically create properties, expenses, allocations
  or depreciation schedules. The rental view audits those records once present;
  completing their browser data-entry flow remains follow-up work.
- Sidebar chat is the existing **budget** assistant, not evidence-grounded tax
  Q&A (TAX-12).
- Fake-client tests establish software behavior, not live model quality. TAX-10
  qualification and TAX-11 complete-user-journey validation remain needed.

## Debugging

Record model ID, document category, page/box, expected vs proposed amount, and
the failing stage: PDF text, model output, citation validation or verification.
If the inspector text omits the amount or scrambles columns, start with PDF
extraction rather than the model prompt.

NOT_RUNNING/MODEL_MISSING means check the server and Settings. Rejected snippets
or page references did not match extracted text. Unsupported fields intentionally
stay unresolved. Malformed output did not meet the structured schema.

For shared debugging, provide synthetic/redacted reproductions and error messages.
Keep originals in the local pilot: uploading them to this coding conversation
would be a separate cloud transfer.

## Coordinator integration — October 1, 2026

Connected investment/rental/comparison navigation, extraction and scope controls;
added original download; removed the mixed amount KPI; fixed inspector access to
nonexistent `TaxDocument.byte_size`. Local generation uses the SDK HTTP client
with proxies and redirects disabled. Routing tests use fake transports and
synthetic requests, not live-network or model-quality qualification.

Official GLM live check: `glm-5.3-flash-chat` through `get_local_client` and the
real `extract_document_fields` entry point returned the three synthetic 1099-INT
figures exactly: box 1 **142,550 cents**, box 3 **20,000 cents**, box 4 **1,234
cents**. All page/snippet citations passed validation; no unresolved fields or
notes. Elapsed **46.66 seconds**, no retries. No database or real documents were
read or written. This is one connectivity/extraction example, not TAX-10 model
qualification. The server remains in the user service until stopped.

The combined-suite check also found unused temporary-storage fixtures in the
investment, rental and export tests: 44 attempted writes used the real default
document directory and were blocked by the sandbox. Their service fixtures now
depend on the temporary-storage fixtures, so these tests use isolated files.

Final verification: **643 passed, 10 skipped, 4 deselected**; Ruff over
`src tests app.py app_pages` and `git diff --check` passed. Changes remain
uncommitted. The app itself has not been started as a persistent browser service;
use the isolated pilot instructions above to launch it.

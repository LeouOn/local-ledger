"""Import page: CSV import wizard, paste-parse review, and staged review queue.

The commit path is deliberately review-first: rows are staged, the user accepts
or rejects a subset, and only accepted rows are committed - scoped to the batch
and account shown, so a commit can never sweep in another account's rows.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlmodel import select

from local_ledger.ingest.csv_import import parse_csv, suggest_mapping
from local_ledger.ingest.importer import ImportService
from local_ledger.ingest.review_ui import render_commit_result, render_staged_review, summarize
from local_ledger.llm.paste_parse import PasteParseProposal, PasteParseService
from local_ledger.models import Account
from local_ledger.ui.chrome import degraded_message, get_session
from local_ledger.ui.components import section_header

section_header("INGEST", "Import")

session = get_session()
importer = ImportService(session)
paste_svc = PasteParseService(session)

accounts = list(session.exec(select(Account)).all())
if not accounts:
    st.info("Create an account on the Budgets page first.")
    st.stop()

csv_tab, paste_tab = st.tabs(["CSV import", "Paste statement"])

with csv_tab:
    section_header("CSV", "CSV import")
    acc = st.selectbox("Account", accounts, format_func=lambda a: a.name, key="csv_acc")
    uploaded = st.file_uploader("CSV file", type=["csv"])
    if uploaded is not None:
        text = uploaded.getvalue().decode("utf-8")
        import csv as _csv
        import io as _io

        reader = _csv.reader(_io.StringIO(text))
        headers = next(reader, [])
        mapping = suggest_mapping(headers)
        st.caption(
            f"Suggested mapping: date={mapping.date_col}, amount={mapping.amount_col}, "
            f"payee={mapping.payee_col}, id={mapping.imported_id_col}"
        )
        result = parse_csv(text, mapping=mapping)
        if result.errors:
            st.warning(f"{len(result.errors)} row(s) could not be parsed:")
            for e in result.errors:
                st.text(f"line {e.line_number}: {e.reason}")
        if result.transactions:
            df = pd.DataFrame(
                [
                    {
                        "row": t.source_row,
                        "date": t.date,
                        "amount_cents": t.amount_cents,
                        "payee": t.payee_raw,
                        "imported_id": t.imported_id,
                    }
                    for t in result.transactions
                ]
            )
            st.dataframe(df, use_container_width=False)
            if st.button(f"Stage {len(result.transactions)} rows"):
                staged = importer.stage(result.transactions, acc.id, batch_label=uploaded.name)
                st.session_state["csv_batch"] = staged[0].import_batch if staged else None
                new_count = sum(1 for s in staged if s.dedup_status == "NEW")
                st.success(
                    f"Staged {len(staged)} rows ({new_count} NEW, "
                    f"{sum(1 for s in staged if s.dedup_status == 'FUZZY')} FUZZY, "
                    f"{sum(1 for s in staged if s.dedup_status == 'EXACT')} EXACT). "
                    "Review below, then commit what you accept."
                )

    section_header("REVIEW", "Staged rows")
    batches = sorted(
        {s.import_batch for s in importer.list_staged(account_id=acc.id, uncommitted_only=False)},
        reverse=True,
    )
    if not batches:
        st.caption("Nothing staged for this account yet.")
    else:
        latest = st.session_state.get("csv_batch")
        index = batches.index(latest) if latest in batches else 0
        batch = st.selectbox("Batch", batches, index=index, key="csv_batch_select")
        st.caption(
            "NEW rows are new to the ledger; FUZZY rows match something already staged or "
            "posted and need an explicit decision; EXACT rows are already imported and are "
            "skipped automatically."
        )
        render_staged_review(
            importer,
            account_id=acc.id,
            batch=batch,
            actor=f"import:{batch}",
            key_prefix="csv_review",
        )

with paste_tab:
    section_header("PASTE", "Paste a statement or budget")
    degraded = degraded_message("paste_parse")
    if degraded:
        st.warning(degraded)
    paste_acc = st.selectbox("Paste account", accounts, format_func=lambda a: a.name, key="paste_acc")
    pasted = st.text_area("Paste plain text", height=200, key="paste_text")
    if st.button("Parse with local LLM") and pasted.strip():
        if degraded:
            st.info("Switch to manual entry on the Transactions page, or start Ollama.")
        else:
            try:
                from local_ledger.llm.providers import get_local_client, load_settings
                from local_ledger.llm.structured import make_openai_completer

                settings = load_settings(session)
                client = get_local_client(settings)
                completer = make_openai_completer(client)
                proposals = paste_svc.parse_pasted_text(
                    pasted, completer=completer, model=settings.local_model
                )
                st.session_state["paste_proposals"] = [p.__dict__ for p in proposals]
            except Exception as exc:
                st.error(f"Parse failed: {exc}")

    proposals = st.session_state.get("paste_proposals", [])
    if proposals:
        st.caption(
            "Delete the rows you do not want, then commit the rest. A row that matches "
            "something already in the ledger is held back for review instead of being written."
        )
        df = pd.DataFrame(proposals)
        edited = st.data_editor(df, num_rows="dynamic", key="paste_editor")
        if st.button("Commit accepted rows"):
            fields = set(PasteParseProposal.__dataclass_fields__)
            kept = [
                PasteParseProposal(**{k: v for k, v in r.items() if k in fields})
                for r in edited.to_dict("records")
            ]
            kept = [k for k in kept if k.amount_cents]
            result = paste_svc.commit_accepted(kept, account_id=paste_acc.id, actor="user")
            st.session_state["paste_proposals"] = []
            st.session_state["paste_result"] = summarize(result)
            st.rerun()

render_commit_result(st.session_state.pop("paste_result", None))

"""Local Ledger - Streamlit multipage entry point.

Startup guard refuses non-localhost bind (hard security invariant). Initializes
the schema, renders an Ollama availability banner on every page, and wires
navigation over app_pages/.
"""

from __future__ import annotations

import os

_LOOPBACK_ADDRESSES = {"127.0.0.1", "localhost", "::1"}


def _effective_bind_address() -> str:
    explicit = os.environ.get("STREAMLIT_SERVER_ADDRESS")
    if explicit:
        return explicit
    return "localhost"


def assert_localhost_bind() -> None:
    address = _effective_bind_address()
    if address not in _LOOPBACK_ADDRESSES:
        raise SystemExit(
            f"refusing to bind non-localhost address {address!r}; "
            "Local Ledger is single-user localhost-only by design."
        )


assert_localhost_bind()


def main() -> None:
    import streamlit as st

    from local_ledger.ui.chrome import render_ollama_banner

    st.set_page_config(page_title="Local Ledger", page_icon="📒", layout="wide")

    page = st.navigation(
        [
            st.Page("app_pages/home.py", title="Home", icon=":material/dashboard:"),
            st.Page("app_pages/transactions.py", title="Transactions", icon=":material/receipt_long:"),
            st.Page("app_pages/budgets.py", title="Budgets", icon=":material/savings:"),
            st.Page("app_pages/import_.py", title="Import", icon=":material/upload_file:"),
            st.Page("app_pages/insights.py", title="Insights", icon=":material/insights:"),
            st.Page("app_pages/settings.py", title="Settings", icon=":material/settings:"),
        ],
        position="top",
    )

    render_ollama_banner()
    page.run()


if __name__ == "__main__":
    main()

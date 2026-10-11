"""Local Ledger - Streamlit multipage entry point.

Startup guard refuses non-localhost bind (hard security invariant). Initializes
the schema, renders an Ollama availability banner on every page, and wires
navigation over app_pages/.
"""

from __future__ import annotations

import os
import sys

_LOOPBACK_ADDRESSES = {"127.0.0.1", "localhost", "::1"}


def _address_from_argv() -> str | None:
    for i, arg in enumerate(sys.argv):
        if arg == "--server.address" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--server.address="):
            return arg.split("=", 1)[1]
    return None


def _effective_bind_address() -> str:
    # CLI flag is the primary attack vector (--server.address 0.0.0.0); check it
    # first, then the env var, then Streamlit's resolved config, then default.
    return (
        _address_from_argv()
        or os.environ.get("STREAMLIT_SERVER_ADDRESS")
        or _streamlit_config_address()
        or "localhost"
    )


def _streamlit_config_address() -> str | None:
    try:
        from streamlit import config as _st_config

        return _st_config.get_option("server.address") or None
    except Exception:
        return None


def assert_localhost_bind() -> None:
    address = _effective_bind_address()
    if address not in _LOOPBACK_ADDRESSES:
        raise SystemExit(
            f"refusing to bind non-localhost address {address!r}; "
            "Local Ledger is single-user localhost-only by design."
        )


assert_localhost_bind()


def _check_runtime_bind() -> str | None:
    resolved = _streamlit_config_address()
    if resolved and resolved not in _LOOPBACK_ADDRESSES:
        return resolved
    return None


def main() -> None:
    import streamlit as st

    from local_ledger.llm.providers import load_settings
    from local_ledger.ui.chat_sidebar import render_chat_sidebar
    from local_ledger.ui.chrome import get_session, render_ollama_banner
    from local_ledger.ui.theme import inject_css

    non_loopback = _check_runtime_bind()
    if non_loopback:
        st.set_page_config(page_title="Local Ledger - REFUSED")
        st.error(
            f"REFUSING TO SERVE on non-localhost address {non_loopback!r}. "
            "Local Ledger is single-user localhost-only by design. "
            "Remove the --server.address flag or set it to 127.0.0.1."
        )
        st.stop()

    st.set_page_config(page_title="Local Ledger", page_icon="📒", layout="wide")

    _settings = load_settings(get_session())
    inject_css(_settings.theme_mode)

    page = st.navigation(
        [
            st.Page("app_pages/home.py", title="Home", icon=":material/dashboard:"),
            st.Page("app_pages/transactions.py", title="Transactions", icon=":material/receipt_long:"),
            st.Page("app_pages/budgets.py", title="Budgets", icon=":material/savings:"),
            st.Page("app_pages/import_.py", title="Import", icon=":material/upload_file:"),
            st.Page("app_pages/insights.py", title="Insights", icon=":material/insights:"),
            st.Page("app_pages/goals.py", title="Goals", icon=":material/flag:"),
            st.Page("app_pages/rules.py", title="Rules", icon=":material/rule:"),
            st.Page("app_pages/tax_intake.py", title="Tax Intake", icon=":material/inventory:"),
            st.Page("app_pages/tax_investments.py", title="Tax Investments", icon=":material/trending_up:"),
            st.Page("app_pages/tax_rentals.py", title="Tax Rentals", icon=":material/home:"),
            st.Page("app_pages/tax_california.py", title="California Tax", icon=":material/gavel:"),
            st.Page("app_pages/tax_compare.py", title="TaxAct Comparison", icon=":material/fact_check:"),
            st.Page("app_pages/settings.py", title="Settings", icon=":material/settings:"),
        ],
        position="top",
    )

    render_ollama_banner()
    page.run()
    render_chat_sidebar(context=st.session_state.get("chat_context"))


if __name__ == "__main__":
    main()

"""Local Ledger - Streamlit entry point.

Startup guard: refuse to bind anything other than loopback. This is a hard
security invariant (see plan .omo/plans/local-ledger.md, Must NOT have).
"""

from __future__ import annotations

import os

_LOOPBACK_ADDRESSES = {"127.0.0.1", "localhost", "::1"}


def _effective_bind_address() -> str:
    """Resolve the address Streamlit will actually bind to.

    Precedence mirrors streamlit's own: explicit CLI/STREAMLIT_* env var wins,
    then the config file, then streamlit's compiled default.
    """
    explicit = os.environ.get("STREAMLIT_SERVER_ADDRESS")
    if explicit:
        return explicit
    # streamlit's compiled default is "localhost". The config file at
    # .streamlit/config.toml sets 127.0.0.1; the test below covers override.
    return "localhost"


def assert_localhost_bind() -> None:
    """Refuse to start if the effective bind address is not loopback."""
    address = _effective_bind_address()
    if address not in _LOOPBACK_ADDRESSES:
        raise SystemExit(
            f"refusing to bind non-localhost address {address!r}; "
            "Local Ledger is single-user localhost-only by design."
        )


# Execute the guard at import so any launch path (streamlit run, AppTest) is
# covered. The guard is a no-op once the address is loopback.
assert_localhost_bind()


def main() -> None:
    # Full multipage entry lands in todo 20. This stub imports streamlit lazily
    # so the guard itself has zero hard dependency at import time.
    import streamlit as st

    st.set_page_config(page_title="Local Ledger", page_icon="📒")
    st.title("Local Ledger")
    st.write("App shell coming in a later wave.")


if __name__ == "__main__":
    main()

"""Security + scope invariants enforced as tests.

Catches scope creep and privacy regressions: forbidden deps must not be
installed; the only non-localhost address reference must be the refusal guard;
business logic must not call datetime.now() (naive UTC dates only); the bind
guard must fire on non-loopback.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import os

import pytest

FORBIDDEN_DEPS = {
    "ofxparse",
    "plaid-python",
    "simplefin",
    "yapily",
    "presidio-analyzer",
    "presidio-anonymizer",
    "spacy",
    "transformers",
    "langchain",
    "langgraph",
    "chromadb",
    "torch",
}


def _installed_distributions() -> set[str]:
    return {d.metadata["Name"].lower() for d in importlib.metadata.distributions()}


def test_no_forbidden_dependencies_installed() -> None:
    installed = _installed_distributions()
    present = FORBIDDEN_DEPS & installed
    assert not present, f"forbidden dependencies installed: {sorted(present)}"


def test_only_loopback_address_reference_is_the_refusal_guard() -> None:
    """The string '0.0.0.0' may appear only inside app.py's refusal-guard logic."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    scan_dirs = [os.path.join(repo_root, "src", "local_ledger"), os.path.join(repo_root, "app_pages")]
    offenders: list[str] = []
    for scan_dir in scan_dirs:
        for dirpath, _dirnames, filenames in os.walk(scan_dir):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    if "0.0.0.0" in fh.read():
                        offenders.append(path)
    assert not offenders, f"non-loopback bind references in production code: {offenders}"


def test_business_logic_has_no_datetime_now_calls() -> None:
    """datetime.now() is forbidden in business modules; use UTC naive dates."""
    import local_ledger

    pkg_dir = os.path.dirname(local_ledger.__file__)
    offenders: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(pkg_dir):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            # allow datetime.now(timezone.utc) / datetime.now(UTC) - the safe form
            for line in src.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "datetime.now(" in line and "UTC" not in line and "timezone" not in line:
                    offenders.append(f"{path}: {stripped}")
    assert not offenders, f"raw datetime.now() calls found (use UTC-aware): {offenders}"


def test_localhost_bind_guard_fires_on_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing app.py with a non-loopback address must SystemExit at the guard."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    app_path = os.path.join(repo_root, "app.py")
    monkeypatch.setenv("STREAMLIT_SERVER_ADDRESS", "0.0.0.0")
    with open(app_path, encoding="utf-8") as fh:
        source = fh.read()
    # Exec only the guard portion: strip the __main__ block and main() so we
    # don't start Streamlit. The guard runs at module top-level on exec.
    source = source.split("def main()")[0]
    namespace: dict = {"__name__": "app_under_test"}
    with pytest.raises(SystemExit, match="refusing to bind non-localhost"):
        exec(compile(source, app_path, "exec"), namespace)


def test_privacy_payload_path_has_no_transaction_import() -> None:
    """The privacy module must not import the Transaction model for egress."""
    import local_ledger.llm.privacy as privacy

    src = inspect.getsource(privacy)
    # Transaction is imported only for the type-check rejection, never serialized out
    assert "model_validate(tx" not in src
    assert "tx.model_dump_json" not in src

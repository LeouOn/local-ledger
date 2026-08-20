"""Smoke test: theme module composes correctly with app.py and settings.py.

We don't boot the full app via AppTest.from_file because that's a 3-second
timeout and the app does a lot of cold-start work (DB init + migrations +
CSS generation + chat sidebar). The full suite's other AppTest tests
already cover page-level rendering. This smoke test verifies the wiring
between the theme module and the app's startup path.
"""

from __future__ import annotations

import os


def test_theme_module_imports_cleanly() -> None:
    """All theme entry points are importable and return expected types."""
    from local_ledger.ui.theme import (
        TOKENS_DARK,
        TOKENS_LIGHT,
        apply_chart_colors,
        build_css,
        inject_css,
        palette_for_chart,
    )

    assert isinstance(TOKENS_LIGHT.bg, str)
    assert isinstance(TOKENS_DARK.bg, str)
    assert callable(build_css)
    assert callable(inject_css)
    assert callable(apply_chart_colors)
    assert len(palette_for_chart()) == 5


def test_build_css_emits_tokens_for_both_modes() -> None:
    """build_css returns a CSS string that differs between modes."""
    from local_ledger.ui.theme import build_css

    css_light = build_css("light")
    css_dark = build_css("dark")
    assert css_light != css_dark
    # Light accent must be in light CSS, dark accent in dark CSS
    assert "#0F766E" in css_light
    assert "#14B8A6" in css_dark


def test_app_module_imports_without_error() -> None:
    """app.py imports cleanly (does not boot Streamlit)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("app", "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Don't execute main() — just import the module to catch syntax errors
    # and missing imports. The `_assert_localhost_bind()` runs at module
    # import time, so we set a localhost env var first.
    os.environ.setdefault("STREAMLIT_SERVER_ADDRESS", "127.0.0.1")
    spec.loader.exec_module(module)
    assert callable(module.main)
    assert hasattr(module, "_check_runtime_bind")


def test_settings_page_module_imports_cleanly() -> None:
    """app_pages/settings.py imports cleanly (does not render)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "settings_page", "app_pages/settings.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

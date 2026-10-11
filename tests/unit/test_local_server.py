"""Local server manager tests: reuse, single-flight, slow load, failure, mismatch.

All probes/spawns are injected fakes; no sockets and no real processes.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_ledger.llm.local_server import (
    DEFAULT_PROFILE_KEY,
    PROFILES,
    LocalProfile,
    LocalServerManager,
    profile_from_settings,
)
from local_ledger.llm.ollama import OllamaState

DS4 = PROFILES["ds4"]


class FakeProc:
    def __init__(self, *, exit_after: int | None = None) -> None:
        self.poll_calls = 0
        self.exit_after = exit_after  # polls before "exiting"; None = never
        self.returncode: int | None = None

    def poll(self) -> int | None:
        self.poll_calls += 1
        if self.exit_after is not None and self.poll_calls > self.exit_after:
            self.returncode = 1
        return self.returncode


class FakeDetect:
    """Injectable detect_state; state is settable and call-counted."""

    def __init__(self, state: OllamaState = OllamaState.NOT_RUNNING) -> None:
        self.state = state
        self.calls = 0
        self.ready_after: int | None = None  # flip to READY after N calls
        self._lock = threading.Lock()

    def set(self, state: OllamaState) -> None:
        self.state = state

    def __call__(self, base_url: str, model: str, *args: object, **kwargs: object) -> OllamaState:
        with self._lock:
            self.calls += 1
            calls = self.calls
        if self.ready_after is not None and calls >= self.ready_after:
            return OllamaState.READY
        return self.state


class FakeSpawn:
    def __init__(self, proc: FakeProc, *, emit: bytes = b"") -> None:
        self.proc = proc
        self.calls: list[tuple[list[str], dict]] = []
        self.emit = emit

    def __call__(self, argv: list[str], **kwargs: object) -> FakeProc:
        self.calls.append((list(argv), dict(kwargs)))
        if self.emit and "stdout" in kwargs:
            kwargs["stdout"].write(self.emit)
        return self.proc


def make_manager(
    detect: FakeDetect,
    spawn: FakeSpawn,
    *,
    log_dir: Path,
    timeout: float = 10.0,
    clock=None,
    which: Callable[[str], str | None] | None = None,
) -> LocalServerManager:
    if which is None:
        def fake_which(cmd: str) -> str | None:
            if "nonexistent" in cmd or "ghost" in cmd:
                return None
            if "run-ds4f.sh" in cmd or "run-glm.sh" in cmd or "ollama" in cmd:
                return os.path.expanduser(cmd)
            return shutil.which(cmd)

        which = fake_which

    return LocalServerManager(
        detect=detect,
        spawn=spawn,
        sleeper=lambda _s: None,
        clock=clock if clock is not None else time.monotonic,
        log_dir=log_dir,
        poll_interval=0,
        startup_timeout=timeout,
        which=which,
    )


def wait_final(mgr: LocalServerManager, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = mgr.status()
        if s is not None and s.state != "starting":
            return s
        time.sleep(0.005)
    raise AssertionError("manager never left the starting state")


# --- server reuse ---


def test_healthy_server_is_adopted_without_launching(tmp_path: Path) -> None:
    detect = FakeDetect(OllamaState.READY)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    status = mgr.ensure_running(DS4)

    assert status.state == "ready"
    assert status.reused is True
    assert spawn.calls == []  # nothing was started; the running server is used


def test_fresh_process_adopts_server_started_earlier(tmp_path: Path) -> None:
    """App restart while our spawned server keeps running: adopt, don't dup."""
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)
    mgr.ensure_running(DS4)
    detect.set(OllamaState.READY)
    assert wait_final(mgr).state == "ready"

    # Simulated new app process: brand-new manager, endpoint already healthy.
    detect2 = FakeDetect(OllamaState.READY)
    mgr2 = make_manager(detect2, FakeSpawn(FakeProc()), log_dir=tmp_path)
    status = mgr2.ensure_running(DS4)
    assert status.state == "ready"
    assert status.reused is True


# --- single-flight startup ---


def test_concurrent_ensure_running_launches_exactly_once(tmp_path: Path) -> None:
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    first = mgr.ensure_running(DS4)
    second = mgr.ensure_running(DS4)  # a Streamlit rerun during startup

    assert first is second
    assert first.state == "starting"
    assert len(spawn.calls) == 1

    detect.set(OllamaState.READY)
    assert wait_final(mgr).state == "ready"
    assert len(spawn.calls) == 1


def test_launch_uses_argument_list_without_shell(tmp_path: Path) -> None:
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)
    mgr.ensure_running(DS4)

    argv, kwargs = spawn.calls[0]
    assert isinstance(argv, list)
    assert argv[0].endswith("run-ds4f.sh")
    assert argv[1] == "server"
    assert "--ssd-streaming" in argv  # resident load OOMs on this machine
    assert "shell" not in kwargs or kwargs.get("shell") is False
    # CTX override reaches the launcher through the environment.
    assert kwargs["env"]["CTX"] == "16384"
    assert kwargs["env"]["PATH"] == os.environ["PATH"]


# --- slow loading / readiness ---


def test_slow_loading_server_becomes_ready_after_polling(tmp_path: Path) -> None:
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    detect.ready_after = 5  # model loads over the first few polls
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    mgr.ensure_running(DS4)
    status = wait_final(mgr)

    assert status.state == "ready"
    assert status.reused is False
    assert detect.calls >= 5


# --- startup failure ---


def test_startup_failure_reports_exit_and_logs(tmp_path: Path) -> None:
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc(exit_after=2), emit=b"boom: cannot load gguf\n")
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    mgr.ensure_running(DS4)
    status = wait_final(mgr)

    assert status.state == "failed"
    assert "exited during startup" in status.detail
    assert any("boom" in line for line in status.logs)


def test_startup_timeout_is_bounded_and_process_left_running(tmp_path: Path) -> None:
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())  # never exits, never becomes healthy
    ticks = iter(range(10_000))  # clock advances 1s per read
    mgr = make_manager(detect, spawn, log_dir=tmp_path, timeout=5.0, clock=lambda: next(ticks))

    mgr.ensure_running(DS4)
    status = wait_final(mgr)

    assert status.state == "failed"
    assert "did not become healthy" in status.detail


def test_unresolvable_launcher_fails_without_spawn(tmp_path: Path) -> None:
    ghost = LocalProfile(
        key="ghost",
        label="Ghost",
        base_url="http://127.0.0.1:9/v1",
        model="ghost",
        launch=("/nonexistent/ghost-server",),
    )
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    status = mgr.ensure_running(ghost)

    assert status.state == "failed"
    assert "Launch command not found" in status.detail
    assert spawn.calls == []


# --- never replace a running server ---


def test_endpoint_serving_different_model_is_never_replaced(tmp_path: Path) -> None:
    """GLM selected while a DS4 server holds the port: report, don't touch."""
    detect = FakeDetect(OllamaState.MODEL_MISSING)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    status = mgr.ensure_running(PROFILES["glm"])

    assert status.state == "failed"
    assert "different model" in status.detail
    assert spawn.calls == []


# --- profiles ---


def test_profiles_cover_ollama_ds4_glm() -> None:
    assert set(PROFILES) == {"ollama", "ds4", "glm", "glmo"}
    assert PROFILES["ds4"].model == "deepseek-v4-flash"
    # The GLM engine is LISTED as glm-5.2 by the server; health checks must
    # use the listed id even though request aliases exist.
    assert PROFILES["glm"].model == "glm-5.2"
    # Both DwarfStar profiles share one port, so only one can run at a time.
    assert PROFILES["ds4"].base_url == PROFILES["glm"].base_url
    # Launch goes through the project's wrapper scripts, which carry the
    # streaming / expert-cache / MTP / memory-guard settings.
    assert PROFILES["ds4"].launch[0].endswith("run-ds4f.sh")
    assert PROFILES["ds4"].launch[1] == "server"
    assert PROFILES["ds4"].launch_env == (("CTX", "16384"),)
    assert PROFILES["glm"].launch == (
        "~/Projects/ds4/run-glm.sh",
        "server",
        "--kv-disk-dir",
        "/tmp/ds4-kv-glm",
        "--kv-disk-space-mb",
        "8192",
    )


def test_official_glm_launcher_discovery_and_default_mode(monkeypatch) -> None:
    from local_ledger.llm.local_server import resolve_launcher

    monkeypatch.delenv("LOCAL_LEDGER_DS4_HOME", raising=False)
    monkeypatch.delenv("DS4_HOME", raising=False)
    profile = PROFILES["glmo"]
    expected = str(Path.home() / "ds4" / "run-glm-official.sh")
    assert resolve_launcher(profile, which=lambda path: path if path == expected else None) == expected
    assert len(profile.launch) == 1
    assert profile.model == "glm-5.3-flash-chat"
    assert profile.base_url == PROFILES["ds4"].base_url


def test_profile_from_settings_falls_back_to_ollama() -> None:
    assert profile_from_settings(SimpleNamespace(local_profile="glm")).key == "glm"
    assert profile_from_settings(SimpleNamespace(local_profile="bogus")).key == DEFAULT_PROFILE_KEY
    assert profile_from_settings(object()).key == DEFAULT_PROFILE_KEY


# --- effective endpoint/model come from settings ---


class RecordingDetect(FakeDetect):
    def __init__(self, state: OllamaState) -> None:
        super().__init__(state)
        self.seen: list[tuple[str, str]] = []

    def __call__(self, base_url: str, model: str, *args: object, **kwargs: object) -> OllamaState:
        self.seen.append((base_url, model))
        return super().__call__(base_url, model, *args, **kwargs)


def test_health_checks_use_custom_endpoint_and_model(tmp_path: Path) -> None:
    """Startup must verify the same endpoint/model the client will use."""
    detect = RecordingDetect(OllamaState.READY)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    status = mgr.ensure_running(
        DS4, base_url="http://localhost:9999/v1", model="custom-model"
    )

    assert status.state == "ready"
    assert detect.seen[0] == ("http://localhost:9999", "custom-model")  # /v1 stripped
    assert spawn.calls == []


def test_readiness_poll_uses_custom_endpoint_and_model(tmp_path: Path) -> None:
    """Custom endpoint overrides probe for adoption, but launching is
    rejected because the wrapper scripts bind a fixed port."""
    detect = RecordingDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    status = mgr.ensure_running(
        DS4, base_url="http://127.0.0.1:9000/v1", model="deepseek-v4-flash"
    )

    assert status.state == "failed"
    assert "does not match" in status.detail
    assert spawn.calls == []


def test_fixed_model_profile_rejects_model_override(tmp_path: Path) -> None:
    """DwarfStar scripts load one fixed GGUF; a custom model setting cannot
    be produced by launching and must be rejected."""
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    status = mgr.ensure_running(PROFILES["glm"], model="glm-5.2-chat")

    assert status.state == "failed"
    assert "does not match" in status.detail
    assert spawn.calls == []


def test_multi_model_profile_launches_with_custom_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama serves whatever is pulled, so a custom model is launchable and
    readiness waits on the configured model."""
    import local_ledger.llm.local_server as local_server_module

    monkeypatch.setattr(
        local_server_module, "resolve_launcher", lambda profile: f"/fake/{profile.key}"
    )
    detect = RecordingDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())
    mgr = make_manager(detect, spawn, log_dir=tmp_path)

    mgr.ensure_running(PROFILES["ollama"], model="phi4:latest")
    detect.set(OllamaState.READY)
    status = wait_final(mgr)

    assert status.state == "ready"
    assert len(spawn.calls) == 1
    assert detect.seen[0] == ("http://localhost:11434", "phi4:latest")
    assert all(s == ("http://localhost:11434", "phi4:latest") for s in detect.seen)


# --- no duplicate launch while the first process lives ---


def test_timeout_does_not_permit_duplicate_launch_while_process_lives(tmp_path: Path) -> None:
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    proc = FakeProc()  # never exits on its own
    spawn = FakeSpawn(proc)
    ticks = iter(range(100_000))  # clock advances 1s per read
    mgr = make_manager(detect, spawn, log_dir=tmp_path, timeout=5.0, clock=lambda: next(ticks))

    mgr.ensure_running(DS4)
    assert wait_final(mgr).state == "failed"  # timed out, process still alive

    # Subsequent attempts while the process lives must NOT spawn another.
    mgr.ensure_running(DS4)
    mgr.ensure_running(DS4)
    assert len(spawn.calls) == 1
    assert wait_final(mgr).state == "failed"  # keeps timing out, never duplicating

    # Once the process finally exits, a relaunch is allowed again.
    proc.exit_after = 0  # next poll() reports the process dead
    mgr.ensure_running(DS4)
    assert len(spawn.calls) == 2


# --- launcher discovery and configurability ---


def test_resolve_launcher_honors_custom_ds4_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom_dir = tmp_path / "custom_ds4"
    custom_dir.mkdir()
    script = custom_dir / "run-ds4f.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)

    monkeypatch.setenv("LOCAL_LEDGER_DS4_HOME", str(custom_dir))

    from local_ledger.llm.local_server import resolve_launcher

    found = resolve_launcher(DS4)
    assert found is not None
    assert Path(found).resolve() == script.resolve()


def test_resolve_launcher_discovers_on_path() -> None:
    from local_ledger.llm.local_server import resolve_launcher

    def fake_which(cmd: str) -> str | None:
        if cmd == "run-ds4f.sh":
            return "/usr/local/bin/run-ds4f.sh"
        return None

    found = resolve_launcher(DS4, which=fake_which)
    assert found == "/usr/local/bin/run-ds4f.sh"


def test_manager_respects_injected_which(tmp_path: Path) -> None:
    detect = FakeDetect(OllamaState.NOT_RUNNING)
    spawn = FakeSpawn(FakeProc())
    custom_calls: list[str] = []

    def tracking_which(cmd: str) -> str | None:
        custom_calls.append(cmd)
        if "run-ds4f.sh" in cmd:
            return "/injected/bin/run-ds4f.sh"
        return None

    mgr = make_manager(detect, spawn, log_dir=tmp_path, which=tracking_which)
    mgr.ensure_running(DS4)
    assert len(spawn.calls) == 1
    assert spawn.calls[0][0][0] == "/injected/bin/run-ds4f.sh"
    assert len(custom_calls) > 0

"""Local LLM server management: profiles, health checks, single-flight startup.

Contract:
- The configured endpoint is probed first; a healthy server serving the
  selected model is adopted as-is — never stopped or replaced, even when it
  was started outside the app.
- GLM and DS4 are explicit profiles over the DwarfStar engine, launched
  through the project's own wrapper scripts (``run-ds4f.sh server`` /
  ``run-glm.sh server``) so streaming, expert-cache, MTP, and memory-guard
  settings always match the working configurations. Ollama remains the
  default profile.
- Health checks and readiness polling use the *effective* endpoint and model
  from ProviderSettings (falling back to profile defaults), so startup and
  inference can never target different configurations.
- Startup is single-flight: a process-wide lock, a tracked "starting" state,
  AND the retained process handle mean Streamlit reruns — or a wait timeout
  while the process still lives — can never launch a duplicate. A new launch
  is only possible after the previous process has exited. (The engine itself
  also refuses duplicate model instances, and a second process could not bind
  the same port — those failures are captured, not fatal.)
- Launch uses an argument list without ``shell=True``; the child is detached
  (``start_new_session``) and writes startup output to a log file that is
  tailed into the failure status.
- Readiness is polled with a bounded timeout and requires the served model to
  verify; an endpoint occupied by a different model is reported, not replaced.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from local_ledger.llm.ollama import OllamaState, detect_state


def get_ds4_home() -> str:
    """Configurable directory for DwarfStar engine launchers."""
    return os.environ.get("LOCAL_LEDGER_DS4_HOME") or os.environ.get("DS4_HOME") or "~/Projects/ds4"


DS4_HOME = get_ds4_home()
DS4_PORT = 8000


@dataclass(frozen=True)
class LocalProfile:
    key: str
    label: str
    base_url: str  # OpenAI-compatible base URL (includes /v1)
    model: str  # model ID the server reports via model listing
    launch: tuple[str, ...]  # argv; argv[0] may contain ~
    launch_env: tuple[tuple[str, str], ...] = ()  # extra env for the launcher
    cwd: str | None = None  # working directory for the launch command
    # False when the launcher fixes the served model (DwarfStar scripts load
    # exactly one GGUF): a custom model setting then cannot be produced by
    # launching and must be rejected instead of silently mismatching.
    model_overrides_ok: bool = True


PROFILES: dict[str, LocalProfile] = {
    "ollama": LocalProfile(
        key="ollama",
        label="Ollama",
        base_url="http://localhost:11434/v1",
        model="llama3.3:8b",
        launch=("ollama", "serve"),
    ),
    "ds4": LocalProfile(
        key="ds4",
        label="DwarfStar (DeepSeek V4 Flash)",
        base_url=f"http://127.0.0.1:{DS4_PORT}/v1",
        model="deepseek-v4-flash",
        # Certified smoke config (~/pi-smoke/RESULTS.md): resident load OOMs
        # on the 58.78 GiB GTT-limited GPU pool, so stream with a bounded
        # expert cache + KV disk cache at ctx 16384 (CTX env is the script's
        # override mechanism). Revisit after raising kernel GTT params.
        launch=(
            f"{DS4_HOME}/run-ds4f.sh",
            "server",
            "--ssd-streaming",
            "--ssd-streaming-cache-experts",
            "16GB",
            "--kv-disk-dir",
            "/tmp/ds4-kv",
            "--kv-disk-space-mb",
            "8192",
        ),
        launch_env=(("CTX", "16384"),),
        model_overrides_ok=False,
    ),
    "glm": LocalProfile(
        key="glm",
        label="DwarfStar (GLM 5.3 Flash)",
        base_url=f"http://127.0.0.1:{DS4_PORT}/v1",
        # The uncensored GLM 5.3 Flash GGUF is LISTED as "glm-5.2" by the
        # server (DSA engine); health checks must use the listed id. Request
        # aliases like "glm-5.3-flash" also work, but only the listed id
        # appears in /v1/models.
        model="glm-5.2",
        # run-glm.sh adds SSD streaming, the 100GB expert cache (self-fits),
        # MTP, and the GLM memory guard; only it should configure those.
        launch=(
            f"{DS4_HOME}/run-glm.sh",
            "server",
            "--kv-disk-dir",
            "/tmp/ds4-kv-glm",
            "--kv-disk-space-mb",
            "8192",
        ),
        model_overrides_ok=False,
    ),
}

DEFAULT_PROFILE_KEY = "ollama"


def probe_root(base_url: str) -> str:
    """Strip the OpenAI ``/v1`` suffix to get the plain server root URL."""
    return base_url.rstrip("/").removesuffix("/v1")


def profile_from_settings(settings: object) -> LocalProfile:
    """The profile selected in settings, falling back to Ollama."""
    key = getattr(settings, "local_profile", DEFAULT_PROFILE_KEY)
    return PROFILES.get(key, PROFILES[DEFAULT_PROFILE_KEY])


def resolve_launcher(
    profile: LocalProfile,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """Absolute path of the profile's launch command, or None when absent.

    Resolves via:
    1. Direct executable path via `which(argv0)`.
    2. PATH lookup of executable basename (e.g. `run-ds4f.sh` on PATH).
    3. Configured/discoverable DS4 directories from `LOCAL_LEDGER_DS4_HOME` / `DS4_HOME` / `~/Projects/ds4`.
    """
    raw_argv0 = profile.launch[0]
    argv0 = os.path.expanduser(raw_argv0)
    found = which(argv0)
    if found:
        return found

    name = Path(argv0).name

    # For DwarfStar scripts (or commands configured without absolute paths), allow
    # PATH discovery or candidate home overrides.
    if profile.key in ("ds4", "glm") or name in ("run-ds4f.sh", "run-glm.sh"):
        found = which(name)
        if found:
            return found
        candidate_homes = [
            os.environ.get("LOCAL_LEDGER_DS4_HOME"),
            os.environ.get("DS4_HOME"),
            "~/Projects/ds4",
        ]
        for home in candidate_homes:
            if not home:
                continue
            cand = str(Path(os.path.expanduser(home)) / name)
            found = which(cand)
            if found:
                return found
    elif "/" not in raw_argv0 and "\\" not in raw_argv0:
        found = which(name)
        if found:
            return found

    return None


@dataclass
class ServerStatus:
    profile: str
    state: str  # "starting" | "ready" | "failed"
    detail: str = ""
    started_at: float = 0.0
    reused: bool = False  # True when an already-running server was adopted
    logs: list[str] = field(default_factory=list)

    @property
    def is_starting(self) -> bool:
        return self.state == "starting"


class LocalServerManager:
    """Single-flight local server starter; safe across Streamlit reruns."""

    def __init__(
        self,
        *,
        detect: Callable[..., OllamaState] = detect_state,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log_dir: Path | None = None,
        poll_interval: float = 0.5,
        startup_timeout: float = 300.0,
        max_log_lines: int = 200,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._lock = threading.Lock()
        self._status: ServerStatus | None = None
        self._process: subprocess.Popen | None = None  # our live launch, if any
        self._detect = detect
        self._spawn = spawn
        self._sleep = sleeper
        self._clock = clock
        self._log_dir = log_dir
        self._poll_interval = poll_interval
        self._startup_timeout = startup_timeout
        self._max_log_lines = max_log_lines
        self._which = which

    def status(self) -> ServerStatus | None:
        with self._lock:
            return self._status

    def _resolve_launcher(self, profile: LocalProfile) -> str | None:
        try:
            return resolve_launcher(profile, which=self._which)
        except TypeError:
            return resolve_launcher(profile)

    def ensure_running(
        self,
        profile: LocalProfile,
        *,
        base_url: str | None = None,
        model: str | None = None,
    ) -> ServerStatus:
        """Adopt a healthy server, or start the profile exactly once.

        ``base_url``/``model`` override the profile defaults with the
        *effective* ProviderSettings values so health checks, startup
        verification, and inference all target the same configuration.

        Never launches while a server is still loading or while a previously
        launched process is still alive (even past the wait timeout); never
        stops or replaces a server that is already running (even a different
        model); never launches when the launch command cannot be resolved.
        """
        root = probe_root(base_url or profile.base_url)
        model_id = model or profile.model
        with self._lock:
            current = self._status
            if current is not None and current.state == "starting":
                return current  # a server is loading; do not launch another
            live = self._process is not None and self._process.poll() is None
            state = self._detect(root, model_id)
            if state == OllamaState.READY:
                self._status = ServerStatus(
                    profile=profile.key,
                    state="ready",
                    detail=f"Connected to the {profile.label} server already running at "
                    f"{root}.",
                    started_at=self._clock(),
                    reused=True,
                )
                return self._status
            if state == OllamaState.MODEL_MISSING:
                self._status = ServerStatus(
                    profile=profile.key,
                    state="failed",
                    detail=(
                        f"Another server is already listening at {root} serving a "
                        f"different model. It was left running; stop it manually before "
                        f"starting {profile.label}."
                    ),
                    started_at=self._clock(),
                )
                return self._status
            # The launchers bind a fixed port (and, for DwarfStar, load a
            # fixed model); a custom endpoint/model that the wrapper cannot
            # produce must be rejected instead of launched-then-mismatched.
            canonical_root = probe_root(profile.base_url)
            model_override = model_id != profile.model
            if root != canonical_root or (model_override and not profile.model_overrides_ok):
                self._status = ServerStatus(
                    profile=profile.key,
                    state="failed",
                    detail=(
                        f"The configured endpoint/model ({root}, {model_id}) does not "
                        f"match what launching {profile.label} would serve "
                        f"({canonical_root}, {profile.model}); restore the profile "
                        "defaults in Settings or start the server manually."
                    ),
                    started_at=self._clock(),
                )
                return self._status
            # Wait on what the launcher actually serves: the canonical
            # endpoint (launchers bind fixed ports) and, for multi-model
            # servers (Ollama), the configured model.
            wait_root = canonical_root
            wait_model = model_id if profile.model_overrides_ok else profile.model
            proc = self._process
            if live and proc is not None:
                # An earlier launch is still alive (typically past the wait
                # timeout on a very slow model load): block any duplicate
                # launch and resume waiting on the existing process.
                status = current or ServerStatus(
                    profile=profile.key, state="starting", started_at=self._clock()
                )
                status.profile = profile.key
                status.state = "starting"
                status.detail = (
                    f"{profile.label} from an earlier launch is still running; "
                    "continuing to wait for it to become healthy…"
                )
                self._status = status
                threading.Thread(
                    target=self._wait_until_ready,
                    args=(proc, status, profile, wait_root, wait_model),
                    daemon=True,
                    name=f"local-server-{profile.key}",
                ).start()
                return status
            launcher = self._resolve_launcher(profile)
            if launcher is None:
                self._status = ServerStatus(
                    profile=profile.key,
                    state="failed",
                    detail=f"Launch command not found: {profile.launch[0]}",
                    started_at=self._clock(),
                )
                return self._status
            argv = [launcher, *(os.path.expanduser(a) for a in profile.launch[1:])]
            cwd = os.path.expanduser(profile.cwd) if profile.cwd else None
            env = {**os.environ, **dict(profile.launch_env)}
            try:
                log_path = self._log_path(profile.key)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                # Detached child, argv list (no shell), output to a log file so
                # the pipe can never fill and block the server. Profile env
                # (e.g. CTX) rides along on top of the current environment.
                with log_path.open("ab") as logf:
                    logf.write(
                        f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting "
                        f"{' '.join(argv)}\n".encode()
                    )
                    proc = self._spawn(
                        argv,
                        cwd=cwd,
                        stdout=logf,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                        env=env,
                    )
            except OSError as exc:
                self._status = ServerStatus(
                    profile=profile.key,
                    state="failed",
                    detail=f"Failed to launch {profile.label}: {exc}",
                    started_at=self._clock(),
                    logs=self._tail_logs(profile.key),
                )
                return self._status
            self._process = proc  # retained: no duplicate launch until it exits
            status = ServerStatus(
                profile=profile.key,
                state="starting",
                detail=f"Started {profile.label}; waiting for it to become healthy…",
                started_at=self._clock(),
            )
            self._status = status
        threading.Thread(
            target=self._wait_until_ready,
            args=(proc, status, profile, wait_root, wait_model),
            daemon=True,
            name=f"local-server-{profile.key}",
        ).start()
        return status

    # ---- internals ----

    def _wait_until_ready(
        self,
        proc: subprocess.Popen,
        status: ServerStatus,
        profile: LocalProfile,
        root: str,
        model_id: str,
    ) -> None:
        deadline = self._clock() + self._startup_timeout
        while True:
            if proc.poll() is not None:
                with self._lock:
                    if self._process is proc:
                        self._process = None  # exited: a relaunch is allowed again
                self._finish(
                    status,
                    "failed",
                    detail=(
                        f"{profile.label} exited during startup "
                        f"(code {proc.returncode}); it is not running."
                    ),
                    profile_key=profile.key,
                )
                return
            state = self._detect(root, model_id)
            if state == OllamaState.READY:
                self._finish(
                    status,
                    "ready",
                    detail=f"{profile.label} is ready at {root}.",
                    profile_key=profile.key,
                )
                return
            if state == OllamaState.MODEL_MISSING:
                # Something else took the endpoint while we were starting
                # (or the server loaded an unexpected model). Never replace it.
                self._finish(
                    status,
                    "failed",
                    detail=(
                        f"The endpoint {root} is now serving a different model; "
                        f"the started {profile.label} process was left alone."
                    ),
                    profile_key=profile.key,
                )
                return
            if self._clock() >= deadline:
                # Bounded give-up for THIS wait round: the process stays alive
                # (large models can simply be slow) and stays tracked, so no
                # duplicate can be launched while it lives.
                self._finish(
                    status,
                    "failed",
                    detail=(
                        f"{profile.label} did not become healthy within "
                        f"{self._startup_timeout:.0f}s; it was left running — check "
                        "the startup logs."
                    ),
                    profile_key=profile.key,
                )
                return
            self._sleep(self._poll_interval)

    def _finish(
        self,
        status: ServerStatus,
        state: str,
        *,
        detail: str,
        profile_key: str,
    ) -> None:
        with self._lock:
            if self._status is not status:
                return  # a newer attempt superseded this one
            status.state = state
            status.detail = detail
            if state == "failed":
                status.logs = self._tail_logs(profile_key)

    def _log_path(self, profile_key: str) -> Path:
        if self._log_dir is not None:
            return self._log_dir / f"local-server-{profile_key}.log"
        from local_ledger.db import DB_PATH, ensure_data_dir

        ensure_data_dir()
        return DB_PATH.parent / f"local-server-{profile_key}.log"

    def _tail_logs(self, profile_key: str) -> list[str]:
        try:
            data = self._log_path(profile_key).read_bytes()
        except OSError:
            return []
        lines = data.decode("utf-8", "replace").splitlines()
        return lines[-self._max_log_lines :]


# Process-wide singleton: Streamlit reruns share this, so a launch attempt is
# ever only in flight once per app process.
MANAGER = LocalServerManager()

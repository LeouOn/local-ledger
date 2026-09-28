"""Reproducible local LLM server smoke runner.

Uses only synthetic input to probe a configured local server profile, measuring
latency, verifying endpoint and model, and returning an explicit verdict:
- PASS: server is ready and replied to synthetic prompt.
- DEFERRED: server or model is not installed/running/ready (no pass assumed).
- FAILED: server encountered an error or timeout during the smoke check.

Never mutates or accesses application databases or user settings.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from openai import OpenAI

from local_ledger.llm.local_server import (
    DEFAULT_PROFILE_KEY,
    PROFILES,
    probe_root,
)
from local_ledger.llm.ollama import (
    OllamaState,
    detect_state,
    guidance_for,
)
from local_ledger.llm.providers import assert_local_endpoint


@dataclass
class SmokeResult:
    profile: str
    model: str
    endpoint: str
    verdict: str  # "PASS" | "DEFERRED" | "FAILED"
    timeout: float
    latency_ms: float | None = None
    detail: str = ""
    output: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_smoke(
    profile_key: str = DEFAULT_PROFILE_KEY,
    *,
    endpoint: str | None = None,
    model: str | None = None,
    timeout: float = 10.0,
    synthetic_prompt: str = "Synthetic smoke check: reply with 'OK' only.",
    state_detector: Callable[[str, str], OllamaState] = detect_state,
    client_factory: Callable[[str, float], Any] | None = None,
) -> SmokeResult:
    """Execute a hermetic smoke check against a local model server."""
    profile = PROFILES.get(profile_key, PROFILES[DEFAULT_PROFILE_KEY])
    base_url = endpoint or profile.base_url
    model_id = model or profile.model

    try:
        assert_local_endpoint(base_url)
    except Exception as exc:
        return SmokeResult(
            profile=profile_key,
            model=model_id,
            endpoint=base_url,
            verdict="FAILED",
            timeout=timeout,
            detail=f"Endpoint validation failed: {exc}",
        )

    root = probe_root(base_url)
    state = state_detector(root, model_id)
    if state != OllamaState.READY:
        guidance = guidance_for(state, profile_key)
        return SmokeResult(
            profile=profile_key,
            model=model_id,
            endpoint=base_url,
            verdict="DEFERRED",
            timeout=timeout,
            detail=f"Model not ready: {state.value} ({guidance})",
        )

    # Server is ready; send synthetic probe
    try:
        t0 = time.perf_counter()
        if client_factory is not None:
            client = client_factory(base_url, timeout)
        else:
            client = OpenAI(base_url=base_url, api_key="ollama", timeout=timeout)

        resp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": synthetic_prompt}],
            max_tokens=16,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        text = resp.choices[0].message.content if resp.choices else ""
        return SmokeResult(
            profile=profile_key,
            model=model_id,
            endpoint=base_url,
            verdict="PASS",
            timeout=timeout,
            latency_ms=round(latency_ms, 2),
            detail="Synthetic prompt completed successfully.",
            output=text.strip() if text else "",
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return SmokeResult(
            profile=profile_key,
            model=model_id,
            endpoint=base_url,
            verdict="FAILED",
            timeout=timeout,
            latency_ms=round(latency_ms, 2),
            detail=f"Request failed: {exc}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local Ledger reproducible local runtime smoke runner."
    )
    parser.add_argument(
        "--profile",
        choices=list(PROFILES),
        default=DEFAULT_PROFILE_KEY,
        help="Local profile key (ollama, ds4, glm)",
    )
    parser.add_argument("--endpoint", default=None, help="Override base URL")
    parser.add_argument("--model", default=None, help="Override model ID")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="Request timeout in seconds"
    )

    args = parser.parse_args(argv)
    result = run_smoke(
        profile_key=args.profile,
        endpoint=args.endpoint,
        model=args.model,
        timeout=args.timeout,
    )

    print(f"Profile:   {result.profile}")
    print(f"Model:     {result.model}")
    print(f"Endpoint:  {result.endpoint}")
    print(f"Timeout:   {result.timeout}s")
    print(f"Verdict:   {result.verdict}")
    if result.latency_ms is not None:
        print(f"Latency:   {result.latency_ms} ms")
    if result.detail:
        print(f"Detail:    {result.detail}")
    if result.output:
        print(f"Output:    {result.output}")

    if result.verdict == "FAILED":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

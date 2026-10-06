"""Launch conformance: the served model, its reasoning dialect and its parser
agree with the model profile, checked before the first pipeline call.

Before this check, launch verified nothing about the model. `continue.sh`
probed readiness with a 1-token chat to `MODEL`; a server serving another id
answered 404 for 20 minutes, after which the launcher printed `starting
pipeline anyway`. Nothing checked that the chat template reads the dialect
variable — an unknown template variable is dropped, not refused — or that the
server's reasoning parser splits reasoning out of the answer.

Three checks, each a pure verdict over what the server returned (so the
recorded responses under `tests/fixtures/model_conformance/` drive the same
code the launch runs):

1. served id   — `GET /v1/models` lists the id `.env` names. Also the
                 readiness probe: the API answers it before inference is up.
2. dialect     — `POST /tokenize` of the same messages under the profile's ON
                 and OFF payloads gives different token ids. Ids, not counts:
                 `low` and `high` are one token each. Identical ids mean the
                 template dropped the variable — the wrong dialect.
3. parser split — one short generation with thinking on returns reasoning on
                 the reasoning channel, an answer in content, and no `<think>`
                 in content. With thinking off, a boolean-dialect model returns
                 no reasoning (or the known Qwen misroute: the answer on the
                 reasoning channel, rerouted by the transport); a strength model
                 still reasons at its lowest rung.

CLI (continue.sh, run_smokes.sh): `python -m pipeline_core.adapters.model_conformance`
exits 0 when all pass and writes `logs/model_conformance.json`, which the
pipeline turns into a `model.conformance` event at run start.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pipeline_core.domain.model_profile import (
    ModelProfile,
    ReasoningDialect,
    UnknownModelError,
    reasoning_payload,
    require_profile,
    resolve_model_knobs,
    served_model,
)

# Exit codes of the CLI, one per failing check, so a launcher can say which.
EXIT_OK = 0
EXIT_UNKNOWN_PROFILE = 2
EXIT_SERVED_ID = 3
EXIT_DIALECT = 4
EXIT_PARSER = 5
EXIT_UNREACHABLE = 6

PROBE_MESSAGES: tuple[dict[str, str], ...] = (
    {"role": "user", "content": "Reply with exactly one word: ready"},
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(slots=True)
class ConformanceReport:
    model: str
    family: str | None
    payload_on: dict[str, Any] = field(default_factory=dict)
    payload_off: dict[str, Any] = field(default_factory=dict)
    checks: list[CheckResult] = field(default_factory=list)
    checked_at: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "family": self.family,
                "payload_on": self.payload_on, "payload_off": self.payload_off,
                "checks": [asdict(c) for c in self.checks],
                "ok": self.ok, "checked_at": self.checked_at}


# ── pure verdicts ────────────────────────────────────────────────────────────


def served_ids(models_json: Mapping[str, Any]) -> list[str]:
    return [str(m.get("id", "")) for m in models_json.get("data", []) or []
            if isinstance(m, Mapping)]


def check_served(model: str, models_json: Mapping[str, Any]) -> CheckResult:
    ids = served_ids(models_json)
    if model in ids:
        return CheckResult("served_id", True, f"{model} is served")
    return CheckResult("served_id", False,
                       f".env names {model!r} but the server serves {ids or 'nothing'}")


def check_dialect(ids_on: list[int] | None, ids_off: list[int] | None,
                  payload_on: Mapping[str, Any], payload_off: Mapping[str, Any]) -> CheckResult:
    if not ids_on or not ids_off:
        return CheckResult("dialect", False, "the server's /tokenize returned no token ids")
    if ids_on == ids_off:
        return CheckResult(
            "dialect", False,
            f"{dict(payload_on)} and {dict(payload_off)} render identical prompts "
            f"({len(ids_on)} tokens): the chat template ignores the variable, so "
            "this is the wrong reasoning dialect for the served model")
    return CheckResult("dialect", True,
                       f"on/off render differently ({len(ids_on)} vs {len(ids_off)} tokens)")


def message_parts(chat_json: Mapping[str, Any]) -> tuple[str, str, str | None]:
    """(content, reasoning, finish_reason) of a non-streamed chat completion.
    vLLM names the reasoning field `reasoning_content` or `reasoning`."""
    choice = (chat_json.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    return str(content), str(reasoning), choice.get("finish_reason")


def check_parser_split(dialect: ReasoningDialect, on_json: Mapping[str, Any],
                       off_json: Mapping[str, Any]) -> CheckResult:
    on_c, on_r, on_fin = message_parts(on_json)
    off_c, off_r, off_fin = message_parts(off_json)
    problems: list[str] = []
    if on_fin != "stop":
        problems.append(f"think-on finish_reason={on_fin!r}")
    if not on_r.strip():
        problems.append("think-on returned no reasoning on the reasoning channel")
    if not on_c.strip():
        problems.append("think-on returned no answer in content")
    for label, text in (("think-on", on_c), ("think-off", off_c)):
        if "<think>" in text or "</think>" in text:
            problems.append(f"{label} content carries a think tag: the parser is not splitting")
    if dialect is ReasoningDialect.ENABLE_THINKING:
        # A real off. Content with no reasoning is the healthy case; the answer
        # alone on the reasoning channel is the measured Qwen misroute the
        # transport reroutes (`VLLMTransport._reasoning_delta`). Both together
        # mean the template did not suppress reasoning.
        if off_c.strip() and off_r.strip():
            problems.append("think-off returned reasoning AND content: the off payload did not suppress reasoning")
        if not off_c.strip() and not off_r.strip():
            problems.append("think-off returned nothing")
    else:
        # The lowest rung still reasons; it must still answer.
        if not off_r.strip():
            problems.append("think-off (lowest rung) returned no reasoning on the reasoning channel")
        if not off_c.strip():
            problems.append("think-off returned no answer in content")
    if problems:
        return CheckResult("parser_split", False, "; ".join(problems))
    return CheckResult("parser_split", True,
                       f"on: {len(on_r)} reasoning / {len(on_c)} content chars; "
                       f"off: {len(off_r)} / {len(off_c)}")


# ── HTTP ─────────────────────────────────────────────────────────────────────


def _root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def fetch_models(base_url: str, timeout: float = 5.0) -> dict[str, Any] | None:
    try:
        r = httpx.get(f"{_root(base_url)}/v1/models", timeout=timeout)
        r.raise_for_status()
        out = r.json()
        return out if isinstance(out, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def tokenize_ids(base_url: str, model: str, payload: Mapping[str, Any]) -> list[int] | None:
    body = {"model": model, "messages": list(PROBE_MESSAGES),
            "add_generation_prompt": True, "chat_template_kwargs": dict(payload)}
    try:
        r = httpx.post(f"{_root(base_url)}/tokenize", json=body, timeout=30.0)
        r.raise_for_status()
        toks = r.json().get("tokens")
    except (httpx.HTTPError, ValueError):
        return None
    return [int(t) for t in toks] if isinstance(toks, list) else None


def generate(base_url: str, model: str, payload: Mapping[str, Any],
             *, max_tokens: int = 8192, timeout: float = 600.0) -> dict[str, Any] | None:
    body = {"model": model, "messages": list(PROBE_MESSAGES), "max_tokens": max_tokens,
            "stream": False, "chat_template_kwargs": dict(payload)}
    try:
        r = httpx.post(f"{_root(base_url)}/v1/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        out = r.json()
        return out if isinstance(out, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def _payloads(profile: ModelProfile, env: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    knobs = resolve_model_knobs(env)
    on = reasoning_payload(profile.dialect, True,
                           strength_on=knobs.strength_on, strength_off=knobs.strength_off)
    off = reasoning_payload(profile.dialect, False,
                            strength_on=knobs.strength_on, strength_off=knobs.strength_off)
    return dict(on), dict(off)


def run_conformance(env: Mapping[str, str], *, deadline_s: float = 1200.0,
                    log: Callable[[str], None] = print,
                    sleep: Callable[[float], None] = time.sleep) -> tuple[int, ConformanceReport]:
    """Wait for the API, then run the three checks in order; stop at the first
    failure. Returns (exit code, report)."""
    model = served_model(env)
    base = (env.get("VLLM_BASE_URL") or "http://localhost:8000/v1").strip()
    report = ConformanceReport(model=model, family=None,
                               checked_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        profile = require_profile(model, env.get("MODEL_FAMILY"))
    except UnknownModelError as exc:
        report.checks.append(CheckResult("profile", False, str(exc)))
        return EXIT_UNKNOWN_PROFILE, report
    report.family = profile.family
    report.payload_on, report.payload_off = _payloads(profile, env)

    t0 = time.monotonic()
    models: dict[str, Any] | None = None
    while models is None:
        models = fetch_models(base)
        if models is not None:
            break
        if time.monotonic() - t0 > deadline_s:
            report.checks.append(CheckResult(
                "served_id", False, f"{base}/models did not answer in {deadline_s:.0f}s"))
            return EXIT_UNREACHABLE, report
        sleep(5.0)
    served = check_served(model, models)
    report.checks.append(served)
    log(f"  [conformance] served id: {served.detail}")
    if not served.ok:
        return EXIT_SERVED_ID, report

    dialect = check_dialect(tokenize_ids(base, model, report.payload_on),
                            tokenize_ids(base, model, report.payload_off),
                            report.payload_on, report.payload_off)
    report.checks.append(dialect)
    log(f"  [conformance] dialect {profile.dialect.value}: {dialect.detail}")
    if not dialect.ok:
        return EXIT_DIALECT, report

    # Inference comes up after the API (CUDA graph capture): retry the first
    # generation until the deadline rather than reading a 5xx as a verdict.
    on_json = generate(base, model, report.payload_on)
    while on_json is None and time.monotonic() - t0 <= deadline_s:
        sleep(5.0)
        on_json = generate(base, model, report.payload_on)
    off_json = generate(base, model, report.payload_off) if on_json is not None else None
    if on_json is None or off_json is None:
        report.checks.append(CheckResult(
            "parser_split", False, f"no completion from {base} within {deadline_s:.0f}s"))
        return EXIT_UNREACHABLE, report
    split = check_parser_split(profile.dialect, on_json, off_json)
    report.checks.append(split)
    log(f"  [conformance] parser split: {split.detail}")
    return (EXIT_OK if split.ok else EXIT_PARSER), report


# ── run start: the startup line and the event ────────────────────────────────

REPORT_PATH = Path("logs") / "model_conformance.json"


def report_at_run_start(env: Mapping[str, str], *, log: Callable[[str], None],
                        emit: Callable[..., None], report_path: Path = REPORT_PATH) -> None:
    """Print the model profile line (family, each knob and its source), warn
    on an uncalibrated profile, and emit `model.conformance` from the launch's
    report — `status: absent` when no report names this served id."""
    knobs = resolve_model_knobs(env)
    log(f"  {knobs.summary()}")
    if knobs.sources().get("VLLM_RUNAWAY_THINK_CHARS") == "uncalibrated":
        log(f"  [model-profile] WARNING: {knobs.family} has no measured runaway cap: "
            "per_call reasoning runs until it fills the context")
    try:
        saved = json.loads(report_path.read_text())
    except (OSError, ValueError):
        saved = None
    if not isinstance(saved, dict) or saved.get("model") != knobs.model:
        emit("model.conformance", model=knobs.model, family=knobs.family, status="absent")
        return
    emit("model.conformance", status="pass" if saved.get("ok") else "fail", **{
        k: saved.get(k) for k in ("model", "family", "payload_on", "payload_off",
                                  "checks", "checked_at")})


def _load_env(env_file: Path) -> dict[str, str]:
    from dotenv import dotenv_values
    # The process env wins, as `load_dotenv` (no override) gives the pipeline.
    merged = {k: v for k, v in dotenv_values(env_file).items() if v is not None}
    merged.update(os.environ)
    return merged


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--deadline", type=float, default=1200.0,
                    help="seconds to wait for the API and the first completion")
    ap.add_argument("--report", default=str(REPORT_PATH))
    args = ap.parse_args(argv)
    code, report = run_conformance(_load_env(Path(args.env_file)), deadline_s=args.deadline)
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.as_dict(), indent=2) + "\n")
    if code != EXIT_OK:
        failed = next((c for c in report.checks if not c.ok), None)
        print(f"[conformance] REFUSED ({failed.name if failed else '?'}): "
              f"{failed.detail if failed else ''}", file=sys.stderr)
    else:
        print(f"[conformance] {report.model} ({report.family}): all checks pass")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

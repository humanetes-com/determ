#!/usr/bin/env python3
"""
Pipeline for the deterministic process loop.

Usage:
  python pipeline.py --project <subfolder>
"""

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from functools import partial as _partial
from pathlib import Path
from typing import TypeVar

_RT = TypeVar("_RT")

import importlib

from dotenv import load_dotenv

import model_interact as _mi
from model_interact import MODEL
from model_interact import ask as _ask_raw
from pipeline_core import log as _pclog
from pipeline_core import retry as _pcretry
from pipeline_core.adapters import build_adapters
from pipeline_core.adapters import coverage as _covmod
from pipeline_core.adapters import state as _state_mod
from pipeline_core.adapters import workspace as _wsmod
from pipeline_core.adapters.coverage import CoverageDeps
from pipeline_core.adapters.workspace import WorkspaceDeps
from pipeline_core.application.refine_loop import run_refine_loop as _run_refine_loop
from pipeline_core.application.tasks.genesis_audit import (
    GenesisAuditDeps,
    genesis_audit,
)
from pipeline_core.application.tasks.task0_docs import (
    Task0DocsDeps,
)
from pipeline_core.application.tasks.task0_docs import (
    task0_project_context as _task0_context_impl,
)
from pipeline_core.application.tasks.task0_docs import (
    task0_project_profile as _task0_profile_impl,
)
from pipeline_core.application.tasks.task0b_discovery import (
    Task0bDeps,
)
from pipeline_core.application.tasks.task0b_discovery import (
    task0b_discovery as _task0b_impl,
)
from pipeline_core.application.tasks.task0c import Task0cDeps
from pipeline_core.application.tasks.task0c import task0c_augment_module as _task0c_impl
from pipeline_core.application.tasks.task1 import Task1Deps
from pipeline_core.application.tasks.task1 import task1 as _task1_impl
from pipeline_core.application.tasks.task2 import (
    Task2Deps,
)
from pipeline_core.application.tasks.task2 import (
    task2 as _task2_impl,
)
from pipeline_core.application.tasks.task2 import (
    task2_fix_loop as _task2_fix_impl,
)
from pipeline_core.application.tasks.task2 import (
    task2_inner_loop as _task2_inner_impl,
)
from pipeline_core.application.tasks.task3 import Task3Deps
from pipeline_core.application.tasks.task3 import task3 as _task3_impl
from pipeline_core.application.tasks.task4 import Task4Deps
from pipeline_core.application.tasks.task4 import task4 as _task4_impl
from pipeline_core.application.workspace_lifecycle import (
    best_tree_pass_count,
    restore_best_tree,
    save_best_tree,
)
from pipeline_core.application.workspace_lifecycle import (
    save_gen_tests_siblings as _wl_save_gen_tests_siblings,
)
from pipeline_core.application.workspace_lifecycle import (
    wipe_module_sources as _wl_wipe_module_sources,
)
from pipeline_core.domain.enums import (
    Backend,
    CoverageBackend,
    FillUnit,
    FixStrategy,
    PhaseCDriver,
    PhaseCMode,
)
from pipeline_core.domain.llm_output import (
    GENESIS_DELIM_END,
    GENESIS_DELIM_START,
    _extract_genesis,
    extract_files,
)
from pipeline_core.domain.model_caps import (
    model_context_chars as _model_context_chars_pure,
)
from pipeline_core.domain.model_caps import (
    model_tier as _model_tier_pure,
)
from pipeline_core.domain.models import (
    BuildCluster,
    ConstRecord,
    Diagnostic,
    DispatchFamily,
    Frontier,
    ModRecord,
    ModuleAugmentState,
    TypeRecord,
)
from pipeline_core.domain.refine import (
    Attempt as _Attempt,
)
from pipeline_core.domain.refine import FloorStall as _FloorStall
from pipeline_core.domain.refine import (
    LoopState as _LoopState,
)
from pipeline_core.domain.test_results import (
    OUTPUT_SEPARATOR,
    _parse_test_counts,
    build_test_name_index,
    count_passing_tests,
    extract_test_output_from_file,
    failing_test_names,
    identifiers_in,
    per_file_test_stats,
    validate_task3_output_strict,
)
from pipeline_core.domain.text import (
    extract_code_content,
    smart_truncate,
    trim_to_line,
)
from pipeline_core.domain.text import (
    strip_markdown_fences as _strip_markdown_fences,
)
from pipeline_core.io import read, write
from pipeline_core.prompts import runtime as _prompts_runtime
from pipeline_core.prompts.per_fn import (
    build_per_fn_fix_prompt as _build_per_fn_fix_prompt,
)
from pipeline_core.prompts.per_fn import (
    build_per_fn_prompt as _build_per_fn_prompt,
)
from pipeline_core.prompts.per_impl import (
    build_per_impl_prompt as _build_per_impl_prompt,
)
from pipeline_core.prompts.repo_spec import (
    RepoSpecDocs,
    identifiers_for_target,
    render_repo_spec,
)
from pipeline_core.prompts.sections import (
    build_dispatch_family_section as _build_dispatch_family_section_pure,
)
from pipeline_core.prompts.sections import (
    format_type_for_prompt as _format_type_for_prompt,
)
from pipeline_core.prompts.sections import (
    gotchas_for_error as _gotchas_for_error,
)
from pipeline_core.prompts.sections import (
    gotchas_summary as _gotchas_summary,
)
from pipeline_core.prompts.sections import (
    tag_block as _tag_block,
)
from pipeline_core.retry import with_retry
from pipeline_core.settings import Settings
from strip_to_tests import detect_language, process_rust

# Names re-exported through pipeline.py for backward-compatible call sites + the smoke
# suite (which references them as pipeline.X). Declaring them here documents the public
# surface and tells the linter these imports are intentional re-exports, not dead code.
__all__ = [
    "count_passing_tests", "validate_task3_output_strict", "extract_test_output_from_file",
    "GENESIS_DELIM_START", "GENESIS_DELIM_END", "_extract_genesis",
    "_tag_block",
]

_mi_mtime: float = os.path.getmtime(_mi.__file__)


def _hot_reload_mi() -> None:
    """Reload model_interact.py if it changed on disk since last ask() call."""
    global _ask_raw, _mi_mtime
    try:
        mtime = os.path.getmtime(_mi.__file__)
        if mtime != _mi_mtime:
            importlib.reload(_mi)
            _ask_raw  = _mi.ask
            _mi_mtime = mtime
            _log("  [hot-reload] model_interact.py reloaded")
    except Exception as e:
        _log(f"  [hot-reload] reload failed: {e}")

load_dotenv(Path(__file__).parent / ".env")

# Composition root: resolve all env-driven knobs once into one typed object.
# Everything downstream reads SETTINGS rather than os.environ for these values.
# (Per-task numeric tuning knobs still live as their own getenv reads for now.)
SETTINGS = Settings.from_env(model=MODEL)

# Per-task model overrides — each task uses the most appropriate model.
# Single-sourced from Settings.task_models (env TASK*_MODEL, falling back to MODEL).
# Rationale for each choice is documented in .env.
_TASK0_MODEL  = SETTINGS.task_models.task0
_TASK0C_MODEL = SETTINGS.task_models.task0c
_TASK1_MODEL  = SETTINGS.task_models.task1
_TASK2_MODEL  = SETTINGS.task_models.task2
_TASK4_MODEL  = SETTINGS.task_models.task4


def _model_tier(model: str) -> str:
    """Thin shim → pipeline_core.domain.model_caps.model_tier (returns 'mid'/'weak')."""
    return str(_model_tier_pure(model))


_TASK0C_TIER = _model_tier(_TASK0C_MODEL)
_TASK1_TIER  = _model_tier(_TASK1_MODEL)
_TASK2_TIER  = _model_tier(_TASK2_MODEL)
_TASK4_TIER  = _model_tier(_TASK4_MODEL)

# Thinking directive (mid-tier only): injected at the start of generation prompts.
# Guides WHAT to reason about during the model's thinking budget before writing code.
# Omitted for weak-tier — they don't have a thinking budget; chain-of-thought in the
# prompt just adds tokens to the output rather than improving reasoning.
_TASK2_THINKING_DIRECTIVE = (
    "Before writing code: reason through all required public types, trait impls, "
    "module structure, and re-exports against the API surface and test assertions. "
    "Then write complete, compilable source.\n\n"
    if _TASK2_TIER == "mid" else ""
)

_FIX_THINKING_DIRECTIVE = (
    "Before writing fixes: trace the error to its root cause — identify the specific "
    "type mismatch, missing impl, or wrong signature — then write a targeted correction.\n\n"
    if _TASK2_TIER == "mid" else ""
)

# Verbosity guard (weak-tier only): appended after FILE_FORMAT_INSTRUCTION.
# Weak-tier models sometimes emit prose outside FILE markers despite the instruction;
# this explicit reminder reduces that failure mode. Mid-tier models follow reliably.
_TASK2_VERBOSITY_GUARD = (
    ""
    if _TASK2_TIER == "mid"
    else "Output ONLY the FILE markers above — no prose, no markdown fences, no text outside markers.\n"
)

# Task 1: requirements / contract extraction.
# Thinking directive guides the model to trace every behavioral contract to a specific
# test assertion before writing, preventing omissions and hallucination.
_TASK1_THINKING_DIRECTIVE = (
    "Before writing requirements: for each test assertion, trace exactly what behavior "
    "it verifies — function name, arguments, return value, invariants. Miss nothing.\n\n"
    if _TASK1_TIER == "mid" else ""
)

# Task 0c: coverage test generation.
# Thinking directive guides planning: which input values cover which branches, what
# assertion verifies each result. Prevents tests that execute code but don't assert.
_TASK0C_THINKING_DIRECTIVE = (
    "Before writing tests: for each uncovered function, determine which input values "
    "or states exercise each branch, and what assertion verifies the result. Then write.\n\n"
    if _TASK0C_TIER == "mid" else ""
)

# Task 0c: compile / test-block fix.
# Directs the model to identify the error class before writing the fix.
_TASK0C_FIX_THINKING_DIRECTIVE = (
    "Before writing the fix: identify the exact error — wrong type, wrong path, missing "
    "impl, bad import — then write the minimal correction.\n\n"
    if _TASK0C_TIER == "mid" else ""
)

# Task 4: genesis update.
# The prompts already prescribe a step-by-step analysis structure, so no thinking
# directive is added. Only a verbosity guard for weak-tier models that might miss the
# custom GENESIS_DELIM_START / GENESIS_DELIM_END markers.
_TASK4_VERBOSITY_GUARD = (
    ""
    if _TASK4_TIER == "mid"
    else "Use ONLY the delimiters shown above for output — no text outside them.\n"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Source code file extensions — the ONLY file types that may be created or overwritten
# by the model. Everything else (Cargo.toml, *.toml, *.json, *.proto, *.yaml, …) is
# unconditionally protected so the AI can never corrupt workspace config files.
SOURCE_EXTENSIONS = {
    # Systems / compiled
    ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".go", ".swift", ".kt", ".kts", ".java", ".cs", ".vb", ".fs", ".fsx",
    ".d", ".nim", ".cr", ".zig", ".v", ".odin",
    # Scripting
    ".py", ".rb", ".pl", ".pm", ".php", ".php3", ".php4", ".php5", ".phtml",
    ".lua", ".sh", ".bash", ".zsh", ".fish", ".tcl",
    # JVM / functional
    ".scala", ".sc", ".clj", ".cljs", ".cljc", ".groovy", ".gvy", ".gradle",
    ".ex", ".exs", ".hs", ".lhs", ".ml", ".mli", ".mll", ".mly",
    ".erl", ".hrl", ".elm", ".purs",
    # Web / JS
    ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx",
    ".coffee", ".vue", ".svelte",
    # Database / query
    ".sql",
    # Other
    ".r", ".R", ".m", ".mm", ".f", ".f90", ".f95", ".f03", ".f08",
    ".ada", ".adb", ".ads", ".pas", ".pp",
}

MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Model-aware context budget (auto-detected from environment)
# ---------------------------------------------------------------------------
# start.sh already sets OLLAMA_NUM_CTX in .env based on measured free VRAM.
# Here we read it back and derive all content limits from it so that they
# automatically adapt to any hardware change without manual edits.
#
# Derivation: (num_ctx_tokens - prompt_overhead) * chars_per_token
#   prompt_overhead ≈ 6k tokens (instructions + system prompt + response buffer)
#   chars_per_token ≈ 4 (conservative; typical Rust/English source is 3.5–4.5)
#
# Hardware reference:
#   RTX 3090 24 GB, qwen3:32b Q4_K_M (~20 GB): OLLAMA_NUM_CTX=32768 → ~104k chars budget
#   Claude API (200k tokens):                                         → ~776k chars budget
#   Fallback (8192 tokens):                                           → ~26k chars budget

def _model_context_chars() -> int:
    """Return the safe content budget in chars for one model call.

    Thin shim: resolves env live → pipeline_core.domain.model_caps.model_context_chars.
    Reads env per-call (not SETTINGS) so late env changes recompute the budget.
    """
    backend = Backend.coerce(os.getenv("BACKEND"), Backend.OLLAMA)
    var = backend.max_len_env_var
    raw = os.getenv(var) if var else None
    max_len = int(raw) if raw and raw.strip().isdigit() else None
    return _model_context_chars_pure(MODEL, backend, max_len)


MODEL_CONTEXT_CHARS = _model_context_chars()

_GUARD_LOG        = os.getenv("PIPELINE_GUARD_LOG",   str(Path(__file__).parent / "logs" / "pipeline_guards.log"))
_PIPELINE_LOG     = os.getenv("PIPELINE_LOG",         str(Path(__file__).parent / "logs" / "pipeline_anyhow.log"))
_LOG_MAX_BYTES    = 512 * 1024   # keep last 512 KB in pipeline_anyhow.log
_LOG_TRIM_EVERY   = 50           # check file size every N _log() calls
_log_trim_counter = 0
_RUN_REGISTRY     = Path(os.getenv("PIPELINE_RUN_REGISTRY", "/tmp/determ_pipelines.json"))


def _trim_pipeline_log() -> None:
    """Truncate logs/pipeline_anyhow.log to _LOG_MAX_BYTES by dropping the head."""
    try:
        path = _PIPELINE_LOG
        size = os.path.getsize(path)
        if size <= _LOG_MAX_BYTES:
            return
        with open(path, "r+b") as fh:
            fh.seek(-_LOG_MAX_BYTES, 2)
            tail = fh.read()
            nl = tail.find(b"\n")
            if nl >= 0:
                tail = tail[nl + 1:]
            fh.seek(0)
            fh.write(tail)
            fh.truncate()
    except Exception:
        pass


def _guard(location: str, msg: str) -> None:
    """Log a content truncation event to both the pipeline log and the guard log file.

    Format: GUARD: [location] msg
    Grep `GUARD:` across pipeline log + guard log to find all truncations in one place.
    Also emits a `guard.fired` structured event so the JSONL log captures truncations.
    """
    import datetime
    line = f"  GUARD: [{location}] {msg}"
    _log(line)
    try:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        with open(_GUARD_LOG, "a") as fh:
            fh.write(f"[{ts}] {line}\n")
    except Exception:
        pass
    _emit("guard.fired", data={"location": location, "msg": msg})


# ── Structured event log ───────────────────────────────────────────────────────
# Append-only JSONL at logs/events.jsonl. One JSON object per line. Cheap to
# write, crash-safe, tail-friendly, easy to ingest into SQLite later.
#
# Fixed schema fields (see backlog "Structured event log for pipeline observability"):
#   ts             ISO 8601 UTC, ms precision
#   product        e.g. "anyhow"  — set once by init_events()
#   pipeline_sha   determ HEAD short SHA — set once by init_events()
#   event          dot.notation verb: "llm.request", "coverage.measured", ...
#   (optional)     task, cluster, module, file, attempt, duration_ms, data
#
# Prompts/responses are content-addressed to logs/prompts/{sha256}.txt to keep
# events.jsonl small. ~90% of context is repeated across calls (genesis,
# profile, examples) so dedup is strong.

_EVENTS_LOG  = os.getenv("PIPELINE_EVENTS_LOG",
                         str(Path(__file__).parent / "logs" / "events.jsonl"))
_PROMPTS_DIR = Path(__file__).parent / "logs" / "prompts"
_RUN_CONTEXT: dict[str, str] = {"product": "unknown", "pipeline_sha": "unknown"}


def init_events(product: str) -> None:
    """Initialize the event log: cache the product name and determ HEAD SHA."""
    _RUN_CONTEXT["product"] = product
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        _RUN_CONTEXT["pipeline_sha"] = result.stdout.strip() or "unknown"
    except Exception:
        _RUN_CONTEXT["pipeline_sha"] = "unknown"
    with suppress(Exception):
        _PROMPTS_DIR.mkdir(parents=True, exist_ok=True)


def _store_prompt_content(text: str) -> dict[str, int | str]:
    """Content-address a prompt/response. Returns {sha256, char_count}.

    Writes to logs/prompts/{sha256}.txt if not already present. Never raises.
    """
    if not text:
        return {"sha256": "", "char_count": 0}
    h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    path = _PROMPTS_DIR / f"{h}.txt"
    if not path.exists():
        with suppress(Exception):
            path.write_text(text, encoding="utf-8", errors="replace")
    return {"sha256": h, "char_count": len(text)}


def _emit(event: str, **fields) -> None:
    """Append a structured event to logs/events.jsonl. Never raises."""
    try:
        record: dict = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "product": _RUN_CONTEXT.get("product", "unknown"),
            "pipeline_sha": _RUN_CONTEXT.get("pipeline_sha", "unknown"),
            "event": event,
        }
        for k, v in fields.items():
            if v is not None:
                record[k] = v
        with open(_EVENTS_LOG, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass  # observability must never crash the pipeline


@contextmanager
def _phase(prefix: str, **fields):
    """Emit `{prefix}.start` on enter and `{prefix}.done` on exit with auto duration_ms.

    Yields a mutable dict the caller can update; the keys become extra fields on
    `.done`. Top-level keys (task, cluster, module, attempt, data) merge with
    `fields`; the `data` dict deep-merges so callers can add per-result info.

    On exception, `.done` is still emitted with `data.error = <ExceptionName>` so
    every `.start` has a matching `.done`. The exception is re-raised.
    """
    t0 = time.monotonic()
    _emit(f"{prefix}.start", **fields)
    extra: dict = {}
    err: str | None = None
    try:
        yield extra
    except BaseException as _e:
        err = type(_e).__name__
        raise
    finally:
        merged: dict = {**fields, **{k: v for k, v in extra.items() if k != "data"}}
        merged_data: dict = {**(fields.get("data") or {}), **(extra.get("data") or {})}
        if err is not None:
            merged_data["error"] = err
        if merged_data:
            merged["data"] = merged_data
        merged["duration_ms"] = int((time.monotonic() - t0) * 1000)
        _emit(f"{prefix}.done", **merged)


# Set once per run by _register_run; used to build the from-scratch disclaimer.
_CRATE_DISPLAY_NAME = ""


def _from_scratch_disclaimer() -> str:
    """Anti-leakage system note: our impl is independent of any public crate of the same name.

    Local models often 'know' a popular crate (anyhow, serde, …) and silently reconstruct
    its real internals from memory. That defeats tests-only reconstruction. Stated on every
    call so any code-viewing step is reminded the bytes it sees are ours, not upstream's.
    """
    name = _CRATE_DISPLAY_NAME or "this crate"
    return (
        f"This project reuses the name `{name}`, but the implementation has been written "
        f"entirely from scratch and is COMPLETELY DIFFERENT from any public library known as "
        f"`{name}`. Do NOT assume types, signatures, internal structure, or behavior from the "
        f"upstream `{name}`. Reason ONLY from the code, signatures, and tests shown to you; "
        f"the structure of names and modules may differ from what you remember."
    )


def ask(prompt: str, system: str | None = None, model: str | None = None,
        max_tokens: int | None = None, think: bool | None = None) -> str:
    """Drop-in wrapper around model_interact.ask that enforces context limits.

    BL-015: Section-aware context guard.
    When the prompt overflows MODEL_CONTEXT_CHARS, we first try to drop whole
    low-priority sections (identified by their delimiter markers) before falling
    back to blind middle-trimming. This preserves structural integrity — we drop
    a complete integration-test file rather than cutting a source file in half.

    Sections are dropped in reverse priority order:
      1. <TESTFILE ...> blocks (integration tests — lowest priority in overflow)
      2. `// --- filename ---` blocks (sibling source files)
      3. <FILE ...> blocks that appear in the middle (not the last one — keep target)
      4. Fall back to middle-trim if still over budget.
    """
    HEAD_KEEP = MODEL_CONTEXT_CHARS // 4
    TAIL_KEEP = MODEL_CONTEXT_CHARS // 8
    MARGIN = 1_000  # small safety buffer only — prompt builders handle their own budgets

    _disclaimer = _from_scratch_disclaimer()
    system = f"{_disclaimer}\n\n{system}" if system else _disclaimer
    sys_len = len(system) if system else 0
    total = sys_len + len(prompt)

    if total > MODEL_CONTEXT_CHARS - MARGIN:
        budget = MODEL_CONTEXT_CHARS - MARGIN - sys_len

        # --- BL-015: section-aware dropping ---
        def _prompt_fits(p: str) -> bool:
            return sys_len + len(p) <= MODEL_CONTEXT_CHARS - MARGIN

        trimmed = prompt

        # Pass 1: drop <TESTFILE> blocks (whole blocks) starting from the last one.
        if not _prompt_fits(trimmed):
            testfile_re = re.compile(r'\n<TESTFILE path="[^"]*">.*?</TESTFILE>', re.DOTALL)
            blocks = list(testfile_re.finditer(trimmed))
            for blk in reversed(blocks):
                trimmed = trimmed[:blk.start()] + trimmed[blk.end():]
                if _prompt_fits(trimmed):
                    break

        # Pass 2: drop `// --- sibling_file ---\n...` blocks (end to start).
        if not _prompt_fits(trimmed):
            sib_re = re.compile(r'\n// --- [^\n]+ ---\n[^/]*?(?=\n// ---|$)', re.DOTALL)
            sib_blocks = list(sib_re.finditer(trimmed))
            for blk in reversed(sib_blocks):
                trimmed = trimmed[:blk.start()] + trimmed[blk.end():]
                if _prompt_fits(trimmed):
                    break

        # Pass 3: drop middle <FILE> blocks (keep first and last, drop intermediates).
        if not _prompt_fits(trimmed):
            file_re = re.compile(r'\n<FILE path="[^"]*">.*?</FILE>', re.DOTALL)
            file_blocks = list(file_re.finditer(trimmed))
            for blk in file_blocks[1:-1]:  # skip first and last
                trimmed = trimmed[:blk.start()] + trimmed[blk.end():]
                if _prompt_fits(trimmed):
                    break

        if _prompt_fits(trimmed):
            dropped_chars = len(prompt) - len(trimmed)
            _guard("context-guard/section_removal",
                   f"dropped {dropped_chars:,} chars via section removal "
                   f"(prompt was {total:,} chars, limit {MODEL_CONTEXT_CHARS:,})")
            prompt = trimmed
        else:
            # Fall back to middle-trimming.
            if budget < HEAD_KEEP + TAIL_KEEP + 200:
                budget = max(200, budget)
                prompt = trimmed[:budget]
                _guard("context-guard/severe", f"only {budget:,} chars available — hard-truncating prompt")
            else:
                head = trimmed[:HEAD_KEEP]
                tail = trimmed[max(HEAD_KEEP, len(trimmed) - TAIL_KEEP):]
                dropped = len(trimmed) - HEAD_KEEP - len(tail)
                prompt = (
                    head
                    + f"\n\n[... {dropped:,} chars trimmed by context-guard to fit "
                    f"{MODEL_CONTEXT_CHARS:,}-char context window ...]\n\n"
                    + tail
                )
                _guard("context-guard/middle_trim",
                       f"prompt {total:,} > effective limit {MODEL_CONTEXT_CHARS - MARGIN:,} (context={MODEL_CONTEXT_CHARS:,} margin={MARGIN:,}): dropped {dropped:,} chars from middle")

    kwargs: dict = {}
    if model is not None:
        kwargs["model"] = model
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if think is not None:
        kwargs["think"] = think
    _hot_reload_mi()

    _prompt_meta = _store_prompt_content(prompt)
    _system_meta = _store_prompt_content(system) if system else None
    # Budget-utilization warning: flag prompts that consume most of the input
    # window so silent overflows surface in logs.
    _prompt_len = len(prompt) + (len(system) if system else 0)
    if _prompt_len > SINGLE_CALL_CHAR_LIMIT * 90 // 100:
        _guard("prompt/budget_high",
               f"prompt {_prompt_len:,} chars = "
               f"{_prompt_len * 100 // max(1, SINGLE_CALL_CHAR_LIMIT)}% of "
               f"SINGLE_CALL_CHAR_LIMIT ({SINGLE_CALL_CHAR_LIMIT:,})")
    _emit("llm.request", data={
        "model": model or MODEL,
        "prompt": _prompt_meta,
        "system": _system_meta,
        "max_tokens": max_tokens,
    })
    _t0 = time.monotonic()
    for _net_attempt in range(3):
        try:
            _response = _ask_raw(prompt, system, **kwargs)
            _dur_ms = int((time.monotonic() - _t0) * 1000)
            _emit("llm.response", duration_ms=_dur_ms,
                  data={"prompt_sha": _prompt_meta["sha256"],
                        "response": _store_prompt_content(_response or "")})
            return _response
        except Exception as _e:
            _ename = type(_e).__name__
            if _net_attempt < 2 and any(w in _ename for w in ("Timeout", "Connect", "Protocol", "Network")):
                _guard("ask/network_retry", f"attempt={_net_attempt} err={_ename}: {_e}")
                time.sleep(10)
                continue
            _emit("llm.error", duration_ms=int((time.monotonic() - _t0) * 1000),
                  data={"error_type": _ename, "error_msg": str(_e)[:500]})
            raise
    raise RuntimeError("ask: retry loop exhausted")


# Maximum safe input chars, derived mathematically from the KV cache budget.
#
# model_interact._cap() guarantees at least min_content_tok output tokens when:
#   input_chars ≤ (ctx - thinking_budget - 512 - min_content_tok) × cpt_floor
#
# cpt_floor=3.0 matches the floor in model_interact._estimated_input_tokens (line ~846).
# Actual measured qwen3 cpt on Rust code is 3.1–3.6 consistently.
#
# History: the old empirical cap of 120_000 was a workaround for cpt_floor=2.0.
# With cpt=2.0 and a 199K char prompt (ptr.rs observed case), estimated input tokens
# = 99K > VLLM_MAX_MODEL_LEN=98304, so _cap() hit its 512-token minimum, leaving
# almost no content budget → model could not write a test block → empty response.
#
# REVERT if empty responses return or model output degrades (session 26 added this):
#   1. In model_interact.py ~line 846: max(3.0, _vllm_cpt) → max(2.0, _vllm_cpt)
#   2. Override the budget in Settings.from_env with the old empirical cap:
#        _THINKING_MODEL_CHAR_LIMIT = 120_000
#        single_call_char_limit = min(_THINKING_MODEL_CHAR_LIMIT, mcc * 3 // 4)
#      (the safe-input math now lives in domain.model_caps.safe_input_char_limit).
# slice 5b/Settings: the safe-input + single-call char budgets are now computed once in
# Settings.from_env (pipeline_core.settings, via domain.model_caps.safe_input_char_limit).
# These constants are single-sourced from SETTINGS; call sites are unchanged.
_SAFE_INPUT_CHAR_LIMIT        = SETTINGS.safe_input_char_limit
_SAFE_INPUT_CHAR_LIMIT_TASK0C = SETTINGS.safe_input_char_limit_task0c
SINGLE_CALL_CHAR_LIMIT        = SETTINGS.single_call_char_limit

# Supplementary material caps — scale with context so they don't freeze at
# values tuned for an old (small) context window.
_README_CAP    = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # ~5% of budget
_PROFILE_CAP   = max(2_000, SINGLE_CALL_CHAR_LIMIT // 30)   # ~3% of budget
_GENESIS_CAP   = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # methodology doc
_FIX_ERR_CAP   = max(2_000, SINGLE_CALL_CHAR_LIMIT // 10)   # per-attempt error in fix loop
_REPO_SPEC_CAP = max(2_500, SINGLE_CALL_CHAR_LIMIT // 10)   # REPOSITORY_SPECIFICATIONS in per-fn/per-impl prompts
_REPO_SPEC_CAP_WIDE = max(5_000, SINGLE_CALL_CHAR_LIMIT // 5)  # …in whole-file fix prompts (bigger budget)
_STRATEGY_CAP  = max(4_000, SINGLE_CALL_CHAR_LIMIT // 15)   # coverage strategy doc
_SIBLING_CAP   = max(3_000, SINGLE_CALL_CHAR_LIMIT // 30)   # per-sibling condensed source
_SRC_SNIP_CAP  = max(4_000, SINGLE_CALL_CHAR_LIMIT // 10)   # source snippet in task0c prompts
_EXTRA_CAP     = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # extra context files (Cargo.toml etc.)
_WORKING_CAP   = max(800,   SINGLE_CALL_CHAR_LIMIT // 100)  # "what worked before" hint

# Per-fn / per-impl reconstruction context (Plan 9 F1/F2). Types+traits are the
# highest-value context for body reconstruction — they drive the dominant
# E0277/E0599/E0046/E0053 errors. Budget-derive generously; do NOT freeze at
# 8192-era hardcoded values (3000 chars / 6 types). The char cap governs; the
# count cap is only a runaway safety net.
_TYPES_SECTION_CAP = max(8_000, SINGLE_CALL_CHAR_LIMIT // 3)   # type/trait defs in fill prompt
_TYPES_COUNT_CAP   = max(40,    SINGLE_CALL_CHAR_LIMIT // 6000)  # safety net only; chars govern
_FAILED_BODY_CAP   = max(2_000, SINGLE_CALL_CHAR_LIMIT // 12)  # prior failed body in fix prompt

# Plan 10 U2: test-based acceptance. When on, a per-fn fill is accepted only if
# the fn's OWN tests pass (not just compile); test failures route through the
# same fix loop as compile errors. Default OFF — identical to legacy behavior —
# so it can be validated live before enabling. Fail-safe: any error in the test
# check falls back to compile-based acceptance.
_UNIFIED_FILL_LOOP = os.getenv("UNIFIED_FILL_LOOP", "1").strip() == "1"
_U2_TEST_TIMEOUT   = int(os.getenv("U2_TEST_TIMEOUT", "120"))


def prompt_budget(*fixed_parts: str) -> int:
    """Chars remaining for dynamic content after fixed parts are accounted for.

    Every prompt builder that injects dynamic content calls this to get the
    available budget, then distributes it top-down (e.g. divide by num_files).
    Never returns below 2,000 so there is always room for something useful.
    """
    used = sum(len(p) for p in fixed_parts)
    return max(2_000, SINGLE_CALL_CHAR_LIMIT - used)


# task0c output is a small test block (~500-2000 chars), so reserve only 2000 chars
# for the response — use almost the full context window for source+siblings+tests.
# Do NOT use SINGLE_CALL_CHAR_LIMIT here (that reserves 25% for large outputs like
# full source files or requirements docs, which task0c does not produce).
#
# task0c generates small test blocks — no 25% output reservation needed.
# Use the full safe input limit rather than the 75%-budget SINGLE_CALL_CHAR_LIMIT.
TASK0C_PROMPT_CHAR_CAP = _SAFE_INPUT_CHAR_LIMIT_TASK0C

# Per-file limit before semantic splitting (FR-001).
FILE_CHAR_LIMIT = min(_SAFE_INPUT_CHAR_LIMIT, MODEL_CONTEXT_CHARS * 85 // 100)

# Shared output format instruction injected into every prompt that expects <FILE> output.
# Explicitly forbids code fences — LLMs default to wrapping content in ```lang``` blocks,
# which breaks the extractor. Stating consequences ("will be ignored") improves compliance.
FILE_FORMAT_INSTRUCTION = """
CRITICAL OUTPUT FORMAT:
- Use ONLY these markers to output files — nothing else:

<FILE path="path/to/file">
file contents go here
</FILE>

- Do NOT wrap contents in code fences (no ```rust, ```python, ```json, or any other ```).
- Do NOT add any text, explanation, or commentary outside the FILE markers.
- Any response that does not use these exact markers will be silently discarded.
"""

# Max chars per intermediate group summary (stage-1 hierarchical synthesis).
# 50% of context leaves room for the merge prompt and the model's output.
INTERMEDIATE_CHAR_LIMIT = MODEL_CONTEXT_CHARS // 2

# Number of partials per intermediate group in stage-1 synthesis.
# When total partials <= this value, stage-1 is skipped entirely and all partials
# go directly to stage-2 (final synthesis). 20 covers argh (18 files) with no intermediate.
SYNTHESIS_GROUP_SIZE = 20

# Persistent cache directory for chunk extraction results.
# Keyed by content hash — survives across iterations and restarts.
CHUNK_CACHE_DIR = ".chunk_cache"

# Timeout in seconds for the full test subprocess.
TEST_TIMEOUT = 600

# Timeout in seconds for the compile-only build check.
BUILD_TIMEOUT = 120

# task0c stops augmenting once coverage reaches this percent (was hardcoded 100).
COVERAGE_AUGMENTATION_TARGET = float(os.getenv("COVERAGE_AUGMENTATION_TARGET", "98"))

# Backstop for the inner build-fix loop (TASK 2 + compile check). The
# real terminator is the per-loop floor-stall on rustc error count
# (`_t2_max_stalls`). Set to a high value so the stall budget always
# fires first under the "no hard caps on improvement" principle. Operators
# wanting a strict regen cap can override the env.
BUILD_CHECK_RETRIES = int(os.getenv("BUILD_CHECK_RETRIES", "10000"))

# Fix attempts after TASK 3 failure before escalating to TASK 4 + full regeneration.
# Each attempt asks the LLM to fix only the specific files causing test failures,
# preserving files that are already correct — same principle as TASK 0c.
TASK2_FIX_RETRIES = 2

# Max chars for genesis.md and functional_requirements.md when passed to task4.
# Set high enough that truncation never occurs in practice on Claude's 200k context.
TASK4_DOC_LIMIT = 200_000

# Failure log: running record of every iteration's outcome. Fed into TASK 4.
FAILURE_LOG_NAME = "failure_log.md"

# Directory for locked module source backups (bottom-up integration).
LOCKED_MODULES_DIR = ".locked_modules"

# Sentinel file written by TASK 0c when coverage augmentation is complete.
COVERAGE_AUGMENT_SENTINEL = ".coverage_augmented"

# Path to cargo-tarpaulin binary (installed via `cargo install cargo-tarpaulin`).
# Resolve against SUDO_USER's home when running under sudo, so `sudo continue.sh`
# doesn't look in /root/.cargo instead of the real user's home.
_real_home = Path(os.path.expanduser(f"~{os.environ.get('SUDO_USER', '')}") if os.environ.get("SUDO_USER") else str(Path.home()))
TARPAULIN_BIN = _real_home / ".cargo" / "bin" / "cargo-tarpaulin"
GRCOV_BIN     = _real_home / ".cargo" / "bin" / "grcov"
COVERAGE_BACKEND = SETTINGS.coverage_backend

# Pragma comment each backend recognises to exclude a line from coverage.
COVERAGE_IGNORE_PRAGMA = (
    "// # tarpaulin coverage ignore" if COVERAGE_BACKEND is CoverageBackend.TARPAULIN
    else "// GRCOV_EXCL_LINE"
)

# <FILE>-extraction + genesis delimiters relocated to domain.llm_output (slice 5b);
# imported at top. _CODE_BLOCK_FILE_PATTERN stays here (its only consumer is local).
_CODE_BLOCK_FILE_PATTERN = re.compile(
    r'```[^\n]*\n(?:(?://|#|--)\s*FILE\s+path=["\']?([^\s"\'>\n]+)["\']?\s*\n)(.*?)```',
    re.DOTALL,
)

# Separator written into tests_output_N.txt between genesis snapshot and output.

# Manifest tracking which source files the model generated.
MANIFEST_NAME = ".generated_files.txt"

# Persistent log file written alongside the script.
LOG_NAME = "run.log"

# Project profile: produced once at startup, stable across all iterations.
PROJECT_PROFILE_NAME = "project_profile.md"

# Behavioral context synthesized from test content (README substitute for README-less projects).
PROJECT_CONTEXT_NAME = "project_context.md"

# Hash file that invalidates the profile and context cache when project inputs change.
PROJECT_INPUTS_HASH_NAME = "project_profile.inputs_hash"

# Discovery pass output: import paths, type construction patterns, feature flags (BL-001).
COVERAGE_STRATEGY_NAME = "coverage_strategy.md"
COVERAGE_STRATEGY_HASH_NAME = "coverage_strategy.inputs_hash"
PIPELINE_STATE_NAME = _state_mod.PIPELINE_STATE_NAME  # single-sourced (slice 9)

# Workspace directory for cloned/copied project repos (relative to pipeline root).
WORKSPACE_DIR = "tmp"

# Compiler cluster plan — derived from source, written once per project.
BUILD_CONFIGS_NAME = "build_configs.md"

# Per-cluster per-module augmentation sentinels (parallel to MODULE_AUGMENTED_DIR).
CLUSTER_AUGMENTED_DIR  = ".cluster_augmented"
MODULE_AUGMENTED_DIR   = ".module_augmented"
MODULE_STRIPPED_DIR    = ".module_stripped"

# Per-file attempt log for TASK 0c: JSONL, one entry per generation attempt (BL-002).
TASK0C_ATTEMPTS_LOG = ".task0c_attempts.jsonl"

# Directory names that conventionally contain test files, across languages.
TEST_DIR_NAMES = {"tests", "test", "spec", "specs", "__tests__", "testing"}

# Directory names to skip entirely when scanning for test files.
SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn",           # VCS internals
    "target",                          # Rust / Maven build output
    "node_modules",                    # Node.js dependencies
    "vendor",                          # Go / PHP / Ruby vendored deps
    ".venv", "venv", "env",           # Python virtual environments
    "__pycache__", ".mypy_cache",     # Python caches
    "dist", "build", ".build",        # Generic build output
    ".tox", ".eggs",                   # Python test infra artifacts
    ".skeleton", ".skeleton-filled",  # TASK 2 skeleton backend scratch dirs
    ".best_tree",                      # best-tree ratchet snapshot
    ".original_structure",             # pre-strip API snapshot
    ".chunk_cache", ".task1_partials", # task1 caches
    ".phase_c_logs",                   # Phase C test-fix iteration archives (P4)
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_path: Path = None
_nolog: bool = False        # --nolog: skip writing to run.log
_progress_mode: bool = False  # --progress: compact \r-rewriting terminal progress


def _log(msg: str):
    """Print to stdout and append to the run log (unless --nolog)."""
    global _log_trim_counter
    print(msg)
    sys.stdout.flush()
    if not _nolog and _log_path is not None:
        with _log_path.open("a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    _log_trim_counter += 1
    if _log_trim_counter >= _LOG_TRIM_EVERY:
        _log_trim_counter = 0
        _trim_pipeline_log()


def _git_file_hash(root: Path, filename: str) -> str:
    # Slice 4b: relocated to adapters.workspace; thin shim until call sites migrate.
    return _wsmod.git_file_hash(root, filename)


def _resolve_stdout_path() -> str | None:
    """Best-effort: when stdout is redirected (nohup ... > /tmp/foo.log) return
    the resolved target path so monitors can find the live log. Returns None
    when stdout is a tty or otherwise non-file."""
    try:
        if sys.stdout.isatty():
            return None
        return str(Path(f"/proc/{os.getpid()}/fd/1").resolve())
    except Exception:
        return None


def _register_run(project_dir: Path) -> None:
    """Write this run's coordinates to the shared registry so monitor.py can
    auto-discover it. Idempotent: also prunes dead pids."""
    global _CRATE_DISPLAY_NAME
    _name = project_dir.name
    for _suffix in ("-master", "-main", "-trunk"):
        if _name.endswith(_suffix):
            _name = _name[: -len(_suffix)]
            break
    _CRATE_DISPLAY_NAME = _name
    try:
        pid = os.getpid()
        stdout_log = _resolve_stdout_path()
        entry: dict = {
            "pid": pid,
            "project": project_dir.name,
            "project_dir": str(project_dir),
            "pipeline_log": _PIPELINE_LOG,
            "run_log": (str(_log_path) if _log_path else None),
            "stdout_log": stdout_log,
            "stream_log": os.getenv("LLM_STREAM_LOG", os.getenv("OLLAMA_STREAM_LOG",
                str(Path(__file__).parent / "logs" / "stream.log"))),
            "events_log": os.getenv("PIPELINE_EVENTS_LOG",
                str(Path(__file__).parent / "logs" / "events.jsonl")),
            "guard_log": _GUARD_LOG,
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
            "started_at": int(time.time()),
        }
        reg: list[dict] = []
        if _RUN_REGISTRY.exists():
            try:
                reg = json.loads(_RUN_REGISTRY.read_text())
                if not isinstance(reg, list):
                    reg = []
            except Exception:
                reg = []
        # Drop dead pids and any duplicate of our own pid.
        reg = [r for r in reg
               if r.get("pid") != pid and Path(f"/proc/{r.get('pid')}").exists()]
        reg.append(entry)
        _RUN_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        _RUN_REGISTRY.write_text(json.dumps(reg, indent=2))

        import atexit
        def _unregister() -> None:
            try:
                if not _RUN_REGISTRY.exists():
                    return
                cur = json.loads(_RUN_REGISTRY.read_text())
                cur = [r for r in cur if r.get("pid") != pid]
                _RUN_REGISTRY.write_text(json.dumps(cur, indent=2))
            except Exception:
                pass
        atexit.register(_unregister)
    except Exception as e:
        # Never let registry bookkeeping break a real run.
        with suppress(Exception):
            _log(f"  (registry write failed: {e})")


def init_log(root: Path, nolog: bool = False, progress_mode: bool = False):
    global _log_path, _nolog, _progress_mode
    _nolog = nolog
    _progress_mode = progress_mode
    if not nolog:
        _log_path = root / LOG_NAME
    Path(_GUARD_LOG).parent.mkdir(parents=True, exist_ok=True)
    _log(f"\n{'=' * 60}")
    _log(f"{'=' * 60}")
    _py_hash = hashlib.md5(Path(__file__).read_bytes()).hexdigest()[:8]
    _git_hash = ""
    try:
        import subprocess as _sp
        _git_hash = " git=" + _sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, stderr=_sp.DEVNULL, text=True
        ).strip()
    except Exception:
        pass
    global _RUN_STARTED_ISO
    _RUN_STARTED_ISO = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    _log(f"  PIPELINE STARTED — {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC  pipeline.py={_py_hash}{_git_hash}")
    _log(f"  CMD: {sys.executable} {' '.join(sys.argv)}")
    _log(f"{'=' * 60}")
    _log(f"{'=' * 60}")
    _register_run(root)


# --- flow self-audit (--audit) ----------------------------------------------
# Runs pipeline_core.audit (the tools/flow_audit.py core) over THIS run's own
# records at iteration boundaries + on a timer; verdicts go to the log and a
# flow_audit.report event the monitor renders. Read-only and fail-safe — it
# observes the run, never steers it.
_AUDIT_ENABLED = False
_RUN_STARTED_ISO = ""                 # set by init_log; scopes verdicts to this run
_AUDIT_PROJECT_DIR: "Path | None" = None


def _run_flow_audit(context: str) -> None:
    if not _AUDIT_ENABLED:
        return
    try:
        from pipeline_core.audit import FAIL, audit
        verdicts = audit(Path(_EVENTS_LOG), _PROMPTS_DIR, _log_path,
                         _AUDIT_PROJECT_DIR, _RUN_STARTED_ISO,
                         int(os.getenv("AUDIT_PLATEAU_N", "3")))
        _log(f"  [audit] flow invariants ({context}):")
        for v in verdicts:
            _log(f"    {v.check.ljust(22)} {v.status:4s} {v.evidence}")
        _emit("flow_audit.report", data={
            "context": context,
            "failed": sum(1 for v in verdicts if v.status == FAIL),
            "verdicts": [{"check": v.check, "status": v.status,
                          "evidence": v.evidence} for v in verdicts],
        })
    except Exception as exc:
        _log(f"  [audit] skipped: {exc}")


def _start_audit_thread() -> None:
    import threading as _t
    interval = int(os.getenv("AUDIT_INTERVAL_SEC", "900"))

    def _loop() -> None:
        while True:
            time.sleep(interval)
            _run_flow_audit(f"periodic/{interval}s")

    _t.Thread(target=_loop, name="flow-audit", daemon=True).start()


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _render_progress(current: int, total: int, avg_seconds: float | None,
                     bar_width: int = 30) -> str:
    """
    Render a single-line progress bar string.
    Example: [=====>    ] 45/276 (16%) | avg 2m04s/chunk | ETA ~7h21m
    """
    pct = current / total if total else 0
    filled = int(bar_width * pct)
    arrow = "=" * filled + (">" if filled < bar_width else "")
    bar = f"[{arrow:<{bar_width}}]"
    base = f"{bar} {current}/{total} ({pct:.0%})"
    if avg_seconds and avg_seconds > 0:
        remaining = avg_seconds * (total - current)
        base += f" | avg {_fmt_duration(avg_seconds)}/chunk | ETA ~{_fmt_duration(remaining)}"
    return base


class ChunkProgress:
    """
    Tracks per-chunk timing and emits progress output after each chunk.

    Default mode  : prints a new line per chunk (log-friendly).
    --progress mode: overwrites the same terminal line with \\r (clean live view).
    """

    def __init__(self, total: int, rolling_window: int = 5):
        self.total = total
        self.window = rolling_window
        self._times: list[float] = []   # recent chunk durations
        self._chunk_start: float = 0.0
        self._last_line_len: int = 0    # for \r padding in progress mode

    def start_chunk(self):
        self._chunk_start = time.monotonic()

    def finish_chunk(self, current: int):
        elapsed = time.monotonic() - self._chunk_start
        self._times.append(elapsed)
        if len(self._times) > self.window:
            self._times.pop(0)
        avg = sum(self._times) / len(self._times) if self._times else None

        line = _render_progress(current, self.total, avg)

        if _progress_mode:
            # Overwrite previous line in terminal.
            padded = line.ljust(self._last_line_len)
            print(f"\r{padded}", end="", flush=True)
            self._last_line_len = len(line)
        else:
            # New line — works in log files and when tailing.
            _log(f"  {line}")

    def finish(self):
        """Print a final newline when in --progress mode."""
        if _progress_mode and self._last_line_len:
            print()  # move past the \r line


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# SEARCH/REPLACE patch protocol REMOVED. The model could not reliably
# reproduce an anchor byte-exact (flush-left vs indented bodies → no_match),
# and the anchor example leaked crate-specific source. Fix-loop edit
# granularity is now selected by FIX_LOOP_STRATEGY: `legacy` = whole-file
# rewrite (the proven path for small files), `per_fn` = per-fn body splice
# (for large files where whole-file rewrites truncate). See generate_fix_loop.


def extract_test_mod(text: str) -> str:
    """Brace-counter extraction of a gen_tests mod block.

    Finds the first #[cfg(test)] in text, scans forward past any attribute
    lines (#[allow(...)], etc.) to the opening brace of mod gen_*, then walks
    character-by-character counting brace depth until the block closes.
    Returns everything from #[cfg(test)] to the closing }, or "" if not found.
    No regex — works regardless of what the model puts between cfg and mod.
    """
    cfg_pos = text.find('#[cfg(test)]')
    if cfg_pos == -1:
        return ""
    mod_pos = text.find('mod gen_', cfg_pos)
    if mod_pos == -1:
        mod_pos = text.find('mod gen_tests', cfg_pos)
    if mod_pos == -1:
        return ""
    brace_start = text.find('{', mod_pos)
    if brace_start == -1:
        return ""
    depth = 0
    for i, ch in enumerate(text[brace_start:], brace_start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[cfg_pos:i + 1].strip()
    return ""


def generate_in_parallel(
    queries: "list[str]",
    *,
    model: "str | None" = None,
    think: "bool | None" = None,
) -> "list[str]":
    """Fire one ask() per query concurrently; return responses index-aligned to
    `queries`.

    Used by task0c split-and-merge: the N subset prompts share their large file-
    context prefix and differ only in the small "target these functions" tail, so
    vLLM's prefix cache prefills the shared part once and the marginal KV per
    extra request is just its tail — N requests batch on the GPU where N big
    distinct prompts would thrash (measured: shared-prefix x4 clean, distinct x4
    thrashed).

    ask() is already thread-safe (per-call OpenAI client in model_interact;
    content-addressed prompt store; append-only event log). A failed/empty call
    contributes "" rather than aborting the batch. len<=1 short-circuits to a
    single synchronous call (no threads).
    """
    if not queries:
        return []
    if len(queries) == 1:
        try:
            return [ask(queries[0], model=model, think=think)]
        except Exception:
            return [""]

    import threading as _t
    results: list[str] = [""] * len(queries)

    def _worker(i: int) -> None:
        try:
            results[i] = ask(queries[i], model=model, think=think) or ""
        except Exception as _e:  # one bad request must not sink the batch
            _guard("task0c/candidate_error", f"request {i}: {type(_e).__name__}: {_e}")
            results[i] = ""

    threads = [_t.Thread(target=_worker, args=(i,), name=f"task0c-gen-{i}") for i in range(len(queries))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.llm_output (imported at top).


# slice 5b foundation: read/write relocated to pipeline_core.io (imported at top).


# slice 5b foundation: read/write relocated to pipeline_core.io (imported at top).


def _write_fr_preserving_notes(path, new_content: str) -> None:
    """Write FR content, preserving an existing `## Model Notes` section.
    Use at every TASK 1 write site so regenerating FR never wipes the
    model's accumulated self-corrections.
    """
    p = Path(path)
    old = p.read_text(encoding="utf-8") if p.exists() else None
    merged = _preserve_model_notes(old, new_content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(merged, encoding="utf-8")


def delete(path):
    p = Path(path)
    if p.exists():
        p.unlink()


def _parse_cmd(cmd: str) -> list[str]:
    """Split a shell command string into a list, falling back to str.split on parse error."""
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _derive_check_only_cmd(check_cmd: str) -> str:
    """Derive a compile-only (`cargo check`) command from a caller's check_cmd:
    swap the `test`/`build` subcommand → `check`, drop the test harness args
    (anything after a bare `--`) and `--no-run`/`--no-fail-fast`. Used as the
    fast inner-loop check for the per_fn fix strategy — `cargo check` skips
    codegen and test execution, and incremental compilation means a check
    after editing one fn recompiles only that module + its dependents.

    Examples:
      cargo test -p anyhow --no-run          → cargo check -p anyhow
      cargo +nightly test -p x -- --threads  → cargo +nightly check -p x
      cargo check -p x                       → cargo check -p x  (unchanged)
    """
    out_toks: list[str] = []
    for t in _parse_cmd(check_cmd):
        if t == "--":
            break
        if t in ("test", "build"):
            out_toks.append("check"); continue
        if t in ("--no-run", "--no-fail-fast"):
            continue
        out_toks.append(t)
    s = " ".join(out_toks).strip()
    return s if "check" in s.split() else "cargo check"


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# slice 5b foundation: relocated to pipeline_core.domain.text (imported at top).


# ---------------------------------------------------------------------------
# BL-010: Rust source condenser — signatures and type definitions only
# ---------------------------------------------------------------------------

def _condense_rust_to_signatures(source: str, filename: str = "code.rs") -> str:
    """
    Reduce a Rust source file to structural declarations (type definitions +
    function signatures). Function bodies are replaced with { /* ... */ }.
    Used by TASK 0b (fits more files in context) and TASK 0c attempt history
    (compresses failed test blocks to save space for the full compile error).

    Uses grep-ast (tree-sitter) when available; falls back to brace-counting.
    """
    try:
        from grep_ast import TreeContext
        tc = TreeContext(filename, source, color=False, line_number=False,
                         show_top_of_file_lines=0, margin=0)
        # Mark all lines as of interest so we get the full structural skeleton.
        tc.add_lines_of_interest(set(range(1, source.count('\n') + 2)))
        tc.add_context()
        result = tc.format()
        if result and len(str(result).strip()) > 20:
            return str(result)
    except Exception:
        pass

    # Fallback: brace-counting line scanner.
    lines = source.splitlines(keepends=True)
    result_lines: list[str] = []
    depth = 0
    skip_until_depth: int | None = None

    for line in lines:
        if skip_until_depth is not None:
            depth += line.count('{') - line.count('}')
            if depth <= skip_until_depth:
                skip_until_depth = None
            continue

        stripped = line.strip()
        is_fn = bool(re.search(r'\bfn\s+\w+', stripped)) and not stripped.startswith('//')
        is_type_kw = bool(re.match(
            r'\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl|type|macro_rules)\b', line
        ))

        net = line.count('{') - line.count('}')

        if is_fn and not is_type_kw and '{' in line and net > 0:
            last_brace = line.rindex('{')
            result_lines.append(line[:last_brace].rstrip() + ' { /* ... */ }\n')
            skip_until_depth = depth
            depth += net
        else:
            depth += net
            result_lines.append(line)

    return ''.join(result_lines)


# ---------------------------------------------------------------------------
# BL-021: SQLite pipeline knowledge store
# ---------------------------------------------------------------------------

PIPELINE_DB_NAME = "pipeline_knowledge.sqlite"


def _pipeline_db(project_dir: Path) -> sqlite3.Connection:
    """Open (and initialize if new) the per-project knowledge store."""
    db_path = project_dir / PIPELINE_DB_NAME
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    _init_pipeline_db(conn)
    return conn


def _init_pipeline_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS type_signals (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        project  TEXT NOT NULL,
        file     TEXT NOT NULL,
        type_name TEXT NOT NULL,
        difficulty INTEGER NOT NULL DEFAULT 1,
        pattern  TEXT,
        ts       TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS compile_patterns (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        project        TEXT NOT NULL,
        module         TEXT NOT NULL,
        error_category TEXT NOT NULL,
        example        TEXT,
        fix_hint       TEXT,
        iteration      INTEGER,
        ts             TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS genesis_rules (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        project      TEXT NOT NULL,
        module_scope TEXT NOT NULL DEFAULT '*',
        category     TEXT NOT NULL,
        rule_text    TEXT NOT NULL,
        iteration    INTEGER,
        ts           TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS module_status (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        project    TEXT NOT NULL,
        module     TEXT NOT NULL,
        iteration  INTEGER NOT NULL,
        outcome    TEXT NOT NULL,
        pass_count INTEGER,
        fail_count INTEGER,
        ts         TEXT NOT NULL
    );
    """)
    conn.commit()


def _db_project_name(project_dir: Path) -> str:
    return project_dir.name


def type_signals_write(project_dir: Path, file: str, type_name: str,
                       difficulty: int = 1, pattern: str = "") -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO type_signals(project,file,type_name,difficulty,pattern,ts) "
            "VALUES(?,?,?,?,?,?)",
            (_db_project_name(project_dir), file, type_name, difficulty, pattern,
             datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def type_signals_read(project_dir: Path) -> str:
    """Return a formatted summary of types that were difficult to test, for TASK 1."""
    try:
        conn = _pipeline_db(project_dir)
        rows = conn.execute(
            "SELECT type_name, SUM(difficulty) as total, GROUP_CONCAT(pattern, ' | ') "
            "FROM type_signals WHERE project=? "
            "GROUP BY type_name ORDER BY total DESC LIMIT 20",
            (_db_project_name(project_dir),)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["Types that were difficult to construct in tests (extract especially precise "
                 "requirements for these):"]
        for type_name, total, patterns in rows:
            line = f"  - {type_name} (difficulty score: {total})"
            if patterns:
                first = patterns.split(' | ')[0]
                if first:
                    line += f" — e.g. {first[:120]}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def compile_patterns_write(project_dir: Path, module: str, error_category: str,
                            example: str = "", fix_hint: str = "",
                            iteration: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO compile_patterns(project,module,error_category,example,fix_hint,iteration,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (_db_project_name(project_dir), module, error_category, example[:500],
             fix_hint[:500], iteration, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def compile_patterns_read(project_dir: Path, module: str) -> str:
    """Return known compile pitfalls for this module, for TASK 2 prompt."""
    try:
        conn = _pipeline_db(project_dir)
        rows = conn.execute(
            "SELECT error_category, example, fix_hint FROM compile_patterns "
            "WHERE project=? AND (module=? OR module='*') "
            "ORDER BY id DESC LIMIT 15",
            (_db_project_name(project_dir), module)
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["Known compile pitfalls for this module (avoid repeating these errors):"]
        seen: set[str] = set()
        for cat, example, fix_hint in rows:
            if cat in seen:
                continue
            seen.add(cat)
            line = f"  - [{cat}]"
            if example:
                line += f" e.g. `{example[:100]}`"
            if fix_hint:
                line += f" → fix: {fix_hint[:100]}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def genesis_rules_write(project_dir: Path, module_scope: str, category: str,
                         rule_text: str, iteration: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO genesis_rules(project,module_scope,category,rule_text,iteration,ts) "
            "VALUES(?,?,?,?,?,?)",
            (_db_project_name(project_dir), module_scope, category, rule_text[:1000],
             iteration, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def genesis_rules_read(project_dir: Path, module_scope: str = "*",
                        categories: list | None = None) -> str:
    """Return structured rules relevant to a module and failure categories, for TASK 1/2."""
    try:
        conn = _pipeline_db(project_dir)
        placeholders = "?,?" if module_scope != "*" else "?"
        args: list = [_db_project_name(project_dir)]
        if module_scope != "*":
            args += [module_scope, "*"]
        else:
            args += ["*"]
        if categories:
            cat_placeholders = ",".join("?" * len(categories))
            query = (
                f"SELECT category, rule_text FROM genesis_rules "
                f"WHERE project=? AND module_scope IN ({placeholders}) "
                f"AND category IN ({cat_placeholders}) "
                f"ORDER BY id DESC LIMIT 20"
            )
            args += categories
        else:
            query = (
                f"SELECT category, rule_text FROM genesis_rules "
                f"WHERE project=? AND module_scope IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 20"
            )
        rows = conn.execute(query, args).fetchall()
        conn.close()
        if not rows:
            return ""
        seen: set[str] = set()
        lines = ["Structured rules from previous failures (apply these):"]
        for cat, rule in rows:
            key = cat + rule[:50]
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  [{cat}] {rule}")
        return "\n".join(lines)
    except Exception:
        return ""


def module_status_write(project_dir: Path, module: str, iteration: int,
                         outcome: str, pass_count: int = 0,
                         fail_count: int = 0) -> None:
    try:
        conn = _pipeline_db(project_dir)
        conn.execute(
            "INSERT INTO module_status(project,module,iteration,outcome,pass_count,fail_count,ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (_db_project_name(project_dir), module, iteration, outcome, pass_count,
             fail_count, datetime.now(UTC).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# BL-023: Test assertion extractor for TASK 2
# ---------------------------------------------------------------------------

def _extract_test_assertions(test_files: list, module_dir_rel: str = "",
                              max_assertions: int = 40) -> str:
    """
    Extract assert_* lines with minimal surrounding context from test files.
    Returns a compact 'assertion manifest' that TASK 2 can use as ground truth.
    Only includes files relevant to the current module (if module_dir_rel given).
    """
    assertion_re = re.compile(
        r'\b(assert(?:_eq|_ne|_matches|_approx_eq)?!|assert!\s*\(|\.unwrap\(\)|\.expect\()',
        re.MULTILINE,
    )
    results: list[str] = []

    for rel, content in test_files:
        if module_dir_rel and not (rel.startswith(module_dir_rel) or
                                   f"/{module_dir_rel}" in rel):
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if assertion_re.search(stripped) and not stripped.startswith('//'):
                # Include one line of context before (usually the `let` binding)
                context_line = lines[i - 1].strip() if i > 0 else ""
                entry = (f"  {context_line}  // context\n  {stripped}"
                         if context_line and context_line.startswith("let")
                         else f"  {stripped}")
                results.append(entry)
                if len(results) >= max_assertions:
                    break
        if len(results) >= max_assertions:
            break

    if not results:
        return ""
    return ("Assertions the generated code must satisfy (from test files):\n"
            + "\n".join(results))


# ---------------------------------------------------------------------------
# BL-018: Type name extractor for cross-chunk manifest
# ---------------------------------------------------------------------------

def _categorize_compile_error(error_output: str) -> str:
    """Classify a compile error into a short category tag for compile_patterns."""
    o = error_output.lower()
    if "cannot find type" in o or "unresolved import" in o or "use of undeclared" in o:
        return "missing_type"
    if "module not found" in o or "no module named" in o or "mod " in o and "not found" in o:
        return "module_declaration"
    if "trait bound" in o or "does not implement" in o or "the trait" in o:
        return "trait_bound"
    if "mismatched types" in o or "expected" in o and "found" in o:
        return "type_mismatch"
    if "cannot borrow" in o or "borrow" in o and "conflict" in o:
        return "borrow_checker"
    if "lifetime" in o:
        return "lifetime"
    if "unused import" in o or "dead_code" in o:
        return "lint_error"
    if "proc-macro" in o or "proc_macro" in o:
        return "proc_macro"
    return "compile_error"


def _extract_type_names_from_requirements(text: str) -> set[str]:
    """
    Extract likely type names (CamelCase identifiers) from a requirements chunk.
    Used to build the cross-chunk type manifest in TASK 1.
    """
    return set(re.findall(r'\b([A-Z][a-zA-Z0-9]{2,})\b', text))


# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).


# ---------------------------------------------------------------------------
# Workspace structure scanner (static — no AI involved)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Workspace structure scanning — SOLID design
#
# Two implementations:
#   StaticWorkspaceScanner  — deterministic Python parsing (Rust + Node.js)
#   LLMWorkspaceScanner     — language-agnostic: feeds manifests to the model
#
# Active implementation is selected by WORKSPACE_SCANNER in .env:
#   WORKSPACE_SCANNER=llm     (default)
#   WORKSPACE_SCANNER=static
# ---------------------------------------------------------------------------

class WorkspaceScanner(ABC):
    """Produce a markdown description of workspace structure for TASK 2."""

    @abstractmethod
    def scan(self, project_dir: Path) -> str:
        """Return a markdown string, or empty string if nothing found."""


# Cargo.toml is TOML — parse it with the stdlib parser, never regex (CODE_ARCHITECTURE.md §8
# R8: a structured format scraped with regex is a recurring bug source — comments, quoting,
# inline tables, and multi-line all break the patterns).
def _load_cargo_toml(text: str) -> dict:
    """Parse Cargo.toml content via tomllib. Returns {} on malformed/unparseable input."""
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, TypeError):
        return {}


def _cargo_package_name(text: str) -> str:
    """The [package] name, or "" if absent/unparseable."""
    pkg = _load_cargo_toml(text).get("package", {})
    return str(pkg.get("name", "") or "") if isinstance(pkg, dict) else ""


def _cargo_is_proc_macro(text: str) -> bool:
    """True iff `[lib] proc-macro = true`."""
    lib = _load_cargo_toml(text).get("lib", {})
    return isinstance(lib, dict) and lib.get("proc-macro", False) is True


def _cargo_default_features(text: str) -> list[str]:
    """The `[features] default = [...]` list (empty if absent)."""
    feats = _load_cargo_toml(text).get("features", {})
    default = feats.get("default", []) if isinstance(feats, dict) else []
    return [str(x) for x in default] if isinstance(default, list) else []


class StaticWorkspaceScanner(WorkspaceScanner):
    """
    Deterministic Python parsing of known build manifests (Rust/Cargo, Node/npm).
    Fast, no LLM call, but requires per-language code for each new ecosystem.
    """

    def scan(self, project_dir: Path) -> str:
        lines = ["## Workspace structure (read from existing build files — follow exactly)\n"]

        # ---- Rust / Cargo ---------------------------------------------------
        cargo_tomls = sorted(project_dir.rglob("Cargo.toml"))
        if cargo_tomls:
            lines.append("### Rust workspace (Cargo)\n")

            for ct in cargo_tomls:
                rel = ct.relative_to(project_dir)
                if rel.parts[0] in SKIP_DIR_NAMES:
                    continue
                try:
                    text = ct.read_text()
                except Exception:
                    continue

                data = _load_cargo_toml(text)
                pkg = data.get("package", {})
                pkg_name = pkg.get("name", "") if isinstance(pkg, dict) else ""
                if not pkg_name:
                    continue
                crate_dir = ct.parent
                rel_dir = str(crate_dir.relative_to(project_dir)) if crate_dir != project_dir else "."

                targets = []
                lib = data.get("lib")
                if isinstance(lib, dict):
                    lib_path = lib.get("path", "src/lib.rs")
                    is_proc_macro = lib.get("proc-macro", False) is True
                    label = "proc-macro target" if is_proc_macro else "lib target"
                    targets.append(f"  - {label} → required file: `{rel_dir}/{lib_path}`")
                elif (crate_dir / "src" / "lib.rs").exists():
                    targets.append(f"  - lib target (implicit) → required file: `{rel_dir}/src/lib.rs`")

                for b in data.get("bin", []) if isinstance(data.get("bin"), list) else []:
                    bin_name = b.get("name", pkg_name) if isinstance(b, dict) else pkg_name
                    bin_path = b.get("path", "src/main.rs") if isinstance(b, dict) else "src/main.rs"
                    targets.append(f"  - bin target `{bin_name}` → required file: `{rel_dir}/{bin_path}`")

                if not targets and (crate_dir / "src" / "main.rs").exists():
                    targets.append(f"  - bin target (implicit) → required file: `{rel_dir}/src/main.rs`")

                if not targets and not (crate_dir / "src" / "main.rs").exists():
                    targets.append(f"  - lib target (implicit default) → required file: `{rel_dir}/src/lib.rs`")

                # Note if a separate tests.rs file exists — lib.rs must declare it.
                tests_note = []
                if (crate_dir / "src" / "tests.rs").exists():
                    tests_note.append(
                        f"  - ⚠️  `{rel_dir}/src/tests.rs` exists — lib.rs MUST declare "
                        f"`#[cfg(test)] mod tests;` at the end"
                    )

                # Expose [dependencies] versions (from the parsed TOML, rendered compactly).
                dep_lines = []
                deps = data.get("dependencies", {})
                if isinstance(deps, dict):
                    for name, spec in deps.items():
                        if isinstance(spec, str):
                            rendered = f'"{spec}"'
                        elif isinstance(spec, dict):
                            inner = ", ".join(f"{k} = {v!r}" for k, v in spec.items())
                            rendered = f"{{ {inner} }}"
                        else:
                            rendered = str(spec)
                        dep_lines.append(f"    {name} = {rendered}")

                if targets:
                    lines.append(f"**Crate `{pkg_name}`** (at `{rel_dir}/`):")
                    lines.extend(targets)
                    if tests_note:
                        lines.extend(tests_note)
                    if dep_lines:
                        lines.append("  - dependencies:")
                        lines.extend(dep_lines)
                    lines.append("")

            lines.append(
                "_Each crate MUST have the required entry-point file(s) listed above or the build will fail._\n"
            )

        # ---- Node / npm -----------------------------------------------------
        pkg_jsons = [p for p in sorted(project_dir.rglob("package.json"))
                     if "node_modules" not in p.parts]
        if pkg_jsons:
            lines.append("### Node.js workspace (npm/yarn)\n")
            for pj in pkg_jsons:
                try:
                    data = json.loads(pj.read_text())
                    name = data.get("name", str(pj.parent.relative_to(project_dir)))
                    main = data.get("main", "index.js")
                    rel_dir = str(pj.parent.relative_to(project_dir))
                    lines.append(f"**Package `{name}`** (at `{rel_dir}/`): main entry `{main}`")
                except Exception:
                    continue
            lines.append("")

        if len(lines) == 1:
            return ""
        return "\n".join(lines)


class LLMWorkspaceScanner(WorkspaceScanner):
    """
    Language-agnostic scanner: collects all build/manifest files found in the
    project and asks the LLM to describe the workspace structure.
    Works for any language without requiring per-language Python code.
    """

    # Manifest filenames recognised across ecosystems.
    _MANIFEST_NAMES = {
        "Cargo.toml", "package.json", "pom.xml",
        "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
        "composer.json", "build.sbt", "Gemfile", "go.mod",
        "pyproject.toml", "setup.py", "setup.cfg",
        "CMakeLists.txt", "mix.exs", "pubspec.yaml",
    }

    def scan(self, project_dir: Path) -> str:
        manifests = self._collect_manifests(project_dir)
        if not manifests:
            return ""

        manifest_text = "\n\n".join(
            f"### {rel_path}\n```\n{content}\n```"
            for rel_path, content in manifests
        )

        prompt = (
            "You are analysing a software project's build manifests to determine its workspace structure.\n\n"
            f"{manifest_text}\n\n"
            "Based solely on the manifests above, produce a concise markdown description of the workspace:\n"
            "- List every package/crate/module.\n"
            "- For each, state the relative directory and the required source entry-point file(s) "
            "(e.g. src/lib.rs, src/main.rs, index.js, Main.java, lib/my_app.ex, etc.).\n"
            "- Use this exact format for each entry:\n"
            "  **Package `<name>`** (at `<rel_dir>/`): required file(s): `<path>` [, `<path>`]\n"
            "- End with the line: "
            "_Each package MUST have the required entry-point file(s) listed above or the build will fail._\n"
            "Do not include explanations — only the structured list."
        )

        response = ask(prompt, model=_TASK0_MODEL)
        if not response or not response.strip():
            return ""
        return "## Workspace structure (derived from build manifests — follow exactly)\n\n" + response.strip()

    def _collect_manifests(self, project_dir: Path) -> list[tuple[str, str]]:
        results = []
        for path in sorted(project_dir.rglob("*")):
            if path.name not in self._MANIFEST_NAMES:
                continue
            rel = path.relative_to(project_dir)
            if any(part in SKIP_DIR_NAMES for part in rel.parts):
                continue
            try:
                content = path.read_text(errors="replace")
                results.append((str(rel), content))
            except Exception:
                continue
        return results


def _workspace_scanner_factory() -> WorkspaceScanner:
    backend = os.getenv("WORKSPACE_SCANNER", "llm").lower()
    if backend == "static":
        return StaticWorkspaceScanner()
    return LLMWorkspaceScanner()


def scan_downstream_api(project_dir: Path, module_name: str) -> str:
    """
    Scan all .rs source files in the workspace for 'use <module_name>::...' imports.
    Returns a formatted hint telling the model what the module must publicly export.
    Used to give argh_shared (and similar leaf crates) context about their API contract
    even though they have no upstream locked deps to learn from.
    """
    direct_imports: set[str] = set()
    submodule_imports: dict[str, set[str]] = {}

    for p in sorted(project_dir.rglob("*.rs")):
        rel = p.relative_to(project_dir)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        try:
            text = p.read_text(errors='replace')
        except Exception:
            continue

        mod = re.escape(module_name)

        # use module::{A, B, C}
        for m in re.finditer(rf'use {mod}::\{{([^}}]+)\}}', text):
            for item in m.group(1).split(','):
                item = item.strip()
                if item:
                    direct_imports.add(item)

        # use module::submod::{A, B}
        for m in re.finditer(rf'use {mod}::(\w+)::\{{([^}}]+)\}}', text):
            submod = m.group(1)
            for item in m.group(2).split(','):
                item = item.strip()
                if item:
                    submodule_imports.setdefault(submod, set()).add(item)

        # use module::submod::Item  (single item, no braces)
        for m in re.finditer(rf'use {mod}::(\w+)::(\w+)\b', text):
            submod, item = m.group(1), m.group(2)
            if submod[0].islower() and item[0].isupper():
                submodule_imports.setdefault(submod, set()).add(item)

    # Also scan functional_requirements.md for type names used in code blocks that look like
    # internal types (not imported from outside). These must be defined in the module itself.
    internal_types: set[str] = set()
    func_req_path = project_dir / "functional_requirements.md"
    if func_req_path.exists():
        try:
            func_text = func_req_path.read_text(errors='replace')
            # Find type names in code blocks that are not imported from elsewhere
            # Look for PascalCase identifiers in Rust signatures inside ```rust blocks
            for block in re.finditer(r'```rust\s*(.*?)```', func_text, re.DOTALL):
                code = block.group(1)
                # Find PascalCase identifiers
                for m in re.finditer(r'\b([A-Z][A-Za-z0-9]+)\b', code):
                    name = m.group(1)
                    # Skip if it's a stdlib/known type or already in our direct/submodule imports
                    if name in ('Result', 'Option', 'String', 'Vec', 'Ok', 'Err', 'Some', 'None',
                                'Box', 'Arc', 'Rc', 'Fn', 'FnMut', 'FnOnce', 'Send', 'Sync',
                                'Self', 'Args', 'bool', 'usize', 'str'):
                        continue
                    if name in direct_imports:
                        continue
                    if any(name in v for v in submodule_imports.values()):
                        continue
                    internal_types.add(name)
        except Exception:
            pass

    if not direct_imports and not submodule_imports and not internal_types:
        return ""

    lines = [
        f"\n⚠️  REQUIRED PUBLIC API for `{module_name}` (inferred from workspace source files):",
        f"Downstream crates import from `{module_name}` as follows — you MUST match these paths exactly:",
    ]

    if submodule_imports:
        lines.append("")
        lines.append("Sub-module paths (must be separate .rs files declared with `pub mod` in lib.rs):")
        for submod in sorted(submodule_imports.keys()):
            exports = ', '.join(sorted(submodule_imports[submod]))
            lines.append(f"  {module_name}/src/{submod}.rs  →  pub mod {submod};  // exports: {exports}")
        lines.append("")
        lines.append("lib.rs MUST contain these exact pub mod declarations:")
        for submod in sorted(submodule_imports.keys()):
            lines.append(f"  pub mod {submod};")

    if direct_imports:
        lines.append("")
        lines.append(f"Types imported directly from the crate root (`use {module_name}::TypeName`):")
        for item in sorted(direct_imports):
            lines.append(f"  pub use ...::{item};  // or define directly in lib.rs")

    if internal_types:
        lines.append("")
        lines.append("Internal types referenced in functional requirements — MUST be defined in lib.rs at the crate root:")
        lines.append("⚠️  DO NOT put these in submodules. Define them directly in lib.rs so all submodules can use `crate::TypeName`.")
        for item in sorted(internal_types):
            lines.append(f"  pub struct {item}; // define in lib.rs, NOT in submodules")

    return '\n'.join(lines) + '\n'


def scan_workspace_structure(project_dir: Path) -> str:
    """
    Produce a markdown workspace map for TASK 2.
    Implementation is selected by WORKSPACE_SCANNER in .env (default: llm).
    """
    return _workspace_scanner_factory().scan(project_dir)


# ---------------------------------------------------------------------------
# Dependency graph (TASK 0b) — inter-module dependency analysis
#
# Two implementations:
#   StaticDependencyGraph  — deterministic Python parsing (Rust/Cargo only)
#   LLMDependencyGraph     — language-agnostic: feeds all manifests to the model
#
# Active implementation is selected by DEPENDENCY_GRAPH in .env:
#   DEPENDENCY_GRAPH=static   (default)
#   DEPENDENCY_GRAPH=llm
# ---------------------------------------------------------------------------

class DependencyGraph(ABC):
    """
    Analyses intra-workspace module dependencies and provides a topological
    processing order (leaves first, dependents last).

    Also owns the logic for scoping workspace-level commands (test, build check)
    to a single module — because that logic is ecosystem-specific.
    """

    @abstractmethod
    def modules(self) -> list[str]:
        """Return all module names in the workspace. Empty list → whole-workspace fallback."""

    @abstractmethod
    def crate_dir(self, module_name: str) -> Path:
        """Return the root directory of a module."""

    @abstractmethod
    def crate_type(self, module_name: str) -> str:
        """Return 'lib', 'bin', or 'proc-macro' (or a similar ecosystem label)."""

    @abstractmethod
    def workspace_deps(self, module_name: str) -> set[str]:
        """Return the set of workspace-internal dependencies of a module."""

    @abstractmethod
    def topological_order(self) -> list[str]:
        """Return module names ordered so every dependency precedes its dependents."""

    @abstractmethod
    def module_test_command(self, test_command: str, module_name: str) -> str:
        """Derive a module-scoped test command from the full workspace command."""

    @abstractmethod
    def module_build_check_command(self, build_cmd: str | None, module_name: str) -> str | None:
        """Derive a module-scoped compile-check command from the workspace command."""


class StaticDependencyGraph(DependencyGraph):
    """
    Deterministic Python parsing of Cargo.toml files.
    Fast, no LLM call, but Rust/Cargo workspaces only.
    Returns an empty module list for non-Cargo projects.
    """

    def __init__(self, project_dir: Path):
        self._project_dir = project_dir
        self._modules: dict[str, Path] = {}
        self._deps: dict[str, set[str]] = {}
        self._crate_types: dict[str, str] = {}
        self._parse()

    def _try_cargo_metadata(self) -> bool:
        """BL-006: Use `cargo metadata` to populate module/dep/type data authoritatively."""
        try:
            result = subprocess.run(
                ["cargo", "metadata", "--format-version", "1", "--no-deps"],
                cwd=str(self._project_dir),
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return False
            data = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, Exception):
            return False

        workspace_members = set(data.get("workspace_members", []))
        packages = {pkg["id"]: pkg for pkg in data.get("packages", [])}

        for pkg_id in workspace_members:
            pkg = packages.get(pkg_id)
            if pkg is None:
                continue
            name = pkg["name"]
            manifest_path = Path(pkg["manifest_path"])
            try:
                rel = manifest_path.relative_to(self._project_dir)
            except ValueError:
                continue
            rel_parts_lower = [p.lower() for p in rel.parts]
            if any(p in ("tests", "test-helpers", "test_helpers", "testdata") for p in rel_parts_lower):
                continue

            self._modules[name] = manifest_path.parent

            crate_type = "lib"
            for target in pkg.get("targets", []):
                kinds = target.get("kind", [])
                if "proc-macro" in kinds:
                    crate_type = "proc-macro"
                    break
                if "bin" in kinds:
                    crate_type = "bin"
            self._crate_types[name] = crate_type

            dep_names: set[str] = set()
            for dep in pkg.get("dependencies", []):
                dep_name = dep["name"]
                dep_names.add(dep_name)
                dep_names.add(dep_name.replace("-", "_"))
            self._deps[name] = dep_names

        if not self._modules:
            return False

        workspace_names = set(self._modules.keys())
        for name in list(self._deps):
            resolved: set[str] = set()
            for d in self._deps[name]:
                if d in workspace_names:
                    resolved.add(d)
                elif d.replace("_", "-") in workspace_names:
                    resolved.add(d.replace("_", "-"))
            self._deps[name] = resolved

        _log(f"  [dep graph] cargo metadata: {len(self._modules)} module(s).")
        return True

    def _parse(self):
        # Try cargo metadata first (authoritative, handles workspace edge cases).
        # Falls back to regex parser for environments without cargo or network access.
        if self._try_cargo_metadata():
            return
        _log("  [dep graph] cargo metadata unavailable — using regex parser.")
        self._parse_regex()

    def _parse_regex(self):
        cargo_tomls = sorted(self._project_dir.rglob("Cargo.toml"))
        if not cargo_tomls:
            return

        for ct in cargo_tomls:
            rel = ct.relative_to(self._project_dir)
            if any(part in SKIP_DIR_NAMES for part in rel.parts):
                continue
            # Skip test-fixture crates: publish=false crates living under tests/ or
            # test-helpers/ directories are auxiliary and not pipeline modules.
            rel_parts_lower = [p.lower() for p in rel.parts]
            if any(p in ("tests", "test-helpers", "test_helpers", "testdata") for p in rel_parts_lower):
                continue
            try:
                text = ct.read_text()
            except Exception:
                continue

            data = _load_cargo_toml(text)
            pkg = data.get("package", {})
            pkg_name = pkg.get("name", "") if isinstance(pkg, dict) else ""
            if not pkg_name:
                continue
            self._modules[pkg_name] = ct.parent

            if _cargo_is_proc_macro(text):
                self._crate_types[pkg_name] = "proc-macro"
            elif isinstance(data.get("bin"), list) and data["bin"]:
                self._crate_types[pkg_name] = "bin"
            else:
                self._crate_types[pkg_name] = "lib"

            dep_names: set[str] = set()
            for section_pat in (r'\[dependencies\](.*?)(?=\n\[|\Z)',
                                r'\[dev-dependencies\](.*?)(?=\n\[|\Z)'):
                for sec_m in re.finditer(section_pat, text, re.DOTALL):
                    block = sec_m.group(1)
                    # Match both inline (`name = ...` / `name { ...`) and
                    # workspace-inherited (`name.workspace = true`) dep syntax.
                    for name_m in re.finditer(
                        r'^([a-zA-Z0-9_-]+)(?:\s*[=\{]|\.[a-zA-Z_]+\s*=)',
                        block, re.MULTILINE
                    ):
                        raw = name_m.group(1)
                        dep_names.add(raw)
                        dep_names.add(raw.replace("-", "_"))
            self._deps[pkg_name] = dep_names

        workspace_names = set(self._modules.keys())
        for name in list(self._deps):
            resolved: set[str] = set()
            for d in self._deps[name]:
                if d in workspace_names:
                    resolved.add(d)
                elif d.replace("_", "-") in workspace_names:
                    resolved.add(d.replace("_", "-"))
            self._deps[name] = resolved

    def modules(self) -> list[str]:
        return list(self._modules.keys())

    def crate_dir(self, module_name: str) -> Path:
        return self._modules[module_name]

    def crate_type(self, module_name: str) -> str:
        return self._crate_types.get(module_name, "lib")

    def workspace_deps(self, module_name: str) -> set[str]:
        return self._deps.get(module_name, set())

    def topological_order(self) -> list[str]:
        """Kahn's algorithm; falls back to dep-count order if a cycle is found."""
        if not self._modules:
            return []

        dependents: dict[str, list[str]] = {n: [] for n in self._modules}
        in_deg: dict[str, int] = dict.fromkeys(self._modules, 0)

        for name, deps in self._deps.items():
            for dep in deps:
                if dep in self._modules:
                    in_deg[name] += 1
                    dependents[dep].append(name)

        queue: list[str] = sorted(n for n in self._modules if in_deg[n] == 0)
        result: list[str] = []

        while queue:
            node = queue.pop(0)
            result.append(node)
            for dependent in sorted(dependents.get(node, [])):
                in_deg[dependent] -= 1
                if in_deg[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(self._modules):
            _log("  WARNING: dependency cycle detected — using dep-count order.")
            return sorted(self._modules.keys(), key=lambda n: len(self._deps.get(n, set())))

        return result

    def module_test_command(self, test_command: str, module_name: str) -> str:
        """
        cargo test                  →  cargo test -p <name>
        cargo nextest run           →  cargo nextest run -p <name>
        cargo test --all-features   →  cargo test -p <name> --all-features
        """
        cmd = test_command.strip()
        if not cmd.startswith("cargo"):
            return test_command
        parts = _parse_cmd(cmd)
        insert_at = 1
        for i in range(1, len(parts)):
            if not parts[i].startswith("-"):
                insert_at = i + 1
            else:
                break
        return " ".join(parts[:insert_at] + ["-p", module_name] + parts[insert_at:])

    def module_build_check_command(self, build_cmd: str | None, module_name: str) -> str | None:
        """cargo check --workspace  →  cargo check -p <name>"""
        if not build_cmd:
            return None
        cmd = build_cmd.strip()
        if "--workspace" in cmd:
            return cmd.replace("--workspace", f"-p {module_name}")
        if cmd.startswith("cargo check"):
            return f"cargo check -p {module_name}"
        return build_cmd


class LLMDependencyGraph(DependencyGraph):
    """
    Language-agnostic dependency graph: collects all build/manifest files and
    asks the model to extract the module list, dependency edges, topological
    order, and module-scoped command templates.

    Works for any build system without requiring per-ecosystem Python code.
    Falls back to an empty module list if the model returns unusable output.
    """

    # Same manifest filenames recognised by LLMWorkspaceScanner.
    _MANIFEST_NAMES = {
        "Cargo.toml", "package.json", "pom.xml",
        "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
        "composer.json", "build.sbt", "Gemfile", "go.mod",
        "pyproject.toml", "setup.py", "setup.cfg",
        "CMakeLists.txt", "mix.exs", "pubspec.yaml",
    }

    def __init__(self, project_dir: Path):
        self._project_dir = project_dir
        self._modules: dict[str, Path] = {}
        self._deps: dict[str, set[str]] = {}
        self._crate_types: dict[str, str] = {}
        self._topo: list[str] = []
        self._test_cmd_tpl: str = ""        # e.g. "cargo test -p {module}"
        self._build_check_tpl: str = ""     # e.g. "cargo check -p {module}"
        self._parse()

    def _collect_manifests(self) -> list[tuple[str, str]]:
        results = []
        for path in sorted(self._project_dir.rglob("*")):
            if path.name not in self._MANIFEST_NAMES:
                continue
            rel = path.relative_to(self._project_dir)
            if any(part in SKIP_DIR_NAMES for part in rel.parts):
                continue
            try:
                content = path.read_text(errors="replace")
                results.append((str(rel), content))
            except Exception:
                continue
        return results

    def _parse(self):
        manifests = self._collect_manifests()
        if not manifests:
            return

        manifest_text = "\n\n".join(
            f"### {rel_path}\n```\n{content}\n```"
            for rel_path, content in manifests
        )

        prompt = f"""You are analysing a software project's build manifests to extract its
module dependency graph.

{manifest_text}

Based solely on the manifests above, output the dependency graph in this exact format.
Use one line per module, then the topological order, then the module-scoped command templates.
Do not add any explanation or extra text.

For each module:
MODULE: name=<name> dir=<relative_dir> type=<lib|bin|proc-macro> deps=<comma-separated names or empty>

Then:
TOPO_ORDER: <module names in topological order, space-separated, leaves first>
MODULE_TEST_CMD: <full test command with the literal placeholder {{module}}, e.g. cargo test -p {{module}}>
MODULE_BUILD_CHECK: <compile-only check command with {{module}}, or NONE if not applicable>

Rules:
- deps must only include other modules listed above (intra-workspace only, no external libraries)
- If the project has only one module, output that single module with no deps
- MODULE_TEST_CMD and MODULE_BUILD_CHECK use {{module}} as a placeholder for the module name
"""

        def attempt():
            response = ask(prompt, model=_TASK0_MODEL)
            parsed = self._parse_response(response)
            return parsed if parsed else False

        result = with_retry(attempt)
        if result is False:
            _log("  WARNING: LLMDependencyGraph could not parse model response — "
                 "falling back to whole-workspace mode.")
            return

        modules, deps, types, topo, test_tpl, check_tpl = result
        self._modules = modules
        self._deps = deps
        self._crate_types = types
        self._topo = topo
        self._test_cmd_tpl = test_tpl
        self._build_check_tpl = check_tpl

    def _parse_response(self, text: str):
        """
        Parse the structured model response into (modules, deps, types, topo, test_tpl, check_tpl).
        Returns None if required fields are missing or malformed.
        """
        modules: dict[str, Path] = {}
        deps: dict[str, set[str]] = {}
        types: dict[str, str] = {}

        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("MODULE:"):
                continue
            # MODULE: name=argh dir=argh type=lib deps=argh_shared,argh_derive
            name_m = re.search(r'name=([^\s]+)', line)
            dir_m  = re.search(r'dir=([^\s]+)', line)
            type_m = re.search(r'type=([^\s]+)', line)
            deps_m = re.search(r'deps=([^\s]*)', line)
            if not name_m or not dir_m:
                continue
            name = name_m.group(1)
            crate_dir = self._project_dir / dir_m.group(1)
            crate_type = type_m.group(1) if type_m else "lib"
            raw_deps = deps_m.group(1) if deps_m else ""
            dep_set = {d.strip() for d in raw_deps.split(",") if d.strip()} if raw_deps else set()
            modules[name] = crate_dir
            types[name] = crate_type
            deps[name] = dep_set

        if not modules:
            return None

        # Prune deps to workspace-only.
        for name in deps:
            deps[name] = {d for d in deps[name] if d in modules}

        # Topological order.
        topo: list[str] = []
        topo_m = re.search(r'^TOPO_ORDER:\s*(.+)$', text, re.MULTILINE)
        if topo_m:
            topo = [t.strip() for t in topo_m.group(1).split() if t.strip() in modules]
        if len(topo) != len(modules):
            # Fall back to alphabetical if model output is incomplete.
            topo = sorted(modules.keys())

        # Command templates.
        test_tpl = ""
        test_m = re.search(r'^MODULE_TEST_CMD:\s*(.+)$', text, re.MULTILINE)
        if test_m:
            test_tpl = test_m.group(1).strip()

        check_tpl = ""
        check_m = re.search(r'^MODULE_BUILD_CHECK:\s*(.+)$', text, re.MULTILINE)
        if check_m and check_m.group(1).strip().upper() != "NONE":
            check_tpl = check_m.group(1).strip()

        return modules, deps, types, topo, test_tpl, check_tpl

    def modules(self) -> list[str]:
        return list(self._modules.keys())

    def crate_dir(self, module_name: str) -> Path:
        return self._modules[module_name]

    def crate_type(self, module_name: str) -> str:
        return self._crate_types.get(module_name, "lib")

    def workspace_deps(self, module_name: str) -> set[str]:
        return self._deps.get(module_name, set())

    def topological_order(self) -> list[str]:
        return list(self._topo)

    def module_test_command(self, test_command: str, module_name: str) -> str:
        if self._test_cmd_tpl:
            return self._test_cmd_tpl.replace("{module}", module_name)
        return test_command  # no template: whole-workspace command unchanged

    def module_build_check_command(self, build_cmd: str | None, module_name: str) -> str | None:
        if self._build_check_tpl:
            return self._build_check_tpl.replace("{module}", module_name)
        return build_cmd


def _dependency_graph_factory(project_dir: Path) -> DependencyGraph:
    backend = os.getenv("DEPENDENCY_GRAPH", "static").lower()
    if backend == "llm":
        return LLMDependencyGraph(project_dir)
    return StaticDependencyGraph(project_dir)


# ---------------------------------------------------------------------------
# Module lock management
# ---------------------------------------------------------------------------

def load_locked_modules(project_dir: Path) -> set[str]:
    """Return the set of module names that have been locked (tests pass, files frozen)."""
    p = project_dir / LOCKED_MODULES_DIR / "locked.txt"
    if not p.exists():
        return set()
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


def lock_module(project_dir: Path, module_name: str, module_dir: Path,
                manifest_path: Path) -> int:
    """
    Lock a module: copy its generated files to .locked_modules/<name>/ as a
    permanent backup, then record the module name in locked.txt.

    The live files remain on disk so the compiler can use them as dependencies
    when building subsequent modules. The backup is used to:
      - Restore locked files if the project directory is cleaned between runs.
      - Provide locked-source context when generating dependent modules.

    Returns the number of files locked.
    """
    lock_dir = project_dir / LOCKED_MODULES_DIR / module_name
    lock_dir.mkdir(parents=True, exist_ok=True)

    module_rel = module_dir.relative_to(project_dir)
    files = load_manifest(manifest_path)
    locked_count = 0

    for rel_path in files:
        full = project_dir / rel_path
        if not full.exists():
            continue
        try:
            Path(rel_path).relative_to(module_rel)  # only files under this module's dir
        except ValueError:
            continue
        dest = lock_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(full.read_text())
        locked_count += 1

    # Update locked.txt
    locked = load_locked_modules(project_dir)
    locked.add(module_name)
    lock_txt = project_dir / LOCKED_MODULES_DIR / "locked.txt"
    lock_txt.parent.mkdir(parents=True, exist_ok=True)
    lock_txt.write_text("\n".join(sorted(locked)) + "\n")

    _log(f"  LOCKED '{module_name}' ({locked_count} file(s) → {LOCKED_MODULES_DIR}/{module_name}/)")
    return locked_count


def load_locked_source_context(project_dir: Path, dep_names: set[str]) -> str:
    """
    Build the locked-source context string for TASK 2: the actual source of all
    locked dependency modules, injected into the prompt so the model uses exact
    types and function signatures instead of inventing them.

    Returns an empty string if no locked dependencies exist.
    """
    if not dep_names:
        return ""

    parts: list[str] = []
    for dep_name in sorted(dep_names):
        lock_dir = project_dir / LOCKED_MODULES_DIR / dep_name
        if not lock_dir.exists():
            continue
        for f in sorted(lock_dir.rglob("*")):
            if not f.is_file() or f.name == "locked.txt":
                continue
            rel = f.relative_to(lock_dir)
            try:
                content = f.read_text()
                parts.append(f"=== {dep_name}/{rel} ===\n{content}")
            except Exception:
                continue

    if not parts:
        return ""

    return (
        "--- Locked dependency source (already generated and tests passing — "
        "use these exact types, signatures, and module paths in your implementation) ---\n\n"
        + "\n\n".join(parts)
    )


# ---------------------------------------------------------------------------
# Workspace stub helpers
# ---------------------------------------------------------------------------

def _write_workspace_stub(crate_type: str, module_dir: Path) -> Path:
    """
    Write a minimal entry-point stub so Cargo can parse the workspace.
    Bin crates get `fn main() {}`, all others get an empty lib stub.
    Always writes (creates or overwrites) so state is deterministic.
    Returns the path written.
    """
    if "bin" in crate_type.lower() and "lib" not in crate_type.lower():
        entry = module_dir / "src" / "main.rs"
        content = "fn main() {}\n"
    else:
        entry = module_dir / "src" / "lib.rs"
        content = "// pipeline workspace stub\n"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(content)
    return entry


# ---------------------------------------------------------------------------
# Failure classification (build error vs test logic failure)
# ---------------------------------------------------------------------------

def classify_failure(test_output: str) -> str:
    """
    BL-019: Classify a test/build failure into a specific category.

    Returns one of:
      "build"     — code did not compile (drives TASK 2 inner loop)
      "assertion" — tests ran, assertion failed (wrong return value / behavior)
      "panic"     — runtime panic not from an assertion (logic error / unwrap)
      "timeout"   — test runner timed out
      "no_tests"  — no tests ran (test harness misconfiguration)
      "test"      — catch-all for other test failures

    TASK 4 uses the sub-categories to produce more targeted genesis updates.
    """
    build_signals = [
        "error[E",
        "error: could not compile",
        "failed to compile",
        "failed to load manifest",
        "failed to parse manifest",
        "error: aborting due to",
        "cannot find",
        "unresolved import",
        "module not found",
        "ModuleNotFoundError",
        "SyntaxError",
        "Cannot find module",
        "TSError",
        "build failed",
        "BUILD FAILED",
        "CompilationError",
    ]

    output_lower = test_output.lower()
    build_score = sum(1 for s in build_signals if s.lower() in output_lower)

    if build_score > 0:
        return "build"

    if "timeout" in output_lower or "timed out" in output_lower:
        return "timeout"

    if "0 tests" in output_lower or "running 0 tests" in output_lower:
        return "no_tests"

    # Distinguish assertion failures from other panics.
    has_panic = "panicked at" in output_lower or "panicked at '" in output_lower
    has_assertion = (
        "assertion" in output_lower
        or "assert_eq" in output_lower
        or "assert_ne" in output_lower
        or "left ==" in output_lower
        or "left: " in output_lower
    )

    if has_panic and has_assertion:
        return "assertion"
    if has_panic:
        return "panic"

    test_signals = ["FAILED", "test result: FAILED", "failures:", "FAIL\t",
                    "AssertionError", "pytest"]
    if any(s.lower() in output_lower for s in test_signals):
        return "test"

    return "test"


# ---------------------------------------------------------------------------
# Chunk extraction cache
# ---------------------------------------------------------------------------

def _chunk_hash(chunk_text: str) -> str:
    """SHA-256 of the formatted chunk content used as cache key."""
    return hashlib.sha256(chunk_text.encode()).hexdigest()[:16]


def _load_cached_chunk(cache_dir: Path, chunk_hash: str) -> str | None:
    """Return cached partial text if it exists, else None."""
    p = cache_dir / f"{chunk_hash}.txt"
    return p.read_text() if p.exists() else None


def _save_cached_chunk(cache_dir: Path, chunk_hash: str, content: str):
    """Persist a chunk extraction result to the cache."""
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / f"{chunk_hash}.txt").write_text(content)


# ---------------------------------------------------------------------------
# Failure log
# ---------------------------------------------------------------------------

def append_failure_log(log_path: Path, iteration: int | str, failure_type: str,
                       pass_count: int, prev_pass_count: int, summary: str):
    """
    Append a structured entry to failure_log.md after each iteration.
    This log is fed into TASK 4 so the model can avoid repeating failed approaches.
    """
    delta = pass_count - prev_pass_count
    trend = (f"+{delta} improvement" if delta > 0
             else f"{delta} regression" if delta < 0
             else "no change")

    entry = (
        f"\n## Iteration {iteration}\n"
        f"- **Failure type:** {failure_type}\n"
        f"- **Tests passing:** {pass_count} (was {prev_pass_count}, {trend})\n"
        f"- **Summary:** {summary}\n"
    )
    with log_path.open("a") as f:
        if log_path.stat().st_size == 0:
            f.write("# Failure Log\n\nRunning record of every iteration. "
                    "Most recent entry is at the bottom.\n")
        f.write(entry)


def _log_task0c_attempt(project_dir: Path, rel_path: str, attempt_n: int,
                        block: str, compile_error: str, outcome: str) -> None:
    """BL-002/BL-016: Append one TASK 0c attempt record to the per-project JSONL log.

    BL-016: Compress the failed test block to function signatures only (saves space)
    so the full compile error can be stored uncapped.
    """
    log_path = project_dir / TASK0C_ATTEMPTS_LOG
    # BL-016: compress block to signatures — frees budget for full compile error.
    block_preview = ""
    if block:
        condensed = _condense_rust_to_signatures(block, filename="gen_tests.rs")
        block_preview = condensed[:600] if condensed else block[:300]
    entry = {
        "file": rel_path,
        "attempt": attempt_n,
        "block_preview": block_preview,
        "compile_error": compile_error,  # BL-016: no cap — full error stored
        "outcome": outcome,  # "success", "compile_error", "no_block", "reverted"
        "ts": datetime.now(UTC).isoformat(),
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    # BL-022: if this is a compile error, extract type names for type_signals.
    if outcome == "compile_error" and compile_error:
        type_re = re.compile(r'(?:expected type|mismatched types|cannot find type|'
                             r'no method named|struct|enum)\s+`?([A-Z][a-zA-Z0-9]+)`?')
        found_types = set(type_re.findall(compile_error))
        for type_name in found_types:
            # Extract a snippet of the error as pattern context
            m = re.search(re.escape(type_name), compile_error)
            pattern = compile_error[max(0, m.start() - 30):m.start() + 80].strip() if m else ""
            type_signals_write(project_dir, rel_path, type_name, difficulty=1, pattern=pattern)


def _get_task0c_history(project_dir: Path, rel_path: str, max_entries: int = 5) -> str:
    """BL-002/BL-016: Return formatted prior-attempt history for rel_path, most recent first.

    BL-016: Shows the full compile error (no cap). Block preview is already compressed
    to signatures by _log_task0c_attempt, so this doesn't re-truncate it.
    """
    log_path = project_dir / TASK0C_ATTEMPTS_LOG
    if not log_path.exists():
        return ""
    entries = []
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("file") == rel_path:
                    entries.append(entry)
            except json.JSONDecodeError:
                pass
    except Exception:
        return ""
    if not entries:
        return ""
    recent = entries[-max_entries:]
    n = len(recent)
    has_successes = any(e.get("outcome") == "success" for e in recent)
    parts = [f"Attempt history for `{rel_path}` ({n} attempt{'s' if n != 1 else ''}):"]
    for e in recent:
        outcome = e.get("outcome", "unknown")
        err = e.get("compile_error", "")
        preview = e.get("block_preview", "")
        result_label = "compiled and is COMMITTED TO DISK" if outcome == "success" else f"FAILED ({outcome})"
        entry_lines = [f"Attempt {e.get('attempt', '?')}: → {result_label}"]
        if preview:
            entry_lines.append(f"Test signatures:\n{preview}")
        if err:
            err_capped = err[-_FIX_ERR_CAP:] if len(err) > _FIX_ERR_CAP else err
            entry_lines.append(f"Compiler said:\n{err_capped}")
        if outcome == "success":
            entry_lines.append(
                "→ These tests ARE ALREADY COMMITTED as a gen_tests_N module on disk. "
                "Do NOT include them in your new block — they are already active as gen_tests_N modules."
            )
        else:
            entry_lines.append("→ This approach did not work. Do NOT repeat it.")
        parts.append("\n".join(entry_lines))
    if has_successes:
        parts.append(
            "The committed tests above are already on disk as gen_tests_N modules. "
            "Do NOT copy or repeat them. Write ONLY NEW test functions "
            "targeting the uncovered lines shown in fn_bodies below."
        )
    else:
        parts.append("Write a new attempt that avoids every failure pattern above.")
    return "\n\n".join(parts)


def load_failure_log(log_path: Path, max_chars: int = 0) -> str:
    """Return the full failure log. Each entry is ~100-150 chars; 100 iterations ≈ 15k chars,
    well within any model context. max_chars is ignored but kept for call-site compatibility."""
    return read(log_path)


# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).


# ---------------------------------------------------------------------------
# Step output validators (anti-corruption layer)
# ---------------------------------------------------------------------------

# Words that appear in any English text and carry no signal about whether the
# requirements document contains module-specific identifiers.
_STOP_WORDS = {
    "the", "and", "for", "are", "not", "use", "let", "mut", "pub", "fn",
    "mod", "impl", "struct", "enum", "trait", "type", "this", "that",
    "with", "from", "into", "self", "true", "false", "none", "some",
    "return", "match", "where", "else", "then", "each", "when", "which",
    "must", "should", "will", "can", "all", "any", "one", "new", "get",
    "set", "add", "run", "has", "its", "if", "in", "of", "to", "at",
    "as", "is", "it", "on", "do", "no", "be", "by", "or", "an", "a",
    "always", "never", "only", "also", "both", "either", "every",
    "given", "used", "list", "item", "value", "values", "name", "names",
    "error", "result", "output", "input", "string", "number", "data",
    "file", "path", "field", "method", "function", "module",
    "crate", "test", "tests", "assert", "check", "build", "code",
}

_META_PHRASES = [
    "this document describes",
    "functional requirements are",
    "the pipeline",
    "this requirement",
    "the following requirements",
    "requirements document",
    "requirement specification",
    "the purpose of this",
]


def _extract_identifiers(text: str) -> set[str]:
    """Extract CamelCase and snake_case identifiers from text, filtered by stop-words."""
    raw = set(re.findall(r'\b([A-Z][a-zA-Z0-9]{1,}|[a-z][a-z0-9_]{2,}[a-z0-9])\b', text))
    return {w for w in raw if w.lower() not in _STOP_WORDS}


def validate_task1_output(req_path: Path, test_files: list) -> tuple[bool, str]:
    """
    Anti-corruption check before TASK 2.
    Returns (ok, reason). All checks are deterministic — no LLM calls.
    """
    content = req_path.read_text(encoding="utf-8") if req_path.exists() else ""

    if len(content) < 500:
        return False, f"too short ({len(content)} chars, need ≥ 500)"

    content_lower = content.lower()
    for phrase in _META_PHRASES:
        if phrase in content_lower:
            return False, f"meta-commentary detected ('{phrase}')"

    req_idents = _extract_identifiers(content)
    test_idents: set[str] = set()
    for _, tc in test_files:
        test_idents.update(_extract_identifiers(tc))

    overlap = req_idents & test_idents
    if len(overlap) < 3:
        sample = sorted(overlap)[:5]
        return False, (
            f"requirements too generic — only {len(overlap)} identifiers overlap "
            f"with test files (need ≥ 3). Overlap: {sample}"
        )

    return True, "ok"


# Regex patterns for TASK 2 placeholder detection.
_PLACEHOLDER_RE = re.compile(
    r'unimplemented!\s*\(\)|todo!\s*\(\)|#\s*TODO|#\s*FIXME|pass\s*$|raise\s+NotImplementedError',
    re.MULTILINE,
)
_COMMENT_RE = re.compile(r'//[^\n]*|/\*.*?\*/|#[^\n]*', re.DOTALL)


def validate_task2_output(project_dir: Path, expected_paths: list[str],
                          req_path: Path | None = None) -> tuple[bool, str]:
    """
    Anti-corruption check before TASK 3.
    Checks: all files present, none empty, none placeholder-only, named types exist in code.
    """
    if not expected_paths:
        return True, "ok (no expected paths recorded)"

    missing = [p for p in expected_paths if not (project_dir / p).exists()]
    if missing:
        return False, f"missing files: {', '.join(missing[:5])}"

    tiny = [
        p for p in expected_paths
        if (project_dir / p).exists() and (project_dir / p).stat().st_size < 50
    ]
    if tiny:
        return False, f"empty/stub files (< 50 bytes): {', '.join(tiny[:5])}"

    # Placeholder check: reject files where all non-comment, non-whitespace content
    # is unimplemented!()/todo!()/pass/etc. These compile but contain no real logic.
    placeholder_files = []
    all_source = ""
    for p in expected_paths:
        fp = project_dir / p
        if not fp.exists():
            continue
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        all_source += raw
        stripped = _COMMENT_RE.sub("", raw)
        stripped = re.sub(r'\s+', '', stripped)
        placeholders_found = len(_PLACEHOLDER_RE.findall(raw))
        non_placeholder_len = len(re.sub(r'\s+', '', _PLACEHOLDER_RE.sub("", stripped)))
        if placeholders_found > 0 and non_placeholder_len < 50:
            placeholder_files.append(p)
    if placeholder_files:
        return False, f"placeholder-only files (all unimplemented!/todo!): {', '.join(placeholder_files[:3])}"

    # Type-presence check: CamelCase types named in requirements must appear as
    # struct/enum definitions in the generated code.
    if req_path and req_path.exists():
        req_content = req_path.read_text(encoding="utf-8", errors="ignore")
        named_types = set(re.findall(r'\b([A-Z][a-zA-Z0-9]{2,})\b', req_content))
        named_types -= {"README", "TODO", "FIXME", "API", "URL", "CLI", "LLM", "AI"}
        if named_types:
            missing_types = [
                t for t in sorted(named_types)
                if not re.search(rf'\b(?:struct|enum|trait)\s+{re.escape(t)}\b', all_source)
            ]
            # Only fail if more than half the named types are absent — allows for
            # types that are re-exported or defined via macros.
            if len(missing_types) > max(1, len(named_types) // 2):
                return False, (
                    f"types from requirements not defined in generated code "
                    f"({len(missing_types)}/{len(named_types)} missing): "
                    f"{', '.join(missing_types[:5])}"
                )

    return True, "ok"


_PUB_ITEM_RE = re.compile(
    r'\bpub(?:\([^)]*\))?\s+'
    r'(?:unsafe\s+)?(?:async\s+)?(?:const\s+)?(?:default\s+)?'
    r'(fn|struct|enum|trait|type|mod)\s+(\w+)',
)


def _extract_pub_items(text: str) -> set[tuple[str, str]]:
    """Return {(kind, name)} for every pub item in the API-surface LISTING artifact.

    NOTE: this parses the `_extract_public_symbols` *listing* (`pub struct X { ... }`,
    `pub fn sig`, …), which is a summary artifact, NOT valid Rust source — so syn cannot
    parse it and the regex is legitimate here (Hard Rule applies to Rust *source*). The
    generated-source side uses `_pub_items_from_tree` (syn). See R7.2 in CODE_ARCHITECTURE.md.
    """
    return {(m.group(1), m.group(2)) for m in _PUB_ITEM_RE.finditer(text)}


def _pub_items_from_tree(src_dir: Path) -> set[tuple[str, str]]:
    """Return {(kind, name)} for pub struct/enum/trait/fn in a Rust source tree, via the syn
    extractor (no regex on source). Excludes examples/benches/tests + scratch dirs by record
    path. Only these kinds matter for the conformance check — the API-surface listing never
    contains `type`/`mod`/`const` items."""
    def _excluded(rel: str) -> bool:
        parts = set(Path(rel).parts)
        return bool(parts & {"examples", "benches", "tests"}) or bool(parts & SKIP_DIR_NAMES)

    out: set[tuple[str, str]] = set()
    for t in _enumerate_types_via_extractor(src_dir) or []:
        if t.is_pub and t.kind in {"struct", "enum", "trait"} and not _excluded(t.file):
            out.add((t.kind, t.name))
    for f in _enumerate_fns_via_extractor(src_dir) or []:   # dicts (engine contract)
        if f.get("is_pub") and not _excluded(f.get("file", "")):
            out.add(("fn", f.get("fn_name", "")))
    return out


def validate_task2_api_surface_conformance(
    project_dir: Path, module_name: str, module_dir: Path,
) -> tuple[bool, str]:
    """Compare pub items in the regenerated source to those captured pre-strip
    in the api_surface. Returns (ok, drift_report).

    Soft check: missing pub items are reported but do NOT block the iteration.
    The drift report is logged and (when present) appended to test_output before
    TASK 4 so the methodology refinement sees both the cargo failure and the
    contract violation in one prompt. Use this signal to spot "LLM ignored the
    given api_surface" failures distinctly from "tests genuinely don't pass".
    """
    api_text = load_api_surface(project_dir, module_name)
    if not api_text:
        return True, "no api_surface captured — skipping conformance check"

    api_items = _extract_pub_items(api_text)
    if not api_items:
        return True, "api_surface contained no pub items — skipping"

    # Generated source — parsed with syn (the extractor walks the tree itself), NOT regex.
    src_root = module_dir / "src"
    walk_root = src_root if src_root.is_dir() else module_dir
    gen_items = _pub_items_from_tree(walk_root)

    missing = api_items - gen_items
    if not missing:
        return True, f"ok — all {len(api_items)} pub item(s) present in regenerated source"

    by_kind: dict[str, list[str]] = {}
    for kind, name in sorted(missing):
        by_kind.setdefault(kind, []).append(name)
    parts = [f"{kind}: {', '.join(names)}" for kind, names in sorted(by_kind.items())]
    report = (
        f"API drift — {len(missing)} of {len(api_items)} pub item(s) missing from "
        f"regenerated source: " + "; ".join(parts)
    )
    return False, report


def validate_task3_output(pass_count: int, fail_count: int,
                          module_has_tests: bool) -> tuple[str, str]:
    """
    Anti-corruption check on test results — LEGACY non-strict variant kept
    for the few call sites that have no cluster context. The current TASK 3
    path uses `validate_task3_output_strict` below.

    Returns (status, reason) where status is one of:
      'pass'      — all tests passed, lock the module
      'fail'      — tests ran but failed, continue to TASK 4
      'no_tests'  — 0 tests ran despite test files existing (structural problem)
      'no_files'  — module has no test files (expected — lock on build success)
    """
    if pass_count > 0 and fail_count == 0:
        return "pass", f"{pass_count} tests passed"
    if not module_has_tests and pass_count == 0 and fail_count == 0:
        return "no_files", "module has no test files — lock on build success"
    if pass_count == 0 and fail_count == 0:
        return "no_tests", (
            "0 tests ran despite test files existing — "
            "test binary may be empty, harness may not compile, or test filter excludes everything"
        )
    return "fail", f"{pass_count} passed, {fail_count} failed"


_TEST_ATTR_RE = re.compile(
    r"#\s*\[\s*(?:test|tokio::test|async_std::test|"
    r"serial_test::serial|rstest|parameterized|test_case)\s*[\s\],(]",
)


def _count_test_attrs_in_text(text: str) -> int:
    """Count `#[test]`-family attribute occurrences in a Rust source string.
    Naive but adequate: counts every macro-attribute that recognises as a
    test marker. Used by the per-cluster expected_count computation.

    Regex is deliberate here, NOT a no-regex-on-source violation: this counts markers in
    generated test code that may not yet be valid Rust (mid-fill), where syn would fail to
    parse and under-count. A robust marker count is the right tool. See CODE_ARCHITECTURE.md
    §8 R7.3."""
    return len(_TEST_ATTR_RE.findall(text or ""))


def _count_test_fns_cfg_aware(
    text: str,
    cfg_true: set[str],
    cfg_false: set[str],
) -> int:
    """Count `#[test]`-attributed fn declarations in `text` whose surrounding
    `#[cfg(...)]` attributes evaluate true (or undecidable) under the given
    cluster cfg sets. A `#[test]` fn with a `#[cfg(X)]` where X is in
    cfg_false is NOT counted — cargo won't compile it under this cluster
    and we shouldn't expect it to report a verdict.

    Pragmatic line-level walker: for each line containing `#[test]`,
    look at the contiguous block of `#[...]` attribute lines around the
    fn declaration. Collect every `#[cfg(...)]` predicate. If any of
    them evaluates false under (cfg_true, cfg_false), drop the fn.
    Otherwise count it (true or undecidable predicates → still counted).
    """
    lines = text.splitlines()
    test_attr_re = re.compile(
        r"^\s*#\s*\[\s*(?:test|tokio::test|async_std::test|"
        r"serial_test::serial|rstest|parameterized|test_case)\s*[\s\],(]",
    )
    cfg_inline_re = re.compile(
        r"#\s*\[\s*cfg\s*\(([^]]+)\)\s*\]",
    )
    attr_line_re = re.compile(r"^\s*#\s*\[")
    count = 0
    for i, line in enumerate(lines):
        if not test_attr_re.search(line):
            continue
        # Walk backward across the attribute block above this #[test] to
        # collect cfg predicates that gate this fn.
        gates: list[str] = []
        for cm in cfg_inline_re.finditer(line):
            gates.append(cm.group(1).strip())
        j = i - 1
        while j >= 0:
            s = lines[j].strip()
            if not s:
                break  # blank line ends the attribute block
            if s.startswith("//") or s.startswith("/*"):
                j -= 1
                continue
            if not attr_line_re.match(lines[j]):
                break  # not an attribute line — chain ends
            for cm in cfg_inline_re.finditer(lines[j]):
                gates.append(cm.group(1).strip())
            j -= 1
        # If any gate is definitively False, skip this test.
        excluded = False
        for g in gates:
            v = _eval_cfg_pred(g, cfg_true, cfg_false)
            if v is False:
                excluded = True
                break
        if not excluded:
            count += 1
    return count


def _count_active_tests_for_cluster(
    project_dir: Path,
    cluster: "BuildCluster | None",
) -> int:
    """FR-024 strict-verdict helper: how many `#[test]` fns SHOULD have run
    if cargo test compiles every test source active under `cluster`'s cfg.

    Counts three sources, all cfg-filtered against the cluster's
    (cfg_true, cfg_false) sets so cargo-cannot-compile tests don't inflate
    the expected count:

      1. `tests/**/*.rs` — integration tests; per-test cfg attrs filter.
      2. `src/*.gen_tests_*.rs` — gen_tests siblings; included only if
         the parent file's `#[path] mod gen_tests_N;` decl is active
         under this cluster's cfg (the decl carries the gate).
      3. `src/**/*.rs` (excluding gen_tests siblings) — per-test cfg
         attrs filter, same as tests/.

    Returns the cfg-aware total. Used to gate SUCCESS: cargo must report
    at least this many tests across all binaries before TASK 3 accepts.
    """
    if cluster is None:
        cfg_true: set[str] = set()
        cfg_false: set[str] = set()
    else:
        cfg_true = set(getattr(cluster, "cfg_true", []) or [])
        cfg_false = set(getattr(cluster, "cfg_false", []) or [])

    total = 0
    # 1. tests/**/*.rs — cfg-filtered per-test.
    tests_dir = project_dir / "tests"
    if tests_dir.is_dir():
        for tp in tests_dir.rglob("*.rs"):
            try:
                text = tp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            total += _count_test_fns_cfg_aware(text, cfg_true, cfg_false)

    src_dir = project_dir / "src"
    if not src_dir.is_dir():
        return total

    # 2. gen_tests siblings — gated by their parent file's #[path] decl.
    decl_re = re.compile(
        r"(?:#\[cfg\(([^)]+)\)\][^\n]*\n)?"
        r"\s*#\[cfg\(test\)\][^\n]*\n"
        r"(?:\s*#\[[^\n]*\][^\n]*\n)*"
        r'\s*#\[path\s*=\s*"([^"]+\.gen_tests_\d+\.rs)"\][^\n]*\n'
        r"\s*mod\s+gen_tests_\d+\s*;",
    )
    for parent in src_dir.glob("*.rs"):
        if re.search(r"\.gen_tests_\d+\.rs$", parent.name):
            continue
        try:
            ptext = parent.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in decl_re.finditer(ptext):
            gate = (m.group(1) or "").strip()
            sib_name = m.group(2)
            if gate:
                v = _eval_cfg_pred(gate, cfg_true, cfg_false)
                if v is False:
                    continue
            sib = src_dir / sib_name
            if not sib.exists():
                continue
            try:
                stext = sib.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            # gen_tests bodies don't have additional cfg gates on
            # individual #[test] fns (they're written by task0c, which
            # doesn't emit per-test cfgs), so cfg-aware vs naive count
            # produces the same result here.
            total += _count_test_fns_cfg_aware(stext, cfg_true, cfg_false)

    # 3. src/**/*.rs (excluding gen_tests siblings) — inline test mods.
    for srf in src_dir.rglob("*.rs"):
        if re.search(r"\.gen_tests_\d+\.rs$", srf.name):
            continue
        try:
            stext = srf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        total += _count_test_fns_cfg_aware(stext, cfg_true, cfg_false)
    return total


# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).


def validate_task4_output(genesis_before: str, genesis_after: str,
                          test_output: str,
                          failure_type: str = "test") -> tuple[bool, str]:
    """
    Anti-corruption check on genesis.md update.
    Returns (ok, reason). A stall or duplicate is flagged but not fatal.
    """
    if genesis_after.strip() == genesis_before.strip():
        return False, "genesis_stall — TASK 4 produced no update to genesis.md"

    if len(genesis_after) <= len(genesis_before):
        return False, "genesis shrank — TASK 4 may have removed content"

    new_text = genesis_after[len(genesis_before):]

    # R9.5: the identifier-overlap check is a TEST-failure heuristic. For BUILD
    # failures test_output is compiler errors (project-specific idents) but the
    # build prompt MANDATES general, identifier-free structural rules — so a correct
    # update has empty intersection. Applying it there contradicts the prompt and
    # falsely stalls legitimate updates. Skip it for build; keep stall/shrank/dedup.
    if failure_type != "build":
        # Verify new content references at least one identifier from the test output.
        test_idents = _extract_identifiers(test_output)
        new_idents = _extract_identifiers(new_text)
        if test_idents and not (test_idents & new_idents):
            return False, "genesis update contains no identifiers from the failing test output"

    # Deduplication: flag if any substantial new paragraph already exists verbatim.
    new_paragraphs = [p.strip() for p in new_text.split("\n\n") if len(p.strip()) > 100]
    for para in new_paragraphs:
        if para in genesis_before:
            return False, "genesis update duplicates existing content (paragraph already present)"

    return True, "ok"


# ---------------------------------------------------------------------------
# Resume detection
# ---------------------------------------------------------------------------

def detect_resume_point(root: Path, project_dir: Path, manifest_path: Path,
                        genesis_path: Path, functional_req_path: Path) -> tuple:
    """
    Inspect existing files to determine exactly where the previous run stopped.
    Returns (iteration, start_task) where start_task is 1-6.

    State machine:
      The four observable signals are:
        - max_n:            highest N in tests_output_N.txt (0 if none)
        - has_functional:   functional_requirements.md exists
        - has_manifest:     .generated_files.txt exists

      Possible states and their meaning:
        max_n=0, no functional, no manifest  -> clean start, TASK 1 of iter 1
        max_n=0, functional,    no manifest  -> TASK 1 done, resume TASK 2 of iter 1
        max_n=0, functional,    manifest     -> TASK 2 done, resume TASK 3 of iter 1
        max_n=0, no functional, manifest     -> corrupt state, clean up, TASK 1 of iter 1

        max_n>0, no functional, no manifest  -> iteration max_n complete, TASK 1 of max_n+1
        max_n>0, functional,    no manifest  -> TASK 1 of max_n+1 done, resume TASK 2
        max_n>0, functional,    manifest     -> TASK 2 of max_n+1 done OR crash after
                                               TASK 3 of max_n; safest: resume TASK 3
        max_n>0, no functional, manifest     -> TASK 5 done, TASK 6 not done; finish
                                               cleanup then TASK 1 of max_n+1
    """
    existing = list(project_dir.glob("tests_output_*.txt"))
    max_n = 0
    if existing:
        max_n = max(
            int(re.search(r"tests_output_(\d+)\.txt", p.name).group(1))
            for p in existing
        )

    has_functional = functional_req_path.exists()
    has_manifest = manifest_path.exists()

    if max_n == 0:
        if not has_functional and not has_manifest:
            _log("Clean start: beginning from iteration 1, TASK 1.")
            return 1, 1
        if has_functional and not has_manifest:
            _log("Resume: TASK 1 completed. Resuming iteration 1 from TASK 2.")
            return 1, 2
        if has_functional and has_manifest:
            _log("Resume: TASK 2 completed. Resuming iteration 1 from TASK 3.")
            return 1, 3
        # manifest but no functional_req: corrupt, clean up
        _log("Unexpected state (manifest without functional_requirements.md). Cleaning up.")
        delete_source_files(project_dir, manifest_path)
        return 1, 1

    # max_n > 0
    next_iter = max_n + 1

    if not has_functional and not has_manifest:
        _log(f"Resume: iteration {max_n} complete. Starting iteration {next_iter} from TASK 1.")
        return next_iter, 1

    if has_functional and not has_manifest:
        _log(f"Resume: TASK 1 of iteration {next_iter} completed. Resuming from TASK 2.")
        return next_iter, 2

    if has_functional and has_manifest:
        # Ambiguous: could be after TASK 2 of next_iter OR after TASK 3 of max_n.
        # Safest resolution: treat as after TASK 2 of next_iter and run TASK 3.
        # Worst case: we re-run tests once with the same code; harmless.
        _log(f"Resume: source files present. Resuming iteration {next_iter} from TASK 3.")
        return next_iter, 3

    # no functional_req, manifest exists: TASK 5 done, TASK 6 not done
    _log(f"Resume: TASK 5 of iteration {max_n} done. Completing TASK 6 then starting {next_iter}.")
    delete_source_files(project_dir, manifest_path)
    return next_iter, 1


# ---------------------------------------------------------------------------
# Test file collection -- language agnostic
# ---------------------------------------------------------------------------

def is_test_file(path: Path, project_dir: Path) -> bool:
    """
    Determine if a file is a test file using path and name conventions that
    apply across languages. Does not inspect file contents.
    """
    rel = path.relative_to(project_dir)

    # Any directory component is a well-known test directory name.
    if any(part.lower() in TEST_DIR_NAMES for part in rel.parts[:-1]):
        return True

    stem = path.stem           # filename without final extension, case preserved
    stem_lower = stem.lower()

    # Files named exactly "test" or "spec" in any language.
    if stem_lower in {"test", "spec", "tests", "specs"}:
        return True

    # snake_case conventions: test_foo.py, foo_test.go
    if stem_lower.startswith("test_") or stem_lower.endswith("_test"):
        return True

    # CamelCase conventions: FooTest.java, FooSpec.rb, FooTests.php
    if stem.endswith(("Test", "Tests", "Spec", "Specs")):
        return True

    # Dot-separated conventions: foo.test.js, foo.spec.ts
    if len(path.suffixes) >= 2 and path.suffixes[-2].lower() in (".test", ".spec"):
        return True

    # Pipeline-generated gen_tests siblings: foo.gen_tests_3.rs
    return _match_gen_tests_sibling(path.name) is not None


def collect_test_files(project_dir: Path):
    """Return (rel_path, content) pairs for every file containing test code.

    Used by TASK 1 to read the full test spec. Includes:
    - Pure test files: identified by name/location (is_test_file) — e.g. tests/*.rs
    - Rust source files with inline test blocks (#[cfg(test)] or #[test]) — e.g. src/lib.rs
      after stripping, these contain the test spec that TASK 1 must read.

    NOTE: this is the READ set for TASK 1. For the WRITE protection set (files TASK 2
    must never overwrite), use collect_protected_test_files() instead — it excludes
    inline test files in src/ so TASK 2 can regenerate them with new implementation.
    """
    result = []
    for p in sorted(project_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(project_dir)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        try:
            content = p.read_text()
        except Exception:
            continue
        if is_test_file(p, project_dir):
            result.append((str(rel), content))
        elif p.suffix == ".rs" and ("#[cfg(test)]" in content or "#[test]" in content):
            # Skip stripped source files that contain only gen_tests path-mod
            # declarations — their actual test content lives in sibling files
            # (which are picked up separately by is_test_file).
            if _is_gen_tests_decl_only_src(content):
                continue
            result.append((str(rel), content))
    return result


def collect_examples(project_dir: Path, module_dir: Path | None = None) -> list[tuple[str, str]]:
    """Return (rel_path, content) for example source files.

    Examples sit in <crate>/examples/ and demonstrate idiomatic API usage. They
    are not assertions — TASK 1 should treat them as usage signal, not as
    compliance targets. Filtered to module_dir when supplied (multi-crate
    workspaces).
    """
    roots = [project_dir] if module_dir is None else [module_dir]
    result: list[tuple[str, str]] = []
    for root in roots:
        ex_dir = root / "examples"
        if not ex_dir.is_dir():
            continue
        for p in sorted(ex_dir.rglob("*.rs")):
            if any(part in SKIP_DIR_NAMES for part in p.relative_to(project_dir).parts):
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except OSError:
                continue
            result.append((str(p.relative_to(project_dir)), content))
    return result


def collect_protected_test_files(project_dir: Path) -> set[str]:
    """Return rel paths of files TASK 2 must NEVER overwrite.

    Only pure test files: those in tests/ directories or named like test files.
    Excludes source files (src/lib.rs, src/foo.rs) that happen to contain inline
    #[cfg(test)] blocks — those must be regeneratable by TASK 2 so it can write
    the implementation back into them alongside the test blocks.
    """
    result = set()
    for p in sorted(project_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(project_dir)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        if is_test_file(p, project_dir):
            result.add(str(rel))
    return result


def format_test_files(test_files):
    parts = []
    for rel_path, content in test_files:
        parts.append(f"=== {rel_path} ===\n{content}")
    return "\n\n".join(parts)


def _split_at_semantic_boundaries(content: str, part_size: int) -> list[str]:
    """
    Split content into parts of at most part_size chars, preferring to break
    at semantic boundaries in this order:
      1. Test function boundaries (lines starting with #[test], def test_, it(, etc.)
      2. Blank lines (paragraph boundaries)
      3. Hard character split (last resort)

    This ensures each part contains coherent, self-contained test logic rather
    than an arbitrary fragment mid-function.
    """
    if len(content) <= part_size:
        return [content]

    # Detect test function boundary markers across languages.
    BOUNDARY_RE = re.compile(
        r'^(\s*#\[(?:test|cfg\(test\))\]'   # Rust: #[test], #[cfg(test)]
        r'|\s*def test_'                      # Python
        r'|\s*fn test_'                       # Rust (without attribute)
        r'|\s*(?:it|test|describe)\s*\('      # JS/TS
        r'|\s*func Test'                      # Go
        r'|\s*@Test'                          # Java/Kotlin
        r')',
        re.MULTILINE,
    )

    lines = content.splitlines(keepends=True)
    parts = []
    current_chars = 0
    current_lines: list[str] = []
    i = 0

    def emit():
        if current_lines:
            parts.append("".join(current_lines))
            current_lines.clear()

    while i < len(lines):
        line = lines[i]
        # Check if adding this line would overflow the part.
        if current_lines and current_chars + len(line) > part_size:
            # Try to find a semantic break in current_lines (scan backwards).
            split_at = None
            for j in range(len(current_lines) - 1, 0, -1):
                if BOUNDARY_RE.match(current_lines[j]):
                    split_at = j
                    break
            if split_at is None:
                # No test boundary — try blank line.
                for j in range(len(current_lines) - 1, 0, -1):
                    if current_lines[j].strip() == "":
                        split_at = j + 1
                        break
            if split_at is not None and split_at > 0:
                # Emit up to split_at, keep the rest.
                overflow = current_lines[split_at:]
                del current_lines[split_at:]
                emit()
                current_lines.extend(overflow)
                current_chars = sum(len(ln) for ln in current_lines)
            else:
                # Hard split — no good boundary found.
                emit()
                current_chars = 0

        current_lines.append(line)
        current_chars += len(line)
        i += 1

    emit()
    return parts


def chunk_test_files(test_files, char_limit):
    """Split test files into groups where each group fits within char_limit.

    Files larger than char_limit are split into labeled sub-chunks at semantic
    boundaries (test function / blank line), not arbitrary character positions.
    This ensures the model always receives complete, coherent test logic.
    """
    chunks = []
    current: list[tuple[str, str]] = []
    current_size = 0

    def flush():
        if current:
            chunks.append(list(current))
            current.clear()

    for rel_path, content in test_files:
        header_size = len(rel_path) + 10  # accounts for "=== path ===" wrapper
        if len(content) + header_size > char_limit:
            # File is too large to fit alone — split at semantic boundaries.
            flush()
            part_size = char_limit - header_size
            parts = _split_at_semantic_boundaries(content, part_size)
            total_parts = len(parts)
            for idx, part in enumerate(parts, 1):
                label = f"{rel_path} (part {idx}/{total_parts})"
                chunks.append([(label, part)])
        else:
            entry_size = header_size + len(content)
            if current and current_size + entry_size > char_limit:
                flush()
                current_size = 0
            current.append((rel_path, content))
            current_size += entry_size

    flush()
    return chunks


def chunk_test_files_by_file(test_files, file_char_limit):
    """One chunk per test file (FR-001). Files exceeding file_char_limit are split at
    semantic boundaries (test function / blank line). This ensures the model always
    processes one complete, coherent, named file per extraction call — independent of
    context window size.
    """
    chunks = []
    for rel_path, content in test_files:
        header_size = len(rel_path) + 10
        if len(content) + header_size > file_char_limit:
            part_size = file_char_limit - header_size
            parts = _split_at_semantic_boundaries(content, part_size)
            total_parts = len(parts)
            for idx, part in enumerate(parts, 1):
                label = f"{rel_path} (part {idx}/{total_parts})"
                chunks.append([(label, part)])
        else:
            chunks.append([(rel_path, content)])
    return chunks


# ---------------------------------------------------------------------------
# Startup: discover and validate the test command from README.md
# ---------------------------------------------------------------------------

def discover_test_command(readme_text: str, project_dir: Path | None = None) -> str:
    """
    Ask the configured model to extract the primary test command from README.md,
    Cargo.toml, and other project files. Falls back to inferred convention if none found.
    Retries if the result is empty.
    """
    # Collect available project signals beyond the README.
    extra_sections: list[str] = []
    if project_dir:
        for extra_path, label in [
            (project_dir / "Cargo.toml", "Cargo.toml"),
            (project_dir / ".cargo" / "config.toml", ".cargo/config.toml"),
            (project_dir / "Makefile", "Makefile"),
            (project_dir / "package.json", "package.json"),
        ]:
            if extra_path.exists():
                try:
                    content = extra_path.read_text(encoding="utf-8")[:_EXTRA_CAP]
                    extra_sections.append(f"{label}:\n```\n{content}\n```")
                except Exception:
                    pass

    context_block = ("\n\n" + "\n\n".join(extra_sections)) if extra_sections else ""

    if readme_text.strip() or extra_sections:
        _log(f"Initializing: asking {MODEL} to discover test command from project files...")
        readme_block = f"README.md:\n{readme_text}\n" if readme_text.strip() else "README.md: (not present or empty)\n"
        prompt = f"""Determine the single primary shell command used to run the full test suite for this project.

Check all provided files — README.md may not mention testing; if so, use Cargo.toml, package.json, Makefile, or project-type conventions to infer the correct command.

Rules:
- Output only the command on one line. No explanation, no markdown fences, no alternatives.
- If multiple test commands exist, pick the one that runs all tests.
- If Cargo.toml is present, default is `cargo test` unless the README or Makefile says otherwise.
- If a rust-toolchain.toml / rust-toolchain file or the README pins a channel (e.g. nightly),
  prefix it: `cargo +nightly test`. Likewise include any global flag the docs say is REQUIRED
  to compile the tests at all (e.g. `--all-features`). Do NOT add per-feature/per-cfg variants —
  those are handled separately per build configuration.
- If package.json is present with a "test" script, use `npm test`.

{readme_block}{context_block}
"""
    else:
        _log(f"Initializing: no project files found -- asking {MODEL} to infer test command from conventions...")
        prompt = """No project files are available. Output the single most common shell command used to run
the full test suite for a typical software project (e.g. `cargo test`, `pytest`, `npm test`).
Output only the command on one line. No explanation, no markdown fences.
"""

    # Known command prefixes across common project types.
    KNOWN_PREFIXES = (
        "cargo", "pytest", "python", "npm", "npx", "yarn", "pnpm",
        "go ", "mvn", "gradle", "gradlew", "make", "rake", "mix",
        "dotnet", "swift", "stack", "cabal", "mix ", "jest", "mocha",
    )

    def _extract_cmd_from_response(response: str) -> str:
        """
        Extract the shell command from a model response that may contain prose.
        Strategy:
          1. If a backtick-quoted string on any line starts with a known prefix, use it.
          2. Otherwise take the first non-fence line and strip leading prose up to ': '.
          3. Strip leading `$ ` shell prompt markers.
        """
        # 1. Find backtick-quoted content that looks like a command.
        for match in re.finditer(r'`([^`]+)`', response):
            candidate = match.group(1).lstrip("$ ./")
            first = candidate.split()[0].lower() if candidate.split() else ""
            if any(first.startswith(p.strip()) for p in KNOWN_PREFIXES) or "/" in first:
                return match.group(1).lstrip("$ ")
        # 2. Take first non-fence line, strip any prose prefix before ': '.
        lines = [ln.strip() for ln in response.splitlines()
                 if ln.strip() and not ln.strip().startswith("```")]
        if not lines:
            return ""
        line = lines[0]
        if ": " in line:
            # "The command is: cargo test" -> "cargo test"
            after_colon = line.split(": ", 1)[1].strip().lstrip("`").rstrip("`")
            if after_colon:
                line = after_colon
        return line.lstrip("$ ")

    def attempt():
        response = ask(prompt, model=_TASK0_MODEL).strip()
        cmd = _extract_cmd_from_response(response)
        if not cmd:
            _log(f"  WARNING: {MODEL} returned an empty test command.")
            return False
        # Reject if it still looks like prose (no known command prefix).
        first_word = cmd.split()[0].lower() if cmd.split() else ""
        looks_like_cmd = (
            any(first_word.startswith(p.strip()) for p in KNOWN_PREFIXES)
            or "/" in first_word
            or first_word.endswith(".sh")
        )
        if not looks_like_cmd:
            _log(f"  WARNING: could not extract a command from response: {response[:120]!r}")
            return False
        return cmd

    cmd = with_retry(attempt)
    if cmd is False:
        _log("ERROR: could not extract a test command from README.md after all retries.")
        sys.exit(1)

    # Normalize bare tool names that require a subcommand to be valid.
    # e.g. "cargo" → "cargo test", "npm" → "npm test"
    BARE_TOOL_DEFAULTS = {
        "cargo": "cargo test",
        "npm": "npm test",
        "npx": "npx jest",
        "yarn": "yarn test",
        "pnpm": "pnpm test",
    }
    bare = cmd.strip().lower()
    if bare in BARE_TOOL_DEFAULTS:
        normalized = BARE_TOOL_DEFAULTS[bare]
        _log(f"  Normalized bare tool name '{cmd}' → '{normalized}'")
        cmd = normalized

    # Sanity-check: if project is clearly Rust (has Cargo.toml) but command
    # doesn't use cargo, reject and fall back to `cargo test`.
    if project_dir and (project_dir / "Cargo.toml").exists() and not cmd.startswith("cargo"):
        _log(f"  WARNING: extracted command '{cmd}' looks wrong for a Rust project — "
             f"defaulting to 'cargo test'.")
        cmd = "cargo test"

    _log(f"Test command: {cmd}")
    return str(cmd)


# ---------------------------------------------------------------------------
# Source file manifest
# ---------------------------------------------------------------------------

def save_manifest(manifest_path: Path, files: list):
    write(manifest_path, "\n".join(files))


def load_manifest(manifest_path: Path) -> list:
    content = read(manifest_path)
    if not content:
        return []
    return [line for line in content.splitlines() if line.strip()]


def write_source_files(files_dict: dict, project_dir: Path, manifest_path: Path,
                       existing_test_files: set) -> int:
    """
    Write source files to disk and record them in the manifest.
    Skips any path that matches an existing test file or a protected project file
    (README.md, LICENSE, etc.) that must never be overwritten.
    Returns the number of files actually written.
    """
    # Files that must never be overwritten regardless of what the model generates.
    PROTECTED = {"README.md", "readme.md", "README.rst", "LICENSE", "LICENSE.md",
                 "LICENSE.txt", "Cargo.lock"}
    written = []
    skipped = []
    RUST_SOURCE_DIRS = {'src', 'tests', 'benches', 'examples', 'build'}
    for rel_path, content in files_dict.items():
        # Strip leading slashes — model sometimes returns absolute-looking paths like
        # /argh_derive/src/lib.rs instead of argh_derive/src/lib.rs. Python's Path /
        # operator discards the left operand when the right side is absolute, which
        # would escape the project directory entirely.
        rel_path = rel_path.lstrip("/")
        # Normalise Rust source paths: insert src/ if the file sits directly under a
        # crate directory with no intermediate source-subdir component.
        # e.g. "argh_shared/traits.rs" → "argh_shared/src/traits.rs"
        if Path(rel_path).suffix == '.rs':
            parts = Path(rel_path).parts
            if len(parts) == 2 and parts[0] not in RUST_SOURCE_DIRS:
                fixed = str(Path(parts[0]) / 'src' / parts[1])
                _log(f"  PATH NORMALISED: {rel_path} → {fixed}")
                rel_path = fixed
            # For library crates the entry point must be lib.rs, not main.rs.
            # If the model generates */src/main.rs but */src/lib.rs already exists
            # (i.e. the workspace stub is there), treat this as a mislabelled lib.rs.
            if rel_path.endswith('/src/main.rs') or rel_path == 'src/main.rs':
                lib_path = rel_path[:-len('main.rs')] + 'lib.rs'
                if (project_dir / lib_path).exists():
                    _log(f"  PATH NORMALISED: {rel_path} → {lib_path} (lib crate stub present)")
                    rel_path = lib_path
        # Replace `{ ... }` placeholder bodies with `{ todo!() }` so Rust code
        # compiles during the build check phase (model sometimes uses ... as a stub body).
        if Path(rel_path).suffix == '.rs' and '...' in content:
            content = re.sub(r'\{\s*\.\.\.\s*\}', '{ todo!() }', content)
        if not rel_path:
            continue
        if rel_path in existing_test_files:
            skipped.append(rel_path)
            continue
        # Block writing into any test directory — model must never create test files.
        p_parts = Path(rel_path).parts
        if any(part in TEST_DIR_NAMES for part in p_parts):
            skipped.append(rel_path)
            continue
        # Protect top-level project files by name (any depth would be unusual, keep cheap).
        if Path(rel_path).name in PROTECTED and (project_dir / rel_path).exists():
            skipped.append(rel_path)
            continue
        # Protect any existing non-source file (Cargo.toml, *.toml, *.json, *.proto, etc.)
        # The model must only create/overwrite source code files.
        p = Path(rel_path)
        if p.suffix not in SOURCE_EXTENSIONS and (project_dir / p).exists():
            skipped.append(rel_path)
            _log(f"  PROTECTED (non-source file): {rel_path}")
            continue
        full = project_dir / rel_path
        write(full, content)
        written.append(rel_path)
        _log(f"  wrote {rel_path}")
    if skipped:
        _log(f"  SKIPPED (would overwrite test/config files): {', '.join(skipped)}")
    # Merge with any files written in previous inner-loop attempts so the
    # manifest always tracks the full accumulated set on disk.
    existing = load_manifest(manifest_path)
    merged = list(dict.fromkeys(existing + written))  # deduplicate, preserve order
    save_manifest(manifest_path, merged)
    return len(written)


def delete_source_files(project_dir: Path, manifest_path: Path,
                        module_dir: Path | None = None,
                        existing_test_files: set | None = None):
    """Delete model-generated files (tracked in the manifest).

    If module_dir is provided, also sweeps that directory for any untracked
    source files (ghost files written by the fix loop but never added to the
    manifest) — these are deleted as well, provided they are not test files.
    """
    files = load_manifest(manifest_path)
    deleted = 0
    deleted_paths = set()
    for rel_path in files:
        full = project_dir / rel_path
        if full.exists():
            full.unlink()
            deleted += 1
            deleted_paths.add(str(full))
    delete(manifest_path)

    # Sweep module src/ for ghost files (written by fix loop, not in manifest).
    if module_dir is not None:
        src_dir = module_dir / "src"
        if src_dir.exists():
            for candidate in src_dir.rglob("*"):
                if not candidate.is_file():
                    continue
                if candidate.suffix not in SOURCE_EXTENSIONS:
                    continue
                if str(candidate) in deleted_paths:
                    continue
                rel = str(candidate.relative_to(project_dir))
                if existing_test_files and rel in existing_test_files:
                    continue
                # Only delete if the file has no test blocks — it's a ghost implementation file.
                try:
                    content = candidate.read_text(encoding="utf-8")
                except OSError:
                    continue
                if "#[cfg(test)]" not in content and "#[test]" not in content:
                    candidate.unlink()
                    deleted += 1
                    _log(f"  Deleted ghost file (untracked by manifest): {rel}")

    _log(f"  Deleted {deleted} generated source file(s).")


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

RATE_LIMIT_WAIT = 60  # seconds to wait when hitting a rate limit

# slice 5b infra: relocated to pipeline_core.retry (imported at top).


# slice 5b infra: relocated to pipeline_core.retry (imported at top).


# ---------------------------------------------------------------------------
# Build check (compile-only, no tests)
# ---------------------------------------------------------------------------

def _infer_build_check_command(test_command: str, project_dir: Path) -> str | None:
    """
    Infer a compile-only command from the test command and project layout.
    Returns None if no suitable command can be determined.

    The build check command runs BEFORE the full test suite and is used in the
    inner loop: if compilation fails, TASK 2 is retried with the compiler error
    rather than running the slow full test cycle.
    """
    cmd = test_command.strip().lower()

    # Rust / Cargo
    if cmd.startswith("cargo"):
        return "cargo check --workspace"

    # Python / pytest
    if cmd.startswith("pytest") or cmd.startswith("python -m pytest"):
        return None  # Python is interpreted; syntax errors surface at import time

    # Node / npm
    if cmd.startswith("npm ") or cmd.startswith("npx "):
        # Only attempt if a build script is defined (avoid running tests again).
        pkg_json = project_dir / "package.json"
        if pkg_json.exists():
            try:
                scripts = json.loads(pkg_json.read_text()).get("scripts", {})
                if "build" in scripts:
                    return "npm run build"
            except Exception:
                pass
        return None

    # Go
    if cmd.startswith("go test"):
        return "go build ./..."

    return None


def run_build_check(project_dir: Path, build_cmd: str) -> tuple[bool, str]:
    """
    Run the compile-only check command in project_dir.
    Returns (success, output) where output contains stderr + stdout on failure.
    """
    env = _cargo_env()

    try:
        result = subprocess.run(
            _parse_cmd(build_cmd),
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            env=env,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Build check timed out after {BUILD_TIMEOUT}s."
    except FileNotFoundError as e:
        return False, f"Build check command not found: {e}"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

def _profile_inputs_hash(readme_text: str, test_files: list) -> str:
    """SHA-256 of (readme + pure test file paths + first 500 chars of each).

    Only hashes files from tests/ directories — excludes src/*.rs files that
    happen to contain inline #[cfg(test)] blocks. task0c appends gen_tests
    blocks to those source files, which would otherwise change the hash on
    every restart and force unnecessary re-runs of task0a and task0b.
    """
    stable = [(rel, content) for rel, content in test_files
              if "tests/" in rel or rel.startswith("tests")]
    paths_joined = "\n".join(rel for rel, _ in stable)
    excerpts = "\n".join(content[:500] for _, content in stable[:25])
    raw = (readme_text + paths_joined + excerpts).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Compiler cluster system
# ---------------------------------------------------------------------------

# BuildCluster + ModuleAugmentState relocated to pipeline_core.domain.models (slice 6);
# imported at the top of this module and re-exported for unchanged call sites.


# Active cluster during task0c — set by main() before each cluster pass.
# Read by _cargo_env() and _run_tarpaulin() so every subprocess call inherits
# the cluster's env and exclusion list without touching every call site.
_current_cluster: "BuildCluster | None" = None

# Full cluster list from the cfg scan — set by main() alongside _current_cluster.
# Read by _cluster_cfg_attr() so the default cluster's gen_tests can be gated
# with the NEGATION of every other cluster's cfg flags.
_all_clusters: "list[BuildCluster]" = []

# True when the project_dir is a git-managed workspace (--repo or auto-detected).
# Enables git commits at key pipeline transitions.
_git_managed: bool = False


# ---------------------------------------------------------------------------
# Git workspace helpers
# ---------------------------------------------------------------------------

def _is_git_repo(path: Path) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path, check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


# Slice 4b: git workspace I/O relocated to pipeline_core.adapters.workspace. These are
# thin shims delegating to the adapter with injected log/emit and the live `_git_managed`
# flag, until call sites migrate onto a GitWorkspace instance (slice 5).
_WS_DEPS = WorkspaceDeps(log=_log, emit=_emit)


def _git_default_branch(project_dir: Path) -> str:
    return _wsmod.git_default_branch(project_dir)


def git_commit(project_dir: Path, message: str) -> bool:
    return _wsmod.git_commit(_WS_DEPS, project_dir, message, managed=_git_managed)


def git_tag(project_dir: Path, tag: str) -> None:
    _wsmod.git_tag(_WS_DEPS, project_dir, tag, managed=_git_managed)


def git_clean_reset(project_dir: Path) -> None:
    _wsmod.git_clean_reset(_WS_DEPS, project_dir)


def setup_workspace(repo: str, workspace_dir: Path, force_fresh: bool = False) -> Path:
    """
    Prepare a git-managed workspace for the given repo (URL or local path).

    - URL       → git clone into workspace_dir/{name}
    - local git → copy into workspace_dir/{name} (preserves history)
    - local dir → copy into workspace_dir/{name}, git init + initial commit

    In all cases, checks out (or creates) a 'pipeline' branch and returns
    the workspace path.

    If `force_fresh` is True AND the repo source is a URL, an existing
    workspace is deleted and re-cloned. This is what --clean + --repo means:
    nuke target/, stale gen_tests artifacts, half-applied LLM edits — start
    from a true upstream HEAD. For local-path repos we never auto-delete the
    user's source tree.
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Derive workspace name from repo
    # Normalize various GitHub URL forms to HTTPS (SSH requires a key; HTTPS works everywhere).
    # git@github.com:user/repo.git  →  https://github.com/user/repo
    # github.com/user/repo          →  https://github.com/user/repo
    if re.match(r'^git@github\.com:', repo):
        repo = "https://github.com/" + repo.split(":", 1)[1]
    elif re.match(r'^github\.com/', repo):
        repo = "https://" + repo

    name = repo.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    dest = workspace_dir / name

    _is_url = repo.startswith("http://") or repo.startswith("https://") \
              or repo.startswith("git@") or repo.startswith("ssh://")

    if dest.exists() and force_fresh and _is_url:
        _log(f"  [git] --clean + URL repo: removing {dest} for fresh re-clone")
        shutil.rmtree(dest)

    if dest.exists():
        _log(f"  [git] workspace exists at {dest} — resuming")
    elif _is_url:
        _log(f"  [git] cloning {repo} → {dest}")
        subprocess.run(["git", "clone", repo, str(dest)], check=True)
    else:
        src = Path(repo).resolve()
        if not src.exists():
            raise RuntimeError(f"Repo path not found: {src}")
        _log(f"  [git] copying {src} → {dest}")
        shutil.copytree(str(src), str(dest))
        if not (dest / ".git").exists():
            _log("  [git] no .git found — initialising git repo")
            subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "initial: source snapshot", "--no-gpg-sign"],
                cwd=dest, check=True, capture_output=True,
            )
            _log("  [git] initial commit done")

    # Checkout or create pipeline branch
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=dest, capture_output=True, text=True,
    ).stdout.strip()
    if current != "pipeline":
        branches = subprocess.run(
            ["git", "branch", "--list", "pipeline"],
            cwd=dest, capture_output=True, text=True,
        ).stdout.strip()
        if branches:
            subprocess.run(["git", "checkout", "pipeline"],
                           cwd=dest, check=True, capture_output=True)
            _log("  [git] checked out existing pipeline branch")
        else:
            subprocess.run(["git", "checkout", "-b", "pipeline"],
                           cwd=dest, check=True, capture_output=True)
            _log("  [git] created pipeline branch")

    return dest


def _cluster_augmented_path(project_dir: Path, cluster_name: str, module_name: str) -> Path:
    return project_dir / CLUSTER_AUGMENTED_DIR / f"{cluster_name}__{module_name}"


def is_cluster_augmented(project_dir: Path, cluster_name: str, module_name: str) -> bool:
    return _cluster_augmented_path(project_dir, cluster_name, module_name).exists()


def mark_cluster_augmented(project_dir: Path, cluster_name: str, module_name: str, cov: float) -> None:
    p = _cluster_augmented_path(project_dir, cluster_name, module_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{cov:.1f}%\n")


def _nightly_rules_for_cluster() -> str:
    """Return the nightly boundary rules to inject into generation prompts.
    Non-default cluster: cluster-specific override from prompt_nightly_rule.
    Default/stable cluster: derived from the cluster's excluded_cfg list.
    Returns empty string if no exclusions apply.
    """
    if _current_cluster and not _current_cluster.is_default:
        return _current_cluster.prompt_nightly_rule
    if _current_cluster and _current_cluster.excluded_cfg:
        predicates = ", ".join(f"`{p}`" for p in sorted(_current_cluster.excluded_cfg))
        return (
            f"- Do NOT write tests for code gated behind these excluded cfg predicates: {predicates}.\n"
            f"  Skip any source lines inside their `#[cfg(...)]` blocks — they are untestable on this toolchain."
        )
    return ""


def _estimate_cluster_ceiling(project_dir: Path, cluster: "BuildCluster") -> tuple[float, int, int]:
    """Scan source files and estimate the maximum coverage % achievable in this cluster.

    Returns (ceiling_pct, excluded_lines, total_lines). Uses source line count as a proxy
    for grcov region count — the ratio is approximate but directionally correct.
    """
    if not cluster.excluded_cfg:
        return 100.0, 0, 0
    total = 0
    excluded = 0
    src_dir = project_dir / "src"
    if not src_dir.exists():
        return 100.0, 0, 0
    for rs in sorted(src_dir.rglob("*.rs")):
        try:
            text = rs.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        n = len(lines)
        excl = _filter_cfg_excluded_lines(text, set(range(1, n + 1)), cluster.excluded_cfg)
        total += n
        excluded += len(excl)
    if total == 0:
        return 100.0, 0, 0
    ceiling = (1.0 - excluded / total) * 100.0
    return ceiling, excluded, total


# ---------------------------------------------------------------------------
# Skeleton extractor (TASK 2 Phase A) — wraps the Rust binary built by setup.sh.
# ---------------------------------------------------------------------------

SKELETON_EXTRACTOR_BIN = Path(__file__).parent / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"

# Composition root (continued from SETTINGS): build the port adapters once, here where
# the extractor-binary path is in scope. `pipeline.py` is the __main__/composition layer
# (CODE_ARCHITECTURE.md §1), so it owns construction; call sites migrate onto ADAPTERS
# one task at a time (slice 5). Adapters wrap real primitives and never import back here.
ADAPTERS = build_adapters(SETTINGS, extractor_bin=SKELETON_EXTRACTOR_BIN, model=MODEL)
# The application-layer composition context. main()'s task dispatch migrates onto it
# slice by slice (5b); for now it bundles the resolved settings + adapters.
# Wire the cross-cutting infra seams to the pipeline's concrete sinks/config (slice 5b).
_pclog.set_sinks(_log, _emit)
_pcretry.configure(max_retries=MAX_RETRIES, rate_limit_wait=RATE_LIMIT_WAIT, model=MODEL)


def _prune_redundant_clusters(
    clusters: list["BuildCluster"],
    source_dir: Path,
    cargo_toml_path: "Path | None" = None,
    min_gated_items: int = 5,
) -> list["BuildCluster"]:
    """Drop clusters whose work is already covered by another cluster.

    Two pruning strategies:
      (P) Default-redundancy: if a cluster's distinguishing cfg_true is a
          subset of Cargo.toml's `default = [...]` features, drop it —
          the default build (cluster with no cfgs added) already covers
          its activated set.
      (Q) Empty-cluster: count items in `source_dir/src/` whose
          `#[cfg(...)]` references the cluster's distinguishing predicate.
          If fewer than `min_gated_items`, the cluster's contribution is
          too small to justify its own LLM run. Drop it.

    Always keep at least one cluster (the default). Returns the pruned
    list. Logs each prune decision with reason.
    """
    if not clusters:
        return clusters
    # Parse Cargo.toml [features].default to get baseline-enabled features.
    default_features: set[str] = set()
    if cargo_toml_path is None:
        cargo_toml_path = source_dir / "Cargo.toml"
    if cargo_toml_path.exists():
        default_features = set(_cargo_default_features(cargo_toml_path.read_text(errors="replace")))

    # Count per-predicate gated items in source.
    pred_counts: dict[str, int] = {}
    src_dir = source_dir / "src"
    if src_dir.exists():
        pred_re = re.compile(r'#\[cfg(?:_attr)?\(([^)]+(?:\([^)]*\)[^)]*)*)\)')
        bare_re = re.compile(r'\b([a-z_]\w*)(?!\s*=)')
        feat_re = re.compile(r'\bfeature\s*=\s*"([^"]*)"')
        skip = {"any", "all", "not", "cfg", "cfg_attr", "feature"}
        for rs in src_dir.rglob("*.rs"):
            try:
                txt = rs.read_text(errors="replace")
            except Exception:
                continue
            for m in pred_re.finditer(txt):
                inner = m.group(1)
                for fm in feat_re.finditer(inner):
                    name = fm.group(1)
                    if name:
                        pred_counts[name] = pred_counts.get(name, 0) + 1
                for bm in bare_re.finditer(feat_re.sub("", inner)):
                    name = bm.group(1)
                    if name and name not in skip:
                        pred_counts[name] = pred_counts.get(name, 0) + 1

    keep: list[BuildCluster] = []
    dropped: list[tuple[str, str]] = []
    # Sort clusters: stable/default first so we always retain a baseline.
    ordered = sorted(clusters, key=lambda c: (not c.is_default, c.name))
    has_baseline = False
    for c in ordered:
        distinguishing = list(c.active_cfg or [])
        # (P) default-redundancy: drop if all distinguishing cfgs are in defaults.
        if distinguishing and all(p in default_features for p in distinguishing):
            if has_baseline:
                dropped.append((c.name, f"all cfgs {distinguishing} are in default features"))
                continue
        # (Q) empty-cluster: drop if distinguishing predicates gate too little.
        if distinguishing:
            total_gated = sum(pred_counts.get(p, 0) for p in distinguishing)
            if total_gated < min_gated_items and not c.is_default:
                if has_baseline:
                    dropped.append((c.name,
                                    f"distinguishing cfgs gate only {total_gated} item(s) "
                                    f"(< {min_gated_items})"))
                    continue
        keep.append(c)
        if c.is_default or not distinguishing:
            has_baseline = True

    if not keep:
        # Pathological: nothing survived. Keep the lex-first as safety baseline.
        keep = [sorted(clusters, key=lambda c: c.name)[0]]
        _log(f"  [prune] all clusters were redundant; keeping baseline '{keep[0].name}' as safety.")

    if dropped:
        for name, reason in dropped:
            _log(f"  [prune] dropped cluster '{name}': {reason}")
    else:
        _log(f"  [prune] no redundant clusters; keeping all {len(keep)}.")
    return keep


def _filter_tests_for_cluster(test_files: list[tuple[str, str]],
                              cluster: "BuildCluster | None") -> list[tuple[str, str]]:
    """File-level cluster test filter.

    For each test file, evaluate any top-level `#![cfg(...)]` against the
    cluster's cfg-set. If the predicate is decidably false, drop the file.
    Files with no top-level cfg, or with undecidable predicates, are kept
    (safe default — better to include than wrongly exclude).
    """
    if cluster is None:
        return test_files
    cfg_true_set = {_normalize_cfg_pred(p) for p in cluster.cfg_true}
    cfg_false_set = {_normalize_cfg_pred(p) for p in cluster.cfg_false}

    out: list[tuple[str, str]] = []
    for rel, content in test_files:
        if _file_top_cfg_excluded(content, cfg_true_set, cfg_false_set):
            continue
        out.append((rel, content))
    return out


def _normalize_cfg_pred(p: str) -> str:
    """Normalize a cfg predicate for set comparison (strip spaces)."""
    return p.replace(" ", "")


def _file_top_cfg_excluded(content: str, cfg_true: set[str], cfg_false: set[str]) -> bool:
    """Return True if the file has a top-level `#![cfg(...)]` that evaluates
    to false. Conservative: returns False (don't exclude) on any uncertainty.

    We use a narrow regex match for the common case `#![cfg(PRED)]` rather
    than full AST parsing — test files are small and the pattern is common.
    """
    # Match all top-level #![cfg(...)] occurrences (allow nested parens up to 3 deep).
    pat = re.compile(r'^\s*#!\[cfg\((.*?)\)\]\s*$', re.MULTILINE)
    for m in pat.finditer(content):
        pred = m.group(1).strip()
        if _eval_cfg_pred(pred, cfg_true, cfg_false) is False:
            return True
    return False


def _eval_cfg_pred(pred: str, cfg_true: set[str], cfg_false: set[str]) -> "bool | None":
    """Evaluate a cfg predicate string (the part inside `cfg(...)`).

    Supports `not(X)`, `all(X, Y)`, `any(X, Y)`, bare ident, `feature = "X"`.
    Returns True/False if fully decidable, None if any inner key is unknown.
    """
    s = pred.strip()
    norm = _normalize_cfg_pred(s)
    # Normalize the comparison sets too: source predicates spell features as
    # `feature = "std"` (spaced) while the cluster sets may hold either form.
    # Without this, space-sensitive membership made every `feature = "X"`
    # predicate undecidable (the filter silently no-op'd).
    if norm in {_normalize_cfg_pred(x) for x in cfg_true}:
        return True
    if norm in {_normalize_cfg_pred(x) for x in cfg_false}:
        return False
    # Operators — parse the NORMALIZED form: syn renders `all (…)`/`not (…)`
    # with a space (R7.4b), which made every extractor-emitted operator
    # predicate silently undecidable. Inner parts are recursively normalized
    # again, so slicing from `norm` is safe.
    for op in ("not", "all", "any"):
        if norm.startswith(op + "(") and norm.endswith(")"):
            inner = norm[len(op) + 1:-1]
            parts = _split_top_level_commas(inner)
            evaluated = [_eval_cfg_pred(p, cfg_true, cfg_false) for p in parts]
            if op == "not":
                if not evaluated or evaluated[0] is None:
                    return None
                return not evaluated[0]
            if op == "all":
                if any(e is None for e in evaluated):
                    return None
                return all(evaluated)
            if op == "any":
                if any(e is None for e in evaluated):
                    return None
                return any(evaluated)
    return None  # undecidable


def _split_top_level_commas(s: str) -> list[str]:
    """Split `s` on commas not inside parens."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


def _cluster_extractor_args(cluster: "BuildCluster | None") -> list[str]:
    """Build `--cfg-true PRED` / `--cfg-false PRED` CLI args for the extractor
    from a BuildCluster. Returns empty list if cluster is None."""
    if cluster is None:
        return []
    out: list[str] = []
    for pred in cluster.cfg_true:
        out.extend(["--cfg-true", pred])
    for pred in cluster.cfg_false:
        out.extend(["--cfg-false", pred])
    return out


def _extract_skeleton(input_dir: Path, output_dir: Path,
                      cluster: "BuildCluster | None" = None) -> dict:
    """Invoke the Rust skeleton extractor over a live source tree.

    Run only during the bake step (`_bake_skeleton_artifacts`), before
    strip removes the original source (Hard Rule 11).

    When `cluster` is provided, item-level `#[cfg(...)]` filtering runs
    first — items whose predicates evaluate to false under the cluster's
    cfg-set are dropped before body-stubbing.

    Returns {'ok': bool, 'reports': list[dict], 'stderr': str}. On success,
    `output_dir` contains the skeletonised .rs files mirroring input_dir's
    layout.
    """
    # slice 5a: via Extractor adapter. Map the cluster's cfg-sets to the adapter's
    # cfg_true/cfg_false tuples (same `--cfg-true/--cfg-false` args as _cluster_extractor_args).
    ct = tuple(cluster.cfg_true) if cluster is not None else ()
    cf = tuple(cluster.cfg_false) if cluster is not None else ()
    return ADAPTERS.extractor.skeletonise(input_dir, output_dir, cfg_true=ct, cfg_false=cf)


def _compute_fn_name_counts(src_dir: Path) -> dict[str, int]:
    """Count word-boundary fn-name references across every .rs file under
    `src_dir`. Used to build leaf-first fill ordering for Phase C.

    Caller is responsible for passing a directory that contains real source
    (typically the live project_dir/src/ BEFORE strip). Result is a dict
    `{fn_name: ref_count}` that gets serialised to disk so Phase C never
    needs to re-read source after strip.
    """
    text_parts: list[str] = []
    if not src_dir.exists():
        return {}
    for p in src_dir.rglob("*.rs"):
        try:
            text_parts.append(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    text = "\n".join(text_parts)
    if not text:
        return {}
    counts: dict[str, int] = {}
    # Extract candidate fn names from `fn <name>` declarations. Counting
    # those names across all files gives a usage-frequency map. This is
    # an approximation — comments and string literals can inflate counts —
    # but the order it produces (most-referenced first) is robust enough
    # for leaf-first scheduling.
    for m in re.finditer(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)", text):
        name = m.group(1)
        if name in counts:
            continue
        counts[name] = len(re.findall(
            r"\b" + re.escape(name) + r"\b", text))
    return counts


def _bake_skeleton_artifacts(
    project_dir: Path, module_name: str,
) -> dict:
    """Run BEFORE strip while the live source is still on disk.

    Discovers clusters, prunes redundant ones, then for each cluster:
      - extracts the skeleton (signatures + types + macros, bodies →
        `unimplemented!()`) to `.skeleton/{cluster}/`
      - computes fn-name reference counts and writes them to
        `.skeleton/{cluster}/_leaf_order.json`

    Cluster metadata is saved to `.skeleton/_clusters.json` so TASK 2 can
    rehydrate clusters post-strip without re-scanning source.

    Per Hard Rule 11: this is the ONLY place pipeline code is allowed to
    read the original source. After strip, every downstream phase reads
    `.skeleton/` artefacts only.

    Returns {'ok': bool, 'clusters': list[str], 'reason': str}.
    """
    skel_root = project_dir / ".skeleton"
    skel_root.mkdir(parents=True, exist_ok=True)

    src_dir = project_dir / "src"
    if not src_dir.exists() or not any(src_dir.rglob("*.rs")):
        return {"ok": False, "clusters": [],
                "reason": f"no source at {src_dir} — strip already happened?"}

    # Cluster discovery on the live source (Principle 1: deterministic).
    try:
        clusters = task0_cfg_scan(project_dir)
    except Exception as e:
        return {"ok": False, "clusters": [], "reason": f"task0_cfg_scan: {e}"}
    if not clusters:
        clusters = [BuildCluster(
            name="default", env_extra={}, tarpaulin_exclude_files=[],
            excluded_cfg=[], active_cfg=[], prompt_nightly_rule="",
            is_default=True,
        )]
    clusters = sorted(clusters, key=lambda c: c.name)
    _log(f"  [bake-skeleton] discovered {len(clusters)} cluster(s): "
         f"{', '.join(c.name for c in clusters)}")

    cargo_toml = project_dir / "Cargo.toml"
    clusters = _prune_redundant_clusters(
        clusters, project_dir,
        cargo_toml_path=cargo_toml if cargo_toml.exists() else None,
    )
    _log(f"  [bake-skeleton] processing {len(clusters)} cluster(s) after pruning: "
         f"{', '.join(c.name for c in clusters)}")

    name_counts = _compute_fn_name_counts(src_dir)
    _log(f"  [bake-skeleton] computed reference counts for {len(name_counts)} fn name(s).")

    baked: list[str] = []
    for cluster in clusters:
        skeleton_dir = skel_root / cluster.name
        if skeleton_dir.exists():
            shutil.rmtree(skeleton_dir)
        skeleton_dir.mkdir(parents=True, exist_ok=True)
        # Pass `src_dir` (NOT project_dir) so the extractor can never walk
        # into prior cluster outputs under `.skeleton/`. Output mirrors
        # under `skeleton_dir/src/`, matching the runnable-crate layout
        # Phase A expects.
        res = _extract_skeleton(src_dir, skeleton_dir / "src", cluster=None)
        if not res["ok"]:
            _log(f"  [bake-skeleton] cluster {cluster.name}: extractor FAILED: "
                 f"{res['stderr']}")
            return {"ok": False, "clusters": baked,
                    "reason": f"extract {cluster.name}: {res['stderr']}"}
        order_path = skeleton_dir / "_leaf_order.json"
        try:
            order_path.write_text(json.dumps(name_counts, sort_keys=True),
                                  encoding="utf-8")
        except Exception as e:
            _log(f"  [bake-skeleton] cluster {cluster.name}: leaf-order write failed: {e}")
        _log(f"  [bake-skeleton] cluster {cluster.name}: {len(res['reports'])} "
             f"file(s) skeletonised → {skeleton_dir}")
        baked.append(cluster.name)

    # Save cluster metadata for TASK 2 to rehydrate.
    try:
        manifest = {
            "module": module_name,
            "clusters": [
                {
                    "name": c.name,
                    "env_extra": dict(c.env_extra),
                    "tarpaulin_exclude_files": list(c.tarpaulin_exclude_files),
                    "excluded_cfg": list(c.excluded_cfg),
                    "active_cfg": list(c.active_cfg),
                    "prompt_nightly_rule": c.prompt_nightly_rule,
                    "is_default": c.is_default,
                    "cfg_true": list(getattr(c, "cfg_true", []) or []),
                    "cfg_false": list(getattr(c, "cfg_false", []) or []),
                    "toolchain": getattr(c, "toolchain", None),
                    "requires_nightly": bool(getattr(c, "requires_nightly", False)),
                }
                for c in clusters
            ],
        }
        (skel_root / "_clusters.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )
    except Exception as e:
        _log(f"  [bake-skeleton] cluster manifest write failed: {e}")

    # Capture each gen_tests sibling's ORIGIN cluster cfg from the live (pre-strip)
    # decls. task0c stamps the generating cluster's cfg on the `mod gen_tests_N;`
    # decl; strip then removes the decl and `saved_gen_blocks` keeps only the body,
    # losing provenance. Without this manifest, downstream propagation re-derives
    # the gate from the CURRENT cluster (always-active → no protection), so a
    # nightly-only test (e.g. `request_ref`) leaks into stable/no_core → E0658.
    # Keyed by sibling stem "{parent_stem}.gen_tests_{n}" → origin cfg ('' = default).
    try:
        origin: dict[str, str] = {}
        for rs in sorted(src_dir.rglob("*.rs")):
            if _is_gen_tests_file(rs.name):
                continue
            try:
                text = rs.read_text(encoding="utf-8")
            except OSError:
                continue
            for n, cfg in _gen_tests_origin_cfgs(text).items():
                origin[f"{rs.stem}.gen_tests_{n}"] = cfg
        (skel_root / "_gen_tests_origin.json").write_text(
            json.dumps(origin, indent=2, sort_keys=True), encoding="utf-8",
        )
        if origin:
            _log(f"  [bake-skeleton] captured origin cfg for {len(origin)} gen_tests sibling(s).")
    except Exception as e:
        _log(f"  [bake-skeleton] gen_tests origin manifest write failed: {e}")

    return {"ok": True, "clusters": baked, "reason": ""}


def _load_baked_clusters(project_dir: Path) -> list["BuildCluster"]:
    """Rehydrate clusters from `.skeleton/_clusters.json` (written by
    `_bake_skeleton_artifacts` before strip). Returns [] if absent.

    Replaces every post-strip call to `task0_cfg_scan` — those would need
    the source tree, which is gone.
    """
    path = project_dir / ".skeleton" / "_clusters.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[BuildCluster] = []
    for c in data.get("clusters", []):
        bc = BuildCluster(
            name=c.get("name", ""),
            env_extra=dict(c.get("env_extra", {}) or {}),
            tarpaulin_exclude_files=list(c.get("tarpaulin_exclude_files", []) or []),
            excluded_cfg=list(c.get("excluded_cfg", []) or []),
            active_cfg=list(c.get("active_cfg", []) or []),
            prompt_nightly_rule=c.get("prompt_nightly_rule", "") or "",
            is_default=bool(c.get("is_default", False)),
        )
        # cfg_true / cfg_false / toolchain are read-only @properties derived
        # from active_cfg, excluded_cfg, and requires_nightly. They're written
        # into the manifest for human readability but must not be assigned
        # back onto the cluster.
        if c.get("requires_nightly"):
            bc.requires_nightly = True
        out.append(bc)
    return out


def _load_baked_leaf_order(project_dir: Path, cluster_name: str) -> dict[str, int]:
    """Load the `{fn_name: ref_count}` map saved during the bake step.
    Returns {} if absent — caller falls back to alphabetical order."""
    path = project_dir / ".skeleton" / cluster_name / "_leaf_order.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))}


def _load_skeleton_files(skeleton_dir: Path) -> list[tuple[str, str]]:
    """Return [(relative_path, content), ...] for every .rs file under skeleton_dir."""
    out: list[tuple[str, str]] = []
    for p in sorted(skeleton_dir.rglob("*.rs")):
        try:
            out.append((str(p.relative_to(skeleton_dir)), p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _format_skeleton_for_prompt(skeleton_files: list[tuple[str, str]]) -> str:
    """Render the skeleton as a sequence of <FILE> blocks for prompt embedding."""
    parts: list[str] = []
    for rel, content in skeleton_files:
        parts.append(f'<FILE path="{rel}">\n{content}\n</FILE>')
    return "\n\n".join(parts)


def _format_skeleton_index_for_cluster(
    skeleton_files: list[tuple[str, str]],
    cluster: "BuildCluster | None",
) -> str:
    """Same as _format_skeleton_index but additionally elides items whose
    `#[cfg(...)]` evaluates to false under the cluster's cfg-set.

    Used in Phase C prompts so the model sees only items that compile in
    THIS cluster — reduces cognitive load without affecting the on-disk
    unfiltered skeleton (which Rust's own cfg eval handles correctly).

    Items whose cfg is undecidable (unknown keys) are kept (safe default).
    """
    if cluster is None:
        return _format_skeleton_index(skeleton_files)
    cfg_true = {_normalize_cfg_pred(p) for p in cluster.cfg_true}
    cfg_false = {_normalize_cfg_pred(p) for p in cluster.cfg_false}

    # Per-item cfg attr matcher: a leading `#[cfg(...)]` (or several) above
    # the item declaration.
    cfg_attr_re = re.compile(r'^[ \t]*#\[cfg\(([^\]]+(?:\([^\]]*\)[^\]]*)*)\)\]\s*$', re.MULTILINE)

    filtered_files: list[tuple[str, str]] = []
    for rel, content in skeleton_files:
        # Walk the content line by line collecting leading #[cfg(...)] groups
        # before each top-level item; drop the item block if any cfg evaluates
        # to false.
        lines = content.split("\n")
        out_lines: list[str] = []
        i = 0
        while i < len(lines):
            # Collect attributes that precede the current item (any line
            # starting with `#[...]` or `#![...]` or doc comment).
            attr_start = i
            collected_cfgs: list[str] = []
            while i < len(lines):
                line = lines[i]
                stripped = line.strip()
                m = cfg_attr_re.match(line)
                if m:
                    collected_cfgs.append(m.group(1).strip())
                    i += 1
                    continue
                # Any other attribute / doc / blank line: keep walking.
                if stripped.startswith("#[") or stripped.startswith("#![") or stripped.startswith("///") or stripped.startswith("//!") or stripped == "":
                    i += 1
                    continue
                break
            # i now points to (probably) an item declaration. Evaluate cfgs.
            keep = True
            for pred in collected_cfgs:
                if _eval_cfg_pred(pred, cfg_true, cfg_false) is False:
                    keep = False
                    break
            if keep:
                # Emit attr block + the item.
                out_lines.extend(lines[attr_start:i + 1])
                i += 1
            else:
                # Skip this item entirely — including its body if it has one.
                # Simple heuristic: skip until brace depth returns to zero or
                # we hit a `;` at depth zero (for body-less items like
                # `pub use X;`).
                if i >= len(lines):
                    break
                line = lines[i]
                if "{" in line:
                    depth = line.count("{") - line.count("}")
                    i += 1
                    while i < len(lines) and depth > 0:
                        depth += lines[i].count("{") - lines[i].count("}")
                        i += 1
                else:
                    # body-less item terminated by ';' on the same line
                    i += 1
        filtered_files.append((rel, "\n".join(out_lines)))
    return _format_skeleton_index(filtered_files)


def _format_skeleton_index(skeleton_files: list[tuple[str, str]]) -> str:
    """Render skeleton as signatures-only index. Drops bodies (which are
    `unimplemented!()` anyway), keeps type declarations + function signatures
    + macro definitions for cross-reference. Used in Phase C prompts so we
    don't waste input tokens on body stubs the model already knows about.

    P3.11: macro_rules! bodies are also elided. Phase A preserves macros
    verbatim on disk; the prompt's index just needs the macro NAMES so the
    model knows what's exported, not the multi-KB rule definitions.
    """
    out_parts: list[str] = []
    fn_re = re.compile(
        r'((?:^|\n)[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
        r'(?:const\s+)?fn\s+[A-Za-z_]\w*[^{]*)\{',
        re.MULTILINE,
    )
    # `macro_rules!` (with optional `#[macro_export]` etc. attributes lurking
    # above on a separate line — we don't try to capture those, just the macro).
    macro_re = re.compile(
        r'((?:^|\n)[ \t]*(?:pub(?:\([^)]*\))?\s+)?macro_rules!\s+[A-Za-z_]\w*\s*)\{',
        re.MULTILINE,
    )

    def _elide_bodies(text: str, regex: re.Pattern, terminator: str) -> str:
        """Walk `regex` matches; for each, brace-match to find the body and
        replace it with `terminator`. Returns the rewritten text."""
        result_parts: list[str] = []
        last = 0
        for m in regex.finditer(text):
            body_start = m.end()
            depth = 1
            i = body_start
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue
            body_end = i
            result_parts.append(text[last:m.start(1)])
            result_parts.append(m.group(1).rstrip())
            result_parts.append(terminator)
            last = body_end
        result_parts.append(text[last:])
        return "".join(result_parts)

    for rel, content in skeleton_files:
        # Order matters: elide macros first (their bodies contain `fn` patterns
        # in rule expansions that would otherwise confuse the fn regex).
        condensed = _elide_bodies(content, macro_re, " { /* macro body elided — preserved verbatim on disk */ }")
        condensed = _elide_bodies(condensed, fn_re, ";")
        out_parts.append(f'<FILE path="{rel}" mode="signatures-only">\n{condensed}\n</FILE>')
    return "\n\n".join(out_parts)


def _collect_skeleton_symbols(skeleton_files: list[tuple[str, str]]) -> set[str]:
    """Return the set of fn/method names declared in the skeleton.

    Used by Phase B validation: every target_symbol the LLM emits should exist
    in this set. We use a coarse regex — sufficient for validation, not
    intended to be precise.
    """
    syms: set[str] = set()
    fn_re = re.compile(r'\bfn\s+([A-Za-z_]\w*)\s*[<(]')
    for _, content in skeleton_files:
        for m in fn_re.finditer(content):
            syms.add(m.group(1))
    return syms


def _task2_group_tests(
    skeleton_dir: Path,
    test_files: list[tuple[str, str]],
    cluster_name: str,
    module_name: str,
) -> list[dict]:
    """Phase B: ask the LLM to group test files into cohesive units.

    Returns a list of group dicts: {name, rationale, test_files, target_symbols}.
    Falls back to one-group-per-test-file on LLM failure or validation failure.
    Result is also written to skeleton_dir/groups.json.
    """
    out_path = skeleton_dir / "groups.json"
    skeleton_files = _load_skeleton_files(skeleton_dir)
    if not skeleton_files or not test_files:
        groups = _fallback_groups_per_file(test_files)
        out_path.write_text(json.dumps(groups, indent=2), encoding="utf-8")
        return groups

    skeleton_block = _format_skeleton_for_prompt(skeleton_files)
    tests_block = "\n\n".join(
        f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>'
        for rel, content in test_files
    )
    sym_set = _collect_skeleton_symbols(skeleton_files)
    test_paths = sorted({rel for rel, _ in test_files})

    prompt = f"""You are planning a test-driven reconstruction of the `{module_name}` crate (cluster: {cluster_name}).

The implementation skeleton below has all type definitions, signatures, and macros, but
every function body is `unimplemented!()`. Your job is to group the test files into
cohesive units. Each group should exercise a coherent piece of functionality so that one
LLM call per group can fill the necessary `unimplemented!()` bodies and make that group's
tests pass.

Rules:
  - Aim for 6-15 groups. Each group should contain 1-3 test files (smaller
    groups → smaller per-call output → fewer empty responses from the model).
  - Every test file appears in EXACTLY one group. Include compile-fail tests
    (e.g. tests/compiletest.rs, trybuild harness files) — those are real
    tests and must be assigned to a group too.
  - target_symbols must be function/method names that exist in the skeleton.
  - rationale: one sentence describing what the group tests.

Test files you MUST cover (every entry here appears in exactly one group):
{chr(10).join(f"  - {p}" for p in test_paths)}

Implementation skeleton:
{skeleton_block}

Test files:
{tests_block}

Output a JSON object with this exact shape (no prose, no markdown fences):

{{
  "groups": [
    {{
      "name": "short_kebab_case_name",
      "rationale": "one sentence",
      "test_files": ["tests/foo.rs", "tests/bar.rs"],
      "target_symbols": ["func_a", "Method::name", "Type::method"]
    }}
  ]
}}
"""

    for attempt in range(2):
        response = ask(prompt, model=_TASK1_MODEL)
        groups, err, recoverable_missing = _parse_and_validate_groups(
            response, test_paths, sym_set
        )
        if groups is not None:
            # P3.6: post-process to enforce max group size = 3.
            groups = _split_oversized_groups(groups, max_size=3)
            _log(f"  [phase-b] LLM produced {len(groups)} group(s) (post-split).")
            out_path.write_text(json.dumps(groups, indent=2), encoding="utf-8")
            return groups
        # Auto-recover small omissions (≤2 missing test files) by appending
        # them to a "miscellaneous" group rather than burning another full LLM
        # round-trip. Compile-fail tests (trybuild) tend to get silently
        # dropped by the model.
        if recoverable_missing and len(recoverable_missing) <= 2 and attempt == 0:
            # Re-parse to get the partial groups (validator returned None when
            # it rejected, but the parsed structure is still useful).
            partial = _extract_groups_or_none(response)
            if partial is not None:
                partial.append({
                    "name": "miscellaneous_auto_added",
                    "rationale": "auto-appended test files omitted by LLM grouping",
                    "test_files": sorted(recoverable_missing),
                    "target_symbols": [],
                })
                # Apply P3.6 split even on the auto-recovered path so big
                # groups still get chunked.
                partial = _split_oversized_groups(partial, max_size=3)
                _log(f"  [phase-b] auto-recovered {len(recoverable_missing)} "
                     f"omitted test file(s) into a 'miscellaneous' group "
                     f"({len(partial)} groups post-split).")
                out_path.write_text(json.dumps(partial, indent=2), encoding="utf-8")
                return partial
        _log(f"  [phase-b] attempt {attempt + 1} rejected: {err}")

    _log("  [phase-b] LLM grouping failed validation twice — falling back to per-file groups.")
    groups = _fallback_groups_per_file(test_files)
    out_path.write_text(json.dumps(groups, indent=2), encoding="utf-8")
    return groups


def _split_oversized_groups(groups: list[dict], max_size: int = 3) -> list[dict]:
    """Split any group with >max_size test files into smaller chunks.

    Each chunk inherits the parent's rationale + target_symbols, with the
    name suffixed by a 1-based index. Keeps target_symbols on every chunk
    so leaf-first ordering still works.
    """
    out: list[dict] = []
    for g in groups:
        tests = list(g.get("test_files", []))
        if len(tests) <= max_size:
            out.append(g)
            continue
        base_name = g.get("name", "group")
        chunks: list[list[str]] = [tests[i:i + max_size] for i in range(0, len(tests), max_size)]
        for idx, chunk in enumerate(chunks, 1):
            out.append({
                "name": f"{base_name}_{idx}of{len(chunks)}",
                "rationale": f"{g.get('rationale', '')} (auto-split {idx}/{len(chunks)})",
                "test_files": chunk,
                "target_symbols": list(g.get("target_symbols", [])),
            })
    return out


def _extract_groups_or_none(response: str) -> list[dict] | None:
    """Parse the LLM's JSON manifest into a groups list without validation."""
    txt = response.strip()
    if txt.startswith("```"):
        txt = re.sub(r'^```[a-zA-Z]*\n?', '', txt)
        txt = re.sub(r'\n?```\s*$', '', txt)
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "groups" not in data:
        return None
    g = data["groups"]
    return g if isinstance(g, list) else None


def _parse_and_validate_groups(
    response: str, test_paths: list[str], known_symbols: set[str]
) -> tuple[list[dict] | None, str, set[str]]:
    """Parse the LLM's JSON manifest, validate coverage and symbol existence.

    Returns (groups, error_message, recoverable_missing).
      - groups is None on validation failure.
      - recoverable_missing is the set of test files the LLM omitted; populated
        only when the failure was a coverage gap (not a structural error).
    """
    # Tolerate fenced JSON.
    txt = response.strip()
    if txt.startswith("```"):
        txt = re.sub(r'^```[a-zA-Z]*\n?', '', txt)
        txt = re.sub(r'\n?```\s*$', '', txt)
    try:
        data = json.loads(txt)
    except json.JSONDecodeError as e:
        return None, f"not valid JSON: {e}", set()
    if not isinstance(data, dict) or "groups" not in data:
        return None, "missing 'groups' key", set()
    groups = data["groups"]
    if not isinstance(groups, list) or not groups:
        return None, "'groups' must be a non-empty list", set()

    seen_tests: set[str] = set()
    expected_tests = set(test_paths)
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            return None, f"group #{i} is not an object", set()
        for key in ("name", "rationale", "test_files", "target_symbols"):
            if key not in g:
                return None, f"group #{i} missing key '{key}'", set()
        if not isinstance(g["test_files"], list) or not g["test_files"]:
            return None, f"group '{g.get('name','?')}' has empty test_files", set()
        for tf in g["test_files"]:
            if tf in seen_tests:
                return None, f"test file '{tf}' appears in multiple groups", set()
            seen_tests.add(tf)
        # Symbol existence is a soft check — warn but don't reject.
        # (LLM may use slight name variations like `Self::method`.)

    missing = expected_tests - seen_tests
    extra = seen_tests - expected_tests
    if missing:
        return None, f"groups omit {len(missing)} test file(s); first: {sorted(missing)[:3]}", missing
    if extra:
        return None, f"groups include unknown test file(s): {sorted(extra)[:3]}", set()
    return groups, "ok", set()


def _fallback_groups_per_file(test_files: list[tuple[str, str]]) -> list[dict]:
    """One group per test file — deterministic safe default."""
    groups: list[dict] = []
    for rel, _ in test_files:
        stem = Path(rel).stem
        groups.append({
            "name": f"file_{stem}",
            "rationale": f"single-file fallback grouping for {rel}",
            "test_files": [rel],
            "target_symbols": [],
        })
    return groups


_FAILED_ATTEMPT_SENTINEL = "// SKELETON_STUB_FAILED_ATTEMPT"


def _is_unimplemented_body(body_text: str) -> bool:
    """True if a function body looks like our `unimplemented!()` stub.

    Recognises two forms:
      1. Plain stub: contains `unimplemented` and body < 60 chars
      2. Annotated stub: contains the SKELETON_STUB_FAILED_ATTEMPT sentinel
         (used by the fix-loop exhaustion path to preserve the failed attempt
         and cargo error as comments while keeping the fn callable)
    """
    stripped = body_text.strip().strip("{").strip("}").strip()
    if _FAILED_ATTEMPT_SENTINEL in stripped:
        return True
    return "unimplemented" in stripped and len(stripped) < 60


def _make_failed_attempt_body(attempted_body: str, error_excerpt: str,
                               fix_attempts: int) -> str:
    """Build a fn body that documents a failed fill attempt as comments and
    ends in `unimplemented!()` so the crate still compiles.

    The body carries the SKELETON_STUB_FAILED_ATTEMPT sentinel so a later
    pass's `_scan_unimplemented_functions` re-discovers this fn as still
    needing a fill — but the model sees the failure history in context.
    """
    ts = int(time.time())
    lines = [
        _FAILED_ATTEMPT_SENTINEL,
        f"// Previous attempt failed compilation after {fix_attempts} fix-loop "
        "attempts.",
        "// Body reverted to `unimplemented!()` so the crate compiles. A later",
        "// pass will retry — by then more sibling functions in this file will",
        "// be implemented, giving the model richer context.",
        "//",
        "// ATTEMPTED BODY:",
    ]
    if attempted_body:
        for ln in attempted_body.splitlines()[:60]:
            lines.append(f"//   {ln}")
    else:
        lines.append("//   (no body captured)")
    lines.append("//")
    lines.append("// CARGO ERROR (focused excerpt):")
    if error_excerpt:
        for ln in error_excerpt.splitlines()[:30]:
            lines.append(f"//   {ln}")
    else:
        lines.append("//   (no error captured)")
    lines.append(f"// Timestamp: {ts}")
    lines.append("unimplemented!()")
    return "\n".join(lines)


def _extract_fn_blocks(response: str) -> dict[str, str]:
    """Parse `<FN sig="...">body</FN>` blocks from the LLM response.

    Returns {signature → body_text}. The body is the raw content between
    the opening `<FN ...>` and closing `</FN>` markers, trimmed of leading/
    trailing whitespace. The signature must include everything from "fn name"
    to the opening `{` exclusive — matching how `_merge_fills_preserving_existing`
    keys its disk-function map.

    Tolerates loosely-formatted responses (extra whitespace, mixed quoting on
    the sig attribute).
    """
    # `(?s)` for DOTALL so body can span lines. Match sig attribute value
    # in double quotes (most common) or single quotes (sometimes used).
    pat = re.compile(
        r'<FN\s+sig\s*=\s*(?P<q>["\'])(?P<sig>.+?)(?P=q)\s*>(?P<body>.*?)</FN>',
        re.DOTALL,
    )
    out: dict[str, str] = {}
    stubs_dropped = 0
    for m in pat.finditer(response):
        sig = m.group("sig").strip()
        body = m.group("body").strip()
        if not sig or not body:
            continue
        # P3.10 enforcement: drop stub bodies so they don't appear successful.
        if _is_unimplemented_body(body) or body.strip() in ("todo!()", "todo!();"):
            stubs_dropped += 1
            continue
        out[sig] = body
    if stubs_dropped:
        _log(f"  [phase-c] dropped {stubs_dropped} stub <FN> block(s) "
             f"from model output (model re-emitted unimplemented!).")
    return out


def _merge_fn_blocks_into_disk(disk_text: str, fn_blocks: dict[str, str]) -> str:
    """Merge dict of {signature → body} into disk_text by replacing the body
    of each disk function whose signature key matches and whose body is
    currently `unimplemented!()`.

    All non-function content on disk is preserved verbatim. This is the
    P3.2 partner of `_merge_fills_preserving_existing` for the per-function
    output format — same safety guarantees, simpler input.
    """
    if not fn_blocks:
        return disk_text
    fn_re = re.compile(
        r'((?<![A-Za-z_0-9])(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
        r'(?:const\s+)?fn\s+[A-Za-z_]\w*[^{]*\{)',
    )
    # Re-extract disk functions to find their bodies, then splice in.
    matches: list[tuple[str, int, int]] = []  # (sig, body_start, body_end)
    for m in fn_re.finditer(disk_text):
        body_start = m.end()
        depth = 1
        i = body_start
        while i < len(disk_text) and depth > 0:
            ch = disk_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        body_end = i - 1
        # Strip the trailing '{' so the sig matches the LLM's <FN sig="..."> key
        # (which is the signature without the opening brace).
        sig_line = disk_text[m.start():body_start].rstrip("{ \t\n").strip()
        matches.append((sig_line, body_start, body_end))

    out = disk_text
    for sig, body_start, body_end in reversed(matches):
        existing_body = out[body_start:body_end]
        if not _is_unimplemented_body(existing_body):
            continue  # already filled — never overwrite
        new_body = fn_blocks.get(sig)
        if new_body is None:
            # Try a relaxed match: normalize whitespace and compare.
            normalized_sig = " ".join(sig.split())
            for k, v in fn_blocks.items():
                if " ".join(k.split()) == normalized_sig:
                    new_body = v
                    break
        if new_body is None:
            continue
        out = out[:body_start] + new_body + out[body_end:]
    return out


# ---------------------------------------------------------------------------
# Per-fn TDD fill (Flavour B): one LLM call per unimplemented function.
# We own the splice site; the model returns only the body text.
# ---------------------------------------------------------------------------

_FN_RE_GLOBAL = re.compile(
    r'((?<![A-Za-z_0-9])(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
    r'(?:const\s+)?fn\s+([A-Za-z_]\w*)[^{]*\{)',
)


def _locate_fn(file_text: str, fn_name: str) -> tuple[str, int, int, int] | None:
    """Return (sig_text, sig_start, body_start, body_end) for the first fn whose
    name matches `fn_name`. body_start is just after the opening `{`; body_end
    is the position of the matching `}` (exclusive end of body).

    Returns None if not found or braces are malformed.
    """
    for m in _FN_RE_GLOBAL.finditer(file_text):
        if m.group(2) != fn_name:
            continue
        sig_start = m.start()
        body_start = m.end()
        depth = 1
        i = body_start
        while i < len(file_text) and depth > 0:
            ch = file_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth != 0:
            return None
        body_end = i - 1
        sig_text = file_text[sig_start:body_start].rstrip("{ \t\n").strip()
        return sig_text, sig_start, body_start, body_end
    return None


def _is_pub_fn(file_text: str, fn_name: str) -> bool:
    """True if the fn declaration includes `pub` (any visibility variant)."""
    loc = _locate_fn(file_text, fn_name)
    if loc is None:
        return False
    sig = loc[0]
    return bool(re.match(r"\s*pub\b", sig))


def _replace_fn_signature(file_path: Path, fn_name: str, new_sig: str) -> bool:
    """P3.1: replace the verbatim signature line(s) of `fn_name` with
    `new_sig`. The body is left intact. Used only when the per-fn fix loop
    has tried >= 5 attempts and the model proposes a signature change in
    addition to a new body. Returns True if the splice was applied.

    Constraints:
      - target fn must exist in file
      - target fn must NOT be `pub` (changing public sigs would break tests)
      - new_sig must contain `fn {fn_name}` (sanity check; model can't rename)
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False
    if _is_pub_fn(text, fn_name) is not False:
        # Only relax for private fns; pub fns are part of the test surface.
        pass
    if _is_pub_fn(text, fn_name):
        return False
    if not re.search(r"\bfn\s+" + re.escape(fn_name) + r"\b", new_sig):
        return False
    loc = _locate_fn(text, fn_name)
    if loc is None:
        return False
    _old_sig, sig_start, body_start, _body_end = loc
    # Find the start of the line that contains the sig.
    line_start = text.rfind("\n", 0, sig_start) + 1
    # New sig must end immediately before the opening brace (which body_start
    # points just past). Compose: text[:line_start] + new_sig.rstrip() + " {" + text[body_start:]
    cleaned = new_sig.strip().rstrip("{").rstrip()
    new_text = text[:line_start] + cleaned + " {" + text[body_start:]
    file_path.write_text(new_text, encoding="utf-8")
    return True


def _extract_signature_block(response: str) -> str | None:
    """Pull `<SIGNATURE>...</SIGNATURE>` from an LLM response. Returns the
    trimmed body or None if absent."""
    m = re.search(r"<SIGNATURE>(.*?)</SIGNATURE>", response, re.DOTALL)
    if not m:
        return None
    txt = m.group(1).strip()
    return txt or None


def _splice_fn_body_at(file_path: Path, body_byte_start: int, body_byte_end: int,
                        new_body: str, allow_nonstub: bool = False) -> tuple[bool, int]:
    """Splice `new_body` between bytes [body_byte_start, body_byte_end) in
    `file_path`. Replaces an explicit byte range — used when the caller has
    canonical offsets from the syn enumerator. By default refuses to
    overwrite a body that isn't currently a stub.

    The body content between [start, end) is whatever currently sits between
    the fn's `{` and `}`. If that text doesn't look like an unimplemented
    stub, the splice is rejected so we never clobber a real body — UNLESS
    `allow_nonstub=True` (the fix-loop per_fn strategy, which re-fills a fn
    whose previous body was filled but wrong). When re-filling a non-stub,
    the caller MUST re-enumerate offsets before each splice, since every
    write shifts the byte ranges of later fns in the same file.

    Returns (success, bytes_delta).
    """
    # The offsets are BYTE offsets from the syn enumerator. They MUST be applied
    # in byte space: a `str` from read_text() is indexed by CHARACTERS, so any
    # multibyte UTF-8 char before the span (e.g. an em-dash `—` = 3 bytes/1 char
    # in a doc comment) shifts char-index past byte-offset and the slice eats the
    # closing `}` / next item. Work on raw bytes; decode only the small spans.
    try:
        raw = file_path.read_bytes()
    except Exception:
        return False, 0
    if body_byte_end > len(raw) or body_byte_start > body_byte_end:
        return False, 0
    cur_body = raw[body_byte_start:body_byte_end].decode("utf-8", errors="replace")
    if not allow_nonstub and not _is_unimplemented_body(cur_body):
        return False, 0
    nb = new_body.strip()
    if nb.startswith("{") and nb.endswith("}"):
        nb = nb[1:-1].strip()
    if not nb or _is_unimplemented_body(nb):
        return False, 0
    # Re-indent the body to match the fn's existing indentation. Find the
    # column of the fn declaration by walking back to the previous newline.
    line_start = raw.rfind(b"\n", 0, body_byte_start) + 1
    base_indent = ""
    for ch in raw[line_start:body_byte_start].decode("utf-8", errors="replace"):
        if ch in (" ", "\t"):
            base_indent += ch
        else:
            break
    inner_indent = base_indent + "    "
    nb_indented = "\n" + "\n".join(
        (inner_indent + ln) if ln.strip() else "" for ln in nb.splitlines()
    ) + "\n" + base_indent
    new_raw = raw[:body_byte_start] + nb_indented.encode("utf-8") + raw[body_byte_end:]
    file_path.write_bytes(new_raw)
    return True, len(new_raw) - len(raw)


def _splice_fn_body(file_path: Path, fn_name: str, new_body: str,
                    allow_nonstub: bool = False) -> tuple[bool, int]:
    """Replace the body of `fn_name` in `file_path` with `new_body`.

    By default only splices when the current body is an `unimplemented!()`
    stub — never overwrites an already-filled function. Pass
    `allow_nonstub=True` (fix-loop per_fn strategy) to re-fill a fn whose
    previous body compiled wrong. The file is re-read here on every call, so
    byte offsets are always fresh — safe to call repeatedly on the same file.
    Returns (success, bytes_delta).
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False, 0
    loc = _locate_fn(text, fn_name)
    if loc is None:
        return False, 0
    _sig, _sig_start, body_start, body_end = loc
    cur_body = text[body_start:body_end]
    if not allow_nonstub and not _is_unimplemented_body(cur_body):
        return False, 0
    # Normalise the body: model may have included extraneous braces or
    # leading/trailing whitespace. Strip outer braces if present.
    nb = new_body.strip()
    if nb.startswith("{") and nb.endswith("}"):
        nb = nb[1:-1].strip()
    if not nb or _is_unimplemented_body(nb):
        return False, 0
    # Re-indent so the body sits cleanly inside the existing braces. Detect
    # the indentation of the opening brace's line and use it +4 for body.
    line_start = text.rfind("\n", 0, _sig_start) + 1
    sig_line = text[line_start:_sig_start]
    base_indent = ""
    for ch in sig_line:
        if ch in (" ", "\t"):
            base_indent += ch
        else:
            break
    inner_indent = base_indent + "    "
    nb_indented = "\n" + "\n".join(
        (inner_indent + ln) if ln.strip() else "" for ln in nb.splitlines()
    ) + "\n" + base_indent
    new_text = text[:body_start] + nb_indented + text[body_end:]
    file_path.write_text(new_text, encoding="utf-8")
    return True, len(new_text) - len(text)


# --- Per-impl response parser + splice helpers --------------------------
#
# The per-impl prompt asks the model to emit one `<FN name="X" block="...">`
# block per stub. The parser collects them keyed by (block_container,
# fn_name) so the splicer can route each body to the correct impl.

_FN_BLOCK_RE = re.compile(
    r'<FN\s+name="([^"]+)"\s+block="([^"]+)"\s*>(.*?)</FN\s*>',
    re.DOTALL,
)

# A body may start with an optional `where ...` clause (on its own line) used
# to tighten the impl-level bounds for this fn alone. Match it greedily to
# the end of the line; the splicer rewrites the fn signature when present.
_WHERE_CLAUSE_RE = re.compile(r'^\s*(where\s[^\n]+)\n', re.MULTILINE)


def extract_fn_bodies(text: str) -> list[dict]:
    """Parse `<FN name="X" block="...">body</FN>` blocks from a model
    response. Returns a list of records:
        {"fn_name": str, "block": str, "where_clause": str | None,
         "body": str}
    The order in the response is preserved. Bodies are stripped of
    surrounding fences if present.
    """
    out: list[dict] = []
    for fn_name, block_header, body in _FN_BLOCK_RE.findall(text or ""):
        body_stripped = body.strip()
        # Strip outermost code fence if the model wrapped the body.
        if body_stripped.startswith("```"):
            lines = body_stripped.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            body_stripped = "\n".join(lines).strip()
        if not body_stripped:
            continue
        # Detect an optional leading `where ...` clause used to tighten
        # bounds. Remove it from the body and surface separately.
        where_clause: str | None = None
        m = _WHERE_CLAUSE_RE.match(body_stripped + "\n")
        if m:
            where_clause = m.group(1).strip()
            body_stripped = body_stripped[m.end() - 1:].lstrip()
        out.append({
            "fn_name": fn_name.strip(),
            "block": block_header.strip(),
            "where_clause": where_clause,
            "body": body_stripped,
        })
    return out


def _splice_fn_with_where(
    file_path: Path,
    container: str,
    fn_name: str,
    body: str,
    where_clause: str | None = None,
    allow_nonstub: bool = False,
) -> tuple[bool, str, int]:
    """Splice `body` into `fn_name` whose containing impl block matches
    `container`. If `where_clause` is provided, insert it into the fn
    signature (between sig and the opening brace).

    `allow_nonstub=True` (fix-loop) re-fills a fn whose body is already
    filled-but-wrong; default refuses non-stub bodies (build path).

    Returns (success, reason, bytes_delta). `reason` is "ok" on success
    or a short failure tag for telemetry / retro context.
    """
    if not file_path.exists():
        return False, "file_missing", 0
    loc = _locate_fn_by_record(file_path, fn_name, container, "")
    if loc is None:
        # Fallback: the extractor's container string sometimes includes
        # the first member's signature. Match on the impl HEADER prefix
        # only (everything before the first `{`).
        loose_container = container.split("{", 1)[0].strip()
        loc = _locate_fn_by_record(file_path, fn_name, loose_container, "")
    if loc is None:
        return False, "fn_not_found", 0
    sig_byte_start, body_byte_start, body_byte_end, sig_text = loc

    # If a where-clause patch is supplied, splice it into the signature.
    # BYTE space (offsets are byte offsets from the enumerator — see
    # _splice_fn_body_at; a char-indexed str would mis-slice on multibyte UTF-8).
    # The signature spans [sig_byte_start, opening-brace-of-body); insert
    # `\n    <clause>` before the opening brace.
    if where_clause:
        raw = file_path.read_bytes()
        brace_pos = raw.rfind(b"{", sig_byte_start, body_byte_start + 1)
        if brace_pos != -1:
            # Don't double-add a where clause if the sig already has one.
            sig_slice = raw[sig_byte_start:brace_pos].decode("utf-8", errors="replace")
            if "where" not in sig_slice:
                insert = f"\n    {where_clause}\n".encode()
                raw = raw[:brace_pos] + insert + raw[brace_pos:]
                shift = len(insert)
                body_byte_start += shift
                body_byte_end += shift
                file_path.write_bytes(raw)

    ok, delta = _splice_fn_body_at(file_path, body_byte_start, body_byte_end, body,
                                   allow_nonstub=allow_nonstub)
    if not ok:
        return False, "splice_rejected", 0
    return True, "ok", delta


def _find_tests_referencing_fn(
    test_files: list[Path],
    fn_name: str,
    max_blocks: int = 4,
) -> list[tuple[str, str]]:
    """Return up to `max_blocks` test files whose source text mentions `fn_name`.

    Cheap textual heuristic — sufficient for Rust tests that call the fn
    by name (most common). Results are ranked by reference count.
    """
    if not fn_name:
        return []
    ranked: list[tuple[int, Path]] = []
    pat = re.compile(r"\b" + re.escape(fn_name) + r"\b")
    for tf in test_files:
        try:
            txt = tf.read_text(encoding="utf-8")
        except Exception:
            continue
        n = len(pat.findall(txt))
        if n:
            ranked.append((n, tf))
    ranked.sort(key=lambda x: -x[0])
    out: list[tuple[str, str]] = []
    for _, tf in ranked[:max_blocks]:
        try:
            out.append((str(tf), tf.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _extract_body_response(response: str) -> str | None:
    """Pull a function body out of the LLM response.

    Accepts (in order):
      - `<BODY> ... </BODY>` block
      - ```rust ... ``` fenced block
      - ``` ... ``` fenced block
      - Raw response (last resort)

    Returns the trimmed text, or None on empty.
    """
    if not response or not response.strip():
        return None
    m = re.search(r"<BODY>(.*?)</BODY>", response, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        return candidate or None
    for fence in (r"```rust\s*(.*?)```", r"```(?:rs)?\s*(.*?)```"):
        m = re.search(fence, response, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            if candidate:
                return candidate
    return response.strip() or None


# Matches the start of an attribute: `#[`, `#![`, or any of those with
# Rust-legal whitespace between the tokens (the baked skeleton emits the
# spaced token form `# ! [ doc = ... ]`). Group 1 is the optional `!`.
_ATTR_OPENER_RE = re.compile(r'#\s*(!?)\s*\[')


def _attr_span_end(text: str, open_idx: int) -> int:
    """Given `open_idx` = index of the `[` that opens an attribute, return the
    index just past the matching `]`. Bracket depth is tracked ONLY outside
    string literals, so `[`/`]` inside a doc string (markdown links, badges,
    embedded code) do not corrupt the count. Returns -1 if unbalanced.

    Handles normal (`"..."`, with `\\"` escapes) and raw (`r"..."`, `r#"..."#`)
    string literals — the forms that appear inside `#[doc = ...]`.
    """
    n = len(text)
    i = open_idx
    depth = 0
    while i < n:
        c = text[i]
        if c == '"':
            # Possible raw string: count preceding `r` + `#`s.
            hashes = 0
            j = i - 1
            while j >= 0 and text[j] == '#':
                hashes += 1
                j -= 1
            is_raw = j >= 0 and text[j] == 'r'
            i += 1
            if is_raw:
                closer = '"' + ('#' * hashes)
                end = text.find(closer, i)
                i = n if end == -1 else end + len(closer)
            else:
                while i < n:
                    if text[i] == '\\':
                        i += 2
                        continue
                    if text[i] == '"':
                        i += 1
                        break
                    i += 1
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _strip_attrs_for_view(text: str, *, bang: bool, doc_only: bool) -> str:
    """Remove attributes from the PROMPT view only (on-disk file is untouched).

    `bang=True`  → strip inner `#![...]` attributes (crate-level).
    `bang=False` + `doc_only=True` → strip outer `#[doc...]` attributes.

    Attribute spans are located string-literal-aware via `_attr_span_end`, so
    `[`/`]` inside doc content can never desync the parser (the bug that leaked
    bare doc text into the view). The trailing newline after a stripped
    attribute is swallowed so no blank line is left behind.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '#':
            m = _ATTR_OPENER_RE.match(text, i)
            if m:
                is_bang = m.group(1) == '!'
                open_idx = m.end() - 1  # index of the `[`
                end = _attr_span_end(text, open_idx)
                if end != -1:
                    inner = text[open_idx + 1:end - 1].lstrip()
                    want = (is_bang if bang
                            else (not is_bang and (not doc_only
                                                   or inner.startswith('doc'))))
                    if want:
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
        out.append(text[i])
        i += 1
    return ''.join(out)


def _strip_doc_attrs(text: str) -> str:
    """Strip `#[doc = "..."]` attributes from the file view shown to the LLM.

    anyhow's source embeds hundreds of chars of example code (with `[]`
    markdown and embedded newlines) in `#[doc = "..."]` per public fn. They
    eat prompt budget and don't help fill a body. Stripped from the PROMPT
    view only — the on-disk file keeps them so `cargo doc` still works.
    """
    return _strip_attrs_for_view(text, bang=False, doc_only=True)


def _strip_crate_attrs(text: str) -> str:
    """Strip inner `#![...]` crate attributes from the file view shown to the
    LLM (including the multi-line `#![doc = "..."]` module doc). PROMPT view
    only — the on-disk file keeps them so cargo builds correctly.
    """
    return _strip_attrs_for_view(text, bang=True, doc_only=False)


def _doc_attr_string(attr_inner: str) -> "str | None":
    """Given the text inside an attribute's brackets (e.g. `doc = " summary"`),
    return the doc string content, or None if this is not a `doc = "..."` attr.
    `#[doc(hidden)]` and other non-string doc attrs return "" (drop, no text)."""
    m = re.match(r'doc\s*=\s*', attr_inner)
    if not m:
        return None
    rest = attr_inner[m.end():].lstrip()
    if not rest.startswith('"'):
        return ""  # doc(hidden) / doc(alias=...) etc — no prose to keep
    body = rest[1:]
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == '\\' and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
            continue
        if body[i] == '"':
            break
        out.append(body[i])
        i += 1
    return ''.join(out)


def _condense_doc_run(contents: "list[str]") -> str:
    """Condense a run of consecutive doc-attr strings to a one-line summary:
    first sentence of prose, with fenced code, badges, and link-refs dropped
    and inline `[text](url)` reduced to `text`. Empty when the doc is all
    examples/badges (nothing worth a line)."""
    summary: list[str] = []
    for raw in contents:
        s = raw.strip()
        if s.startswith('```'):
            break                      # code example begins — stop the summary
        if not s:
            if summary:
                break                  # blank line after prose ends the summary
            continue
        if s.startswith('[![') or re.match(r'\[[^\]]+\]:', s):
            continue                   # badge / link reference definition
        summary.append(s)
        if '.' in s:
            break                      # sentence terminator reached
    text = ' '.join(summary).strip()
    if '.' in text:
        text = text[:text.index('.') + 1]
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)   # [t](url) -> t
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:200].rstrip()


def _condense_doc_attrs(text: str) -> str:
    """Condense (not delete) doc comments in the prompt view: each run of
    `#[doc=...]` / `#![doc=...]` attributes collapses to a single
    `// <intent>` (or `//! <intent>`) line. Non-doc `#![...]` crate attrs are
    dropped (they don't help fill a body). All other tokens pass through.

    Deterministic Python (Hard Rule 9) — preserves the author's intent overview
    at ~1 line instead of either dumping hundreds of chars of examples or
    erasing the intent entirely (FR-026). String-literal-aware via
    `_attr_span_end`, so `[]` inside doc content can never desync the parse.
    """
    out: list[str] = []
    run: list[str] = []
    run_bang = False
    run_active = False

    def _flush() -> None:
        nonlocal run, run_active, run_bang
        if run_active:
            s = _condense_doc_run(run)
            if s:
                out.append(('//! ' if run_bang else '// ') + s + '\n')
            run = []
            run_active = False
            run_bang = False

    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '#':
            m = _ATTR_OPENER_RE.match(text, i)
            if m:
                is_bang = m.group(1) == '!'
                open_idx = m.end() - 1
                end = _attr_span_end(text, open_idx)
                if end != -1:
                    inner = text[open_idx + 1:end - 1].strip()
                    doc_str = _doc_attr_string(inner)
                    if doc_str is not None:           # a doc attr
                        if not run_active:
                            run_active = True
                            run_bang = is_bang
                        if doc_str:
                            run.append(doc_str)
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
                    if is_bang:                       # non-doc crate attr → drop
                        _flush()
                        i = end
                        if i < n and text[i] == '\n':
                            i += 1
                        continue
        if not c.isspace():
            _flush()
        out.append(c)
        i += 1
    _flush()
    return ''.join(out)


# Below this file-view size, keep doc comments in FULL (their usage examples are
# concrete guidance). Above it, condense to one-line intents to protect budget.
# ~1/6 of a single call → only the few genuinely huge files (e.g. error.rs) get
# condensed; the many small/medium files keep their examples.
_DOC_KEEP_FULL_LIMIT = SINGLE_CALL_CHAR_LIMIT // 6


def _annotate_file_view(file_text: str, target_fn: str) -> str:
    """Return a view of file_text where every unimplemented stub is prefixed
    with a comment clarifying its status. The TARGET fn (the one being filled
    in this call) is marked distinctly. Helps the model reason about which
    fns are "to-be-filled later" vs the immediate target, instead of treating
    the stubs as permanent missing pieces it must work around.

    Doc comments are kept in FULL when the view fits comfortably in budget —
    the crate's own usage examples are concrete WRONG/RIGHT guidance the model
    learns from, so we never strip them from a small prompt (FR-026 amended).
    Only when the file view is large enough to threaten the context budget do
    we CONDENSE docs to a one-line intent. Non-doc crate attrs are always
    dropped. PROMPT view only — the on-disk file is untouched.
    """
    # Keep examples when there's room; condense only big files.
    if len(file_text) > _DOC_KEEP_FULL_LIMIT:
        file_text = _condense_doc_attrs(file_text)
    else:
        file_text = _strip_crate_attrs(file_text)   # drop only #![...] crate attrs
    out: list[str] = []
    last_fn_end = 0
    for m in _FN_RE_GLOBAL.finditer(file_text):
        fn_name = m.group(2)
        sig_start = m.start()
        body_start = m.end()
        depth = 1
        j = body_start
        while j < len(file_text) and depth > 0:
            c = file_text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth != 0:
            continue
        body_end = j - 1
        body = file_text[body_start:body_end]
        is_stub = _is_unimplemented_body(body)
        # Find the indent of the fn's first line.
        line_start = file_text.rfind("\n", 0, sig_start) + 1
        indent = ""
        for ch in file_text[line_start:sig_start]:
            if ch in (" ", "\t"):
                indent += ch
            else:
                break
        # Emit everything before this fn from where we left off.
        out.append(file_text[last_fn_end:sig_start])
        if fn_name == target_fn:
            annot = (f"{indent}// >>> TARGET — implement THIS function below; the body is "
                     f"currently `unimplemented!()`. <<<\n")
        elif is_stub:
            annot = (f"{indent}// SKELETON STUB — body will be filled in a separate "
                     f"call; you may rely on it existing (signature is fixed).\n")
        else:
            annot = ""
        out.append(annot)
        out.append(file_text[sig_start:j])
        last_fn_end = j
    out.append(file_text[last_fn_end:])
    return "".join(out)


def _enumerate_macro_variants(
    src_dir: Path,
) -> list[tuple[str, str, str, str]]:
    """Scan all .rs files under `src_dir` for `macro_rules!` blocks and
    return a list of `(macro_name, cfg_pred, body, source_file)` tuples.

    `cfg_pred` is the predicate of the `#[cfg(...)]` attribute immediately
    preceding the macro (empty string if no cfg gate). `body` is the full
    `macro_rules! NAME { ... }` text. Used by
    `_compute_cluster_macro_invariants` to decide which variant of a
    multiply-defined macro is active under a given cluster.
    """
    variants: list[tuple[str, str, str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(r"macro_rules!\s+(\w+)\s*\{", text):
            name = m.group(1)
            start = m.start()
            # Walk backwards from the macro to find a leading #[cfg(...)]
            # attribute on the same item. Stop at a blank line or another
            # item — only attributes that immediately precede the macro
            # gate it.
            head = text[max(0, start - 800):start]
            cfg_pred = ""
            for am in reversed(list(re.finditer(
                    r"#\[cfg\(((?:[^()]|\([^()]*\))+)\)\]\s*$",
                    head, re.MULTILINE))):
                cfg_pred = am.group(1).strip()
                break
            # Match balanced braces of the macro_rules! body.
            i = text.find("{", start)
            if i < 0:
                continue
            depth = 1
            j = i + 1
            while j < len(text) and depth > 0:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            if depth != 0:
                continue
            variants.append((name, cfg_pred, text[start:j], p.name))
    return variants


def _detect_crate_no_std_invariant(src_dir: Path) -> str:
    """Return a no_std-aware invariant string if the crate's lib.rs
    declares `#![no_std]`. Otherwise empty.

    Crate-level rather than cluster-level: anyhow is `#![no_std]` by
    default with `extern crate std` conditionally enabled via the
    `std` feature. Even with `std` enabled, `format!`, `vec!`, etc.
    are NOT auto-imported in a no_std crate root — they must be
    pulled in explicitly. The model trips over this every time.
    """
    lib_rs = src_dir / "src" / "lib.rs" if (src_dir / "src").exists() else src_dir / "lib.rs"
    if not lib_rs.exists():
        return ""
    try:
        head = lib_rs.read_text(encoding="utf-8")[:4000]
    except Exception:
        return ""
    if not re.search(r"#\s*!\s*\[\s*no_std\s*\]", head):
        return ""
    return (
        "crate is #![no_std]. `format!`, `vec!`, `print!`, `println!` "
        "are NOT in scope by default — even when `feature = \"std\"` is "
        "enabled. If you need to produce a String, prefer passing the "
        "Display value directly (most APIs take `M: Display` and don't "
        "need a String at all). Only if you genuinely need allocation, "
        "use `alloc::format!` after `use alloc::format;` or "
        "`crate::__private::format` if the crate provides it."
    )


def _compute_cluster_macro_invariants(
    src_dir: Path, cluster: "BuildCluster | None",
) -> str:
    """For macros defined multiple times under different `#[cfg(...)]`,
    select the variant active under `cluster`'s cfg-set and render a
    compact "in this cluster, NAME!(...) expands to: <body>" line per
    macro. Macros with a single variant are skipped (no ambiguity).

    Without this section the model has to read every `#[cfg(...)]`
    variant in the MACROS block and reason about which one applies —
    leading to defensive fills (e.g. `unwrap_or(&DEFAULT)`) when the
    active expansion in this cluster guarantees a `Some`.
    """
    if cluster is None:
        return ""
    variants = _enumerate_macro_variants(src_dir)
    if not variants:
        return ""
    by_name: dict[str, list[tuple[str, str, str]]] = {}
    for name, cfg, body, src in variants:
        by_name.setdefault(name, []).append((cfg, body, src))
    cfg_true = {_normalize_cfg_pred(p) for p in cluster.cfg_true}
    cfg_false = {_normalize_cfg_pred(p) for p in cluster.cfg_false}
    lines: list[str] = []
    for name in sorted(by_name):
        vs = by_name[name]
        if len(vs) < 2:
            continue
        # Pick the variant whose cfg evaluates True. A variant with no cfg
        # is treated as always-active fallback.
        active: tuple[str, str, str] | None = None
        unconditional: tuple[str, str, str] | None = None
        for cfg, body, src in vs:
            if not cfg:
                unconditional = (cfg, body, src)
                continue
            v = _eval_cfg_pred(cfg, cfg_true, cfg_false)
            if v is True:
                active = (cfg, body, src)
                break
        if active is None and unconditional is not None:
            active = unconditional
        if active is None:
            continue
        cfg, body, src = active
        # Render compactly: strip the `macro_rules! NAME {` header and
        # outer `}` so the model sees just the arm(s).
        compact = body
        head_match = re.search(r"macro_rules!\s+\w+\s*\{\s*", compact)
        if head_match:
            compact = compact[head_match.end():].rstrip()
            if compact.endswith("}"):
                compact = compact[:-1].rstrip()
        # Collapse internal whitespace runs for compactness.
        compact = re.sub(r"\n\s*\n+", "\n", compact)
        if len(compact) > 240:
            compact = compact[:237] + "..."
        cfg_note = f" [cfg={cfg}]" if cfg else " [always]"
        lines.append(f"{name}!(...){cfg_note} → {compact}")
    if not lines:
        return ""
    header = (
        "Macros with multiple #[cfg]-gated variants — only the active "
        "expansion for this cluster is shown."
    )
    return header + "\n" + "\n".join(lines)


def _collect_crate_macros(src_dir: Path, max_chars: int = max(6_000, SINGLE_CALL_CHAR_LIMIT // 25)) -> str:  # Plan 9 F1: budget-derived (was 6000)
    """Extract `macro_rules!` blocks from every .rs file in `src_dir`.

    Returned as a compact section the model can scan to understand how
    user-facing macros invoke internal helpers (the most common reason a
    private fn has no direct test-name match).
    """
    macros: list[tuple[str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(r"(?:#\[macro_export\][^\n]*\n)?macro_rules!\s+(\w+)\s*\{",
                              text):
            start = m.start()
            # Match balanced braces of the macro_rules! body.
            i = text.find("{", start)
            if i < 0:
                continue
            depth = 1
            j = i + 1
            while j < len(text) and depth > 0:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            if depth != 0:
                continue
            block = text[start:j]
            macros.append((p.name, f"// from {p.name}\n{block}"))
    if not macros:
        return ""
    joined = "\n\n".join(b for _, b in macros)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n... (macros truncated)"
    return joined


def _find_tests_for_fn(
    tests_dir_files: list[Path],
    fn_name: str,
    file_rel: str = "",
    src_dir: Path | None = None,
    max_blocks: int = 4,
    container: str = "",
) -> list[tuple[str, str]]:
    """Layered heuristic to find tests relevant to one fn.

    Order of attempts (each adds to the candidate set):
      0. Container-type filter (P5.4) — when fn_name is generic (`new`,
         `fmt`, `source`, etc.), match by the type name extracted from
         `container` rather than the fn name alone. Tests for
         `impl Display for ContextError::fmt` only count when they
         mention "ContextError".
      1. Direct name reference — tests calling `fn_name` literally.
      2. File-stem match — for fn in `src/ensure.rs`, include tests whose
         filename contains "ensure".
      3. Macro-aware — if `src_dir` is given and `file_rel`'s file defines
         macros, include tests that call those macros by name.

    Results ranked by relevance score (container > direct > stem > macro)
    then by hit count. Returns up to `max_blocks` (path, content) tuples.
    """
    if not fn_name and not file_rel:
        return []
    # Build candidate set with scores.
    scored: dict[Path, tuple[int, int]] = {}  # path → (tier, hits) — lower tier = better

    # P5.4 Tier 0: when fn_name is generic (very common: new, fmt, source,
    # clone, drop, default), filter by the container's TYPE NAME so we don't
    # match every test that mentions e.g. "new". Falls back to the regular
    # name-based search if the container doesn't yield a clear type name.
    _GENERIC_FN_NAMES = {
        "new", "fmt", "source", "clone", "default", "drop", "next",
        "from", "into", "deref", "deref_mut", "as_ref", "as_mut",
        "hash", "eq", "cmp", "partial_cmp", "from_str", "anyhow_kind",
        "provide",
    }
    container_type = ""
    if container:
        # Match the "for X" part of "impl Trait for X<...>"; fall back to
        # "impl X<...>" for inherent impls.
        m = re.search(r"\bfor\s+([A-Za-z_]\w*)", container)
        if not m:
            m = re.search(r"\bimpl\s+(?:<[^>]*>\s+)?([A-Za-z_]\w*)", container)
        if m:
            container_type = m.group(1)
    if fn_name in _GENERIC_FN_NAMES and container_type:
        pat0 = re.compile(r"\b" + re.escape(container_type) + r"\b")
        for tf in tests_dir_files:
            try:
                txt = tf.read_text(encoding="utf-8")
            except Exception:
                continue
            n = len(pat0.findall(txt))
            if n:
                cur = scored.get(tf, (99, 0))
                # Tier 0 = highest priority. Hit count = type mentions.
                scored[tf] = (0, max(cur[1], n))

    # Tier 1: direct name.
    if fn_name:
        pat = re.compile(r"\b" + re.escape(fn_name) + r"\b")
        for tf in tests_dir_files:
            try:
                txt = tf.read_text(encoding="utf-8")
            except Exception:
                continue
            n = len(pat.findall(txt))
            if n:
                cur = scored.get(tf, (99, 0))
                scored[tf] = (min(cur[0], 1), max(cur[1], n))

    # Tier 2: file-stem match.
    if file_rel:
        stem = Path(file_rel).stem.lower()
        # Strip common prefixes/suffixes that don't help matching:
        stem = re.sub(r"^(mod|tests?)_", "", stem)
        for tf in tests_dir_files:
            tname = tf.stem.lower()
            if stem and (stem in tname or tname in stem):
                cur = scored.get(tf, (99, 0))
                scored[tf] = (min(cur[0], 2), max(cur[1], 1))

    # Tier 3: macro-aware.
    if file_rel and src_dir is not None:
        fp = src_dir / file_rel
        macro_names: set[str] = set()
        try:
            txt = fp.read_text(encoding="utf-8")
            for m in re.finditer(r"macro_rules!\s+(\w+)\s*\{", txt):
                macro_names.add(m.group(1))
        except Exception:
            pass
        if macro_names:
            pats = [re.compile(r"\b" + re.escape(n) + r"\s*!") for n in macro_names]
            for tf in tests_dir_files:
                try:
                    ttxt = tf.read_text(encoding="utf-8")
                except Exception:
                    continue
                hits = sum(len(p.findall(ttxt)) for p in pats)
                if hits:
                    cur = scored.get(tf, (99, 0))
                    scored[tf] = (min(cur[0], 3), max(cur[1], hits))

    ranked = sorted(scored.items(), key=lambda kv: (kv[1][0], -kv[1][1]))[:max_blocks]
    out: list[tuple[str, str]] = []
    for path, _ in ranked:
        try:
            out.append((str(path), path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _leaf_first_fn_order(
    stubs: list[tuple[str, str]],
    name_counts: dict[str, int] | None,
) -> list[tuple[str, str]]:
    """Sort stubs so the most-referenced fns come first (likely call-graph
    leaves — utility fns relied on by many others). Public-API entry points
    end up last, where they can see real helper bodies on disk.

    `name_counts` is the `{fn_name: ref_count}` map written during the
    pre-strip bake step (`_bake_skeleton_artifacts`). When empty/None,
    falls back to deterministic order (file, fn_name).
    """
    if not name_counts:
        return sorted(stubs)
    scores: dict[tuple[str, str], int] = {}
    for s in stubs:
        _, fn = s
        scores[s] = name_counts.get(fn, 0) if fn else 0
    # Higher refs = more depended on = fill earlier (leaf-first).
    return sorted(stubs, key=lambda s: (-scores.get(s, 0), s[0], s[1]))


# ---------------------------------------------------------------------------
# Model self-correction notes — stored in functional_requirements_{crate}.md
# under the `## Model Notes — Confusions to Avoid` section. The MODEL writes
# entries in its own voice after fix loops; the next attempt reads them as
# part of the per-fn prompt. See CLAUDE.md "The model authors its own
# anti-hallucination notes".
# ---------------------------------------------------------------------------

_MODEL_NOTES_HEADER = "## Model Notes — Confusions to Avoid"
_MODEL_NOTES_MAX_CHARS = 4000  # cap on the notes section in any one prompt
_MODEL_NOTES_MAX_ENTRIES = 80  # hard cap; older entries dropped LRU on overflow


def _slice_model_notes(fr_text: str) -> tuple[str, str]:
    """Split FR text into (body_without_notes, notes_section_inclusive).

    notes_section_inclusive starts at the `## Model Notes` header line and
    extends to the next top-level `##` heading or EOF. Returns empty notes
    string when the section is absent.
    """
    if _MODEL_NOTES_HEADER not in fr_text:
        return fr_text, ""
    idx = fr_text.index(_MODEL_NOTES_HEADER)
    body = fr_text[:idx].rstrip() + "\n"
    rest = fr_text[idx:]
    # Find the next top-level heading after the notes header (skip the
    # notes header itself).
    m = re.search(r"\n##\s", rest[len(_MODEL_NOTES_HEADER):])
    if m:
        end = len(_MODEL_NOTES_HEADER) + m.start() + 1  # keep the '\n'
        notes = rest[:end]
        body += rest[end:]
    else:
        notes = rest
    return body, notes.rstrip() + "\n"


def _preserve_model_notes(old_fr_text: str | None, new_fr_text: str) -> str:
    """Re-append the `## Model Notes` section from old_fr_text to new_fr_text.

    Used at every TASK 1 write site so regenerating FR never wipes the
    model's accumulated self-corrections.
    """
    if not old_fr_text or _MODEL_NOTES_HEADER not in old_fr_text:
        return new_fr_text
    _, notes_section = _slice_model_notes(old_fr_text)
    if not notes_section.strip():
        return new_fr_text
    # If the new FR happens to already include a notes header (it shouldn't
    # but be defensive), strip it first.
    new_body, _ = _slice_model_notes(new_fr_text)
    return new_body.rstrip() + "\n\n" + notes_section


def _load_model_notes(fr_path: Path) -> list[str]:
    """Return the list of note bullets currently in FR. Each bullet is the
    text after `- ` on a single logical entry (multi-line entries joined)."""
    try:
        fr_text = fr_path.read_text(encoding="utf-8")
    except Exception:
        return []
    _, notes_section = _slice_model_notes(fr_text)
    if not notes_section.strip():
        return []
    notes: list[str] = []
    cur: list[str] = []
    for ln in notes_section.splitlines():
        if ln.startswith("- "):
            if cur:
                notes.append(" ".join(cur).strip())
                cur = []
            cur.append(ln[2:].strip())
        elif cur and ln.startswith("  "):
            cur.append(ln.strip())
        elif cur and not ln.strip():
            notes.append(" ".join(cur).strip())
            cur = []
    if cur:
        notes.append(" ".join(cur).strip())
    return [n for n in notes if n]


def _save_model_notes(fr_path: Path, notes: list[str]) -> None:
    """Rewrite the `## Model Notes` section in FR with the given bullets.
    Creates the FR file (with just the section) if it doesn't exist.
    """
    if fr_path.exists():
        try:
            fr_text = fr_path.read_text(encoding="utf-8")
        except Exception:
            fr_text = ""
    else:
        fr_text = ""
    body, _ = _slice_model_notes(fr_text)
    if not body.strip():
        body = "# Functional Requirements\n\n"
    section = _MODEL_NOTES_HEADER + "\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    out = body.rstrip() + "\n\n" + section
    try:
        fr_path.parent.mkdir(parents=True, exist_ok=True)
        fr_path.write_text(out, encoding="utf-8")
    except Exception as e:
        _log(f"  [model-notes] write failed: {e}")


def _filter_model_notes_for_prompt(
    fr_path: Path,
    file_rel: str,
    fn_name: str,
    file_text: str = "",
    current_error: str = "",
    max_chars: int = _MODEL_NOTES_MAX_CHARS,
) -> str:
    """Pull notes from FR ranked by relevance for the current (file, fn).

    P3.4 ranking tiers (lower number = higher priority):
      1. Note mentions an error code present in `current_error` (E0xxx).
      2. Note keyword overlap with `current_error`'s _note_signature keywords.
      3. Note mentions the file's basename or fn name.
      4. Note mentions any symbol (type/fn) appearing in `file_text`.
      5. Otherwise (recency only).

    Returns a single string ready to insert into a <MODEL_NOTES> block,
    capped at `max_chars`. Empty string if no notes apply.
    """
    notes = _load_model_notes(fr_path)
    if not notes:
        return ""
    base = Path(file_rel).stem if file_rel else ""
    symbols: set[str] = set()
    if file_text:
        for m in re.finditer(r"\b(?:fn|struct|enum|trait|type)\s+([A-Za-z_]\w*)",
                              file_text):
            symbols.add(m.group(1))
    # Extract error context (codes + keywords) for tier 1/2 matching.
    err_codes: set[str] = set()
    err_keywords: set[str] = set()
    if current_error:
        err_codes = set(re.findall(r"\bE\d{3,4}\b", current_error))
        low_err = current_error.lower()
        err_keywords = {k for k in _NOTE_SIGNATURE_KEYWORDS if k in low_err}

    def tier(n: str) -> int:
        lower = n.lower()
        if err_codes and any(c in lower.upper() or c.lower() in lower
                              for c in err_codes):
            return 1
        n_codes, n_kw = _note_signature(n)
        if err_keywords and n_kw and len(err_keywords & n_kw) >= 2:
            return 2
        if (base and base.lower() in lower) or (fn_name and fn_name.lower() in lower):
            return 3
        if any(sym in n for sym in symbols):
            return 4
        return 5

    indexed = list(enumerate(notes))
    indexed.sort(key=lambda iv: (tier(iv[1]), -iv[0]))
    out_lines: list[str] = []
    total = 0
    for _, n in indexed:
        line = f"- {n}"
        if total + len(line) + 1 > max_chars:
            break
        out_lines.append(line)
        total += len(line) + 1
    return "\n".join(out_lines)




_NOTE_HASH_CACHE: dict[Path, set[str]] = {}

# Curated set of Rust-load-bearing keywords that, when co-mentioned in two
# notes, almost certainly mean the notes are about the same confusion.
# Used by _note_signature for semantic dedup (P3.3).
_NOTE_SIGNATURE_KEYWORDS = {
    "typeid", "'static", "static", "&dyn", "dyn", "box", "nonnull",
    "ptr", "as_ptr", "as_ref", "from_raw", "deref",
    "unimplemented", "todo", "unsafe",
    "trait", "impl", "generic", "lifetime", "sized", "unsized",
    "format", "display", "debug",
    "feature", "no_std", "core::", "std::",
    "ref::", "wrapper", "delegation",
}


def _note_hash(text: str) -> str:
    """Exact-text hash for early-rejection of literal duplicates."""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:16]


def _note_signature(text: str) -> tuple[frozenset, frozenset]:
    """Semantic signature: (error codes mentioned, keyword set).

    P3.3: two notes are considered semantically duplicate when their
    signatures' keyword sets overlap by >= 80%. Catches the 4-way
    paraphrase of "TypeId::of::<T>() needs T: 'static" we saw overnight,
    where exact-hash dedup let all four through.
    """
    low = text.lower()
    err_codes = frozenset(re.findall(r"\be\d{3,4}\b", low))
    keywords = frozenset(k for k in _NOTE_SIGNATURE_KEYWORDS if k in low)
    return err_codes, keywords


def _notes_are_semantic_dupes(a: str, b: str) -> bool:
    """True when two notes mostly mean the same thing.

    Two-tier check:
      - Same error code set (non-empty) → dupe regardless of phrasing.
      - Otherwise: keyword overlap >= 2 keywords AND >= 50% Jaccard.
        50% catches the overnight TypeId-paraphrase set where shared
        keywords are {typeid, static, generic} but each note adds one
        unique extra ("impl", "lifetime", etc.).
    """
    a_err, a_kw = _note_signature(a)
    b_err, b_kw = _note_signature(b)
    if a_err and b_err and a_err == b_err:
        return True
    if not a_kw or not b_kw:
        return False
    inter = len(a_kw & b_kw)
    union = len(a_kw | b_kw)
    if inter < 2:
        return False
    return union > 0 and inter / union >= 0.5


def _append_model_note(
    fr_path: Path,
    note: str,
    error_codes: "list[str] | set[str] | None" = None,
) -> bool:
    """Append `note` to FR's model-notes section. Returns True if added,
    False on dedup or empty/short input.

    Dedup logic (P3.3 tightened):
      1. Exact hash match → reject (literal duplicate)
      2. Semantic match against any existing note → reject
         (paraphrase of an already-known rule)

    P6.4: when `error_codes` is given, prefix the stored note with a
    `[E0xxx,E0yyy]` tag. This tag is readable to the LLM and lets us
    later surface past notes tied to recurring error codes alongside
    <RECURRING_ERRORS>.
    """
    note = (note or "").strip()
    if not note or len(note) < 20:
        return False
    if note.startswith("- "):
        note = note[2:].strip()
    # P6.4: prepend error-code tag (deduped, sorted, only well-formed codes).
    if error_codes:
        clean = sorted({c for c in error_codes
                        if isinstance(c, str) and re.fullmatch(r"E\d{3,4}", c)})
        if clean and not note.startswith("[E"):
            note = f"[{','.join(clean)}] {note}"
    existing = _load_model_notes(fr_path)
    existing_hashes = _NOTE_HASH_CACHE.get(fr_path)
    if existing_hashes is None:
        existing_hashes = {_note_hash(n) for n in existing}
        _NOTE_HASH_CACHE[fr_path] = existing_hashes
    h = _note_hash(note)
    if h in existing_hashes:
        return False
    # P3.3: semantic dedup. Walk existing notes; reject if any is a paraphrase.
    for prev in existing:
        if _notes_are_semantic_dupes(prev, note):
            return False
    existing.append(note)
    existing_hashes.add(h)
    if len(existing) > _MODEL_NOTES_MAX_ENTRIES:
        dropped = existing[:-_MODEL_NOTES_MAX_ENTRIES]
        existing = existing[-_MODEL_NOTES_MAX_ENTRIES:]
        for d in dropped:
            existing_hashes.discard(_note_hash(d))
    _save_model_notes(fr_path, existing)
    return True


_NOTE_EXTRACTION_PROMPT = """A model attempted to implement a Rust function and
the first attempt did not compile. The second (or later) attempt did compile.

Read the failed body, the cargo error from that failure, and the body that
finally compiled. Then write a SINGLE short note to your future self, in
first-person voice, that names the specific confusion and how to avoid it.

Format guidance:
  - Lead with what you tried that failed ("I tried X").
  - Name the reason it failed in concrete terms ("because Y").
  - State the correction as a rule ("Use Z, not X").
  - Keep it to ONE sentence; ≤300 chars.
  - Do NOT mention the function name, file name, or crate name — the note
    should generalise. Focus on the language/library/type confusion.
  - Output ONLY the note text, no preamble, no markdown, no quotes.

If no general lesson can be extracted (e.g. the failure was just a typo),
output the literal token NONE.

<FAILED_BODY>
{failed_body}
</FAILED_BODY>

<COMPILE_ERROR>
{error_excerpt}
</COMPILE_ERROR>

<FIXED_BODY>
{fixed_body}
</FIXED_BODY>
"""


def _extract_self_note(
    failed_body: str, error_excerpt: str, fixed_body: str,
) -> str | None:
    """One LLM call to author a self-correction note. Returns None when the
    model declines (responds with NONE or empty).
    """
    if not failed_body or not error_excerpt:
        return None
    prompt = _NOTE_EXTRACTION_PROMPT.format(
        failed_body=failed_body[:_FAILED_BODY_CAP],
        error_excerpt=error_excerpt[:_FIX_ERR_CAP],
        fixed_body=fixed_body[:_FAILED_BODY_CAP] if fixed_body else "(no fixed body — fix loop exhausted)",
    )
    # Two-attempt ladder for extraction: default (thinking on) then think=False
    # fallback. Mirrors the body-extraction recovery — Qwen3.6 occasionally
    # returns empty with think=False on small prompts and vice versa.
    resp = ""
    try:
        resp = ask(prompt, model=_TASK2_MODEL)
    except Exception as e:
        _log(f"  [model-notes] extraction failed (default): {e}")
    if not resp or not resp.strip():
        try:
            resp = ask(prompt, model=_TASK2_MODEL, think=False)
        except Exception as e:
            _log(f"  [model-notes] extraction failed (think=False): {e}")
            return None
    if not resp:
        return None
    txt = resp.strip()
    # Pull just the last non-empty line if model wrote multiple lines.
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return None
    candidate = lines[-1]
    # Strip surrounding quotes / fences.
    candidate = candidate.strip("`").strip("\"'")
    if candidate.upper() == "NONE" or len(candidate) < 20:
        return None
    return candidate[:300]




# P5.5: track Exxxx error codes seen across the current run. When a code
# fires 3+ times, auto-promote a short rule to every subsequent per-fn
# prompt (as a <RECURRING_ERRORS> block). Catches when one class of
# Rust gotcha keeps biting and one fix-loop's local note isn't enough.
from collections import Counter as _Counter_PY

_ERROR_CODE_COUNTS: "_Counter_PY | None" = None
# P6.3: per-error-code accumulator of cargo-error excerpts (capped). When
# an error code recurs, we mine these for common type tokens / file
# locations and surface them in <RECURRING_ERRORS>.
_ERROR_CODE_EXCERPTS: dict[str, list[str]] = {}
_ERROR_EXCERPT_MAX_PER_CODE = 10  # keep at most N excerpts per code
_ERROR_EXCERPT_MAX_CHARS = 600   # truncate each excerpt
_ERROR_CODE_PROMOTED_THRESHOLD = 3
_ERROR_CODE_HINTS: dict[str, str] = {
    "E0277": "trait bound not satisfied — TWO opposite cases (see the matching gotcha for concrete examples): (a) by-value return/use of a possibly-unsized `T` (\"size cannot be known\") → you MAY add `where T: Sized` to THIS one fn, or return `&T`/`Box<T>`; (b) `T doesn't implement Display/Debug/...` → you may NOT add that bound (it is the caller's contract) — do not call that trait on `T`, build the value another way.",
    "E0308": "mismatched types — check whether you're returning a Result vs Option, owned vs referenced, or wrong generic instantiation.",
    "E0382": "use of moved value — clone, borrow, or use Cow/Arc if you need both old and new value.",
    "E0507": "cannot move out of borrowed — use .clone(), .as_ref(), or destructure with `ref`.",
    "E0599": "no method found — likely calling a method that doesn't exist on this type. Read TYPES_IN_SCOPE for the exact method names.",
    "E0609": "no field — usually trying to access a method as if it were a field. Use .name() not .name. See TYPES_IN_SCOPE.",
    "E0615": "took value of method instead of calling it — `.method` vs `.method()`. Add parens.",
    "E0107": "wrong number of generic arguments — check the type's actual generic signature in TYPES_IN_SCOPE.",
    "E0310": "type parameter may not live long enough — add a `'static` bound.",
    "E0119": "conflicting impls — remove the duplicate or specialise via different generic params.",
    "E0554": "feature attribute only on nightly — guard with #[cfg] or check that the nightly toolchain is active.",
}


def _record_error_codes_seen(output: str) -> None:
    """Increment the global counter for each unique E0xxx code found in
    `output`. Also accumulate a short excerpt per code for later mining
    of recurring type-token context (P6.3)."""
    global _ERROR_CODE_COUNTS
    if _ERROR_CODE_COUNTS is None:
        _ERROR_CODE_COUNTS = _Counter_PY()
    if not output:
        return
    codes = set(re.findall(r"\bE\d{3,4}\b", output))
    for c in codes:
        _ERROR_CODE_COUNTS[c] += 1
        # Capture an excerpt: the error[Exxxx]: line + its few surrounding
        # lines. Per-code list is capped to N excerpts (FIFO).
        m = re.search(
            r"error\[" + re.escape(c) + r"\][^\n]*\n(?:[^\n]*\n){0,6}",
            output,
        )
        if m:
            excerpt = m.group(0).strip()[:_ERROR_EXCERPT_MAX_CHARS]
            bucket = _ERROR_CODE_EXCERPTS.setdefault(c, [])
            bucket.append(excerpt)
            if len(bucket) > _ERROR_EXCERPT_MAX_PER_CODE:
                bucket.pop(0)


def _mine_common_tokens(excerpts: list[str], top_n: int = 3) -> list[tuple[str, int]]:
    """From a list of cargo error excerpts, return the most common Rust
    type-like tokens (CapitalisedCamelCase names, with possible generic
    decoration). Used to enrich RECURRING_ERRORS with type context.
    """
    if not excerpts:
        return []
    tok: _Counter_PY[str] = _Counter_PY()
    # Match `Foo`, `Foo<T>`, `Box<dyn StdError + Send + Sync>` etc.
    type_re = re.compile(
        r"\b([A-Z][A-Za-z0-9_]*(?:<[^<>\n]{1,60}>)?)\b"
    )
    # Generic noise that's NOT useful type context.
    skip = {
        "Err", "Ok", "None", "Some", "Box", "Self", "T", "E", "C", "V",
        "From", "Into", "Vec", "String", "Result", "Option",
    }
    for ex in excerpts:
        seen_here: set[str] = set()
        for m in type_re.finditer(ex):
            t = m.group(1)
            # Filter: must be longer than 3 chars OR clearly composite.
            bare = t.split("<", 1)[0]
            if bare in skip:
                continue
            if len(bare) < 4 and "<" not in t:
                continue
            if t in seen_here:
                continue  # one count per excerpt
            seen_here.add(t)
            tok[t] += 1
    return [(t, n) for t, n in tok.most_common(top_n) if n >= 2]


def _promoted_error_rules(fr_path: "Path | None" = None) -> str:
    """Return a compact block listing error codes that have recurred 3+
    times this run, with their canonical fix hint and (P6.3) any shared
    type-tokens mined from the error excerpts.

    P6.4: when `fr_path` is provided, scan stored model notes for
    `[E0xxx,...]` prefix tags and append up to 2 matching past notes
    per promoted code (most-recent first, deduped). This gives the LLM
    the exact rule it wrote for itself the previous time the same code
    fired."""
    if not _ERROR_CODE_COUNTS:
        return ""
    # Pre-build code → notes index from the FR file (P6.4).
    code_to_notes: dict[str, list[str]] = {}
    if fr_path is not None:
        try:
            stored = _load_model_notes(fr_path)
        except Exception:
            stored = []
        tag_re = re.compile(r"^\[((?:E\d{3,4})(?:,E\d{3,4})*)\]\s*(.+)$")
        # Walk newest-first so the most recent rule wins per code.
        for n in reversed(stored):
            m = tag_re.match(n)
            if not m:
                continue
            body = m.group(2).strip()
            for code in m.group(1).split(","):
                bucket = code_to_notes.setdefault(code, [])
                if body not in bucket:
                    bucket.append(body)

    promoted: list[str] = []
    for code, count in _ERROR_CODE_COUNTS.most_common():
        if count < _ERROR_CODE_PROMOTED_THRESHOLD:
            break
        hint = _ERROR_CODE_HINTS.get(code,
                                      f"recurring; rustc --explain {code} for details.")
        # P6.3: mine common type tokens from the excerpts for this code.
        common = _mine_common_tokens(_ERROR_CODE_EXCERPTS.get(code, []))
        if common:
            shared = ", ".join(f"`{t}`" for t, _ in common[:3])
            promoted.append(
                f"  {code} (seen {count}×, often on {shared}): {hint}")
        else:
            promoted.append(f"  {code} (seen {count}×): {hint}")
        # P6.4: surface past notes tagged with this code.
        for past in code_to_notes.get(code, [])[:2]:
            past_short = past if len(past) <= 240 else past[:237] + "..."
            promoted.append(f"    ↳ past note: {past_short}")
    if not promoted:
        return ""
    return ("These errors keep recurring in this run; address them proactively:\n"
            + "\n".join(promoted))


# Repository Specifications (2026-06-11): the crate's spec-document set —
# functional requirements + genesis (prose + structured rules) + API surface
# — injected into EVERY code-producing prompt from t2 onward via the prompts
# runtime facade. Loaded here (I/O + mtime cache), rendered by the pure
# pipeline_core.prompts.repo_spec selector.
_REPO_SPEC_CACHE: dict[str, tuple[tuple, RepoSpecDocs]] = {}
_FR_PREFIX = "functional_requirements_"


def _strip_model_notes_section(fr_text: str) -> str:
    """Drop '## Model Notes …' from the FR copy used for repo-spec excerpts —
    the notes already reach prompts via the dedicated MODEL_NOTES filter."""
    idx = fr_text.find("## Model Notes")
    if idx == -1:
        return fr_text
    nxt = fr_text.find("\n## ", idx + 1)
    return fr_text[:idx] + (fr_text[nxt + 1:] if nxt != -1 else "")


def _load_repo_spec_docs(project_dir: Path, module_name: str) -> RepoSpecDocs:
    genesis_p = project_dir / "genesis.md"
    fr_p = project_dir / f"{_FR_PREFIX}{module_name}.md"
    api_p = _api_surface_path(project_dir, module_name)
    mtimes = tuple(p.stat().st_mtime if p.exists() else 0.0
                   for p in (genesis_p, fr_p, api_p))
    key = str(project_dir / module_name)
    cached = _REPO_SPEC_CACHE.get(key)
    if cached and cached[0] == mtimes:
        return cached[1]
    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except OSError:
            return ""
    docs = RepoSpecDocs(
        genesis_rules=genesis_rules_read(project_dir, module_scope=module_name or "*"),
        genesis=_read(genesis_p),
        functional_req=_strip_model_notes_section(_read(fr_p)),
        api_surface=_read(api_p),
    )
    _REPO_SPEC_CACHE[key] = (mtimes, docs)
    return docs


def _repo_spec_section(
    fr_path: "Path | None" = None, *, file_rel: str = "", fn_name: str = "",
    container: str = "", char_budget: "int | None" = None,
) -> str:
    """Provider behind prompts.runtime.repo_spec_section. Derives project_dir
    and module from fr_path (functional_requirements_{module}.md). Fail-safe:
    any problem → "" (prompt renders without the section)."""
    try:
        if fr_path is None:
            return ""
        fr_path = Path(fr_path)
        if not fr_path.name.startswith(_FR_PREFIX) or not fr_path.name.endswith(".md"):
            return ""
        module = fr_path.name[len(_FR_PREFIX):-3]
        docs = _load_repo_spec_docs(fr_path.parent, module)
        return render_repo_spec(
            docs, file_rel=file_rel,
            identifiers=identifiers_for_target(fn_name, container, file_rel),
            char_budget=char_budget or _REPO_SPEC_CAP,
        )
    except Exception:
        return ""


# Slice 8: the prompt builders moved to pipeline_core/prompts/ (pure
# text→text). Their impure collaborators stay here — the regex-based
# file-view annotator, the run-state promoted-error rules, and the
# repo-spec loader — and are wired into the prompts runtime facade once
# at import time.
_prompts_runtime.configure(
    annotate_file_view=_annotate_file_view,
    promoted_error_rules=_promoted_error_rules,
    repo_spec_section=_repo_spec_section,
)


def _build_dispatch_family_section(family: DispatchFamily) -> str:
    """Shim: render via pipeline_core.prompts with the Settings-derived cap."""
    return _build_dispatch_family_section_pure(
        family, single_call_char_limit=SINGLE_CALL_CHAR_LIMIT)


_GRACEFUL_SENTINEL_PATH = Path("/tmp/pipeline_graceful_stop")
_SHUTDOWN_SENTINEL_PATH = Path("/tmp/pipeline_shutdown_on_finish")
# P7.2: per-fn cargo-check inner loop is now governed by a failure-floor
# stall budget against rustc error count. Hard cap was a counter — the
# user's "no hard limits" principle replaced that with floor-stall as the
# sole non-success exit. The 10k backstop is now a runaway safety net only;
# `PHASE_C_FN_MAX_STALLS` is the real terminator. Legacy env vars
# `PHASE_C_FN_HARD_CAP` and `PHASE_C_FIX_CAP_PER_FN` still work for
# operators who want to clamp behaviour explicitly.
_PER_FN_FIX_HARD_CAP = int(os.getenv(
    "PHASE_C_FN_HARD_CAP",
    os.getenv("PHASE_C_FIX_CAP_PER_FN", "10000"),
))
_PER_FN_FIX_MAX_STALLS = int(os.getenv("PHASE_C_FN_MAX_STALLS", "3"))
_PER_FN_FIX_CAP = _PER_FN_FIX_HARD_CAP  # back-compat alias
# P1: per-pass test-fix loop now governed by a failure-floor stall budget,
# not a flat count. Iterate as long as the minimum failure count keeps
# decreasing. Break when N consecutive iterations make no new minimum, or
# when the absolute hard cap fires (runaway safety). `PHASE_C_FIX_CAP_PER_PASS`
# is the legacy alias for the hard cap; kept for backward compatibility.
_PER_PASS_TEST_FIX_MAX_STALLS = int(os.getenv("PHASE_C_TEST_FIX_MAX_STALLS", "3"))
# Hard cap raised to a backstop value (10k) — floor-stall is the real exit.
# Operators can clamp explicitly via env if needed.
_PER_PASS_TEST_FIX_HARD_CAP = int(os.getenv(
    "PHASE_C_TEST_FIX_HARD_CAP",
    os.getenv("PHASE_C_FIX_CAP_PER_PASS", "10000"),
))

# Pass-through / focus-convergence smoke mode. With PIPELINE_PASS_THROUGH=1 every
# phase-c stall budget collapses to PIPELINE_PASS_THROUGH_STALLS (default 1): each
# fn/impl/test-fix loop does a couple of attempts then reverts-to-stub and moves on,
# giving an end-to-end run for controller/routing telemetry without grinding to
# convergence. PIPELINE_CONVERGE_MODULE names ONE module exempt from the collapse —
# it keeps full budgets and converges for real (the deterministic R3/R9 baseline).
_PASS_THROUGH = os.getenv("PIPELINE_PASS_THROUGH", "0") == "1"
_PASS_THROUGH_STALLS = int(os.getenv("PIPELINE_PASS_THROUGH_STALLS", "1"))
_CONVERGE_MODULE = os.getenv("PIPELINE_CONVERGE_MODULE", "").strip()


def _effective_stalls(full_budget: int, module: str | None) -> int:
    """Stall budget for a phase-c loop, honoring pass-through/focus mode. Normal
    runs return full_budget unchanged. Under pass-through every module collapses to
    PIPELINE_PASS_THROUGH_STALLS EXCEPT the designated converge module, which keeps
    full_budget so it reaches real convergence."""
    if not _PASS_THROUGH:
        return full_budget
    if module and _CONVERGE_MODULE and module == _CONVERGE_MODULE:
        return full_budget
    return min(full_budget, _PASS_THROUGH_STALLS)


def _module_of(rel_file: str) -> str:
    """Rust module identity of a source file: 'src/ptr.rs' -> 'ptr'. This is the
    granularity PIPELINE_CONVERGE_MODULE names: phase-c fills operate per-file, so
    the converge exemption must key on the file under fill, not the crate dir."""
    return Path(rel_file).stem


def _converge_in_cluster(cluster: "BuildCluster | None") -> str | None:
    """_CONVERGE_MODULE if its source file is active (not gated) in this cluster,
    else None. Cluster-scoped loops (TDD / per-pass test-fix) run whole-cluster, so
    they keep full budget whenever the converge module's tests live in the cluster."""
    if not _CONVERGE_MODULE:
        return None
    gated = {_module_of(f) for f in (cluster.gated_files or [])} if cluster else set()
    return _CONVERGE_MODULE if _CONVERGE_MODULE not in gated else None

# P4: directory under each project workspace where Phase C test-fix
# iterations are archived for post-hoc retrospective. Per-iter file:
# tmp/{project}/.phase_c_logs/{cluster}/pass_{n}/fix_{iter}.txt
_PHASE_C_LOGS_DIRNAME = ".phase_c_logs"


# P7.1: shared improvement-driven loop primitive. Every fix/retry loop in
# the pipeline converges on the same shape — run a check, if it fails feed
# the output to a fix step, repeat while the failure metric keeps falling.
# Numeric caps survive only as runaway-safety hard ceilings; the real exit
# is "no new minimum for K consecutive iterations".
#
# Used by Phase C per-fn (L1), task3 inline retry (L5), generate_fix_loop
# (L6), task2 build retries (L4), Phase C pass loop (L2), task0c (L3).

def _improvement_loop(
    *,
    name: str,
    run_check,                    # () -> (ok: bool, output: str, metric: int)
    do_fix,                       # (output, history, *, retro: str) -> dict
    do_retro=None,                # optional (output, history) -> str — produces
                                  # strategy guidance shown to do_fix on stall
    max_stalls: int = 3,
    hard_cap: int = 10_000,       # backstop against runaway only; the real
                                  # terminator is the stall budget
    on_iter=None,                 # optional (record: dict) -> None — archival
) -> tuple[bool, str, list[dict]]:
    """Canonical pipeline fix-loop wrapper — adapts the run_check/do_fix/
    do_retro callback convention onto THE refine driver
    (`pipeline_core.application.run_refine_loop`, slice 8a f5), so outer
    measure loops share one termination policy with task0c augmentation.
    (`_drive_fix_loop` stays a sibling: inner compile loops have outcome
    kinds and a repeated-signature exit that don't fit a metric strategy.)

    Semantics: improvement = strict decrease of the best metric; exit on
    run_check ok, `{"abort": ...}` from do_fix, the stall budget, or the
    hard-cap backstop. Each attempt is RE-MEASURED immediately (run_check
    after do_fix) — the retired hand-rolled loop recorded the pre-fix
    metric one iteration late, which burned a spurious stall on iteration
    1 and exited a stalled loop without measuring its final fix
    (discarding a possible improvement). Fix-attempt counts per stall
    budget are unchanged.

    `do_fix(output, history, *, retro)` — `retro` carries
    `LoopState.context()` (trajectory + stall count + latest retro);
    legacy 2-arg callables are still accepted. `do_retro` fires before an
    attempt whenever the loop is stalled. Emits `loop.iter` per attempt;
    history records carry `metric` (pre-fix) and `post_fix_metric`
    (measured, no longer a hint).
    """
    import inspect as _inspect
    try:
        _accepts_retro = "retro" in _inspect.signature(do_fix).parameters
    except (TypeError, ValueError):
        _accepts_retro = False

    history: list[dict] = []

    class _CallbackStrategy:
        """RefineStrategy over the callback convention. Metric here is
        lower-is-better; LoopState is higher-is-better, so the negated
        metric flows through the driver."""

        def __init__(self) -> None:
            self.ok = False
            self.aborted = False
            self.output = ""
            self.metric = 0
            self.iter_n = 0

        def _check(self) -> None:
            self.ok, self.output, self.metric = run_check()

        def measure(self) -> float:
            self._check()
            return float(-self.metric)

        def is_done(self, metric: float) -> bool:
            return self.ok or self.aborted

        def retrospect(self, state: "_LoopState") -> str:
            if do_retro is None:
                return ""
            try:
                return do_retro(self.output, history) or ""
            except Exception as e:
                _log(f"  [improvement_loop/{name}] do_retro failed: {e}")
                return ""

        def attempt(self, state: "_LoopState") -> _Attempt:
            fix_guidance = state.context()
            retro = state.pending_retro
            pre_metric = self.metric
            output = self.output
            self.iter_n += 1
            try:
                if _accepts_retro:
                    record = do_fix(output, history, retro=fix_guidance) or {}
                else:
                    record = do_fix(output, history) or {}
            except Exception as e:
                _log(f"  [improvement_loop/{name}] do_fix raised: {e}")
                record = {"abort": True, "reason": f"do_fix_exception: {e}"}
            if record.get("abort"):
                self.aborted = True
                new_metric = pre_metric          # no re-measure on abort
            else:
                self._check()                    # post-fix truth
                new_metric = self.metric
            improved = float(-new_metric) > state.best_metric
            ev = {
                "name": name,
                "iter": self.iter_n,
                "metric_prev": pre_metric,
                "metric_new": new_metric,
                "metric_best": int(min(-state.best_metric, float(new_metric))),
                "improved": improved,
                "stalled": 0 if improved else state.stalls + 1,
                "fix_summary": record.get("summary", ""),
                "retro_chars": len(fix_guidance),
            }
            with suppress(Exception):
                _emit("loop.iter", data=ev)
            if on_iter is not None:
                try:
                    on_iter(ev | {"output": output, "record": record,
                                  "retro": retro})
                except Exception as e:
                    _log(f"  [improvement_loop/{name}] on_iter raised: {e}")
            history.append({"iter": self.iter_n, **record, "retro": retro,
                            "metric": pre_metric, "post_fix_metric": new_metric})
            if self.aborted:
                _log(f"  [improvement_loop/{name}] aborted at iter "
                     f"{self.iter_n}: {record.get('reason', 'unknown')}")
            return _Attempt(metric=float(-new_metric), error=output[:2000],
                            retro=retro)

    strat = _CallbackStrategy()
    state = _run_refine_loop(strat, max_stalls=max_stalls, hard_cap=hard_cap)
    if strat.ok:
        return True, strat.output, history
    if not strat.aborted:
        if state.stalls >= max_stalls:
            _log(f"  [improvement_loop/{name}] floor stalled at "
                 f"{int(-state.best_metric)} for {state.stalls} iter(s) — stopping.")
        else:
            _log(f"  [improvement_loop/{name}] hard cap {hard_cap} reached "
                 "(should never happen — stall budget should fire first).")
    return False, strat.output, history


# Retro/diagnosis calls MUST run with think=True (FR-028). The served
# Qwen3.6 model (vLLM + --reasoning-parser qwen3) returns EMPTY content under
# think=False — its hybrid-thinking path never populates `content`. Fill calls
# already use think=True and work. think=True was once dropped pipeline-wide for
# context-budget reasons, but that was the large fill prompts; retro INPUT is
# tiny, so we cap OUTPUT tokens instead of disabling thinking. The cap must be
# generous enough for reasoning + the 2-line answer to both emit (if reasoning
# alone exhausts the budget, `content` is truncated to empty again).
_RETRO_MAX_TOKENS = 6144


def _ask_retrospective(prompt: str, model: str | None = None) -> str:
    """Issue a retro/diagnosis prompt with think=True and a bounded output
    budget. One retry when the first response is empty (transient empty-content
    from the reasoning endpoint). Returns "" if both attempts come back empty.
    Raising callers keep their own try/except for true LLM exceptions."""
    result = ""
    for _attempt in range(2):
        result = ask(prompt, model=model or _TASK2_MODEL,
                     think=True, max_tokens=_RETRO_MAX_TOKENS)
        if (result or "").strip():
            return result
    return result or ""


def _whole_tree_retrospective(
    error_excerpt: str,
    history: "list[dict]",
    *,
    name: str = "fix_loop",
) -> str:
    """Strategy retrospective for whole-tree fix loops. Mirrors the per-fn
    `_phase_c_retrospective` but at the tree-wide compile-error level.

    Called by `_improvement_loop` when the prior iter did not reduce the
    error floor. Asks the model: "you tried X and the error count stayed
    the same — what categorically different approach should the next
    attempt take?". Returns a short guidance string the next `do_fix`
    closure injects into its prompt.

    Empty string on transient failures (LLM exception, empty response).
    """
    if not error_excerpt.strip():
        return ""
    # Compact history view — the last 3 attempts' summaries and the error
    # signature they failed against.
    recent = history[-3:] if history else []
    history_block = ""
    for h in recent:
        summary = h.get("summary") or h.get("fix_summary") or ""
        err = (h.get("error_sig") or "")[:600]
        history_block += (
            f"\nIter {h.get('iter','?')}: {summary}\n"
            f"  metric_before={h.get('metric','?')} → "
            f"metric_after={h.get('post_fix_metric','?')}\n"
            f"  error_sig: {err}\n"
        )
    prior_retro = ""
    for h in reversed(recent):
        if h.get("retro"):
            prior_retro = h["retro"]
            break
    prior_block = (
        f"\nPrior retrospective said:\n{prior_retro}\n"
        "If that guidance did not move the error floor, escalate to a\n"
        "DIFFERENT strategy this time.\n"
    ) if prior_retro else ""
    prompt = (
        f"You are diagnosing why fix-loop `{name}` has stopped reducing\n"
        f"the rustc error count across multiple attempts.\n\n"
        f"Recent attempts:\n{history_block}\n"
        f"Current error excerpt:\n```\n{error_excerpt[:_FIX_ERR_CAP]}\n```\n"
        f"{prior_block}\n"
        "Output EXACTLY two lines:\n"
        "  CATEGORY: <one short tag — e.g. WRONG_FILE_TARGETED, "
        "TRAIT_HEADER_MISMATCH, UNSIZED_RETURN, DUPLICATE_IMPL, "
        "GENUINELY_HARD>\n"
        "  GUIDANCE: <one paragraph — what concretely different approach to "
        "try next iter. Reference specific symbols, fns, or rustc error codes.>\n\n"
        "GUIDANCE must be actionable, not 'try harder'. If two iters in a row\n"
        "with non-trivial strategy changes both failed to move the floor,\n"
        "set CATEGORY: GENUINELY_HARD."
    )
    try:
        result = _ask_retrospective(prompt)
    except Exception as e:
        _log(f"  [retro/{name}] LLM call failed: {e}")
        return ""
    if not (result or "").strip():
        _log(f"  [retro/{name}] LLM returned empty content")
        return ""
    # Lightweight parse: collect everything after "GUIDANCE:" up to next
    # uppercase-tag-style label. Tolerant of decorations.
    cat = ""
    guidance_lines: list[str] = []
    in_guidance = False
    for ln in (result or "").splitlines():
        s = ln.strip().lstrip("*-• ").strip()
        s = re.sub(r"^\*+\s*|\s*\*+$", "", s)
        m = re.match(r"(?i)^category\s*[:=]\s*(\S+)", s)
        if m:
            cat = m.group(1).strip().rstrip(".,;").upper()
            in_guidance = False
            continue
        m = re.match(r"(?i)^guidance\s*[:=]\s*(.+)$", s)
        if m:
            guidance_lines = [m.group(1).strip()]
            in_guidance = True
            continue
        if in_guidance and s:
            if re.match(r"(?i)^(category|guidance|note)\s*[:=]", s):
                in_guidance = False
            else:
                guidance_lines.append(s)
    guidance = " ".join(guidance_lines).strip()
    if not cat and not guidance:
        guidance = (result or "").strip()[:1200]
    if cat:
        return f"[{cat}] {guidance}" if guidance else f"[{cat}]"
    return guidance


def _write_phase_c_test_fix_log(
    project_dir: Path,
    cluster_name: str,
    pass_n: int,
    fix_iter: int,
    test_out: str,
    failed_tests: list[str],
    fns_retried: list[tuple[str, str]],
    cur_fail_count: int,
    new_fail_count: int,
    best_fail_count: int,
    stall: int,
) -> None:
    """Archive one test-fix iteration to disk for post-hoc analysis (P4).
    Failure to write is non-fatal: this is observability, not control flow.
    """
    try:
        out_dir = (project_dir / _PHASE_C_LOGS_DIRNAME
                   / cluster_name / f"pass_{pass_n}")
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / f"fix_{fix_iter:02d}.txt"
        header = (
            f"# Phase C test-fix iteration\n"
            f"cluster: {cluster_name}\n"
            f"pass:    {pass_n}\n"
            f"iter:    {fix_iter}\n"
            f"prev_fail_count: {cur_fail_count}\n"
            f"new_fail_count:  {new_fail_count}\n"
            f"best_fail_count: {best_fail_count}\n"
            f"stalled:         {stall}\n"
            f"failed_tests:\n"
            + "".join(f"  - {t}\n" for t in failed_tests)
            + "fns_retried:\n"
            + "".join(f"  - {r}::{f}\n" for r, f in fns_retried)
            + "\n=== cargo test output ===\n"
        )
        fp.write_text(header + test_out, encoding="utf-8")
    except Exception as e:
        _log(f"  [phase-c/per-fn] failed to write test-fix log: {e}")


# P3: classification step run before each post-stall test-fix iteration.
# A single LLM call labels each failing test into a small fixed taxonomy so
# the loop can target the right fn(s) instead of bluntly retrying everything
# that was filled this pass.

_PHASE_C_CLASSIFIER_CATEGORIES = (
    "BAD_FILL", "TEST_PRECONDITION_MISSING",
    "CFG_BOUNDARY", "PANIC_OR_TIMEOUT", "ENV_DEPENDENT",
)


def _classify_phase_c_test_failures(
    test_out: str,
    filled_this_pass: list[tuple[str, str]],
    failed_tests: list[str],
    cluster_name: str,
) -> dict[str, dict]:
    """Ask the LLM to classify each failing test. Returns
        {test_name: {"category": str, "fn": "file::name" | "", "reason": str}}.
    Empty dict on failure / no failing tests / classifier disabled.

    This is a single short LLM call; one inference for the whole set, not
    one per test. Output schema is strictly enforced by line-level parsing.
    Unknown categories or unparseable lines are dropped (silently — we'd
    rather fall back to the naive "retry everything" path than block the loop).
    """
    if not failed_tests:
        return {}
    if os.getenv("PHASE_C_TEST_FIX_USE_CLASSIFIER", "1") == "0":
        return {}

    # Budget the test_out excerpt: classifier needs to see error blocks but
    # has no need for the full multi-MB cargo output. Ctx-budget-aware (scales
    # with the model's context, floored for small windows) — not a frozen literal.
    excerpt = test_out[-_FIX_ERR_CAP:] if len(test_out) > _FIX_ERR_CAP else test_out

    filled_lines = "\n".join(f"  - {r}::{f}" for r, f in filled_this_pass[:60])
    failing_lines = "\n".join(f"  - {t}" for t in failed_tests[:30])

    prompt = (
        f"You are diagnosing a Rust test failure in cluster `{cluster_name}` "
        f"of a source-reconstruction pipeline. The fns listed below were filled "
        f"this pass; the tests listed below failed. For each failing test, "
        f"classify the most likely cause and (when applicable) name the fn that "
        f"should be re-implemented.\n\n"
        f"Categories:\n"
        f"  BAD_FILL                  — a fn just filled this pass has a wrong "
        f"body; name the fn (file::name).\n"
        f"  TEST_PRECONDITION_MISSING — the failing test depends on a DIFFERENT "
        f"fn that hasn't been filled correctly yet; name the suspect fn if you "
        f"can identify it from the error.\n"
        f"  CFG_BOUNDARY              — the failure points at code excluded "
        f"from this cluster's cfg-set (different cluster owns it).\n"
        f"  PANIC_OR_TIMEOUT          — the test compiled but panicked at runtime, "
        f"or timed out. Include the panic site/frame if visible.\n"
        f"  ENV_DEPENDENT             — failure stems from environment (missing "
        f"file, env var, network), not the fill body.\n\n"
        f"FNs filled this pass:\n{filled_lines}\n\n"
        f"Failing tests:\n{failing_lines}\n\n"
        f"Cargo test output (tail):\n```\n{excerpt}\n```\n\n"
        f"Output one line PER failing test, EXACT format:\n"
        f"  TEST <name>: <CATEGORY> [fn=<file::name>] — <one-sentence reason>\n"
        f"The fn= field is required for BAD_FILL and TEST_PRECONDITION_MISSING; "
        f"omit the [...] block for the other categories.\n"
        f"Do not output anything else."
    )

    try:
        # Use global MAX_TOKENS default — hardcoded caps risked content
        # truncation under tight thinking-budget conditions.
        result = ask(prompt, model=_TASK0C_MODEL, think=None)
    except Exception as e:
        _log(f"  [phase-c/classifier] LLM call failed: {e}")
        return {}

    out: dict[str, dict] = {}
    line_re = re.compile(
        r"^\s*TEST\s+(?P<test>\S+?)\s*:\s*"
        r"(?P<cat>[A-Z_]+)\s*"
        r"(?:\[fn=(?P<fn>[^\]]+)\])?\s*"
        r"(?:—|-)\s*"
        r"(?P<reason>.+?)\s*$"
    )
    for ln in result.splitlines():
        m = line_re.match(ln)
        if not m:
            continue
        cat = m.group("cat")
        if cat not in _PHASE_C_CLASSIFIER_CATEGORIES:
            continue
        out[m.group("test")] = {
            "category": cat,
            "fn": (m.group("fn") or "").strip(),
            "reason": m.group("reason").strip(),
        }
    return out


def _check_graceful_stop(context: str) -> None:
    """Exit cleanly when the graceful stop sentinel is set. Called from inside
    long-running loops so `bash continue.sh --graceful` is responsive."""
    if _GRACEFUL_SENTINEL_PATH.exists():
        with suppress(OSError):
            _GRACEFUL_SENTINEL_PATH.unlink()
        _log(f"  [graceful] stop sentinel detected at {context} — exiting cleanly.")
        sys.exit(0)


def _cargo_err_sig(output: str) -> frozenset:
    """LEGACY regex-based fallback. Prefer `_cargo_diagnostics_from_json`
    (the JSON-format parse) when cargo is invoked with
    `--message-format=json-diagnostic-rendered-ansi` — the regex path
    misses non-E-coded failures (linker, build-script, cargo wrapper) and
    is the source of the recurring "compile failed but errs=0" false
    positive.

    This wrapper inspects `output` for JSON `compiler-message` lines and
    routes to the structured path when present. Otherwise falls back to
    the regex over rendered text (with a guard so the rc-honest counter
    can be derived by the caller).
    """
    diags = _cargo_diagnostics_from_json(output)
    if diags is not None:
        return _cargo_err_sig_from_diagnostics(diags)
    return (
        frozenset(re.findall(r"error\[E\d+\]: .+", output))
        | frozenset(re.findall(r"^error: .+", output, re.MULTILINE))
    )


def _cargo_diagnostics_from_json(output: str) -> "list[Diagnostic] | None":
    """Walk `output` line-by-line, parse JSON cargo messages, return the
    `compiler-message`s with `level == "error"` as structured records:

        [{"code": "E0277" | "", "file": "src/error.rs" | "",
          "line": int | 0, "message": str, "rendered": str}, …]

    Returns None if the output contains no JSON cargo messages — caller
    falls back to regex. Empty list means "JSON present, zero errors"
    (an actual signal: a JSON cargo check that returned cleanly).
    """
    json_seen = False
    out: list[Diagnostic] = []
    for ln in output.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if obj.get("reason") != "compiler-message":
            json_seen = True  # JSON present even if not an error
            continue
        msg = obj.get("message") or {}
        if msg.get("level") != "error":
            json_seen = True
            continue
        json_seen = True
        code_field = msg.get("code") or {}
        code = (code_field.get("code") or "") if isinstance(code_field, dict) else ""
        spans = msg.get("spans") or []
        primary = next((s for s in spans if s.get("is_primary")), spans[0] if spans else {})
        out.append(Diagnostic(
            code=code,
            file=(primary.get("file_name") or "") if isinstance(primary, dict) else "",
            line=int(primary.get("line_start") or 0) if isinstance(primary, dict) else 0,
            message=msg.get("message") or "",
            rendered=msg.get("rendered") or "",
        ))
    return out if json_seen else None


def _cargo_err_sig_from_diagnostics(diagnostics: "list[Diagnostic]") -> frozenset:
    """Build the stable error signature directly from structured
    diagnostics. Sig is `code:message` (or just `message` when no code) —
    same disambiguation power as the regex sig but produced from
    rustc's own classification rather than guessed from rendered text."""
    sigs: set[str] = set()
    for d in diagnostics:
        code = d.code or ""
        msg = (d.message or "").strip()
        sigs.add(f"{code}:{msg}" if code else msg)
    return frozenset(sigs)


def _run_cargo_check_diag(
    cmd: str,
    cwd: Path,
    env: dict,
    timeout: int = 180,
) -> "tuple[int, str, list[Diagnostic]]":
    """Run a cargo check / build / test command with
    `--message-format=json-diagnostic-rendered-ansi` so the caller gets
    BOTH a rendered text view (for prompts / logs) AND structured
    diagnostics (for error counting / file attribution). The flag is
    appended automatically when the command doesn't already include
    `--message-format`.

    Returns (rc, rendered_text, diagnostics). Diagnostics is the list
    returned by `_cargo_diagnostics_from_json` over the JSON output, or
    [] if cargo emitted no compiler messages. The rendered text is
    reconstructed from the JSON `message.rendered` fields plus any
    non-JSON lines, so it mirrors what a plain `cargo check` would print
    without --message-format.

    Use this in place of `_run_cargo` when the caller needs to count or
    locate errors — it eliminates the regex false-positive class.
    """
    argv = shlex.split(cmd)
    if not any(a.startswith("--message-format") for a in argv):
        # Insert after the cargo SUBCOMMAND (check / test / build / etc.),
        # not after `cargo` itself — the cmd may be `cargo +nightly check`
        # (3 tokens) or `cargo check` (2 tokens). Find the first token
        # that is a known subcommand name and insert right after it.
        _known_subs = {"check", "test", "build", "run", "doc", "clippy"}
        _insert_at = len(argv)  # fallback: append at end
        for i, a in enumerate(argv):
            if a in _known_subs:
                _insert_at = i + 1
                break
        argv.insert(_insert_at,
                    "--message-format=json-diagnostic-rendered-ansi")
    try:
        r = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        raw = (r.stdout or "") + ("\n--- stderr ---\n" + r.stderr if r.stderr else "")
        diagnostics = _cargo_diagnostics_from_json(raw) or []
        # Reconstruct a rendered text view: prefer message.rendered when
        # we have it (it's the same string cargo would have printed),
        # plus any non-JSON lines from stderr (linker errors, etc.).
        rendered_parts: list[str] = []
        for ln in raw.splitlines():
            s = ln.strip()
            if s.startswith("{"):
                continue
            rendered_parts.append(ln)
        for d in diagnostics:
            r_text = d.rendered or ""
            if r_text:
                rendered_parts.append(r_text)
        rendered = "\n".join(rendered_parts)
        return r.returncode, rendered, diagnostics
    except subprocess.TimeoutExpired:
        return -1, f"TIMEOUT after {timeout}s", []
    except FileNotFoundError as e:
        return -1, f"FILE NOT FOUND: {e}", []


def _strip_cargo_warnings(output: str) -> str:
    """Drop `warning:` blocks from cargo output. A block runs from the
    `warning:` header until the next blank line (rustc separates each block
    with a blank line). Used as a defensive filter — the model should never
    see warnings, only errors.
    """
    out: list[str] = []
    skip = False
    for ln in output.splitlines():
        if ln.startswith("warning:") or ln.startswith("warning["):
            skip = True
            continue
        if skip:
            if ln.strip() == "":
                # blank line terminates the warning block
                skip = False
            # while skipping, drop every non-blank line of the block
            continue
        out.append(ln)
    return "\n".join(out)


def _focus_cargo_errors(output: str, file_rel: str, max_chars: int = _FIX_ERR_CAP) -> str:
    """Extract a focused error excerpt from cargo output.

    P3.7 tiered output. Two groups of error blocks:
      - In-target-file: blocks whose `-->` mentions `file_rel`'s basename
      - Cross-file: blocks whose `-->` mentions any other .rs file
    Returned with a header demarcating the two. Budget split 60/40 in favour
    of in-target. Warnings dropped first.

    A bad fill in error.rs can break compilation in lib.rs (downstream
    consumer of error.rs). Without the cross-file slice, the model would
    not see the real symptom — only the absence of expected errors in its
    own file.
    """
    output = _strip_cargo_warnings(output)
    base = file_rel.rsplit("/", 1)[-1]
    in_blocks: list[list[str]] = []
    cross_blocks: list[list[str]] = []
    in_err_block = False
    cur_block: list[str] = []
    has_target = False
    has_any_loc = False
    for ln in output.splitlines():
        if ln.startswith("error[") or ln.startswith("error: ") or re.match(r"^error\b", ln):
            if in_err_block and cur_block:
                if has_target:
                    in_blocks.append(cur_block)
                elif has_any_loc:
                    cross_blocks.append(cur_block)
            cur_block = [ln]
            in_err_block = True
            has_target = False
            has_any_loc = False
        elif in_err_block:
            cur_block.append(ln)
            if base in ln:
                has_target = True
            if re.search(r"\.rs:\d+", ln):
                has_any_loc = True
            if ln.strip() == "" and cur_block:
                if has_target:
                    in_blocks.append(cur_block)
                elif has_any_loc:
                    cross_blocks.append(cur_block)
                cur_block = []
                in_err_block = False
                has_target = False
                has_any_loc = False
    if in_err_block and cur_block:
        if has_target:
            in_blocks.append(cur_block)
        elif has_any_loc:
            cross_blocks.append(cur_block)

    if not in_blocks and not cross_blocks:
        return output[:max_chars]

    in_budget = int(max_chars * 0.6)
    cross_budget = max_chars - in_budget
    parts: list[str] = []
    if in_blocks:
        parts.append("# Errors in target file:")
        used = 0
        for b in in_blocks:
            txt = "\n".join(b)
            if used + len(txt) + 1 > in_budget:
                break
            parts.append(txt)
            used += len(txt) + 1
    if cross_blocks:
        parts.append("# Errors elsewhere in the crate (likely downstream of the target):")
        used = 0
        for b in cross_blocks:
            txt = "\n".join(b)
            if used + len(txt) + 1 > cross_budget:
                break
            parts.append(txt)
            used += len(txt) + 1
    return "\n".join(parts)[:max_chars]




def _run_cargo(cmd: str, cwd: Path, env: dict, timeout: int = 180) -> tuple[int, str]:
    """Run a cargo command and return (rc, combined_output). rc=-1 on timeout.

    slice 5a: delegates to the Cargo adapter. rc=-1 (timeout / binary-not-found) keeps the
    bare message in stderr (parity with the legacy form); otherwise the stdout+stderr-block
    combined layout.
    """
    p = ADAPTERS.cargo.run(cmd, cwd, env=env, timeout=timeout)
    if p.returncode == -1:
        return -1, p.stderr
    return p.returncode, p.combined


# FR-024: every cargo test invocation must be wrapped in an improvement-driven
# loop. These helpers provide the metric (pass/fail counts), the attribution
# fallbacks (recently-filled queue, test-reverse map), and the stall budget.

def _build_test_reverse_map(
    test_files: list[Path],
    src_dir: Path,
) -> dict[str, set[tuple[str, str]]]:
    """Return {test_name: set[(file_rel, fn_name)]} — for each #[test] fn in
    the test files, the set of fn records in `src_dir` whose names appear
    textually in the test's body. Best-effort static analysis used as the
    last-resort attribution path when panics/compile errors/classifier
    all fail to identify a fn to fix.

    Coarse: matches any identifier whose name equals a src fn name, even
    if it's a comment or string. Acceptable — over-attribution just
    queues a few extra fns for refill; the per-fn fix loop will skip
    fns that are already filled correctly.
    """
    fn_index = _enumerate_fns_via_extractor(src_dir) or []
    if not fn_index:
        return {}
    # name → list[(file, fn_name)] (a name may map to multiple impls)
    by_name: dict[str, list[tuple[str, str]]] = {}
    for r in fn_index:
        name = r.get("fn_name", "")
        f = r.get("file", "")
        if name and f:
            by_name.setdefault(name, []).append((f, name))
    out: dict[str, set[tuple[str, str]]] = {}
    test_fn_re = re.compile(
        r"#\s*\[\s*test\s*\][^\n]*\n[^\n]*?fn\s+(\w+)\s*\(",
    )
    for tp in test_files:
        try:
            text = tp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        ids = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", text))
        # Match each #[test] fn's name; pair it with all fn records whose
        # identifier appears anywhere in the test file. This is file-level
        # not fn-body-level (cheaper, captures helper fns the test calls).
        for m in test_fn_re.finditer(text):
            test_name = m.group(1)
            candidates: set[tuple[str, str]] = set()
            for ident in ids:
                if ident in by_name:
                    candidates.update(by_name[ident])
            out[test_name] = candidates
    return out


def _attribute_failures(
    test_output: str,
    src_dir: Path,
    recently_filled: list[tuple[str, str]],
    test_reverse_map: dict[str, set[tuple[str, str]]],
    cluster_name: str,
) -> tuple[Frontier, str]:
    """Robust failure-to-fn attribution chain. Returns (frontier, kind).
    Tries each path until one yields a non-empty frontier:
      1. Runtime panics (unimplemented stubs) — `_extract_tdd_frontier`
      2. Compile errors — `_extract_tdd_compile_frontier`
      3. LLM classifier — `_classify_phase_c_test_failures` (P4.5)
      4. Recently-filled fallback — most recent fills are likely culprits
         for new assertion failures
      5. Test-reverse-map fallback — every fn the failing tests could reach

    Empty frontier means all paths exhausted — caller should let the
    stall budget exit the loop. Kind tells the caller which path produced
    the frontier (for logs/events).
    """
    # 1. Runtime panics.
    frontier = _extract_tdd_frontier(test_output, src_dir)
    if frontier:
        return frontier, "runtime"
    # 2. Compile errors.
    frontier = _extract_tdd_compile_frontier(test_output, src_dir)
    if frontier:
        return frontier, "compile"
    # 3. LLM classifier — turn off via PHASE_C_TDD_USE_CLASSIFIER=0.
    if os.getenv("PHASE_C_TDD_USE_CLASSIFIER", "1") != "0":
        failing = sorted(_parse_failed_tests(test_output))
        if failing:
            classification = _classify_phase_c_test_failures(
                test_out=test_output, filled_this_pass=[],
                failed_tests=failing, cluster_name=cluster_name,
            )
            synth: Frontier = {}
            for tname, info in classification.items():
                if info.get("category") not in (
                        "BAD_FILL", "TEST_PRECONDITION_MISSING"):
                    continue
                ref = info.get("fn") or ""
                if "::" in ref:
                    fp, fn = ref.split("::", 1)
                    key = (fp.strip(), fn.strip())
                    if key[0] and key[1]:
                        synth.setdefault(key, set()).add(tname)
            if synth:
                return synth, "classifier"
    # 4. Recently-filled fallback. If new assertion failures appeared
    # after recent fills, the recent fills are the most likely cause.
    # Queue them in reverse-filled order (newest first).
    if recently_filled:
        failing = sorted(_parse_failed_tests(test_output))
        if failing:
            recent_synth: Frontier = {}
            for key in reversed(recently_filled):
                recent_synth.setdefault(key, set()).update(failing)
            return recent_synth, "recently_filled"
    # 5. Test-reverse-map. For each failing test, queue every fn it
    # could reach according to the static reverse map. Over-attribution
    # is tolerable here — per-fn splice guards reject overwriting
    # non-stub bodies, so already-correct fills aren't clobbered.
    if test_reverse_map:
        failing = sorted(_parse_failed_tests(test_output))
        if failing:
            reverse_synth: Frontier = {}
            for tname in failing:
                # Strip module prefix if cargo formats as `module::test_name`.
                bare = tname.split("::")[-1]
                cands = test_reverse_map.get(bare) or test_reverse_map.get(tname) or set()
                for key in cands:
                    reverse_synth.setdefault(key, set()).add(tname)
            if reverse_synth:
                return reverse_synth, "reverse_map"
    return {}, "no_frontier"


def _run_scoped_tests(
    test_cmd: str, cwd: Path, env: dict, *, timeout: int = 120,
) -> "tuple[bool, int, str, set[str]]":
    """Plan 10 U1 — reusable test-validation step (the runtime analog of a
    compile check). Runs `test_cmd` (scoped to the relevant test stems via
    _build_test_cmd) and returns (passed, fail_count, detail, failed_names).

    `passed` is True only when cargo reports rc==0 with zero failing tests.
    `detail` is the failing-test output tail (panic / assertion / expected-vs-
    actual) for feeding back into the next generation prompt. `failed_names` is
    the set of failing test names (same format as _parse_failed_tests) so a
    caller can check whether a SPECIFIC unit's tests pass. On timeout the caller
    gets (False, -1, ..., set()) and should treat it as INCONCLUSIVE — fall back
    to compile-based acceptance rather than block. Single place the pipeline
    runs tests, so every loop validates identically.
    """
    rc, out = _run_cargo(test_cmd, cwd, env, timeout=timeout)
    if rc == -1:
        return False, -1, "scoped test run timed out", set()
    failed = _parse_failed_tests(out)
    passed = (rc == 0 and len(failed) == 0)
    detail = "" if passed else out[-_FIX_ERR_CAP:]
    return passed, len(failed), detail, failed


def _u2_test_gate(
    fn_tests: "set[str]", test_cmd: "str | None",
    target_src_dir: Path, cluster_env: "dict | None",
) -> "tuple[frozenset, str] | None":
    """Plan 10 — the SINGLE reusable test-acceptance gate, called by every fill
    loop (per-fn, per-impl, post-merge) at the point a unit compiles.

    Given the test names the unit must make pass (`fn_tests`), run them via the
    shared `_run_scoped_tests` and decide:
      - return None  → ACCEPT (tests pass, or U2 off, or no tests, or the test
        run was inconclusive — never block on uncertainty).
      - return (fail_sig, detail) → DO NOT ACCEPT: the unit compiled but its own
        tests still fail. `fail_sig` is the failing-test set (use as the stall
        signature so the fix loop terminates), `detail` is the assertion / panic
        text to feed back into the next prompt.
    Fail-safe: any error → None (fall back to compile-based acceptance).
    """
    if not (_UNIFIED_FILL_LOOP and test_cmd and fn_tests):
        return None
    try:
        _passed, _fc, _detail, _failed = _run_scoped_tests(
            test_cmd, target_src_dir, cluster_env or {}, timeout=_U2_TEST_TIMEOUT)
    except Exception as _e:
        _guard("phase-c/u2_skip", f"{type(_e).__name__}: {_e}")
        return None
    _still = set(fn_tests) & _failed
    if not _still:
        return None
    _msg = (f"This compiled, but these tests still FAIL: {sorted(_still)}\n\n"
            "Fix the BEHAVIOR so they pass.\n" + _detail)
    return frozenset(_still), _msg


def _failure_hit_unfilled_stub(detail: str) -> bool:
    """True when a test FAILURE is a panic from a still-unfilled `unimplemented!()`
    / `todo!()` stub rather than a real assertion mismatch.

    Used by the per-fn fill loop (#3): a multi-fn test attributed to fn A can fail
    because a SIBLING fn it also calls is still a stub — not because A is wrong.
    Runtime panic attribution is first-panic-order, so we can't tell from the
    frontier alone; the panic MESSAGE is the reliable signal. The fn just filled
    is non-stub (splice rejects stub bodies), so a "not implemented" panic here
    came from another, still-unfilled fn → keep A's compiling body and defer the
    verdict to the outer TDD loop. An assertion mismatch means A is wrong → don't.
    """
    d = detail or ""
    return ("not implemented" in d) or ("not yet implemented" in d)


def _build_test_cmd(test_stems: list[str], cluster: "BuildCluster | None") -> str:
    """Build `cargo test --no-fail-fast --test stem1 --test stem2 ...` honoring
    the cluster toolchain."""
    parts = ["cargo"]
    if cluster and cluster.toolchain:
        parts.append(f"+{cluster.toolchain}")
    parts += ["test", "--no-fail-fast"]
    for s in test_stems:
        parts += ["--test", s]
    return shlex.join(parts)


def _parse_failed_tests(output: str) -> set[str]:
    """Extract names of failed test fns from cargo test output.

    Matches lines like:
        test foo::bar ... FAILED
        ---- foo::bar stdout ----
    Returns set of unqualified-or-qualified test fn names ('foo::bar').
    """
    failed: set[str] = set()
    for ln in output.splitlines():
        m = re.match(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", ln)
        if m:
            failed.add(m.group(1))
        m = re.match(r"^----\s+(\S+)\s+stdout\s+----", ln)
        if m:
            failed.add(m.group(1))
    return failed


def _ask_per_fn_body(
    prompt: str, fn_name: str, attempts_log: list[str],
    sig_text: str = "",
) -> tuple[str | None, str]:
    """Run the 3-attempt response-extraction ladder for a per-fn prompt.

    Returns (body or None, last_response). On None, the model's response was
    unparseable or stub-only on all three tries.

    For `unsafe` fns (rigid pointer/vtable manipulation), drops sampling
    temperature to PHASE_C_UNSAFE_TEMP (default 0.1). High-temperature
    creative sampling is the wrong mode for unsafe code — there's
    typically one correct token sequence per body, not a range of valid
    creative choices. The Qwen3.6 model documentation explicitly
    recommends lowering temperature for syntactically rigid code.
    """
    # Detect unsafe-fn context from the signature.
    is_unsafe = bool(
        sig_text and re.search(r"\bunsafe\s+fn\b", sig_text)
    )
    unsafe_temp = float(os.getenv("PHASE_C_UNSAFE_TEMP", "0.1"))
    last_response = ""
    for think_off, label in [(False, "default"), (True, "think=False"),
                             (True, "think=False+retry")]:
        kwargs: dict = {"model": _TASK2_MODEL}
        if think_off:
            kwargs["think"] = False
        if is_unsafe:
            kwargs["temperature"] = unsafe_temp
        last_response = ask(prompt, **kwargs)
        body = _extract_body_response(last_response)
        if body and not _is_unimplemented_body(body):
            if label != "default":
                attempts_log.append(f"{fn_name}: recovered via {label}")
            return body, last_response
        attempts_log.append(f"{fn_name}: empty/stub from {label}")
    return None, last_response


def _collect_sibling_filled_bodies(
    file_text: str, target_fn_name: str, target_container: str,
    max_siblings: int = 3, max_body_chars: int = 800,
) -> str:
    """FR-024 fix 3: hoist already-filled sibling-impl bodies to the prompt.

    Finds every fn with the same `target_fn_name` in `file_text` whose body
    is NOT a stub, EXCLUDING the target itself (matched by container).
    Returns a formatted block ready for `<SIBLING_REFERENCE>` injection.
    Empty string when no usable siblings exist.

    The model is much more likely to produce a working body for an
    overloaded `fn as_ref` if a sibling `as_ref` impl in the same file
    is already correct — it can pattern-match the structural template.
    """
    out: list[str] = []
    count = 0
    for m in _FN_RE_GLOBAL.finditer(file_text):
        if m.group(2) != target_fn_name:
            continue
        sig_start = m.start()
        body_start = m.end()
        depth = 1
        i = body_start
        while i < len(file_text) and depth > 0:
            ch = file_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        body_end = i - 1
        body = file_text[body_start:body_end].strip()
        if not body or _is_unimplemented_body(body):
            continue
        # Skip the target fn itself — heuristic by container substring match
        # since we don't have AST nodes here. The container fragment usually
        # appears in the ~200 chars above the fn declaration.
        if target_container:
            context_above = file_text[max(0, sig_start - 200):sig_start]
            if target_container.strip() in context_above:
                # Likely the target — skip.
                continue
        sig = file_text[sig_start:body_start].strip().rstrip("{").rstrip()
        if len(body) > max_body_chars:
            body = body[:max_body_chars] + "\n    /* ...truncated... */"
        out.append(f"{sig} {{\n{body}\n}}")
        count += 1
        if count >= max_siblings:
            break
    return "\n\n".join(out)


def _retrospectives_path(project_dir: Path) -> Path:
    """Path to the per-fn retrospectives JSON. Keyed by (file, fn, container)
    so same-named fns in different impls track independently. Survives
    across module iterations — that's the point: learning compounds."""
    d = project_dir / ".phase_c_logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "retrospectives.json"


def _load_retrospectives(project_dir: Path) -> dict:
    """Load the on-disk retrospective record. Returns {} on first run or
    when the file is malformed (caller treats missing entries as 'no
    prior retrospective'). Keys written before container canonicalisation
    are re-keyed on load so old records keep matching."""
    p = _retrospectives_path(project_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        rekeyed: dict = {}
        for key, rec in data.items():
            parts = key.split("::", 2)
            if len(parts) == 3:
                key = _retro_key(parts[0], parts[1], parts[2])
            rekeyed[key] = rec
        return rekeyed
    except Exception:
        return {}


def _save_retrospectives(project_dir: Path, data: dict) -> None:
    with suppress(Exception):
        _retrospectives_path(project_dir).write_text(
            json.dumps(data, indent=2), encoding="utf-8")


def _canon_container(container: str) -> str:
    """Canonical container form for keying. The extractor's fn records and
    the impl-block manifest render the same impl differently (token-spaced
    `impl Context < T , E > for Result < T , E >` vs pretty-printed
    `impl<T, E> Context<T, E> for Result<T, E>\\nwhere\\n    E: ...`), which
    used to split one impl across two retrospective keys. Reduce both to
    `<Trait>for<SelfTy>`: drop a leading impl-generics list, drop the
    where-clause, drop all whitespace."""
    s = " ".join(container.split())
    if s.startswith("impl"):
        s = s[4:].lstrip()
        if s.startswith("<"):
            depth = 0
            for i, ch in enumerate(s):
                if ch == "<":
                    depth += 1
                elif ch == ">":
                    depth -= 1
                    if depth == 0:
                        s = s[i + 1:]
                        break
    if s.endswith(" where"):
        s = s[: -len(" where")]
    s = s.split(" where ")[0]
    return "".join(s.split())


def _retro_key(file_rel: str, fn_name: str, container: str) -> str:
    """Stable key for the retrospective dict. Container is included so
    overloaded `fn new` impls (Adhoc/Trait/Boxed) track independently;
    it is canonicalised so different renderings of one impl share a key."""
    return f"{file_rel}::{fn_name}::{_canon_container(container)}"


def _reset_exhaustion_flags(project_dir: Path, reason: str) -> int:
    """Clear every `fully_exhausted` flag (keep the lessons — category,
    guidance, history all stay) at a phase boundary where the evidence
    context changed (e.g. the merged tree landed dozens of fills). FR-024's
    cross-iter skip was written against a narrower tree; one fresh, note-
    informed attempt per boundary is exactly D3's re-entry rule applied
    across phases. Returns the number of flags cleared."""
    retros = _load_retrospectives(project_dir)
    cleared = 0
    for rec in retros.values():
        if isinstance(rec, dict) and rec.get("fully_exhausted"):
            rec["fully_exhausted"] = False
            cleared += 1
    if cleared:
        _save_retrospectives(project_dir, retros)
        _log(f"  [phase-c] reset {cleared} fully_exhausted flag(s) — {reason}")
        _emit("phase_c.exhaustion.reset", data={"cleared": cleared, "reason": reason})
    return cleared


def _phase_c_retrospective(
    fn_name: str, sig_text: str, container: str, file_rel: str,
    cluster_name: str,
    last_body: str, error_excerpt: str,
    fix_attempts: int,
    prior_retrospective: str = "",
    sibling_bodies: str = "",
) -> dict:
    """Diagnose why the per-fn fix loop exhausted on this fn. Returns
    {category, guidance, signal} — a structured diagnosis used to shape
    the NEXT attempt's prompt with explicit "try X instead" hints rather
    than just retrying the same approach.

    Categories:
      WRONG_TYPE_CHOICE / WRONG_DEREF_PATTERN / MISSING_UNSAFE_PRIMITIVE
      TRAIT_BOUND_VIOLATION / API_HALLUCINATION / NEEDS_VTABLE_ACCESS
      GENUINELY_HARD

    'GENUINELY_HARD' signals the caller should mark the fn as
    fully-exhausted and skip on subsequent iters.
    """
    if not last_body and not error_excerpt:
        return {"category": "", "guidance": "", "signal": "no_data"}
    # #2' (FR-037): if EVERY error is downstream (in other files, none in the
    # target file), this fn is probably fine and the crate is broken elsewhere.
    # Running a target retro here misattributes the cause and writes a WRONG
    # anti-hallucination note (e.g. blaming `construct_from_boxed` for a doc
    # parse error in lib.rs). Skip it; let the blocking fn's own loop fix it.
    _exc = error_excerpt or ""
    if "Errors elsewhere in the crate" in _exc and "Errors in target file" not in _exc:
        return {"category": "DOWNSTREAM", "guidance": "", "signal": "downstream_only"}
    prior_section = (
        f"\nPrior retrospective for this fn (you said this LAST time it failed; "
        f"if the same approach still failed, escalate to GENUINELY_HARD):\n"
        f"{prior_retrospective}\n"
    ) if prior_retrospective.strip() else ""
    siblings_section = (
        f"\nSibling impls of `{fn_name}` already filled correctly in this file "
        f"(use as reference):\n{sibling_bodies}\n"
    ) if sibling_bodies.strip() else ""
    prompt = (
        f"You are diagnosing why a reconstruction attempt for fn `{fn_name}` "
        f"in `{file_rel}` (impl: `{container}`, cluster: `{cluster_name}`) "
        f"could not be made to compile after {fix_attempts} attempt(s).\n\n"
        f"Signature:\n```rust\n{sig_text}\n```\n\n"
        f"Last attempted body (failed to compile):\n```rust\n{last_body[:_FAILED_BODY_CAP]}\n```\n\n"
        f"Compile errors from that attempt:\n```\n{error_excerpt[:_FIX_ERR_CAP]}\n```\n"
        f"{prior_section}"
        f"{siblings_section}\n"
        "Classify the root cause in ONE of these categories:\n"
        "  WRONG_TYPE_CHOICE       — used wrong wrapper/type (e.g. Box vs NonNull vs Own)\n"
        "  WRONG_DEREF_PATTERN     — *ptr vs .as_ref() vs .deref(); name the correct chain\n"
        "  MISSING_UNSAFE_PRIMITIVE— body needs unsafe { Self::X() } or similar; name X\n"
        "  TRAIT_BOUND_VIOLATION   — generic bounds or where-clause mismatch; name the bound\n"
        "  API_HALLUCINATION       — model called a method that does not exist; name it\n"
        "  NEEDS_VTABLE_ACCESS     — requires reading the ErrorVTable; layout is needed\n"
        "  GENUINELY_HARD          — multiple categories or fundamental layout unknown; flag for skip\n\n"
        "Output EXACTLY two lines:\n"
        "  CATEGORY: <one of the above>\n"
        "  GUIDANCE: <one paragraph: what to try instead, specific to this fn>\n\n"
        "The GUIDANCE should be a concrete instruction (e.g. \"Use "
        "`self.inner.by_ref().as_ref()` to traverse the NonNull wrapper "
        "instead of `*self.inner`\"), NOT a generic 'try harder' platitude."
    )
    # FR-028: retro runs think=True with a bounded output budget. think=False
    # returns empty content on the vLLM reasoning-parser endpoint; the OUTPUT
    # cap (not a context-budget cap) keeps reasoning from running away. One
    # retry on empty is handled inside _ask_retrospective.
    try:
        result = _ask_retrospective(prompt)
    except Exception as e:
        _log(f"  [phase-c/retro] LLM call failed: {e}")
        return {"category": "", "guidance": "", "signal": "llm_error"}
    if not (result or "").strip():
        _log(f"  [phase-c/retro] LLM returned empty content for {fn_name}")
        return {"category": "", "guidance": "", "signal": "empty_response"}
    cat = ""
    guid = ""
    # Tolerant parsing: model may emit "CATEGORY:", "category:", "**CATEGORY**:",
    # or bullet-prefix variants. Strip markdown-ish decorations and match
    # case-insensitively. GUIDANCE may span multiple lines — collect them all.
    lines = (result or "").splitlines()
    guidance_lines: list[str] = []
    in_guidance = False
    for ln in lines:
        s = ln.strip().lstrip("*").lstrip("-").lstrip("•").strip()
        s_clean = re.sub(r"^\*+\s*|\s*\*+$", "", s)  # strip markdown bold
        m = re.match(r"(?i)^category\s*[:=]\s*(\S+)", s_clean)
        if m:
            cat = m.group(1).strip().rstrip(".,;").upper()
            in_guidance = False
            continue
        m = re.match(r"(?i)^guidance\s*[:=]\s*(.+)$", s_clean)
        if m:
            guidance_lines = [m.group(1).strip()]
            in_guidance = True
            continue
        if in_guidance and s_clean:
            # Multi-line guidance continuation (stop on blank or new label).
            if re.match(r"(?i)^(category|guidance|note|reason)\s*[:=]", s_clean):
                in_guidance = False
            else:
                guidance_lines.append(s_clean)
    guid = " ".join(guidance_lines).strip()
    # Fallback: if neither field parsed, but the response is non-empty,
    # treat the whole response as guidance and leave category blank. This
    # still gives the next attempt useful "what to try" text in the
    # retrospective block, even if our structured parser missed.
    if not cat and not guid and (result or "").strip():
        guid = (result or "").strip()[:1200]
    return {
        "category": cat,
        "guidance": guid,
        "signal": "ok" if (cat or guid) else "unparseable",
    }


def _is_fn_fully_exhausted(
    retros: dict, file_rel: str, fn_name: str, container: str,
) -> bool:
    """FR-024 cross-iter skip: progress-gated. A fn is permanently skipped
    only when (a) the LLM classified it GENUINELY_HARD, or (b) a
    retrospective fired AND the next-best error count did NOT improve over
    the prior best (`fully_exhausted` flag set at write time). No hard
    retrospective-count cap — as long as each retro reduces errors, the
    loop is free to continue. `retrospective_count` is kept only for
    telemetry."""
    key = _retro_key(file_rel, fn_name, container)
    rec = retros.get(key)
    if not rec:
        return False
    return bool(rec.get("fully_exhausted"))


def _count_relevant_notes(fr_path: Path | None,
                            fn_key: tuple[str, str]) -> int:
    """Count model-notes currently relevant to a (file, fn) — proxy for
    'has the model's accumulated knowledge about this fn grown since
    the prior attempt'. Used by the TDD driver's D3 re-entry policy.
    """
    if fr_path is None or not fr_path.exists():
        return 0
    try:
        fr_path.read_text(encoding="utf-8")  # readability probe; content unused
    except Exception:
        return 0
    rel, fn = fn_key
    section = _filter_model_notes_for_prompt(
        fr_path, file_rel=rel, fn_name=fn,
        file_text="", current_error="",
    )
    return section.count("\n- ") if section else 0


def _record_tdd_regression(
    fr_path: Path,
    src_dir: Path,
    filled_keys: "list[tuple[str, str]]",
    broken_tests: "set[str]",
    prev_best: int,
    now_pass: int,
) -> str:
    """Hook 3 — turn a `14→11` pass-count regression into an ACTIONABLE note.

    For each fn filled in the pass that just broke previously-passing tests,
    write a concrete WRONG-body Model Note (file + fn + the broken test names +
    the body that was applied) into the FR Model Notes channel, which the next
    fill prompt for that fn injects automatically (relevance-ranked by file/fn).
    The bare metric is unactionable; the body+broken-tests pair is the WRONG
    example the model can react to.

    Best-effort: never raises into the TDD loop. Returns a compact note (e.g.
    "regressed t1, t2") to stamp on the LoopState trajectory, or "" if none.
    """
    try:
        recs = _enumerate_fns_via_extractor(src_dir) or []
    except Exception:
        recs = []
    by_name: dict[str, dict] = {}
    for r in recs:
        nm = r.get("fn_name", "")
        if nm and nm not in by_name:
            by_name[nm] = r
    broken = ", ".join(sorted(broken_tests)[:6])
    for key in filled_keys:
        fn = key[1] if isinstance(key, tuple) and len(key) > 1 else str(key)
        rec = by_name.get(fn)
        if not rec:
            continue
        file_rel = rec.get("file", "")
        body = (rec.get("body_text", "") or "").strip()
        note = (
            f"[regression] In {Path(file_rel).name or '?'}, the body just written for "
            f"`{fn}` made previously-passing test(s) FAIL: {broken} (cluster pass "
            f"count dropped {int(prev_best)}→{int(now_pass)}). WRONG (this body "
            f"broke those tests):\n{body[:_FAILED_BODY_CAP]}\nRe-implement `{fn}` so "
            f"{broken} keep passing; do not repeat that approach."
        )
        with suppress(Exception):
            _append_model_note(fr_path, note)
    return f"regressed {broken}" if broken else ""


def _run_phase_c_tdd(
    *, project_dir: Path,
    target_src_dir: Path,
    tests_dir_files: list[Path],
    cluster_tests: list[tuple[str, str]],
    module: str,
    cluster: "BuildCluster",
    check_cmd: str,
    cluster_env: dict,
    leaf_counts: dict[str, int] | None,
    fr_path: Path | None,
    test_cmd_builder,
    tag: str,
) -> dict:
    """Plan 4 — TDD-scoped Phase C driver.

    Outer loop: cargo test → frontier → fill frontier → repeat.

    Per D-decisions resolved 2026-05-27:
      D2: order frontier by ascending panic depth, descending caller count
      D3: re-enter an exhausted fn only if other fills landed OR new
          relevant notes accumulated since the last attempt
      D4: cargo build failure routes into a compile-error frontier
      D5: 120s wall-clock timeout per cargo test invocation
      D7: iter-0 frontier surfaces shallowest fn per test naturally
    """
    # Frontier extraction expects the cargo project root (NOT project_root/src):
    # cargo's panic messages emit `src/foo.rs` paths relative to the project
    # root, and the syn extractor records `file` fields the same way. Passing
    # `project_root/src` would produce double-prefixed paths and zero matches.
    src_dir = target_src_dir
    # Filter to direct children of tests/ — only those are cargo --test targets.
    test_stems = sorted({
        Path(rel).stem
        for rel, _ in cluster_tests
        if "tests" in Path(rel).parts and Path(rel).parent.name == "tests"
    })
    if not test_stems:
        _log(f"  {tag} [phase-c/tdd] no test stems in cluster; skipping TDD.")
        return {"fns_filled": 0, "bytes_filled": 0, "attempts": 0,
                "remaining": len(_scan_unimplemented_for_cluster(
                    target_src_dir, cluster)),
                "errors": []}
    test_cmd = test_cmd_builder(test_stems)
    test_timeout = int(os.getenv("PHASE_C_TEST_TIMEOUT", "120"))
    # max_iters is legacy — only the 10k backstop in the loop body honors it.
    # The real terminator is floor-stall on pass_count (max_stalls below).
    max_stalls = _effective_stalls(int(os.getenv("TDD_MAX_STALLS", "3")),
                                   _converge_in_cluster(cluster) or module)

    _log(f"  {tag} [phase-c/tdd] driver=tdd, "
         f"max_stalls={max_stalls}, timeout={test_timeout}s, "
         f"stems={len(test_stems)}")

    permanently_exhausted: set[tuple[str, str]] = set()
    last_notes_count: dict[tuple[str, str], int] = {}
    last_total_filled = 0

    total_filled = 0
    total_attempts = 0
    total_bytes = 0
    errors: list[str] = []

    # FR-024: improvement-driven loop. Track passing-test count across iters;
    # exit only after `max_stalls` consecutive non-improving iters. The 10k
    # backstop in the loop body is a runaway-safety net only.
    # LoopState is the source of truth for the pass_count floor / stall streak
    # AND the carrier of cumulative meta-awareness (the pass trajectory + the
    # causal "this fill broke these tests" notes) fed back into each fill.
    # `stall`/`best_pass` below are kept as synced mirrors so the existing
    # log/emit lines read unchanged.
    state = _LoopState()
    stall = 0
    best_pass = -1
    best_fail = None
    recently_filled: list[tuple[str, str]] = []
    recently_filled_max = int(os.getenv("TDD_RECENTLY_FILLED_MAX", "50"))
    # Hook 3 — causal-regression tracking. `prev_failed` = the failing-test set
    # from the previous iter; `last_newly_filled` = the fns we materially
    # changed last iter. A test that flips pass→fail right after a fill is a
    # regression attributable to that fill.
    prev_failed: set[str] = set()
    last_newly_filled: list[tuple[str, str]] = []
    _regression_notes = os.getenv("TDD_REGRESSION_NOTES", "1") == "1"
    # Build the test reverse map once per cluster — cheap (one extractor
    # invocation, one regex scan of each test file). Used as the LAST
    # attribution fallback when panics/compile errors/classifier all miss.
    try:
        test_reverse_map = _build_test_reverse_map(tests_dir_files, src_dir)
    except Exception as _e:
        _log(f"  {tag} [phase-c/tdd] reverse-map build failed: {_e}")
        test_reverse_map = {}

    iter_n = 0
    # Hard cap removed — the sole non-success terminator is `stall >=
    # max_stalls` on the pass_count floor. The 10k backstop here mirrors
    # `_improvement_loop`'s safety net and should never fire in practice.
    _tdd_backstop = int(os.getenv("PHASE_C_TDD_BACKSTOP", "10000"))
    while True:
        iter_n += 1
        if iter_n > _tdd_backstop:
            _log(f"  {tag} [phase-c/tdd] backstop {_tdd_backstop} reached — "
                 "stopping. This should never happen — stall budget should "
                 "have fired first.")
            break
        _check_graceful_stop(f"TDD iter {iter_n}")
        _log(f"  {tag} [phase-c/tdd] iter {iter_n}: running cargo test...")
        t0 = time.monotonic()
        rc, output = _run_cargo(test_cmd, target_src_dir, cluster_env,
                                  timeout=test_timeout)
        dt = time.monotonic() - t0
        pass_count, fail_count, _ignored = _parse_test_counts(output)
        cur_failed = _parse_failed_tests(output)
        if rc == 0:
            _log(f"  {tag} [phase-c/tdd] iter {iter_n}: all tests pass "
                 f"({pass_count} passed, {dt:.1f}s). Cluster done.")
            _emit("phase_c.tdd.iter", data={
                "cluster": cluster.name, "iter": iter_n, "rc": 0,
                "frontier_size": 0, "kind": "pass",
                "pass_count": pass_count, "fail_count": fail_count,
            })
            break
        if rc == -1:
            _log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test TIMED OUT "
                 f"({test_timeout}s). Stopping.")
            _emit("phase_c.tdd.iter", data={
                "cluster": cluster.name, "iter": iter_n, "rc": -1,
                "frontier_size": 0, "kind": "timeout",
                "pass_count": pass_count, "fail_count": fail_count,
            })
            break

        # Improvement metric: did pass count strictly increase since best?
        improved = pass_count > best_pass
        if improved:
            best_fail = fail_count
        # Hook 3 — detect a causal regression BEFORE recording, so the note
        # rides this iter's Attempt (frozen) and surfaces in context(). A test
        # that flipped pass→fail right after last iter's fill is attributable
        # to that fill; write the concrete WRONG-body note for the model.
        _regr_note = ""
        if _regression_notes and fr_path and last_newly_filled:
            _newly_failed = cur_failed - prev_failed
            if _newly_failed:
                _regr_note = _record_tdd_regression(
                    fr_path, src_dir, last_newly_filled, _newly_failed,
                    best_pass, pass_count,
                )
        prev_failed = cur_failed
        # Record this iter's measurement on the shared LoopState (metric is
        # pass_count — "higher is better" here, so NO negation). record()
        # updates the floor + stall streak; mirror them into the scalars the
        # log/emit lines below read.
        state.record(_Attempt(metric=float(pass_count), iteration=state.iteration,
                              improved=improved, note=_regr_note))
        best_pass = int(state.best_metric)
        stall = state.stalls

        # Attribute failures via the full chain (panics → compile →
        # classifier → recently_filled → reverse_map).
        frontier, kind = _attribute_failures(
            output, src_dir, recently_filled,
            test_reverse_map, cluster.name,
        )

        # D3 — re-entry filter for fns that already exhausted the per-fn
        # fix loop in a prior iter (avoid burning compute on the same fn
        # with no new context).
        eligible: Frontier = {}
        skipped_perm: int = 0
        for key, tests in frontier.items():
            if key in permanently_exhausted:
                notes_now = _count_relevant_notes(fr_path, key)
                if total_filled == last_total_filled and \
                   notes_now <= last_notes_count.get(key, 0):
                    skipped_perm += 1
                    continue
                permanently_exhausted.discard(key)
            eligible[key] = tests

        # Exit condition: stall budget exhausted on the cluster-level
        # pass_count floor. Fires regardless of whether `eligible` is
        # empty — pass_count is the only metric that captures real-world
        # progress, so when it hasn't moved for `max_stalls` iters the
        # loop isn't making progress no matter how busy the frontier
        # looks. Pre-fix, an unfixable fn that stayed in the frontier
        # forever would override the stall check and burn compute
        # indefinitely. The frontier is now a hint about WHERE to look,
        # not a license to keep iterating.
        if stall >= max_stalls:
            _log(f"  {tag} [phase-c/tdd] iter {iter_n}: pass_count plateau "
                 f"{stall} iter(s) at best={best_pass}; stopping "
                 f"({len(eligible)} fn(s) still on frontier — will be "
                 "marked exhausted). best pass="
                 f"{best_pass} fail={best_fail}.")
            for _stuck_key in eligible:
                permanently_exhausted.add(_stuck_key)
            _emit("phase_c.tdd.iter", data={
                "cluster": cluster.name, "iter": iter_n, "rc": rc,
                "frontier_size": len(eligible), "kind": "stall_exit",
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            break
        if not eligible:
            _log(f"  {tag} [phase-c/tdd] iter {iter_n}: no actionable "
                 f"frontier this iter but stall={stall}/{max_stalls} — "
                 f"continuing (pass={pass_count}, fail={fail_count}, "
                 f"improved={improved}).")
            _emit("phase_c.tdd.iter", data={
                "cluster": cluster.name, "iter": iter_n, "rc": rc,
                "frontier_size": 0, "kind": "no_frontier_continue",
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            # Nothing to do this iter — loop back to re-run cargo test.
            # (Mostly a no-op, but keeps the loop honest: if a flaky
            # failure or test ordering surfaced a fn next iter, we'll
            # pick it up.)
            continue

        ordered = _order_frontier(eligible, leaf_counts)
        frontier_keys = {key for key, _ in ordered}
        _log(f"  {tag} [phase-c/tdd] iter {iter_n}: cargo test rc={rc} "
             f"({dt:.1f}s); pass={pass_count} fail={fail_count} "
             f"(improved={improved}, stall={stall}/{max_stalls}); "
             f"frontier={len(eligible)} fn(s) ({kind}), "
             f"{skipped_perm} skipped as exhausted.")
        _emit("phase_c.tdd.iter", data={
            "cluster": cluster.name, "iter": iter_n, "rc": rc,
            "frontier_size": len(eligible), "kind": kind,
            "pass_count": pass_count, "fail_count": fail_count,
            "improved": improved, "stall": stall,
        })

        # Drive the per-fn fill machinery with this frontier.
        sub = _task2_fill_fns_one_by_one(
            project_dir=project_dir,
            target_src_dir=target_src_dir,
            tests_dir_files=tests_dir_files,
            module_name=module,
            cluster_name=cluster.name,
            cluster=cluster,
            check_cmd=check_cmd,
            cluster_env=cluster_env,
            max_passes=1,
            leaf_name_counts=leaf_counts,
            fr_path=fr_path,
            tdd_frontier_keys=frontier_keys,
            tdd_frontier_tests=eligible,   # Plan 10 U2: {key: {tests}} to validate
            tdd_test_cmd=test_cmd,         # Plan 10 U2: scoped cluster test cmd
            skip_end_pass_test=True,
        )
        total_filled += sub.get("fns_filled", 0)
        total_attempts += sub.get("attempts", 0)
        total_bytes += sub.get("bytes_filled", 0)
        errors.extend(sub.get("errors", []))

        # Update recently_filled ring buffer with this iter's actual fills.
        # We don't have the exact list from the sub-result, so use the
        # frontier keys that are NO LONGER in the unimplemented set after
        # the pass — that's the set of fns that materially changed.
        cur_stubs = set(_scan_unimplemented_functions(src_dir))
        newly_filled = [k for k in frontier_keys if k not in cur_stubs]
        last_newly_filled = newly_filled   # hook 3: next iter attributes regressions to these
        for k in newly_filled:
            recently_filled.append(k)
        if len(recently_filled) > recently_filled_max:
            recently_filled = recently_filled[-recently_filled_max:]
        for key in frontier_keys:
            if key in cur_stubs:
                permanently_exhausted.add(key)
                last_notes_count[key] = _count_relevant_notes(fr_path, key)
        last_total_filled = total_filled

    return {
        "fns_filled": total_filled,
        "bytes_filled": total_bytes,
        "attempts": total_attempts,
        "remaining": len(_scan_unimplemented_for_cluster(
            target_src_dir, cluster)),
        "errors": errors,
    }


# Soft cap on stub members per impl block. Larger blocks fall back to the
# per-fn path so the output budget always fits. Anyhow's `impl Error` is
# the only block likely to exceed this in practice.
_PER_IMPL_MAX_STUBS = int(os.getenv("PHASE_C_IMPL_MAX_STUBS", "12"))

# Module-level splice+cargo lock. Originally a local in
# `_task2_fill_fns_one_by_one`, hoisted because the per-impl path also
# needs to serialise its splice + cargo invocations against any other
# worker mutating the same cluster's source tree. cargo's own target/
# .cargo-lock blocks concurrent invocations anyway, but holding our own
# lock keeps file mutations deterministic across the per-impl + per-fn
# fallback path interaction.
import threading as _threading_mod

_splice_check_lock = _threading_mod.Lock()


@dataclasses.dataclass(frozen=True, slots=True)
class _FixOutcome:
    """One fix-loop attempt's result, returned by a unit's attempt callable.

    kind: "accepted" — the unit accepted and finalized the fill;
          "stop"     — protocol dead-end (empty response, splice rejected,
                       race lost, LLM error): retrying cannot help;
          "failed"   — compile/test failure: the driver's gauge decides
                       whether to retry (err_sig/err_count feed FloorStall).
    """
    kind: str
    # None = "this unit has no error signature" → the driver's repeated-
    # signature exit never fires (loop C / f4: failing-test counts carry no
    # rustc sig). An EMPTY frozenset is a real signature: two consecutive
    # empty sigs (rc!=0, zero parsed diagnostics) trigger the repeated exit,
    # as the hand-rolled per-fn/per-impl loops always did.
    err_sig: "frozenset | None" = None
    err_count: int = 0


def _drive_fix_loop(
    one_attempt: "Callable[[int], _FixOutcome]",
    *,
    hard_cap: int,
    max_stalls: int,
    stop_context: str,
    on_failure: "Callable[[int, _FixOutcome, _FloorStall], None] | None" = None,
    on_stall: "Callable[[int, _FloorStall], None] | None" = None,
    on_repeated: "Callable[[int, _FloorStall], None] | None" = None,
    initial_floor: "int | None" = None,
) -> tuple[bool, int]:
    """THE fix-loop driver (slice 8a f2). Owns the loop skeleton every fix
    loop hand-rolled — attempt counting, graceful-stop checks, FloorStall
    observation, and the stall / repeated-signature terminations. The unit
    owns everything domain-specific inside `one_attempt` (context gathering,
    prompt, splice + locking, verification, acceptance, revert); callbacks
    carry the unit's telemetry (loop.iter emit, stop logs).

    Consumers: per-fn loop (f2). Per-impl (f3) and the post-pass test retry
    (f4) port onto this same driver. Returns (accepted, attempts_used).
    """
    gauge = _FloorStall()
    if initial_floor is not None:
        # Seed the floor with the metric the loop ENTERED with (loop C / f4):
        # a first iteration that fails to improve on the entry state counts
        # as a stall, instead of getting a free baseline observation.
        gauge.observe(initial_floor)
    attempt_n = 0
    while attempt_n < hard_cap:
        attempt_n += 1
        _check_graceful_stop(f"{stop_context} attempt {attempt_n}")
        outcome = one_attempt(attempt_n)
        if outcome.kind == "accepted":
            return True, attempt_n
        if outcome.kind == "stop":
            return False, attempt_n
        gauge.observe(outcome.err_count, signature=outcome.err_sig)
        if on_failure is not None:
            on_failure(attempt_n, outcome, gauge)
        if gauge.stalled(max_stalls):
            if on_stall is not None:
                on_stall(attempt_n, gauge)
            return False, attempt_n
        if gauge.repeated:
            if on_repeated is not None:
                on_repeated(attempt_n, gauge)
            return False, attempt_n
    return False, attempt_n


def _phase_c_fill_impl_blocks(
    project_dir: Path,
    target_src_dir: Path,
    tests_dir_files: list[Path],
    module_name: str,
    cluster_name: str,
    cluster: "BuildCluster | None",
    check_cmd: str,
    cluster_env: dict,
    fr_path: Path | None,
    tdd_frontier_keys: "set[tuple[str, str]] | None",
    tdd_frontier_tests: "Frontier | None" = None,  # Plan 10 U2
    tdd_test_cmd: str | None = None,                                       # Plan 10 U2
) -> dict:
    """Per-impl-block Phase C fill — alternative to `_task2_fill_fns_one_by_one`.

    Collects impl blocks active in this cluster, optionally narrows to
    those that contain a frontier fn, then for each block runs one LLM
    call that fills every stub member of the block in one shot. Splices
    all bodies, runs cargo check; on failure reverts and re-prompts with
    the focused errors. Each impl block has its own floor-stall on rustc
    error count.

    Returns the same stats dict shape as `_task2_fill_fns_one_by_one` so
    callers don't need to branch:
        {fns_filled, bytes_filled, attempts, errors, remaining,
         impls_filled, impls_failed}.

    Free fns (container == "") and impl blocks with more than
    `_PER_IMPL_MAX_STUBS` stubs are left untouched — the caller's per-fn
    fallback handles them.
    """
    if cluster_env is None:
        cluster_env = _cargo_env()
    target_src_dir.mkdir(parents=True, exist_ok=True)
    bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    impls_filled = 0
    impls_failed = 0
    fns_filled_total = 0
    attempts_total = 0
    errors: list[str] = []

    blocks = _collect_impl_blocks_for_cluster(target_src_dir, cluster) or []
    if not blocks:
        return {
            "fns_filled": 0, "bytes_filled": 0, "attempts": 0,
            "errors": [], "remaining": 0,
            "impls_filled": 0, "impls_failed": 0,
        }

    # If a TDD frontier is provided, narrow to blocks whose stub_members
    # intersect the frontier. Otherwise process all blocks with stubs.
    if tdd_frontier_keys is not None:
        narrowed: list[dict] = []
        for b in blocks:
            stub_keys = {(b["file"], m["fn_name"]) for m in b["stub_members"]}
            if stub_keys & tdd_frontier_keys:
                narrowed.append(b)
        blocks = narrowed

    # Drop empty / free-fn / oversized blocks (handled by per-fn fallback).
    def _viable(b: dict) -> bool:
        if not b["container"]:
            return False
        if not b["stub_members"]:
            return False
        return not len(b["stub_members"]) > _PER_IMPL_MAX_STUBS
    viable_blocks = [b for b in blocks if _viable(b)]
    _log(f"  [phase-c/impl] cluster {cluster_name}: "
         f"{len(viable_blocks)} impl block(s) viable "
         f"({len(blocks) - len(viable_blocks)} skipped — free fns / "
         "oversized).")

    # Crate-level context (shared by every block we visit).
    _src_for_macros = target_src_dir / "src" if (target_src_dir / "src").exists() else target_src_dir
    _macro_invariants = _compute_cluster_macro_invariants(_src_for_macros, cluster)
    _no_std_invariant = _detect_crate_no_std_invariant(target_src_dir)
    invariants_section = "\n\n".join(
        s for s in (_no_std_invariant, _macro_invariants) if s)
    _crate_type_records = _cluster_filter_type_methods(
        _enumerate_types_via_extractor(target_src_dir) or [], cluster)
    macros_section = _collect_crate_macros(_src_for_macros)

    # Dispatch families (FR-027): autoref-specialization trait sets dispatched
    # by a macro. Their correctness is a cross-impl invariant, so when a block
    # belongs to one we co-design the whole family — broaden related_blocks to
    # every family-trait block and inject the dispatching macro as the spec.
    _families = _dispatch_families_via_extractor(target_src_dir) or []
    # A block belongs to a family if its container names a family trait OR one
    # of the dispatched method's return ("tag") types — the latter pulls the
    # tag's inherent impl (where the chained `new` lives) into co-design.
    _name_to_family: dict[str, DispatchFamily] = {}
    for _fam in _families:
        for _nm in list(_fam.traits) + list(_fam.return_types):
            _name_to_family[_nm] = _fam
    if _families:
        _log(f"  [phase-c/impl] {len(_families)} dispatch family(ies): "
             + "; ".join(
                 f"{f.method}←{f.traits}"
                 f"+tags{f.return_types}.{f.chained_methods}"
                 for f in _families))

    def _container_names(container: str) -> set:
        return set(container.replace("<", " ").replace(">", " ")
                   .replace("+", " ").split())

    def _family_for_container(container: str) -> "DispatchFamily | None":
        toks = _container_names(container)
        for _nm, _f in _name_to_family.items():
            if _nm in toks:
                return _f
        return None

    retros_cache = _load_retrospectives(project_dir)

    _impl_full_stalls = int(os.getenv("PHASE_C_IMPL_MAX_STALLS", "3"))

    def _fill_one_block(block: dict) -> None:
        """One impl block's fill: context assembly, the `_attempt_impl_fill`
        unit on `_drive_fix_loop` (slice 8a f3 — replaces the hand-rolled
        while loop), and exhausted-block bookkeeping (retro + fail emit)."""
        nonlocal attempts_total, fns_filled_total, impls_filled, impls_failed
        block_id = _impl_block_id(block["file"], block["container"])
        # Per-file converge keying: only the converge module keeps full budget.
        block_max_stalls = _effective_stalls(_impl_full_stalls, _module_of(block["file"]))
        file_path = target_src_dir / block["file"]
        if not file_path.exists():
            return
        # Honor cross-iter exhaustion — if a prior retrospective marked
        # this block GENUINELY_HARD it's skipped.
        if _is_fn_fully_exhausted(retros_cache, block["file"], "*impl*", block["container"]):
            _log(f"  [phase-c/impl] {block_id}: skipped (prior retro "
                 "marked exhausted).")
            return
        # Related blocks (same trait, different generic) for the prompt.
        all_blocks_by_id = {_impl_block_id(b["file"], b["container"]): b for b in blocks}
        related_blocks: list[dict] = []
        for rc in block.get("related_containers", []):
            rb = all_blocks_by_id.get(_impl_block_id(block["file"], rc))
            if rb is not None:
                related_blocks.append(rb)
        # Dispatch-family co-design: if this block implements a family trait,
        # surface EVERY family-trait block (not just same-name relatives) plus
        # the dispatching macro, so the model designs the family consistently.
        _family = _family_for_container(block["container"])
        dispatch_family_section = ""
        if _family is not None:
            _fam_names = set(_family.traits) | set(_family.return_types)
            _seen = {_impl_block_id(block["file"], block["container"])}
            _seen |= {_impl_block_id(rb["file"], rb["container"]) for rb in related_blocks}
            for b2 in blocks:
                bid = _impl_block_id(b2["file"], b2["container"])
                if bid in _seen:
                    continue
                # Pull in every block of this family — the dispatch traits AND
                # the tag types' inherent impls (where the chained `new` lives).
                if _container_names(b2["container"]) & _fam_names:
                    related_blocks.append(b2)
                    _seen.add(bid)
            dispatch_family_section = _build_dispatch_family_section(_family)
        snapshot = file_path.read_text(encoding="utf-8")
        # Per-block context.
        stub_fn_names = [m["fn_name"] for m in block["stub_members"]]
        test_blocks: list[tuple[str, str]] = []
        # Reuse the per-fn test finder by unioning tests touching any stub.
        for fn_name in stub_fn_names[:4]:
            for path_str, txt in _find_tests_referencing_fn(
                    tests_dir_files, fn_name, max_blocks=2):
                if not any(p == path_str for p, _ in test_blocks):
                    test_blocks.append((path_str, txt))
        # Types in scope: union over all stub signatures in this block,
        # plus the impl header and its where clause — the self-type
        # (e.g. `impl Display for BoxedError`) appears in no member sig,
        # so without the container its definition is never injected.
        _block_container = block.get("container") or ""
        _block_where = _extract_impl_where_clause(snapshot, _block_container)
        _member_sigs_text = " ".join(
            (m.get("sig_text") or "") for m in block["members"])
        # Crate idioms — surfaces THIS file's repeating patterns (Self::
        # constructor family, vtable dispatch, wrapper construction,
        # unsafe chains) from already-filled fn bodies. The target
        # sig_text drives the relevance ranking so the model sees the
        # patterns most likely to apply to the fns it's about to write.
        _target_sig_concat = " ".join(
            (m.get("sig_text") or "") for m in block["stub_members"])
        crate_idioms_section = _extract_crate_idioms(
            blocks, block["file"],
            target_sig_text=_target_sig_concat,
            max_idioms=5,
        )
        # FR-024 sibling reference: union over stub member names so e.g.
        # a stub `fmt` sees an already-filled `fmt` from another impl.
        _sib_parts: list[str] = []
        for fn_name in stub_fn_names[:4]:
            _s = _collect_sibling_filled_bodies(
                snapshot, fn_name, block.get("container") or "")
            if _s:
                _sib_parts.append(_s)
        siblings_section = "\n\n".join(_sib_parts)
        prior_retro = retros_cache.get(
            _retro_key(block["file"], "*impl*", block["container"]), {}
        ).get("guidance", "")
        notes_section = ""
        if fr_path is not None:
            notes_section = _filter_model_notes_for_prompt(
                fr_path, file_rel=block["file"], fn_name=stub_fn_names[0] if stub_fn_names else "",
                file_text=snapshot, current_error="",
            )
        last_error_excerpt = ""
        last_response = ""
        members_filled_this_block = 0
        # B3 (FR-029): baseline = the crate's error signature with THIS block's
        # stubs still unimplemented. A fill is "non-regressing" if it adds no
        # error beyond this baseline and none on the filled fns — then we keep
        # it even when the crate doesn't fully compile, so prerequisites of an
        # interdependent web accumulate instead of being reverted forever.
        # Gated by PHASE_C_KEEP_NONREGRESSING (instant rollback); captured once
        # per block (the snapshot is pristine here).
        _b3_keep = os.getenv("PHASE_C_KEEP_NONREGRESSING", "0").strip() not in ("", "0", "false")
        _baseline_sig: frozenset = frozenset()
        if _b3_keep:
            with _splice_check_lock:
                _brc, _bout, _bdiag = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
            _baseline_sig = _cargo_err_sig_from_diagnostics(_bdiag)
        _t0 = time.monotonic()
        _emit("phase_c.impl.fill.start", data={
            "cluster": cluster_name,
            "file": block["file"],
            "container": block["container"],
            "stub_count": len(block["stub_members"]),
        })

        def _attempt_impl_fill(fix_attempt: int) -> _FixOutcome:
            nonlocal attempts_total, fns_filled_total, impls_filled, \
                last_error_excerpt, last_response, members_filled_this_block
            attempts_total += 1
            # Slice 8a: TYPES_IN_SCOPE re-selected each attempt from the
            # member sigs + impl header + where clause + the current error
            # (Plan 9 F4: E0277/E0599/E0046/E0053 routinely name a trait
            # the impl block's signatures don't mention).
            _types_section_eff = _types_section_for_fix(
                _member_sigs_text, "", last_error_excerpt or "",
                _block_container, _block_where, _crate_type_records)
            prompt = _build_per_impl_prompt(
                block=block,
                related_blocks=related_blocks,
                test_blocks=test_blocks,
                module_name=module_name,
                cluster_name=cluster_name,
                types_section=_types_section_eff,
                invariants_section=invariants_section,
                notes_section=notes_section,
                fr_path=fr_path,
                retrospective_guidance=prior_retro,
                crate_idioms_section=crate_idioms_section,
                dispatch_family_section=dispatch_family_section,
                macros_section=macros_section,
                siblings_section=siblings_section,
            )
            try:
                response = ask(prompt, model=_TASK2_MODEL)
            except Exception as e:
                _log(f"  [phase-c/impl] {block_id}: LLM error: {e}")
                return _FixOutcome("stop")
            last_response = response
            parsed = extract_fn_bodies(response)
            if not parsed:
                _log(f"  [phase-c/impl] {block_id}: empty/no FN blocks "
                     f"(attempt {fix_attempt}).")
                return _FixOutcome("stop")
            # Hold the global splice+cargo lock while we mutate and check.
            with _splice_check_lock:
                # Splice each parsed body into the impl indicated by its
                # `block` attribute. Match against TARGET container OR any
                # related container.
                routed: list[tuple[dict, str]] = []  # (fn_data, container)
                for fn_data in parsed:
                    container = fn_data["block"]
                    if container == block["container"]:
                        routed.append((fn_data, block["container"]))
                        continue
                    matched = False
                    for rb in related_blocks:
                        if container == rb["container"]:
                            routed.append((fn_data, rb["container"]))
                            matched = True
                            break
                    if not matched:
                        # Loose match: header prefix only.
                        for cand in [block, *related_blocks]:
                            if container.split("{", 1)[0].strip() == cand["container"].split("{", 1)[0].strip():
                                routed.append((fn_data, cand["container"]))
                                matched = True
                                break
                    if not matched:
                        _log(f"  [phase-c/impl] {block_id}: dropped "
                             f"`{fn_data['fn_name']}` — block header "
                             "does not match any TARGET or RELATED.")
                spliced_now = 0
                # Apply each splice. Operating on the same file in
                # sequence keeps byte offsets consistent because we
                # re-locate after each splice via the syn enumerator.
                for fn_data, container in routed:
                    target_file = target_src_dir / block["file"]
                    ok, reason, delta = _splice_fn_with_where(
                        target_file, container, fn_data["fn_name"],
                        fn_data["body"], fn_data["where_clause"],
                    )
                    if ok:
                        spliced_now += 1
                    else:
                        _log(f"  [phase-c/impl] {block_id}: splice "
                             f"`{fn_data['fn_name']}` failed: {reason}")
                if spliced_now == 0:
                    _log(f"  [phase-c/impl] {block_id}: nothing spliced "
                         f"(attempt {fix_attempt}).")
                    return _FixOutcome("stop")
                # Run cargo check via the structured-diagnostics wrapper
                # so error counts come from rustc's own classification
                # (JSON `compiler-message`s with `level == "error"`) and
                # not from regex over rendered text. The regex path
                # produced false zeros for non-E-coded failures (linker,
                # build script, cargo wrapper) — the source of the
                # recurring "compile failed but errs=0" stall bug.
                # Run cargo check via the structured-diagnostics wrapper
                # so error counts come from rustc's own classification
                # (JSON `compiler-message`s with `level == "error"`) and
                # not from regex over rendered text.
                rc, cargo_out, diagnostics = _run_cargo_check_diag(
                    check_cmd, target_src_dir, cluster_env)
                if rc == 0:
                    # Plan 10 U2 (shared gate): accept the block only if its
                    # members' own tests pass too — not just compile.
                    _block_tests: set = set()
                    for _bm in block["stub_members"]:
                        _block_tests |= (tdd_frontier_tests or {}).get(
                            (block["file"], _bm.get("fn_name", "")), set())
                    _u2_impl = _u2_test_gate(_block_tests, tdd_test_cmd,
                                             target_src_dir, cluster_env)
                else:
                    _u2_impl = None
                if rc == 0 and _u2_impl is None:
                    members_filled_this_block = spliced_now
                    fns_filled_total += spliced_now
                    impls_filled += 1
                    _log(f"  [phase-c/impl] {block_id}: "
                         f"FILLED {spliced_now} fn(s) "
                         f"(attempt {fix_attempt}).")
                    _emit("phase_c.impl.fill.done", duration_ms=int(
                        (time.monotonic() - _t0) * 1000),
                        data={
                            "cluster": cluster_name,
                            "file": block["file"],
                            "container": block["container"],
                            "members_filled": spliced_now,
                            "members_failed": 0,
                            "fix_attempts": fix_attempt,
                        })
                    return _FixOutcome("accepted")
                # B3 (FR-029): keep a NON-REGRESSING fill even when the crate
                # doesn't fully compile — it adds no error beyond baseline and
                # none on the filled fns, so it's a correct prerequisite of the
                # interdependent web, not a failure to discard. This dissolves
                # the convergence deadlock where every correct piece is reverted
                # for not single-handedly making rc==0.
                if _b3_keep:
                    _cur_sig = _cargo_err_sig_from_diagnostics(diagnostics)
                    _new_errs = _cur_sig - _baseline_sig
                    _frontier = _frontier_from_diags(diagnostics, target_src_dir)
                    _filled = {fd["fn_name"] for fd, _ in routed}
                    _on_fill = any(f == block["file"] and fn in _filled
                                   for (f, fn) in _frontier)
                    if not _new_errs and not _on_fill:
                        members_filled_this_block = spliced_now
                        fns_filled_total += spliced_now
                        impls_filled += 1
                        _log(f"  [phase-c/impl] {block_id}: KEPT {spliced_now} "
                             f"fn(s) (B3 non-regressing; {len(_cur_sig)} crate "
                             f"err(s) remain elsewhere) (attempt {fix_attempt}).")
                        _emit("phase_c.impl.fill.done", duration_ms=int(
                            (time.monotonic() - _t0) * 1000),
                            data={
                                "cluster": cluster_name,
                                "file": block["file"],
                                "container": block["container"],
                                "members_filled": spliced_now,
                                "members_failed": 0,
                                "fix_attempts": fix_attempt,
                                "b3_kept": True,
                                "crate_errs_remaining": len(_cur_sig),
                            })
                        return _FixOutcome("accepted")
                if _u2_impl is not None:
                    cargo_out = _u2_impl[1]   # route test-failure detail as the error
                # Compile failed (or U2 test failure). Structured diagnostics
                # give the error signature; a U2 test failure uses the
                # failing-test set (test output has no rustc E-codes).
                # rc != 0 establishes a minimum count of 1.
                cur_err_sig = (_u2_impl[0] if _u2_impl is not None
                               else _cargo_err_sig_from_diagnostics(diagnostics))
                cur_err_count = max(1, len(cur_err_sig))  # rc-honest minimum
                last_error_excerpt = _focus_cargo_errors(cargo_out, block["file"])
                _record_error_codes_seen(cargo_out)
                # The fill regressed (a new error, or an error on the filled
                # fns) — B3 (FR-029) already accepted non-regressing fills
                # above. Revert; the driver decides whether to retry.
                file_path.write_text(snapshot, encoding="utf-8")
                return _FixOutcome("failed", err_sig=cur_err_sig,
                                   err_count=cur_err_count)

        def _on_impl_failure(attempt_n: int, outcome: _FixOutcome,
                             gauge: _FloorStall) -> None:
            _log(f"  [phase-c/impl] {block_id}: compile failed "
                 f"(attempt {attempt_n}); errs={outcome.err_count} "
                 f"floor={gauge.best} stall={gauge.stalls}.")

        def _on_impl_stall(attempt_n: int, gauge: _FloorStall) -> None:
            _log(f"  [phase-c/impl] {block_id}: floor stalled "
                 f"at {gauge.best} for {gauge.stalls} iter(s) "
                 "— stopping this block.")

        def _on_impl_repeated(attempt_n: int, gauge: _FloorStall) -> None:
            _log(f"  [phase-c/impl] {block_id}: identical errors "
                 f"after attempt {attempt_n} — stopping.")

        _, fix_attempt = _drive_fix_loop(
            _attempt_impl_fill,
            hard_cap=_PER_FN_FIX_CAP,
            max_stalls=block_max_stalls,
            stop_context=f"per-impl fix for {block_id}",
            on_failure=_on_impl_failure,
            on_stall=_on_impl_stall,
            on_repeated=_on_impl_repeated,
        )
        if members_filled_this_block == 0:
            impls_failed += 1
            errors.append(f"{block['file']}::{block['container']}: "
                          f"impl-fill failed after {fix_attempt} attempt(s)")
            _emit("phase_c.impl.fill.fail", duration_ms=int(
                (time.monotonic() - _t0) * 1000),
                data={
                    "cluster": cluster_name,
                    "file": block["file"],
                    "container": block["container"],
                    "members_failed": len(block["stub_members"]),
                    "fix_attempts": fix_attempt,
                    "last_error_preview": last_error_excerpt[:200],
                })
            # Per-impl retrospective — surface a strategy hint for the
            # next iter to consume. Uses the existing per-fn retro helper
            # with the impl id as the fn key so the on-disk store layout
            # stays uniform.
            try:
                cur_errs = len(_cargo_err_sig(last_error_excerpt))
                prior = retros_cache.get(
                    _retro_key(block["file"], "*impl*", block["container"]), {})
                prior_best = prior.get("best_error_count")
                retro = _phase_c_retrospective(
                    fn_name="*impl*",
                    sig_text=block["container"],
                    container=block["container"],
                    file_rel=block["file"],
                    cluster_name=cluster_name,
                    last_body=last_response[:_FAILED_BODY_CAP],
                    error_excerpt=last_error_excerpt,
                    fix_attempts=fix_attempt,
                    prior_retrospective=prior.get("guidance", ""),
                )
                if prior_best is None:
                    new_best = cur_errs
                    improved = True
                else:
                    new_best = min(int(prior_best), cur_errs)
                    improved = cur_errs < int(prior_best)
                full_exhaust = (
                    retro.get("category") == "GENUINELY_HARD"
                    or (prior_best is not None and not improved)
                )
                retros_cache[_retro_key(block["file"], "*impl*", block["container"])] = {
                    "category": retro.get("category", ""),
                    "guidance": retro.get("guidance", ""),
                    "signal": retro.get("signal", ""),
                    "retrospective_count": int(prior.get("retrospective_count", 0)) + 1,
                    "error_count": cur_errs,
                    "best_error_count": new_best,
                    "fully_exhausted": full_exhaust,
                    "last_error_preview": last_error_excerpt[:240],
                }
                _save_retrospectives(project_dir, retros_cache)
            except Exception as _re:
                _log(f"  [phase-c/impl/retro] {block_id}: {_re}")

    for block in viable_blocks:
        _fill_one_block(block)
    bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    remaining_records = _scan_unimplemented_records_for_cluster(target_src_dir, cluster)
    # None = extractor absent → text-scan fallback; 0 here would make the
    # caller skip the per-fn cleanup pass entirely.
    remaining_n = (len(remaining_records) if remaining_records is not None
                   else len(_scan_unimplemented_for_cluster(target_src_dir, cluster)))
    return {
        "fns_filled": fns_filled_total,
        "bytes_filled": max(0, bytes_after - bytes_before),
        "attempts": attempts_total,
        "errors": errors,
        "remaining": remaining_n,
        "impls_filled": impls_filled,
        "impls_failed": impls_failed,
    }


def _group_priority_levels(
    stubs: list[dict], leaf_name_counts: dict[str, int] | None,
) -> list[list[tuple[int, dict]]]:
    """Group leaf-first-sorted `stubs` into contiguous equal-ref-count LEVELS.

    Each level is a list of `(1-based index, stub)`. `stubs` is already sorted
    descending by ref-count, so equal-priority stubs are contiguous; a key change
    starts a new level. The dispatch loop runs each level with a barrier between
    them, so a more-referenced (callee) level always completes before the level
    that depends on it — every fill sees real sibling bodies in a scheduling-
    independent order (R11.3 determinism). Pure: no engine/closure state.
    """
    levels: list[list[tuple[int, dict]]] = []
    prev_key: object = object()
    for i, r in enumerate(stubs, 1):
        key = leaf_name_counts.get(r["fn_name"], 0) if leaf_name_counts else 0
        if key != prev_key:
            levels.append([])
            prev_key = key
        levels[-1].append((i, r))
    return levels


def _task2_fill_fns_one_by_one(
    project_dir: Path,
    target_src_dir: Path,
    tests_dir_files: list[Path],
    module_name: str,
    cluster_name: str,
    cluster: "BuildCluster | None" = None,
    check_cmd: str = "cargo check",
    cluster_env: dict | None = None,
    max_passes: int = 3,
    leaf_name_counts: dict[str, int] | None = None,
    fr_path: Path | None = None,
    # Plan 4 — TDD-scoped driver knobs.
    tdd_frontier_keys: "set[tuple[str, str]] | None" = None,
    skip_end_pass_test: bool = False,
    # Plan 10 U2 — per-fn test-based acceptance: {(file, fn): {test names}}
    # the fn must make pass, plus the cluster test command to run.
    tdd_frontier_tests: "Frontier | None" = None,
    tdd_test_cmd: str | None = None,
) -> dict:
    """Phase C — Flavour B with full validation loops.

    For each pass:
      For each unimplemented fn:
        Snapshot the file.
        Ask LLM → splice → cargo check.
        On compile failure: revert, re-prompt with focused errors, retry.
        Loop while the error signature CHANGES (model making progress).
        Stop on identical-errors-twice or PER_FN_FIX_CAP attempts.
        On exhaustion: revert, leave fn as stub for next pass.
      After all fns in the pass: cargo test on test files referenced by
      this pass's fills. On assertion failure: re-prompt the responsible
      fns with the test output, loop while progress.

    Returns {'fns_filled', 'bytes_filled', 'attempts', 'errors', 'remaining'}.
    """
    if cluster_env is None:
        cluster_env = _cargo_env()
    # P5 — per-impl-block path. New default unit of work: fill all stubs of
    # one impl block in one LLM call. Solves the class of failures where the
    # right fn body depends on a constraint declared at the impl level that
    # per-fn prompts cannot show. The per-impl path delegates free fns and
    # oversized blocks back to the per-fn path below, which continues to
    # exist as a fallback. Toggle via `PHASE_C_FILL_UNIT=fn`.
    _fill_unit = SETTINGS.phase_c_fill_unit
    if _fill_unit is FillUnit.IMPL:
        _impl_stats = _phase_c_fill_impl_blocks(
            project_dir=project_dir,
            target_src_dir=target_src_dir,
            tests_dir_files=tests_dir_files,
            module_name=module_name,
            cluster_name=cluster_name,
            cluster=cluster,
            check_cmd=check_cmd,
            cluster_env=cluster_env,
            fr_path=fr_path,
            tdd_frontier_keys=tdd_frontier_keys,
            tdd_frontier_tests=tdd_frontier_tests,   # Plan 10 U2
            tdd_test_cmd=tdd_test_cmd,               # Plan 10 U2
        )
        # If there are still stub members the per-impl path didn't cover
        # (free fns or oversized blocks), fall through to the per-fn path
        # to clean those up. We keep impl_stats counters around so the
        # caller can see both populations.
        if _impl_stats["remaining"] == 0:
            return _impl_stats
        # Carry the impl-pass numbers into the combined stats by stashing
        # them; the per-fn path below will overlay its own numbers.
        _carryover = {
            "impls_filled": _impl_stats.get("impls_filled", 0),
            "impls_failed": _impl_stats.get("impls_failed", 0),
            "fns_filled_via_impl": _impl_stats.get("fns_filled", 0),
        }
    else:
        _carryover = {}
    target_src_dir.mkdir(parents=True, exist_ok=True)
    bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    fns_filled = 0
    attempts = 0
    # P4: PHASE_C_PARALLELISM env knob.
    #   1 = strictly sequential — single worker, identical quality.
    #  >1 = up to N concurrent fn-fills via worker threads. LLM calls
    #       overlap; splice + cargo check is serialised through a global
    #       lock (cargo's own target/.cargo-lock blocks concurrent invocations
    #       anyway, and we want predictable file mutations).
    _parallelism = max(1, int(os.getenv("PHASE_C_PARALLELISM", "1")))
    if _parallelism > 1:
        _log(f"  [phase-c/per-fn] parallelism={_parallelism} (LLM calls "
             f"concurrent; splice+cargo serialised).")
    import queue as _queue_mod
    import threading as _threading
    # _splice_check_lock is now module-level (used by both per-impl and
    # per-fn paths so they don't race on the same file).
    _counters_lock = _threading.Lock()
    errors: list[str] = []

    failures_dir = project_dir / ".skeleton" / "_failures"
    failures_dir.mkdir(parents=True, exist_ok=True)

    # C: crate-wide macros (cached across passes — file set doesn't change).
    _src_for_macros = target_src_dir / "src" if (target_src_dir / "src").exists() else target_src_dir
    macros_section = _collect_crate_macros(_src_for_macros)
    # Per-cluster macro invariants: which arm of each cfg-gated macro is
    # active under this cluster. Cached per call — small set, cheap to
    # recompute, but constant for the duration of this Phase C loop.
    # Plus crate-level no_std warning when applicable (A).
    _macro_invariants = _compute_cluster_macro_invariants(
        _src_for_macros, cluster)
    _no_std_invariant = _detect_crate_no_std_invariant(target_src_dir)
    invariants_section = "\n\n".join(
        s for s in (_no_std_invariant, _macro_invariants) if s)
    # P5.1: crate-wide type records (struct/enum/type/trait with fields +
    # method names). Used per-fn to inject only the types the fn's signature
    # actually mentions. Cached across passes; file set doesn't change.
    # Cluster-aware: drop methods cfg-gated out of this cluster (Hard Rule 8).
    _crate_type_records = _cluster_filter_type_methods(
        _enumerate_types_via_extractor(target_src_dir) or [], cluster)

    # P7.6: floor-stall against (remaining_stubs + last_pass_failures) across
    # passes. max_passes becomes the hard cap. A pass that strictly decreases
    # the metric resets the stall counter; otherwise we increment. The legacy
    # `progress_this_pass == 0` early exit is preserved as a fast path.
    _pass_max_stalls = int(os.getenv("PHASE_C_PASS_MAX_STALLS", "2"))
    _pass_gauge = _FloorStall()  # slice 8a
    _pass_last_fail_count = 0

    for pass_n in range(1, max_passes + 1):
        _check_graceful_stop(f"per-fn pass {pass_n}")
        # Note: every pass scans for stubs fresh. Fns that failed their
        # fix-loop in earlier passes are now annotated stubs (sentinel +
        # commented failure history) and re-appear here, so passes 2/3
        # retry them with the new context of siblings filled meanwhile.
        # Cluster-aware filter: skip stubs whose enclosing cfg doesn't
        # match the current cluster's cfg_set (P5 focused fix). Avoids
        # wasted fills of e.g. nightly-only fns from the stable cluster.
        # Prefer record-based scan (syn enumerator gives container + sig_text
        # which disambiguates same-named fns like Display::fmt vs Debug::fmt).
        # Falls back to name-only tuples when the extractor isn't available.
        recs = _scan_unimplemented_records_for_cluster(target_src_dir, cluster)
        if recs is not None:
            stubs = recs
        else:
            stubs = [{"file": f, "fn_name": fn, "container": "", "sig_text": ""}
                     for f, fn in _scan_unimplemented_for_cluster(target_src_dir, cluster)]
        # True remaining stub count BEFORE the frontier filter — used by
        # the progress log so "already filled" reflects actual disk state,
        # not the TDD-narrowed pass list (which would over-report progress
        # since the frontier is only the subset tests have reached).
        _all_stubs_count = len(stubs)
        # Plan 4 — when TDD driver supplies a frontier, restrict this pass
        # to fns it nominated. Avoids walking fns no test reaches.
        if tdd_frontier_keys is not None:
            stubs = [s for s in stubs
                     if (s["file"], s["fn_name"]) in tdd_frontier_keys]
        if not stubs:
            break
        # D: leaf-first order — most-referenced fns first.
        _tuples = [(r["file"], r["fn_name"]) for r in stubs]
        _ordered_tuples = _leaf_first_fn_order(_tuples, leaf_name_counts)
        # Reorder records to match the leaf-first tuple order. When the same
        # (file, fn_name) appears multiple times (same-named fns), preserve
        # their relative order.
        _seen: dict[tuple[str, str], int] = {}
        _reordered: list[dict] = []
        for tup in _ordered_tuples:
            matches = [r for r in stubs if (r["file"], r["fn_name"]) == tup]
            taken = _seen.get(tup, 0)
            if taken < len(matches):
                _reordered.append(matches[taken])
                _seen[tup] = taken + 1
        stubs = _reordered
        # Crate idioms source: impl-block records snapshot for this pass —
        # already-filled bodies feed <CRATE_IDIOMS> in the per-fn prompts
        # (same section the per-impl path carries). [] when extractor absent.
        _idiom_blocks = _collect_impl_blocks(target_src_dir) or []
        # Count total fns in the cluster (regardless of body state). Used by
        # monitor to render a CUMULATIVE progress bar across restarts —
        # filled = total - stubs_at_pass_start + pass_fills_done.
        # Excludes `.gen_tests_*.rs` siblings: those are test fns (not
        # reconstruction work) and inflate the denominator misleadingly.
        _total_fns_in_cluster = 0
        try:
            _all_recs = _enumerate_fns_via_extractor(target_src_dir) or []
            _all_recs = [r for r in _all_recs
                          if not re.search(r"\.gen_tests_\d+\.rs$",
                                            r.get("file", ""))]
            if cluster is not None:
                _all_recs = [r for r in _all_recs
                              if _fn_belongs_to_cluster(
                                  target_src_dir, r["file"], r["fn_name"], cluster,
                                  fn_cfg_predicates=list(r.get("cfg_predicates") or []))]
            _total_fns_in_cluster = len(_all_recs)
        except Exception:
            _total_fns_in_cluster = len(stubs)  # fallback
        # Progress numbers — use the TRUE remaining stubs (pre-frontier)
        # so "already filled" reflects disk state. When the TDD driver
        # narrows this pass, surface "K in frontier" separately so the
        # log shows both the actionable size and the global stub count.
        _filled_count = max(0, _total_fns_in_cluster - _all_stubs_count)
        _frontier_suffix = (
            f", {len(stubs)} in frontier this pass"
            if tdd_frontier_keys is not None and len(stubs) != _all_stubs_count
            else ""
        )
        _log(f"  [phase-c/per-fn] pass {pass_n}/{max_passes}: "
             f"{_all_stubs_count} stub(s) remaining in cluster "
             f"'{cluster_name}' "
             f"({_filled_count}/{_total_fns_in_cluster} already filled"
             f"{_frontier_suffix}).")
        _pass_t0 = time.monotonic()
        _emit("phase_c.pass.start", data={
            "pass_n": pass_n, "cluster": cluster_name, "stubs": len(stubs),
            "max_passes": max_passes,
            "total_fns_in_cluster": _total_fns_in_cluster,
            # Disambiguate "all stubs in cluster" vs "frontier size in stubs".
            # `stubs` is the per-pass workload (frontier when TDD driver
            # narrows it). `all_stubs_remaining` is the cluster-scope count
            # the monitor needs to compute "filled / total" correctly.
            "all_stubs_remaining": _all_stubs_count,
            "filled_count": _filled_count,
        })
        progress_this_pass = 0
        filled_this_pass: list[tuple[str, str]] = []  # [(file_rel, fn_name)]
        pass_fail_floor = 0  # P7.6: this pass's failing-test floor (test phase sets it)

        # P4: per-level work queue consumed by workers — created per priority
        # level in the dispatch loop below and passed to each worker
        # explicitly. Sequential mode = 1 worker; parallel mode = N workers
        # concurrently. Workers exit when the queue is empty.

        # P8: priority-level batching. `stubs` is already leaf-first sorted
        # (descending ref-count), so equal-priority stubs are contiguous.
        # Group them into levels; the dispatch loop runs each level in
        # parallel with a barrier between levels, so a more-referenced
        # (callee) level always completes before the level depending on it —
        # each fill sees real sibling bodies in deterministic order,
        # independent of worker scheduling.
        _levels = _group_priority_levels(stubs, leaf_name_counts)

        def _worker(work_queue: "_queue_mod.Queue"):
            # B023: the per-LEVEL queue is passed explicitly — the old
            # free-var read of `_work_queue` relied on late binding across
            # the level loop, exactly the capture class B023 flags.
            nonlocal attempts, fns_filled, progress_this_pass
            while True:
                try:
                    idx, rec = work_queue.get_nowait()
                except _queue_mod.Empty:
                    return
                try:
                    _process_one_stub(idx, rec)
                except Exception as _e:
                    # Isolate worker failures so a single bad fn doesn't
                    # kill the worker thread (others keep draining the queue).
                    _log(f"  [phase-c/per-fn] worker error on "
                         f"{rec.get('file')}::{rec.get('fn_name')}: {_e}")
                    with _counters_lock:
                        errors.append(f"{rec.get('file')}::{rec.get('fn_name')}: "
                                      f"worker exception: {_e}")

        # FR-024 cross-iter exhausted skip: load the retrospectives map
        # once per pass (cheap JSON read) and use it to skip fns the LLM
        # has classified as GENUINELY_HARD or that have exhausted >=2
        # retrospectives. Saves compute and lets the pipeline complete
        # the rest of the corpus.
        _retros_cache = _load_retrospectives(project_dir)

        # B023: per-pass loop vars bound at def time (the def re-executes
        # each pass, so each pass's closure sees its own values; workers are
        # joined within the pass, but binding makes that explicit, not
        # incidental). `filled_this_pass` is bound by IDENTITY — appends in
        # here are read by the end-of-pass test phase.
        def _process_one_stub(idx, rec, *, _retros_cache=_retros_cache,
                              stubs=stubs, pass_n=pass_n,
                              filled_this_pass=filled_this_pass,
                              _idiom_blocks=_idiom_blocks):
            nonlocal attempts, fns_filled, progress_this_pass
            rel_file = rec["file"]
            fn_name = rec["fn_name"]
            container = rec.get("container", "")
            sig_text = rec.get("sig_text", "")
            if _is_fn_fully_exhausted(_retros_cache, rel_file, fn_name, container):
                _log(f"  [phase-c/per-fn] {idx}/{len(stubs)} skipped "
                     f"{rel_file}::{fn_name} [{container}] — fully exhausted "
                     "across iters (retrospective marked GENUINELY_HARD or "
                     ">=2 prior retrospectives).")
                _emit("phase_c.fn.fill.skip", data={
                    "cluster": cluster_name, "pass_n": pass_n,
                    "file": rel_file, "fn": fn_name,
                    "reason": "fully_exhausted",
                })
                return
            _check_graceful_stop(f"per-fn pass {pass_n} fn {fn_name}")
            file_path = target_src_dir / rel_file
            try:
                snapshot = file_path.read_text(encoding="utf-8")
            except Exception:
                return
            # Resolve current offsets — file may have been modified by prior
            # fills in this pass, shifting byte positions. Re-enumerate to
            # find this specific record's current location.
            loc_at = _locate_fn_by_record(file_path, fn_name, container, sig_text)
            if loc_at is None:
                # Fall back to regex locator (for tests-fixture path where
                # the extractor binary isn't built).
                loc = _locate_fn(snapshot, fn_name)
                if loc is None:
                    return
                sig_text = sig_text or loc[0]
                loc_sig_start = loc[1]
                loc_body_start = loc[2]
                loc_body_end = loc[3]
            else:
                loc_sig_start, loc_body_start, loc_body_end, sig_text = loc_at
            test_blocks = _find_tests_for_fn(
                tests_dir_files, fn_name,
                file_rel=rel_file, src_dir=_src_for_macros,
                container=container,
            )
            # Crate idioms ranked by THIS fn's signature tokens.
            crate_idioms_section = _extract_crate_idioms(
                _idiom_blocks, rel_file, target_sig_text=sig_text,
                max_idioms=5,
            )
            _fn_t0 = time.monotonic()
            _emit("phase_c.fn.fill.start", data={
                "cluster": cluster_name, "pass_n": pass_n,
                "file": rel_file, "fn": fn_name,
                "tests_attached": len(test_blocks),
                "sig_chars": len(sig_text),
            })

            # Notes loaded inside the fix-loop on every iteration so that
            # (a) notes added during this fn's own fix loop become visible
            # to later iterations, and (b) the relevance filter (P3.4) can
            # use the CURRENT cargo error excerpt to prioritise notes whose
            # error codes / keywords match.
            notes_section = ""

            # === Inner fix loop (slice 8a f2: unit fn + _drive_fix_loop —
            # the driver owns attempt counting, the FloorStall gauge, and the
            # stall/repeated terminations; this unit owns prompt/splice/lock/
            # verify/accept/revert) ===
            success = False
            last_response = ""
            last_body = ""
            error_excerpt = ""
            attempts_log: list[str] = []
            first_failed_body = ""
            first_error_excerpt = ""
            # P2: history of (body, error_sig, outcome) for THIS fn's fix loop.
            # Surfaced to the model in subsequent fix prompts so it sees the
            # search space it has already explored.
            per_fn_history: list[dict] = []

            def _attempt_fn_fill(fix_attempt: int) -> _FixOutcome:
                nonlocal attempts, sig_text, notes_section, last_response, \
                    last_body, error_excerpt, first_failed_body, \
                    first_error_excerpt
                with _counters_lock:
                    attempts += 1
                cur_text = file_path.read_text(encoding="utf-8")
                # Slice 8a: one gather per attempt — notes re-filtered with the
                # LATEST cargo error (P3.4), TYPES_IN_SCOPE re-selected from
                # sig + container + where + the PREVIOUS BODY (types the last
                # attempt actually manipulated — new) + the current error
                # (Plan 9 F4: E0277/E0599/E0046/E0053 routinely name a
                # trait/type the signature doesn't mention).
                _fn_ctx = _gather_fn_fix_context(
                    file_text=cur_text, file_rel=rel_file, fn_name=fn_name,
                    sig_text=sig_text, body_text=last_body,
                    error_excerpt=error_excerpt, container=container,
                    type_records=_crate_type_records, fr_path=fr_path,
                )
                notes_section = _fn_ctx.notes_section
                _types_section_eff = _fn_ctx.types_section
                if fix_attempt == 1:
                    prompt = _build_per_fn_prompt(
                        file_text=cur_text, fn_name=fn_name, sig_text=sig_text,
                        test_blocks=test_blocks, module_name=module_name,
                        cluster_name=cluster_name, file_rel=rel_file,
                        macros_section=macros_section,
                        notes_section=notes_section,
                        container=container,
                        types_section=_types_section_eff,
                        invariants_section=invariants_section,
                        fr_path=fr_path,
                        siblings_section=_collect_sibling_filled_bodies(
                            cur_text, fn_name, container,
                        ),
                        crate_idioms_section=crate_idioms_section,
                    )
                else:
                    prompt = _build_per_fn_fix_prompt(
                        file_text=cur_text, fn_name=fn_name, sig_text=sig_text,
                        test_blocks=test_blocks, module_name=module_name,
                        cluster_name=cluster_name, file_rel=rel_file,
                        prev_body=last_body, error_excerpt=error_excerpt,
                        macros_section=macros_section,
                        notes_section=notes_section,
                        container=container,
                        types_section=_types_section_eff,
                        invariants_section=invariants_section,
                        # P3.1: open the sig-change escape hatch after multiple
                        # compile failures. The model can return an additional
                        # <SIGNATURE> block; we splice it only when fn is non-pub.
                        allow_sig_change=(fix_attempt >= 5),
                        fr_path=fr_path,
                        attempt_history=per_fn_history,  # P2
                        # FR-024: inject prior retrospective guidance (if any)
                        # so the fix attempt starts from "what to try instead"
                        # rather than naive retry-with-errors.
                        retrospective_guidance=(_retros_cache.get(
                            _retro_key(rel_file, fn_name, container), {}
                        ).get("guidance", "")),
                        # FR-024 fix 3: hoist sibling fn impls (same name,
                        # different container) that are already filled —
                        # best source of truth for the target body shape.
                        siblings_section=_collect_sibling_filled_bodies(
                            cur_text, fn_name, container,
                        ),
                        crate_idioms_section=crate_idioms_section,
                    )

                body, last_response = _ask_per_fn_body(prompt, fn_name, attempts_log, sig_text=sig_text)
                if body is None:
                    # Model gave no usable response — stop retrying this fn.
                    _emit("phase_c.fn.fill.skip", data={
                        "cluster": cluster_name, "pass_n": pass_n,
                        "file": rel_file, "fn": fn_name,
                        "fix_attempt": fix_attempt,
                        "reason": "empty_response",
                    })
                    return _FixOutcome("stop")

                # P3.1: if the model proposed a sig change, try it FIRST so
                # the body splice operates on the updated sig. Rolls back if
                # the resulting cargo check still fails (revert via snapshot).
                if fix_attempt >= 5:
                    proposed_sig = _extract_signature_block(last_response)
                    if proposed_sig and _replace_fn_signature(file_path, fn_name, proposed_sig):
                        # Refresh sig_text so subsequent prompts use the new one.
                        new_loc = _locate_fn(file_path.read_text(encoding="utf-8"), fn_name)
                        if new_loc is not None:
                            sig_text = new_loc[0]
                        _log(f"  [phase-c/per-fn] {fn_name}: sig swapped (attempt "
                             f"{fix_attempt}): {sig_text}")

                # Splice by exact byte offset. Re-enumerate just before
                # splicing because prior fills (or our sig swap above) may
                # have shifted offsets. When a fn's name is duplicated in a
                # file (Display::fmt vs Debug::fmt), we disambiguate by
                # container + sig_text in _locate_fn_by_record.
                # P4: hold _splice_check_lock from splice through cargo check
                # through the revert (if applicable). Other workers wait so
                # we don't corrupt target/ with concurrent cargo invocations.
                _splice_check_lock.acquire()
                _lock_released = False
                _splice_loc = _locate_fn_by_record(
                    file_path, fn_name, container, sig_text)
                if _splice_loc is not None:
                    _, _bs, _be, _ = _splice_loc
                    ok, delta = _splice_fn_body_at(file_path, _bs, _be, body)
                else:
                    # Fallback: regex-based locator. Less accurate for
                    # same-name collisions but acceptable when the syn
                    # binary isn't available.
                    ok, delta = _splice_fn_body(file_path, fn_name, body)
                if not ok:
                    _splice_check_lock.release()
                    _lock_released = True
                    # splice rejected — usually because another worker filled
                    # this fn between our read and our splice (race) OR
                    # because the body was no longer `unimplemented!()` for
                    # some other reason. Distinguish via a quick re-read.
                    _race_detected = False
                    try:
                        _cur_text = file_path.read_text(encoding="utf-8")
                        _race_loc = _locate_fn_by_record(
                            file_path, fn_name, container, sig_text)
                        if _race_loc is not None:
                            _, _race_bs, _race_be, _ = _race_loc
                            _race_cur_body = _cur_text[_race_bs:_race_be]
                            if not _is_unimplemented_body(_race_cur_body):
                                _race_detected = True
                    except Exception:
                        pass
                    _ctx = f" [{container}]" if container else ""
                    if _race_detected:
                        _log(f"  [phase-c/per-fn] GUARD POSSIBLE RACE — "
                             f"{rel_file}::{fn_name}{_ctx} already filled by "
                             "another worker; this worker's LLM output "
                             "discarded.")
                    else:
                        _log(f"  [phase-c/per-fn] splice rejected for "
                             f"{rel_file}::{fn_name}{_ctx} (body no longer "
                             "a stub).")
                    _emit("phase_c.fn.fill.skip", data={
                        "cluster": cluster_name, "pass_n": pass_n,
                        "file": rel_file, "fn": fn_name,
                        "container": container,
                        "fix_attempt": fix_attempt, "body_chars": len(body),
                        "reason": "race_lost" if _race_detected else "splice_rejected",
                    })
                    return _FixOutcome("stop")

                rc, cargo_out = _run_cargo(check_cmd, target_src_dir, cluster_env)
                # Plan 10 U2: compile is only a pre-gate — accept the fn only if
                # its OWN tests pass. The SHARED _u2_test_gate decides; on a test
                # failure it routes through the same fix loop as a compile error
                # (failing-test set as the stall signature).
                _u2_fail_sig: frozenset | None = None
                if rc == 0:
                    _g = _u2_test_gate(
                        (tdd_frontier_tests or {}).get((rel_file, fn_name), set()),
                        tdd_test_cmd, target_src_dir, cluster_env)
                    if _g is not None:
                        _u2_fail_sig, cargo_out = _g
                        # #3 co-dependency: this fn COMPILES but an attributed test
                        # still fails BECAUSE a sibling it also calls is an unfilled
                        # stub (panic "not implemented") — not this fn's fault. Keep
                        # the compiling body and defer to the outer TDD loop (which
                        # re-runs once the whole pass fills); reverting to a stub
                        # would be strictly worse. An assertion mismatch is real →
                        # fall through to the normal fix path.
                        if _u2_fail_sig and _failure_hit_unfilled_stub(cargo_out):
                            _ctx2 = f" [{container}]" if container else ""
                            _log(f"  [phase-c/per-fn] {idx}/{len(stubs)} kept "
                                 f"{rel_file}::{fn_name}{_ctx2} (compiles; attributed "
                                 "test blocked by an unfilled sibling stub — deferred "
                                 "to outer loop).")
                            _u2_fail_sig = None
                if rc == 0 and _u2_fail_sig is None:
                    _splice_check_lock.release()
                    _lock_released = True
                    _ctx = f" [{container}]" if container else ""
                    _log(f"  [phase-c/per-fn] {idx}/{len(stubs)} filled "
                         f"{rel_file}::{fn_name}{_ctx} "
                         f"(+{delta} bytes, body={len(body)}"
                         + (f", fix-attempts={fix_attempt}" if fix_attempt > 1 else "")
                         + ")")
                    _emit("phase_c.fn.fill.done", duration_ms=int(
                            (time.monotonic() - _fn_t0) * 1000),
                          data={
                        "cluster": cluster_name, "pass_n": pass_n,
                        "file": rel_file, "fn": fn_name,
                        "body_chars": len(body), "bytes_delta": delta,
                        "fix_attempts": fix_attempt,
                        "recovered": fix_attempt > 1,
                        "success": True,
                    })
                    # Recovered fix-loop → extract a self-note (model writes
                    # for itself). High-signal moment: we know the lesson
                    # actually worked.
                    if fix_attempt > 1 and fr_path is not None and first_failed_body:
                        note = _extract_self_note(
                            failed_body=first_failed_body,
                            error_excerpt=first_error_excerpt,
                            fixed_body=body,
                        )
                        # P6.4: tag the note with the error codes from the
                        # original failure so it can be surfaced next time
                        # any of those codes recurs.
                        _ecodes = set(re.findall(r"\bE\d{3,4}\b",
                                                  first_error_excerpt or ""))
                        if note and _append_model_note(fr_path, note,
                                                       error_codes=_ecodes):
                            _log(f"  [model-notes] +1 note (recovered): {note[:80]}...")
                            _emit("phase_c.note.added", data={
                                "cluster": cluster_name, "pass_n": pass_n,
                                "file": rel_file, "fn": fn_name,
                                "kind": "recovered", "preview": note[:200],
                            })
                    return _FixOutcome("accepted")

                # Capture first-failure context for note extraction later.
                if fix_attempt == 1:
                    first_failed_body = body
                    first_error_excerpt = _focus_cargo_errors(cargo_out, rel_file)

                # P5.5: track error codes seen so we can promote recurring
                # ones to every subsequent prompt.
                _record_error_codes_seen(cargo_out)
                # Plan 10 U2: when the failure is a TEST failure (not compile),
                # the stall signature is the failing-test set so floor-stall
                # works (a test output has no E-codes → _cargo_err_sig would be
                # empty and never stall).
                cur_err_sig = (_u2_fail_sig if _u2_fail_sig is not None
                               else _cargo_err_sig(cargo_out))
                # P2: record this failed attempt for the prior-attempts block.
                # We store the focused excerpt (already tier-sliced) so the
                # next prompt sees what really failed without re-paying budget.
                _focused = _focus_cargo_errors(cargo_out, rel_file, max_chars=_FIX_ERR_CAP)
                per_fn_history.append({
                    "attempt": fix_attempt,
                    "body": body,
                    "error_sig": _focused,
                    "outcome": "compile_fail",
                })
                # P7.2: floor-stall metric — rustc error count (the driver's
                # FloorStall observes it and owns the stall/repeated exits).
                cur_err_count = len(cur_err_sig)
                last_body = body
                error_excerpt = _focus_cargo_errors(cargo_out, rel_file)
                # Revert to snapshot before returning; the model will see
                # the file as unimplemented again but with last_body + errors
                # in the prompt. (Reverting unconditionally — the exhaustion
                # block below restores + annotates anyway, so the end state
                # on a stall/repeated exit is identical to the old in-loop
                # ordering.)
                file_path.write_text(snapshot, encoding="utf-8")
                if not _lock_released:
                    _splice_check_lock.release()
                    _lock_released = True
                _log(f"  [phase-c/per-fn] {fn_name}: compile failed "
                     f"(attempt {fix_attempt}); re-prompting with errors.")
                return _FixOutcome("failed", err_sig=cur_err_sig,
                                   err_count=cur_err_count)

            def _on_fn_failure(attempt_n: int, outcome: _FixOutcome,
                               gauge: _FloorStall) -> None:
                _emit("loop.iter", data={
                    "name": "phase_c.per_fn",
                    "cluster": cluster_name, "pass_n": pass_n,
                    "file": rel_file, "fn": fn_name,
                    "iter": attempt_n,
                    "metric_new": outcome.err_count,
                    "metric_best": gauge.best,
                    "improved": gauge.stalls == 0,
                    "stalled": gauge.stalls,
                })

            def _on_fn_stall(attempt_n: int, gauge: _FloorStall) -> None:
                _log(f"  [phase-c/per-fn] {fn_name}: floor stalled at "
                     f"{gauge.best} errors for {gauge.stalls} iter(s) — "
                     f"stopping fix loop (attempt {attempt_n}).")

            def _on_fn_repeated(attempt_n: int, gauge: _FloorStall) -> None:
                _log(f"  [phase-c/per-fn] {fn_name}: same error set after "
                     f"attempt {attempt_n} — stopping fix loop.")

            success, fix_attempt = _drive_fix_loop(
                _attempt_fn_fill,
                hard_cap=_PER_FN_FIX_CAP,
                max_stalls=_effective_stalls(
                    _PER_FN_FIX_MAX_STALLS, _module_of(rel_file)),
                stop_context=f"per-fn fix for {fn_name}",
                on_failure=_on_fn_failure,
                on_stall=_on_fn_stall,
                on_repeated=_on_fn_repeated,
            )

            if not success:
                # Restore the snapshot first (clears whatever broken body the
                # loop left in place), then splice an annotated stub that
                # preserves the last attempt + cargo error as comments. The
                # fn stays callable (unimplemented!() at the end) and a later
                # pass will retry — by then more siblings in the file will be
                # implemented, giving richer context. Lock the file-write
                # phase against concurrent workers.
                with _splice_check_lock:
                    file_path.write_text(snapshot, encoding="utf-8")
                    annotated_body = _make_failed_attempt_body(
                        attempted_body=last_body or first_failed_body or "",
                        error_excerpt=error_excerpt or first_error_excerpt or "",
                        fix_attempts=fix_attempt,
                    )
                    _splice_fn_body(file_path, fn_name, annotated_body)
                with _counters_lock:
                    errors.append(f"{rel_file}::{fn_name}: fix loop exhausted "
                                  f"after {fix_attempt} attempt(s)")
                _emit("phase_c.fn.fill.fail", duration_ms=int(
                        (time.monotonic() - _fn_t0) * 1000),
                      data={
                    "cluster": cluster_name, "pass_n": pass_n,
                    "file": rel_file, "fn": fn_name,
                    "fix_attempts": fix_attempt,
                    "last_error_chars": len(error_excerpt),
                    "last_error_preview": error_excerpt[:200],
                })
                # Exhausted fix-loop — extract a "what didn't work" note so the
                # model doesn't repeat the same dead-end approach on the next pass.
                if fr_path is not None and (first_failed_body or last_body):
                    note = _extract_self_note(
                        failed_body=last_body or first_failed_body,
                        error_excerpt=error_excerpt or first_error_excerpt,
                        fixed_body="",  # no fixed body — fix loop exhausted
                    )
                    # P6.4: tag exhausted-fail notes with the codes seen
                    # across the run so they re-surface for the same code.
                    _ecodes2 = set(re.findall(r"\bE\d{3,4}\b",
                                               (error_excerpt or "")
                                               + " "
                                               + (first_error_excerpt or "")))
                    if note and _append_model_note(fr_path, note,
                                                   error_codes=_ecodes2):
                        _log(f"  [model-notes] +1 note (exhausted): {note[:80]}...")
                        _emit("phase_c.note.added", data={
                            "cluster": cluster_name, "pass_n": pass_n,
                            "file": rel_file, "fn": fn_name,
                            "kind": "exhausted", "preview": note[:200],
                        })

                # FR-024 retrospective: actively diagnose why the fix
                # loop exhausted and prescribe a different strategy for
                # the next attempt. Persisted across module iterations.
                #
                # Exhaustion is PROGRESS-GATED — no hard cap on the number
                # of retros. A retro fires; if the next-best error count
                # is strictly lower than the prior best (real improvement),
                # the fn stays eligible. If it isn't, the fn is marked
                # fully exhausted on the spot.
                try:
                    _retros = _load_retrospectives(project_dir)
                    _retro_k = _retro_key(rel_file, fn_name, container)
                    _prior = _retros.get(_retro_k, {})
                    _prior_count = int(_prior.get("retrospective_count", 0))
                    _prior_best = _prior.get("best_error_count")
                    _prior_guidance = _prior.get("guidance", "")
                    _retro = _phase_c_retrospective(
                        fn_name=fn_name, sig_text=sig_text,
                        container=container, file_rel=rel_file,
                        cluster_name=cluster_name,
                        last_body=last_body or first_failed_body or "",
                        error_excerpt=error_excerpt or first_error_excerpt or "",
                        fix_attempts=fix_attempt,
                        prior_retrospective=_prior_guidance,
                    )
                    # Transient empties (llm_error / empty_response) don't
                    # update state — we never saw a real fix attempt's
                    # outcome, so they cannot count toward exhaustion.
                    _signal = _retro.get("signal", "")
                    _transient = _signal in ("llm_error", "empty_response")
                    if not _transient:
                        _cur_errs = len(_cargo_err_sig(
                            error_excerpt or first_error_excerpt or ""
                        ))
                        # First retro for this fn establishes a baseline —
                        # no prior to compare, so we never exhaust on
                        # round 1. Subsequent retros exhaust unless
                        # current count is strictly lower than prior best.
                        if _prior_best is None:
                            _new_best = _cur_errs
                            _improved = True
                        else:
                            _new_best = min(int(_prior_best), _cur_errs)
                            _improved = _cur_errs < int(_prior_best)
                        _new_count = _prior_count + 1
                        _full_exhaust = (
                            _retro.get("category") == "GENUINELY_HARD"
                            or (_prior_best is not None and not _improved)
                        )
                        _retros[_retro_k] = {
                            "category": _retro.get("category", ""),
                            "guidance": _retro.get("guidance", ""),
                            "signal": _signal,
                            "retrospective_count": _new_count,
                            "error_count": _cur_errs,
                            "best_error_count": _new_best,
                            "fully_exhausted": _full_exhaust,
                            "last_error_preview": (error_excerpt or "")[:240],
                        }
                        _save_retrospectives(project_dir, _retros)
                        _log(f"  [phase-c/retro] {fn_name} "
                             f"[{_retro.get('category','?')}] "
                             f"signal={_signal} "
                             f"errs={_cur_errs} "
                             f"(prior_best={_prior_best}, "
                             f"improved={_improved}, "
                             f"exhausted={_full_exhaust}): "
                             f"{_retro.get('guidance','')[:120]}...")
                        _emit("phase_c.retrospective", data={
                            "cluster": cluster_name, "pass_n": pass_n,
                            "file": rel_file, "fn": fn_name,
                            "category": _retro.get("category", ""),
                            "signal": _signal,
                            "retrospective_count": _new_count,
                            "error_count": _cur_errs,
                            "best_error_count": _new_best,
                            "improved": _improved,
                            "fully_exhausted": _full_exhaust,
                            "preview": _retro.get("guidance", "")[:200],
                        })
                except Exception as _re:
                    _log(f"  [phase-c/retro] failed for {fn_name}: {_re}")
                try:
                    ts = int(time.time())
                    fpath = failures_dir / f"{fn_name}-{ts}.txt"
                    fpath.write_text(
                        f"# fn: {rel_file}::{fn_name}\n"
                        f"# fix_attempts: {fix_attempt}\n"
                        f"# last_response_chars: {len(last_response)}\n"
                        f"# last_error_excerpt: {error_excerpt[:500]}\n"
                        f"{'=' * 60}\n## ATTEMPTS LOG\n{'=' * 60}\n"
                        + "\n".join(attempts_log) + "\n"
                        f"{'=' * 60}\n## LAST RESPONSE\n{'=' * 60}\n{last_response}\n"
                        f"{'=' * 60}\n## LAST ERROR EXCERPT\n{'=' * 60}\n{error_excerpt}\n",
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                _log(f"  [phase-c/per-fn] {idx}/{len(stubs)} {rel_file}::{fn_name}: "
                     f"reverted after {fix_attempt} attempt(s).")
                return  # done with this fn — next worker grabs from queue

            with _counters_lock:
                fns_filled += 1
                progress_this_pass += 1
                filled_this_pass.append((rel_file, fn_name))

        # P8 dispatch: process priority levels in order, barrier between them.
        # Within a level, spawn min(P, level size) workers (concurrency
        # self-bounds for small levels); join before the next level so the
        # callee level is fully filled before its callers start.
        for _level in _levels:
            _work_queue: _queue_mod.Queue = _queue_mod.Queue()
            for _item in _level:
                _work_queue.put(_item)
            _nw = min(_parallelism, len(_level))
            if _nw > 1:
                _workers = [
                    _threading.Thread(target=_worker, args=(_work_queue,),
                                      name=f"per-fn-worker-{i}")
                    for i in range(_nw)
                ]
                for _t in _workers:
                    _t.start()
                for _t in _workers:
                    _t.join()
            else:
                _worker(_work_queue)

        # === Per-pass cargo test ===
        # Always run at end of every pass, even when filled_this_pass is empty
        # (e.g. cluster fully filled from a prior run). A pre-filled cluster
        # without a test run silently passes through to Phase D — masking any
        # breakage introduced by other clusters' fills or by previously-stalled
        # fix loops. Run unconditionally so we always have signal.
        # Plan 4 — TDD driver runs the cluster test in its OUTER loop; the
        # per-pass run would be redundant. Skip when the caller asked us to.
        if not skip_end_pass_test:
            _check_graceful_stop(f"per-fn pass {pass_n} test phase")
            # Build the union of test files referenced by ANY fn filled this pass.
            # Only keep paths under the project's `tests/` directory — those are
            # the actual `cargo test --test {stem}` targets. Filter out anything
            # else (e.g. src/*.gen_tests_*.rs siblings, which are imported via
            # `#[path]` from src files, NOT compiled as separate test binaries —
            # passing them to `--test` makes cargo fail at command-parse time
            # with rc=101, which is what was breaking the per-pass test loop).
            test_paths: set[Path] = set()
            for rel_f, fn in filled_this_pass:
                for tf in _find_tests_for_fn(
                    tests_dir_files, fn,
                    file_rel=rel_f, src_dir=_src_for_macros,
                ):
                    p = Path(tf[0])
                    # Only direct children of `tests/` are cargo --test
                    # targets. Files under `tests/ui/`, `tests/common/`,
                    # etc. are trybuild fixtures or shared helpers, not
                    # standalone test binaries — passing them to `--test`
                    # makes cargo fail rc=101 at command-parse time.
                    if p.parent.name == "tests":
                        test_paths.add(p)
            # When nothing was filled this pass (or no test could be linked to
            # the fills), fall back to the cluster's complete tests/ surface so
            # we still get pass/fail signal for the whole cluster.
            if not test_paths:
                for p in tests_dir_files:
                    # Only direct children of `tests/` are cargo --test
                    # targets. Files under `tests/ui/`, `tests/common/`,
                    # etc. are trybuild fixtures or shared helpers, not
                    # standalone test binaries — passing them to `--test`
                    # makes cargo fail rc=101 at command-parse time.
                    if p.parent.name == "tests":
                        test_paths.add(p)
            test_stems = sorted({p.stem for p in test_paths})
            if test_stems:
                test_cmd = _build_test_cmd(test_stems, cluster)
                _log(f"  [phase-c/per-fn] pass {pass_n}: cargo test "
                     f"{len(test_stems)} test file(s): {', '.join(test_stems[:5])}"
                     + ("..." if len(test_stems) > 5 else ""))
                _test_t0 = time.monotonic()
                rc, test_out = _run_cargo(test_cmd, target_src_dir, cluster_env,
                                          timeout=300)
                _test_dur_ms = int((time.monotonic() - _test_t0) * 1000)
                _emit("phase_c.pass.test", duration_ms=_test_dur_ms, data={
                    "cluster": cluster_name, "pass_n": pass_n,
                    "stems": test_stems, "rc": rc,
                    "failed_tests": sorted(_parse_failed_tests(test_out))
                                    if rc != 0 else [],
                })
                if rc == 0:
                    _log(f"  [phase-c/per-fn] pass {pass_n}: cargo test PASSED.")
                else:
                    _log(f"  [phase-c/per-fn] pass {pass_n}: cargo test FAILED "
                         f"(rc={rc}).")
                    # === Per-pass test fix loop ===
                    # Slice 8a f4: `_attempt_test_fix` unit on `_drive_fix_loop`.
                    # Loop-C contract (pinned in tests/test_e2e_test_retry.py):
                    # NO revert — splices kept on every exit; metric = failing-
                    # test count with the floor SEEDED by the entry count
                    # (initial_floor), so a non-improving first iteration
                    # already stalls; no repeated-signature exit (no signature
                    # is passed). The dead `prev_test_sig` diagnostic of the
                    # hand-rolled loop is gone.
                    # P2: per-(file, fn) attempt history maintained across
                    # this pass's test-fix iterations. Surfaced to the model
                    # so it can see what bodies it has already tried for the
                    # same fn rather than re-deriving from scratch.
                    test_fix_history: dict[tuple[str, str], list[dict]] = {}
                    _entry_fail_count = len(_parse_failed_tests(test_out))
                    # Gauge mirror — the unit reads the running stall count
                    # (classifier escalation) and the floor (per-iter log
                    # file); updated in `_on_test_fix_failure` after each
                    # driver observation.
                    _stall_seen = {"best": _entry_fail_count, "stalls": 0}
                    _iter_ctx: dict = {"cur": 0, "fns": []}

                    def _attempt_test_fix(test_fix_attempt: int, *,
                                          pass_n: int = pass_n,
                                          filled_this_pass=filled_this_pass,
                                          test_cmd=test_cmd,
                                          test_fix_history=test_fix_history,
                                          _stall_seen=_stall_seen,
                                          _iter_ctx=_iter_ctx) -> _FixOutcome:
                        nonlocal attempts, rc, test_out
                        failed_tests = _parse_failed_tests(test_out)
                        if not failed_tests:
                            return _FixOutcome("stop")  # rc!=0 but no parseable failed tests; bail
                        cur_fail_count = len(failed_tests)
                        # P3: classifier-driven retry candidates. Normally
                        # fires only after one naive iteration stalls. But when
                        # filled_this_pass is empty (pre-filled cluster), there
                        # IS no naive baseline — the token-match path produces
                        # nothing — so the classifier must fire on iter 1.
                        classification: dict[str, dict] = {}
                        if _stall_seen["stalls"] >= 1 or not filled_this_pass:
                            classification = _classify_phase_c_test_failures(
                                test_out=test_out,
                                filled_this_pass=filled_this_pass,
                                failed_tests=sorted(failed_tests),
                                cluster_name=cluster_name,
                            )
                        fns_to_retry: list[tuple[str, str]] = []
                        if classification:
                            # Collect fn= refs only from actionable categories.
                            wanted: set[tuple[str, str]] = set()
                            for _test, info in classification.items():
                                if info["category"] not in (
                                        "BAD_FILL", "TEST_PRECONDITION_MISSING"):
                                    continue
                                ref = info["fn"]
                                if "::" in ref:
                                    rel_f, fn = ref.split("::", 1)
                                    wanted.add((rel_f.strip(), fn.strip()))
                            # When we filled something this pass, restrict to
                            # that scope (classifier may hallucinate). When
                            # nothing was filled, accept any fn the classifier
                            # names that actually exists in the workspace —
                            # the resolve happens in _locate_fn below.
                            if filled_this_pass:
                                in_scope = set(filled_this_pass)
                                fns_to_retry = [p for p in wanted if p in in_scope]
                            else:
                                fns_to_retry = list(wanted)
                            # Log a one-line summary of categories.
                            _cat_counts = _Counter_PY(
                                v["category"] for v in classification.values())
                            _log(f"  [phase-c/classifier] pass {pass_n} iter "
                                 f"{test_fix_attempt}: "
                                 + ", ".join(f"{c}={n}"
                                             for c, n in _cat_counts.items()))
                        if not fns_to_retry and filled_this_pass:
                            # Naive fallback: token match in test output.
                            for (rel_f, fn) in filled_this_pass:
                                if any(fn in t for t in failed_tests) or fn in test_out:
                                    fns_to_retry.append((rel_f, fn))
                        if not fns_to_retry and not filled_this_pass:
                            # Reverse-lookup fallback: for each failing test
                            # binary, enumerate fns it exercises via
                            # _find_tests_for_fn and feed those as candidates.
                            test_fname_set = {
                                t.split("::")[0].split("__")[0]
                                for t in failed_tests
                            }
                            for rs in target_src_dir.rglob("*.rs"):
                                rel_path = str(rs.relative_to(target_src_dir))
                                try:
                                    txt = rs.read_text(encoding="utf-8")
                                except Exception:
                                    continue
                                for m in re.finditer(
                                    r"\bfn\s+([A-Za-z_]\w*)", txt
                                ):
                                    fn = m.group(1)
                                    tfs = _find_tests_for_fn(
                                        tests_dir_files, fn,
                                        file_rel=rel_path,
                                        src_dir=_src_for_macros,
                                    )
                                    for tf in tfs:
                                        if Path(tf[0]).stem in test_fname_set:
                                            fns_to_retry.append(
                                                (rel_path, fn))
                                            break
                            # Dedupe.
                            fns_to_retry = list(dict.fromkeys(fns_to_retry))
                        if not fns_to_retry:
                            # Conservative: retry all fns filled this pass.
                            fns_to_retry = list(filled_this_pass)

                        any_progress = False
                        # P5.1 parity: this fix prompt previously carried no
                        # TYPES_IN_SCOPE and no container. Map (file, fn) →
                        # container via the syn enumerator so the self-type's
                        # definition gets injected (cf. the BoxedError gap).
                        _retry_containers = _fn_container_index(target_src_dir)
                        for rel_f, fn in fns_to_retry:
                            fp = target_src_dir / rel_f
                            try:
                                cur_text = fp.read_text(encoding="utf-8")
                            except Exception:
                                continue
                            sloc = _locate_fn(cur_text, fn)
                            if sloc is None:
                                continue
                            sig_t = sloc[0]
                            cur_body = cur_text[sloc[2]:sloc[3]]
                            error_excerpt_test = _focus_cargo_errors(
                                test_out, rel_f, max_chars=_FIX_ERR_CAP)
                            _ctx = _gather_fn_fix_context(
                                file_text=cur_text, file_rel=rel_f, fn_name=fn,
                                sig_text=sig_t, body_text=cur_body,
                                error_excerpt=error_excerpt_test,
                                container=_retry_containers.get((rel_f, fn), ""),
                                type_records=_crate_type_records,
                                fr_path=fr_path,
                            )
                            # P5.4: container filter disambiguates tests for
                            # generic fn names (fmt/new/source) — the container
                            # is known here now, so use it.
                            t_blocks = _find_tests_for_fn(
                                tests_dir_files, fn,
                                file_rel=rel_f, src_dir=_src_for_macros,
                                container=_ctx.container,
                            )
                            prompt = _build_per_fn_fix_prompt(
                                file_text=cur_text, fn_name=fn, sig_text=sig_t,
                                test_blocks=t_blocks, module_name=module_name,
                                cluster_name=cluster_name, file_rel=rel_f,
                                prev_body=cur_body, error_excerpt=error_excerpt_test,
                                macros_section=macros_section,
                                notes_section=_ctx.notes_section,
                                container=_ctx.container,
                                types_section=_ctx.types_section,
                                invariants_section=invariants_section,
                                fr_path=fr_path,  # P6.4 note recall
                                attempt_history=test_fix_history.get(  # P2
                                    (rel_f, fn), []),
                            )
                            attempts += 1
                            attempts_log_t: list[str] = []
                            new_body, _ = _ask_per_fn_body(prompt, fn, attempts_log_t, sig_text=sig_t)
                            if not new_body:
                                continue
                            # allow_nonstub: every test-retry candidate is an
                            # already-FILLED fn — without it the splice is
                            # always rejected and the whole loop is a silent
                            # no-op (same bug class as the session-58
                            # _apply_per_fn_fixes find; caught by the f4 pins).
                            ok2, _ = _splice_fn_body(fp, fn, new_body,
                                                     allow_nonstub=True)
                            if ok2:
                                any_progress = True
                                _log(f"  [phase-c/per-fn] test-fix: re-spliced "
                                     f"{rel_f}::{fn} (attempt {test_fix_attempt}).")
                                # P2: record this attempt's body + the error
                                # excerpt that PROMPTED the retry. We can't
                                # bind the new error until after the test run
                                # below; bind a placeholder now and update
                                # with the real outcome later.
                                test_fix_history.setdefault(
                                    (rel_f, fn), []).append({
                                        "attempt": test_fix_attempt,
                                        "body": new_body,
                                        "error_sig": _focus_cargo_errors(
                                            test_out, rel_f, max_chars=_FIX_ERR_CAP),
                                        "outcome": "test_fail",
                                    })
                        if not any_progress:
                            _log(f"  [phase-c/per-fn] pass {pass_n}: no fn could "
                                 f"be re-spliced for failing tests — stopping.")
                            return _FixOutcome("stop")

                        # Re-run cargo check (cheap) then cargo test.
                        rc_c, out_c = _run_cargo(check_cmd, target_src_dir,
                                                 cluster_env)
                        if rc_c != 0:
                            _log(f"  [phase-c/per-fn] pass {pass_n}: test-fix "
                                 f"broke cargo check; leaving for next pass.")
                            return _FixOutcome("stop")
                        rc, test_out = _run_cargo(test_cmd, target_src_dir,
                                                  cluster_env, timeout=300)
                        if rc == 0:
                            _log(f"  [phase-c/per-fn] pass {pass_n}: cargo test "
                                 f"PASSED after test-fix attempt {test_fix_attempt}.")
                            return _FixOutcome("accepted")
                        new_fail_count = len(_parse_failed_tests(test_out))
                        # P4: persist this iter's cargo test output for post-hoc.
                        _write_phase_c_test_fix_log(
                            project_dir=project_dir,
                            cluster_name=cluster_name,
                            pass_n=pass_n,
                            fix_iter=test_fix_attempt,
                            test_out=test_out,
                            failed_tests=sorted(_parse_failed_tests(test_out)),
                            fns_retried=fns_to_retry,
                            cur_fail_count=cur_fail_count,
                            new_fail_count=new_fail_count,
                            best_fail_count=min(_stall_seen["best"], new_fail_count),
                            stall=_stall_seen["stalls"],  # value before this iter's observation
                        )
                        _iter_ctx["cur"] = cur_fail_count
                        _iter_ctx["fns"] = list(fns_to_retry)
                        return _FixOutcome("failed", err_count=new_fail_count)

                    def _on_test_fix_failure(attempt_n: int, outcome: _FixOutcome,
                                             gauge: _FloorStall, *,
                                             pass_n: int = pass_n,
                                             _stall_seen=_stall_seen,
                                             _iter_ctx=_iter_ctx) -> None:
                        # P5: expose the signal as an event for retro tooling.
                        _emit("phase_c.test_fix.iter", data={
                            "cluster": cluster_name, "pass_n": pass_n,
                            "iter": attempt_n,
                            "prev_fail_count": _iter_ctx["cur"],
                            "new_fail_count": outcome.err_count,
                            "delta": outcome.err_count - _iter_ctx["cur"],
                            "best": gauge.best,
                            "improved": gauge.stalls == 0,
                            "stalled": gauge.stalls,
                            "fns_retried": [f"{r}::{f}" for r, f in _iter_ctx["fns"]],
                        })
                        _log(f"  [phase-c/per-fn] pass {pass_n} test-fix iter "
                             f"{attempt_n}: "
                             f"{_iter_ctx['cur']}→{outcome.err_count} failures "
                             f"(floor={gauge.best}, stall={gauge.stalls}).")
                        _stall_seen["best"] = gauge.best
                        _stall_seen["stalls"] = gauge.stalls

                    def _on_test_fix_stall(attempt_n: int, gauge: _FloorStall, *,
                                           pass_n: int = pass_n) -> None:
                        _log(f"  [phase-c/per-fn] pass {pass_n}: failure "
                             f"floor stalled at {gauge.best} for "
                             f"{gauge.stalls} iter(s) — stopping test-fix loop.")

                    _drive_fix_loop(
                        _attempt_test_fix,
                        hard_cap=_PER_PASS_TEST_FIX_HARD_CAP,
                        max_stalls=_effective_stalls(
                            _PER_PASS_TEST_FIX_MAX_STALLS,
                            _converge_in_cluster(cluster) or module_name),
                        stop_context="test fix",
                        on_failure=_on_test_fix_failure,
                        on_stall=_on_test_fix_stall,
                        initial_floor=_entry_fail_count,
                    )
                    # P7.6 floor for the pass-level gauge: 0 once the tests
                    # pass, else the best (lowest) failure count seen. Replaces
                    # the locals()-probe of the old loop, which also leaked a
                    # stale count into LATER passes whose tests passed outright.
                    pass_fail_floor = 0 if rc == 0 else int(_stall_seen["best"] or 0)

        # P7.6: capture last-pass failure count for the pass-level floor.
        # If the test phase ran, `best_fail_count` reflects the last cargo
        # test outcome of this pass; otherwise zero (no failing tests yet).
        _pass_last_fail_count = pass_fail_floor
        _remaining_pass = len(_scan_unimplemented_for_cluster(
            target_src_dir, cluster))
        _pass_metric = _remaining_pass + _pass_last_fail_count
        _pass_gauge.observe(_pass_metric)
        _emit("phase_c.pass.done", duration_ms=int(
                (time.monotonic() - _pass_t0) * 1000),
              data={
            "pass_n": pass_n, "cluster": cluster_name,
            "fns_filled": progress_this_pass, "errors_so_far": len(errors),
            "remaining": _remaining_pass,
            "pass_metric": _pass_metric,
            "pass_metric_best": _pass_gauge.best,
            "pass_stalled": _pass_gauge.stalls,
        })
        if progress_this_pass == 0:
            _log(f"  [phase-c/per-fn] pass {pass_n} made no progress; stopping.")
            break
        if _pass_gauge.stalled(_pass_max_stalls):
            _log(f"  [phase-c/per-fn] cluster {cluster_name}: pass-level floor "
                 f"stalled at metric={_pass_gauge.best} for {_pass_gauge.stalls} "
                 f"pass(es) — stopping early at pass {pass_n}/{max_passes}.")
            break

    bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    remaining = len(_scan_unimplemented_for_cluster(target_src_dir, cluster))
    _emit("phase_c.cluster.done", data={
        "cluster": cluster_name,
        "fns_filled": fns_filled,
        "bytes_filled": bytes_after - bytes_before,
        "llm_calls": attempts,
        "remaining_stubs": remaining,
        "errors": len(errors),
    })
    return {
        "fns_filled": fns_filled + _carryover.get("fns_filled_via_impl", 0),
        "bytes_filled": bytes_after - bytes_before,
        "attempts": attempts,
        "errors": errors,
        "remaining": remaining,
        "impls_filled": _carryover.get("impls_filled", 0),
        "impls_failed": _carryover.get("impls_failed", 0),
    }


def _merge_fills_preserving_existing(disk_text: str, model_text: str) -> str:
    """Merge model-emitted file with the on-disk file.

    Strategy: disk_text is the structural base (skeleton + any prior fills
    + all imports/types/macros/attributes). We walk it function-by-function
    and ONLY replace `unimplemented!()` bodies with the corresponding body
    from model_text. Everything else from disk is preserved verbatim —
    imports, type definitions, module declarations, attributes, comments.

    This is the safe direction: the model can emit a thin file that drops
    the surrounding context, but we never lose what's already on disk.
    The model contributes function bodies only.

    Matching is by function signature line. If the model didn't return a
    given function (or returned it with `unimplemented!()` still), the disk
    body is kept — Phase D will pick up any residuals.
    """
    fn_re = re.compile(
        r'((?<![A-Za-z_0-9])(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
        r'(?:const\s+)?fn\s+[A-Za-z_]\w*[^{]*\{)',
    )

    def extract_fns(text: str) -> list[tuple[str, str, int, int]]:
        """Return [(signature_line, body, body_start, body_end), ...] for each fn.

        body_start is the offset of the first char inside the opening `{`.
        body_end is the offset of the closing `}` itself (exclusive end of body).
        """
        out: list[tuple[str, str, int, int]] = []
        for m in fn_re.finditer(text):
            body_start = m.end()  # right after the opening `{`
            depth = 1
            i = body_start
            while i < len(text) and depth > 0:
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue  # malformed; skip
            body_end = i - 1  # position of the matching '}'
            body = text[body_start:body_end]
            sig_line = text[m.start():body_start].strip()
            out.append((sig_line, body, body_start, body_end))
        return out

    disk_fns = extract_fns(disk_text)
    if not disk_fns:
        # Disk has no functions to fill — either an empty or
        # non-Rust-source-ish file. Don't risk overwriting structure;
        # return disk verbatim.
        return disk_text

    # Build a map from signature → model's body, for any signature the
    # model returned that ALSO doesn't look like a fresh unimplemented stub.
    # (If the model regurgitated `unimplemented!()` for some function, that
    # adds no value over what's already on disk.)
    model_bodies: dict[str, str] = {}
    for sig, body, _bs, _be in extract_fns(model_text):
        if _is_unimplemented_body(body):
            continue
        model_bodies[sig] = body

    if not model_bodies:
        return disk_text  # model gave us nothing usable

    # Walk disk fns IN REVERSE so character offsets stay valid as we splice.
    # Replace body slice only when disk's body is unimplemented AND model
    # has a real body for the same signature.
    out = disk_text
    for sig, body, body_start, body_end in reversed(disk_fns):
        if not _is_unimplemented_body(body):
            continue  # already filled — never overwrite
        new_body = model_bodies.get(sig)
        if new_body is None:
            continue  # model didn't return this function
        out = out[:body_start] + new_body + out[body_end:]
    return out


def _leaf_first_group_order(groups: list[dict]) -> list[dict]:
    """Sort groups so leaf groups (fewest cross-group symbol overlaps) come first.

    A group with many shared target_symbols depends on more "other" groups'
    work, so should be filled later.
    """
    all_other_syms: list[set[str]] = []
    for g in groups:
        all_other_syms.append(set(g.get("target_symbols", [])))

    def overlap_count(idx: int) -> int:
        mine = set(groups[idx].get("target_symbols", []))
        if not mine:
            return 0
        count = 0
        for j, others in enumerate(all_other_syms):
            if j == idx:
                continue
            count += len(mine & others)
        return count

    indexed = list(range(len(groups)))
    indexed.sort(key=lambda i: (overlap_count(i), groups[i].get("name", "")))
    return [groups[i] for i in indexed]


def _task2_fill_groups(
    skeleton_dir: Path,
    target_src_dir: Path,
    groups: list[dict],
    project_dir: Path,
    cluster_name: str,
    module_name: str,
    functional_req: str,
    genesis: str,
    cluster: "BuildCluster | None" = None,
) -> dict:
    """Phase C: per-group fill calls.

    - Seeds target_src_dir from the skeleton on first call (idempotent).
    - For each group (leaf-first), feeds the LLM the current skeleton state +
      the group's test files + an instruction to replace `unimplemented!()`
      bodies that these tests need.
    - Merges model output with on-disk source, preserving already-filled bodies.

    Returns {'groups_processed': int, 'bytes_filled': int, 'errors': list}.
    """
    target_src_dir.mkdir(parents=True, exist_ok=True)
    # Seed: copy any skeleton file that doesn't yet exist in target_src_dir.
    for src in skeleton_dir.rglob("*.rs"):
        rel = src.relative_to(skeleton_dir)
        dst = target_src_dir / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    ordered = _leaf_first_group_order(groups)
    bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    errors: list[str] = []
    skipped_already_filled = 0

    # P3.5: cache the smallest successful fills from earlier groups so we can
    # offer them as few-shot examples on retry.
    successful_examples: list[tuple[str, str]] = []  # [(sig, body), ...]

    # P3.7: stash directory for failure triples (prompt, response, outcome).
    failures_dir = project_dir / ".skeleton" / "_failures"
    failures_dir.mkdir(parents=True, exist_ok=True)

    def _stub_count_for_group(group: dict) -> int:
        """Decide whether to skip the group's LLM call because all work is done.

        Returns 0 ONLY when we are confident no work remains:
          - the group has target_symbols
          - at least one of them resolves to a function name in target_src_dir
          - and EVERY matched function has a non-stub body
        Otherwise returns 1 (treat as needing work) — including the ambiguous
        case where the target_symbols are type names that don't map to any fn.
        That ambiguity caused F7's over-firing and is the safer default.
        """
        target_names: set[str] = set()
        for sym in group.get("target_symbols", []):
            bare = sym.rsplit("::", 1)[-1]
            target_names.add(bare)
        if not target_names:
            return 1  # no signal → run
        # Build set of ALL fn names in target_src_dir (filled + stubs).
        all_fn_names: set[str] = set()
        stub_fn_names: set[str] = set()
        fn_re = re.compile(
            r'(?:^|\n)[ \t]*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
            r'(?:const\s+)?fn\s+([A-Za-z_]\w*)[^{]*\{',
            re.MULTILINE,
        )
        for p in target_src_dir.rglob("*.rs"):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for m in fn_re.finditer(text):
                all_fn_names.add(m.group(1))
        for _rel, fn in _scan_unimplemented_functions(target_src_dir):
            stub_fn_names.add(fn)
        # How many target names map to ANY function (filled or unfilled)?
        matched_to_fn = target_names & all_fn_names
        if not matched_to_fn:
            # Targets are type names (or strings the skeleton has no fn for).
            # Can't infer from this signal. Run the group.
            return 1
        # Targets DID resolve to fns. Skip only when none of those fns are
        # currently unimplemented stubs.
        remaining_stub_targets = matched_to_fn & stub_fn_names
        return len(remaining_stub_targets)

    for g_idx, g in enumerate(ordered, 1):
        gname = g.get("name", f"group_{g_idx}")
        gtests = g.get("test_files", [])
        _log(f"  [phase-c] group {g_idx}/{len(ordered)}: {gname} "
             f"({len(gtests)} test file(s))")

        # P3.4: skip the group entirely if its target_symbols all already
        # have bodies. Saves an LLM round-trip and prevents the model from
        # being asked to "fill" things that are already done.
        remaining = _stub_count_for_group(g)
        if remaining == 0:
            _log(f"  [phase-c] group {gname}: all target symbols already "
                 f"filled — skipping LLM call.")
            skipped_already_filled += 1
            continue

        # Collect this group's test contents.
        test_blocks: list[str] = []
        for trel in gtests:
            tp = project_dir / trel
            if tp.exists():
                test_blocks.append(f'<TESTFILE path="{trel}">\n{tp.read_text(encoding="utf-8")}\n</TESTFILE>')
        if not test_blocks:
            _log(f"  [phase-c] group {gname}: no readable tests; skipping.")
            continue

        # P3.3 + Plan 4: signature-only skeleton index, additionally
        # cfg-filtered for the active cluster so the model sees only
        # items active in this cluster (reduces cognitive load).
        cur_files = _load_skeleton_files(target_src_dir)
        skeleton_index = _format_skeleton_index_for_cluster(cur_files, cluster)

        # Genesis and FR capped against the per-prompt budget.
        _genesis_capped = trim_to_line(genesis, _GENESIS_CAP,
                                       "\n... (genesis truncated)") if genesis else ""
        _fr_cap = SINGLE_CALL_CHAR_LIMIT // 4
        _fr_capped = smart_truncate(functional_req or "", total_limit=_fr_cap,
                                    head=_fr_cap // 3, tail=2 * _fr_cap // 3)

        # P3.5: few-shot examples included from attempt 2 onward.
        # B023: per-group loop vars bound at def time.
        def _build_prompt(include_examples: bool, *, gname=gname, g=g,
                          skeleton_index=skeleton_index, _fr_capped=_fr_capped,
                          test_blocks=test_blocks,
                          _genesis_capped=_genesis_capped) -> str:
            examples_block = ""
            if include_examples and successful_examples:
                ex_parts = ["Example successful fills from earlier groups in this run:"]
                # Sort by body length, take 2 smallest.
                for sig, body in sorted(successful_examples, key=lambda x: len(x[1]))[:2]:
                    ex_parts.append(f'<FN sig="{sig}">\n{body}\n</FN>')
                examples_block = "\n\n" + "\n".join(ex_parts) + "\n"
            return f"""You are filling `unimplemented!()` bodies in a Rust crate so that a specific
group of tests passes.

Crate: `{module_name}` (cluster: {cluster_name})
Group: {gname}
Group rationale: {g.get('rationale', '(none)')}

Implementation skeleton (signature-only index of every file on disk; bodies
that are `unimplemented!()` need filling, bodies that are already implemented
must be preserved verbatim):
{skeleton_index}

Functional requirements (the spec):
{_fr_capped}

Tests this group must pass:
{chr(10).join(test_blocks)}

Methodology (genesis.md):
{_genesis_capped}
{examples_block}
Output format (mandatory — anything else is silently discarded):

  <FN sig="EXACT_SIGNATURE_FROM_SKELETON">
  body code goes here, WITHOUT the surrounding braces
  </FN>

One block per function whose body you fill. The `sig` attribute must be the
function declaration text exactly as it appears in the skeleton up to (but
excluding) the opening `{{`. Do NOT include the braces. Do NOT include other
content (no markdown, no prose, no full-file replacements).

CRITICAL — omit, don't stub:
  - If you cannot produce a real implementation for a function, OMIT it
    entirely from your output. Do not emit a `<FN>` block whose body is
    `unimplemented!()`, `todo!()`, or just panics.
  - Emitting nothing for a function leaves its current `unimplemented!()`
    body intact, which is correct — a later pass will handle it.
  - Re-emitting a stub wastes our LLM budget and yields no progress.

Only emit blocks for functions you ACTUALLY fill with real implementations
that satisfy the tests above.
"""

        # Three-attempt strategy:
        #   1. Default (thinking on, no few-shot).
        #   2. think=False (no few-shot) — recovers groups thinking-mode chokes on.
        #   3. think=False with few-shot examples (if any from prior groups).
        attempt_specs: list[tuple[bool, bool, str]] = [
            (False, False, "default"),  # include_examples=False, think_off=False
            (False, True, "think=False"),
            (True, True, "think=False+few-shot"),
        ]
        fn_blocks: dict[str, str] = {}
        last_response = ""
        for ex_flag, think_off, label in attempt_specs:
            this_prompt = _build_prompt(include_examples=ex_flag)
            kwargs: dict = {"model": _TASK2_MODEL}
            if think_off:
                kwargs["think"] = False
            last_response = ask(this_prompt, **kwargs)
            fn_blocks = _extract_fn_blocks(last_response)
            if fn_blocks:
                if label != "default":
                    _log(f"  [phase-c] group {gname}: recovered via {label}.")
                break
            _log(f"  [phase-c] group {gname}: no FN blocks from {label} attempt.")

        if not fn_blocks:
            _log(f"  [phase-c] group {gname}: all attempts failed.")
            errors.append(f"{gname}: empty/unparseable response")
            # P3.7: persist the failure for inspection / future training corpus.
            try:
                ts = int(time.time())
                fpath = failures_dir / f"{gname}-{ts}.txt"
                fpath.write_text(
                    f"# group: {gname}\n# response_chars: {len(last_response)}\n"
                    f"# response_preview: {last_response[:200].replace(chr(10),' ')}\n"
                    f"{'='*60}\n## RESPONSE\n{'='*60}\n{last_response}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            continue

        # Merge: walk each file in target_src_dir, splice in any of our fn_blocks
        # whose signature matches an unimplemented body in that file.
        for src_path in sorted(target_src_dir.rglob("*.rs")):
            rel_str: str = str(src_path.relative_to(target_src_dir))
            if ".gen_tests_" in rel_str:
                continue  # protected
            disk_text = src_path.read_text(encoding="utf-8")
            merged = _merge_fn_blocks_into_disk(disk_text, fn_blocks)
            if merged != disk_text:
                src_path.write_text(merged, encoding="utf-8")
                _log(f"  [phase-c] group {gname}: updated {rel_str} "
                     f"({len(disk_text)} → {len(merged)} bytes)")
                # P3.5: cache small successful fills as future few-shot examples.
                for sig, body in fn_blocks.items():
                    if len(body) < 800 and len(successful_examples) < 20:
                        successful_examples.append((sig, body))

    bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    return {
        "groups_processed": len(ordered),
        "groups_skipped_already_filled": skipped_already_filled,
        "bytes_filled": bytes_after - bytes_before,
        "errors": errors,
    }


def _cmd_phase_a_only(project_dir: Path) -> int:
    """Standalone Phase A: run the pre-strip skeleton bake on live source.

    Per Hard Rule 11, the bake MUST run while the original source is still
    on disk (i.e. before strip). This command refuses to run if `src/` is
    missing or empty — strip has already happened and there is nothing to
    extract from.

    Returns 0 on success, non-zero on any failure step.
    """
    _log(f"\n=== Phase A (skeleton bake) for {project_dir.name} ===")
    src_dir = project_dir / "src"
    if not src_dir.exists() or not any(src_dir.rglob("*.rs")):
        _log(f"  [phase-a] no source at {src_dir} — strip already ran. "
             "Cannot bake skeleton from absent source (Hard Rule 11).")
        return 2
    module_name = project_dir.name
    res = _bake_skeleton_artifacts(project_dir, module_name)
    if not res.get("ok"):
        _log(f"  [phase-a] bake FAILED: {res.get('reason', 'unknown')}")
        return 3
    _log(f"  [phase-a] baked {len(res['clusters'])} cluster(s): "
         f"{', '.join(res['clusters'])}")
    return 0


_SKELETON_EXTRACTOR_BIN = (
    Path(__file__).parent / "tools" / "skeleton_extractor"
    / "target" / "release" / "skeleton-extractor"
)


def _locate_fn_by_record(file_path: Path, name: str, container: str, sig_text: str) -> tuple[int, int, int, str] | None:
    """Re-enumerate `file_path` and return (sig_byte_start, body_byte_start,
    body_byte_end, fresh_sig_text) for the fn matching (name, container,
    sig_text). Used by the per-fn loop to splice by EXACT byte offset
    instead of regex-locating by name (which collides when a file has
    multiple fns sharing a name — e.g. Display::fmt vs Debug::fmt).

    Re-enumeration is required because byte offsets shift after any prior
    splice in the same file. ~30 ms via the syn binary; negligible vs the
    LLM call cost.

    Disambiguation order (each tier narrows the candidate set):
      1. fn name match
      2. container match (e.g. "impl Display for X")
      3. signature similarity (exact, else normalised whitespace)
    """
    src_dir = file_path.parent
    # The extractor walks a directory tree; we want one file. Easiest path:
    # enumerate the parent directory and filter to records from this file.
    # For large crates that's wasteful; for our scale (12 files) it's fine.
    recs = _enumerate_fns_via_extractor(src_dir.parent
                                         if src_dir.name == "src" else src_dir)
    if recs is None:
        return None
    file_rel_candidates = {
        str(file_path.name),
        str(file_path).split("/src/", 1)[-1] if "/src/" in str(file_path) else str(file_path.name),
        f"src/{file_path.name}",
    }
    file_recs = [r for r in recs if r.get("file") in file_rel_candidates
                  or r.get("file", "").endswith("/" + file_path.name)
                  or r.get("file", "") == file_path.name]
    if not file_recs:
        return None
    cands = [r for r in file_recs if r.get("fn_name") == name]
    if not cands:
        return None
    if container:
        narrowed = [r for r in cands if r.get("container") == container]
        if narrowed:
            cands = narrowed
    if sig_text:
        exact = [r for r in cands if r.get("sig_text") == sig_text]
        if exact:
            cands = exact
        else:
            norm = " ".join(sig_text.split())
            close = [r for r in cands if " ".join(r.get("sig_text", "").split()) == norm]
            if close:
                cands = close
    if len(cands) != 1:
        # Ambiguous still — pick the first stub-bodied one (more likely to
        # be the one we want to fill).
        stubs = [r for r in cands if r.get("has_unimplemented_body")]
        cands = [stubs[0]] if stubs else [cands[0]]
    r = cands[0]
    return (
        int(r["sig_byte_start"]),
        int(r["body_byte_start"]),
        int(r["body_byte_end"]),
        r.get("sig_text", ""),
    )


def _enumerate_types_via_extractor(src_dir: Path) -> list[TypeRecord] | None:
    """Invoke the syn-based extractor's --enumerate-types subcommand.

    Returns a list of type records (struct/enum/type/trait) with fields and
    method names. Used by per-fn prompts to inject type definitions when a
    fn's signature references the type (P5.1).

    Each record has: file, kind, name, generics, definition, fields[],
    variants[], methods[], is_pub.
    """
    return ADAPTERS.extractor.enumerate_types(src_dir)  # slice 5a: via Extractor adapter


def _cluster_filter_type_methods(
    records: list[TypeRecord],
    cluster: "BuildCluster | None",
) -> list[TypeRecord]:
    """Drop methods whose cfg is excluded by `cluster` so TYPES_IN_SCOPE
    advertises only what compiles here (Hard Rule 8). A method is kept unless
    one of its cfg predicates evaluates definitively False; undecidable
    predicates are kept (conservative — matches _fn_belongs_to_cluster).
    Fields/variants carry no cfg today and pass through unchanged."""
    if cluster is None:
        return records
    cfg_true = set(cluster.cfg_true)
    cfg_false = set(cluster.cfg_false)
    out: list[TypeRecord] = []
    for r in records:
        kept = tuple(
            m for m in r.methods
            if not any(_eval_cfg_pred(p, cfg_true, cfg_false) is False
                       for p in m.cfg_predicates)
        )
        out.append(dataclasses.replace(r, methods=kept)
                   if len(kept) != len(r.methods) else r)
    return out




def _types_section_for_fix(
    sig_text: str,
    body_text: str,
    error_excerpt: str,
    container: str,
    impl_where: str,
    type_records: "list[TypeRecord]",
) -> str:
    """TYPES_IN_SCOPE for retry/fix prompts (P5.1 parity). Selects from
    everything that can name a type at fix time — the signature, the
    enclosing impl header (+ where clause), the CURRENT body (names the
    types the previous attempt actually manipulated), and the error
    text — then formats and caps like the initial-fill path."""
    search = "\n".join([
        sig_text or "", container or "", impl_where or "",
        body_text or "", error_excerpt or "",
    ])
    recs = _types_referenced_in_text(search, type_records)[:_TYPES_COUNT_CAP]
    full = "\n\n".join(_format_type_for_prompt(r) for r in recs)
    if len(full) > _TYPES_SECTION_CAP:  # Plan 9 F5: surface starvation
        _guard("phase-c/types_truncated",
               f"{(container or sig_text)[:50]}: type context {len(full):,} > "
               f"cap {_TYPES_SECTION_CAP:,} ({len(recs)} types)")
    return full[:_TYPES_SECTION_CAP]


def _fn_container_index(src_dir: Path) -> dict[tuple[str, str], str]:
    """(file_rel_to_src, fn_name) → container ("impl … for …" / "trait …" / "").

    Slice 8a (FnContext): the syn enumerator is the one source of truth for
    which impl owns a fn — every retry site needs this to resolve the
    self-type whose definition must be injected (the BoxedError gap). First
    record wins for duplicate names, matching the previous inline builders.
    """
    idx: dict[tuple[str, str], str] = {}
    for rec in (_enumerate_fns_via_extractor(src_dir) or []):
        idx.setdefault((rec.get("file", ""), rec.get("fn_name", "")),
                       rec.get("container", ""))
    return idx


@dataclasses.dataclass(frozen=True, slots=True)
class _FnPromptContext:
    """Per-fn prompt context assembled by `_gather_fn_fix_context` — one
    place that derives every injectable section, so a site can't silently
    omit one (slice 8a companion to the slice-8 PromptContext records)."""
    container: str
    impl_where: str
    types_section: str
    notes_section: str


def _gather_fn_fix_context(
    *,
    file_text: str,
    file_rel: str,
    fn_name: str,
    sig_text: str,
    body_text: str,
    error_excerpt: str,
    container: str,
    type_records: "list[TypeRecord]",
    fr_path: "Path | None" = None,
) -> _FnPromptContext:
    """Assemble the fix-prompt context for one fn: impl where-clause from the
    file, TYPES_IN_SCOPE from everything that names a type at fix time, and
    the relevance-filtered model notes (empty when `fr_path` is None)."""
    impl_where = _extract_impl_where_clause(file_text, container)
    types_section = _types_section_for_fix(
        sig_text, body_text, error_excerpt, container, impl_where,
        type_records)
    notes_section = ""
    if fr_path is not None:
        notes_section = _filter_model_notes_for_prompt(
            fr_path, file_rel=file_rel, fn_name=fn_name,
            file_text=file_text, current_error=error_excerpt,
        )
    return _FnPromptContext(
        container=container, impl_where=impl_where,
        types_section=types_section, notes_section=notes_section,
    )


def _extract_impl_where_clause(file_text: str, container: str) -> str:
    """For a fn whose container is `impl ... for ...` or `impl ...`,
    locate the impl block in `file_text` and return its `where` clause
    (or empty string if absent / not found).

    The syn extractor records the container header but drops the
    `where` clause. Bounds like `where E: ext::StdError` are critical
    for picking the right TYPES_IN_SCOPE entries — they tell us which
    trait's methods are callable on the generic parameter.

    Best-effort string match: works for impls written on one line
    (the skeleton extractor produces compact single-line items, so
    nearly every impl will match).
    """
    if not container or not container.startswith("impl"):
        return ""
    # The extractor's container string strips the impl's own generics:
    # the file has `impl <T, E> Context<T, E> for Result<T, E>` but
    # container is `impl Context<T, E> for Result<T, E>`. Match on the
    # post-"impl" portion to bridge the gap.
    body = container[len("impl"):].lstrip()  # e.g. "Context<T, E> for Result<T, E>"
    if not body:
        return ""
    # Build a regex that allows optional `< ... >` generics after `impl`.
    norm_target = re.sub(r"\s+", " ", body).strip()
    norm_text = re.sub(r"\s+", " ", file_text)
    # Escape regex specials in the body, then locate it preceded by
    # `impl` and optional generics on the normalised text.
    pat = re.compile(
        r"impl\s*(?:<[^>]*>\s*)?" + re.escape(norm_target),
    )
    m = pat.search(norm_text)
    if m is None:
        return ""
    start = m.start()
    end = norm_text.find("{", m.end())
    if end < 0:
        return ""
    header = norm_text[start:end]
    wm = re.search(r"\bwhere\b(.*)$", header, re.DOTALL)
    if wm:
        return wm.group(1).strip().rstrip(",").strip()
    return ""


def _types_referenced_in_text(text: str, all_types: list[TypeRecord]) -> list[TypeRecord]:
    """Return type records that the fn's prompt should expose, with one
    level of transitive expansion through fields.

    Pass 1: direct text match — types whose `name` appears in `text` as a
    standalone identifier. Picks up names mentioned in the fn signature
    or container.

    Pass 2: field-transitive expansion — for each type matched in pass 1,
    parse the type names out of its fields' Rust type strings and pull
    those records in too. This is what lets the model see `Own::by_ref()`
    when the only direct match was `Error` (whose field is
    `inner: Own<ErrorImpl>`). Without this step the model sees the field
    type as a bare name with no API and starts inventing methods.

    Word boundaries guard against `Boxed` matching when searching for
    `Box`. Field expansion is one level only — going deeper would bloat
    the prompt.
    """
    if not text or not all_types:
        return []
    by_name = {rec.name: rec for rec in all_types if rec.name}
    out: list[TypeRecord] = []
    seen_names: set[str] = set()
    for rec in all_types:
        name = rec.name
        if not name or name in seen_names:
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", text):
            out.append(rec)
            seen_names.add(name)

    # Pass 2: expand by one field-hop. For each matched type, look at the
    # type names appearing in its fields and pull those records in.
    field_hop: list[TypeRecord] = []
    for rec in list(out):
        for f in rec.fields or ():
            ty = f.ty or ""
            if not ty:
                continue
            # Extract identifier-shaped tokens from the field's type
            # string. Tokens like `Own`, `ErrorImpl`, `Backtrace` get
            # added; generics and primitives are dropped via the
            # by_name lookup (we only keep names that match real records).
            for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ty):
                if tok in seen_names:
                    continue
                hop_rec = by_name.get(tok)
                if hop_rec is None:
                    continue
                field_hop.append(hop_rec)
                seen_names.add(tok)
    out.extend(field_hop)
    return out


def _enumerate_fns_via_extractor(src_dir: Path) -> list[dict] | None:
    """Invoke the syn-based extractor's --enumerate-fns subcommand.

    The adapter now returns typed :class:`FnRecord`s (the seam is typed, slice 6).
    This wrapper feeds the dict-based per-fn / per-impl fill engine, so it serialises
    each record back to its wire dict via the model's own ``to_dict`` — keeping the
    engine (``_process_one_stub``, ``_collect_impl_blocks``, the hand-built fallback
    stub dicts) byte-identical. Each dict:
        file (str, rel path from src_dir)
        fn_name (str)
        sig_text (str)
        body_text (str)
        sig_byte_start (int), body_byte_start (int), body_byte_end (int)
        cfg_predicates (list[str])
        is_pub (bool)
        container (str): "" for free fn, "impl ..." or "trait ..."
        has_unimplemented_body (bool)
    """
    recs = ADAPTERS.extractor.enumerate_fns(src_dir)  # typed FnRecords (slice 6)
    return None if recs is None else [r.to_dict() for r in recs]


def _dispatch_families_via_extractor(src_dir: Path) -> list[DispatchFamily] | None:
    """Invoke the extractor's --enumerate-dispatch-families subcommand.

    Returns a list of family records (or None if the binary is unavailable /
    fails). Each record: method (str), traits (list[str]), macros (list[str]),
    trait_files (list[str]), macro_defs (list[str]). A family is a set of 2+
    sibling traits sharing a method that a `macro_rules!` dispatches — anyhow's
    AdhocKind/TraitKind/BoxedKind (anyhow_kind, via anyhow!) being canonical.
    Dispatch correctness is a cross-impl invariant (FR-027), so the whole
    family is co-designed in one prompt context.
    """
    return ADAPTERS.extractor.enumerate_dispatch_families(src_dir)  # slice 5a: via adapter


def _feature_gate_cfgs_via_extractor(src_dir: Path) -> list[str] | None:
    """Invoke the extractor's --enumerate-feature-gate-cfgs subcommand.

    Returns the cfg predicates that gate a nightly `#![feature(...)]` (via
    `#[cfg_attr(cond, feature(...))]`), or None if the binary is unavailable.
    A compiler cluster whose active cfg-set intersects this MUST compile with
    the nightly toolchain (FR-030) — running it on stable yields E0554.
    """
    return ADAPTERS.extractor.enumerate_feature_gate_cfgs(src_dir)  # slice 5a: via adapter




def _scan_unimplemented_records(src_dir: Path) -> list[dict] | None:
    """Return full FnRecord dicts (from the syn enumerator) for every fn whose
    body is still unimplemented. Returns None when the extractor binary isn't
    available — callers should fall back to _scan_unimplemented_functions.

    Records include `container` and `sig_text` for disambiguating same-named
    fns (Display::fmt vs Debug::fmt).
    """
    recs = _enumerate_fns_via_extractor(src_dir)
    if recs is None:
        return None
    return [r for r in recs if r.get("has_unimplemented_body")]


# --- Per-impl-block unit of work -----------------------------------------
#
# The skeleton extractor tags every fn with `container` — the impl-block (or
# trait, or mod) header text that owns the fn. Grouping fns by (file,
# container) yields an impl-block view of the codebase. The per-impl Phase C
# path operates on these blocks instead of individual fns so that the model
# sees the impl-level design decisions (lifetime parameters, where-clauses,
# trait headers) when filling each member — solving the class of failures
# where the right fn body depends on a constraint declared at the impl level
# that per-fn prompts cannot show.


def _parse_impl_block_ranges(source: str) -> "list[dict]":
    """Source-aware scan of a Rust file for impl block byte ranges.

    Returns one record per impl block:
        {
            "header_start": int,   # byte offset of the `impl` keyword
            "body_open":    int,   # byte offset of the `{` that opens the body
            "body_close":   int,   # byte offset of the matching `}`
        }

    The scanner respects comments (line and block, including nested),
    string literals (regular and raw), char literals, and lifetime
    tokens — so braces inside any of those don't confuse the
    depth counter. Used by `_collect_impl_blocks` to assign each fn
    record to the SPECIFIC impl block whose body range contains the
    fn's `sig_byte_start`, which disambiguates two impls that share an
    identical container string but differ in their where-clauses
    (e.g. `impl<'a, T> Mut<'a, T> where T: ?Sized` vs the unsized
    `impl<'a, T> Mut<'a, T>`).
    """
    n = len(source)
    out: list[dict] = []

    def _skip_line_comment(i: int) -> int:
        while i < n and source[i] != "\n":
            i += 1
        return i

    def _skip_block_comment(i: int) -> int:
        # Rust permits nested `/* … */` blocks.
        depth = 1
        i += 2
        while i < n and depth > 0:
            if source[i] == "/" and i + 1 < n and source[i+1] == "*":
                depth += 1; i += 2
            elif source[i] == "*" and i + 1 < n and source[i+1] == "/":
                depth -= 1; i += 2
            else:
                i += 1
        return i

    def _skip_string(i: int) -> int:
        # Cursor is on the opening `"`.
        i += 1
        while i < n and source[i] != '"':
            if source[i] == "\\" and i + 1 < n:
                i += 2
            else:
                i += 1
        return i + 1 if i < n else i

    def _skip_raw_string(i: int) -> int:
        # Cursor is on `r`. Match `r#…#"…"#…#`.
        j = i + 1
        hashes = 0
        while j < n and source[j] == "#":
            hashes += 1; j += 1
        if j >= n or source[j] != '"':
            return i + 1  # not a raw string, just consume `r`
        end_pat = '"' + ("#" * hashes)
        pos = source.find(end_pat, j + 1)
        return pos + len(end_pat) if pos != -1 else n

    def _skip_char_or_lifetime(i: int) -> int:
        # Cursor is on `'`. Could be `'x'` (char) or `'a` (lifetime).
        j = i + 1
        if j < n and (source[j].isalpha() or source[j] == "_"):
            k = j
            while k < n and (source[k].isalnum() or source[k] == "_"):
                k += 1
            if k < n and source[k] == "'":
                return k + 1  # char literal `'A'`
            return k  # lifetime `'a` — already past
        # Escaped char or symbol literal.
        if j < n and source[j] == "\\" and j + 1 < n:
            j += 2
        elif j < n:
            j += 1
        if j < n and source[j] == "'":
            return j + 1
        return j

    def _is_word_boundary(i: int) -> bool:
        if i == 0:
            return True
        prev = source[i - 1]
        return not (prev.isalnum() or prev == "_")

    i = 0
    while i < n:
        ch = source[i]
        # Whitespace.
        if ch.isspace():
            i += 1; continue
        # Comments.
        if ch == "/" and i + 1 < n and source[i+1] == "/":
            i = _skip_line_comment(i); continue
        if ch == "/" and i + 1 < n and source[i+1] == "*":
            i = _skip_block_comment(i); continue
        # Strings.
        if ch == '"':
            i = _skip_string(i); continue
        if ch == "r" and i + 1 < n and source[i+1] in ('"', "#"):
            i = _skip_raw_string(i); continue
        # Char / lifetime.
        if ch == "'":
            i = _skip_char_or_lifetime(i); continue
        # Impl keyword.
        if (ch == "i" and source.startswith("impl", i)
                and _is_word_boundary(i)
                and (i + 4 >= n
                     or not (source[i+4].isalnum() or source[i+4] == "_"))):
            header_start = i
            j = i + 4
            # Walk to the `{` that opens the impl body — same scanner so
            # `<…>` and `where … { … }` clauses (in generics' const
            # expressions, theoretical) don't fool us.
            body_open = -1
            while j < n:
                cj = source[j]
                if cj.isspace(): j += 1; continue
                if cj == "/" and j + 1 < n and source[j+1] == "/":
                    j = _skip_line_comment(j); continue
                if cj == "/" and j + 1 < n and source[j+1] == "*":
                    j = _skip_block_comment(j); continue
                if cj == '"':
                    j = _skip_string(j); continue
                if cj == "r" and j + 1 < n and source[j+1] in ('"', "#"):
                    j = _skip_raw_string(j); continue
                if cj == "'":
                    j = _skip_char_or_lifetime(j); continue
                if cj == "{":
                    body_open = j; break
                if cj == ";":
                    # `impl X;` is not valid Rust; abort this candidate.
                    body_open = -1; break
                j += 1
            if body_open == -1:
                i = j; continue
            # Walk to matching `}`.
            depth = 1
            k = body_open + 1
            while k < n and depth > 0:
                ck = source[k]
                if ck == "/" and k + 1 < n and source[k+1] == "/":
                    k = _skip_line_comment(k); continue
                if ck == "/" and k + 1 < n and source[k+1] == "*":
                    k = _skip_block_comment(k); continue
                if ck == '"':
                    k = _skip_string(k); continue
                if ck == "r" and k + 1 < n and source[k+1] in ('"', "#"):
                    k = _skip_raw_string(k); continue
                if ck == "'":
                    k = _skip_char_or_lifetime(k); continue
                if ck == "{": depth += 1; k += 1; continue
                if ck == "}":
                    depth -= 1; k += 1
                    if depth == 0:
                        out.append({
                            "header_start": header_start,
                            "body_open": body_open,
                            "body_close": k - 1,
                        })
                        i = k; break
                    continue
                k += 1
            else:
                i = k
            continue
        i += 1
    return out


def _collect_impl_blocks(src_dir: Path) -> list[dict] | None:
    """Group every fn record into its impl/trait/mod container.

    Disambiguation: blocks are keyed by the FULL impl header (text from
    source between the `impl` keyword and the opening `{`), not by the
    container string the extractor records. Two impls with identical
    `impl Foo<T>` heads but different `where T: …` clauses are now
    separate blocks instead of collapsing into one — this fixes the
    Mut::read class of failure where the impl-level `T: ?Sized` was
    invisible to the model.

    Returns one record per (file, header_text) pair:
        {
            "file": str,
            "container": str,            # FULL impl header text including
                                          # where-clauses (or "" for free
                                          # fns / fallback when source
                                          # parsing failed).
            "members": [fn_record, …],   # ordered by source byte position
            "stub_members": [fn_record, …],  # subset where body is unimplemented
            "related_containers": [str, …],  # other impl blocks in the SAME
                                              # file whose trait name matches
                                              # this block's (lets the prompt
                                              # show e.g. both impl AsRef<T1>
                                              # and impl AsRef<T2> for X side
                                              # by side).
        }

    Returns None when the extractor binary isn't available.
    """
    recs = _enumerate_fns_via_extractor(src_dir)
    if recs is None:
        return None

    # Step 1 — per file: parse the source to get impl block byte ranges,
    # then assign each fn record to the block whose body range contains
    # its sig_byte_start. Caches parsed ranges so we don't re-parse the
    # same file once per fn record.
    file_to_source: dict[str, str] = {}
    file_to_impl_ranges: dict[str, list[dict]] = {}
    def _source_for(rel: str) -> str:
        if rel not in file_to_source:
            p = src_dir / rel
            try:
                file_to_source[rel] = p.read_text(encoding="utf-8")
            except Exception:
                file_to_source[rel] = ""
        return file_to_source[rel]
    def _ranges_for(rel: str) -> "list[dict]":
        if rel not in file_to_impl_ranges:
            file_to_impl_ranges[rel] = _parse_impl_block_ranges(_source_for(rel))
        return file_to_impl_ranges[rel]

    # Step 2 — group fns by (file, header_text). header_text is the
    # actual source slice for the impl block — includes generics,
    # where-clauses, `for Trait` headers verbatim.
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in recs:
        rel = r.get("file", "")
        sig_start = int(r.get("sig_byte_start", 0))
        # Find the impl block whose body range contains this sig.
        header_text = ""
        for rng in _ranges_for(rel):
            if rng["body_open"] < sig_start < rng["body_close"]:
                src = _source_for(rel)
                # Header slice = [impl keyword .. opening `{`).
                header_text = src[rng["header_start"]:rng["body_open"]].rstrip()
                break
        if not header_text:
            # Fallback to the extractor's container string when source
            # parsing missed (e.g. fn in a trait, free fn, or our
            # scanner failed). Keeps legacy behaviour for those cases.
            header_text = r.get("container", "") or ""
        key = (rel, header_text)
        groups.setdefault(key, []).append(r)

    # Step 3 — render block records. `related_containers` lookup uses
    # the trait-name regex against the header text.
    blocks: list[dict] = []
    # We want the TRAIT NAME (`impl Trait<T> for Type`), not the
    # `Type` — use a stricter pattern for related grouping. Two impls
    # are "related" iff they share the same trait name AND target type.
    trait_with_for_re = re.compile(
        r"\bimpl\s*(?:<[^>]*>)?\s+([A-Za-z_:][\w:]*)\s*(?:<[^>]*>)?\s+for\b"
    )
    file_to_trait_containers: dict[str, dict[str, list[str]]] = {}
    for (file_rel, container), _members in groups.items():
        if not container:
            continue
        m = trait_with_for_re.search(container)
        if not m:
            continue
        trait_name = m.group(1)
        file_to_trait_containers.setdefault(file_rel, {}).setdefault(
            trait_name, []).append(container)
    for (file_rel, container), members in groups.items():
        members_sorted = sorted(members, key=lambda r: int(r.get("sig_byte_start", 0)))
        stub_members = [m for m in members_sorted if m.get("has_unimplemented_body")]
        related: list[str] = []
        if container:
            m = trait_with_for_re.search(container)
            if m:
                trait_name = m.group(1)
                same_trait = file_to_trait_containers.get(file_rel, {}).get(trait_name, [])
                related = [c for c in same_trait if c != container]
        blocks.append({
            "file": file_rel,
            "container": container,
            "members": members_sorted,
            "stub_members": stub_members,
            "related_containers": related,
        })
    return blocks


def _impl_block_id(file_rel: str, container: str) -> str:
    """Stable hashable id for an impl block (used as a dict key in the
    Phase C frontier mapping and as a per-block lock key)."""
    return f"{file_rel}::{container}"


# --- Crate-internal idiom extraction --------------------------------------
#
# When the model is filling a stub, the gotchas section provides GENERIC
# Rust patterns (NonNull deref, trait header signature matching, etc.). The
# generic patterns are necessary but insufficient: each crate develops its
# own internal idioms — e.g. anyhow's `Self::construct_from_*(arg, None)`
# family, its `ContextError { context, error }` wrapper, its vtable
# fn-pointer dispatch `(vtable.method)(this)` syntax. The model can only
# discover those by seeing them used in already-filled fns of the same
# file. `_extract_crate_idioms` scans the file's filled bodies, identifies
# the highest-signal idioms, and renders a compact `<CRATE_IDIOMS>`
# section the per-impl prompt injects so the model has a cheat-sheet of
# THIS crate's patterns alongside the generic gotchas.
#
# Generic over crates: there is no anyhow-specific code in here. The
# pattern detectors look for shapes (`Self::name(args)` recurrence,
# `(field.subfield)(args)` fn-pointer calls, `unsafe { x.as_ref() }`
# chains, `Struct { … }` literal construction). Anyhow happens to be a
# rich source of all of these; smaller crates emit fewer idioms and the
# section is correspondingly shorter.


_IDIOM_SELF_CALL_RE = re.compile(
    r"\bSelf::([A-Za-z_][A-Za-z_0-9]*)\s*\(([^()]{0,200}?)\)",
    re.DOTALL,
)
_IDIOM_FN_PTR_FIELD_RE = re.compile(
    r"\(\s*([A-Za-z_][\w.]+?)\s*\)\s*\(",
)
_IDIOM_STRUCT_LITERAL_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]{2,40})\s*\{\s*([^{}]{1,400})\s*\}",
    re.DOTALL,
)
_IDIOM_UNSAFE_CHAIN_RE = re.compile(
    r"unsafe\s*\{\s*([^\n{}]{1,160})\s*\}",
)


def _extract_filled_fn_bodies(impl_blocks: "list[dict]", file_rel: str) -> "list[dict]":
    """Return [{fn_name, container, body}] for every FILLED fn in `file_rel`,
    pulled from the impl block records. Free fns are dropped (they have no
    container). Used by `_extract_crate_idioms` to scan for patterns."""
    out: list[dict] = []
    for b in impl_blocks:
        if b.get("file") != file_rel:
            continue
        for m in b.get("members", []):
            if m.get("has_unimplemented_body"):
                continue
            body = (m.get("body_text") or "").strip()
            if not body:
                continue
            out.append({
                "fn_name": m.get("fn_name", ""),
                "container": b.get("container", ""),
                "body": body,
            })
    return out


def _extract_crate_idioms(
    impl_blocks: "list[dict]",
    file_rel: str,
    target_sig_text: str = "",
    max_idioms: int = 5,
) -> str:
    """Walk filled fn bodies in `file_rel` and surface up to `max_idioms`
    distinctive patterns the model would benefit from seeing while filling
    a stub in the same file.

    The `_IDIOM_*` regex here mine recurring TEXT patterns (Self::call, struct literals,
    fn-ptr field calls) to build a heuristic cheat-sheet — NOT a no-regex-on-source violation:
    this is fuzzy pattern-mining, not structured-fact extraction (which goes through syn). See
    CODE_ARCHITECTURE.md §8 R7.5.

    Returned string is the body of a `<CRATE_IDIOMS>` section — empty
    when no patterns clear the relevance bar. Each idiom is one line:

        # Pattern label — used by `fn caller_name` in this file
        <one-line code snippet>

    Detection axes (each rendered at most twice):
      1. Self::method(arg, None) family — recurrent constructor pattern.
      2. (field.subfield)(args) — fn-pointer-via-field dispatch (vtable).
      3. unsafe { … } single-line chains — pointer / NonNull manipulation.
      4. StructName { field: …, … } — wrapper-type construction.

    Relevance: prefer idioms whose symbols (Self::method name, struct
    name, field name) appear in `target_sig_text` so the model sees
    crate-specific patterns it's about to need.
    """
    fns = _extract_filled_fn_bodies(impl_blocks, file_rel)
    if not fns:
        return ""

    target_tokens = set(re.findall(r"[A-Za-z_][\w]*", target_sig_text or ""))

    # Per-axis collectors with rank score.
    candidates: list[tuple[float, str]] = []

    # Axis 1 — Self:: family. Count occurrences per method name.
    self_method_counts: dict[str, int] = {}
    self_method_examples: dict[str, tuple[str, str]] = {}  # name → (caller, snippet)
    for fn in fns:
        for m in _IDIOM_SELF_CALL_RE.finditer(fn["body"]):
            name = m.group(1)
            self_method_counts[name] = self_method_counts.get(name, 0) + 1
            self_method_examples.setdefault(name, (fn["fn_name"], m.group(0)))
    family_names = [n for n, c in self_method_counts.items() if c >= 2]
    if family_names:
        # One synthesised entry: "this file uses Self::X / Self::Y / Self::Z as constructors"
        family_list = ", ".join(f"Self::{n}" for n in sorted(family_names))
        # Show ONE concrete call from the highest-count family member.
        top = max(family_names, key=lambda n: self_method_counts[n])
        caller, snippet = self_method_examples[top]
        snippet = " ".join(snippet.split())  # collapse whitespace
        if len(snippet) > 140:
            snippet = snippet[:140] + " …"
        rel_score = 2.0 if any(n in target_tokens for n in family_names) else 1.0
        candidates.append((
            rel_score + 0.1 * sum(self_method_counts[n] for n in family_names),
            f"# Self:: family — this file's constructors: {family_list}.\n"
            f"# Example, from `fn {caller}`:\n"
            f"#   {snippet}"
        ))

    # Axis 2 — fn-pointer field dispatch `(x.y)(args)`. Group by the path.
    fnptr_counts: dict[str, int] = {}
    fnptr_examples: dict[str, tuple[str, str]] = {}
    for fn in fns:
        for m in _IDIOM_FN_PTR_FIELD_RE.finditer(fn["body"]):
            path = m.group(1).strip()
            if "." not in path:
                continue  # likely an arithmetic expression, not a vtable access
            base = path.rsplit(".", 1)[1]
            fnptr_counts[base] = fnptr_counts.get(base, 0) + 1
            fnptr_examples.setdefault(base, (fn["fn_name"], path))
    # Pick the 2 most frequent fn-pointer fields.
    for name in sorted(fnptr_counts, key=lambda n: -fnptr_counts[n])[:2]:
        if fnptr_counts[name] < 1:
            continue
        caller, path = fnptr_examples[name]
        rel_score = 2.0 if name in target_tokens else 1.0
        candidates.append((
            rel_score + 0.05 * fnptr_counts[name],
            f"# Fn-pointer field dispatch — call via the field, NOT via\n"
            f"# method syntax. Used in `fn {caller}`:\n"
            f"#   ({path})(args…)   // NOT  {path.split('.')[-1]}(args…)"
        ))

    # Axis 3 — unsafe { … } single-line chains. Surface the most common
    # SHAPE of such a chain (collapsed by token-pattern, not exact text).
    unsafe_examples: dict[str, tuple[str, str]] = {}
    unsafe_counts: dict[str, int] = {}
    for fn in fns:
        for m in _IDIOM_UNSAFE_CHAIN_RE.finditer(fn["body"]):
            chain = " ".join(m.group(1).split())
            if not chain:
                continue
            # Shape = identifier-only with `.`s preserved.
            shape = re.sub(r"[A-Za-z_][\w]*", "X", chain)
            shape = re.sub(r"\s+", " ", shape).strip()
            unsafe_counts[shape] = unsafe_counts.get(shape, 0) + 1
            unsafe_examples.setdefault(shape, (fn["fn_name"], chain))
    for shape in sorted(unsafe_counts, key=lambda s: -unsafe_counts[s])[:2]:
        if unsafe_counts[shape] < 1:
            continue
        caller, chain = unsafe_examples[shape]
        if len(chain) > 140:
            chain = chain[:140] + " …"
        rel_score = 1.0 + 0.05 * unsafe_counts[shape]
        # Bias if the chain mentions a target token (e.g. inner, vtable, ptr).
        for t in re.findall(r"[A-Za-z_][\w]*", chain):
            if t in target_tokens:
                rel_score += 0.5
                break
        candidates.append((
            rel_score,
            f"# Unsafe chain pattern — used in `fn {caller}`:\n"
            f"#   unsafe {{ {chain} }}"
        ))

    # Axis 4 — Struct literal construction. Show wrapper types the file
    # constructs that aren't trivial single-field types.
    struct_examples: dict[str, tuple[str, str]] = {}
    struct_counts: dict[str, int] = {}
    for fn in fns:
        for m in _IDIOM_STRUCT_LITERAL_RE.finditer(fn["body"]):
            name = m.group(1)
            # Skip stdlib-y names that are noise.
            if name in {"Self", "Some", "None", "Ok", "Err", "Box", "Vec",
                        "String", "Result", "Option"}:
                continue
            inner = " ".join(m.group(2).split())
            if not inner or "," not in inner and ":" not in inner:
                continue
            struct_counts[name] = struct_counts.get(name, 0) + 1
            struct_examples.setdefault(name, (fn["fn_name"], inner))
    for name in sorted(struct_counts, key=lambda n: -struct_counts[n])[:2]:
        if struct_counts[name] < 1:
            continue
        caller, inner = struct_examples[name]
        if len(inner) > 140:
            inner = inner[:140] + " …"
        rel_score = 2.0 if name in target_tokens else 1.0
        candidates.append((
            rel_score + 0.05 * struct_counts[name],
            f"# Wrapper construction — used in `fn {caller}`:\n"
            f"#   {name} {{ {inner} }}"
        ))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: -x[0])
    chosen = [snippet for _score, snippet in candidates[:max_idioms]]
    return "\n\n".join(chosen)


def _group_frontier_by_impl_block(
    frontier: "Frontier",
    impl_blocks: "list[dict]",
) -> "list[dict]":
    """Map a per-fn frontier into a per-impl-block fill plan.

    The TDD loop attributes failures to `(file, fn_name)` keys. The
    per-impl path widens each entry to the WHOLE impl block containing
    that fn — if any member of an impl block is on the frontier, the
    block becomes one fill unit covering all its still-stub members
    (not just the trapped one).

    Returns a list of `{block: impl_block_record, fns_in_frontier:
    set[str], related_blocks: list[impl_block_record]}` records, ordered
    by the position the first frontier fn appears (so TDD's leaf
    ordering carries through).
    """
    # Index impl blocks by (file, container) for quick lookup, and by
    # member fn key for the inverse mapping.
    file_container_to_block: dict[tuple[str, str], dict] = {}
    fn_to_block: dict[tuple[str, str], dict] = {}
    for b in impl_blocks:
        file_container_to_block[(b["file"], b["container"])] = b
        for m in b["members"]:
            fn_to_block[(b["file"], m["fn_name"])] = b
    plan: list[dict] = []
    seen_block_ids: set[str] = set()
    for (file_rel, fn_name), _tests in frontier.items():
        block = fn_to_block.get((file_rel, fn_name))
        if block is None:
            continue
        bid = _impl_block_id(file_rel, block["container"])
        if bid in seen_block_ids:
            # Already added this block — record the additional frontier fn.
            for entry in plan:
                if _impl_block_id(entry["block"]["file"], entry["block"]["container"]) == bid:
                    entry["fns_in_frontier"].add(fn_name)
                    break
            continue
        seen_block_ids.add(bid)
        related_blocks = []
        for rc in block.get("related_containers", []):
            rb = file_container_to_block.get((file_rel, rc))
            if rb is not None:
                related_blocks.append(rb)
        plan.append({
            "block": block,
            "fns_in_frontier": {fn_name},
            "related_blocks": related_blocks,
        })
    return plan


def _collect_impl_blocks_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[dict] | None:
    """Cluster-aware variant of `_collect_impl_blocks`. A block is included
    iff at least one of its STUB members is active under this cluster's
    cfg-set. Stub members not active in the cluster are dropped from
    `stub_members` so the per-impl prompt never asks the model to fill
    something that wouldn't compile in this cluster anyway."""
    blocks = _collect_impl_blocks(target_src_dir)
    if blocks is None:
        return None
    if cluster is None:
        return blocks
    out: list[dict] = []
    for b in blocks:
        kept_stubs = [
            m for m in b["stub_members"]
            if _fn_belongs_to_cluster(
                target_src_dir, b["file"], m["fn_name"], cluster,
                fn_cfg_predicates=list(m.get("cfg_predicates") or []))
        ]
        if not kept_stubs:
            continue
        out.append({**b, "stub_members": kept_stubs})
    return out


def _scan_unimplemented_records_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[dict] | None:
    """Cluster-aware variant of _scan_unimplemented_records.

    Returns records filtered to fns whose file/fn cfg evaluates true for
    `cluster`. Returns None if the extractor binary isn't available —
    callers should fall back to _scan_unimplemented_for_cluster (tuple form).
    """
    recs = _scan_unimplemented_records(target_src_dir)
    if recs is None:
        return None
    if cluster is None:
        return recs
    out: list[dict] = []
    skipped = 0
    for r in recs:
        if _fn_belongs_to_cluster(
                target_src_dir, r["file"], r["fn_name"], cluster,
                fn_cfg_predicates=list(r.get("cfg_predicates") or [])):
            out.append(r)
        else:
            skipped += 1
    if skipped:
        _log(f"  [phase-c/per-fn] cluster {cluster.name}: skipped "
             f"{skipped} stub(s) not active in this cluster's cfg-set.")
    return out


# ---------------------------------------------------------------------------
# Plan 4 — TDD-scoped driver: frontier extraction from cargo test output.
#
# A "frontier" maps (file_rel, fn_name) → set of test names that hit it at
# runtime. Built from cargo test output by parsing per-test stdout blocks
# for `not yet implemented` panics (and optionally assertion failures), then
# resolving panic file:line back to the containing fn via the syn extractor.
#
# The TDD outer loop in `_generate_one_cluster_tdd` uses this to drive
# what gets filled — only fns reachable from at least one failing test.
# ---------------------------------------------------------------------------


_PANIC_HEADER_RE = re.compile(
    # Modern Rust prints `thread 'NAME' (TID) panicked at FILE:LINE:COL:`
    # with an optional `(THREAD_ID)` after the name. Older Rust just has
    # `thread 'NAME' panicked at ...`. Match both. We capture the column
    # too — the skeleton extractor produces compact one-line-per-item
    # output, so multiple fns can live on the same line and only the
    # column disambiguates which one panicked.
    r"thread '([^']+)'(?:\s+\(\d+\))?\s+panicked at ([^:]+):(\d+)(?::(\d+))?:\s*\n([^\n]*)"
)
_TEST_STDOUT_HEADER_RE = re.compile(r"^----\s+(\S+)\s+stdout\s+----\s*$",
                                     re.MULTILINE)
_COMPILE_ERROR_RE = re.compile(
    r"^error(?:\[E\d+\])?:\s+([^\n]+)\n\s*-->\s*([^:]+):(\d+)(?::\d+)?",
    re.MULTILINE,
)


def _line_to_fn_record(
    file_text: str, line_no: int, fn_records: list[dict],
    col_no: int | None = None,
) -> dict | None:
    """Given file text and a 1-based (line, col), return the fn record
    whose body spans that position. Returns None if no fn contains it.

    `fn_records` should be the syn extractor's enumerate-fns output for
    the file (caller is responsible for filtering to one file's records).

    The skeleton extractor produces compact one-line-per-item output, so
    a single line can hold several fns. Without `col_no` we pick the
    FIRST fn whose body range overlaps the line — usually fine but can
    misattribute in dense files. With `col_no`, we resolve precisely.
    """
    if line_no < 1 or not fn_records:
        return None
    lines_iter = file_text.splitlines(keepends=True)
    if line_no > len(lines_iter):
        return None
    line_start = sum(len(ln) for ln in lines_iter[:line_no - 1])
    line_end = line_start + len(lines_iter[line_no - 1])
    if col_no is not None and col_no >= 1:
        byte_offset = line_start + col_no - 1
        for rec in fn_records:
            s = rec.get("sig_byte_start", 0)
            e = rec.get("body_byte_end", 0)
            if s <= byte_offset < e:
                return rec
    # Fallback: pick the first fn whose body overlaps the line range.
    for rec in fn_records:
        s = rec.get("sig_byte_start", 0)
        e = rec.get("body_byte_end", 0)
        if max(s, line_start) < min(e, line_end):
            return rec
    return None


def _extract_tdd_frontier(
    test_output: str, src_dir: Path,
) -> Frontier:
    """Parse cargo test output → frontier dict.

    Keys are (file_rel, fn_name), values are sets of test names that
    panicked inside that fn. Only `not yet implemented` panics are
    included by default — they map cleanly to a still-unfilled fn.
    Assertion-failure attribution is handled in a separate pass (see
    `_extract_tdd_assertion_frontier`).

    Returns {} when no panics could be parsed (e.g. cargo build failed
    before any test ran).
    """
    frontier: Frontier = {}
    if not test_output:
        return frontier
    # Per-file fn records cache.
    fn_records_by_file: dict[str, list[dict]] = {}
    file_text_cache: dict[str, str] = {}
    # Split output into per-test stdout sections. Each section holds the
    # panic header for that test's failures.
    sections = _TEST_STDOUT_HEADER_RE.split(test_output)
    # _TEST_STDOUT_HEADER_RE.split returns [pre, test1, body1, test2, body2, ...].
    if len(sections) < 3:
        # No per-test sections — try parsing the whole output as a single
        # block (some cargo versions don't emit `---- N stdout ----` headers
        # for single-test runs).
        sections = ["", "unknown_test", test_output]
    for i in range(1, len(sections) - 1, 2):
        test_name = sections[i].strip()
        body = sections[i + 1]
        for m in _PANIC_HEADER_RE.finditer(body):
            _thread, file_path, line_str, col_str, _panic_msg = m.group(
                1, 2, 3, 4, 5)
            # Accept every panic whose path resolves to a fn in src/. The
            # skeleton extractor replaced every fn body with
            # `unimplemented!()`, so any panic inside src/ IS from a stub.
            # Test-file panics (tests/foo.rs:N) get rejected later by the
            # fn-lookup, since we only enumerate src/ fns.
            try:
                line_no = int(line_str)
            except ValueError:
                continue
            # Resolve file_path relative to src_dir.
            p_abs = Path(file_path)
            if not p_abs.is_absolute():
                p_abs = (src_dir / file_path).resolve()
            try:
                rel = str(p_abs.relative_to(src_dir.resolve()))
            except ValueError:
                # File outside src_dir — skip (likely a panic from std).
                continue
            # Look up fn at this line.
            if rel not in fn_records_by_file:
                _all_recs = _enumerate_fns_via_extractor(src_dir) or []
                fn_records_by_file[rel] = [r for r in _all_recs
                                            if r.get("file") == rel]
            recs = fn_records_by_file[rel]
            if not recs:
                continue
            if rel not in file_text_cache:
                try:
                    file_text_cache[rel] = p_abs.read_text(encoding="utf-8")
                except Exception:
                    continue
            col_no = int(col_str) if col_str else None
            rec = _line_to_fn_record(file_text_cache[rel], line_no, recs,
                                      col_no=col_no)
            if rec is None:
                continue
            key = (rel, rec["fn_name"])
            frontier.setdefault(key, set()).add(test_name)
    return frontier


def _behavior_frontier(
    test_output: str, project_dir: Path, existing_test_files: set,
) -> "tuple[Frontier, dict[str, tuple[str, str]]]":
    """Attribute behavioral test failures (harness compiles, no stub panics —
    assertion failures etc.) to the src fns they exercise, so the fix loop can
    repair fn bodies instead of falling back to whole-file rewrites.

    For each FAILED test: locate its source via the test-name index, collect
    the identifiers its text mentions, and intersect with the extractor's fn
    records. A fn name shared by several records is attributed only to records
    whose container also shares a token with the test text.

    Returns ({(file_rel, fn): {test names}}, {test name: (test_rel, test_src)}).
    """
    failed = failing_test_names(test_output)
    if not failed:
        return {}, {}
    texts: dict[str, str] = {}
    for trel in sorted(existing_test_files):
        tp = project_dir / trel
        if tp.exists():
            with suppress(Exception):
                texts[trel] = tp.read_text(encoding="utf-8", errors="replace")
    if not texts:
        return {}, {}
    index = build_test_name_index(texts)
    by_name: dict[str, list[dict]] = {}
    for r in _enumerate_fns_via_extractor(project_dir) or []:
        f_rel = r.get("file", "")
        if f_rel in existing_test_files or _is_gen_tests_file(Path(f_rel).name):
            continue
        by_name.setdefault(r.get("fn_name", ""), []).append(r)
    frontier: Frontier = {}
    test_srcs: dict[str, tuple[str, str]] = {}
    for tname in failed:
        bare = tname.split("::")[-1].strip()
        trel = index.get(bare, "")
        if not trel or trel not in texts:
            continue
        loc = _locate_fn(texts[trel], bare)
        if loc is None:
            continue
        sig_text, _ss, _bs, _be = loc
        tsrc = f"{sig_text} {{{texts[trel][_bs:_be]}}}"
        idents = identifiers_in(tsrc)
        test_srcs[tname] = (trel, tsrc)
        for fn_name in idents & set(by_name):
            recs = by_name[fn_name]
            for rec in recs:
                if len(recs) > 1 and not (
                        identifiers_in(rec.get("container", "")) & idents):
                    continue
                frontier.setdefault((rec["file"], fn_name), set()).add(tname)
    return frontier, test_srcs


def _extract_tdd_compile_frontier(
    build_output: str, src_dir: Path,
) -> Frontier:
    """When cargo test fails because cargo BUILD failed (compile errors,
    not panics), parse error[Exxxx] messages → fn records. Same shape as
    `_extract_tdd_frontier` but with test names replaced by "<compile>"
    so the caller can distinguish.
    """
    frontier: Frontier = {}
    if not build_output:
        return frontier
    fn_records_by_file: dict[str, list[dict]] = {}
    file_text_cache: dict[str, str] = {}
    for m in _COMPILE_ERROR_RE.finditer(build_output):
        _msg, file_path, line_str = m.group(1, 2, 3)
        try:
            line_no = int(line_str)
        except ValueError:
            continue
        p_abs = Path(file_path)
        if not p_abs.is_absolute():
            p_abs = (src_dir / file_path).resolve()
        try:
            rel = str(p_abs.relative_to(src_dir.resolve()))
        except ValueError:
            continue
        if rel not in fn_records_by_file:
            _all_recs = _enumerate_fns_via_extractor(src_dir) or []
            fn_records_by_file[rel] = [r for r in _all_recs
                                        if r.get("file") == rel]
        recs = fn_records_by_file[rel]
        if not recs:
            continue
        if rel not in file_text_cache:
            try:
                file_text_cache[rel] = p_abs.read_text(encoding="utf-8")
            except Exception:
                continue
        rec = _line_to_fn_record(file_text_cache[rel], line_no, recs)
        if rec is None:
            continue
        frontier.setdefault((rel, rec["fn_name"]), set()).add("<compile>")
    return frontier


def _compile_error_frontier(
    output: str, project_dir: Path, check_cmd: str,
    env: dict, timeout: int,
) -> Frontier:
    """Structured-first compile-error frontier (R8.3): re-run the derived
    check-only command for JSON diagnostics and attribute errors→fns via
    `_frontier_from_diags`. Regex-parsing `output` remains only as the
    fallback when no structured diagnostics came back (cargo unavailable /
    JSON parse failure). rc==0 means the crate compiles — no compile
    frontier, regardless of what `output` (e.g. test failures) contains.
    """
    try:
        rc, _rendered, diags = _run_cargo_check_diag(
            _derive_check_only_cmd(check_cmd), project_dir, env, timeout)
        if rc == 0:
            return {}
        if diags:
            return _frontier_from_diags(diags, project_dir)
    except Exception as e:
        _log(f"  [fix loop] structured frontier failed ({e}) → text fallback")
    return _extract_tdd_compile_frontier(output, project_dir)


def _frontier_from_diags(
    diagnostics: list[Diagnostic], src_dir: Path,
) -> Frontier:
    """Same (file, fn) → {"<compile>"} frontier as
    `_extract_tdd_compile_frontier`, but built from STRUCTURED cargo
    diagnostics (each has file/line/code) instead of regex-parsing rendered
    text. Use this when the diagnostics come from `_run_cargo_check_diag`,
    whose rendered text carries ANSI colour codes that the plain-text
    `_COMPILE_ERROR_RE` cannot match — re-parsing that text yields an empty
    frontier and silently defeats any "is this fn still broken?" check.
    """
    frontier: Frontier = {}
    fn_records_by_file: dict[str, list[dict]] = {}
    file_text_cache: dict[str, str] = {}
    for d in diagnostics or []:
        rel = d.file or ""
        line_no = int(d.line or 0)
        if not rel or line_no < 1:
            continue
        if rel not in fn_records_by_file:
            _all_recs = _enumerate_fns_via_extractor(src_dir) or []
            fn_records_by_file[rel] = [r for r in _all_recs if r.get("file") == rel]
        recs = fn_records_by_file[rel]
        if not recs:
            continue
        if rel not in file_text_cache:
            try:
                file_text_cache[rel] = (src_dir / rel).read_text(encoding="utf-8")
            except Exception:
                continue
        rec = _line_to_fn_record(file_text_cache[rel], line_no, recs)
        if rec is None:
            continue
        frontier.setdefault((rel, rec["fn_name"]), set()).add("<compile>")
    return frontier


# "cannot find …" diagnostics — an undefined value/fn/type/macro referenced but
# never defined. When several fns in one file hit these, they likely share
# internal machinery (a constructor, a const, a helper) that no single per-fn
# fill establishes — the vtable class (FR-040).
_CANNOT_FIND_CODES = {"E0425", "E0433", "E0412", "E0531", "E0405", "E0422", "E0423"}


def _coupled_cofill_groups(
    diagnostics: list[dict], src_dir: Path,
) -> list[dict]:
    """#1 (FR-040): detect co-fill groups — 2+ fns in the SAME file that fail
    with "cannot find" errors (undefined symbols). Filled per-fn they each
    invent the shared item under inconsistent names (`ADHOC_VTABLE` vs
    `VTABLE_ADHOC`); designed together the model can define it ONCE and
    reference it consistently. Returns [{file, fns:[...], symbols:[...]}], one
    per file with >= 2 such fns. Symbols are extracted from the diagnostic
    message between backticks (no regex)."""
    recs_by_file: dict[str, list[dict]] = {}
    text_cache: dict[str, str] = {}
    fn_syms: dict[str, dict[str, set]] = {}
    for d in diagnostics or []:
        if (d.get("code") or "") not in _CANNOT_FIND_CODES:
            continue
        rel = d.get("file") or ""
        line_no = int(d.get("line") or 0)
        msg = d.get("message") or ""
        if not rel or line_no < 1:
            continue
        sym = msg.split("`")[1] if msg.count("`") >= 2 else ""
        if rel not in recs_by_file:
            _all = _enumerate_fns_via_extractor(src_dir) or []
            recs_by_file[rel] = [r for r in _all if r.get("file") == rel]
        recs = recs_by_file[rel]
        if not recs:
            continue
        if rel not in text_cache:
            try:
                text_cache[rel] = (src_dir / rel).read_text(encoding="utf-8")
            except Exception:
                continue
        rec = _line_to_fn_record(text_cache[rel], line_no, recs)
        if rec is None:
            continue
        fn_syms.setdefault(rel, {}).setdefault(rec["fn_name"], set())
        if sym:
            fn_syms[rel][rec["fn_name"]].add(sym)
    groups: list[dict] = []
    for rel, fns in fn_syms.items():
        if len(fns) >= 2:
            syms = sorted({s for ss in fns.values() for s in ss})
            groups.append({"file": rel, "fns": sorted(fns), "symbols": syms})
    return groups


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Drop ANSI SGR colour escapes (cargo's rendered-ansi diagnostics)."""
    return _ANSI_RE.sub("", text or "")


def _order_frontier(
    frontier: Frontier,
    leaf_name_counts: dict[str, int] | None,
    panic_depths: dict[tuple[str, str], int] | None = None,
) -> list[tuple[tuple[str, str], set[str]]]:
    """Order frontier entries by D2's rule: shallowest panic depth first,
    then most-callers, then alphabetical (file, fn) for reproducibility.

    `panic_depths` (optional) maps the same keys to integer panic stack
    depths. Absent → all treated as same depth, the secondary sort wins.
    Most-referenced fn (from `leaf_name_counts`) breaks final ties so the
    pre-strip leaf-order metadata still influences scheduling.
    """
    panic_depths = panic_depths or {}
    leaf_name_counts = leaf_name_counts or {}
    items = list(frontier.items())

    def _key(item: tuple[tuple[str, str], set[str]]):
        (file_rel, fn_name), tests = item
        return (
            panic_depths.get((file_rel, fn_name), 0),  # shallowest first
            -len(tests),                                # most tests first
            -leaf_name_counts.get(fn_name, 0),          # most-referenced first
            file_rel, fn_name,                          # alphabetical reproducible
        )
    return sorted(items, key=_key)


def _scan_unimplemented_functions(src_dir: Path) -> list[tuple[str, str]]:
    """Return [(file_rel_path, fn_name), ...] for every fn whose body is still
    `unimplemented!()` (or an annotated stub).

    Prefers the syn-based extractor binary for robustness — it catches every
    fn rustc would consider (free fns, impl methods, trait method defaults,
    inline-compressed declarations, cfg-attributed fns). Falls back to a
    regex scanner when the binary isn't available.

    Note: this is the LEGACY name-only return form. New code should use
    `_scan_unimplemented_records` to also get container + signature, which
    are needed to disambiguate same-named fns within a single file.
    """
    recs = _enumerate_fns_via_extractor(src_dir)
    if recs is not None:
        return [(r["file"], r["fn_name"]) for r in recs
                if r.get("has_unimplemented_body")]
    # Fallback: best-effort regex scan. Less robust but doesn't require
    # the Rust binary; kept for the tests-fixture path and for portability.
    fn_re = re.compile(
        r'(?<![A-Za-z_0-9])'
        r'(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
        r'(?:const\s+)?fn\s+([A-Za-z_]\w*)[^{]*\{',
    )
    out: list[tuple[str, str]] = []
    for p in sorted(src_dir.rglob("*.rs")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in fn_re.finditer(text):
            fn_name = m.group(1)
            body_start = m.end()
            depth = 1
            i = body_start
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            if depth != 0:
                continue
            body = text[body_start:i - 1]
            if _is_unimplemented_body(body):
                out.append((str(p.relative_to(src_dir)), fn_name))
    return out


# ---------------------------------------------------------------------------
# Cluster-aware stub filter (P5 Step 1, focused fix). Filters stubs to only
# those whose enclosing cfg evaluates true for the current cluster. Skips
# fns that wouldn't compile in this cluster anyway (e.g., nightly.rs items
# being scanned in the stable cluster). Pure static analysis, no LLM.
# ---------------------------------------------------------------------------

_CFG_ATTR_RE = re.compile(r"^\s*#\s*\[\s*cfg\s*\(([^]]+)\)\s*\]\s*$")
# Inline form: matches `#[cfg(X)]` even when not on its own line. Used when
# searching the lib.rs `mod foo;` declaration — the skeleton extractor
# formats attributes with spaces (`# [cfg (X)] mod foo ;`) and may keep
# them on the same line as the item.
_CFG_INLINE_RE = re.compile(r"#\s*\[\s*cfg\s*\(([^]]+)\)\s*\]")


def _fn_cfg_predicates(file_text: str, fn_name: str) -> list[str]:
    """Return all `#[cfg(...)]` predicates that gate `fn_name` in `file_text`.

    FALLBACK ONLY (R7.4a): callers holding extractor records must pass the
    record's `cfg_predicates` to `_fn_belongs_to_cluster` instead — this
    regex's backward window (previous blank line → decl) can over-capture
    a PRECEDING fn's cfg attrs when fns are not blank-line separated, and
    it only ever finds the FIRST same-named fn in the file.

    Walks BACKWARD from the fn's declaration line, collecting consecutive
    `#[cfg(...)]` and `#[...]` attribute lines. Stops at the first
    non-attribute, non-blank line. Returns just the cfg predicates
    (the part inside `cfg(...)`).
    """
    fn_pat = re.compile(
        r"(?:^|\n)([ \t]*)(?:pub(?:\([^)]*\))?\s+)?"
        r"(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?"
        r"fn\s+" + re.escape(fn_name) + r"\b",
        re.MULTILINE,
    )
    m = fn_pat.search(file_text)
    if not m:
        return []
    decl_offset = m.start() + (1 if file_text[m.start()] == "\n" else 0)
    # Window: from the previous blank line (or start) to the fn decl.
    # Captures both same-line and above-line cfg attributes that the
    # skeleton extractor may emit (`# [cfg (X)] fn foo ...` or
    # `#[cfg(X)]\nfn foo`).
    ws = file_text.rfind("\n\n", 0, decl_offset)
    window_start = ws + 2 if ws >= 0 else 0
    window = file_text[window_start:decl_offset + 1]
    preds: list[str] = []
    for cm in _CFG_INLINE_RE.finditer(window):
        preds.append(cm.group(1).strip())
    return preds


def _enumerate_mods_via_extractor(src_dir: Path) -> list[ModRecord] | None:
    """Invoke the syn-based extractor's --enumerate-mods subcommand.

    Returns a list of mod records (each: {name, file, cfg_predicates,
    declared_in}) or None if the binary isn't available or fails.
    Replaces the regex-based scan of `mod foo;` declarations — syn parses
    Rust correctly and never conflates an adjacent mod's cfg with the
    target mod's cfg.
    """
    return ADAPTERS.extractor.enumerate_mods(src_dir)  # slice 5a: via Extractor adapter


def _enumerate_consts_via_extractor(src_dir: Path) -> list[ConstRecord] | None:
    """Invoke the syn-based extractor's --enumerate-consts subcommand: pub/private
    `const` and `static` items (name, type, is_pub). Replaces regex scans of source
    for `pub const`/`pub static` (Hard Rule: no regex on Rust source — use syn)."""
    return ADAPTERS.extractor.enumerate_consts(src_dir)


def _public_api_from_source(content: str) -> str:
    """Render a single Rust source string's public API surface using the syn extractor —
    no regex on Rust source (Hard Rule). The extractor is path-based, so write the string to a
    throwaway crate tree and enumerate types / fns / consts over it.

    Output mirrors the legacy regex format: `pub struct Name { f: T, ... }`, `pub enum Name
    { A, B }`, `pub trait Name`, full pub fn signatures, and `pub const NAME: T`.
    """
    import tempfile as _tempfile
    lines: list[str] = []
    with _tempfile.TemporaryDirectory() as _d:
        _src = Path(_d) / "src"
        _src.mkdir()
        (_src / "lib.rs").write_text(content, encoding="utf-8")
        _td = Path(_d)
        types = _enumerate_types_via_extractor(_td) or []
        fns = _enumerate_fns_via_extractor(_td) or []        # dicts (engine contract)
        consts = _enumerate_consts_via_extractor(_td) or []

    for t in types:
        if not t.is_pub:
            continue
        if t.kind == "struct":
            if t.fields:
                fields = ", ".join(f"{f.name}: {f.ty}" for f in t.fields)
                lines.append(f"pub struct {t.name}{t.generics} {{ {fields} }}")
            else:
                lines.append(f"pub struct {t.name}{t.generics}")
        elif t.kind == "enum":
            if t.variants:
                lines.append(f"pub enum {t.name}{t.generics} {{ {', '.join(t.variants)} }}")
            else:
                lines.append(f"pub enum {t.name}{t.generics}")
        elif t.kind == "trait":
            lines.append(f"pub trait {t.name}")

    for f in fns:
        if f.get("is_pub"):
            # sig_text starts at the `fn`/`unsafe`/`async` keyword (the extractor's sig span
            # excludes the visibility), so prepend `pub ` to match the API-listing convention.
            sig = " ".join((f.get("sig_text") or "").split())
            if sig:
                lines.append(sig if sig.startswith("pub ") else f"pub {sig}")

    for c in consts:
        if c.is_pub:
            lines.append(f"pub const {c.name}: {c.ty}")

    return "\n".join(lines)


# Cache of mod records per src_dir to avoid re-invoking the extractor for
# every fn lookup. Invalidated when target_src_dir's identity changes — the
# pipeline only ever queries one dir at a time during a cluster pass.
_MOD_RECORDS_CACHE: dict[str, list[ModRecord]] = {}


def _file_cfg_predicate(target_src_dir: Path, file_rel: str) -> str | None:
    """Return the cfg predicate (if any) under which `file_rel` is compiled.

    AST-derived via the syn-based skeleton extractor (`--enumerate-mods`).
    Returns None when the file is unconditionally included or when no
    parent mod decl is found.

    Falls back to the old regex-based scan when the extractor binary
    isn't available (test-fixture environments, partial setup, etc).
    """
    stem = Path(file_rel).stem
    if stem in ("lib", "main"):
        return None
    key = str(target_src_dir.resolve())
    if key not in _MOD_RECORDS_CACHE:
        recs = _enumerate_mods_via_extractor(target_src_dir)
        if recs is None:
            return _file_cfg_predicate_regex_fallback(target_src_dir, file_rel)
        _MOD_RECORDS_CACHE[key] = recs
    file_name = Path(file_rel).name
    for r in _MOD_RECORDS_CACHE[key]:
        if r.name == stem or r.file == file_name:
            preds: list[str] = list(r.cfg_predicates)
            if not preds:
                return None
            if len(preds) == 1:
                return preds[0]
            return "all(" + ", ".join(preds) + ")"
    return None


def _invalidate_mod_records_cache() -> None:
    """Clear the mod-records cache. Call after any operation that changes
    the on-disk mod declarations in lib.rs/main.rs (strip, task1 reset,
    task2 regeneration). Cheap enough that callers should err on the side
    of invalidation."""
    _MOD_RECORDS_CACHE.clear()


def _file_cfg_predicate_regex_fallback(
    target_src_dir: Path, file_rel: str,
) -> str | None:
    """Regex-based fallback for environments without the syn extractor
    binary. Known limitation: can conflate cfg attrs from an adjacent
    mod declaration when they share a line region. The AST path is
    preferred whenever available.
    """
    stem = Path(file_rel).stem
    if stem in ("lib", "main"):
        return None
    for root in ("src/lib.rs", "src/main.rs"):
        p = target_src_dir / root
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        mod_decl_pat = re.compile(
            r"(?:^|\n)([^\n]*\b(?:pub(?:\([^)]*\))?\s+)?mod\s+"
            + re.escape(stem) + r"\s*;[^\n]*)",
            re.MULTILINE,
        )
        m = mod_decl_pat.search(text)
        if not m:
            continue
        decl_line = m.group(1)
        preds: list[str] = []
        mod_idx = decl_line.find("mod ")
        if mod_idx < 0:
            mod_idx = len(decl_line)
        for cm in _CFG_INLINE_RE.finditer(decl_line[:mod_idx]):
            preds.append(cm.group(1).strip())
        decl_start = m.start() + (1 if text[m.start()] == "\n" else 0)
        head = text[:decl_start]
        for ln in reversed(head.splitlines()):
            s = ln.strip()
            if not s:
                break
            if s.startswith("//") or s.startswith("/*"):
                continue
            # Stop on any line that contains a declaration keyword — the
            # cfg attrs on such a line belong to ITS item, not ours.
            if re.search(r"\b(mod|fn|struct|enum|trait|impl|use|extern|"
                          r"static|const|type|macro_rules|pub|unsafe)\b", s):
                break
            if _CFG_INLINE_RE.search(ln):
                for cm in _CFG_INLINE_RE.finditer(ln):
                    preds.append(cm.group(1).strip())
                continue
            if s.startswith("#["):
                continue
            break
        if not preds:
            return None
        if len(preds) == 1:
            return preds[0]
        return "all(" + ", ".join(preds) + ")"
    return None


def _fn_belongs_to_cluster(
    target_src_dir: Path,
    file_rel: str,
    fn_name: str,
    cluster: "BuildCluster",
    fn_cfg_predicates: "list[str] | None" = None,
) -> bool:
    """True when the fn (or its enclosing file) is active in `cluster`.

    Evaluates the conjunction of:
      - The file's parent-mod cfg (if any)
      - The fn's own cfg attrs (if any)
    against the cluster's cfg_true / cfg_false sets.

    `fn_cfg_predicates` (R7.4a): the syn extractor's cfg list for THIS fn
    record — callers that hold extractor records pass it ([] = extractor
    says ungated, trusted as-is). Only when None does the legacy regex
    text-walk run (extractor-absent text-scan path); its backward window
    can over-capture a preceding fn's cfgs, which is exactly why record
    callers must not fall through to it.

    Conservative on uncertainty: when a predicate is undecidable, defaults
    to True (include the stub) so the per-fn fill loop sees it — better
    to waste a call than miss a fn we should have filled.
    """
    cfg_true = set(cluster.cfg_true)
    cfg_false = set(cluster.cfg_false)
    preds: list[str] = []
    file_pred = _file_cfg_predicate(target_src_dir, file_rel)
    if file_pred:
        preds.append(file_pred)
    if fn_cfg_predicates is not None:
        preds.extend(fn_cfg_predicates)
    else:
        try:
            text = (target_src_dir / file_rel).read_text(encoding="utf-8")
        except Exception:
            text = ""
        if text:
            preds.extend(_fn_cfg_predicates(text, fn_name))
    if not preds:
        return True  # no cfg gating → exists in every cluster
    for p in preds:
        v = _eval_cfg_pred(p, cfg_true, cfg_false)
        if v is False:
            return False
        # v is None (undecidable) → conservatively keep
    return True


def _implicated_fns_from_cargo_errors(
    cargo_output: str,
    target_src_dir: Path,
) -> list[tuple[str, str]]:
    """Parse cargo error output and return the (file_rel, fn_name) pairs the
    errors implicate.

    For each `error[...]` block: find the `--> file.rs:line:col` marker, then
    walk backward in that file from `line` to find the enclosing fn. Dedup
    by (file, fn). Used by P3.12 end-of-cluster fix loop to identify which
    fns to revert + retry.
    """
    if not cargo_output:
        return []
    cargo_output = _strip_cargo_warnings(cargo_output)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    loc_re = re.compile(r"-->\s+([^:\s]+\.rs):(\d+):\d+")
    for m in loc_re.finditer(cargo_output):
        file_str = m.group(1)
        line_no = int(m.group(2))
        # Normalise to rel path under target_src_dir.
        try:
            abs_p = (target_src_dir / file_str).resolve()
            rel_p = str(abs_p.relative_to(target_src_dir.resolve()))
        except Exception:
            # cargo sometimes emits absolute paths; try suffix match.
            cand = Path(file_str)
            rel_p = None
            for p in target_src_dir.rglob("*.rs"):
                if str(p).endswith(file_str) or p.name == cand.name:
                    rel_p = str(p.relative_to(target_src_dir))
                    break
            if rel_p is None:
                continue
        src_path = target_src_dir / rel_p
        if not src_path.exists():
            continue
        try:
            text = src_path.read_text(encoding="utf-8")
        except Exception:
            continue
        # Walk backward from line_no to find the enclosing fn declaration.
        lines = text.splitlines()
        if line_no > len(lines):
            line_no = len(lines)
        fn_re = re.compile(
            r"\bfn\s+([A-Za-z_]\w*)",
        )
        fn_name: str | None = None
        for i in range(line_no - 1, -1, -1):
            fm = fn_re.search(lines[i])
            if fm:
                # Make sure this fn line is actually a declaration (has fn
                # followed by name and parens/generics later in the line OR
                # within the next few lines).
                fn_name = fm.group(1)
                break
        if not fn_name:
            continue
        key = (rel_p, fn_name)
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


def _scan_unimplemented_for_cluster(
    target_src_dir: Path,
    cluster: "BuildCluster | None",
) -> list[tuple[str, str]]:
    """Cluster-aware variant of _scan_unimplemented_functions.

    Returns only stubs that belong to `cluster` per static cfg evaluation.
    When `cluster` is None (no cluster context), behaves identically to
    the unfiltered scan — used by callers that don't yet thread cluster.
    """
    raw = _scan_unimplemented_functions(target_src_dir)
    if cluster is None:
        return raw
    out: list[tuple[str, str]] = []
    skipped: int = 0
    for rel, fn in raw:
        if _fn_belongs_to_cluster(target_src_dir, rel, fn, cluster):
            out.append((rel, fn))
        else:
            skipped += 1
    if skipped:
        _log(f"  [phase-c/per-fn] cluster {cluster.name}: skipped "
             f"{skipped} stub(s) not active in this cluster's cfg-set.")
    return out


def _filter_test_exercised(stubs: list[tuple[str, str]],
                            test_files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep only stubs whose function name appears (as a token) in any test file."""
    if not stubs or not test_files:
        return []
    test_blob = "\n".join(content for _, content in test_files)
    out: list[tuple[str, str]] = []
    for rel, name in stubs:
        if re.search(rf'\b{re.escape(name)}\b', test_blob):
            out.append((rel, name))
    return out


def _task2_cleanup_residuals(
    target_src_dir: Path,
    project_dir: Path,
    functional_req: str,
    test_files: list[tuple[str, str]],
    module_name: str,
) -> dict:
    """Phase D: scan for residual `unimplemented!()` and patch what tests need.

    - Classifies remaining stubs as test-exercised vs not.
    - For test-exercised stubs, issues ONE LLM call with the relevant tests + the
      current source of each touched file + an instruction to fill those stubs.
    - Untouched stubs are left as-is (cargo test won't trigger them).

    Returns {'stubs_total': int, 'stubs_exercised': int, 'files_updated': int,
             'stubs_remaining': int}.
    """
    stubs = _scan_unimplemented_functions(target_src_dir)
    if not stubs:
        return {"stubs_total": 0, "stubs_exercised": 0, "files_updated": 0, "stubs_remaining": 0}

    exercised = _filter_test_exercised(stubs, test_files)
    _log(f"  [phase-d] {len(stubs)} residual stub(s); {len(exercised)} reachable from tests.")

    if not exercised:
        return {"stubs_total": len(stubs), "stubs_exercised": 0,
                "files_updated": 0, "stubs_remaining": len(stubs)}

    # Group exercised stubs by file so the model sees each affected file as one unit.
    by_file: dict[str, list[str]] = {}
    for rel, fn in exercised:
        by_file.setdefault(rel, []).append(fn)

    # Load only the affected files (plus all of the rest for type context).
    skeleton_block = _format_skeleton_for_prompt(_load_skeleton_files(target_src_dir))
    targets_block = "\n".join(
        f"  - {rel}: {', '.join(fns)}" for rel, fns in by_file.items()
    )
    # Surface only tests that mention any of the target functions (relevance filter).
    relevant_tests: list[str] = []
    for rel, content in test_files:
        if any(re.search(rf'\b{re.escape(fn)}\b', content)
               for fns in by_file.values() for fn in fns):
            relevant_tests.append(f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>')
    tests_block = "\n\n".join(relevant_tests)
    _fr_cap = SINGLE_CALL_CHAR_LIMIT // 4
    _fr_capped = smart_truncate(functional_req or "", total_limit=_fr_cap,
                                head=_fr_cap // 3, tail=2 * _fr_cap // 3)

    prompt = f"""You are cleaning up residual `unimplemented!()` bodies in a Rust crate after a
phase of group-driven test-driven fills.

Crate: `{module_name}`

These specific functions are still `unimplemented!()` but the tests call them:
{targets_block}

Current source on disk:
{skeleton_block}

Tests that reference these functions:
{tests_block}

Functional requirements:
{_fr_capped}

{FILE_FORMAT_INSTRUCTION}

Output the source files you modified. Fill ONLY the functions listed above.
Do NOT modify any function whose body is NOT `unimplemented!()`.
Do NOT modify type definitions, imports, or any other items.
"""
    response = ask(prompt, model=_TASK2_MODEL)
    files = extract_files(response)
    files_updated = 0
    for rel, model_text in files.items():
        if ".gen_tests_" in rel:
            continue  # never touch task0c protected test artifacts
        disk_path = target_src_dir / rel
        if not disk_path.exists():
            continue
        disk_text = disk_path.read_text(encoding="utf-8")
        merged = _merge_fills_preserving_existing(disk_text, model_text)
        if merged != disk_text:
            disk_path.write_text(merged, encoding="utf-8")
            files_updated += 1

    remaining = _scan_unimplemented_functions(target_src_dir)
    return {
        "stubs_total": len(stubs),
        "stubs_exercised": len(exercised),
        "files_updated": files_updated,
        "stubs_remaining": len(remaining),
    }


def _cmd_phase_c_only(project_dir: Path) -> int:
    """Standalone Phase A + B + C: skeleton → grouping → per-group fill."""
    rc = _cmd_phase_a_only(project_dir)
    if rc != 0:
        return rc
    skeleton_dir = project_dir / ".skeleton" / "_standalone"
    test_files = collect_protected_test_files(project_dir)
    pairs: list[tuple[str, str]] = []
    for rel in sorted(test_files):
        p = project_dir / rel
        if p.exists():
            try:
                pairs.append((rel, p.read_text(encoding="utf-8")))
            except Exception:
                continue
    if not pairs:
        _log("  [phase-c] no test files found.")
        return 6

    _log(f"\n=== Phase B (test grouping) for {project_dir.name} ===")
    groups = _task2_group_tests(skeleton_dir, pairs, cluster_name="default",
                                 module_name=project_dir.name)
    _log(f"  [phase-b] {len(groups)} groups produced.")

    _log(f"\n=== Phase C (per-group fill) for {project_dir.name} ===")
    target_src_dir = project_dir / ".skeleton-filled"
    if target_src_dir.exists():
        shutil.rmtree(target_src_dir)
    fr_path = project_dir / "functional_requirements.md"
    genesis_path = project_dir / "genesis.md"
    fr = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""
    genesis = genesis_path.read_text(encoding="utf-8") if genesis_path.exists() else ""

    result = _task2_fill_groups(skeleton_dir, target_src_dir, groups, project_dir,
                                 cluster_name="default", module_name=project_dir.name,
                                 functional_req=fr, genesis=genesis)
    _log(f"  [phase-c] processed {result['groups_processed']} group(s), "
         f"filled {result['bytes_filled']:,} bytes; "
         f"{len(result['errors'])} error(s).")
    for e in result["errors"]:
        _log(f"    - {e}")
    # Always return 0 from Phase C so downstream callers (Phase D, the
    # backend class) can perform residual cleanup regardless of per-group
    # failures. Partial Phase C output is the recovery case Phase D exists for.
    return 0


def _cmd_skeleton_backend(project_dir: Path) -> int:
    """Standalone harness invoking StaticSkeletonTask2Generator.generate()
    directly. Drives the full per-cluster Plan 4 flow without needing
    task0c/strip/task1 to have run."""
    _log(f"\n=== Skeleton backend (Plan 4 per-cluster) for {project_dir.name} ===")
    fr_path = project_dir / "functional_requirements.md"
    genesis_path = project_dir / "genesis.md"
    manifest_path = project_dir / MANIFEST_NAME
    if not fr_path.exists():
        # No FR yet — synthesize an empty one so the backend can still run.
        fr_path.write_text("# (no functional_requirements.md available)\n", encoding="utf-8")
    if not genesis_path.exists():
        genesis_path.write_text("# (no genesis.md available)\n", encoding="utf-8")
    existing_test_files = collect_protected_test_files(project_dir)
    backend = StaticSkeletonTask2Generator()
    ok = backend.generate(
        project_dir=project_dir,
        genesis_path=genesis_path,
        functional_req_path=fr_path,
        manifest_path=manifest_path,
        existing_test_files=existing_test_files,
        current_module=project_dir.name,
    )
    return 0 if ok else 8


def _cmd_phase_d_only(project_dir: Path) -> int:
    """Standalone Phase A + B + C + D."""
    rc = _cmd_phase_c_only(project_dir)
    if rc != 0:
        return rc
    _log(f"\n=== Phase D (residual cleanup) for {project_dir.name} ===")
    target_src_dir = project_dir / ".skeleton-filled"
    test_files = collect_protected_test_files(project_dir)
    pairs: list[tuple[str, str]] = []
    for rel in sorted(test_files):
        p = project_dir / rel
        if p.exists():
            try:
                pairs.append((rel, p.read_text(encoding="utf-8")))
            except Exception:
                continue
    fr_path = project_dir / "functional_requirements.md"
    fr = fr_path.read_text(encoding="utf-8") if fr_path.exists() else ""

    result = _task2_cleanup_residuals(target_src_dir, project_dir, fr, pairs,
                                       module_name=project_dir.name)
    _log(f"  [phase-d] {result['stubs_total']} residual stubs, "
         f"{result['stubs_exercised']} reachable from tests; "
         f"{result['files_updated']} file(s) updated; "
         f"{result['stubs_remaining']} stub(s) still remaining.")
    return 0


def _cmd_phase_b_only(project_dir: Path) -> int:
    """Standalone Phase A + B: skeleton + LLM-driven test grouping."""
    rc = _cmd_phase_a_only(project_dir)
    if rc != 0:
        return rc
    _log(f"\n=== Phase B (test grouping) for {project_dir.name} ===")
    skeleton_dir = project_dir / ".skeleton" / "_standalone"
    test_files = collect_protected_test_files(project_dir)
    # collect_protected_test_files returns set of relative paths; load contents.
    pairs: list[tuple[str, str]] = []
    for rel in sorted(test_files):
        p = project_dir / rel
        if p.exists():
            try:
                pairs.append((rel, p.read_text(encoding="utf-8")))
            except Exception:
                continue
    if not pairs:
        _log("  [phase-b] no test files found.")
        return 6
    # Use a synthetic cluster_name + the project name as module for a quick smoke.
    groups = _task2_group_tests(skeleton_dir, pairs, cluster_name="default",
                                 module_name=project_dir.name)
    _log(f"  [phase-b] manifest written to {skeleton_dir / 'groups.json'} "
         f"({len(groups)} groups, "
         f"{sum(len(g['test_files']) for g in groups)} test files covered).")
    for g in groups:
        _log(f"    - {g['name']}: {len(g['test_files'])} test(s); "
             f"{len(g.get('target_symbols', []))} target symbol(s)")
    return 0


def task0_cfg_scan(project_dir: Path) -> "list[BuildCluster]":
    """
    Derive compiler clusters from project source — no LLM, pure static scan.

    Algorithm:
    1. Parse Cargo.toml [features] to know which predicates are cargo features.
    2. Scan src/**/*.rs for all #[cfg(...)] predicates (flattened from any()/all()/not()).
    3. Classify each predicate:
       - Skip: test, doc, debug_assertions, standard target/platform predicates
       - Cargo feature AND in default = [] → always active, skip
       - Cargo feature AND NOT default → potential cluster (--features X)
       - Unknown/custom (not in known-stable set, not Cargo feature) → RUSTFLAGS --cfg X cluster
    4. Count lines gated per predicate; skip if < 5 (noise).
    5. Return [stable_cluster, ...one per interesting predicate].
    6. Write build_configs.md.
    """
    # --- Step 1: parse Cargo.toml features ---
    cargo_toml = project_dir / "Cargo.toml"
    cargo_features: set[str] = set()
    default_features: set[str] = set()
    if cargo_toml.exists():
        _feats = _load_cargo_toml(cargo_toml.read_text(errors="replace")).get("features", {})
        if isinstance(_feats, dict):
            cargo_features = set(_feats.keys())   # all feature names (incl. "default", as before)
            _def = _feats.get("default", [])
            default_features = {str(x) for x in _def} if isinstance(_def, list) else set()

    # --- Step 2: collect cfg predicates from src/**/*.rs ---
    # Match entire #[cfg(...)] or #[cfg_attr(..., ...)] attribute contents.
    _PREDICATE_RE = re.compile(r'#\[cfg(?:_attr)?\(([^)]+(?:\([^)]*\)[^)]*)*)\)')
    # Extract feature="X" values (the actual feature name, not the word "feature").
    _FEATURE_VAL_RE = re.compile(r'\bfeature\s*=\s*"([^"]*)"')
    # Extract bare cfg identifiers (not followed by ="...") — custom/platform predicates.
    _BARE_CFG_RE = re.compile(r'\b([a-z_]\w*)(?!\s*=)')
    _SKIP_KEYWORDS = {'any', 'all', 'not', 'cfg', 'cfg_attr', 'feature'}

    # Known stable predicates — never warrant their own cluster
    _STABLE_KNOWN = {
        'test', 'doc', 'debug_assertions', 'proc_macro',
        'unix', 'windows',
        'target_os', 'target_arch', 'target_env', 'target_family',
        'target_pointer_width', 'target_endian', 'target_vendor',
        'target_has_atomic', 'target_thread_local',
        'panic',
    }

    predicate_lines: dict[str, int] = {}  # predicate → line count gating it

    src_dir = project_dir / "src"
    rs_files = list(src_dir.rglob("*.rs")) if src_dir.exists() else []

    for rs_file in rs_files:
        try:
            text = rs_file.read_text(errors="replace")
        except Exception:
            continue
        for m in _PREDICATE_RE.finditer(text):
            inner = m.group(1)
            # 1. Extract feature="X" → record X as a Cargo-feature predicate
            for feat_m in _FEATURE_VAL_RE.finditer(inner):
                feat = feat_m.group(1)
                if feat:
                    predicate_lines[feat] = predicate_lines.get(feat, 0) + 1
            # 2. Extract bare cfg names — remove feature="X" first to avoid
            #    the regex matching a prefix of "feature" before the =.
            bare_inner = _FEATURE_VAL_RE.sub('', inner)
            for bare_m in _BARE_CFG_RE.finditer(bare_inner):
                name = bare_m.group(1)
                if name in _SKIP_KEYWORDS:
                    continue
                predicate_lines[name] = predicate_lines.get(name, 0) + 1

    # --- Step 3 & 4: classify and filter ---
    interesting: list[tuple[str, str]] = []  # (predicate, kind) where kind in ('feature', 'custom')

    for pred, line_count in predicate_lines.items():
        if line_count < 5:
            continue  # noise threshold
        # Check if it's a known-stable prefix
        base = pred.split('=')[0].strip()
        if base in _STABLE_KNOWN:
            continue
        if pred in cargo_features:
            if pred in default_features:
                continue  # always active, not a separate cluster
            interesting.append((pred, 'feature'))
        else:
            interesting.append((pred, 'custom'))

    # --- Step 4b: find files whose entire module is gated behind a single predicate ---
    # Parse lib.rs / main.rs for:  #[cfg(pred)]  mod filename;
    # These files are only compiled when `pred` is active, so they must not be
    # augmented in clusters where `pred` is excluded.
    _MOD_CFG_RE = re.compile(
        r'#\[cfg\(([^)]+)\)\]\s*(?:pub\s+)?mod\s+(\w+)\s*;',
        re.MULTILINE,
    )
    pred_to_gated_files: dict[str, set[str]] = {}  # pred → {rel_path, ...}
    for entry_file in ("src/lib.rs", "src/main.rs"):
        entry_path = project_dir / entry_file
        if not entry_path.exists():
            continue
        text = entry_path.read_text(errors="replace")
        for m in _MOD_CFG_RE.finditer(text):
            cfg_arg, mod_name = m.group(1).strip(), m.group(2)
            # Only handle simple single-predicate gates (no any/all/not nesting)
            if any(kw in cfg_arg for kw in ('any(', 'all(', 'not(')):
                continue
            # Strip feature="" wrapper if present, else use bare name
            feat_m = re.search(r'feature\s*=\s*"([^"]*)"', cfg_arg)
            pred = feat_m.group(1) if feat_m else cfg_arg
            rel = f"src/{mod_name}.rs"
            pred_to_gated_files.setdefault(pred, set()).add(rel)

    def _gated_files_for_excluded(excluded: list[str]) -> set:
        result: set[str] = set()
        for p in excluded:
            result |= pred_to_gated_files.get(p, set())
        return result

    # --- Step 5: build clusters ---
    # FR-030: cfgs that switch on a nightly `#![feature(...)]`. Any cluster
    # whose active cfg-set intersects this needs the nightly toolchain — set
    # requires_nightly statically here rather than relying on a runtime E0554
    # retry that doesn't persist to TASK 3.
    _feature_gate_cfgs = set(_feature_gate_cfgs_via_extractor(src_dir) or [])
    stable_tarpaulin_exclude = ["*/nightly.rs"]
    stable_excluded_cfg = [p for p, _ in interesting]

    stable_cluster = BuildCluster(
        name="stable",
        env_extra={},
        tarpaulin_exclude_files=stable_tarpaulin_exclude,
        excluded_cfg=stable_excluded_cfg,
        active_cfg=[],
        prompt_nightly_rule=(
            "- Do NOT use `error_generic_member_access`, `.provide()`, `Request`, or import `crate::nightly`\n"
            "  — these are nightly-only features unavailable on the stable test toolchain. Skip any source\n"
            "  lines inside `#[cfg(error_generic_member_access)]` blocks — they are untestable on stable.\n"
            "- Do NOT use `FormattingOptions` — it is nightly-only. Skip source lines that use it."
        ),
        is_default=True,
        gated_files=_gated_files_for_excluded(stable_excluded_cfg),
    )
    clusters: list[BuildCluster] = [stable_cluster]

    for pred, kind in interesting:
        if kind == 'feature':
            env_extra = {}
            cluster_name = f"feature/{pred}"
            tarpaulin_excl: list[str] = []
            active = [pred]
            excl = [p for p, _ in interesting if p != pred]
            pr = (
                f"- COMPILER CLUSTER: {cluster_name}\n"
                f"  Cargo feature `{pred}` IS enabled. Code inside `#[cfg(feature=\"{pred}\")]` IS active.\n"
                f"  Focus your tests on lines inside these cfg blocks."
            )
        else:
            # custom cfg → RUSTFLAGS --cfg
            env_extra = {"RUSTFLAGS": f"--cfg {pred}"}
            cluster_name = _cfg_to_cluster_name(pred)
            tarpaulin_excl = []
            active = [pred]
            excl = [p for p, _ in interesting if p != pred]
            pr = (
                f"- COMPILER CLUSTER: {cluster_name}\n"
                f"  `{pred}` IS active (RUSTFLAGS=--cfg {pred}). All `#[cfg({pred})]` blocks ARE compiled.\n"
                f"  You MAY use any symbols gated behind `#[cfg({pred})]`.\n"
                f"  Only write tests for lines inside `#[cfg({pred})]` blocks — stable lines are already covered."
            )
        _bc = BuildCluster(
            name=cluster_name,
            env_extra=env_extra,
            tarpaulin_exclude_files=tarpaulin_excl,
            excluded_cfg=excl,
            active_cfg=active,
            prompt_nightly_rule=pr,
            is_default=False,
            gated_files=_gated_files_for_excluded(excl),
        )
        # FR-030: this cluster activates a nightly feature attr → needs +nightly.
        if set(active) & _feature_gate_cfgs:
            _bc.requires_nightly = True
            _log(f"  cluster/{cluster_name}: requires nightly toolchain "
                 f"(active cfg gates #![feature(...)]).")
        clusters.append(_bc)

    # --- Step 6: write build_configs.md ---
    _write_build_configs(project_dir, clusters, predicate_lines)
    return clusters


def _cluster_pre_check(project_dir: Path, module_name: str, cluster: "BuildCluster") -> bool:
    """
    Verify the base library (no gen_tests) compiles under the cluster's env.
    If E0554 is detected (nightly feature on stable), attempt to install the nightly
    toolchain via setup.sh and retry with RUSTUP_TOOLCHAIN=nightly.
    Returns True if the cluster is buildable, False if it should be skipped.
    """
    result = subprocess.run(
        ["cargo", "build", "-p", module_name],
        cwd=project_dir, capture_output=True, text=True,
        timeout=BUILD_TIMEOUT, env=_cargo_env(),
    )
    if result.returncode == 0:
        return True

    err = result.stderr + result.stdout
    if "E0554" not in err and "may not be used on the stable release channel" not in err:
        # Some other compile error — not a toolchain issue, let normal flow handle it.
        return True

    # E0554: nightly feature required. Try to install nightly toolchain.
    _log(f"  cluster/{cluster.name}: E0554 detected — nightly toolchain required.")
    _setup = project_dir.parent.parent / "setup.sh"  # determ/setup.sh
    if not _setup.exists():
        _setup = Path(__file__).parent / "setup.sh"
    if _setup.exists():
        _log(f"  cluster/{cluster.name}: running setup.sh rust nightly ...")
        _r = subprocess.run(
            ["bash", str(_setup), "rust", "nightly"],
            capture_output=False, text=True, timeout=600,
        )
        if _r.returncode != 0:
            _log(f"  cluster/{cluster.name}: setup.sh rust nightly failed — skipping cluster.")
            return False
    else:
        _log(f"  cluster/{cluster.name}: setup.sh not found — cannot install nightly. Skipping cluster.")
        return False

    # Mark cluster as requiring nightly so _cargo_env sets RUSTUP_TOOLCHAIN=nightly.
    cluster.requires_nightly = True

    result2 = subprocess.run(
        ["cargo", "build", "-p", module_name],
        cwd=project_dir, capture_output=True, text=True,
        timeout=BUILD_TIMEOUT, env=_cargo_env(),
    )
    if result2.returncode != 0:
        _log(f"  cluster/{cluster.name}: still broken after nightly install — skipping cluster.")
        _log(f"    {(result2.stderr + result2.stdout)[:300]}")
        return False

    _log(f"  cluster/{cluster.name}: nightly toolchain ready.")
    return True


def _cfg_to_cluster_name(pred: str) -> str:
    """Map a custom cfg predicate to a short cluster name."""
    # Well-known mappings
    _KNOWN = {
        'error_generic_member_access': 'nightly',
        'backtrace_in_std': 'nightly',
        'provide_any': 'nightly',
    }
    if pred in _KNOWN:
        return _KNOWN[pred]
    # Generic: strip known prefixes, truncate
    name = pred.replace('_feature', '').replace('_enabled', '').replace('_support', '')
    return name[:32]


def _write_build_configs(project_dir: Path, clusters: list, predicate_lines: dict) -> None:
    lines = [
        f"# Build configurations — {project_dir.name}",
        f"# {len(clusters)} cluster(s) derived from source",
        f"# Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
    ]
    for c in clusters:
        lines.append(f"## cluster: {c.name}")
        if c.env_extra:
            for k, v in c.env_extra.items():
                lines.append(f"env: {k} += {v}")
        else:
            lines.append("env: (none)")
        if c.tarpaulin_exclude_files:
            for pat in c.tarpaulin_exclude_files:
                lines.append(f"tarpaulin_exclude: {pat}")
        else:
            lines.append("tarpaulin_exclude: (none — all files measured)")
        if c.active_cfg:
            lines.append(f"active_cfg: {', '.join(c.active_cfg)}")
        else:
            lines.append("active_cfg: (default build)")
        if c.excluded_cfg:
            lines.append(f"excluded_cfg: {', '.join(c.excluded_cfg)}")
        gated = sum(predicate_lines.get(p, 0) for p in c.active_cfg)
        if gated:
            lines.append(f"gated_lines: ~{gated}")
        lines.append("")
    (project_dir / BUILD_CONFIGS_NAME).write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Pipeline state file — pipeline_state.md
#
# Tracks which phases completed, against which source hash, so restarts can
# validate cached results and resume from the right point.
#
# Format (one phase per line):
#   [phase_key]  status  hash=<16-char>  detail text
#
# status: done | in_progress | pending
# hash:   16-char SHA-256 prefix of the source inputs for that phase, or "-"
# ---------------------------------------------------------------------------

def _source_hash(project_dir: Path) -> str:
    """Hash of Cargo.toml + src/*.rs (gen_tests artifacts stripped) + test files.
    Shared across all phases — if source changes, all phase hashes become stale."""
    parts: list[str] = []
    cargo = project_dir / "Cargo.toml"
    if cargo.exists():
        parts.append(cargo.read_text(errors="replace"))
    for p in sorted((project_dir / "src").glob("*.rs")) if (project_dir / "src").exists() else []:
        # Skip gen_tests sibling files entirely — pipeline-generated content.
        if re.search(r'\.gen_tests_\d+\.rs$', p.name):
            continue
        with suppress(Exception):
            parts.append(_strip_gen_tests_decls(p.read_text(errors="replace")))
    for p in sorted(project_dir.rglob("*.rs")):
        rel = str(p.relative_to(project_dir))
        if "tests/" in rel or rel.startswith("tests"):
            with suppress(Exception):
                parts.append(p.read_text(errors="replace")[:500])
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:16]


# Slice 9: the pipeline_state.md store moved to pipeline_core/adapters/state.py
# (regex-free line parser; same file format, locking, atomic replace). Aliases
# keep every call site + the task-deps records unchanged.
_state_path = _state_mod.state_path
_read_state = _state_mod.read_state
_write_state = _state_mod.write_state
_mark_phase = _state_mod.mark_phase
_set_current = _state_mod.set_current
_phase_valid = _state_mod.phase_valid
_log_state_summary = _state_mod.log_state_summary



def _commit_and_mark(
    project_dir: Path,
    phase_key: str,
    status: str,
    *,
    commit_message: str,
    tag: "str | None" = None,
    detail: str = "",
    source_hash: "str | None" = None,
) -> None:
    """Atomic transition: COMMIT the workspace, optionally TAG, THEN mark
    the phase done in pipeline_state.md.

    This ordering is the persistence fix for the original bug — the
    legacy pattern was `do_work → _mark_phase("done") → git_commit(...)`,
    which leaks irrecoverably when the process dies between the mark and
    the commit: the next run sees `state=done`, skips re-running the
    phase, and the workspace mutation is never preserved in git. Reverse
    the order: commit first, then mark. Death between commit and mark is
    benign — the next run re-runs the (idempotent) phase, the second
    `git_commit` is a no-op because the tree is already clean, and the
    `_mark_phase` finally records done.

    `commit_message` is passed verbatim to `git_commit`. If the working
    tree has no changes the commit silently no-ops (already-clean case);
    we still proceed to tag + mark, which preserves the "resume" case
    where a prior run already committed.

    Returns nothing — failures are logged inside git_commit/git_tag.
    """
    git_commit(project_dir, commit_message)
    if tag:
        git_tag(project_dir, tag)
    _mark_phase(project_dir, phase_key, status,
                detail=detail, source_hash=source_hash)



# slice 5b: deps for the extracted task0 doc tasks (profile + context).
_TASK0_DOCS_DEPS = Task0DocsDeps(
    phase_valid=_phase_valid,
    mark_phase=_mark_phase,
    ask=ask,
    write=write,
    prompt_budget=prompt_budget,
    task0_model=_TASK0_MODEL,
    model=MODEL,
    readme_cap=_README_CAP,
    profile_cap=_PROFILE_CAP,
    project_context_name=PROJECT_CONTEXT_NAME,
)


def task0_project_profile(profile_path: Path, test_files, readme_text: str) -> str:
    # slice 5b: extracted to application.tasks.task0_docs; thin shim binding deps.
    return _task0_profile_impl(profile_path, test_files, readme_text, _TASK0_DOCS_DEPS)


def task0b_discovery(strategy_path: Path, project_dir: Path, test_files,
                     readme_text: str, profile_content: str,
                     clusters: "list[BuildCluster] | None" = None,
                     force: bool = False) -> str:
    # slice 5b: extracted to application.tasks.task0b_discovery; thin shim binding deps.
    return _task0b_impl(strategy_path, project_dir, test_files, readme_text,
                        profile_content, _TASK0B_DEPS, clusters=clusters, force=force)


def _build_task0c_prompt_sections(
    rel_path: str,
    existing_text: str,
    fn_bodies: str,
    pub_symbols: str,
    cargo_toml_content: str,
    integration_tests: list,
    sibling_files: list,
    coverage_strategy: str,
    attempt_history: str,
    project_context: str,
    old_block: str = "",
    what_worked: str = "",
    retrospective: str = "",
    uncov_lines: "set[int] | None" = None,
    scaffold_chars: int = 0,
) -> dict:
    """
    BL-007: Build context sections for one TASK 0c per-file prompt.

    Prioritized degradation — if total exceeds budget, drop lowest-priority sections first:
      1. Always: Cargo.toml + full source + uncovered fn bodies + public symbols
      2. Add if fits: coverage_strategy, attempt_history, existing inline tests, project context
      3. Add if fits: integration tests (shortest first)
      4. Add if fits: sibling source files (shortest first)

    Returns dict of section_name -> content string.

    `scaffold_chars` (optional): caller-measured size of the wrapping prompt
    text excluding section content. When supplied, used directly as the
    overhead — eliminates the magic 8_000 fallback.
    """
    OVERHEAD = scaffold_chars if scaffold_chars > 0 else 8_000
    budget = TASK0C_PROMPT_CHAR_CAP - OVERHEAD
    dropped: list[str] = []

    # --- Always-included (priority 1) ---
    cargo_section = (
        f"\nCargo.toml (available dependencies — ONLY use what is listed here):\n"
        f"```toml\n{cargo_toml_content}\n```\n"
    ) if cargo_toml_content.strip() else ""

    # Cap full source at 50% of budget — macro-heavy files (ensure.rs 50k, error.rs 35k)
    # otherwise consume the entire budget leaving no room for siblings or fn_bodies.
    # The fn_bodies section already captures all uncovered code; full source adds import
    # and type context. Truncation preserves the top of the file (imports, type defs).
    _src_cap = int(budget * 0.50)
    if len(existing_text) > _src_cap:
        _guard("task0c/source_cap", f"{rel_path}: {len(existing_text):,} chars → capped at {_src_cap:,} (50% budget)")
    if len(existing_text) <= _src_cap:
        _src_text = existing_text
    else:
        _cut = existing_text.rfind("\n", 0, _src_cap)
        _src_text = existing_text[:_cut if _cut != -1 else _src_cap] + "\n// ... (source truncated — see fn_bodies below for uncovered code)\n"
    full_source_section = (
        f"\nFull source file `{rel_path}` (file you are augmenting — note all `use` statements):\n"
        f"```rust\n{_src_text}\n```\n"
    )

    _fn_cap = int(budget * 0.40)
    if len(fn_bodies) <= _fn_cap:
        _fn_text = fn_bodies
    else:
        _sep = "\n\n// --- next function ---\n\n"
        _cutoff = fn_bodies.rfind(_sep, 0, _fn_cap)
        if _cutoff == -1:
            # Only one function and it's over budget — truncate at last newline
            _cutoff = fn_bodies.rfind("\n", 0, _fn_cap)
        _fn_text = fn_bodies[:_cutoff] + "\n\n// ... (fn_bodies truncated — remaining functions omitted)\n"
    fn_bodies_section = (
        f"\nFunctions with uncovered lines (complete bodies, `// !! UNCOVERED` marks gaps):\n"
        f"```rust\n{_fn_text}\n```\n"
    ) if fn_bodies.strip() else ""

    symbols_section = (
        f"\nPublic API of `{rel_path}` (use ONLY these — do not invent types or functions):\n"
        f"```rust\n{pub_symbols}\n```\n"
    ) if pub_symbols.strip() else ""

    used = len(cargo_section) + len(full_source_section) + len(fn_bodies_section) + len(symbols_section)
    remaining = budget - used

    # --- Optional sections (priority 2) ---
    # History first — most valuable feedback; strategy is generic guidance.
    history_section = ""
    if attempt_history.strip():
        s = f"\n{attempt_history}\n"
        if len(s) <= remaining:
            history_section = s
            remaining -= len(s)
        else:
            dropped.append("attempt_history")

    what_worked_section = ""
    if what_worked.strip():
        s = f"\nPatterns that achieved coverage improvement in previous attempts (reuse these constructs):\n{what_worked}\n"
        if len(s) <= remaining:
            what_worked_section = s
            remaining -= len(s)
        else:
            dropped.append("what_worked")

    retrospective_section = ""
    if retrospective.strip():
        s = f"\nRetrospective — why previous attempt did not improve coverage (act on this):\n{retrospective}\n"
        if len(s) <= remaining:
            retrospective_section = s
            remaining -= len(s)
        else:
            dropped.append("retrospective")

    strategy_section = ""
    if coverage_strategy.strip():
        # Strip raw <think>...</think> blocks — the strategy may have been produced by a
        # thinking model and the reasoning tokens must not occupy the budget slot.
        _strategy_clean = re.sub(r'<think>.*?</think>', '', coverage_strategy, flags=re.DOTALL).strip()
        s = f"\nProject coverage strategy (import paths, type construction, test conventions):\n{_strategy_clean}\n"
        if len(s) <= remaining:
            strategy_section = s
            remaining -= len(s)
        else:
            dropped.append("coverage_strategy")

    existing_tests = _extract_existing_tests(existing_text)
    examples_section = ""
    if existing_tests.strip():
        s = (f"\nExisting tests in this file "
             f"(reference for correct type construction and coding style):\n"
             f"```rust\n{existing_tests}\n```\n")
        if len(s) <= remaining:
            examples_section = s
            remaining -= len(s)
        else:
            dropped.append("existing_inline_tests")

    context_section = ""
    if project_context.strip():
        s = f"\nProject context:\n{project_context}\n"
        if len(s) <= remaining:
            context_section = s
            remaining -= len(s)
        else:
            dropped.append("project_context")

    old_block_section = ""
    if old_block.strip():
        s = (
            f"\nExisting test module(s) for this file — ALREADY COMMITTED TO DISK AND COMPILE."
            f" Do NOT copy, repeat, or modify any test from them."
            f" Write ONLY new test functions that cover the uncovered lines in fn_bodies below:\n```rust\n{old_block}\n```\n"
        )
        if len(s) <= remaining:
            old_block_section = s
            remaining -= len(s)
        else:
            dropped.append("old_block")

    # --- Sibling source files (priority 3, planner relevance order) ---
    # Moved before integration tests: wrong type construction causes more failures than
    # wrong test style. Planner-ranked siblings (e.g. error.rs overlap 3-4) must fit.
    sib_parts: list[str] = []
    for (srel, scontent) in sibling_files:  # order from _select_relevant_siblings (most relevant first)
        s = f"\n// --- {srel} ---\n{scontent}"
        if len(s) <= remaining:
            sib_parts.append(s)
            remaining -= len(s)
        else:
            dropped.append(f"sibling:{srel}")
    sib_section = (
        "\nSibling source files in this module (type definitions and signatures):\n"
        "```rust\n" + "\n\n".join(sib_parts) + "\n```"
    ) if sib_parts else ""

    # --- Integration tests (priority 4, shortest first) ---
    integ_parts: list[str] = []
    for (irel, icontent) in sorted(integration_tests, key=lambda x: len(x[1])):
        s = f'\n<TESTFILE path="{irel}">\n{icontent}\n</TESTFILE>\n'
        if len(s) <= remaining:
            integ_parts.append(s)
            remaining -= len(s)
        else:
            dropped.append(f"integ_test:{irel}")
    integ_section = (
        "\nIntegration test files (already compile and pass — use as pattern reference):\n"
        + "".join(integ_parts)
    ) if integ_parts else ""

    if dropped:
        _guard("task0c/budget", f"{rel_path}: dropped {', '.join(dropped)}")

    return {
        "cargo": cargo_section,
        "full_source": full_source_section,
        "fn_bodies": fn_bodies_section,
        "symbols": symbols_section,
        "history": history_section,
        "what_worked": what_worked_section,
        "retrospective": retrospective_section,
        "old_block": old_block_section,
        "strategy": strategy_section,
        "examples": examples_section,
        "context": context_section,
        "siblings": sib_section,
        "integ_tests": integ_section,
    }


def task0_project_context(context_path: Path, test_files, readme_text: str,
                          profile_content: str) -> str:
    # slice 5b: extracted to application.tasks.task0_docs; thin shim binding deps.
    return _task0_context_impl(context_path, test_files, readme_text, profile_content, _TASK0_DOCS_DEPS)


# ---------------------------------------------------------------------------
# Requirements synthesis (TASK 1) — SOLID design
#
# Two implementations:
#   StaticTask1Synthesizer  — current approach: extract requirements from tests
#                             using natural-language descriptions of behavior.
#   LLMTask1Synthesizer     — contract-first / TDD: extract every method
#                             signature, type, trait, module path called from
#                             tests as explicit API contracts, then synthesize.
#
# Active implementation is selected by TASK1_SYNTHESIZER in .env:
#   TASK1_SYNTHESIZER=static   (conservative — current behavior)
#   TASK1_SYNTHESIZER=llm      (default — contract-first, more deterministic)
# ---------------------------------------------------------------------------

# API-surface section cap: ~25% of the single-call budget. The skeleton must
# survive truncation because TASK 1 treats it as ground truth — but we still
# bound it so it doesn't crowd out tests and README on large projects.
# Plan 9 F3: was SINGLE_CALL//4 (~79K) — dropped 38% of a 129K API surface at
# task1. Raised to //2 (~159K) so the whole API reaches FR synthesis; the
# context guard still protects total-prompt overflow.
_API_SURFACE_CAP = max(8_000, SINGLE_CALL_CHAR_LIMIT // 2)


def _format_api_surface_section(api_surface: str) -> str:
    """Wrap the captured API surface into a labelled prompt section, head+tail
    truncated to _API_SURFACE_CAP. Returns '' if api_surface is empty."""
    if not api_surface or not api_surface.strip():
        return ""
    text = smart_truncate(
        api_surface,
        total_limit=_API_SURFACE_CAP,
        head=2 * _API_SURFACE_CAP // 3,
        tail=_API_SURFACE_CAP // 3,
    )
    return (
        "\n--- Public API surface (ground truth — pre-strip skeleton) ---\n"
        "Use these signatures and type definitions exactly. Do not invent items not\n"
        "listed here, do not change signatures, and do not contradict the shapes shown\n"
        "below. The behavioral semantics (what each function returns, invariants)\n"
        "are still derived from the tests; this skeleton only defines the SHAPE.\n"
        f"{text}\n"
    )


class Task1Synthesizer(ABC):
    """Analyze test files and produce functional_requirements.md (TASK 1)."""

    @abstractmethod
    def synthesize(
        self,
        project_dir: Path,
        genesis_path: Path,
        functional_req_path: Path,
        test_files: list,
        readme_text: str,
        project_profile: str = "",
        project_context: str = "",
        coverage_strategy: str = "",
        api_surface: str = "",
    ) -> bool:
        """Write functional_requirements.md (and genesis stub if absent). Returns True on success.

        api_surface (when provided) is the pre-strip structural skeleton of the module —
        public signatures, type definitions, function bodies elided. It is structural
        ground truth: TASK 1 should derive BEHAVIOR from the tests, but not invent
        signatures or types that conflict with this skeleton.
        """


class StaticTask1Synthesizer(Task1Synthesizer):
    """
    Current implementation: extract behavioral requirements in natural language,
    then synthesize into functional_requirements.md.
    Uses a single LLM call for small projects and a chunked+hierarchical approach
    for large ones.
    """

    def synthesize(self, project_dir, genesis_path, functional_req_path, test_files,
                   readme_text, project_profile="", project_context="", coverage_strategy="",
                   api_surface=""):
        existing_genesis = read(genesis_path)
        total_chars = sum(len(c) for _, c in test_files)

        if total_chars <= SINGLE_CALL_CHAR_LIMIT:
            ok = with_retry(lambda: self._single(
                genesis_path, functional_req_path, test_files, readme_text,
                existing_genesis, project_profile,
                project_context=project_context,
                coverage_strategy=coverage_strategy,
                api_surface=api_surface,
            ))
        else:
            ok = self._chunked(
                genesis_path, functional_req_path, test_files, readme_text,
                existing_genesis, project_profile,
                project_context=project_context,
                coverage_strategy=coverage_strategy,
                api_surface=api_surface,
            )
        return ok

    def _single(self, genesis_path, functional_req_path, test_files, readme_text,
                existing_genesis, project_profile="", project_context="",
                coverage_strategy="", api_surface=""):
        genesis_exists = bool(existing_genesis.strip())
        _full_tests_section = format_test_files(test_files)
        profile_section = (f"\n--- Project profile (language, build system, workspace layout) ---\n"
                           f"{project_profile}\n") if project_profile.strip() else ""
        context_section = (f"\n--- Project context (behavioral summary) ---\n"
                           f"{project_context}\n") if project_context.strip() else ""

        if not genesis_exists and not genesis_path.exists():
            write(genesis_path, "# genesis.md\n\n(populated by TASK 4 after first iteration)\n")

        # Budget the tests_section against the actual prompt overhead. The
        # single-call path is taken when tests are expected to fit, but
        # measure to be safe — overflow would otherwise hit the context
        # guard's middle-trim, which can split a test file mid-content.
        _probe = self._single_prompt(
            "", readme_text, profile_section, existing_genesis,
            coverage_strategy=coverage_strategy, api_surface=api_surface,
            context_section=context_section,
        )
        _tests_budget = max(2_000, SINGLE_CALL_CHAR_LIMIT - len(_probe))
        tests_section = smart_truncate(_full_tests_section, total_limit=_tests_budget,
                                       head=_tests_budget // 3,
                                       tail=2 * _tests_budget // 3)

        prompt = self._single_prompt(
            tests_section, readme_text, profile_section, existing_genesis,
            coverage_strategy=coverage_strategy, api_surface=api_surface,
            context_section=context_section,
        )
        response = ask(prompt, model=_TASK1_MODEL)
        files = extract_files(response)
        if "functional_requirements.md" in files:
            _write_fr_preserving_notes(functional_req_path, files["functional_requirements.md"])
            return True
        # Fallback: model returned content without FILE markers.
        stripped = response.strip()
        if stripped and len(stripped) > 200:
            _log("  WARNING: no FILE markers — using raw response as requirements.")
            _write_fr_preserving_notes(functional_req_path, stripped)
            return True
        _log(f"  ERROR: {MODEL} did not return functional_requirements.md.")
        _log(f"  Raw response snippet: {response[:500]}")
        return False

    def _single_prompt(self, tests_section: str, readme_text: str, profile_section: str,
                       existing_genesis: str, coverage_strategy: str = "",
                       api_surface: str = "", context_section: str = "") -> str:
        """Return the prompt for single-call requirements extraction (overridable)."""
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        # genesis.md is always shown — when empty it carries a placeholder so the
        # LLM understands it is the document that will accumulate retrospective
        # findings across iterations and should be respected even before first fill.
        if existing_genesis.strip():
            genesis_section = (
                f"\n--- genesis.md (methodology — read-only here; do not output or modify) ---\n"
                f"{existing_genesis}\n"
            )
        else:
            genesis_section = (
                "\n--- genesis.md (methodology — empty for now; will be filled by retrospectives in later iterations) ---\n"
                "(empty)\n"
            )
        return f"""You are working on a deterministic AI-driven software reconstruction experiment.
{_TASK1_THINKING_DIRECTIVE}{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

The goal: given a project's test files and README, produce functional requirements precise
enough that any implementation satisfying them will make all tests pass.

Use the methodology in genesis.md below to guide your analysis (it may be empty on the
first iteration — that is expected; it accumulates retrospective findings across loops).
Use the README, project profile, project context, and test files to understand the
language, framework, and project structure.

README.md:
{readme_text[:_README_CAP]}
{profile_section}{context_section}{strategy_section}{api_section}
Test files:
{tests_section}
{genesis_section}
Your task: produce functional_requirements.md. Requirements must be complete enough that
any implementation satisfying them will pass all tests.

For each public function, method, or trait implementation, capture ALL of the following
(not just the signature):
- What it returns: the format, structure, and content of the return value.
- Behavioral invariants: what must always be true of the output?
- Any concrete examples visible in test assertions (copy them verbatim if present).
A requirement like `fn generate() -> String` is insufficient. A requirement like
`fn generate() -> String — returns a shell completion script string; tests assert it
contains flag names and descriptions in the expected format` is actionable.

CRITICAL: Every requirement must trace directly to a test call or assertion in the files above.
Do NOT add items based on general knowledge of the crate, language, or domain.
If no test calls it, do not include it.

Test helpers vs. production API:
- Do NOT include `fn test_*` or `#[test]` functions — these are the tests, not the code under test.
  Extract the APIs that the test functions CALL (the production symbols being exercised).
- Do NOT include test fixtures, mock builders, or helper utilities defined in the test files.
  Only extract the real production API the tests depend on.
"""

    def _synthesize_group(self, group_partials: list[str], group_idx: int, total_groups: int,
                          readme_text: str, profile_section: str, genesis: str = "",
                          coverage_strategy: str = "", api_surface: str = "") -> str | None:
        """
        Stage-1 hierarchical synthesis: merge a group of raw chunk partials into a
        single condensed intermediate summary. Called once per group.
        Returns the summary text, or None on failure.
        """
        genesis_section = (
            f"\n--- genesis.md (methodology — apply when merging requirements) ---\n"
            f"{genesis}\n"
        ) if genesis.strip() else ""
        api_section = _format_api_surface_section(api_surface)

        # Fill-down: render the scaffold with sized sections empty so the
        # overhead is exact (no hardcoded estimate).
        _readme_cap_str = readme_text[:_README_CAP]
        _merge_scaffold = f"""You are merging a group of partial functional requirements extracted from test files.

This is group {group_idx} of {total_groups}. Each entry was extracted independently from a chunk
of test files. Your task: produce a single, deduplicated, concise summary of all requirements
in this group. Preserve all specific types, function signatures, error conditions, and invariants.
Remove duplicate entries. Do not invent new requirements not present in the input.

README.md (for language/framework context):
{_readme_cap_str}

Partial requirements to merge:


Output a plain structured list of the merged requirements. No filler, no code, no headers.
"""
        _fixed_overhead = (
            len(_merge_scaffold) + len(profile_section)
            + len(genesis_section) + len(api_section)
        )
        _combined_cap = max(10_000, SINGLE_CALL_CHAR_LIMIT - _fixed_overhead)
        _combined_raw = "\n\n".join(group_partials)
        combined = smart_truncate(_combined_raw, total_limit=_combined_cap,
                                  head=_combined_cap // 3, tail=2 * _combined_cap // 3)

        prompt = f"""You are merging a group of partial functional requirements extracted from test files.

This is group {group_idx} of {total_groups}. Each entry was extracted independently from a chunk
of test files. Your task: produce a single, deduplicated, concise summary of all requirements
in this group. Preserve all specific types, function signatures, error conditions, and invariants.
Remove duplicate entries. Do not invent new requirements not present in the input.
{profile_section}{genesis_section}{api_section}
README.md (for language/framework context):
{_readme_cap_str}

Partial requirements to merge:
{combined}

Output a plain structured list of the merged requirements. No filler, no code, no headers.
"""

        def attempt():
            result = ask(prompt, model=_TASK1_MODEL)
            if not result or not result.strip():
                return False
            return result

        raw = with_retry(attempt)
        return str(raw) if raw is not False else None

    def _chunked(self, genesis_path, functional_req_path, test_files, readme_text,
                 existing_genesis, project_profile="", project_context="",
                 coverage_strategy="", api_surface=""):
        _fixed = (
            min(len(readme_text), 2_000)
            + min(len(project_profile), 2_000)
            + min(len(project_context), 2_000)
            + min(len(existing_genesis), 3_000)
            + min(len(coverage_strategy), 4_000)
            + 300   # type manifest header
            + 600   # static instruction text
        )
        _chunk_content_budget = prompt_budget(" " * _fixed)
        chunks = chunk_test_files_by_file(test_files, _chunk_content_budget)
        total_chunks = len(chunks)
        _log(f"  Extracting requirements file-by-file: {total_chunks} file(s) → one call each.")
        _log("  Calibration in progress, progress estimations will be available soon...")

        root = genesis_path.parent
        partials_dir = root / ".task1_partials"
        cache_dir = root / CHUNK_CACHE_DIR
        partials_dir.mkdir(exist_ok=True)
        cache_dir.mkdir(exist_ok=True)

        partial_requirements = []
        progress = ChunkProgress(total=total_chunks)
        cache_hits = 0
        # BL-018: running type manifest — accumulate extracted types across chunks
        # so each chunk knows what has already been described and won't duplicate it.
        accumulated_types: set[str] = set()

        existing_genesis = read(genesis_path)
        genesis_hash = hashlib.sha256(existing_genesis.encode()).hexdigest()[:8]

        for i, chunk in enumerate(chunks, 1):
            partial_file = partials_dir / f"chunk_{i:03d}_of_{total_chunks:03d}.txt"
            tests_section = format_test_files(chunk)
            # Include genesis hash so cache is invalidated when TASK 4 updates genesis.md.
            h = _chunk_hash(tests_section + genesis_hash)

            if partial_file.exists():
                entry = partial_file.read_text()
                # Still update the manifest from cached entries.
                accumulated_types |= _extract_type_names_from_requirements(entry)
                partial_requirements.append(entry)
                progress.finish_chunk(i)
                continue

            cached = _load_cached_chunk(cache_dir, h)
            if cached:
                cache_hits += 1
                accumulated_types |= _extract_type_names_from_requirements(cached)
                partial_file.write_text(cached)
                partial_requirements.append(cached)
                _log(f"  Chunk {i}/{total_chunks}: cache hit (skipping model call).")
                progress.finish_chunk(i)
                continue

            # BL-018: build type manifest header for this chunk.
            type_manifest_header = ""
            if accumulated_types:
                type_list = ", ".join(sorted(accumulated_types)[:40])
                type_manifest_header = (
                    f"\nTypes already extracted in previous chunks (do NOT re-describe these — "
                    f"only add NEW requirements not yet covered): {type_list}\n"
                )

            profile_header = (f"\nProject profile:\n{project_profile}\n") if project_profile.strip() else ""
            context_header = (f"\nProject context (behavioral summary):\n{project_context}\n"
                              ) if project_context.strip() else ""
            prompt = self._chunk_prompt(i, total_chunks, readme_text, tests_section,
                                       profile_header + context_header + type_manifest_header,
                                       genesis=existing_genesis, coverage_strategy=coverage_strategy,
                                       api_surface=api_surface)

            def chunk_attempt(p=prompt):
                result = ask(p)
                if not result or not result.strip():
                    return False
                return result

            progress.start_chunk()
            partial = with_retry(chunk_attempt)
            if partial is False:
                _log(f"  ERROR: failed to extract requirements from chunk {i} after all retries.")
                return False
            progress.finish_chunk(i)

            entry = f"=== Requirements from chunk {i}/{total_chunks} ===\n{partial}"
            partial_file.write_text(entry)
            _save_cached_chunk(cache_dir, h, entry)
            # BL-018: update running type manifest from this chunk's output.
            accumulated_types |= _extract_type_names_from_requirements(partial)
            partial_requirements.append(entry)

        progress.finish()

        if cache_hits:
            _log(f"  Cache: {cache_hits}/{total_chunks} chunks reused from previous iterations.")

        genesis_exists = bool(existing_genesis.strip())
        profile_section = (f"\n--- Project profile (language, build system, workspace layout) ---\n"
                           f"{project_profile}\n") if project_profile.strip() else ""
        context_section_chunked = (f"\n--- Project context (behavioral summary) ---\n"
                                   f"{project_context}\n") if project_context.strip() else ""
        # Concatenate profile + context so synthesizers downstream see both as one
        # passed-through section (avoids changing every call signature).
        profile_section = profile_section + context_section_chunked

        groups = [partial_requirements[i:i + SYNTHESIS_GROUP_SIZE]
                  for i in range(0, len(partial_requirements), SYNTHESIS_GROUP_SIZE)]
        total_groups = len(groups)
        intermediates: list[str] = []

        if total_groups > 1:
            _log(f"  Stage 1: merging {total_chunks} partials into {total_groups} intermediate groups...")
            for g_idx, group in enumerate(groups, 1):
                inter_file = partials_dir / f"intermediate_{g_idx:03d}_of_{total_groups:03d}.txt"

                if inter_file.exists():
                    _log(f"  Group {g_idx}/{total_groups}: already done, loading from disk.")
                    intermediates.append(inter_file.read_text())
                    continue

                _log(f"  Merging group {g_idx}/{total_groups} ({len(group)} partials)...")
                summary = self._synthesize_group(group, g_idx, total_groups, readme_text, profile_section,
                                                genesis=existing_genesis, coverage_strategy=coverage_strategy,
                                                api_surface=api_surface)
                if summary is False or not summary:
                    _log(f"  ERROR: group {g_idx} merge failed after all retries.")
                    return False

                inter_file.write_text(summary)
                intermediates.append(summary)

            all_for_synthesis = "\n\n".join(
                f"=== Group {i+1}/{total_groups} ===\n{s}" for i, s in enumerate(intermediates)
            )
            _log(f"  Stage 1 complete. Intermediate total: {len(all_for_synthesis):,} chars.")
        else:
            all_for_synthesis = "\n\n".join(partial_requirements)

        _log("  Stage 2: final synthesis into output documents...")

        if not genesis_exists and not genesis_path.exists():
            write(genesis_path, "# genesis.md\n\n(populated by TASK 4 after first iteration)\n")

        synthesis_prompt = self._synthesis_prompt(
            readme_text, profile_section, existing_genesis, all_for_synthesis,
            coverage_strategy=coverage_strategy,
            project_dir=genesis_path.parent,
            api_surface=api_surface,
        )

        def synthesis_attempt():
            response = ask(synthesis_prompt, model=_TASK1_MODEL)
            files = extract_files(response)
            if "functional_requirements.md" in files:
                _write_fr_preserving_notes(functional_req_path, files["functional_requirements.md"])
                return True
            # Fallback: model returned content without FILE markers.
            # Accept the raw response if it's non-empty and looks like requirements
            # (not an error message). The model produces correct content — wrong format.
            stripped = response.strip()
            if stripped and len(stripped) > 200:
                _log("  WARNING: no FILE markers in synthesis response — using raw response as requirements.")
                _write_fr_preserving_notes(functional_req_path, stripped)
                return True
            _log("  ERROR: synthesis returned empty or unusable response.")
            _log(f"  Raw response snippet: {response[:300]}")
            return False

        ok = with_retry(synthesis_attempt)
        if ok:
            for f in partials_dir.glob("*.txt"):
                f.unlink()
            with suppress(OSError):
                partials_dir.rmdir()
        return ok

    def _synthesis_prompt(self, readme_text: str, profile_section: str,
                          existing_genesis: str, all_for_synthesis: str,
                          coverage_strategy: str = "",
                          project_dir: Path | None = None,
                          api_surface: str = "") -> str:
        """Return the Stage-2 synthesis prompt (overridable by subclasses)."""
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)

        # BL-022: inject type difficulty signals so synthesis focuses on hard types.
        type_signals_section = ""
        if project_dir is not None:
            type_signals_text = type_signals_read(project_dir)
            if type_signals_text:
                type_signals_section = (
                    f"\n--- Type difficulty signals from test coverage (prioritize these) ---\n"
                    f"{type_signals_text}\n"
                )

        _synthesis_cap = int(SINGLE_CALL_CHAR_LIMIT * 0.60)
        _capped_synthesis = smart_truncate(all_for_synthesis, total_limit=_synthesis_cap,
                                           head=_synthesis_cap // 3, tail=2 * _synthesis_cap // 3)

        genesis_exists = bool(existing_genesis.strip())
        if genesis_exists:
            return f"""You are synthesizing requirements into a final requirements document.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

Use the methodology in genesis.md below to guide synthesis.
Derive the language and framework from the README and requirements below.

README.md:
{readme_text[:_README_CAP]}
{profile_section}{strategy_section}{api_section}{type_signals_section}
--- genesis.md (read-only -- do not output or modify it) ---
{existing_genesis}

Requirements (merged from all test chunks):
{_capped_synthesis}

Your task: produce functional_requirements.md -- the complete, deduplicated, final
requirements for this specific project.

CRITICAL: Only include items present in the chunk extractions above.
Do NOT add requirements based on general knowledge of the project or language.
"""
        return f"""You are synthesizing requirements into a final requirements document.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

Derive the language and framework from the README and requirements below.

README.md:
{readme_text[:_README_CAP]}
{profile_section}{strategy_section}{api_section}{type_signals_section}
Requirements (merged from all test chunks):
{_capped_synthesis}

Your task: produce functional_requirements.md -- the complete, deduplicated, final
requirements for this specific project.

CRITICAL: Only include items present in the chunk extractions above.
Do NOT add requirements based on general knowledge of the project or language.
"""

    def _chunk_prompt(self, chunk_idx: int, total_chunks: int, readme_text: str,
                      tests_section: str, profile_header: str, genesis: str = "",
                      coverage_strategy: str = "", api_surface: str = "") -> str:
        """Return the per-chunk extraction prompt (overridable by subclasses)."""
        genesis_section = (
            f"\n--- genesis.md (methodology — apply when extracting requirements) ---\n"
            f"{genesis}\n"
        ) if genesis.strip() else ""
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        return f"""You are extracting functional requirements from a subset of test files for a software project.
{_TASK1_THINKING_DIRECTIVE}This is chunk {chunk_idx} of {total_chunks} -- there are more test files not shown here.
All chunks will be synthesized into one complete requirements document after all chunks are processed.
Focus only on what you can directly observe in the tests below -- do not invent behavior.
{profile_header}{genesis_section}{strategy_section}{api_section}
README.md:
{readme_text[:_README_CAP]}

Test files (chunk {chunk_idx}/{total_chunks}):
{tests_section}

From these tests extract every behavior, interface, data structure, error condition, and invariant
that a correct implementation must satisfy. Be exhaustive but grounded -- only extract what the
tests explicitly verify. Output a plain structured list only -- no code, no filler, just requirements.
Derive the language, types, and function signatures from what you read.
"""


class LLMTask1Synthesizer(StaticTask1Synthesizer):
    """
    Contract-first / TDD implementation.

    All three extraction points are upgraded to emit API contracts rather than
    behavioral descriptions:

      _single_prompt()      — small project path (one LLM call): extracts contracts
                              directly from the full test set.
      _chunk_prompt()       — chunked path per-chunk: extracts contracts per chunk.
      _synthesize_group()   — Stage-1 merge: deduplicates contracts, flags conflicts.
      _synthesis_prompt()   — Stage-2 final synthesis: produces functional_requirements.md
                              as a contract document (module paths, signatures, types,
                              invariants) rather than behavioral prose.

    The pipeline infrastructure (caching, chunking, retry, disk management)
    is inherited from StaticTask1Synthesizer unchanged.
    """

    def _single_prompt(self, tests_section: str, readme_text: str, profile_section: str,
                       existing_genesis: str, coverage_strategy: str = "",
                       api_surface: str = "", context_section: str = "") -> str:
        # genesis is always shown — empty placeholder on first iteration so the
        # LLM knows this is the document accumulating retrospective findings.
        if existing_genesis.strip():
            genesis_section = (f"\n--- genesis.md (methodology — read-only here; do not output or modify) ---\n"
                               f"{existing_genesis}\n")
        else:
            genesis_section = ("\n--- genesis.md (methodology — empty for now; will be filled by retrospectives in later iterations) ---\n"
                               "(empty)\n")
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        return f"""You are performing contract extraction from test files as part of a
contract-first TDD reconstruction experiment.
{_TASK1_THINKING_DIRECTIVE}{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
Do not output anything outside those markers.
{profile_section}{context_section}{strategy_section}{api_section}
README.md:
{readme_text[:_README_CAP]}
{genesis_section}
Test files:
{tests_section}

Your task: produce functional_requirements.md as a precise API contract document.
For every function, method, struct, enum, trait, or module that the tests call or assert:
  - Exact module path or import (e.g. `crate_name::function_name`, `crate_name::TypeName`)
  - Full function/method signature: name, argument names and types, return type
  - Struct/enum/trait definition: fields with types, variants, trait methods with signatures
  - Error types returned or matched
  - Invariants explicitly asserted (e.g. field equals X, output matches Y)
  - For functions returning strings: list every `assert!(output.contains("..."))` substring verbatim.
    A signature like `fn generate() -> String` is INCOMPLETE without the expected output content.
    Example of complete requirement:
      `Bash::generate(cmd_name, cmd) -> String`
      Output must contain: `cmd="<cmd_name>_<subcmd>"`, `opts="--flag -f"`, `cmds="sub1 sub2"`

Rules:
  - Only include what the tests literally call or assert — do not invent.
  - Use exact names, types, and paths as written in the tests.
  - Include concrete type parameters where generics appear in the tests.
  - Group by module/crate. Use concise structured notation, not prose.
  - Do NOT include `fn test_*` or `#[test]` functions — these are the tests themselves, not the code under test.
    Extract the APIs that the test functions CALL (e.g. `Bash::generate(...)`, not `fn test_bash_generator()`).
  - Do NOT include `make_mock_command` or other test helpers — extract the real production API being tested.
  - For every `assert!(result.contains("..."))` or `assert_eq!(result, "...")` in the tests,
    copy the expected string verbatim into the requirements as a behavioral constraint.
"""

    def _synthesis_prompt(self, readme_text: str, profile_section: str,
                          existing_genesis: str, all_for_synthesis: str,
                          coverage_strategy: str = "",
                          project_dir: "Path | None" = None,
                          api_surface: str = "") -> str:
        genesis_exists = bool(existing_genesis.strip())
        genesis_section = (f"\n--- genesis.md (read-only — do not output or modify it) ---\n"
                           f"{existing_genesis}\n") if genesis_exists else ""
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        _readme_cap_str = readme_text[:_README_CAP]
        _contract_synthesis_scaffold = f"""You are producing the final API contract document from contract extractions
across all test chunks. This is the last step of a contract-first TDD reconstruction.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
Do not output anything outside those markers.

README.md:
{_readme_cap_str}

Contract extractions (merged from all test chunks):


Your task: produce functional_requirements.md as the complete, deduplicated API contract.
For every function, method, struct, enum, trait, or module extracted above:
  - Exact module path or import
  - Full signature: name, argument names and types, return type
  - Struct/enum/trait definitions with fields, variants, trait method signatures
  - Error types
  - Invariants explicitly asserted in the tests

Rules:
  - Deduplicate: same symbol + same signature = one entry.
  - Conflicts (same name, different signature): keep all variants, annotate "(conflict)".
  - Do not add, invent, or generalise beyond what is in the extractions above.
  - Group by module/crate. Use concise structured notation, not prose.
"""
        _fixed_contract_overhead = (
            len(_contract_synthesis_scaffold) + len(profile_section)
            + len(strategy_section) + len(api_section) + len(existing_genesis)
        )
        _synthesis_cap = max(10_000, SINGLE_CALL_CHAR_LIMIT - _fixed_contract_overhead)
        _capped_synthesis = smart_truncate(all_for_synthesis, total_limit=_synthesis_cap,
                                           head=_synthesis_cap // 3, tail=2 * _synthesis_cap // 3)
        return f"""You are producing the final API contract document from contract extractions
across all test chunks. This is the last step of a contract-first TDD reconstruction.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
Do not output anything outside those markers.
{profile_section}{strategy_section}{api_section}
README.md:
{readme_text[:_README_CAP]}
{genesis_section}
Contract extractions (merged from all test chunks):
{_capped_synthesis}

Your task: produce functional_requirements.md as the complete, deduplicated API contract.
For every function, method, struct, enum, trait, or module extracted above:
  - Exact module path or import
  - Full signature: name, argument names and types, return type
  - Struct/enum/trait definitions with fields, variants, trait method signatures
  - Error types
  - Invariants explicitly asserted in the tests

Rules:
  - Deduplicate: same symbol + same signature = one entry.
  - Conflicts (same name, different signature): keep all variants, annotate "(conflict)".
  - Do not add, invent, or generalise beyond what is in the extractions above.
  - Group by module/crate. Use concise structured notation, not prose.
"""

    def _chunk_prompt(self, chunk_idx: int, total_chunks: int, readme_text: str,
                      tests_section: str, profile_header: str, genesis: str = "",
                      coverage_strategy: str = "", api_surface: str = "") -> str:
        genesis_section = (
            f"\n--- genesis.md (methodology — apply when extracting contracts) ---\n"
            f"{genesis}\n"
        ) if genesis.strip() else ""
        strategy_section = (
            f"\n--- Coverage strategy (import patterns, type construction, test conventions) ---\n"
            f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
        ) if coverage_strategy.strip() else ""
        api_section = _format_api_surface_section(api_surface)
        return f"""You are performing contract extraction from test files as the first step of
a contract-first TDD reconstruction experiment.
{_TASK1_THINKING_DIRECTIVE}This is chunk {chunk_idx} of {total_chunks}. All chunks will be combined into a single
implementation contract document.
{profile_header}{genesis_section}{strategy_section}{api_section}
README.md:
{readme_text[:_README_CAP]}

Test files (chunk {chunk_idx}/{total_chunks}):
{tests_section}

Your task: extract every concrete API element that these tests call or depend on.
For each item, record:
  - The exact module path or import (e.g. `argh::from_env`, `argh_derive::ArghParser`)
  - The full function/method signature: name, all argument names and types, return type
  - Any struct, enum, or trait definition used (fields, variants, trait methods)
  - Any error type returned or matched
  - Any invariant explicitly asserted (e.g. output equals X, field must be Y)

Rules:
  - Only extract what the tests literally call or assert — do not invent.
  - Use the exact names, types, and paths as written in the tests.
  - If a type is generic, include the concrete type parameter used in the test.
  - Output a plain structured list only. No prose, no examples, no filler.
"""

    def _synthesize_group(self, group_partials: list[str], group_idx: int, total_groups: int,
                          readme_text: str, profile_section: str, genesis: str = "",
                          coverage_strategy: str = "", api_surface: str = "") -> str | None:
        """
        Merge a group of contract partials into a deduplicated intermediate contract list.
        Overrides the behavioral merge prompt with a contract-aware one.
        """
        genesis_section = (
            f"\n--- genesis.md (methodology — apply when merging contracts) ---\n"
            f"{genesis}\n"
        ) if genesis.strip() else ""
        api_section = _format_api_surface_section(api_surface)

        _readme_cap_str = readme_text[:_README_CAP]
        _contract_merge_scaffold = f"""You are merging contract extractions from multiple test file chunks.

This is group {group_idx} of {total_groups}. Each entry lists API contracts extracted
from a subset of test files. Your task: produce a single deduplicated contract list.

Rules:
  - Preserve exact module paths, function signatures, types, and invariants.
  - Merge duplicate entries (same symbol, same signature) into one.
  - Do not add, invent, or generalise — only what is present in the input.
  - If two entries conflict (same name, different signature), keep both and annotate with "(conflict)".

README.md (for language/framework context):
{_readme_cap_str}

Contract partials to merge:


Output a plain structured list of the merged contracts. No prose, no code blocks, no headers.
"""
        _fixed_overhead = (
            len(_contract_merge_scaffold) + len(profile_section)
            + len(genesis_section) + len(api_section)
        )
        _combined_cap = max(10_000, SINGLE_CALL_CHAR_LIMIT - _fixed_overhead)
        _combined_raw = "\n\n".join(group_partials)
        combined = smart_truncate(_combined_raw, total_limit=_combined_cap,
                                  head=_combined_cap // 3, tail=2 * _combined_cap // 3)

        prompt = f"""You are merging contract extractions from multiple test file chunks.

This is group {group_idx} of {total_groups}. Each entry lists API contracts extracted
from a subset of test files. Your task: produce a single deduplicated contract list.

Rules:
  - Preserve exact module paths, function signatures, types, and invariants.
  - Merge duplicate entries (same symbol, same signature) into one.
  - Do not add, invent, or generalise — only what is present in the input.
  - If two entries conflict (same name, different signature), keep both and annotate with "(conflict)".
{profile_section}{genesis_section}{api_section}
README.md (for language/framework context):
{readme_text[:_README_CAP]}

Contract partials to merge:
{combined}

Output a plain structured list of the merged contracts. No prose, no code blocks, no headers.
"""

        def attempt():
            result = ask(prompt, model=_TASK1_MODEL)
            if not result or not result.strip():
                return False
            return result

        raw = with_retry(attempt)
        return str(raw) if raw is not False else None


def _filter_proc_macro_test_files(test_files, crate_name: str = ""):
    """Keep test files that use #[derive(...)] or the crate's own #[crate_name(...)]
    attribute. Falls back to all files if none match. (Regex kept in pipeline.py — Option 1.)"""
    attr_pattern = r'#\[derive\('
    if crate_name:
        safe = re.escape(crate_name.replace("-", "_"))
        attr_pattern += rf'|#\[{safe}\('
    relevant = [
        (rel, content) for rel, content in test_files
        if re.search(attr_pattern, content)
    ]
    return relevant or test_files


def _proc_macro_fallback_requirements(relevant, crate_name: str = "") -> str:
    """Static fallback: scan tests for derive/attribute usage and synthesise minimal
    proc-macro requirements. (Rust-source regex kept in pipeline.py — Option 1.)"""
    derives: set[str] = set()
    attrs: set[str] = set()
    _attr_name = (crate_name or "").replace("-", "_") or "derive"
    _attr_re = re.compile(rf'#\[{re.escape(_attr_name)}\(([^)]+)\)')
    for _, content in relevant:
        for m in re.finditer(r'#\[derive\(([^)]+)\)', content):
            for d in m.group(1).split(','):
                derives.add(d.strip())
        for m in _attr_re.finditer(content):
            attrs.add(m.group(1).strip())
    req_content = "# Proc-macro interface\n\n"
    req_content += "## Derive macros\n"
    for d in sorted(derives):
        req_content += f"- `#[derive({d})]` — implement the `{d}` trait on annotated types\n"
    if attrs:
        req_content += "\n## Helper attributes\n"
        for a in sorted(attrs):
            req_content += f"- `#[{_attr_name}({a})]`\n"
    req_content += (
        "\n## Entry points\n"
        "Each derive macro must be registered with `#[proc_macro_derive(Name)]`.\n"
        "Input: `proc_macro::TokenStream`, Output: `proc_macro::TokenStream`.\n"
        "Parse input with `syn::parse_macro_input!(input as syn::DeriveInput)`.\n"
        "Generate output with `quote::quote! { impl Name for #ident { ... } }`.\n"
    )
    return req_content


def _task1_synthesizer_factory() -> Task1Synthesizer:
    backend = os.getenv("TASK1_SYNTHESIZER", "llm").lower()
    if backend == "static":
        return StaticTask1Synthesizer()
    return LLMTask1Synthesizer()


# slice 5b: _task1_proc_macro moved to application.tasks.task1.


def task1(project_dir, genesis_path, functional_req_path, test_files, readme_text,
          project_profile: str = "", crate_type: str = "", module_name: str = "",
          project_context: str = "", coverage_strategy: str = ""):
    # slice 5b: extracted to application.tasks.task1; thin shim binding deps.
    return _task1_impl(
        project_dir, genesis_path, functional_req_path, test_files, readme_text, _TASK1_DEPS,
        project_profile=project_profile, crate_type=crate_type, module_name=module_name,
        project_context=project_context, coverage_strategy=coverage_strategy,
    )


# ---------------------------------------------------------------------------
# Source code generation (TASK 2) — SOLID design
#
# Two implementations:
#   StaticTask2Generator  — current prompt-based approach (one big ask per attempt)
#   LLMTask2Generator     — file-by-file: first asks which files are needed, then
#                           generates each independently (better for large workspaces)
#
# Active implementation is selected by TASK2_GENERATOR in .env:
#   TASK2_GENERATOR=static    (default — proven, conservative)
#   TASK2_GENERATOR=llm       (experimental — file-by-file)
# ---------------------------------------------------------------------------

class Task2Generator(ABC):
    """Generate source files from functional requirements (TASK 2)."""

    @staticmethod
    def _common_sections(
        project_profile: str,
        workspace_structure: str,
        build_error: str,
        locked_dep_context: str,
    ) -> tuple[str, str, str, str]:
        """Build the four prompt sections shared by all Task2 generators."""
        profile = (
            f"\nProject profile (language, build system, workspace layout):\n{project_profile}\n"
        ) if project_profile.strip() else ""
        workspace = (
            f"\nWorkspace structure (MUST follow exactly — "
            f"every required entry-point file listed below must be generated):\n{workspace_structure}\n"
        ) if workspace_structure.strip() else ""
        build_err = (
            f"\n⚠️  PREVIOUS COMPILE ATTEMPT FAILED. Fix ALL errors below before regenerating.\n"
            f"Do not reuse the previous code. Generate corrected files that address every error.\n\n"
            f"Compiler output:\n{build_error}\n"
        ) if build_error.strip() else ""
        locked_dep = f"\n{locked_dep_context}\n" if locked_dep_context.strip() else ""
        return profile, workspace, build_err, locked_dep

    @abstractmethod
    def generate(
        self,
        project_dir: Path,
        genesis_path: Path,
        functional_req_path: Path,
        manifest_path: Path,
        existing_test_files: list,
        project_profile: str = "",
        workspace_structure: str = "",
        build_error: str = "",
        locked_dep_context: str = "",
        current_module: str = "",
        module_dir_rel: str = "",
        whole_project_context: str = "",
        coverage_strategy: str = "",
        api_surface: str = "",
        prev_source_context: str = "",
    ) -> bool:
        """Write source files to disk. Returns True on success.

        api_surface (when provided) is the pre-strip public-API skeleton. TASK 1
        already received it; passing it here too prevents loss-in-translation
        if TASK 1's requirements doc dropped any signature -- TASK 2 sees the
        ground truth directly.
        """


class StaticTask2Generator(Task2Generator):
    """
    Current implementation: one LLM call per attempt that produces all source
    files in a single response using <FILE path="..."> markers.
    """

    def generate(self, project_dir, genesis_path, functional_req_path, manifest_path,
                 existing_test_files, project_profile="", workspace_structure="", build_error="",
                 locked_dep_context="", current_module="", module_dir_rel="",
                 whole_project_context="", coverage_strategy="", api_surface="",
                 prev_source_context=""):
        _log("\nTASK 2: Implementing source code from functional requirements")
        genesis = read(genesis_path)
        functional_req = read(functional_req_path)

        profile_section, workspace_section, build_error_section, locked_dep_section = \
            self._common_sections(project_profile, workspace_structure, build_error, locked_dep_context)

        downstream_api_static = scan_downstream_api(project_dir, current_module) if current_module else ""

        proc_macro_note = ""
        if "proc-macro target" in workspace_structure and current_module:
            proc_macro_note = (
                "\n⚠️  PROC-MACRO CRATE — mandatory rules (syn = \"2.0\", default features only):\n"
                "\n  IMPORTS — lib.rs must have these at the top:\n"
                "    use proc_macro::TokenStream;\n"
                "    use proc_macro2::{TokenStream as TokenStream2, Span};\n"
                "    use syn::{parse_macro_input, DeriveInput, Data, Fields, FieldsNamed, FieldsUnnamed,\n"
                "              Field, Ident, Type, Attribute, Meta, Expr, Lit, LitStr, LitInt,\n"
                "              Path, PathSegment, Generics, Error as SynError};\n"
                "    use quote::{quote, quote_spanned, ToTokens};\n"
                "    use syn::spanned::Spanned;\n"
                "\n  AVAILABLE syn 2.0 types (default features, no 'full' needed):\n"
                "    DeriveInput, Data, DataStruct, DataEnum, DataUnion\n"
                "    Fields, FieldsNamed, FieldsUnnamed, Field\n"
                "    Attribute, Meta, MetaList, MetaNameValue\n"
                "    Ident, Type, TypePath, Generics, GenericParam\n"
                "    Path, PathSegment, PathArguments, AngleBracketedGenericArguments\n"
                "    Expr, ExprLit, Lit, LitStr, LitInt, LitBool\n"
                "    syn::Error (for compile-time errors via .to_compile_error())\n"
                "    Span comes from proc_macro2::Span, NOT syn::Span\n"
                "\n  NOT AVAILABLE without features = [\"full\"]:\n"
                "    ItemEnum, ItemFn, ItemStruct, ItemImpl — DO NOT USE\n"
                "    PatIdent, Stmt, Block, Arm — DO NOT USE\n"
                "\n  STRUCTURE rules:\n"
                "  - Entry-point fn MUST be tagged `#[proc_macro_derive(TraitName)]` or `#[proc_macro_attribute]`.\n"
                "  - proc-macro crates can ONLY export #[proc_macro_derive]/#[proc_macro_attribute]/#[proc_macro] fns.\n"
                "  - ALL other functions and types MUST be private (no `pub`). Example:\n"
                "      WRONG: `pub fn helper(...)` or `pub struct Foo` — these cause 'cannot export' compile error\n"
                "      RIGHT: `fn helper(...)` or `struct Foo` — private, no pub keyword\n"
                "  - Use `std::env` not bare `env::` for environment variables.\n"
                "  - A proc-macro crate does NOT define runtime types from the crates it serves —\n"
                "    those types live in the runtime crate(s) and are referenced by full path\n"
                "    inside `quote! { ... }` blocks (e.g. `runtime_crate::SomeType`).\n"
                "  - Parse input as `DeriveInput` (not `ItemStruct`/`ItemEnum`/`ItemFn` — those require 'full' feature):\n"
                "      let input = parse_macro_input!(input as DeriveInput);\n"
                "      match input.data { Data::Struct(s) => ..., Data::Enum(e) => ..., _ => ... }\n"
                "  - `syn::AttributeArgs` does not exist in syn 2.x — parse attributes via `Attribute::parse_nested_meta`.\n"
                "  - Iterating FieldsNamed: use `fields.named.iter()` not `fields.iter()`.\n"
                "    FieldsNamed has a `.named` field (Punctuated<Field, Comma>), not a direct iterator.\n"
                "  - Do NOT declare `extern crate proc_macro;` — it is implicit in proc-macro crates.\n"
                "  - Structs are data only — NO function signatures inside struct bodies.\n"
                "  - References in struct fields MUST have lifetimes: `struct Foo<'a> { x: &'a Bar }`\n"
                "  - Do NOT use inline `mod xyz { }` blocks — put helpers directly in lib.rs or in a separate file.\n"
                "  - If a submodule file `foo.rs` is generated, declare it in lib.rs as `mod foo;` (no braces).\n"
                "  - Every `mod foo;` declaration needs a corresponding foo.rs file.\n"
            )

        module_scope_section = (
            f"\n⚠️  MODULE SCOPE: Generate source files ONLY for the `{current_module}` module.\n"
            f"Only output files whose path starts with `{module_dir_rel}`.\n"
            f"Do NOT generate files for any other module in the workspace.\n"
            f"{downstream_api_static}"
            f"{proc_macro_note}"
        ) if current_module else ""

        # BL-025: read known compile pitfalls for this module.
        pitfalls_section = ""
        if current_module:
            pitfalls = compile_patterns_read(project_dir, current_module)
            if pitfalls:
                pitfalls_section = f"\n{pitfalls}\n"

        # BL-023: inject test assertions as ground truth.
        assertions_section = ""
        if existing_test_files:
            test_file_list = [(rel, (project_dir / rel).read_text(encoding="utf-8"))
                              for rel in existing_test_files
                              if (project_dir / rel).exists()]
            assertions = _extract_test_assertions(test_file_list, module_dir_rel)
            if assertions:
                assertions_section = f"\n{assertions}\n"

        # BL-022/BL-024: read structured rules relevant to this module.
        structured_rules = genesis_rules_read(project_dir, module_scope=current_module or "*")
        rules_section = f"\n{structured_rules}\n" if structured_rules else ""

        api_section = _format_api_surface_section(api_surface)
        prev_source_section = (
            f"\nPrevious attempt's generated code (failed the tests — improve on this, do not repeat its mistakes):\n"
            f"{prev_source_context}\n"
        ) if prev_source_context.strip() else ""

        def attempt():
            # Cap genesis and whole_project_context to keep the prompt within
            # the input budget. Without these caps, a large genesis or huge
            # whole-project context could push the prompt past the limit and
            # silently truncate other sections.
            _genesis_capped = trim_to_line(genesis, _GENESIS_CAP, "\n... (genesis truncated)")
            # Render scaffold with sized sections empty for exact overhead.
            _mono_scaffold = f"""You are implementing a software project from functional requirements.
{_TASK2_THINKING_DIRECTIVE}
Process (genesis.md):

{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{pitfalls_section}{rules_section}{api_section}{prev_source_section}
Functional requirements:

{assertions_section}
Produce ALL source files needed to satisfy these requirements.
Every module/crate/package in the workspace structure above MUST have its required entry-point
file (e.g. each Rust crate declaring a lib target needs src/lib.rs; a bin target needs src/main.rs).
The TESTFILE blocks above are READ-ONLY spec; do NOT regenerate any test files.

{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}Output ALL files one after another using only those markers.
"""
            _mono_overhead = len(_mono_scaffold) + len(_genesis_capped) + len(functional_req)
            _wpc_budget = max(0, SINGLE_CALL_CHAR_LIMIT - _mono_overhead)
            _whole_project_capped = smart_truncate(whole_project_context, total_limit=_wpc_budget,
                                                   head=_wpc_budget // 4, tail=3 * _wpc_budget // 4) \
                                    if whole_project_context else ""
            prompt = f"""You are implementing a software project from functional requirements.
{_TASK2_THINKING_DIRECTIVE}
Process (genesis.md):
{_genesis_capped}
{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{pitfalls_section}{rules_section}{api_section}{prev_source_section}
Functional requirements:
{functional_req}
{assertions_section}{_whole_project_capped}
Produce ALL source files needed to satisfy these requirements.
Every module/crate/package in the workspace structure above MUST have its required entry-point
file (e.g. each Rust crate declaring a lib target needs src/lib.rs; a bin target needs src/main.rs).
The TESTFILE blocks above are READ-ONLY spec; do NOT regenerate any test files.

{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}Output ALL files one after another using only those markers.
"""
            response = ask(prompt, model=_TASK2_MODEL)
            files = extract_files(response)
            if not files:
                _log(f"  ERROR: {MODEL} returned no source files.")
                _log(f"  Raw response snippet: {response[:500]}")
                return False
            # Hard filter: discard any files outside the current module's directory.
            if module_dir_rel:
                outside = [p for p in files if not p.startswith(module_dir_rel)]
                if outside:
                    _log(f"  DISCARDED (outside module scope): {', '.join(outside)}")
                files = {p: c for p, c in files.items() if p.startswith(module_dir_rel)}
                if not files:
                    _log(f"  ERROR: no files in scope after filtering to `{module_dir_rel}`.")
                    return False
            written = write_source_files(files, project_dir, manifest_path, existing_test_files)
            _log(f"  {written} source file(s) written.")
            return True

        return with_retry(attempt)


class LLMTask2Generator(Task2Generator):
    """
    File-by-file implementation: first asks the model which files need to be
    created, then generates each file in a separate focused call.
    Better for large workspaces where a single call can't fit all files.
    """

    def generate(self, project_dir, genesis_path, functional_req_path, manifest_path,
                 existing_test_files, project_profile="", workspace_structure="", build_error="",
                 locked_dep_context="", current_module="", module_dir_rel="",
                 whole_project_context="", coverage_strategy="", api_surface="",
                 prev_source_context=""):
        _log("\nTASK 2: Implementing source code from functional requirements (file-by-file)")
        genesis = read(genesis_path)
        functional_req = read(functional_req_path)

        profile_section, workspace_section, build_error_section, locked_dep_section = \
            self._common_sections(project_profile, workspace_structure, build_error, locked_dep_context)

        # Determine if current_module is a lib crate (has lib.rs entry point)
        lib_rs_first_note = ""
        if current_module and module_dir_rel:
            lib_rs_path = f"{module_dir_rel}src/lib.rs"
            lib_rs_first_note = (
                f"\n⚠️  FIRST FILE REQUIRED: The FIRST file you list MUST be `{lib_rs_path}`. "
                f"This is the crate root for `{current_module}`. All other modules are declared "
                f"from lib.rs with `pub mod` statements. Without lib.rs the crate cannot compile.\n"
            )

        downstream_api_section = ""
        if current_module:
            downstream_api_section = scan_downstream_api(project_dir, current_module)

        rust_module_rules = ""
        if current_module and module_dir_rel:
            rust_module_rules = (
                "\n⚠️  RUST MODULE FILE RULES:\n"
                "1. A `foo.rs` file IS the module `foo`. Write its contents directly — do NOT wrap "
                "them in `pub mod foo { ... }`. That creates double nesting and won't compile.\n"
                "2. Types declared in lib.rs at the crate root are referenced from submodules as "
                "`use crate::TypeName;`. Submodules MUST NOT redefine root-level types.\n"
                "3. Submodule types declared at the root must have real field/variant definitions "
                "in lib.rs — not empty stubs.\n"
            )

        module_scope_section = (
            f"\n⚠️  MODULE SCOPE: Generate files ONLY for the `{current_module}` crate.\n"
            f"This is a Cargo workspace. Every file path MUST start with `{module_dir_rel}`.\n"
            f"CORRECT: `{module_dir_rel}src/lib.rs`, `{module_dir_rel}src/parse_attrs.rs`\n"
            f"WRONG:   `src/lib.rs`, `src/{current_module}/lib.rs`, `{current_module}/lib.rs`\n"
            f"Any file not starting with `{module_dir_rel}` will be discarded.\n"
            f"{lib_rs_first_note}"
            f"{downstream_api_section}"
            f"{rust_module_rules}"
        ) if current_module else ""

        # BL-025: read known compile pitfalls for this module.
        pitfalls_section_llm = ""
        if current_module:
            pitfalls_llm = compile_patterns_read(project_dir, current_module)
            if pitfalls_llm:
                pitfalls_section_llm = f"\n{pitfalls_llm}\n"

        # BL-023: inject test assertions as ground truth.
        assertions_section_llm = ""
        if existing_test_files:
            test_file_list_llm = [(rel, (project_dir / rel).read_text(encoding="utf-8"))
                                  for rel in existing_test_files
                                  if (project_dir / rel).exists()]
            assertions_llm = _extract_test_assertions(test_file_list_llm, module_dir_rel)
            if assertions_llm:
                assertions_section_llm = f"\n{assertions_llm}\n"

        # BL-024: structured rules from knowledge store.
        structured_rules_llm = genesis_rules_read(project_dir, module_scope=current_module or "*")
        rules_section_llm = f"\n{structured_rules_llm}\n" if structured_rules_llm else ""

        api_section = _format_api_surface_section(api_surface)
        # Fill-down: measure all fixed sections, give remainder to functional_req then whole_ctx.
        _genesis_truncated_plan = trim_to_line(genesis, _GENESIS_CAP, "\n... (genesis truncated)")
        # Render the plan prompt scaffold with measured sections empty to get
        # an exact overhead (replaces hardcoded estimate).
        _plan_scaffold = f"""You are planning the implementation of a software project.

Process (genesis.md):

{""}{""}{""}{""}{""}{""}{""}{""}{""}
Functional requirements:

{""}{""}
List ALL source files that must be created to implement these requirements.
Do NOT include: test files, config files (Cargo.toml, package.json, build.gradle, etc.), lock files, or any non-source file.
Only source code files (.rs, .py, .js, .ts, .go, .java, etc.).
Output one file path per line, relative to the project root. No other text.
"""
        _plan_fixed_size = (
            len(_plan_scaffold)
            + len(_genesis_truncated_plan) + len(profile_section) + len(workspace_section) +
            len(module_scope_section) + len(locked_dep_section) + len(build_error_section) +
            len(pitfalls_section_llm) + len(rules_section_llm) + len(api_section) +
            len(assertions_section_llm)
        )
        _plan_remaining = max(10_000, SINGLE_CALL_CHAR_LIMIT - _plan_fixed_size)
        _req_cap = _plan_remaining * 2 // 3
        _ctx_cap = _plan_remaining * 1 // 3
        _functional_req_capped = smart_truncate(functional_req, total_limit=_req_cap,
                                                head=_req_cap // 3, tail=2 * _req_cap // 3)
        _whole_ctx_capped = smart_truncate(whole_project_context, total_limit=_ctx_cap,
                                           head=_ctx_cap // 4, tail=3 * _ctx_cap // 4)

        # Step 1: ask model for the list of files to create.
        plan_prompt = f"""You are planning the implementation of a software project.

Process (genesis.md):
{_genesis_truncated_plan}
{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{pitfalls_section_llm}{rules_section_llm}{api_section}
Functional requirements:
{_functional_req_capped}
{assertions_section_llm}{_whole_ctx_capped}
List ALL source files that must be created to implement these requirements.
Do NOT include: test files, config files (Cargo.toml, package.json, build.gradle, etc.), lock files, or any non-source file.
Only source code files (.rs, .py, .js, .ts, .go, .java, etc.).
Output one file path per line, relative to the project root. No other text.
"""
        file_list_response = ask(plan_prompt, model=_TASK2_MODEL)
        file_paths = []
        project_name = project_dir.name  # e.g. "argh-master"
        for line in file_list_response.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip list markers: "- ", "* ", "1. ", "2) ", etc.
            line = re.sub(r'^[\-\*\d]+[\.\):]?\s*', '', line)
            # Strip inline backticks and trailing punctuation (commas, semicolons, etc.)
            line = line.strip('`').strip(',;')
            # Strip trailing prose annotations (everything after first whitespace)
            line = line.split()[0] if line.split() else line
            # Final strip of any remaining backticks/punctuation after split.
            # Also strip trailing colons: model often uses `path`: description format.
            line = line.strip('`').strip(',;:').strip('`')
            # Normalise absolute paths to relative: strip everything up to project root
            if line.startswith('/'):
                try:
                    line = str(Path(line).relative_to(project_dir))
                except ValueError:
                    # Absolute path not under project_dir — skip it
                    continue
            # Strip leading project dir name if model included it (e.g. "argh-master/argh/src/lib.rs")
            if line.startswith(project_name + '/'):
                line = line[len(project_name) + 1:]
            # Final safety: strip any remaining leading slashes or dots
            line = line.lstrip('./')
            # Only keep lines that look like file paths (contain a dot and at least one slash)
            if line and '/' in line and '.' in line:
                file_paths.append(line)
        if not file_paths:
            _log("  ERROR: model returned no file list.")
            return False

        # Hard filter: discard files outside current module scope
        if module_dir_rel:
            outside = [p for p in file_paths if not p.startswith(module_dir_rel)]
            if outside:
                _log(f"  DISCARDED (outside module scope): {', '.join(outside)}")
            file_paths = [p for p in file_paths if p.startswith(module_dir_rel)]
            if not file_paths:
                _log(f"  ERROR: no files in scope after filtering to `{module_dir_rel}`.")
                return False

        _log(f"  Planning: {len(file_paths)} file(s) to generate.")

        # Step 2: generate each file individually, passing already-generated files as context
        # so each file can see what the others define (critical for cross-file type references).
        all_files: dict[str, str] = {}
        for file_path in file_paths:
            # Multi-file roadmap so the LLM doesn't try to cram the whole crate
            # into the current file.
            _file_list_parts = [
                f"\nCrate `{current_module}` consists of {len(file_paths)} "
                f"source files, each generated in a separate call. "
                f"This call generates ONE file; the others have been or will be "
                f"generated by other calls. Trust they will exist — reference "
                f"items from pending files by their declared module path."
                f"\nFile list:"
            ]
            for _fp in file_paths:
                if _fp == file_path:
                    _marker, _status = "→", "THIS CALL"
                elif _fp in all_files:
                    _marker, _status = "✓", "already generated"
                else:
                    _marker, _status = " ", "pending"
                _file_list_parts.append(f"  [{_marker}] {_fp}   ({_status})")
            file_roadmap_section = "\n".join(_file_list_parts) + "\n"

            prev_files_section = ""
            if all_files:
                parts = ["\nAlready-generated files for this module (for context — do NOT regenerate them):"]
                for prev_path, prev_content in all_files.items():
                    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content}\n</FILE>')
                prev_files_section = "\n".join(parts) + "\n"

            file_prompt = f"""You are implementing a single source file for a software project.
{_TASK2_THINKING_DIRECTIVE}
Process (genesis.md):
{trim_to_line(genesis, _GENESIS_CAP, "\n... (genesis truncated)")}
{file_roadmap_section}{profile_section}{module_scope_section}{locked_dep_section}{api_section}
Functional requirements:
{_functional_req_capped}
{_whole_ctx_capped}{prev_files_section}
Generate the file: `{file_path}` (this call writes ONLY that file).

{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}"""
            _per_file_attempts = 2
            for _attempt in range(_per_file_attempts):
                response = ask(file_prompt, model=_TASK2_MODEL)
                files = extract_files(response)
                if file_path in files:
                    all_files[file_path] = files[file_path]
                    break
                matched = next((v for k, v in files.items()
                                if Path(k).name == Path(file_path).name), None)
                if matched:
                    all_files[file_path] = matched
                    _log(f"  Matched '{file_path}' by filename.")
                    break
                bare = extract_code_content(response)
                if bare:
                    all_files[file_path] = bare
                    _log(f"  Extracted '{file_path}' via content strip (model ignored FILE markers).")
                    break
                preview = response.strip()[:200].replace('\n', '↵') if response else "(empty)"
                if _attempt < _per_file_attempts - 1:
                    _log(f"  Empty response for `{file_path}`, retrying ({_attempt + 2}/{_per_file_attempts})...")
                else:
                    _log(f"  WARNING: model did not return `{file_path}`. Response preview: {preview}")

        if not all_files:
            _log("  ERROR: no files were successfully generated.")
            return False

        written = write_source_files(all_files, project_dir, manifest_path, existing_test_files)
        _log(f"  {written} source file(s) written.")
        return True


def _crate_entry_point(cargo_toml_path: Path, module_dir_rel: str,
                        file_path_set: set) -> str | None:
    """Return the crate entry point path (relative to project root) from Cargo.toml."""
    candidates = []
    if cargo_toml_path.exists():
        text = cargo_toml_path.read_text(encoding="utf-8")
        for pattern in [r'\[lib\].*?path\s*=\s*"([^"]+)"',
                        r'\[\[bin\]\].*?path\s*=\s*"([^"]+)"']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                rel = m.group(1)
                candidates.append(str(Path(module_dir_rel) / rel) if module_dir_rel else rel)
    for default in ("src/lib.rs", "src/main.rs"):
        candidates.append(str(Path(module_dir_rel) / default) if module_dir_rel else default)
    return next((c for c in candidates if c in file_path_set), None)


def _topo_sort_files(file_paths: list, known_content: dict) -> list:
    """
    FR-010: topological sort using mod declarations parsed from known_content.
    A file that declares 'mod foo;' must be generated before foo.rs.
    Uses graphlib.TopologicalSorter (stdlib, Python 3.9+).
    """
    from graphlib import TopologicalSorter
    path_set = set(file_paths)
    stems = {Path(p).stem: p for p in file_paths}
    deps: dict[str, set] = {p: set() for p in file_paths}
    for path, content in known_content.items():
        for mod_name in re.findall(r'\bmod\s+(\w+)\s*;', content):
            sub = stems.get(mod_name)
            if sub and sub != path and sub in path_set:
                deps[sub].add(path)  # sub must come after path (path declares it)
    ts = TopologicalSorter(deps)
    return list(ts.static_order())


def _split_api_surface_by_file(api_surface: str) -> list[tuple[str, str]]:
    """Parse monolithic API surface doc into (path, full_section_text) pairs."""
    result = []
    for m in re.finditer(r'<FILE path="([^"]+)">(.*?)</FILE>', api_surface, re.DOTALL):
        result.append((m.group(1), m.group(0)))
    return result


class StaticFileListTask2Generator(Task2Generator):
    """
    File-by-file generator using the original source file list saved before stripping.
    Eliminates the LLM planning step — the file list is deterministic, not guessed.
    For each file, asks the LLM to generate ONLY that file with focused context.
    Falls back to LLMTask2Generator if no saved file list exists.
    """

    def generate(self, project_dir, genesis_path, functional_req_path, manifest_path,
                 existing_test_files, project_profile="", workspace_structure="", build_error="",
                 locked_dep_context="", current_module="", module_dir_rel="",
                 whole_project_context="", coverage_strategy="", api_surface="",
                 prev_source_context=""):
        _log("\nTASK 2: Implementing source code (static file list)")
        genesis = read(genesis_path)
        functional_req = read(functional_req_path)

        # Get file list from pre-strip snapshot — no LLM needed.
        file_paths = load_original_structure(project_dir, current_module) if current_module else []

        if not file_paths:
            _log("  No saved file structure — falling back to LLM file-list generator.")
            return LLMTask2Generator().generate(
                project_dir, genesis_path, functional_req_path, manifest_path,
                existing_test_files, project_profile, workspace_structure, build_error,
                locked_dep_context, current_module, module_dir_rel, whole_project_context,
                coverage_strategy=coverage_strategy,
            )

        # Filter to module scope and exclude pure test files.
        if module_dir_rel:
            file_paths = [p for p in file_paths if p.startswith(module_dir_rel)]
        file_paths = [p for p in file_paths if p not in existing_test_files]

        # FR-010: put entry point first; remaining order updated dynamically after each generation.
        cargo_toml = (project_dir / module_dir_rel / "Cargo.toml") if module_dir_rel \
                     else (project_dir / "Cargo.toml")
        entry_point = _crate_entry_point(cargo_toml, module_dir_rel, set(file_paths))
        if entry_point:
            file_paths = [entry_point] + [p for p in file_paths if p != entry_point]

        _log(f"  File list from original structure: {len(file_paths)} file(s): "
             f"{', '.join(file_paths)}")

        if not file_paths:
            _log("  No non-test files in original structure — nothing to generate.")
            return False

        profile_section, workspace_section, build_error_section, locked_dep_section = \
            self._common_sections(project_profile, workspace_structure, build_error, locked_dep_context)

        # Detect proc-macro crate once — affects lib.rs prompt.
        _is_proc_macro = False
        if module_dir_rel:
            _cargo_toml_path = project_dir / module_dir_rel / "Cargo.toml"
            if _cargo_toml_path.exists():
                _cargo_text = _cargo_toml_path.read_text(encoding="utf-8")
                _is_proc_macro = _cargo_is_proc_macro(_cargo_text)

        _genesis_section = (
            f"Process (genesis.md):\n"
            f"{trim_to_line(genesis, _GENESIS_CAP, chr(10) + '... (genesis truncated)')}\n"
        ) if genesis.strip() else ""

        # Pre-parse API surface into per-file sections once.
        _api_file_sections = _split_api_surface_by_file(api_surface) if api_surface else []

        all_files: dict[str, str] = {}
        pending = list(file_paths)

        while pending:
            file_path = pending.pop(0)
            # Multi-file roadmap so the LLM doesn't try to cram the whole crate
            # into the current file. Without this, the first file (typically
            # lib.rs) gets the full functional_requirements doc and the
            # instruction "Output ONLY this file" and concludes it must
            # implement everything in one place.
            _file_list_parts = [
                f"\nCrate `{current_module}` consists of {len(file_paths)} "
                f"source files, each generated in a separate call. "
                f"This call generates ONE file; the others have been or will be "
                f"generated by other calls. Trust they will exist — reference "
                f"items from pending files by their declared module path."
                f"\nFile list:"
            ]
            for _fp in file_paths:
                if _fp == file_path:
                    _marker = "→"
                    _status = "THIS CALL"
                elif _fp in all_files:
                    _marker = "✓"
                    _status = "already generated"
                else:
                    _marker = " "
                    _status = "pending"
                _file_list_parts.append(f"  [{_marker}] {_fp}   ({_status})")
            file_roadmap_section = "\n".join(_file_list_parts) + "\n"

            # Prev files: most recent N in full content, older files as API
            # signatures only (extracted from api_surface) so the prompt does
            # not balloon as the per-module file count grows.
            prev_files_section = ""
            if all_files:
                _FULL_PREV_COUNT = 3  # most recent N files keep full body
                _prev_items = list(all_files.items())
                _full_items = _prev_items[-_FULL_PREV_COUNT:]
                _digest_items = _prev_items[:-_FULL_PREV_COUNT]
                parts = ["\nAlready-generated files for this module (do NOT regenerate):"]
                # Digest older files via their API surface section if available.
                _api_by_path = dict(_api_file_sections)
                for prev_path, _prev_content in _digest_items:
                    _digest = _api_by_path.get(prev_path, "")
                    if _digest:
                        parts.append(
                            f'\n<FILE path="{prev_path}" digest="api-signatures-only">\n'
                            f'{_digest}\n</FILE>'
                        )
                    else:
                        # No API section available — keep just the file declaration.
                        parts.append(
                            f'\n<FILE path="{prev_path}" digest="omitted-for-budget">\n'
                            f'// (content elided — see other already-generated files for cross-references)\n'
                            f'</FILE>'
                        )
                for prev_path, prev_content in _full_items:
                    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content}\n</FILE>')
                prev_files_section = "\n".join(parts) + "\n"

            proc_macro_note = ""
            if _is_proc_macro:
                proc_macro_note = """
⚠️  PROC-MACRO CRATE (syn = "2.0", default features only):

IMPORTS for lib.rs:
  use proc_macro::TokenStream;
  use proc_macro2::{TokenStream as TokenStream2, Span};
  use syn::{parse_macro_input, DeriveInput, Data, DataStruct, DataEnum, Fields,
            FieldsNamed, FieldsUnnamed, Field, Ident, Type, Attribute, Meta,
            Expr, Lit, LitStr, LitInt, Path, Generics, Error as SynError};
  use syn::spanned::Spanned;
  use quote::{quote, quote_spanned, ToTokens};

AVAILABLE syn 2.0 types (default features, no 'full' needed):
  DeriveInput, Data, DataStruct, DataEnum, Fields, FieldsNamed, FieldsUnnamed, Field
  Attribute, Meta, MetaList, MetaNameValue, Ident, Type, Generics, Path, LitStr, LitInt

NOT AVAILABLE (require 'full' feature — DO NOT USE):
  ItemEnum, ItemFn, ItemStruct, ItemImpl, PatIdent, Stmt, AttributeArgs

RULES:
  - Do NOT declare `extern crate proc_macro;` — it is implicit.
  - ALL helpers must be private (no `pub`) except #[proc_macro_derive]/[proc_macro_attribute] fns.
  - Do NOT define types that belong to dependent crates — reference them via their crate path
    inside quote! blocks (e.g. `my_crate::SomeType`).
  - Entry points must be at the TOP LEVEL of lib.rs with the correct registration attribute.
  - References in struct fields MUST have lifetimes: `struct Foo<'a> { x: &'a Bar }`
  - Iterate FieldsNamed: `fields.named.iter()` (NOT `fields.iter()`)
  - `syn::AttributeArgs` removed in syn 2.x — parse attrs via `Attribute::parse_nested_meta`
  - Do NOT use inline `mod helpers { }` blocks — helper functions go directly in the file.
  - Each `mod foo;` in lib.rs needs a corresponding foo.rs file (already in file list).
  - Derive/attribute macro names and helper attribute paths are in the functional requirements.
"""

            # ── Per-file content: priority-ordered fill-down ──────────────────
            # Split into primary (always include) and secondary (fill remaining budget).
            # No fixed percentage caps — each primary section uses its actual size,
            # leftover budget goes to secondary content in priority order.
            _file_stem = Path(file_path).stem  # e.g. "error" for "src/error.rs"
            _is_entry = _file_stem in ("lib", "main")

            # Split API surface sections.
            _primary_api = ""
            _secondary_api_parts: list[str] = []
            for _api_path, _api_sec in _api_file_sections:
                if _api_path == file_path or Path(_api_path).stem == _file_stem:
                    _primary_api = _api_sec
                else:
                    _secondary_api_parts.append(_api_sec)

            # Split test files. Primary = tests relevant to the current file.
            # Match is loose: a test file is primary if its stem contains, is
            # contained in, or starts with `test_<file_stem>`. Exact-equality
            # is too strict — `test_chain.rs` should bind to `error.rs` when
            # error.rs defines the Chain type.
            def _is_primary_test(rel: str, *, _is_entry=_is_entry,
                                 _file_stem=_file_stem) -> bool:  # B023
                if _is_entry and rel.startswith("tests/"):
                    return True
                test_stem = Path(rel).name.split('.')[0]
                if not _file_stem or not test_stem:
                    return False
                # Drop common test prefixes for the comparison.
                norm_test = re.sub(r'^(test_|tests_|it_)', '', test_stem)
                norm_file = _file_stem
                return (
                    norm_test == norm_file
                    or norm_file in norm_test
                    or norm_test in norm_file
                )

            _primary_tests: list[tuple[str, str]] = []
            _secondary_tests: list[tuple[str, str]] = []
            for _rel in sorted(existing_test_files):
                if module_dir_rel and not _rel.startswith(module_dir_rel):
                    continue
                _tp = project_dir / _rel
                if not _tp.exists():
                    continue
                _content = _tp.read_text(encoding="utf-8")
                if _is_primary_test(_rel):
                    _primary_tests.append((_rel, _content))
                else:
                    _secondary_tests.append((_rel, _content))

            # Build primary test section (always included in full).
            # READ-ONLY: these test files are the SPEC; do not regenerate them.
            _primary_test_parts = [
                "\nTest files — primary (READ-ONLY spec your implementation must satisfy; do NOT output these files):"
            ]
            for _rel, _content in _primary_tests:
                _primary_test_parts.append(f'\n<TESTFILE path="{_rel}">\n{_content}\n</TESTFILE>')
            _primary_test_section = (
                "\n".join(_primary_test_parts) + "\n" if len(_primary_test_parts) > 1 else ""
            )

            # Strategy section (actual size, always included).
            strategy_section = (
                f"\nCoverage strategy (import patterns, type construction, test conventions):\n"
                f"{trim_to_line(coverage_strategy, _STRATEGY_CAP, "\n... (strategy truncated)")}\n"
            ) if coverage_strategy.strip() else ""

            # Functional requirements — always include in full; only hard-truncate if
            # the doc alone would exceed the entire budget (pathological case).
            _f_req = smart_truncate(functional_req, total_limit=SINGLE_CALL_CHAR_LIMIT // 2,
                                    head=SINGLE_CALL_CHAR_LIMIT // 6,
                                    tail=SINGLE_CALL_CHAR_LIMIT // 3)

            # Whole-project context — only present when module has no tests or is stuck;
            # always include in full up to a hard cap.
            # Suppress when primary/secondary tests already cover the same
            # TESTFILE blocks (the whole_project_context block lists each test
            # as <TESTFILE path="...">; if every path it mentions is already
            # in primary+secondary, it adds nothing but token cost).
            _wp_paths = set(re.findall(r'<TESTFILE path="([^"]+)"', whole_project_context or ""))
            _have_paths = {p for p, _ in _primary_tests} | {p for p, _ in _secondary_tests}
            if _wp_paths and _wp_paths.issubset(_have_paths):
                whole_project_context = ""
            _w_ctx = smart_truncate(whole_project_context, total_limit=SINGLE_CALL_CHAR_LIMIT // 4,
                                    head=SINGLE_CALL_CHAR_LIMIT // 16,
                                    tail=3 * SINGLE_CALL_CHAR_LIMIT // 16)

            # Compute scaffolding overhead by rendering the prompt with all
            # measured sections empty. Captures static text + thinking
            # directive + format instruction + verbosity guard + module-scope
            # warning + conditional blocks (proc_macro_note, locked_dep,
            # build_error) at their actual size. No hardcoded estimate.
            _scaffold = f"""You are implementing a single source file for a Rust project.
{_TASK2_THINKING_DIRECTIVE}{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}Output ONLY the file `{file_path}` using the markers above. Nothing else.

⚠️  MODULE SCOPE: This file belongs to the `{current_module}` crate.
    Path: `{file_path}`
{proc_macro_note}{locked_dep_section}{build_error_section}
Functional requirements:



Generate the implementation for `{file_path}` (this call writes ONLY that file).
"""
            _primary_size = (
                len(_scaffold)
                + len(file_roadmap_section)
                + len(_genesis_section)
                + len(profile_section)
                + len(workspace_section)
                + len(strategy_section)
                + len(_primary_api)
                + len(_primary_test_section)
                + len(_f_req)
                + len(_w_ctx)
                + len(prev_files_section)
            )
            _fill_budget = max(0, SINGLE_CALL_CHAR_LIMIT - _primary_size)

            # Fill secondary API sections then secondary test files with remaining budget.
            _secondary_api_section = ""
            _secondary_test_section = ""
            _fill_used = 0
            _sec_api_parts: list[str] = []
            for _sec in _secondary_api_parts:
                if _fill_used + len(_sec) > _fill_budget:
                    break
                _sec_api_parts.append(_sec)
                _fill_used += len(_sec)
            if _sec_api_parts:
                _secondary_api_section = "\n".join(_sec_api_parts) + "\n"

            # Sort secondary tests by relevance to current file before fill:
            # score = number of times the file's API-surface item names appear
            # in the test file (proxy for "this test exercises the current file").
            # Tie-break alphabetically for stability.
            _symbol_names = re.findall(r'\b(?:fn|struct|enum|trait|type|const)\s+([A-Za-z_]\w*)',
                                       _primary_api or "")
            _symbol_set = set(_symbol_names)
            def _relevance_score(item: tuple[str, str], *,
                                 _symbol_set=_symbol_set) -> tuple[int, str]:  # B023
                _, content = item
                if not _symbol_set:
                    return (0, item[0])
                hits = sum(content.count(s) for s in _symbol_set)
                return (-hits, item[0])  # negative for descending sort on hits
            _secondary_tests_sorted = sorted(_secondary_tests, key=_relevance_score)

            _sec_test_parts: list[str] = []
            if _secondary_tests_sorted and _fill_used < _fill_budget:
                _sec_test_parts.append(
                    "\nTest files — additional context (READ-ONLY; do NOT output):"
                )
                for _rel, _content in _secondary_tests_sorted:
                    if _fill_used + len(_content) > _fill_budget:
                        break
                    _sec_test_parts.append(
                        f'\n<TESTFILE path="{_rel}">\n{_content}\n</TESTFILE>'
                    )
                    _fill_used += len(_content)
            if len(_sec_test_parts) > 1:
                _secondary_test_section = "\n".join(_sec_test_parts) + "\n"

            # Assemble final API and test sections.
            api_section = (
                "\nAPI surface — pre-strip structural skeleton (signatures and type definitions):\n"
                + _primary_api + _secondary_api_section
            ) if (_primary_api or _secondary_api_section) else ""
            test_files_section = _primary_test_section + _secondary_test_section
            prompt = f"""You are implementing a single source file for a Rust project.
{_TASK2_THINKING_DIRECTIVE}{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}Output ONLY the file `{file_path}` using the markers above. Nothing else.
{file_roadmap_section}{_genesis_section}{profile_section}{workspace_section}{strategy_section}{api_section}
⚠️  MODULE SCOPE: This file belongs to the `{current_module}` crate.
    Path: `{file_path}`
{proc_macro_note}{locked_dep_section}{build_error_section}
Functional requirements:
{_f_req}
{test_files_section}{_w_ctx}{prev_files_section}
Generate the implementation for `{file_path}` (this call writes ONLY that file).
"""
            # FR-012: per-file retry — qwen3 thinking mode sometimes returns empty output.
            _per_file_attempts = 2
            for _attempt in range(_per_file_attempts):
                response = ask(prompt, model=_TASK2_MODEL)
                files = extract_files(response)
                if file_path in files:
                    all_files[file_path] = files[file_path]
                    break
                # Try relaxed match — model may have used slightly different path
                matched = next((v for k, v in files.items()
                                if Path(k).name == Path(file_path).name), None)
                if matched:
                    all_files[file_path] = matched
                    _log(f"  Matched '{file_path}' by filename.")
                    break
                # Last resort: strip thinking + fences and take the bare content.
                bare = extract_code_content(response)
                if bare:
                    all_files[file_path] = bare
                    _log(f"  Extracted '{file_path}' via content strip (model ignored FILE markers).")
                    break
                # Empty response — retry if attempts remain.
                preview = response.strip()[:200].replace('\n', '↵') if response else "(empty)"
                if _attempt < _per_file_attempts - 1:
                    _log(f"  Empty response for `{file_path}`, retrying ({_attempt + 2}/{_per_file_attempts})...")
                else:
                    _log(f"  WARNING: no output for `{file_path}`. Preview: {preview}")

            # FR-010: re-sort remaining files using mod declarations from just-generated file.
            if pending and file_path in all_files:
                with suppress(Exception):  # cycle or error — keep current order
                    pending = _topo_sort_files(pending, all_files)

        if not all_files:
            _log("  ERROR: no files generated.")
            return False

        written = write_source_files(all_files, project_dir, manifest_path, existing_test_files)
        _log(f"  {written} source file(s) written.")
        return True


def _cluster_cfg_predicate_for_gate(cluster) -> str:
    """Build the cfg predicate body (without `#[cfg(...)]` wrapping) that's
    true for this cluster and false for all others — used as the gate when
    multiple clusters have divergent versions of the same item.

    FR-032: each cfg may be activated EITHER as a bare `--cfg N` (RUSTFLAGS)
    OR as a cargo `feature = "N"`, and a given cluster sets only ONE of the
    two. So each active predicate becomes `any(N, feature = "N")` (true under
    whichever form this cluster uses) and each excluded predicate becomes
    `not(any(N, feature = "N"))`. The whole gate ANDs these together.

    The previous version read `cfg_true`/`cfg_false`, which list BOTH forms as
    separate AND terms — `all(N, feature = "N", …)`. That is unsatisfiable for
    a `--cfg`-activated cluster (the feature is off), so every divergent item
    gated this way vanished under that cluster → E0046 in the merged tree.
    Built from active_cfg/excluded_cfg (bare names) to avoid that double form.
    """
    if cluster is None:
        return ""

    def _either(pred: str) -> str:
        # Active if the bare cfg OR its cargo-feature form is set.
        return f'any({pred}, feature = "{pred}")'

    terms: list[str] = []
    for p in (getattr(cluster, "active_cfg", []) or []):
        terms.append(_either(p))
    for p in (getattr(cluster, "excluded_cfg", []) or []):
        terms.append(f"not({_either(p)})")
    if not terms:
        return ""
    if len(terms) == 1:
        return terms[0]
    return "all(" + ", ".join(terms) + ")"


def _ast_merge_files(out_path: Path,
                     cluster_files: list[tuple[str, Path, str]]) -> bool:
    """Invoke the syn-based merger on a single file. `cluster_files` is a
    list of (cluster_name, source_path, cfg_predicate). Writes the merged
    output to `out_path`. Returns True on success.

    Replaces the regex-based item splicer that broke down on trait/impl
    bodies (each method's textual splice produced orphan declarations
    outside its trait/impl when bodies diverged across clusters).
    """
    if not _SKELETON_EXTRACTOR_BIN.exists():
        return False
    cmd: list[str] = [
        str(_SKELETON_EXTRACTOR_BIN),
        "--merge-clusters",
        "--merge-output", str(out_path),
    ]
    for name, src_path, cfg_pred in cluster_files:
        cmd += ["--cluster", f"{name}:{src_path}:{cfg_pred}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        _log(f"  [skeleton-merge] extractor invocation failed: {e}")
        return False
    if r.returncode != 0:
        _log(f"  [skeleton-merge] extractor returned rc={r.returncode}: "
             f"{(r.stderr or '').strip()[:240]}")
        return False
    return True


def _merge_cluster_outputs(project_dir: Path,
                            cluster_specs: dict,
                            successes: list[dict]) -> dict:
    """Cross-cluster merge: compose per-cluster filled source dirs into
    project_dir/src/ with cfg-gated divergence for conflicting fills.

    AST-based via the syn extractor's --merge-clusters subcommand. For
    each file present in any cluster's `.skeleton-filled/{c}/src/`:
      - The extractor parses each cluster's version into a syn::File.
      - It walks items in parallel: identical token streams collapse to
        one entry; divergent items get emitted as gated copies (with
        their cluster cfg attribute) AT THEIR NATURAL POSITION (inside
        trait body, impl block, etc — recursively).
      - The merge respects nested items, which the previous regex-based
        textual splicer could not.

    Returns {'files_written': int, 'cfg_gated_fns': int}. The
    `cfg_gated_fns` value is approximate — derived from a post-merge
    grep — since the AST merger doesn't currently report it back.
    """
    cluster_dirs: dict[str, Path] = {}
    for s in successes:
        cname = s["cluster"]
        d = project_dir / ".skeleton-filled" / cname
        if d.exists():
            cluster_dirs[cname] = d
    if not cluster_dirs:
        return {"files_written": 0, "cfg_gated_fns": 0}

    # Collect every file path across all cluster outputs.
    all_paths: set[str] = set()
    for d in cluster_dirs.values():
        for p in (d / "src").rglob("*.rs"):
            all_paths.add(str(p.relative_to(d)))

    files_written = 0
    cfg_gated_fns = 0
    extractor_available = _SKELETON_EXTRACTOR_BIN.exists()
    if not extractor_available:
        _log("  [skeleton-merge] WARN: extractor binary missing; falling "
             "back to base-cluster passthrough (no divergence handling).")

    for rel in sorted(all_paths):
        if ".gen_tests_" in rel:
            continue
        # Gather (cluster_name, path, cfg_predicate) for every cluster
        # that has this file. Lexicographic order means a stable cluster
        # is base when present, which keeps merge output deterministic.
        cluster_files: list[tuple[str, Path, str]] = []
        for cname in sorted(cluster_dirs.keys()):
            p = cluster_dirs[cname] / rel
            if not p.exists():
                continue
            cluster_obj = cluster_specs.get(cname)
            cfg_pred = _cluster_cfg_predicate_for_gate(cluster_obj)
            cluster_files.append((cname, p, cfg_pred))
        if not cluster_files:
            continue

        out_path = project_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)

        merged_ok = False
        if extractor_available:
            merged_ok = _ast_merge_files(out_path, cluster_files)
        if not merged_ok:
            # Fallback: copy the base cluster's file verbatim. Loses
            # divergence handling but never corrupts the output. Only
            # exercised when the extractor binary is missing or its
            # invocation fails on a specific file.
            base_path = cluster_files[0][1]
            try:
                out_path.write_text(base_path.read_text(encoding="utf-8"),
                                     encoding="utf-8")
            except Exception as e:
                _log(f"  [skeleton-merge] copy fallback failed for {rel}: {e}")
                continue
        files_written += 1
        # Approximate cfg_gated_fns by counting #[cfg(... )] attributes
        # in the merged output that match our cluster gates. Used only
        # for the post-merge log line.
        try:
            text_out = out_path.read_text(encoding="utf-8")
            cfg_gated_fns += len(re.findall(
                r"#\[cfg\([^)]*(error_generic_member_access|anyhow_no_core_error)",
                text_out,
            ))
        except Exception:
            pass

    return {"files_written": files_written, "cfg_gated_fns": cfg_gated_fns}


class StaticSkeletonTask2Generator(Task2Generator):
    """4-phase reconstruction: mechanical skeleton → LLM grouping → per-group fill → cleanup.

    Phase A:  Load the skeleton (signatures + types + macros +
              `unimplemented!()` bodies) baked from live source before strip
              (see `_bake_skeleton_artifacts`). No LLM. cargo check verifies
              the cached skeleton still compiles under this cluster's env.
    Phase B:  One LLM call to group test files into cohesive units. Validated.
    Phase C:  Per-group LLM call that fills `unimplemented!()` bodies the group's
              tests transitively need. Leaf-first ordering. Merge preserves bodies
              already filled by prior groups.
    Phase D:  Single cleanup pass for test-exercised stubs left untouched.

    Phase A and B results cached on disk; Phase C and D re-run per iteration.
    """

    def generate(self, project_dir, genesis_path, functional_req_path, manifest_path,
                 existing_test_files, project_profile="", workspace_structure="", build_error="",
                 locked_dep_context="", current_module="", module_dir_rel="",
                 whole_project_context="", coverage_strategy="", api_surface="",
                 prev_source_context=""):
        _log("\nTASK 2: Implementing source code (skeleton backend, per-cluster)")
        module = current_module or project_dir.name

        # Load clusters from the bake artefact written before strip
        # (Hard Rule 11: no source access post-strip). If absent, abort —
        # there is no live source to discover from at this point.
        clusters = _load_baked_clusters(project_dir)
        if not clusters:
            _log("  [skeleton] FATAL: no baked clusters at "
                 f"{project_dir}/.skeleton/_clusters.json. The skeleton bake "
                 "step (pre-strip) must run before TASK 2.")
            return False
        clusters = sorted(clusters, key=lambda c: c.name)
        _log(f"  [skeleton] loaded {len(clusters)} baked cluster(s): "
             f"{', '.join(c.name for c in clusters)}")

        genesis = read(genesis_path) if genesis_path.exists() else ""
        functional_req = read(functional_req_path) if functional_req_path.exists() else ""
        all_test_pairs: list[tuple[str, str]] = []
        for rel in sorted(existing_test_files):
            tp = project_dir / rel
            if tp.exists():
                try:
                    all_test_pairs.append((rel, tp.read_text(encoding="utf-8")))
                except Exception:
                    continue
        if not all_test_pairs:
            _log("  [skeleton] no test files; cannot proceed.")
            return False

        # === task2a — per-cluster Phase A→D (RESUMABLE). The per-cluster
        # .skeleton-filled/ trees persist, so once this stage is marked done it
        # is skipped on resume / retry; restart_from.sh task2a re-runs it. ===
        cluster_summaries: list[dict] = []
        if _phase_valid(project_dir, f"task2a/{module}"):
            _log("  [skeleton] task2a (per-cluster reconstruction) already done — "
                 "skipping (fills persist in .skeleton-filled/).")
            cluster_summaries = [{"cluster": c.name, "ok": True} for c in clusters]
        else:
            _set_current(project_dir, f"task2a/{module} / per-cluster reconstruction (0/{len(clusters)})")
            for _ci, cluster in enumerate(clusters):
                tag = f"[{cluster.name}]"
                _set_current(project_dir,
                             f"task2a/{module} / cluster {cluster.name} ({_ci + 1}/{len(clusters)})")
                _log(f"\n  {tag} === cluster start ===")
                res = self._generate_one_cluster(
                    project_dir=project_dir,
                    cluster=cluster,
                    module=module,
                    all_test_pairs=all_test_pairs,
                    genesis=genesis,
                    functional_req=functional_req,
                )
                cluster_summaries.append({"cluster": cluster.name, **res})
                if not res.get("ok"):
                    _log(f"  {tag} cluster aborted: {res.get('reason', 'unknown')}")
            _mark_phase(project_dir, f"task2a/{module}", "done",
                        detail=f"{len(clusters)} cluster(s)")

        # === task2b — cross-cluster merge into project_dir/src/ (RESUMABLE).
        # Skipped only if marked done AND the merged tree is intact: every
        # file in the saved manifest still on disk. A bare `src/ exists`
        # check is defeated by the workspace stub (the startup wipe leaves a
        # stub lib.rs), which let phase C run against an empty tree. (task4's
        # delete-and-retry clears this marker, forcing a re-merge.) ===
        _merged_tree_intact = False
        if _phase_valid(project_dir, f"task2b/{module}"):
            _mf = load_manifest(manifest_path) if manifest_path else []
            _merged_tree_intact = bool(_mf) and all(
                (project_dir / rel).exists() for rel in _mf)
        if _merged_tree_intact:
            _log("  [skeleton-merge] task2b (merge) already done — skipping.")
        else:
            _set_current(project_dir,
                         f"task2b/{module} / merging {len(cluster_summaries)} cluster output(s)")
            _log(f"\n  [skeleton-merge] composing {len(cluster_summaries)} cluster output(s) "
                 f"into {project_dir}/src/")
            merge_stats = _merge_cluster_outputs(
                project_dir=project_dir,
                cluster_specs={c.name: c for c in clusters},
                successes=[s for s in cluster_summaries if s.get("ok")],
            )
            _log(f"  [skeleton-merge] {merge_stats['files_written']} file(s) written, "
                 f"{merge_stats['cfg_gated_fns']} cfg-gated divergence(s).")
            _mark_phase(project_dir, f"task2b/{module}", "done",
                        detail=f"{merge_stats['files_written']} file(s)")

        # Post-merge fill pass: per-cluster Phase C drove fills against each
        # cluster's filtered cfg view of the code. After the AST merge, the
        # default-cfg view of `project_dir/src/` may expose fns that none of
        # the per-cluster Phase Cs reached (typically the stable variants of
        # cfg-divergent code paths). Without this step, TASK 3 panics at
        # `unimplemented!()` on fns Phase C never queued.
        #
        # Strategy: run cargo test --no-fail-fast on the merged tree, extract
        # panic frontier with the same syn-based mapper Phase C uses, fill
        # those fns via the per-fn machinery, repeat until frontier is empty
        # or stall budget exhausted. Always uses --no-fail-fast so panics
        # from every binary surface, not just the first one to crash.
        # === task2c — merged-tree fill (RESUMABLE). This is where U2 test-
        # acceptance does most of its work. restart_from.sh task2c re-runs ONLY
        # this stage (per-cluster + merge skipped if their markers stand). ===
        if _phase_valid(project_dir, f"task2c/{module}"):
            _log("  task2c (merged-tree fill) already done — skipping.")
        else:
            _set_current(project_dir, f"task2c/{module} / merged-tree fill")
            self._post_merge_fill_pass(
                project_dir=project_dir,
                clusters=clusters,
                module=module,
                functional_req=functional_req,
            )
            _mark_phase(project_dir, f"task2c/{module}", "done")

        # Update the manifest so downstream task3/4 see the generated files.
        if manifest_path:
            generated = [str(p.relative_to(project_dir))
                         for p in (project_dir / "src").rglob("*.rs")
                         if str(p.relative_to(project_dir)) not in existing_test_files]
            if generated:
                save_manifest(manifest_path, generated)
                _log(f"  [skeleton] manifest written: {len(generated)} source file(s).")

        return True

    def _generate_one_cluster(
        self,
        project_dir: Path,
        cluster: "BuildCluster",
        module: str,
        all_test_pairs: list[tuple[str, str]],
        genesis: str,
        functional_req: str,
    ) -> dict:
        """Run Phase A + B + C + D for a single cluster, returning a stats dict.

        All artifacts land under `.skeleton/{cluster}/` (skeleton + groups +
        failure dumps) and `.skeleton-filled/{cluster}/src/` (filled output).
        Cargo invocations honour `cluster.env_extra` end-to-end.
        """
        tag = f"[{cluster.name}]"
        skeleton_dir = project_dir / ".skeleton" / cluster.name
        filled_dir = project_dir / ".skeleton-filled" / cluster.name

        # Phase A — load the pre-baked skeleton from disk. Hard Rule 11:
        # the original source is gone after strip, so the skeleton MUST
        # have been produced earlier by `_bake_skeleton_artifacts`. If
        # absent here, that step did not run — fail loud.
        if not (skeleton_dir / "src").exists() or not any(skeleton_dir.rglob("*.rs")):
            _log(f"  {tag} [skeleton-A] FATAL: no baked skeleton at "
                 f"{skeleton_dir}. The pre-strip bake step did not run "
                 "for this cluster.")
            return {"ok": False, "reason": "no_baked_skeleton"}
        _log(f"  {tag} [skeleton-A] using baked skeleton at {skeleton_dir}.")

        # cargo check on the skeleton with cluster env. Fix-loop on failure.
        _cargo_toml = project_dir / "Cargo.toml"
        if _cargo_toml.exists() and not (skeleton_dir / "Cargo.toml").exists():
            shutil.copy(_cargo_toml, skeleton_dir / "Cargo.toml")
        check_cmd = self._cluster_check_cmd(cluster)
        # P3.8 diagnostic: log toolchain + final check_cmd so we can verify
        # the nightly cluster is actually using +nightly.
        _log(f"  {tag} [skeleton-A] cluster.toolchain={cluster.toolchain!r} "
             f"requires_nightly={getattr(cluster, 'requires_nightly', False)}")
        _log(f"  {tag} [skeleton-A] cargo check ({check_cmd})...")
        try:
            _r = subprocess.run(
                shlex.split(check_cmd),
                cwd=skeleton_dir, capture_output=True, text=True, timeout=180,
                env=self._cluster_env(cluster),
            )
        except subprocess.TimeoutExpired:
            _log(f"  {tag} [skeleton-A] cargo check timed out; proceeding anyway.")
            _r = None
        # P3.8: detect E0554 (nightly feature on stable channel) and retry
        # with nightly toolchain. The cluster's requires_nightly flag is set
        # by an earlier TASK 0 phase only when its cargo build hits E0554;
        # the skeleton backend's Phase A may run BEFORE that phase, so we
        # need a local catch.
        if (_r is not None and _r.returncode != 0
                and "E0554" in (_r.stderr or "") + (_r.stdout or "")
                and not cluster.requires_nightly):
            _log(f"  {tag} [skeleton-A] detected E0554 — promoting cluster to nightly.")
            cluster.requires_nightly = True
            check_cmd = self._cluster_check_cmd(cluster)
            try:
                _r = subprocess.run(
                    shlex.split(check_cmd),
                    cwd=skeleton_dir, capture_output=True, text=True, timeout=180,
                    env=self._cluster_env(cluster),
                )
                _log(f"  {tag} [skeleton-A] retry with +nightly: rc={_r.returncode}")
            except subprocess.TimeoutExpired:
                _log(f"  {tag} [skeleton-A] cargo +nightly check timed out.")
                _r = None
        if _r is not None and _r.returncode != 0:
            _log(f"  {tag} [skeleton-A] cargo check FAILED; invoking fix-loop.")
            _skel_sources = list((skeleton_dir / "src").rglob("*.rs"))
            ok, _ = generate_fix_loop(
                project_dir=skeleton_dir,
                check_cmd=check_cmd,
                source_paths=_skel_sources,
                existing_test_files=set(),
                model=_TASK2_MODEL,
                max_fix_attempts=3,
                timeout=180,
            )
            if not ok:
                return {"ok": False, "reason": "skeleton_fix_loop_exhausted"}

        # Per-cluster test scoping.
        cluster_tests = _filter_tests_for_cluster(all_test_pairs, cluster)
        _log(f"  {tag} [skeleton-B] tests for this cluster: "
             f"{len(cluster_tests)}/{len(all_test_pairs)}")
        if not cluster_tests:
            return {"ok": True, "phase_c": None, "phase_d": None,
                    "note": "no tests apply to this cluster"}

        # Mode selection for Phase B+C: per-group (default, current behaviour)
        # or per-fn TDD (Flavour B, smaller prompts, one call per fn, no
        # LLM grouping). Switched via env var PHASE_C_MODE=per_fn.
        per_fn_mode = SETTINGS.phase_c_mode is PhaseCMode.PER_FN

        # Seed filled_dir from skeleton state (idempotent) regardless of mode.
        filled_dir.mkdir(parents=True, exist_ok=True)
        for src in skeleton_dir.rglob("*.rs"):
            rel = src.relative_to(skeleton_dir)
            dst = filled_dir / rel
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

        # Propagate gen_tests siblings from project_dir/src/ into filled_dir/src/.
        # task0c commits these as cluster-specific unit-test specs; the bake step
        # emits only the structural skeleton. Without this propagation Phase C
        # runs cargo test against integration tests only and gen_tests are
        # invisible. `.broken` suffixes (left over from the legacy reattach
        # quarantine path) are treated as live — they were renamed because
        # earlier LLM-regenerated source didn't satisfy them, not because they
        # are broken in themselves.
        _proj_src = project_dir / "src"
        _filled_src = filled_dir / "src"
        if _proj_src.exists() and _filled_src.exists():
            _gt_re = re.compile(r"^(.+?)\.gen_tests_(\d+)\.rs(?:\.broken)?$")
            _gt_allow_line, _ = _derive_gentest_prelude(project_dir)
            # Gate each sibling by the cfg of the cluster that GENERATED it (origin),
            # not the current cluster — `_cluster_cfg_attr(cluster)` is always-active
            # in its own build and gives no protection, so a nightly-only test would
            # compile (and E0658) in stable/no_core. Origin cfg excludes it there and
            # keeps it only where it belongs. Fallback to current cluster if the
            # manifest is missing (older runs).
            _gt_origin = _load_gen_tests_origin(project_dir)
            _gt_cur_attr = _cluster_cfg_attr(cluster)
            _gt_propagated = 0
            for gt in sorted(_proj_src.iterdir()):
                if not gt.is_file():
                    continue
                _m = _gt_re.match(gt.name)
                if not _m:
                    continue
                _parent_stem = _m.group(1)
                _pass_n = int(_m.group(2))
                # R9.6: skip + quarantine undeclared orphans (on disk but absent from
                # a non-empty origin manifest) — they were never coverage-validated and
                # must not be resurrected under a guessed gate.
                if not _propagate_or_quarantine_sibling(
                        gt, _gt_origin, _parent_stem, _pass_n, _log, tag):
                    continue
                _dst_name = f"{_parent_stem}.gen_tests_{_pass_n}.rs"
                _dst = _filled_src / _dst_name
                _parent_path = _filled_src / f"{_parent_stem}.rs"
                if not _parent_path.exists():
                    continue
                try:
                    _body = gt.read_text(encoding="utf-8")
                except Exception as _e:
                    _log(f"  {tag} [skeleton-C] gen_tests read failed {gt.name}: {_e}")
                    continue
                if not _rust_delimiters_balanced(_body):
                    continue
                if not _dst.exists() or _dst.read_text(encoding="utf-8") != _body:
                    _dst.write_text(_body, encoding="utf-8")
                _ptext = _parent_path.read_text(encoding="utf-8")
                if not _has_gen_tests_decl(_ptext, _pass_n):
                    _gt_attr = _gt_origin.get(
                        f"{_parent_stem}.gen_tests_{_pass_n}", _gt_cur_attr)
                    _decl = _build_gen_tests_decl(
                        _dst_name, _pass_n, _gt_allow_line,
                        cluster_cfg_attr=_gt_attr,
                    )
                    _parent_path.write_text(_ptext.rstrip() + _decl, encoding="utf-8")
                    _gt_propagated += 1
            if _gt_propagated:
                _log(f"  {tag} [skeleton-C] propagated {_gt_propagated} "
                     f"gen_tests decl(s) into filled tree.")

        if per_fn_mode:
            _log(f"  {tag} [skeleton-C] per-fn mode (Flavour B): scanning stubs...")
            # Make filled_dir a runnable crate root so cargo check/test work:
            # copy Cargo.toml + symlink tests/. Idempotent.
            if _cargo_toml.exists() and not (filled_dir / "Cargo.toml").exists():
                shutil.copy(_cargo_toml, filled_dir / "Cargo.toml")
            _proj_tests = project_dir / "tests"
            _filled_tests = filled_dir / "tests"
            if _proj_tests.exists() and not _filled_tests.exists():
                try:
                    os.symlink(_proj_tests, _filled_tests)
                except OSError as e:
                    _log(f"  {tag} [skeleton-C] tests symlink failed: {e}")

            # Deterministic cluster-leak guard. A gen_test that belongs to another
            # cluster (e.g. nightly's `request_ref`) must not be ACTIVE in this
            # cluster. With correct origin gating it is cfg-excluded and invisible
            # here; if any gen_tests file still produces an unstable-feature error
            # (E0658/E0554) under this cluster's stable toolchain, its origin gate
            # is missing/wrong — surface it loudly and quarantine the sibling so it
            # can't poison phase-c as undifferentiated reconstruction noise. The
            # skeleton's unimplemented!() bodies compile, so unrelated fill gaps
            # don't false-trigger (we act only on gen_tests-attributed diags).
            _has_gt = _filled_src.exists() and any(
                _is_gen_tests_file(p.name) for p in _filled_src.iterdir() if p.is_file())
            if _has_gt:
                _norun = (f"cargo +{cluster.toolchain} test --no-run"
                          if cluster.toolchain else "cargo test --no-run")
                _lrc, _ltxt, _ldiags = _run_cargo_check_diag(
                    _norun, filled_dir, self._cluster_env(cluster), timeout=300)
                _leaks = sorted({
                    d.file for d in _ldiags
                    if ".gen_tests_" in d.file
                    and (d.code in ("E0658", "E0554") or "unstable" in d.message.lower())
                })
                if _leaks:
                    _log(f"  {tag} [skeleton-C] ⛔ CLUSTER LEAK in '{cluster.name}': "
                         f"{len(_leaks)} gen_tests file(s) use nightly-only API but are "
                         f"active under this cluster's stable toolchain "
                         f"(origin-cfg gate missing/wrong): {', '.join(_leaks)}. "
                         f"Quarantining so phase-c is not poisoned.")
                    for _bf in _leaks:
                        _name = Path(_bf).name
                        if _name.endswith(".rs") and ".gen_tests_" in _name:
                            _stem, _rest = _name.rsplit(".gen_tests_", 1)
                            _ns = _rest[:-3]  # strip ".rs"
                            if _ns.isdigit():
                                _pp = _filled_src / f"{_stem}.rs"
                                if _pp.exists():
                                    _pp.write_text(
                                        _strip_gen_tests_decl_n(
                                            _pp.read_text(encoding="utf-8"), int(_ns)),
                                        encoding="utf-8")
                        _sp = filled_dir / _bf
                        if _sp.exists():
                            _sp.rename(_sp.with_name(_sp.name + ".broken"))
            tests_dir_files: list[Path] = []
            for trel, _ in cluster_tests:
                tp = project_dir / trel
                if tp.exists():
                    tests_dir_files.append(tp)
            # Leaf-first ordering counts come from the bake artefact
            # (computed against live source pre-strip). Falls back to
            # alphabetical when absent.
            _leaf_counts = _load_baked_leaf_order(project_dir, cluster.name)
            if not _leaf_counts:
                _log(f"  {tag} [skeleton-C] no leaf-order artefact "
                     f"({skeleton_dir}/_leaf_order.json); falling back to "
                     "alphabetical fn ordering.")
            # Per-crate FR is the home for model self-correction notes.
            _fr_for_notes = project_dir / f"functional_requirements_{module}.md"
            # Plan 4 — PHASE_C_DRIVER=tdd switches to the TDD outer loop
            # (cargo test → frontier → fill). Default `fn_walk` keeps the
            # legacy per-fn-walk driver.
            _driver = SETTINGS.phase_c_driver
            if _driver is PhaseCDriver.TDD:
                _log(f"  {tag} [skeleton-C] PHASE_C_DRIVER=tdd — using TDD outer loop")
                c_result = _run_phase_c_tdd(
                    project_dir=project_dir,
                    target_src_dir=filled_dir,
                    tests_dir_files=tests_dir_files,
                    cluster_tests=cluster_tests,
                    module=module,
                    cluster=cluster,
                    check_cmd=self._cluster_check_cmd(cluster),
                    cluster_env=self._cluster_env(cluster),
                    leaf_counts=_leaf_counts,
                    fr_path=_fr_for_notes,
                    test_cmd_builder=lambda stems: _build_test_cmd(stems, cluster),
                    tag=tag,
                )
            else:
                c_result = _task2_fill_fns_one_by_one(
                    project_dir=project_dir,
                    target_src_dir=filled_dir,
                    tests_dir_files=tests_dir_files,
                    module_name=module,
                    cluster_name=cluster.name,
                    cluster=cluster,
                    check_cmd=self._cluster_check_cmd(cluster),
                    cluster_env=self._cluster_env(cluster),
                    max_passes=int(os.getenv("PHASE_C_MAX_PASSES", "3")),
                    leaf_name_counts=_leaf_counts,
                    fr_path=_fr_for_notes,
                )
            _log(f"  {tag} [skeleton-C] {c_result['fns_filled']} fn(s) filled, "
                 f"{c_result['bytes_filled']:,} bytes net, "
                 f"{c_result['attempts']} LLM call(s), "
                 f"{c_result['remaining']} stub(s) remaining, "
                 f"{len(c_result['errors'])} error(s).")
        else:
            # Phase B — grouping (cached per cluster).
            groups_path = skeleton_dir / "groups.json"
            groups: list[dict] | None = None
            if groups_path.exists():
                try:
                    groups = json.loads(groups_path.read_text(encoding="utf-8"))
                    _log(f"  {tag} [skeleton-B] cached manifest: {len(groups)} group(s).")
                except Exception:
                    groups = None
            if groups is None:
                _log(f"  {tag} [skeleton-B] grouping...")
                groups = _task2_group_tests(skeleton_dir, cluster_tests,
                                             cluster_name=cluster.name, module_name=module)

            # Phase C — per-group fill into cluster-specific scratch dir.
            _log(f"  {tag} [skeleton-C] filling {len(groups)} group(s) into {filled_dir}/")
            c_result = _task2_fill_groups(skeleton_dir, filled_dir, groups, project_dir,
                                           cluster_name=cluster.name, module_name=module,
                                           functional_req=functional_req, genesis=genesis,
                                           cluster=cluster)
            _log(f"  {tag} [skeleton-C] {c_result['groups_processed']} group(s), "
                 f"{c_result['bytes_filled']:,} bytes filled, "
                 f"{len(c_result['errors'])} error(s).")

        # Phase D — residual cleanup (per cluster).
        # Plan 4 — under driver=tdd, the outer loop fills exactly the fns
        # reachable from failing tests. Residuals are by-design unreachable
        # from any test → leaving them `unimplemented!()` is correct under
        # tests-only reconstruction. Skip Phase D.
        if _driver == "tdd":
            _log(f"  {tag} [skeleton-D] skipped (driver=tdd; residual stubs "
                 "are correctly left unimplemented).")
            d_result = {"stubs_total": 0, "stubs_exercised": 0,
                         "files_updated": 0, "stubs_remaining": 0}
        else:
            _log(f"  {tag} [skeleton-D] scanning for residual stubs...")
            d_result = _task2_cleanup_residuals(filled_dir, project_dir, functional_req,
                                                 cluster_tests, module_name=module)
            _log(f"  {tag} [skeleton-D] {d_result['stubs_total']} stubs, "
                 f"{d_result['stubs_exercised']} test-exercised, "
                 f"{d_result['files_updated']} updated, "
                 f"{d_result['stubs_remaining']} remaining.")

        # Per-cluster intermediate validation: cargo check the filled output.
        if _cargo_toml.exists() and not (filled_dir / "Cargo.toml").exists():
            shutil.copy(_cargo_toml, filled_dir / "Cargo.toml")
        # P3.12: end-of-cluster validation with a fix-loop. The per-fn
        # check catches errors as they happen, but bodies that pass at
        # splice-time can still break integration when all fills are in
        # place (cross-fn semantic issues, stale fills from prior runs).
        # When this cargo check fails, identify the implicated fns from
        # the error output, revert their bodies to unimplemented!() so
        # they re-appear as stubs, then re-enter the per-fn fill loop
        # targeting only those fns. Loop up to PHASE_C_VALIDATE_ITERS
        # times.
        max_validate_iters = int(
            os.getenv("PHASE_C_VALIDATE_ITERS", "3"))
        validate_ok = False
        _vr_out = ""
        for v_iter in range(1, max_validate_iters + 1):
            _check_graceful_stop(f"skeleton-validate iter {v_iter}")
            _log(f"  {tag} [skeleton-validate] cargo check on filled output "
                 f"(iter {v_iter}/{max_validate_iters})...")
            try:
                _vr = subprocess.run(
                    shlex.split(check_cmd),
                    cwd=filled_dir, capture_output=True, text=True,
                    timeout=180, env=self._cluster_env(cluster),
                )
                _vr_out = (_vr.stdout or "") + "\n" + (_vr.stderr or "")
                validate_ok = (_vr.returncode == 0)
            except subprocess.TimeoutExpired:
                _log(f"  {tag} [skeleton-validate] cargo check timed out.")
                validate_ok = False
                break

            if validate_ok:
                if v_iter > 1:
                    _log(f"  {tag} [skeleton-validate] PASSED after "
                         f"{v_iter} iteration(s).")
                break

            # Failure path. Identify implicated fns.
            _log(f"  {tag} [skeleton-validate] cargo check FAILED "
                 f"(rc={_vr.returncode}). First errors:")
            for line in _vr_out.splitlines()[:10]:
                _log(f"    {line}")
            implicated = _implicated_fns_from_cargo_errors(
                _vr_out, filled_dir)
            if not implicated:
                _log(f"  {tag} [skeleton-validate] no implicated fns parsed "
                     f"— cannot retry, giving up.")
                break
            _log(f"  {tag} [skeleton-validate] implicated fns: "
                 f"{', '.join(f'{f}::{fn}' for f, fn in implicated[:6])}"
                 + ("..." if len(implicated) > 6 else ""))
            # Revert each implicated fn to unimplemented!() so the
            # per-fn loop picks them up again as stubs.
            reverted = 0
            for rel_f, fn in implicated:
                fp = filled_dir / rel_f
                try:
                    text = fp.read_text(encoding="utf-8")
                except Exception:
                    continue
                loc = _locate_fn(text, fn)
                if loc is None:
                    continue
                _sig, _ss, body_start, body_end = loc
                cur_body = text[body_start:body_end]
                if _is_unimplemented_body(cur_body):
                    continue  # already a stub — nothing to revert
                # Re-indent unimplemented!() like a normal body.
                line_start = text.rfind("\n", 0, _ss) + 1
                indent = ""
                for ch in text[line_start:_ss]:
                    if ch in (" ", "\t"):
                        indent += ch
                    else:
                        break
                inner = indent + "    "
                new_body = "\n" + inner + "unimplemented!()\n" + indent
                fp.write_text(
                    text[:body_start] + new_body + text[body_end:],
                    encoding="utf-8",
                )
                reverted += 1
            _log(f"  {tag} [skeleton-validate] reverted {reverted} fn(s) "
                 f"to stubs; re-entering per-fn fill loop.")
            _emit("phase_c.cluster.validate_retry", data={
                "cluster": cluster.name, "iter": v_iter,
                "implicated": [{"file": f, "fn": fn}
                               for f, fn in implicated],
                "reverted": reverted,
            })
            # Re-enter the per-fn fill loop for ONE pass over just the
            # reverted stubs. Use the cargo error excerpt as initial
            # prompting context (the fix loop will refresh per attempt).
            _ = _task2_fill_fns_one_by_one(
                project_dir=project_dir,
                target_src_dir=filled_dir,
                tests_dir_files=[project_dir / trel
                                  for trel, _ in cluster_tests
                                  if (project_dir / trel).exists()],
                module_name=module,
                cluster_name=cluster.name,
                cluster=cluster,
                check_cmd=check_cmd,
                cluster_env=self._cluster_env(cluster),
                max_passes=1,  # one pass through the reverted set
                leaf_name_counts=_load_baked_leaf_order(project_dir, cluster.name),
                fr_path=_fr_for_notes,
            )

        return {"ok": True, "phase_c": c_result, "phase_d": d_result,
                "validates": validate_ok}

    def _post_merge_fill_pass(
        self,
        project_dir: Path,
        clusters: list["BuildCluster"],
        module: str,
        functional_req: str,
    ) -> None:
        """FR-024 improvement-driven loop on the merged tree.

        Runs cargo test on `project_dir`, attributes failures via the full
        chain (runtime/compile/classifier/recently_filled/reverse_map),
        fills, repeats. Exits when pass count stalls for `max_stalls`
        consecutive iters AND there's no actionable frontier — never on
        a fixed iter count alone. Closes the per-cluster / merged-view
        mismatch where Phase C declares done but the merged source still
        has unfilled stubs under the default cfg TASK 3 runs under.
        """
        max_stalls = int(os.getenv("POST_MERGE_MAX_STALLS", "3"))
        # Hard cap is now a runaway-safety backstop only — floor-stall
        # (max_stalls) is the real terminator. Set to 0 to skip the pass
        # entirely (legacy behaviour preserved).
        hard_cap = int(os.getenv("POST_MERGE_HARD_CAP", "10000"))
        if hard_cap <= 0:
            return
        tag = "[skeleton-merge]"
        tests_dir_files: list[Path] = list(
            (project_dir / "tests").rglob("*.rs"))
        fr_path = project_dir / f"functional_requirements_{module}.md"
        leaf_counts: dict[str, int] = {}
        for c in clusters:
            for fn_name_k, v in (_load_baked_leaf_order(project_dir, c.name) or {}).items():
                leaf_counts[fn_name_k] = max(leaf_counts.get(fn_name_k, 0), int(v))
        test_cmd = "cargo test --no-fail-fast"
        test_timeout = int(os.getenv("POST_MERGE_TEST_TIMEOUT", "180"))
        try:
            test_reverse_map = _build_test_reverse_map(
                tests_dir_files, project_dir)
        except Exception as _e:
            _log(f"  {tag} post-merge reverse-map build failed: {_e}")
            test_reverse_map = {}

        permanently_exhausted: set[tuple[str, str]] = set()
        # The merge landed every cluster's fills — D3's "other fills landed"
        # criterion holds globally, so cross-iter exhaustion gets one reset.
        _reset_exhaustion_flags(project_dir, "post-merge: merged tree is new context")
        recently_filled: list[tuple[str, str]] = []
        recently_filled_max = int(os.getenv("TDD_RECENTLY_FILLED_MAX", "50"))
        stall = 0
        best_pass = -1
        best_fail = None
        iter_n = 0
        while iter_n < hard_cap:
            iter_n += 1
            _check_graceful_stop(f"post-merge fill iter {iter_n}")
            _log(f"\n  {tag} post-merge fill iter {iter_n}: "
                 f"running cargo test on merged tree...")
            t0 = time.monotonic()
            rc, output = _run_cargo(test_cmd, project_dir, _cargo_env(),
                                     timeout=test_timeout)
            dt = time.monotonic() - t0
            pass_count, fail_count, _ignored = _parse_test_counts(output)
            if rc == 0:
                _log(f"  {tag} post-merge cargo test PASSED "
                     f"({pass_count} passed, {dt:.1f}s).")
                _emit("post_merge.fill.iter", data={
                    "iter": iter_n, "rc": 0, "frontier_size": 0,
                    "kind": "pass",
                    "pass_count": pass_count, "fail_count": fail_count,
                })
                return
            if rc == -1:
                _log(f"  {tag} post-merge cargo test TIMED OUT. Stopping.")
                _emit("post_merge.fill.iter", data={
                    "iter": iter_n, "rc": -1, "frontier_size": 0,
                    "kind": "timeout",
                    "pass_count": pass_count, "fail_count": fail_count,
                })
                return

            improved = pass_count > best_pass
            if improved:
                best_pass = pass_count
                best_fail = fail_count
                stall = 0
            else:
                stall += 1

            frontier, kind = _attribute_failures(
                output, project_dir, recently_filled,
                test_reverse_map, "merged",
            )
            eligible = {k: v for k, v in frontier.items()
                         if k not in permanently_exhausted}
            # Stall on pass_count is the sole real terminator. A non-empty
            # frontier with a stuck fn must not override it — same fix as
            # _run_phase_c_tdd: pass_count plateau is the only "no real
            # improvement" signal that captures cluster-level progress.
            if stall >= max_stalls:
                _log(f"  {tag} post-merge iter {iter_n}: pass_count plateau "
                     f"{stall} iter(s) at best={best_pass}; stopping "
                     f"({len(eligible)} fn(s) still on frontier — marking "
                     "exhausted). best pass="
                     f"{best_pass} fail={best_fail}.")
                for _stuck_key in eligible:
                    permanently_exhausted.add(_stuck_key)
                _emit("post_merge.fill.iter", data={
                    "iter": iter_n, "rc": rc,
                    "frontier_size": len(eligible),
                    "kind": "stall_exit",
                    "pass_count": pass_count, "fail_count": fail_count,
                    "improved": improved, "stall": stall,
                })
                return
            if not eligible:
                _log(f"  {tag} post-merge iter {iter_n}: no actionable "
                     f"frontier (stall={stall}/{max_stalls}); continuing.")
                _emit("post_merge.fill.iter", data={
                    "iter": iter_n, "rc": rc, "frontier_size": 0,
                    "kind": "no_frontier_continue",
                    "pass_count": pass_count, "fail_count": fail_count,
                    "improved": improved, "stall": stall,
                })
                continue

            _log(f"  {tag} post-merge iter {iter_n}: cargo test rc={rc} "
                 f"({dt:.1f}s); pass={pass_count} fail={fail_count} "
                 f"(improved={improved}, stall={stall}/{max_stalls}); "
                 f"frontier={len(eligible)} fn(s) ({kind}).")
            _emit("post_merge.fill.iter", data={
                "iter": iter_n, "rc": rc,
                "frontier_size": len(eligible), "kind": kind,
                "pass_count": pass_count, "fail_count": fail_count,
                "improved": improved, "stall": stall,
            })
            sub = _task2_fill_fns_one_by_one(
                project_dir=project_dir,
                target_src_dir=project_dir,
                tests_dir_files=tests_dir_files,
                module_name=module,
                cluster_name="merged",
                cluster=None,
                check_cmd="cargo check",
                cluster_env=_cargo_env(),
                max_passes=1,
                leaf_name_counts=leaf_counts,
                fr_path=fr_path,
                tdd_frontier_keys=set(eligible.keys()),
                tdd_frontier_tests=eligible,   # Plan 10 U2
                tdd_test_cmd=test_cmd,         # Plan 10 U2
                skip_end_pass_test=True,
            )
            cur_stubs = set(_scan_unimplemented_functions(project_dir / "src"))
            newly_filled: list[tuple[str, str]] = [
                k for k in eligible
                if k not in cur_stubs
                and (k[0].split("/")[-1], k[1]) not in cur_stubs
            ]
            for k in newly_filled:
                recently_filled.append(k)
            if len(recently_filled) > recently_filled_max:
                recently_filled = recently_filled[-recently_filled_max:]
            for key in eligible:
                if key in cur_stubs or (key[0].split("/")[-1], key[1]) in cur_stubs:
                    permanently_exhausted.add(key)
            _log(f"  {tag} post-merge iter {iter_n}: "
                 f"{sub.get('fns_filled', 0)} fn(s) filled, "
                 f"{sub.get('attempts', 0)} attempt(s).")
        _log(f"  {tag} post-merge hard cap {hard_cap} reached (safety net).")

    @staticmethod
    def _cluster_check_cmd(cluster: "BuildCluster") -> str:
        """Build the cargo check command. Cluster-specific flags are passed
        via env_extra (RUSTFLAGS etc.), not via --features — that distinction
        depends on which cfg names are real Cargo features vs --cfg flags,
        and task0_cfg_scan has already encoded the right form in env_extra.
        Toolchain is applied via `cargo +nightly` when required."""
        # Default message format (not --message-format=short) so the model
        # sees rustc's `note:` and `help:` hints alongside the bare error,
        # which often points directly at the fix (e.g. "consider importing
        # one of these traits: ...").
        parts = ["cargo", "check"]
        if cluster.toolchain:
            parts = ["cargo", f"+{cluster.toolchain}"] + parts[1:]
        return shlex.join(parts)

    @staticmethod
    def _cluster_env(cluster: "BuildCluster") -> dict:
        """Cargo env with cluster's env_extra merged in."""
        env = _cargo_env()
        env.update(cluster.env_extra or {})
        return env


def _is_proc_macro_crate(cargo_toml_text: str) -> bool:
    """True if Cargo.toml declares `proc-macro = true`. (Regex kept in pipeline — Option 1.)"""
    return _cargo_is_proc_macro(cargo_toml_text)


def _count_rustc_error_codes(output: str) -> int:
    """Count distinct rustc error codes (E0001 etc.) in build output.
    (Tool-output regex kept in pipeline — Option 1.)"""
    return len(set(re.findall(r"\bE\d{3,4}\b", output or "")))


def _task2_generator_factory() -> Task2Generator:
    backend = os.getenv("TASK2_GENERATOR", "filelist").lower()
    if backend == "llm":
        return LLMTask2Generator()
    if backend == "static":
        return StaticTask2Generator()
    if backend == "skeleton":
        return StaticSkeletonTask2Generator()
    return StaticFileListTask2Generator()  # default: filelist


def _format_whole_project_context(test_files: list, reason: str = "") -> str:
    """
    Format all test files as a context block for TASK 2.
    Used when isolated module context is insufficient:
      - module has 0 tests (whole-project is the only available spec)
      - no improvement after NO_IMPROVEMENT_THRESHOLD iterations (isolated signal exhausted)
    """
    if not test_files:
        return ""
    header = reason or "Full project test suite provided for additional context."
    parts = [f"\n--- WHOLE-PROJECT CONTEXT: {header} ---"]
    for rel, content in test_files:
        parts.append(f'\n<TESTFILE path="{rel}">\n{content}\n</TESTFILE>')
    parts.append("\n--- END WHOLE-PROJECT CONTEXT ---\n")
    return "\n".join(parts)


def task2(project_dir, genesis_path, functional_req_path, manifest_path, existing_test_files,
          project_profile: str = "", workspace_structure: str = "", build_error: str = "",
          locked_dep_context: str = "", current_module: str = "", module_dir_rel: str = "",
          whole_project_context: str = "", coverage_strategy: str = "", api_surface: str = "",
          prev_source_context: str = ""):
    # slice 5b: extracted to application.tasks.task2; thin shim binding deps.
    return _task2_impl(
        project_dir, genesis_path, functional_req_path, manifest_path, existing_test_files,
        _TASK2_DEPS, project_profile=project_profile, workspace_structure=workspace_structure,
        build_error=build_error, locked_dep_context=locked_dep_context,
        current_module=current_module, module_dir_rel=module_dir_rel,
        whole_project_context=whole_project_context, coverage_strategy=coverage_strategy,
        api_surface=api_surface, prev_source_context=prev_source_context,
    )


def task2_inner_loop(project_dir, genesis_path, functional_req_path, manifest_path,
                     existing_test_files, project_profile, workspace_structure,
                     build_check_cmd: str | None, locked_dep_context: str = "",
                     current_module: str = "", module_dir_rel: str = "",
                     whole_project_context: str = "", coverage_strategy: str = "",
                     api_surface: str = "", prev_source_context: str = ""):
    # slice 5b: extracted to application.tasks.task2; thin shim binding deps.
    return _task2_inner_impl(
        project_dir, genesis_path, functional_req_path, manifest_path, existing_test_files,
        project_profile, workspace_structure, build_check_cmd, _TASK2_DEPS,
        locked_dep_context=locked_dep_context, current_module=current_module,
        module_dir_rel=module_dir_rel, whole_project_context=whole_project_context,
        coverage_strategy=coverage_strategy, api_surface=api_surface,
        prev_source_context=prev_source_context,
    )


def _render_impl_block(file_text: str, members: list[dict]) -> str:
    """Verbatim source of the impl block containing `members` (header +
    where-clause + all bodies), via byte-range parse. Used by the per_impl fix
    unit to give the model the whole impl block as context. Falls back to
    listing member signatures if the byte range can't be resolved."""
    if not members:
        return ""
    anchor = members[0].get("sig_byte_start", 0)
    for r in _parse_impl_block_ranges(file_text):
        if r["body_open"] <= anchor < r["body_close"]:
            return file_text[r["header_start"]:r["body_close"] + 1]
    return "\n".join(m.get("sig_text", "") for m in members)


def generate_fix_loop(
    project_dir: Path,
    check_cmd: str,
    source_paths: list[Path],
    existing_test_files: set,
    extra_context: str = "",
    model: str = MODEL,
    max_fix_attempts: int = 2,
    timeout: int = BUILD_TIMEOUT,
    allowed_dir_prefix: str = "",
    functional_req: str = "",
    fr_path: "Path | None" = None,
) -> tuple[bool, str]:
    """
    Shared iterative fix loop for any code generation step.

    fr_path (functional_requirements_{module}.md) keys the Repository
    Specifications section (FR + genesis + API) injected into every fix
    prompt this loop builds — the per-fn/per-impl inner loops and the
    whole-file rewrite path.

    Pattern:
      1. Run check_cmd (compile check or test run).
      2. If it passes → return (True, output).
      3. If it fails → show LLM the error + current source files, ask it to fix
         ONLY the broken files (preserve correct ones).
      4. Write fixed files, re-run check.
      5. Repeat up to max_fix_attempts times.

    Used by:
    - TASK 0c: fix unit tests that don't compile (compile check).
    - TASK 2 inner loop: fix generated code that doesn't compile (build check).
    - Post-TASK 3: fix generated code whose tests fail at runtime (test run).

    Returns (passed, final_output).
    """
    def _error_sigs(output: str) -> frozenset:
        return frozenset(re.findall(r'error\[E\d+\]: .+', output))

    def _errors_by_file(output: str) -> dict[str, int]:
        """Count primary error locations per file from rustc output."""
        counts: dict[str, int] = {}
        # Each error block: error[Exxxx] line followed by --> src/file.rs:line:col
        in_error = False
        for line in output.splitlines():
            if re.match(r'^error\[E\d+\]', line):
                in_error = True
            elif in_error:
                m = re.match(r'\s+-->\s+(\S+\.rs):\d+', line)
                if m:
                    f = m.group(1).lstrip("./")
                    counts[f] = counts.get(f, 0) + 1
                    in_error = False
        return counts

    def _file_stats(paths: list[Path]) -> dict[str, dict]:
        """Return lines and fn count per file (relative path → {lines, fns})."""
        stats: dict[str, dict] = {}
        fn_re = re.compile(r'^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+\w')
        for p in paths:
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(p.relative_to(project_dir))
            lines = text.count("\n") + 1
            fns = sum(1 for ln in text.splitlines() if fn_re.match(ln))
            stats[rel] = {"lines": lines, "fns": fns}
        return stats

    def _log_compile_overview(output: str, attempt_n: int) -> None:
        """Log per-file error table and emit compile.file_stats event."""
        err_by_file = _errors_by_file(output)
        fstats = _file_stats(source_paths)
        all_files = sorted({*fstats.keys(), *err_by_file.keys()})
        clean = sum(1 for f in all_files if err_by_file.get(f, 0) == 0 and f in fstats)
        total = len(all_files)
        _log(f"  [fix loop] File overview — {clean}/{total} clean, attempt {attempt_n}:")
        max_name = max((len(f.split('/')[-1]) for f in all_files), default=10)
        rows_data = []
        for f in sorted(all_files, key=lambda f: (-err_by_file.get(f, 0), f)):
            errs = err_by_file.get(f, 0)
            st = fstats.get(f, {})
            lines = st.get("lines", 0)
            fns = st.get("fns", 0)
            short = f.split("/")[-1]
            bar_s = "█" * min(errs, 8) if errs else "✓"
            _log(f"    {short.ljust(max_name)}  {str(lines).rjust(4)}L  {str(fns).rjust(3)}fn  {bar_s} {errs or 'clean'}")
            rows_data.append({"file": f, "lines": lines, "fns": fns, "errors": errs})
        _emit("compile.file_stats", data={
            "attempt": attempt_n, "clean": clean, "total": total, "files": rows_data,
        })
        # When the check command runs tests (t3 fix loop), publish per-file
        # test results alongside the compile stats.
        _emit_per_file_test_overview(
            project_dir, output, context=f"fix attempt {attempt_n}")

    env = _cargo_env()
    _fl_max_stalls = int(os.getenv("GENERATE_FIX_LOOP_MAX_STALLS", "3"))
    # `max_fix_attempts` retained as a kwarg for backward-compat but is no
    # longer a counter cap — it's used only as a logging hint. Real exit is
    # the floor-stall in `_improvement_loop`.
    _ = max_fix_attempts

    def _run_check() -> tuple[bool, str, int]:
        # Graceful-stop check at the top of each iter.
        _graceful_sentinel = Path("/tmp/pipeline_graceful_stop")
        if _graceful_sentinel.exists():
            with suppress(OSError):
                _graceful_sentinel.unlink()
            _log("  [fix loop] Graceful stop requested — exiting cleanly.")
            sys.exit(0)
        try:
            result = subprocess.run(
                _parse_cmd(check_cmd), cwd=project_dir,
                capture_output=True, text=True, timeout=timeout, env=env,
            )
            output = result.stdout + (
                "\n--- stderr ---\n" + result.stderr if result.stderr else ""
            )
            if result.returncode == 0:
                return True, output, 0
        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT after {timeout}s", 10**9
        metric = len(_error_sigs(output))
        return False, output, metric

    def _do_fix(output: str, history: list[dict], *, retro: str) -> dict:
        if output.startswith("TIMEOUT after "):
            return {"abort": True, "reason": "check_timeout",
                    "summary": "check timed out"}
        _log_compile_overview(output, len(history))
        # Build source context — read current content from disk in full.
        source_block = ""
        for p in source_paths:
            rel = str(p.relative_to(project_dir))
            if rel in existing_test_files or not p.exists():
                continue
            content = p.read_text(encoding="utf-8")
            source_block += f'\n<FILE path="{rel}">\n{content}\n</FILE>\n'

        # Running narrative of prior fix attempts (from improvement-loop history).
        _attempt_lines: list[str] = []
        for rec in history:
            iter_n = rec.get("iter", "?")
            chg = rec.get("files_changed_str", "no files changed")
            err_prev = rec.get("error_preview", "")
            _attempt_lines.append(
                f"Attempt {iter_n}: changed [{chg}] → still failing.\n"
                f"Error was:\n{err_prev}"
            )
        _history_section = ""
        if _attempt_lines:
            n = len(_attempt_lines)
            _history_section = (
                f"You have already made {n} fix attempt{'s' if n != 1 else ''} in this loop. "
                f"Here is what happened — do NOT repeat the same changes:\n\n"
                + "\n\n".join(_attempt_lines)
                + "\n\nNow make a different fix that avoids every approach above.\n"
            )

        # Strategy retrospective injected when prior iter didn't improve.
        _retro_block = (
            f"\n<RETROSPECTIVE_GUIDANCE>\nThe previous attempt did not reduce "
            "the compile error count. Apply this strategy instead of repeating "
            f"the prior approach:\n{retro}\n</RETROSPECTIVE_GUIDANCE>\n"
            if retro.strip() else ""
        )

        _gotchas_brief    = _gotchas_summary()
        _gotchas_specific = _gotchas_for_error(output)
        _gotchas_block    = f"\n<RUST_GOTCHAS>\n{_gotchas_brief}\n</RUST_GOTCHAS>\n"
        if _gotchas_specific:
            _gotchas_block += (
                f"\n<RELEVANT_GOTCHAS_DETAIL>\n{_gotchas_specific}\n"
                "</RELEVANT_GOTCHAS_DETAIL>\n"
            )
        _spec_header = "\nFunctional requirements (the contract code must satisfy):\n" if functional_req else ""
        _fl_scaffold = f"""Code generation produced files that fail the check below.
{_FIX_THINKING_DIRECTIVE}Fix ONLY the files causing the failure — do NOT regenerate correct files.
{_history_section}{_spec_header}{_retro_block}{_gotchas_block}
Error output:
```

```

Current source files:


{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}"""
        _fl_overhead = len(_fl_scaffold)
        _fl_min_err  = 10_000
        _fl_min_src  = 30_000
        _fl_min_spec = 8_000 if functional_req else 0
        _extra_budget = max(0,
            SINGLE_CALL_CHAR_LIMIT - _fl_overhead - _fl_min_err - _fl_min_src - _fl_min_spec)
        _extra_ctx_capped = smart_truncate(extra_context, total_limit=_extra_budget,
                                           head=_extra_budget // 3, tail=2 * _extra_budget // 3)
        _after_extra = max(_fl_min_err + _fl_min_src + _fl_min_spec,
                           SINGLE_CALL_CHAR_LIMIT - _fl_overhead - len(_extra_ctx_capped))
        if functional_req:
            _spec_cap = _after_extra // 4
            _remaining = _after_extra - _spec_cap
            _err_cap = _remaining // 3
            _src_cap = _remaining * 2 // 3
            _spec_capped = smart_truncate(functional_req, total_limit=_spec_cap,
                                          head=_spec_cap // 3, tail=2 * _spec_cap // 3)
            _spec_block = f"\nFunctional requirements (the contract code must satisfy):\n{_spec_capped}\n"
        else:
            _err_cap = _after_extra // 3
            _src_cap = _after_extra * 2 // 3
            _spec_block = ""
        _last_output_capped = smart_truncate(output, total_limit=_err_cap,
                                             head=_err_cap // 4, tail=3 * _err_cap // 4)
        _source_block_capped = smart_truncate(source_block, total_limit=_src_cap,
                                              head=_src_cap // 3, tail=2 * _src_cap // 3)
        # Repository Specifications complement: genesis rules + methodology
        # (the FR is already in _spec_block; no target identifiers here, so
        # the FR-excerpt/API parts of the section stay empty by design).
        _repo_block = _tag_block(
            "REPOSITORY_SPECIFICATIONS",
            _repo_spec_section(fr_path, char_budget=_REPO_SPEC_CAP_WIDE))
        prompt = f"""Code generation produced files that fail the check below.
{_FIX_THINKING_DIRECTIVE}Fix ONLY the files causing the failure — do NOT regenerate correct files.
{_history_section}{_spec_block}{_repo_block}{_retro_block}{_gotchas_block}
Error output:
```
{_last_output_capped}
```

Current source files:
{_source_block_capped}
{_extra_ctx_capped}
{FILE_FORMAT_INSTRUCTION}
{_TASK2_VERBOSITY_GUARD}"""
        _log(f"  [fix loop] iter {len(history) + 1} — asking LLM to fix specific failures.")
        response = ask(prompt, model=model)
        fixed = extract_files(response)
        if not fixed:
            _log(f"  [fix loop] LLM produced no fixes on iter {len(history) + 1}.")
            _err_preview = output[-_FIX_ERR_CAP:] if len(output) > _FIX_ERR_CAP else output
            return {"summary": "no_fixes_produced",
                    "files_changed_str": "no files changed",
                    "error_preview": _err_preview}
        # Whole-file rewrites (legacy strategy — the model returns each broken
        # file in full and we overwrite it).
        for rel, content in fixed.items():
            if rel in existing_test_files:
                continue
            if Path(rel).suffix not in SOURCE_EXTENSIONS:
                continue
            if allowed_dir_prefix and not rel.startswith(allowed_dir_prefix):
                _log(f"  [fix loop] REJECTED {rel}: outside allowed dir '{allowed_dir_prefix}'.")
                continue
            stripped = content.lstrip()
            if re.match(r'^error(\[E\d+\])?:', stripped) or re.match(r'^warning(\[W\d+\])?:', stripped):
                _log(f"  [fix loop] REJECTED {rel}: content looks like compiler output, not code.")
                continue
            write(project_dir / rel, content)
            _log(f"  [fix loop] Fixed: {rel}")

        _all_changed = sorted(fixed.keys())
        _err_preview = output[-_FIX_ERR_CAP:] if len(output) > _FIX_ERR_CAP else output

        # BL-025: log fix patterns for future prompts.
        if _all_changed:
            _bl025_module = allowed_dir_prefix.rstrip('/').split('/')[-2] if allowed_dir_prefix else ""
            if _bl025_module:
                _error_category = _categorize_compile_error(output)
                _fix_hint = "; ".join(_all_changed[:3])
                compile_patterns_write(
                    project_dir, _bl025_module, _error_category,
                    example=output[:200],
                    fix_hint=f"fixed in {_fix_hint}",
                )
        return {
            "summary": f"rewrote={len(_all_changed)}",
            "files_changed_str": ", ".join(_all_changed[:4]) if _all_changed else "no files changed",
            "error_preview": _err_preview,
            "error_sig": _err_preview,
        }

    def _do_retro(output: str, history: list[dict]) -> str:
        return _whole_tree_retrospective(
            error_excerpt=output, history=history, name="generate_fix_loop",
        )

    # --- shared helpers for the per_fn strategy (unit = fn | impl) ----------
    def _tests_for_names(names: "set[str]") -> str:
        """Test files referencing any of `names`, budget-capped."""
        parts: list[str] = []
        used = 0
        for trel in sorted(existing_test_files):
            tp = project_dir / trel
            if not tp.exists():
                continue
            try:
                tc = tp.read_text(encoding="utf-8")
            except OSError:
                continue
            if not any(n and n in tc for n in names):
                continue
            blk = f'<TEST path="{trel}">\n{tc}\n</TEST>'
            if used + len(blk) > _TYPES_SECTION_CAP:
                break
            parts.append(blk); used += len(blk)
        return ("\nTests this code must satisfy:\n" + "\n".join(parts) + "\n") if parts else ""

    def _gotchas_block_for(out: str) -> str:
        gd = _gotchas_for_error(out)
        block = f"\n<RUST_GOTCHAS>\n{_gotchas_summary()}\n</RUST_GOTCHAS>\n"
        if gd:
            block += f"<RELEVANT_GOTCHAS_DETAIL>\n{gd}\n</RELEVANT_GOTCHAS_DETAIL>\n"
        return block

    def _fix_single_fn(rel: str, fn: str) -> list[str]:
        """Inner cargo-check loop that re-fills ONE fn by body splice. Returns
        the changed unit ids (0 or 1)."""
        changed: list[str] = []
        path = project_dir / rel
        if not path.exists():
            return changed
        inner_cmd = _derive_check_only_cmd(check_cmd)
        fn_container = ""
        for _r in (_enumerate_fns_via_extractor(project_dir) or []):
            if _r.get("file") == rel and _r.get("fn_name") == fn:
                fn_container = _r.get("container") or ""
                break

        def _run_check(_rel=rel, _fn=fn) -> tuple[bool, str, int]:
            rc, rendered, diags = _run_cargo_check_diag(inner_cmd, project_dir, env, timeout)
            # Build the frontier from STRUCTURED diags, not the ANSI-rendered
            # text (the plain-text regex can't parse colour codes → empty).
            fr = _frontier_from_diags(diags, project_dir)
            file_errs = sum(1 for d in diags if (d.file or "") == _rel)
            ok = (rc == 0) or ((_rel, _fn) not in fr)
            return ok, _strip_ansi(rendered), file_errs

        def _do(out: str, hist: list[dict], *, retro: str = "",
                _rel=rel, _fn=fn, _path=path) -> dict:
            file_text = _path.read_text(encoding="utf-8", errors="replace")
            loc = _locate_fn(file_text, _fn)
            if loc is None:
                return {"abort": True, "reason": "fn_not_located"}
            sig_text, _ss, _bs, _be = loc
            cur_body = file_text[_bs:_be]
            errs = out[-_FIX_ERR_CAP:] if len(out) > _FIX_ERR_CAP else out
            rblock = (f"\n<RETROSPECTIVE_GUIDANCE>\n{retro}\n</RETROSPECTIVE_GUIDANCE>\n"
                      if retro.strip() else "")
            # After a body-only attempt stalled, a PRIVATE fn may tighten its
            # bounds (unsized-T class: returns `T` by value but `T` is `?Sized`
            # → needs `where T: Sized`). Only a leading `where …` may be added.
            allow_where = len(hist) >= 1 and not _is_pub_fn(file_text, _fn)
            sig_rule = (
                "Signature is FIXED — except, if this fn fails because a type "
                "parameter needs a tighter bound (E0277 'size cannot be known'), "
                "you MAY add it by making the FIRST body line a `where` clause "
                "(e.g. `where T: Sized`).\n"
            ) if allow_where else "Signature (do NOT change it):\n"
            _repo_block = _tag_block(
                "REPOSITORY_SPECIFICATIONS",
                _repo_spec_section(fr_path, file_rel=_rel, fn_name=_fn,
                                   container=fn_container or ""))
            prompt = f"""ONE Rust function does not compile. Return ONLY its corrected body.
{_FIX_THINKING_DIRECTIVE}File: {_rel} | Fn: {_fn}
{sig_rule}{sig_text}

Current body (it does NOT compile):
{{
{cur_body}
}}

Compiler errors:
```
{errs}
```
{_repo_block}{rblock}{_gotchas_block_for(out)}{_tests_for_names({_fn})}
Return the corrected body as EXACTLY one block (statements only — no fn
signature, no outer braces):
<FN name="{_fn}" block="any">
...corrected body...
</FN>
Output nothing else."""
            resp = ask(prompt, model=model)
            bodies = extract_fn_bodies(resp)
            blk = next((b for b in bodies if b.get("fn_name") == _fn),
                       bodies[0] if bodies else None)
            body = blk.get("body") if blk else None
            if not body:
                return {"summary": "no_body", "files_changed_str": "no files changed",
                        "error_preview": out[-_FIX_ERR_CAP:]}
            wc = blk.get("where_clause") if blk else None
            use_wc = wc if (allow_where and wc) else None
            ok_s, reason, delta = _splice_fn_with_where(
                _path, fn_container, _fn, body, where_clause=use_wc, allow_nonstub=True)
            if ok_s:
                changed.append(f"{_rel}::{_fn}")
                _log(f"  [fix loop/fn] spliced {_rel}::{_fn}"
                     f"{' +where' if use_wc else ''} ({delta:+d} bytes)")
            else:
                _log(f"  [fix loop/fn] splice rejected {_rel}::{_fn} ({reason})")
            return {"summary": f"fn {_rel}::{_fn}", "files_changed_str": f"{_rel}::{_fn}",
                    "error_preview": out[-_FIX_ERR_CAP:]}

        _log(f"  [fix loop/fn] fixing {rel}::{fn} (inner cargo check loop)")
        _improvement_loop(name=f"fn:{rel}::{fn}", run_check=_run_check, do_fix=_do,
                          do_retro=_do_retro,
                          max_stalls=int(os.getenv("FIX_LOOP_PER_FN_MAX_STALLS", "2")))
        return changed

    def _fix_impl_block(rel: str, container: str, block: dict) -> list[str]:
        """Inner cargo-check loop that re-fills a WHOLE impl block in one prompt
        (the model sees the header, where-clauses, every member body, and any
        sibling impl of the same trait). Each returned body is spliced into the
        CORRECT impl via container-aware location — so two same-name methods in
        different impls (E0201/E0053 as_ref) are disambiguated. Returns changed
        unit ids."""
        changed: list[str] = []
        path = project_dir / rel
        if not path.exists():
            return changed
        inner_cmd = _derive_check_only_cmd(check_cmd)
        members = block.get("members", [])
        member_names = {m.get("fn_name") for m in members if m.get("fn_name")}
        siblings = block.get("related_containers", []) or []

        def _run_check(_rel=rel, _names=tuple(member_names)) -> tuple[bool, str, int]:
            rc, rendered, diags = _run_cargo_check_diag(inner_cmd, project_dir, env, timeout)
            # Frontier from STRUCTURED diags (ANSI-rendered text defeats the
            # plain-text regex → empty frontier → the loop would think the
            # block is clean and never ask the model).
            fr = _frontier_from_diags(diags, project_dir)
            group_errs = sum(1 for (f2, fn2) in fr if f2 == _rel and fn2 in _names)
            file_errs = sum(1 for d in diags if (d.file or "") == _rel)
            ok = (rc == 0) or (group_errs == 0)
            return ok, _strip_ansi(rendered), file_errs

        def _do(out: str, hist: list[dict], *, retro: str = "") -> dict:
            file_text = path.read_text(encoding="utf-8", errors="replace")
            block_src = _render_impl_block(file_text, members)
            errs = out[-_FIX_ERR_CAP:] if len(out) > _FIX_ERR_CAP else out
            sib_block = (
                "\nSibling impl blocks of the same trait in this file — keep them "
                "mutually consistent (e.g. matching return types / bounds):\n"
                + "\n".join(f"- {s}" for s in siblings) + "\n"
            ) if siblings else ""
            rblock = (f"\n<RETROSPECTIVE_GUIDANCE>\n{retro}\n</RETROSPECTIVE_GUIDANCE>\n"
                      if retro.strip() else "")
            # After a body-only attempt has stalled, allow the model to tighten a
            # PRIVATE fn's bounds — the unsized-T class (e.g. `read(self) -> T`
            # needing `where T: Sized`) cannot be fixed by a body alone. Only a
            # leading `where …` may be added; the rest of the signature is fixed
            # and public fns are never touched (API surface). Spliced via the
            # syn extractor's byte range — no anchor matching.
            allow_where = len(hist) >= 1
            where_rule = (
                "\nIf — and ONLY if — a PRIVATE (non-`pub`) fn fails because a type "
                "parameter needs a tighter bound (e.g. E0277 'size cannot be known': "
                "the fn returns `T` by value but `T` is `?Sized`), you MAY add a bound "
                "by making the FIRST line of that fn's body a `where` clause:\n"
                '<FN name="read" block="impl">\nwhere T: Sized\n... body ...\n</FN>\n'
                "Do not otherwise change any signature. Never add a bound to a `pub` fn.\n"
            ) if allow_where else "Do NOT change any signature.\n"
            _repo_block = _tag_block(
                "REPOSITORY_SPECIFICATIONS",
                _repo_spec_section(fr_path, file_rel=rel, container=container))
            prompt = f"""A Rust impl block fails to compile. Fix the function BODIES so the
whole block compiles. Change ONLY what the errors require; leave already-correct
functions untouched. {where_rule}
{_FIX_THINKING_DIRECTIVE}File: {rel}
Impl block:
```rust
{block_src}
```
{sib_block}
Compiler errors:
```
{errs}
```
{_repo_block}{rblock}{_gotchas_block_for(out)}{_tests_for_names(member_names)}
For EACH function you change, return one block (body statements only — no fn
signature line, no outer braces). Use the function name verbatim:
<FN name="FUNCTION_NAME" block="impl">
...corrected body...
</FN>
Output nothing else."""
            resp = ask(prompt, model=model)
            fn_blocks = extract_fn_bodies(resp)
            spliced = 0
            for fb in fn_blocks:
                nm = fb.get("fn_name"); body = fb.get("body")
                if not nm or not body:
                    continue
                # Honour a where-clause only for PRIVATE fns (API surface guard).
                wc = fb.get("where_clause")
                use_wc = wc if (allow_where and wc and not _is_pub_fn(file_text, nm)) else None
                ok_s, reason, delta = _splice_fn_with_where(
                    path, container, nm, body, where_clause=use_wc, allow_nonstub=True)
                if ok_s:
                    spliced += 1
                    changed.append(f"{rel}::{nm}")
                    _log(f"  [fix loop/impl] spliced {rel}::{nm}"
                         f"{' +where' if use_wc else ''} ({delta:+d} bytes)")
                else:
                    _log(f"  [fix loop/impl] splice rejected {rel}::{nm} ({reason})")
            if spliced == 0:
                return {"summary": "no_splice", "files_changed_str": "no files changed",
                        "error_preview": out[-_FIX_ERR_CAP:]}
            return {"summary": f"impl {rel} ({spliced} fn)",
                    "files_changed_str": f"{rel}::{container[:40]}",
                    "error_preview": out[-_FIX_ERR_CAP:]}

        _log(f"  [fix loop/impl] fixing {rel}::{container[:50]} (inner cargo check loop)")
        _improvement_loop(name=f"impl:{rel}::{container[:40]}", run_check=_run_check,
                          do_fix=_do, do_retro=_do_retro,
                          max_stalls=int(os.getenv("FIX_LOOP_PER_FN_MAX_STALLS", "2")))
        return changed

    def _do_fix_per_fn(output: str, history: list[dict], *, retro: str) -> dict:
        """unit=fn: group compile errors by enclosing fn, fix each unique fn in
        its own inner cargo-check loop. Ambiguous (same-name sibling impls) and
        non-compile failures → legacy whole-file. Fail-safe → legacy."""
        if output.startswith("TIMEOUT after "):
            return {"abort": True, "reason": "check_timeout", "summary": "check timed out"}
        try:
            _log_compile_overview(output, len(history))
            frontier = _compile_error_frontier(
                output, project_dir, check_cmd, env, timeout)
            if not frontier:
                return _do_fix(output, history, retro=retro)
            recs = _enumerate_fns_via_extractor(project_dir) or []
            name_count: dict[tuple[str, str], int] = {}
            for r in recs:
                k = (r.get("file", ""), r.get("fn_name", ""))
                name_count[k] = name_count.get(k, 0) + 1
            changed: list[str] = []
            deferred = False
            for (rel, fn) in sorted(frontier.keys()):
                if rel in existing_test_files:
                    continue
                if allowed_dir_prefix and not rel.startswith(allowed_dir_prefix):
                    continue
                if name_count.get((rel, fn), 0) != 1:
                    deferred = True
                    continue
                changed += _fix_single_fn(rel, fn)
            if deferred:
                rc, rendered, _ = _run_cargo_check_diag(
                    _derive_check_only_cmd(check_cmd), project_dir, env, timeout)
                if rc != 0:
                    _log("  [fix loop/fn] file-level errors remain → whole-file fallback")
                    _do_fix(rendered, history, retro=retro)
            _err_preview = output[-_FIX_ERR_CAP:] if len(output) > _FIX_ERR_CAP else output
            return {"summary": f"per_fn changed={len(changed)}",
                    "files_changed_str": ", ".join(changed[:4]) if changed else "no files changed",
                    "error_preview": _err_preview, "error_sig": _err_preview}
        except Exception as e:
            _log(f"  [fix loop/fn] unexpected error ({e}) → legacy whole-file fallback")
            return _do_fix(output, history, retro=retro)

    def _do_fix_per_impl(output: str, history: list[dict], *, retro: str) -> dict:
        """unit=impl (default, mirrors PHASE_C_FILL_UNIT): group compile errors by
        their enclosing impl block and fix the whole block in one inner
        cargo-check loop — the model sees impl-level constraints and sibling
        impls that per-fn cannot. Free functions fall to the per-fn path; the
        extractor being unavailable or a non-compile failure → fall back."""
        if output.startswith("TIMEOUT after "):
            return {"abort": True, "reason": "check_timeout", "summary": "check timed out"}
        try:
            _log_compile_overview(output, len(history))
            frontier = _compile_error_frontier(
                output, project_dir, check_cmd, env, timeout)
            if not frontier:
                tdd_frontier = _extract_tdd_frontier(output, project_dir)
                kind = "tdd-stub"
                behavior_srcs: dict[str, tuple[str, str]] = {}
                if not tdd_frontier:
                    # Behavioral failures (assertions, wrong values): attribute
                    # failing tests to the fns they exercise and repair those
                    # bodies — the whole-file rewrite is the LAST resort (it
                    # regresses: observed live introducing 24 compile errors).
                    tdd_frontier, behavior_srcs = _behavior_frontier(
                        output, project_dir, existing_test_files)
                    kind = "behavior"
                    if not tdd_frontier:
                        return _do_fix(output, history, retro=retro)
                    _beh_cap = int(os.getenv("FIX_BEHAVIOR_MAX_FNS", "8"))
                    if len(tdd_frontier) > _beh_cap:
                        # Most-failing-tests first; name tiebreak for determinism.
                        tdd_frontier = dict(sorted(
                            tdd_frontier.items(),
                            key=lambda kv: (-len(kv[1]), kv[0]))[:_beh_cap])
                # Stubs that compile but fail tests: direct one-shot fill.
                # _fix_single_fn is wrong here — its inner compile loop exits
                # immediately (ok=True) because the stub has no compile errors.
                tdd_changed: list[str] = []
                for (rel, fn) in sorted(tdd_frontier.keys()):
                    if rel in existing_test_files:
                        continue
                    if allowed_dir_prefix and not rel.startswith(allowed_dir_prefix):
                        continue
                    path = project_dir / rel
                    if not path.exists():
                        continue
                    file_text = path.read_text(encoding="utf-8", errors="replace")
                    loc = _locate_fn(file_text, fn)
                    if loc is None:
                        continue
                    sig_text, _ss, _bs, _be = loc
                    cur_body = file_text[_bs:_be]
                    errs = output[-_FIX_ERR_CAP:] if len(output) > _FIX_ERR_CAP else output
                    if kind == "behavior":
                        _tests_block = ""
                        for _tn in sorted(tdd_frontier[(rel, fn)]):
                            _trel, _tsrc = behavior_srcs.get(_tn, ("", ""))
                            if _tsrc and len(_tests_block) + len(_tsrc) <= _SRC_SNIP_CAP:
                                _tests_block += f"// {_trel} :: {_tn}\n{_tsrc}\n\n"
                        prompt = (
                            f"ONE Rust function compiles but FAILS its tests — "
                            f"its behaviour is wrong. Correct the body.\n"
                            f"{_FIX_THINKING_DIRECTIVE}"
                            f"File: {rel} | Fn: {fn}\n"
                            f"Signature (do NOT change it):\n{sig_text}\n\n"
                            f"Current body (wrong — replace it):\n{{{{\n{cur_body}\n}}}}\n\n"
                            f"Failing test source (the behaviour the body must satisfy):\n"
                            f"```\n{_tests_block}```\n"
                            f"Test failure output:\n```\n{errs}\n```\n"
                            f"Return the corrected body as EXACTLY one block "
                            f"(statements only — no fn signature, no outer braces):\n"
                            f'<FN name="{fn}" block="any">\n...body...\n</FN>\n'
                            f"Output nothing else."
                        )
                    else:
                        prompt = (
                            f"ONE Rust function has an unimplemented body — fill it.\n"
                            f"{_FIX_THINKING_DIRECTIVE}"
                            f"File: {rel} | Fn: {fn}\n"
                            f"Signature (do NOT change it):\n{sig_text}\n\n"
                            f"Current body (unimplemented — replace it):\n{{{{\n{cur_body}\n}}}}\n\n"
                            f"Test failure output:\n```\n{errs}\n```\n"
                            f"Return the corrected body as EXACTLY one block "
                            f"(statements only — no fn signature, no outer braces):\n"
                            f'<FN name="{fn}" block="any">\n...body...\n</FN>\n'
                            f"Output nothing else."
                        )
                    resp = ask(prompt, model=model)
                    bodies = extract_fn_bodies(resp)
                    blk = next(
                        (b for b in bodies if b.get("fn_name") == fn),
                        bodies[0] if bodies else None,
                    )
                    body = blk.get("body") if blk else None
                    if body:
                        ok_s, delta = _splice_fn_body(path, fn, body, allow_nonstub=True)
                        if ok_s:
                            tdd_changed.append(f"{rel}::{fn}")
                            _log(f"  [fix loop/{kind}] spliced {rel}::{fn} ({delta:+d} bytes)")
                        else:
                            _log(f"  [fix loop/{kind}] splice rejected {rel}::{fn}")
                _err_preview = output[-_FIX_ERR_CAP:] if len(output) > _FIX_ERR_CAP else output
                _kind_tag = "tdd_stub" if kind == "tdd-stub" else "behavior"
                return {"summary": f"{_kind_tag} changed={len(tdd_changed)}",
                        "files_changed_str": ", ".join(tdd_changed[:4]) if tdd_changed else "no files changed",
                        "error_preview": _err_preview, "error_sig": _err_preview}
            blocks = _collect_impl_blocks(project_dir)
            if not blocks:
                return _do_fix_per_fn(output, history, retro=retro)
            fn_to_block: dict[tuple[str, str], dict] = {}
            for b in blocks:
                for m in b.get("members", []):
                    fn_to_block[(b["file"], m.get("fn_name"))] = b
            changed: list[str] = []
            impl_groups: dict[tuple[str, str], dict] = {}
            free_fns: list[tuple[str, str]] = []
            for (rel, fn) in frontier:
                if rel in existing_test_files:
                    continue
                if allowed_dir_prefix and not rel.startswith(allowed_dir_prefix):
                    continue
                b = fn_to_block.get((rel, fn))
                if b is None or not b.get("container"):
                    free_fns.append((rel, fn))
                else:
                    impl_groups[(b["file"], b["container"])] = b
            for (f2, cont) in sorted(impl_groups.keys()):
                changed += _fix_impl_block(f2, cont, impl_groups[(f2, cont)])
            for (rel, fn) in sorted(set(free_fns)):
                changed += _fix_single_fn(rel, fn)
            _err_preview = output[-_FIX_ERR_CAP:] if len(output) > _FIX_ERR_CAP else output
            return {"summary": f"per_impl changed={len(changed)}",
                    "files_changed_str": ", ".join(changed[:4]) if changed else "no files changed",
                    "error_preview": _err_preview, "error_sig": _err_preview}
        except Exception as e:
            _log(f"  [fix loop/impl] unexpected error ({e}) → legacy whole-file fallback")
            return _do_fix(output, history, retro=retro)

    _fix_strategy = SETTINGS.fix_loop_strategy
    _fix_unit = SETTINGS.fix_loop_unit
    if _fix_strategy is FixStrategy.PER_FN:
        _chosen_do_fix = _do_fix_per_impl if _fix_unit is FillUnit.IMPL else _do_fix_per_fn
    else:
        _chosen_do_fix = _do_fix
    _log(f"  [fix loop] strategy={_fix_strategy} unit={_fix_unit}")

    ok, last_output, _hist = _improvement_loop(
        name="generate_fix_loop",
        run_check=_run_check,
        do_fix=_chosen_do_fix,
        do_retro=_do_retro,
        max_stalls=_fl_max_stalls,
    )
    return ok, last_output


def task2_fix_loop(
    project_dir: Path,
    manifest_path: Path,
    existing_test_files: set,
    test_output: str,
    test_command: str,
    functional_req_path: Path,
    genesis_path: Path | None = None,
    module_dir_rel: str = "",
) -> tuple[bool, str]:
    # slice 5b: extracted to application.tasks.task2; thin shim binding deps.
    return _task2_fix_impl(
        project_dir, manifest_path, existing_test_files, test_output, test_command,
        functional_req_path, _TASK2_DEPS, genesis_path=genesis_path, module_dir_rel=module_dir_rel,
    )


# slice 5b: deps for the extracted TASK 2 (defined after generate_fix_loop, its latest dep).
_TASK2_DEPS = Task2Deps(
    generator_factory=_task2_generator_factory,
    load_manifest=load_manifest,
    read=read,
    generate_fix_loop=generate_fix_loop,
    store_prompt_content=_store_prompt_content,
    is_proc_macro_crate=_is_proc_macro_crate,
    count_error_codes=_count_rustc_error_codes,
    build_check_retries=BUILD_CHECK_RETRIES,
    build_max_stalls=int(os.getenv("TASK2_BUILD_MAX_STALLS", "2")),
    build_timeout=BUILD_TIMEOUT,
    test_timeout=TEST_TIMEOUT,
    task2_model=_TASK2_MODEL,
    task2_fix_retries=TASK2_FIX_RETRIES,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    genesis_cap=_GENESIS_CAP,
)


# P7.3: task3 inline retry. Currently task3 is one-shot; failure escalates
# straight to task4 + full regen. The inline retry catches the case where
# only a handful of fns broke a test — classifier identifies them, a single
# LLM call per fn fixes the body, cargo test re-runs. Bounded by stall budget
# and a snapshot/revert guard so we never leave the tree worse than we found it.

def _task3_inline_retry(
    project_dir: Path,
    test_command: str,
    iteration,
    initial_test_output: str,
    cluster: "BuildCluster | None" = None,
) -> tuple[bool, str]:
    """Improvement-driven retry over a failed task3. Returns (passed, final_output).
    If the retry cannot beat the initial failure floor, restores the source
    tree from snapshot and returns (False, initial_output).
    """
    if os.getenv("TASK3_INLINE_RETRY", "1") == "0":
        return False, initial_test_output

    # R9 residual: be CLUSTER-AWARE. task3 runs each cluster with its own toolchain
    # + env_extra (RUSTFLAGS/cfg); the failing cluster is threaded in so the retry
    # reproduces THAT cluster's build under THAT env — the global `_cargo_env()` would
    # use whatever `_current_cluster` is stale-set to, so a cluster-cfg-specific compile
    # error (e.g. nightly-only) would not reproduce and BUILD mode would silently bail.
    _cluster_label = cluster.name if cluster is not None else "task3"
    if cluster is not None:
        _parts = ["cargo"]
        if cluster.toolchain:
            _parts.append(f"+{cluster.toolchain}")
        _parts += ["test", "--no-fail-fast"]
        test_command = shlex.join(_parts)

    def _env() -> dict:
        # Mirror task3's per-cluster invocation: base cargo env, then the cluster's
        # env_extra overlaid (its RUSTFLAGS/cfg win over the stale global cluster's).
        e = _cargo_env()
        if cluster is not None and cluster.env_extra:
            e.update(cluster.env_extra)
        return e

    max_stalls = int(os.getenv("TASK3_INLINE_MAX_STALLS", "3"))
    hard_cap = int(os.getenv("TASK3_INLINE_HARD_CAP", "10"))

    # R9.1: two modes. TEST mode (some test FAILED) attributes via the test
    # reverse-map/classifier. BUILD mode (zero FAILED lines because the tree did
    # not COMPILE) attributes via structured cargo diagnostics and fixes the fns
    # the compile errors point at — TASK 4 (a test-failure analyzer) cannot, since
    # it has no failing-test identifiers. compile_cmd compiles the tests without
    # running them so a 0-test "incomplete" cluster yields real error diagnostics.
    failed = sorted(_parse_failed_tests(initial_test_output))
    compile_cmd = test_command if "--no-run" in test_command else test_command + " --no-run"
    build_mode = not failed
    if build_mode:
        _, _, _init_diags = _run_cargo_check_diag(
            compile_cmd, project_dir, _env(), timeout=TEST_TIMEOUT)
        if not _init_diags:
            # No FAILED tests AND no reproducible compile errors under this env
            # (e.g. the failure is cluster-cfg-specific) — nothing to act on here.
            return False, initial_test_output
        initial_count = len(_init_diags)
        _log(f"  [task3-retry] build-failure mode: {initial_count} compile error(s).")
    else:
        initial_count = len(failed)
        _log(f"  [task3-retry] entering inline retry: {initial_count} failing test(s).")

    # Snapshot every .rs file under project_dir/src — only files we might
    # touch. Excluding tests/ keeps the snapshot cheap. Use bytes to handle
    # any odd encodings without surprise.
    snapshot: dict[Path, bytes] = {}
    src_root = project_dir / "src"
    if src_root.is_dir():
        for p in src_root.rglob("*.rs"):
            with suppress(Exception):
                snapshot[p] = p.read_bytes()

    # P5.1 parity: fix prompts here previously carried no TYPES_IN_SCOPE and
    # no container — the model had to guess struct fields. Enumerate from the
    # current (reconstructed) src; cluster-filter so only methods that compile
    # under THIS cluster are advertised (Hard Rule 8).
    _retry_type_recs = _cluster_filter_type_methods(
        _enumerate_types_via_extractor(src_root) or [], cluster)
    _retry_containers = _fn_container_index(src_root)
    # Model-notes recall + relevant tests (slice 8a improvement): these fix
    # prompts previously carried neither. Resolve the module FR doc when
    # unambiguous (single-module case: exactly one functional_requirements_*.md;
    # multi-module would need a per-file module map — left as today's None).
    _fr_candidates = sorted(project_dir.glob("functional_requirements_*.md"))
    _retry_fr_path = _fr_candidates[0] if len(_fr_candidates) == 1 else None
    _retry_tests_dir_files = list((project_dir / "tests").rglob("*.rs"))

    def _apply_per_fn_fixes(refs: list[tuple[str, str]], output: str) -> int:
        # Re-ask the model for each (file, fn) body and splice it. Frozen tests
        # are never rewritten — skip tests/ and gen_tests siblings even if a
        # compile error's span lands inside one (Hard Rule: tests are frozen).
        fixed_count = 0
        for rel_f, fn in refs:
            if rel_f.startswith("tests/") or _is_gen_tests_file(Path(rel_f).name):
                continue
            fp = project_dir / rel_f
            if not fp.exists():
                continue  # classifier may have hallucinated a path
            try:
                cur_text = fp.read_text(encoding="utf-8")
            except Exception:
                continue
            sloc = _locate_fn(cur_text, fn)
            if sloc is None:
                continue
            sig_t = sloc[0]
            cur_body = cur_text[sloc[2]:sloc[3]]
            err_excerpt = _focus_cargo_errors(output, rel_f, max_chars=_FIX_ERR_CAP)
            # Container index keys are rel to src/, rel_f is rel to project_dir.
            _src_rel = rel_f[4:] if rel_f.startswith("src/") else rel_f
            _ctx = _gather_fn_fix_context(
                file_text=cur_text, file_rel=rel_f, fn_name=fn,
                sig_text=sig_t, body_text=cur_body,
                error_excerpt=err_excerpt,
                container=_retry_containers.get((_src_rel, fn), ""),
                type_records=_retry_type_recs,
                fr_path=_retry_fr_path,
            )
            t_blocks = _find_tests_for_fn(
                _retry_tests_dir_files, fn, file_rel=rel_f,
                src_dir=src_root, container=_ctx.container)
            prompt = _build_per_fn_fix_prompt(
                file_text=cur_text, fn_name=fn, sig_text=sig_t,
                test_blocks=t_blocks, module_name=project_dir.name,
                cluster_name=_cluster_label, file_rel=rel_f,
                prev_body=cur_body, error_excerpt=err_excerpt,
                container=_ctx.container,
                types_section=_ctx.types_section,
                notes_section=_ctx.notes_section,
                fr_path=_retry_fr_path,  # P6.4 past-note recall by error code
            )
            new_body, _ = _ask_per_fn_body(prompt, fn, [], sig_text=sig_t)
            if not new_body:
                continue
            # allow_nonstub: a build/test failure comes from a fn with a WRONG,
            # already-filled (non-stub) body — like every other fix-loop site
            # (cf. 17001/17100). A bare `unimplemented!()` stub compiles, so it
            # never causes the failure; without allow_nonstub `_splice_fn_body`
            # refused every real target and the inline retry was a silent no-op.
            ok2, _ = _splice_fn_body(fp, fn, new_body, allow_nonstub=True)
            if ok2:
                fixed_count += 1
                _log(f"  [task3-retry] re-spliced {rel_f}::{fn}.")
        return fixed_count

    def _run_check_test() -> tuple[bool, str, int]:
        rc, out = _run_cargo(test_command, project_dir, _env(),
                             timeout=TEST_TIMEOUT)
        f = _parse_failed_tests(out)
        return rc == 0, out, len(f)

    def _run_check_build() -> tuple[bool, str, int]:
        rc, rendered, diags = _run_cargo_check_diag(
            compile_cmd, project_dir, _env(), timeout=TEST_TIMEOUT)
        # ok = the tree COMPILES; metric = remaining compile errors (lower better).
        return rc == 0, _strip_ansi(rendered), len(diags)

    def _do_fix_test(output: str, history: list[dict]) -> dict:
        # FR-024: full attribution chain. The classifier is one step in a
        # longer chain — when it returns no actionable refs, fall back to
        # the test-reverse-map. Previously this gave up at classifier
        # exhaustion; now it has more candidates to try.
        fails_now = sorted(_parse_failed_tests(output))
        if not fails_now:
            return {"abort": True, "reason": "no_failed_tests"}
        try:
            tests_dir_files = list((project_dir / "tests").rglob("*.rs"))
            test_reverse_map = _build_test_reverse_map(
                tests_dir_files, project_dir)
        except Exception:
            test_reverse_map = {}
        frontier, _kind = _attribute_failures(
            output, project_dir,
            recently_filled=[],  # task3 has no per-cluster fill history
            test_reverse_map=test_reverse_map,
            cluster_name=_cluster_label,
        )
        refs: list[tuple[str, str]] = list(frontier.keys())
        if not refs:
            return {"abort": True, "reason": "no_actionable_refs"}
        fixed_count = _apply_per_fn_fixes(refs, output)
        return {
            "summary": f"fixed_{fixed_count}/{len(refs)}",
            "fns_touched": refs,
            "abort": fixed_count == 0,
            "reason": "no_fns_spliced" if fixed_count == 0 else "",
        }

    def _do_fix_build(output: str, history: list[dict]) -> dict:
        _, _, diags = _run_cargo_check_diag(
            compile_cmd, project_dir, _env(), timeout=TEST_TIMEOUT)
        if not diags:
            return {"abort": True, "reason": "no_compile_errors"}
        frontier = _frontier_from_diags(diags, project_dir)
        refs: list[tuple[str, str]] = list(frontier.keys())
        if not refs:
            return {"abort": True, "reason": "unattributed_compile_errors"}
        fixed_count = _apply_per_fn_fixes(refs, output)
        return {
            "summary": f"fixed_{fixed_count}/{len(refs)}",
            "fns_touched": refs,
            "abort": fixed_count == 0,
            "reason": "no_fns_spliced" if fixed_count == 0 else "",
        }

    passed, last_out, history = _improvement_loop(
        name="task3.inline_retry",
        run_check=_run_check_build if build_mode else _run_check_test,
        do_fix=_do_fix_build if build_mode else _do_fix_test,
        max_stalls=max_stalls,
        hard_cap=hard_cap,
    )

    if build_mode:
        # Re-measure compile errors. Revert if the retry INCREASED them; recurse
        # (changed output) only on strict progress so task3's re-verification
        # terminates — fewer errors each round, or back to the unchanged input.
        _, _, fin_diags = _run_cargo_check_diag(
            compile_cmd, project_dir, _env(), timeout=TEST_TIMEOUT)
        final_err = len(fin_diags)
        if snapshot and final_err > initial_count:
            _log(f"  [task3-retry] build-fix worsened compile ({final_err} > "
                 f"{initial_count}) — reverting from snapshot.")
            for p, data in snapshot.items():
                with suppress(Exception):
                    p.write_bytes(data)
            return False, initial_test_output
        if final_err < initial_count:
            _log(f"  [task3-retry] build-fix progress: {final_err} compile "
                 f"error(s) (initial {initial_count}) — re-running TASK 3.")
            return False, last_out
        _log(f"  [task3-retry] build-fix made no progress ({final_err} error(s)) "
             "— escalating to TASK 4.")
        return False, initial_test_output

    final_failed = _parse_failed_tests(last_out)
    if passed:
        _log(f"  [task3-retry] tests now PASS after {len(history)} iter(s).")
        return True, last_out
    # Decide whether to keep partial progress or revert. R9.2: a non-compiling tree
    # emits NO `FAILED` lines, so `len(final_failed)` reads as 0 ("near success") even
    # when a re-splice broke the build. Compile/ran-state is the real signal — if the
    # tree ran tests before the retry but no longer does, the retry made it STRICTLY
    # WORSE (a tree that won't compile is worse than N failing tests) → revert.
    init_p, init_f, _ = _parse_test_counts(initial_test_output)
    fin_p, fin_f, _ = _parse_test_counts(last_out)
    init_ran = (init_p + init_f) > 0
    final_ran = (fin_p + fin_f) > 0
    broke_build = init_ran and not final_ran
    if snapshot and (broke_build or len(final_failed) > initial_count):
        reason = ("retry broke the build (tests no longer run)" if broke_build
                  else f"result {len(final_failed)} > initial {initial_count}")
        _log(f"  [task3-retry] {reason} — reverting from snapshot.")
        for p, data in snapshot.items():
            with suppress(Exception):
                p.write_bytes(data)
        return False, initial_test_output
    _log(f"  [task3-retry] stopped at {len(final_failed)} failing tests "
         f"(initial {initial_count}). Keeping partial progress.")
    return False, last_out


def _emit_per_file_test_overview(project_dir: Path, output: str, *, context: str) -> None:
    """Attribute the latest cargo-test output to files (domain
    per_file_test_stats; name-based, interleaving-proof) and publish the
    per-file pass/total table to the log + a tests.file_stats event, which
    the monitor merges into its compile overview. No test lines → no-op."""
    try:
        project_dir = Path(project_dir)
        known = {
            str(p.relative_to(project_dir)).replace(os.sep, "/")
            for d in ("src", "tests") if (project_dir / d).is_dir()
            for p in (project_dir / d).rglob("*.rs")
        }
        texts: dict[str, str] = {}
        if (project_dir / "tests").is_dir():
            for p in (project_dir / "tests").rglob("*.rs"):
                with suppress(OSError):
                    texts[str(p.relative_to(project_dir)).replace(os.sep, "/")] = (
                        p.read_text(encoding="utf-8", errors="replace")
                    )
        stats = per_file_test_stats(output, known, build_test_name_index(texts))
    except Exception as exc:
        _log(f"  [test overview] skipped: {exc}")
        return
    if not stats:
        return
    ordered = sorted(stats.items(), key=lambda kv: (-kv[1]["failed"], kv[0]))
    tot_p = sum(s["passed"] for _, s in ordered)
    tot_t = sum(s["passed"] + s["failed"] for _, s in ordered)
    _log(f"  [test overview] {context} — {tot_p}/{tot_t} tests passing:")
    name_w = max(len(f.split("/")[-1]) for f, _ in ordered)
    for f, s in ordered:
        short = f.split("/")[-1]
        total = s["passed"] + s["failed"]
        mark = "✓" if s["failed"] == 0 and total else "✗"
        ign = f"  ({s['ignored']} ignored)" if s["ignored"] else ""
        _log(f"    {short.ljust(name_w)}  {mark} tests {s['passed']}/{total}{ign}")
    _emit("tests.file_stats", data={
        "context": context, "passed": tot_p, "total": tot_t,
        "files": [{"file": f, **s} for f, s in ordered],
    })


def task3(project_dir, test_command, iteration, output_dir, genesis_path):
    # slice 5b: extracted to pipeline_core.application.tasks.task3; thin shim binding deps.
    passed, test_output = _task3_impl(
        project_dir, test_command, iteration, output_dir, genesis_path, _TASK3_DEPS
    )
    _emit_per_file_test_overview(
        Path(project_dir), extract_test_output_from_file(test_output),
        context=f"task3 iter {iteration}",
    )
    return passed, test_output


def _task3_legacy_single(
    project_dir, test_command, iteration, output_dir, genesis_path,
) -> tuple[bool, str]:
    """Pre-FR-024 single-shot path. Retained for pipelines without baked
    clusters (non-skeleton backends, plain-cargo projects). Uses the
    discovered `test_command` and the lenient `validate_task3_output`
    classifier. New skeleton-backend runs never hit this path."""
    output_file = Path(output_dir) / f"tests_output_{iteration}.txt"
    env = _cargo_env()
    _attempt = iteration if isinstance(iteration, int) else None

    with _phase("task", task="task3", attempt=_attempt,
                data={"command": test_command, "iteration": iteration}) as done:
        _emit("test.start", attempt=_attempt, data={"command": test_command})
        _t_start = time.monotonic()
        try:
            result = subprocess.run(
                _parse_cmd(test_command),
                cwd=project_dir, capture_output=True, text=True,
                timeout=TEST_TIMEOUT, env=env,
            )
        except subprocess.TimeoutExpired:
            msg = f"TIMEOUT: tests exceeded {TEST_TIMEOUT} seconds."
            genesis = read(genesis_path)
            write(output_file,
                  f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{msg}")
            _log(f"  {msg}")
            _emit("test.fail",
                  duration_ms=int((time.monotonic() - _t_start) * 1000),
                  data={"reason": "timeout", "timeout_s": TEST_TIMEOUT})
            done["data"] = {"passed": False, "reason": "timeout"}
            return False, msg
        except FileNotFoundError as e:
            msg = (f"ERROR: test command not found: {e}\n"
                   f"Command was: {test_command}\n"
                   "Ensure the required runtime is installed and on PATH.")
            genesis = read(genesis_path)
            write(output_file,
                  f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{msg}")
            _log(f"  {msg}")
            _emit("test.fail",
                  duration_ms=int((time.monotonic() - _t_start) * 1000),
                  data={"reason": "command_not_found", "error": str(e)[:200]})
            done["data"] = {"passed": False, "reason": "command_not_found"}
            sys.exit(1)

        test_output = result.stdout
        if result.stderr:
            test_output += "\n--- stderr ---\n" + result.stderr
        genesis = read(genesis_path)
        write(output_file,
              f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{test_output}")
        passed   = result.returncode == 0
        _pass_n, _fail_n, _ = _parse_test_counts(test_output)
        _log(f"  Tests {'PASSED' if passed else 'FAILED'}. Output -> {output_file}")
        _emit("test.pass" if passed else "test.fail",
              duration_ms=int((time.monotonic() - _t_start) * 1000),
              data={"returncode": result.returncode,
                    "pass_count": _pass_n, "fail_count": _fail_n,
                    "output": _store_prompt_content(test_output)})
        if not passed:
            retry_passed, retry_output = _task3_inline_retry(
                project_dir=project_dir,
                test_command=test_command,
                iteration=iteration,
                initial_test_output=test_output,
            )
            if retry_passed:
                passed = True
                test_output = retry_output
                write(output_file,
                      f"=== genesis.md (iteration {iteration}) ===\n{genesis}"
                      f"\n{OUTPUT_SEPARATOR}\n{test_output}")
                _pass_n, _fail_n, _ = _parse_test_counts(test_output)
                _log(f"  Tests PASSED via inline retry. Output -> {output_file}")
                _emit("test.pass", duration_ms=int(
                    (time.monotonic() - _t_start) * 1000),
                      data={"returncode": 0, "pass_count": _pass_n,
                            "fail_count": _fail_n, "via": "task3_inline_retry",
                            "output": _store_prompt_content(test_output)})
            elif retry_output != test_output:
                test_output = retry_output
                write(output_file,
                      f"=== genesis.md (iteration {iteration}) ===\n{genesis}"
                      f"\n{OUTPUT_SEPARATOR}\n{test_output}")
                _pass_n, _fail_n, _ = _parse_test_counts(test_output)
        done["data"] = {"passed": passed, "pass_count": _pass_n, "fail_count": _fail_n}
        return passed, test_output


def task4(genesis_path, functional_req_path, test_output, iteration, output_dir,
          failure_type: str = "test", failure_log: str = "",
          prev_pass_count: int = 0, prev_test_output: str = "",
          source_context: str = "", api_surface: str = ""):
    # slice 5b: extracted to pipeline_core.application.tasks.task4; thin shim binding deps.
    return _task4_impl(
        genesis_path, functional_req_path, test_output, iteration, output_dir, _TASK4_DEPS,
        failure_type=failure_type, failure_log=failure_log,
        prev_pass_count=prev_pass_count, prev_test_output=prev_test_output,
        source_context=source_context, api_surface=api_surface,
    )


def _task4_extract_and_store_rules(project_dir: Path, genesis_text: str,
                                    module_scope: str, failure_type: str,
                                    iteration: int) -> None:
    """BL-024: Parse bullet-point rules from genesis.md and write to genesis_rules table."""
    # Map failure type to category tag.
    category = {
        "build": "compile",
        "assertion": "assertion",
        "panic": "runtime_panic",
        "timeout": "timeout",
        "no_tests": "test_discovery",
    }.get(failure_type, "general")

    # Extract lines that look like rules (bullet points or numbered items).
    rule_re = re.compile(r'^\s*[-*•]\s+(.+)$|^\s*\d+\.\s+(.+)$', re.MULTILINE)
    rules: list[str] = []
    for m in rule_re.finditer(genesis_text):
        rule = (m.group(1) or m.group(2) or "").strip()
        if len(rule) > 20:  # skip trivially short lines
            rules.append(rule)

    # Store unique rules (deduplicate by content prefix).
    stored_prefixes: set[str] = set()
    for rule in rules[:20]:  # cap at 20 per iteration
        prefix = rule[:40]
        if prefix not in stored_prefixes:
            stored_prefixes.add(prefix)
            genesis_rules_write(project_dir, module_scope, category, rule, iteration)

    if rules:
        _log(f"  [BL-024] Stored {len(stored_prefixes)} structured rules "
             f"(category: {category}) from genesis update.")


# ---------------------------------------------------------------------------
# TASK 1.5 — Static use-statement audit (no LLM)
# ---------------------------------------------------------------------------

def task1_static_audit(functional_req_path: Path, test_files: list) -> list[str]:
    """
    Parse every test file for `use` statements and check that each imported
    symbol path appears in functional_requirements.md.

    Returns a (possibly empty) list of symbol paths that are absent from the
    requirements. Callers should log a warning and optionally re-run TASK 1
    on the missing symbols.

    No LLM call — pure regex. Runs in milliseconds.
    """
    SKIP_PREFIXES = ('std::', 'core::', 'alloc::', 'super::', 'self::', 'crate::')

    reqs = read(functional_req_path)
    if not reqs.strip():
        return []

    found: set[str] = set()
    for _rel, content in test_files:
        # use foo::bar::{A, B, C};  or  use foo::bar::Baz;
        for m in re.finditer(r'\buse\s+([\w:]+(?:::\{[^}]+\})?)\s*;', content):
            raw = m.group(1)
            if '::' not in raw:
                continue
            if '::{' in raw:
                prefix, items_str = raw.split('::{', 1)
                for item in items_str.rstrip('}').split(','):
                    item = item.strip()
                    if item:
                        found.add(f"{prefix}::{item}")
            else:
                found.add(raw)

    gaps = [
        sym for sym in sorted(found)
        if not any(sym.startswith(p) for p in SKIP_PREFIXES)
        and sym not in reqs
    ]

    _log(f"\nTASK 1.5: {len(found)} use-paths in tests, {len(gaps)} missing from requirements.")
    if gaps:
        for g in gaps[:10]:
            _log(f"  missing: {g}")
        if len(gaps) > 10:
            _log(f"  ... and {len(gaps) - 10} more")
    return gaps


# ---------------------------------------------------------------------------
# Genesis audit — detect and resolve contradictions in genesis.md
# ---------------------------------------------------------------------------

# Run a genesis audit every this many total module iterations (across all modules).
GENESIS_AUDIT_INTERVAL = 5

# After this many consecutive module iterations with no pass-count improvement,
# escalate to whole-project context in TASK 2.
NO_IMPROVEMENT_THRESHOLD = 3


# slice 5b: genesis audit extracted to pipeline_core.application.tasks.genesis_audit.
# Deps bind the pipeline's collaborators once; main() calls the shim below unchanged.
_GENESIS_AUDIT_DEPS = GenesisAuditDeps(
    ask=ask, read=read, write=write, model=_TASK4_MODEL,
    verbosity_guard=_TASK4_VERBOSITY_GUARD,
)


def task_genesis_audit(genesis_path: Path) -> bool:
    return genesis_audit(genesis_path, _GENESIS_AUDIT_DEPS)


# ---------------------------------------------------------------------------
# Coverage augmentation (TASK 0c) — pre-strip step
# Generates unit tests (inline #[cfg(test)]) for each module until 100%
# line coverage is reached per tarpaulin.
# Must run before strip_to_tests.py (full implementation must be present).
# Trigger: pipeline.py --augment-tests
# ---------------------------------------------------------------------------

def _cargo_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".cargo" / "bin") + ":" + env.get("PATH", "")
    # Suppress all Rust compiler warnings — only errors appear in output.
    # Keeps LLM error context clean and eliminates false-positive noise.
    env["RUSTFLAGS"] = "-A warnings"
    env["CARGO_TERM_COLOR"] = "never"
    # Do NOT force RUST_BACKTRACE=1 here. It used to be set so tarpaulin would
    # instrument backtrace-conditional code paths (e.g. fmt.rs's debug formatter),
    # but it also makes legitimate library tests fail when they assert on Debug
    # output — for example anyhow's tests/test_fmt.rs:83 compares against a
    # backtrace-free string. With RUST_BACKTRACE=1 the test panics, cargo-test
    # exits non-zero, and cargo-llvm-cov aborts before writing lcov, leaving the
    # pipeline measuring 0/0 lines on every pass. If you ever need backtrace
    # coverage for tarpaulin, set RUST_BACKTRACE only inside TarpaulinProvider.
    # Apply cluster-specific env overrides (e.g. RUSTFLAGS=--cfg error_generic_member_access).
    # RUSTFLAGS entries are appended to avoid clobbering the base -A warnings flag.
    if _current_cluster and _current_cluster.env_extra:
        for k, v in _current_cluster.env_extra.items():
            if k == "RUSTFLAGS":
                env["RUSTFLAGS"] = env["RUSTFLAGS"] + " " + v
            else:
                env[k] = v
    # If this cluster requires nightly, tell rustup to use nightly toolchain.
    if _current_cluster and _current_cluster.requires_nightly:
        env["RUSTUP_TOOLCHAIN"] = "nightly"
    return env


# slice 5b: deps records for the extracted TASK 3 / TASK 4. Defined here, after every
# collaborator they bind (notably _cargo_env). The task3/task4 shims above read these.
_TASK3_DEPS = Task3Deps(
    phase=_phase,
    load_baked_clusters=_load_baked_clusters,
    task3_legacy_single=_task3_legacy_single,
    cargo_env=_cargo_env,
    parse_cmd=_parse_cmd,
    count_active_tests_for_cluster=_count_active_tests_for_cluster,
    store_prompt_content=_store_prompt_content,
    task3_inline_retry=_task3_inline_retry,
    read=read,
    write=write,
    test_timeout=TEST_TIMEOUT,
)
_TASK4_DEPS = Task4Deps(
    ask=ask,
    read=read,
    write=write,
    extract_and_store_rules=_task4_extract_and_store_rules,
    model=MODEL,
    task4_model=_TASK4_MODEL,
    verbosity_guard=_TASK4_VERBOSITY_GUARD,
    intermediate_char_limit=INTERMEDIATE_CHAR_LIMIT,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
)


def _describe_test_environment() -> str:
    """Return a prompt-ready description of the cargo subprocess env.

    The pipeline knows which env vars and cfg predicates are set for the
    test process; the LLM doesn't. Without this signal, the LLM can propose
    strategies that the pipeline knows can't work — for example writing tests
    that try to enable a feature whose env var was never set in the parent
    process. Describe the env factually; let the LLM draw its own implications.

    Generic across projects: only states what IS / IS NOT set + the active
    test runner. No project-specific reasoning is baked in.
    """
    env = _cargo_env()

    # Variables the LLM should always see if present.
    visible = ("RUSTFLAGS", "CARGO_TERM_COLOR", "RUSTUP_TOOLCHAIN")
    # Variables whose ABSENCE is informative — std libs commonly gate behavior
    # on these and cache the lookup at process start, so per-test set_var
    # doesn't help.
    notable = ("RUST_BACKTRACE", "RUST_LIB_BACKTRACE", "RUST_LOG")

    lines = [
        "--- Test environment (cargo subprocess under coverage instrumentation) ---",
    ]
    for k in visible:
        if k in env:
            lines.append(f"  {k} = {env[k]!r}")
    unset = [k for k in notable if k not in env]
    if unset:
        lines.append(f"  NOT SET in parent process: {', '.join(unset)}")
        lines.append(
            "  Code paths whose execution depends on these variables (env-gated "
            "logging, backtrace capture, etc.) are unreachable from this test run."
        )

    if _current_cluster is not None:
        active = ", ".join(_current_cluster.active_cfg) if _current_cluster.active_cfg else "(none)"
        excluded = ", ".join(_current_cluster.excluded_cfg) if _current_cluster.excluded_cfg else "(none)"
        lines.append(f"  active cfg predicates: {active}")
        lines.append(f"  excluded cfg predicates: {excluded}")

    lines.append("  test runner: `cargo test --test-threads=1` (under cargo-llvm-cov instrumentation)")
    return "\n".join(lines) + "\n"


def _crate_name_from_toml(module_dir: Path, fallback: str) -> str:
    """Extract `name` from Cargo.toml and normalise hyphens to underscores."""
    toml = module_dir / "Cargo.toml"
    if toml.exists():
        name = _cargo_package_name(toml.read_text())
        if name:
            return name.replace("-", "_")
    return fallback.replace("-", "_")


# Slice 4b: coverage I/O relocated to pipeline_core.adapters.coverage. _COV_DEPS injects
# the pipeline's log + cargo-env + live-cluster accessor + tool paths/timeout/pragma so the
# adapter stays import-independent of pipeline.py. `cluster` is a callable (the active
# cluster is reassigned across the run; resolve at call time = original global semantics).
_COV_DEPS = CoverageDeps(
    log=_log,
    cargo_env=_cargo_env,
    cluster=lambda: _current_cluster,
    tarpaulin_bin=TARPAULIN_BIN,
    grcov_bin=GRCOV_BIN,
    build_timeout=BUILD_TIMEOUT,
    ignore_pragma=COVERAGE_IGNORE_PRAGMA,
)


def _run_tarpaulin(project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
    # Thin shim until call sites migrate (slice 5).
    return _covmod.run_tarpaulin(_COV_DEPS, project_dir, module_name)


def _mutation_survivors(
    project_dir: Path,
    cluster: "BuildCluster | None" = None,
    file_rel: "str | None" = None,
    timeout: int = 1200,
) -> "list[str] | None":
    """Run cargo-mutants and return the SURVIVING ("missed") mutants — the
    behaviour changes the current test suite is BLIND to (M1, behaviour
    hardening, FR-039). Each entry is the mutant description, e.g.
    `src/fmt.rs:87:25: replace > with >= in <impl Write for Indented>::write_str`.

    Uses the cluster's toolchain (`+nightly`) and env so survivors reflect that
    cluster's real build. Returns None when cargo-mutants is unavailable or no
    report was produced. cargo-mutants exits non-zero when mutants survive, so
    the report (mutants.out/missed.txt) is read regardless of exit code.
    """
    if not (Path.home() / ".cargo" / "bin" / "cargo-mutants").exists() \
            and shutil.which("cargo-mutants") is None:
        return None
    cmd = ["cargo"]
    tc = getattr(cluster, "toolchain", None) if cluster is not None else None
    if tc:
        cmd.append(f"+{tc}")
    cmd += ["mutants", "--no-shuffle"]
    if file_rel:
        cmd += ["--file", file_rel]
    try:
        subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True,
                       timeout=timeout, env=_cargo_env())
    except Exception:
        return None
    missed = project_dir / "mutants.out" / "missed.txt"
    if not missed.exists():
        return None
    try:
        return [ln.strip() for ln in missed.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    except OSError:
        return None


def _mutation_kill_check(
    project_dir: Path,
    cluster: "BuildCluster | None",
    survivor: str,
    test_code: str,
    target_rel: str,
    timeout: int = 900,
) -> "tuple[bool, bool]":
    """M2 discrimination harness. Returns (real_passes, kills_survivor).

    Temporarily attaches `test_code` as a test module to `target_rel`, runs the
    real suite (the test MUST pass on the correct code), then re-runs
    cargo-mutants scoped to the survivor's file and checks the survivor moved
    missed→caught. Always restores the file. Black-box: operates on test code
    and observable behaviour only — never the mutant diff (FR-036).
    """
    surv_file = survivor.split(":", 1)[0] if survivor else None
    tgt = project_dir / target_rel
    if not tgt.exists():
        return (False, False)
    sib = tgt.with_name(f"{tgt.stem}.mut_candidate.rs")
    decl = f'\n#[cfg(test)]\n#[allow(clippy::all, unused)]\n#[path = "{sib.name}"]\nmod mut_candidate;\n'
    orig = tgt.read_text(encoding="utf-8")
    tc = getattr(cluster, "toolchain", None) if cluster is not None else None
    try:
        sib.write_text(test_code, encoding="utf-8")
        tgt.write_text(orig.rstrip() + decl, encoding="utf-8")
        cmd = ["cargo"] + ([f"+{tc}"] if tc else []) + ["test", "--quiet"]
        r = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True,
                           timeout=timeout, env=_cargo_env())
        if r.returncode != 0:
            return (False, False)            # invalid test (won't compile/pass)
        if not surv_file:
            return (True, False)
        remaining = _mutation_survivors(project_dir, cluster, file_rel=surv_file,
                                        timeout=timeout)
        killed = remaining is not None and survivor not in remaining
        return (True, killed)
    except Exception:
        return (False, False)
    finally:
        tgt.write_text(orig, encoding="utf-8")
        try:
            if sib.exists():
                sib.unlink()
        except OSError:
            pass


def _mutation_harden_file(
    project_dir: Path,
    module_name: str,
    cluster: "BuildCluster | None",
    file_rel: str,
    max_rounds: int = 3,
    timeout: int = 900,
) -> str:
    """M3: generate BLACK-BOX behaviour tests (public API only, FR-036) that
    reduce the surviving mutants for `file_rel`. The LLM never sees source
    bodies or mutant diffs — only the crate's public API surface. Tests are
    accepted only when they (a) compile + pass on the correct code and (b)
    actually lower the surviving-mutant count. Returns the accumulated kept
    test code ("" if nothing helped or no survivors).
    """
    baseline = _mutation_survivors(project_dir, cluster, file_rel, timeout)
    if not baseline:
        return ""
    tgt = project_dir / file_rel
    if not tgt.exists():
        return ""
    api = (load_api_surface(project_dir, module_name) or "")[: SINGLE_CALL_CHAR_LIMIT // 3]
    sib = tgt.with_name(f"{tgt.stem}.mut_hardening.rs")
    decl = (f'\n#[cfg(test)]\n#[allow(clippy::all, unused)]\n'
            f'#[path = "{sib.name}"]\nmod mut_hardening;\n')
    orig = tgt.read_text(encoding="utf-8")
    tc = getattr(cluster, "toolchain", None) if cluster is not None else None
    kept: list[str] = []
    cur = set(baseline)
    try:
        for _r in range(max_rounds):
            if not cur:
                break
            prompt = (
                "Write Rust `#[test]` functions that exercise and ASSERT the exact "
                "observable behaviour of this crate's PUBLIC API — return values, "
                "panics/Err, and precise formatted output (Display/Debug). Cover "
                "edge cases and boundaries; pin exact strings/values with "
                "`assert_eq!`. Use ONLY the public API below — do not reference "
                "internal items. The current suite still misses subtle behaviour "
                "changes; make your assertions tight enough to catch them.\n"
                f'<PUBLIC_API crate="{module_name}">\n{api}\n</PUBLIC_API>\n'
                "Output ONLY the test code (no prose, no explanation)."
            )
            try:
                resp = ask(prompt, model=_TASK0C_MODEL)
            except Exception:
                break
            code = _strip_markdown_fences(resp or "").strip()
            if not code:
                continue
            sib.write_text("\n\n".join(kept + [code]), encoding="utf-8")
            tgt.write_text(orig.rstrip() + decl, encoding="utf-8")
            cmd = ["cargo"] + ([f"+{tc}"] if tc else []) + ["test", "--quiet"]
            try:
                rr = subprocess.run(cmd, cwd=project_dir, capture_output=True,
                                    text=True, timeout=timeout, env=_cargo_env())
            except Exception:
                continue
            if rr.returncode != 0:
                continue   # invalid tests — discard this round
            remaining = _mutation_survivors(project_dir, cluster, file_rel, timeout)
            if remaining is None:
                continue
            if len(remaining) < len(cur):
                kept.append(code)
                cur = set(remaining)
                _log(f"  [mut-harden] {file_rel}: survivors "
                     f"{len(baseline)}→{len(cur)} (kept round {_r + 1})")
        return "\n\n".join(kept)
    finally:
        tgt.write_text(orig, encoding="utf-8")
        try:
            if sib.exists():
                sib.unlink()
        except OSError:
            pass


def _task0c_mutation_pass(
    project_dir: Path,
    module_name: str,
    cluster: "BuildCluster | None",
    rel_files: "list[str]",
) -> int:
    """M4: behaviour-hardening pass over a cluster's source files. For each file
    with surviving mutants, generate black-box tests (M3) and persist the kept
    ones as a gen_tests sibling so they survive strip and reattach per-cluster
    (FR-038). Opt-in via TASK0C_MUTATION_HARDEN=1 (mutation-kill as the strength
    metric on top of coverage). Returns the number of files hardened.
    """
    if os.getenv("TASK0C_MUTATION_HARDEN", "0").strip() in ("", "0", "false"):
        return 0
    hardened = 0
    for rel in rel_files:
        src = project_dir / rel
        if not src.exists():
            continue
        try:
            code = _mutation_harden_file(project_dir, module_name, cluster, rel)
        except Exception as e:
            _log(f"  [mut-harden] {rel}: skipped ({e})")
            continue
        if not code.strip():
            continue
        # persist as the next gen_tests sibling of this file
        existing = _gen_tests_siblings(src)
        n = (max((k for k, _ in existing), default=0)) + 1
        sib = _gen_tests_path(src, n)
        sib.write_text(
            f"// mutation-hardening tests (M4) — behaviour, not coverage\n"
            f"#[cfg(test)]\nmod gen_tests_{n} {{\n    use super::*;\n{code}\n}}\n",
            encoding="utf-8")
        hardened += 1
        _log(f"  [mut-harden] {rel}: persisted hardening tests as gen_tests_{n}")
    return hardened


def _quick_file_coverage(project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
    """Per-file coverage snapshot. Delegates to the active CoverageProvider."""
    return _coverage_provider.measure_file(project_dir, module_name, rel_path)


def _measure_coverage(project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
    _t0 = time.monotonic()
    pct, files_data, covered, coverable = _coverage_provider.measure(project_dir, module_name)
    _emit("coverage.measured",
          module=module_name,
          cluster=_current_cluster.name if _current_cluster is not None else None,
          duration_ms=int((time.monotonic() - _t0) * 1000),
          data={"pct": pct, "covered": covered, "coverable": coverable,
                "backend": COVERAGE_BACKEND, "file_count": len(files_data)})
    return pct, files_data, covered, coverable


# ── Coverage provider (relocated to pipeline_core.adapters.coverage, slice 4b) ──
# Built from _COV_DEPS at the composition root; selection keyed on COVERAGE_BACKEND.
_coverage_provider: _covmod.CoverageProvider = _covmod.make_coverage_provider(
    COVERAGE_BACKEND, _COV_DEPS,
)


# ─────────────────────────────────────────────────────────────────────────────
# gen_tests sibling-file storage
#
# Each LLM gen_tests pass for src/foo.rs writes its module body to a sibling
# file src/foo.gen_tests_N.rs, and adds a one-line path-mod declaration to
# foo.rs:
#     #[cfg(test)] #[allow(...)] #[path = "foo.gen_tests_N.rs"] mod gen_tests_N;
#
# This avoids coverage-tool contamination (test code lives in its own file,
# excluded by --ignore-filename-regex) and removes the need for GRCOV_EXCL_*
# markers wrapping inline test blocks.
# ─────────────────────────────────────────────────────────────────────────────

GEN_TESTS_DECL_RE = re.compile(
    r'\n*#\[cfg\(test\)\][\s\n]*'
    r'(?:#\[[^\]]*\][\s\n]*)*'
    r'#\[path\s*=\s*"[^"]+\.gen_tests_\d+\.rs"\][\s\n]*'
    r'mod\s+gen_tests_\d+\s*;',
)


def _gen_tests_siblings(src_file: Path) -> list[tuple[int, Path]]:
    """Return [(N, path)] for all foo.gen_tests_N.rs siblings of src_file, sorted by N."""
    stem = src_file.stem
    pattern = re.compile(rf'^{re.escape(stem)}\.gen_tests_(\d+)\.rs$')
    out: list[tuple[int, Path]] = []
    parent = src_file.parent
    if parent.exists():
        for f in parent.iterdir():
            m = pattern.match(f.name)
            if m:
                out.append((int(m.group(1)), f))
    out.sort()
    return out


def _gen_tests_count(src_file: Path) -> int:
    return len(_gen_tests_siblings(src_file))


def _next_gen_tests_n(src_file: Path) -> int:
    """Return the next free gen_tests pass number for src_file.
    Uses max(existing) + 1 to skip over gaps from deleted siblings."""
    siblings = _gen_tests_siblings(src_file)
    return (siblings[-1][0] + 1) if siblings else 1


def _gen_tests_path(src_file: Path, n: int) -> Path:
    return src_file.with_name(f"{src_file.stem}.gen_tests_{n}.rs")


def _read_all_gen_tests(src_file: Path) -> str:
    """Concatenate gen_tests sibling bodies, each re-wrapped in `mod gen_tests_N { ... }`
    for LLM prompt context (so the LLM sees the same shape it generates)."""
    parts: list[str] = []
    for n, sibling in _gen_tests_siblings(src_file):
        try:
            body = sibling.read_text(encoding="utf-8").rstrip()
        except OSError:
            continue
        parts.append(f"#[cfg(test)]\nmod gen_tests_{n} {{\n{body}\n}}")
    return "\n\n".join(parts)


def _strip_gen_tests_decls(src_text: str) -> str:
    """Remove all gen_tests path-mod declarations from a source file's text."""
    return GEN_TESTS_DECL_RE.sub("", src_text)


def _is_gen_tests_file(name: str) -> bool:
    """True if `name` is a pipeline-generated `*.gen_tests_<N>.rs` sibling."""
    return _match_gen_tests_sibling(name) is not None


def _match_gen_tests_sibling(name: str) -> "tuple[str, int] | None":
    """Parse a `<stem>.gen_tests_<N>.rs` filename → (stem, N), or None."""
    if not name.endswith(".rs"):
        return None
    stem, sep, n_str = name[:-3].rpartition(".gen_tests_")
    if not sep or not stem or not n_str.isdigit():
        return None
    return stem, int(n_str)


def _strip_gen_tests_decl_n(parent_text: str, pass_n: int) -> str:
    """Remove the `#[cfg(test)] … mod gen_tests_<N>;` decl for one pass. (Regex in pipeline.)"""
    decl_re = re.compile(
        r'\n*#\[cfg\(test\)\][\s\n]*(?:#\[[^\]]*\][\s\n]*)*'
        r'#\[path\s*=\s*"[^"]+"\][\s\n]*'
        rf'mod\s+gen_tests_{pass_n}\s*;',
    )
    return decl_re.sub("", parent_text)


def _error_signatures_from_output(cargo_output: str) -> frozenset:
    """Extract stable rustc error signatures (code + message, no file/line). (Regex in pipeline.)"""
    return frozenset(re.findall(r'error\[E\d+\]: .+', cargo_output))


# Prelude imports injected into generated test modules when absent (item, presence-regex).
_GEN_PRELUDE_ITEMS = [
    ("use std::string::{String, ToString};", r'use (?:std|alloc)::[^;]*\bString\b'),
    ("use std::vec::Vec;",                   r'use (?:std|alloc)::[^;]*\bVec\b'),
    ("use std::boxed::Box;",                 r'use (?:std|alloc)::[^;]*\bBox\b'),
    ("use std::format;",                     r'use (?:std|alloc)::[^;]*\bformat\b'),
    ("use std::error::Error as StdError;",   r'use std::error::Error'),
    ("use std::fmt::{self, Display, Debug};", r'use (?:std|core)::fmt'),
]


def _inject_allow_before_gen_mod(block: str, allow_line: str) -> str:
    """Insert `allow_line` immediately before the `mod gen_*` opener. (Regex in pipeline.)"""
    if allow_line in block:
        return block
    return re.sub(r'((?:pub\s+)?mod\s+gen_\w+\s*\{)', f'{allow_line}\n\\1', block, count=1)


def _inject_allow_before_any_mod(block: str, allow_line: str) -> str:
    """Insert `allow_line` before the first `mod <name>` opener (any name). (Regex in pipeline.)"""
    if allow_line in block:
        return block
    return re.sub(r'((?:pub\s+)?mod\s+\w+\s*\{)', f'{allow_line}\n\\1', block, count=1)


def _inject_gen_prelude_imports(block: str) -> str:
    """Inject any missing std prelude imports into a generated test module. (Regex in pipeline.)"""
    missing = [stmt for stmt, pat in _GEN_PRELUDE_ITEMS if not re.search(pat, block)]
    if not missing:
        return block
    inject = "\n    ".join(missing)
    if "use super::*;" in block:
        return block.replace("use super::*;", f"use super::*;\n    {inject}", 1)
    return re.sub(r'((?:pub\s+)?mod\s+gen_\w+\s*\{\s*\n)', f'\\1    {inject}\n', block, count=1)


def _count_test_attrs(block: str) -> int:
    """Number of `#[test]` (incl. path-qualified) attributes in a block. (Regex in pipeline.)"""
    return len(re.findall(r'#\[(?:\w+::)*test\]', block))


def _strip_trailing_file_tag(text: str) -> str:
    """Strip a trailing `</FILE>` tag (+ surrounding whitespace) and strip. (Regex in pipeline.)"""
    return re.sub(r'\s*</FILE>\s*$', '', text).strip()


def _block_is_prose(block: str) -> bool:
    """True if a generated block looks like prose instructions, not code. (Regex in pipeline.)"""
    return bool(re.search(r'block to our Rust file|Here\'s how|You can add|here is', block, re.IGNORECASE))


def _has_nonempty_test_body(block: str) -> bool:
    """True if the block has at least one `#[test]` fn with a non-trivial body. (Regex in pipeline.)"""
    return bool(re.search(r'#\[test\][^{]*\{[^}]*\S', block, re.DOTALL))


def _flags_unreachable(text: str) -> bool:
    """True if the model declared the target genuinely uncoverable. (Regex in pipeline.)"""
    return bool(re.search(
        r'dead.code|unreachable|genuinely|not coverable|cannot be covered|grcov artifact',
        text, re.IGNORECASE,
    ))


def _parse_attempt_number(detail: str) -> "int | None":
    """Parse the `attempt N` counter from a saved phase detail string. (Regex in pipeline.)"""
    m = re.search(r"attempt (\d+)", detail)
    return int(m.group(1)) if m else None


def _is_structural_line(text: str) -> bool:
    """True for structural-only Rust lines (`}`, `} else {`, etc.) that LLVM miscounts.
    (Regex in pipeline.)"""
    return bool(re.match(r'^\}(\s*else\s*\{)?\s*[;,]?\s*$', text))


# slice 5b: deps for the extracted TASK 0b discovery (defined after its parsers).
_TASK0B_DEPS = Task0bDeps(
    ask=ask,
    write=write,
    is_test_file=is_test_file,
    is_gen_tests_file=_is_gen_tests_file,
    strip_gen_tests_decls=_strip_gen_tests_decls,
    condense_rust_to_signatures=_condense_rust_to_signatures,
    manifest_name=MANIFEST_NAME,
    sibling_cap=_SIBLING_CAP,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    task0_model=_TASK0_MODEL,
)


def _has_gen_tests_decl(src_text: str, pass_n: int) -> bool:
    """True if src_text contains a `mod gen_tests_<pass_n>;` declaration.
    Used by the TASK 2 reattach path to detect when a sibling exists on disk
    but its declaration was dropped during regeneration of the source file."""
    return bool(re.search(rf'\bmod\s+gen_tests_{pass_n}\s*;', src_text))


def _is_gen_tests_decl_only_src(content: str) -> bool:
    """True if content consists only of gen_tests path-mod decls + whitespace + comments.

    After strip, a source file like src/lib.rs is reduced to its `#[cfg(test)]
    #[path = "..."] mod gen_tests_N;` declarations -- the rest is removed by
    extract_rust_tests. Those declaration-only files leak into TASK 1's input
    because they contain `#[cfg(test)]`, but they carry no test semantics
    (the actual tests live in the sibling files referenced by the decls).
    """
    if not content.strip():
        return True
    no_decls = _strip_gen_tests_decls(content)
    # Strip line and block comments before checking for remaining code.
    no_line_comments = re.sub(r'//[^\n]*', '', no_decls)
    no_block_comments = re.sub(r'/\*.*?\*/', '', no_line_comments, flags=re.DOTALL)
    return no_block_comments.strip() == ""


def _cluster_cfg_attr(cluster) -> str:
    """Return the cfg predicate string that gates code to this cluster.

    Each active cfg `N` becomes `any(N, feature = "N")` (FR-032): a cluster may
    be switched on by either `--cfg N` (RUSTFLAGS) or `--features N`, and this
    gate must be true under whichever form the cluster uses. Multiple flags
    combine via `all(...)`.

    The default (stable) cluster is gated by the NEGATION of every other
    cluster's flags: stable's gen_tests were only ever validated under the
    default build, and ungated they compile under nightly's cfg-set, where
    they can break that cluster's harness (observed live: nightly task0c
    aborted at 0% on stable's siblings). No other clusters → no gate.
    """
    if cluster is None:
        return ""
    if getattr(cluster, "is_default", True):
        other_flags: set[str] = set()
        for c in _all_clusters:
            if c is cluster or getattr(c, "is_default", True):
                continue
            other_flags.update(getattr(c, "active_cfg", []) or [])
        if not other_flags:
            return ""
        terms = [t for f in sorted(other_flags)
                 for t in (f, f'feature = "{f}"')]
        return "not(any(" + ", ".join(terms) + "))"
    flags = list(getattr(cluster, "active_cfg", []) or [])
    if not flags:
        return ""
    terms = [f'any({f}, feature = "{f}")' for f in flags]
    if len(terms) == 1:
        return terms[0]
    return "all(" + ", ".join(terms) + ")"


def _build_gen_tests_decl(sibling_name: str, n: "int | str", allow_line: str,
                           cluster_cfg_attr: str = "") -> str:
    """Build the one-line-ish `#[cfg(test)] ... mod gen_tests_N;` declaration.

    cluster_cfg_attr (optional): cfg predicate gating this gen_tests mod to a
    specific compiler cluster, e.g. "error_generic_member_access" for nightly.
    When provided, the declaration is wrapped in #[cfg(<attr>)] so the sibling
    file is silently excluded under any other cluster's flags. This is what
    keeps task0c's per-cluster tests from breaking the harness when the next
    cluster's pre-check runs.
    """
    lines: list[str] = []
    if cluster_cfg_attr:
        lines.append(f"#[cfg({cluster_cfg_attr})]")
    lines.append("#[cfg(test)]")
    if allow_line:
        lines.append(allow_line)
    lines.append(f'#[path = "{sibling_name}"]')
    return "\n" + "\n".join(lines) + f"\nmod gen_tests_{n};\n"


def _gen_tests_origin_cfgs(file_text: str) -> dict[int, str]:
    """Map each `mod gen_tests_<n>;` decl in `file_text` to the cluster cfg
    predicate that gates it — i.e. the `#[cfg(...)]` that is NOT `#[cfg(test)]`,
    as stamped by `_build_gen_tests_decl(cluster_cfg_attr=...)` at task0c gen
    time. Returns {pass_n: cfg_predicate}; '' when only `#[cfg(test)]` is present
    (stable/default cluster). Deterministic structural scan of the pipeline's own
    fixed decl format — not a source parse of arbitrary Rust."""
    lines = file_text.splitlines()
    out: dict[int, str] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if not (s.startswith("mod gen_tests_") and s.endswith(";")):
            continue
        try:
            n = int(s[len("mod gen_tests_"):-1])
        except ValueError:
            continue
        cfg = ""
        j = i - 1
        while j >= 0:
            a = lines[j].strip()
            if a.startswith("#["):
                if a.startswith("#[cfg(") and a.endswith(")]") and a != "#[cfg(test)]":
                    inner = a[len("#[cfg("):].rsplit(")]", 1)[0]
                    if inner != "test":
                        cfg = inner
                j -= 1
                continue
            if a == "":
                j -= 1
                continue
            break
        out[n] = cfg
    return out


def _load_gen_tests_origin(project_dir: Path) -> dict[str, str]:
    """Load the gen_tests origin-cfg manifest baked pre-strip. Keyed by sibling
    stem `"{parent_stem}.gen_tests_{n}"` → cluster cfg predicate ('' = default).
    Empty dict if absent (older runs / pre-bake)."""
    path = project_dir / ".skeleton" / "_gen_tests_origin.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}


def _propagate_or_quarantine_sibling(
    gt: Path, origin_manifest: dict, parent_stem: str, pass_n: int,
    log_fn=None, tag: str = "",
) -> bool:
    """R9.6: the origin manifest is authoritative for which gen_tests siblings are
    legitimate. Returns True if `gt` should be propagated (it is declared in a
    non-empty manifest, OR the manifest is empty/absent → legacy pre-bake run).

    If the manifest is NON-empty and this sibling is absent from it, it was never
    declared pre-strip — i.e. never coverage-validated by task0c. Resurrecting it
    under a guessed cfg gate is the latent cluster-leak class (a nightly-only orphan
    would E0658 in a stable cluster). Quarantine it to `.rs.broken` and return False.
    """
    key = f"{parent_stem}.gen_tests_{pass_n}"
    if origin_manifest and key not in origin_manifest:
        if not gt.name.endswith(".broken"):
            broken = gt.with_name(gt.name + ".broken")
            try:
                gt.rename(broken)
                if log_fn:
                    log_fn(f"  {tag} [skeleton-C] ⚠️  orphan gen_tests sibling "
                           f"'{gt.name}' absent from origin manifest (undeclared "
                           f"pre-strip) — quarantined → {broken.name}, not propagated.")
            except OSError as e:
                if log_fn:
                    log_fn(f"  {tag} [skeleton-C] orphan quarantine failed {gt.name}: {e}")
        return False
    return True


def _extract_gen_tests_body(block: str) -> str:
    """Strip the outer `mod gen_X { ... }` wrapper from an LLM-generated block and
    return the dedented body. If the shape doesn't match (e.g. LLM returned bare
    test functions), return block unchanged so caller can still use it."""
    m = re.search(
        r'(?:#\[[^\]]*\]\s*\n)*\s*(?:pub\s+)?mod\s+gen_\w+\s*\{',
        block,
    )
    if not m:
        return block.strip() + "\n"
    start = m.end()
    depth = 1
    i = start
    while i < len(block):
        c = block[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                body = block[start:i]
                lines = body.splitlines()
                non_empty = [ln for ln in lines if ln.strip()]
                if non_empty:
                    indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
                    if indent > 0:
                        body = "\n".join(
                            (ln[indent:] if len(ln) >= indent and ln[:indent].isspace() else ln)
                            for ln in lines
                        )
                return body.strip() + "\n"
        i += 1
    return block.strip() + "\n"


def _rust_delimiters_balanced(text: str) -> bool:
    """Best-effort syntactic sanity check: are `{}`, `[]`, `()` balanced in
    `text`? Respects string literals, char literals, and comments.

    Catches the most common LLM-output corruption (extra closing paren,
    missing brace) without requiring a full Rust parser. Used to validate
    gen_tests siblings before committing them, and to detect already-broken
    siblings at startup.
    """
    if not text:
        return True
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_str = False
    in_chr = False
    in_line_cmt = False
    in_blk_cmt = 0
    esc = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if esc:
            esc = False
            i += 1
            continue
        if in_line_cmt:
            if c == "\n":
                in_line_cmt = False
            i += 1
            continue
        if in_blk_cmt:
            if c == "*" and nxt == "/":
                in_blk_cmt -= 1
                i += 2
                continue
            if c == "/" and nxt == "*":
                in_blk_cmt += 1
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if in_chr:
            if c == "\\":
                esc = True
            elif c == "'":
                in_chr = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "/" and nxt == "/":
            in_line_cmt = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_blk_cmt = 1
            i += 2
            continue
        # Heuristic for char vs lifetime — skip both; lifetimes don't have
        # a matching closing `'` and char literals contain at most 1 visible
        # char or escape. Treating as opaque is good enough for balance.
        if c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack or stack[-1] != pairs[c]:
                return False
            stack.pop()
        i += 1
    return not stack and not in_str and not in_chr and not in_blk_cmt


def _write_gen_tests_pass(
    src_file: Path,
    src_text: str,
    block: str,
    allow_line: str,
    pass_n: int,
    cluster_cfg_attr: str = "",
) -> Path:
    """Write the sibling file with extracted body, and write src_file with the decl appended.

    `src_text` is the source content WITHOUT this pass's declaration (caller-managed
    revert target). `pass_n` is the 1-indexed pass number.
    `cluster_cfg_attr` (optional): cfg predicate used to scope this gen_tests
    mod to a specific compiler cluster — see _build_gen_tests_decl.
    Returns the sibling file path.

    Performs a delimiter-balance sanity check on the extracted body before
    committing. When the body has unbalanced `()`/`[]`/`{}` (a common LLM-
    corruption mode that propagates a syntax error into the test crate and
    breaks subsequent cargo invocations), the sibling is written for
    inspection but the parent src file is NOT updated with the decl —
    making the bad pass a no-op at compile time. Returns the sibling path
    regardless (caller's contract).
    """
    sibling = _gen_tests_path(src_file, pass_n)
    body = _extract_gen_tests_body(block)
    if not _rust_delimiters_balanced(body):
        # Quarantine: still write the file (for debugging) but do NOT attach
        # the decl to the parent. cargo will never see it; pipeline can
        # inspect it offline.
        quarantine = sibling.with_suffix(".rs.broken")
        quarantine.write_text(body, encoding="utf-8")
        with suppress(Exception):
            _log(f"  [gen_tests] REJECTED pass {pass_n} for {src_file.name}: "
                 f"delimiters unbalanced. Quarantined → {quarantine.name}")
        return sibling  # parent decl unchanged
    sibling.write_text(body, encoding="utf-8")
    decl = _build_gen_tests_decl(sibling.name, pass_n, allow_line,
                                  cluster_cfg_attr=cluster_cfg_attr)
    src_file.write_text(src_text.rstrip() + decl, encoding="utf-8")
    return sibling


def _quarantine_broken_gen_tests(project_dir: Path) -> int:
    """Scan all existing gen_tests siblings; rename any with unbalanced
    delimiters to `.rs.broken` and strip their `#[path] mod gen_tests_N`
    decl from the parent src file. Returns count of quarantined files.

    Runs at pipeline startup as a recovery for state inherited from
    earlier runs that committed broken gen_tests before this validation
    existed.
    """
    moved = 0
    for src in (project_dir / "src").rglob("*.gen_tests_*.rs"):
        try:
            text = src.read_text(encoding="utf-8")
        except Exception:
            continue
        if _rust_delimiters_balanced(text):
            continue
        # Identify the parent src file (foo.gen_tests_N.rs → foo.rs).
        m = re.match(r"^(.*?)\.gen_tests_(\d+)\.rs$", src.name)
        if not m:
            continue
        parent_name = m.group(1) + ".rs"
        pass_n = int(m.group(2))
        parent = src.parent / parent_name
        # Strip the decl block from the parent (best-effort).
        try:
            if parent.exists():
                ptext = parent.read_text(encoding="utf-8")
                # Match the gen_tests_N decl block: optional cfg + path
                # attribute + mod statement.
                decl_re = re.compile(
                    r"\n*(?:#\[cfg\([^)]*\)\]\s*)?"
                    r"#\s*\[\s*allow\s*\([^)]*\)\s*\][^\n]*\n*"
                    r"#\s*\[\s*path\s*=\s*\"" + re.escape(src.name)
                    + r"\"\s*\][^\n]*\n*"
                    r"(?:pub\s+)?mod\s+gen_tests_" + str(pass_n)
                    + r"\s*;\s*",
                )
                ptext_new = decl_re.sub("\n", ptext)
                if ptext_new != ptext:
                    parent.write_text(ptext_new, encoding="utf-8")
        except Exception:
            pass
        # Rename the broken file so cargo can't pick it up via stray paths.
        try:
            src.rename(src.with_suffix(".rs.broken"))
            moved += 1
        except Exception:
            try:
                src.unlink()
                moved += 1
            except Exception:
                pass
    if moved:
        _log(f"  [gen_tests] quarantined {moved} broken sibling(s) at startup.")
    return moved


def _revert_gen_tests_pass(src_file: Path, src_text: str, sibling: Path) -> None:
    """Restore src_file to src_text (no decl) and delete the sibling file.

    R9.6 atomicity: restoring `src_text` (a snapshot) can drop the decls of MORE
    than one sibling — e.g. a split/inner-retry added extra siblings since the
    snapshot. Unlinking only the named `sibling` would orphan the rest (file on
    disk, no decl). Enforce the invariant `sibling file ⟺ decl in parent`: after
    the restore, delete every sibling of this parent whose decl the restore removed.
    """
    src_file.write_text(src_text, encoding="utf-8")
    with suppress(OSError):
        sibling.unlink()
    for _n, _sib in _gen_tests_siblings(src_file):
        if not _has_gen_tests_decl(src_text, _n):
            with suppress(OSError):
                _sib.unlink()


def _strip_all_gen_tests_artifacts(src_file: Path) -> int:
    """Delete all gen_tests siblings and remove all decls from src_file.
    Returns the number of siblings deleted."""
    count = 0
    for _, sibling in _gen_tests_siblings(src_file):
        try:
            sibling.unlink()
            count += 1
        except OSError:
            pass
    if src_file.exists():
        original = src_file.read_text(encoding="utf-8")
        cleaned = _strip_gen_tests_decls(original)
        if cleaned != original:
            src_file.write_text(cleaned, encoding="utf-8")
    return count


def _extract_branch_guard(lines: list[str], line_no: int) -> str | None:
    """Walk backward from line_no to find the nearest enclosing if/match-arm/while condition.

    Returns a short string describing the condition that must be true for line_no to execute,
    or None if no guard is found within the search window.
    """
    for i in range(line_no - 2, max(-1, line_no - 30), -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        # Stop at function/impl/struct boundary — guard must be within the same function
        if re.match(r'^(?:pub\s+|async\s+|unsafe\s+)*fn\s+', stripped):
            break
        if re.match(r'^(?:impl|struct|enum|trait)\b', stripped):
            break
        # if / else if / else
        if re.match(r'^(?:\}\s*)?else\s+if\b', stripped) or re.match(r'^if\b', stripped):
            return stripped[:120]
        if re.match(r'^(?:\}\s*)?else\s*\{', stripped):
            return "else branch"
        # match arm: `Pattern => {`
        if '=>' in stripped and re.search(r'=>\s*\{?\s*$', stripped):
            return f"match arm: {stripped[:100]}"
        # while loop
        if re.match(r'^while\b', stripped):
            return stripped[:120]
    return None


def _annotate_uncovered(content: str, uncovered: set[int]) -> str:
    """Return source annotated with '!!' markers on uncovered lines, including branch guards."""
    lines = content.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        if i in uncovered:
            guard = _extract_branch_guard(lines, i)
            guard_hint = f" [guard: {guard}]" if guard else ""
            marker = f"  // !! UNCOVERED{guard_hint}"
        else:
            marker = ""
        out.append(f"{i:4d} | {line}{marker}")
    return "\n".join(out)


def _focused_annotation(content: str, uncovered: set[int], ctx: int = 4) -> str:
    """Show only uncovered lines plus `ctx` lines of surrounding context.

    Showing the full file floods the model's context window (225 lines = ~6k chars
    for argh_shared alone), leaving little room for the generated test block and
    causing truncated or hallucinated output. A focused view of just the uncovered
    regions keeps the prompt small and the types visible.
    """
    lines = content.splitlines()
    show: set[int] = set()
    for u in uncovered:
        for i in range(max(1, u - ctx), min(len(lines) + 1, u + ctx + 1)):
            show.add(i)
    result, prev = [], None
    for i in sorted(show):
        if prev is not None and i > prev + 1:
            result.append("     | ...")
        if i in uncovered:
            guard = _extract_branch_guard(lines, i)
            guard_hint = f" [guard: {guard}]" if guard else ""
            marker = f"  // !! UNCOVERED{guard_hint}"
        else:
            marker = ""
        result.append(f"{i:4d} | {lines[i - 1]}{marker}")
        prev = i
    return "\n".join(result)


def _select_relevant_siblings(
    target_rel: str,
    target_source: str,
    all_siblings: list[tuple[str, str]],
    project_dir: Path,
) -> list[tuple[str, str]]:
    """
    Python type-name overlap ranking — replaces the BL-014 LLM planner call.

    Algorithm:
    1. Extract CamelCase identifiers used in the target file (types, structs, etc.)
    2. For each sibling, count how many of those identifiers are *defined* there
       (defined = appears after struct/enum/trait/type/impl keyword)
    3. Return all siblings sorted by overlap count descending — the budget system
       decides how many to include.

    Zero LLM calls, deterministic, and empirically better for type-heavy files
    (e.g. error.rs uses Own/Mut/Ref → correctly ranks ptr.rs first).
    """
    if not all_siblings:
        return []

    used_types: set[str] = set(re.findall(r'\b([A-Z][A-Za-z0-9]+)\b', target_source))
    defined_re = re.compile(r'\b(?:struct|enum|trait|type|impl)\s+([A-Z][A-Za-z0-9]+)\b')

    def _overlap(content: str) -> int:
        defined = {m.group(1) for m in defined_re.finditer(content)}
        return len(used_types & defined)

    scored = sorted(
        ((srel, sc, _overlap(sc)) for srel, sc in all_siblings),
        key=lambda x: x[2],
        reverse=True,
    )

    top = [(srel, score) for srel, _sc, score in scored if score > 0][:4]
    _log(f"  [task0c planner] {target_rel}: ranked {len(all_siblings)} siblings by type overlap"
         f" (top: {top if top else 'none matched'})")
    return [(srel, sc) for srel, sc, _score in scored]


def _extract_uncovered_functions(content: str, uncovered: set[int], max_fns: int = 0) -> str:
    """Extract complete bodies of functions that contain uncovered lines.

    The model needs the full function body — not just a narrow annotation — to write
    a meaningful behavioral assertion. Seeing the full logic lets it know what the
    function returns and under what conditions.

    Falls back to _focused_annotation if no function bodies can be identified.
    """
    if not uncovered:
        return _focused_annotation(content, uncovered)

    code_lines = content.splitlines()
    n = len(code_lines)
    fn_pattern = re.compile(r'^\s*(?:pub\s+)?(?:async\s+)?fn\s+')
    result_blocks: list[str] = []

    i = 0
    while i < n:
        if fn_pattern.match(code_lines[i]):
            fn_start = i  # 0-indexed
            # Walk forward to find the matching closing brace
            depth = 0
            fn_end = i
            for j in range(i, min(n, i + 300)):
                depth += code_lines[j].count('{') - code_lines[j].count('}')
                fn_end = j
                if j > i and depth <= 0:
                    break
            # 1-indexed line numbers that belong to this function
            fn_line_nums = set(range(fn_start + 1, fn_end + 2))
            if fn_line_nums & uncovered:
                annotated = []
                for k in range(fn_start, fn_end + 1):
                    lineno = k + 1
                    marker = "  // !! UNCOVERED" if lineno in uncovered else ""
                    annotated.append(f"{lineno:4d} | {code_lines[k]}{marker}")
                result_blocks.append("\n".join(annotated))
            i = fn_end + 1
        else:
            i += 1

    if not result_blocks:
        return _focused_annotation(content, uncovered)

    # When many uncovered lines span many functions, cap to the first max_fns bodies.
    # A focused prompt (≤5 functions) produces better-targeted tests than a dump of
    # 10+ bodies where the model picks the easiest paths and ignores the rest.
    if max_fns and len(result_blocks) > max_fns:
        result_blocks = result_blocks[:max_fns]

    return "\n\n// --- next function ---\n\n".join(result_blocks)


def _filter_cfg_incompatible_lines(
    source_text: str,
    uncov_lines: set[int],
    active_cfgs: list[str],
) -> set[int]:
    """Return the subset of uncov_lines that are inside #[cfg(...)] blocks incompatible
    with active_cfgs — i.e. blocks that are compiled OUT in the current cluster.

    A block is incompatible when its cfg attribute contains not(X) where X is in active_cfgs.
    These lines can never be covered by tests in this cluster regardless of what the LLM writes.
    """
    if not active_cfgs or not uncov_lines:
        return set()

    active_set = set(active_cfgs)

    def _is_incompatible(cfg_expr: str) -> bool:
        return any(re.search(rf'\bnot\s*\(\s*{re.escape(pred)}\s*\)', cfg_expr) for pred in active_set)

    code_lines = source_text.splitlines()
    n = len(code_lines)
    incompatible: set[int] = set()

    i = 0
    while i < n:
        stripped = code_lines[i].strip()
        cfg_m = re.match(r'#\[cfg\((.+)\)\]\s*$', stripped)
        if cfg_m and _is_incompatible(cfg_m.group(1)):
            # Found an incompatible cfg — walk forward to find and span the block.
            depth = 0
            block_started = False
            for j in range(i, min(n, i + 500)):
                opens  = code_lines[j].count('{')
                closes = code_lines[j].count('}')
                if opens > 0:
                    block_started = True
                depth += opens - closes
                if block_started and depth <= 0:
                    # Lines i..j (1-indexed: i+1..j+1) are inside this incompatible block.
                    for lno in range(i + 1, j + 2):
                        if lno in uncov_lines:
                            incompatible.add(lno)
                    i = j
                    break
        i += 1

    return incompatible


def _filter_cfg_excluded_lines(
    source_text: str,
    uncov_lines: set[int],
    excluded_cfgs: list[str],
) -> set[int]:
    """Return the subset of uncov_lines inside #[cfg(X)] blocks where X is in excluded_cfgs.

    These lines are compiled only when X is active — in a cluster where X is excluded (e.g.
    stable when error_generic_member_access is excluded), they are never compiled and can
    never be covered regardless of what the LLM writes.

    Unlike _filter_cfg_incompatible_lines (which targets not(X) blocks for active clusters),
    this function targets direct X blocks for excluded clusters.
    """
    if not excluded_cfgs or not uncov_lines:
        return set()

    excluded_set = set(excluded_cfgs)

    def _is_excluded_block(cfg_expr: str) -> bool:
        for pred in excluded_set:
            if not re.search(rf'\b{re.escape(pred)}\b', cfg_expr):
                continue
            # Make sure it's not wrapped in not(...) — that would be a stable-compatible block
            if re.search(rf'\bnot\s*\(\s*{re.escape(pred)}\s*\)', cfg_expr):
                continue
            return True
        return False

    code_lines = source_text.splitlines()
    n = len(code_lines)
    excluded: set[int] = set()

    i = 0
    while i < n:
        stripped = code_lines[i].strip()
        cfg_m = re.match(r'#\[cfg\((.+)\)\]\s*$', stripped)
        if cfg_m and _is_excluded_block(cfg_m.group(1)):
            depth = 0
            block_started = False
            for j in range(i, min(n, i + 500)):
                opens  = code_lines[j].count('{')
                closes = code_lines[j].count('}')
                if opens > 0:
                    block_started = True
                depth += opens - closes
                if block_started and depth <= 0:
                    for lno in range(i + 1, j + 2):
                        if lno in uncov_lines:
                            excluded.add(lno)
                    i = j
                    break
        i += 1

    return excluded


def _extract_existing_tests(source_text: str) -> str:
    """Extract the existing #[cfg(test)] mod tests block from a source file.

    Showing the existing tests as an example prevents the model from inventing
    types that don't exist: it can see exactly how to construct e.g. CommandInfo
    by looking at test code that already compiles correctly.

    Only extracts `mod tests` (the original hand-written tests), NOT `mod gen_tests`
    (our generated block) — gen_tests may contain broken code that would teach the
    model to hallucinate wrong types if used as an example.
    """
    # Find `mod tests {` specifically, not `mod gen_tests`.
    # Return the full block — a truncated example ends mid-function and teaches the model
    # wrong patterns, causing compile failures in generated tests.
    for marker in ("#[cfg(test)]\nmod tests", "#[cfg(test)]\npub mod tests"):
        if marker in source_text:
            idx = source_text.index(marker)
            return source_text[idx:].strip()
    return ""


def _extract_public_symbols(content: str) -> str:
    """Extract public API from source with full signatures and field types.

    Returns a structured listing that lets the model construct types correctly
    without guessing field names, enum variants, or function parameter types.

    Parsing goes through the syn extractor — NOT regex (Hard Rule: no regex on Rust
    source). See `_public_api_from_source`.
    """
    return _public_api_from_source(content)


# ---------------------------------------------------------------------------
# TestAugmentor — language-agnostic ABC for coverage augmentation (FR-S5)
#
# Separates the Rust/Cargo/tarpaulin-specific mechanics from the orchestration
# logic in task0c_augment_module. Adding support for a new language means
# implementing a new concrete class; the orchestration loop stays unchanged.
#
# Selected by TEST_AUGMENTOR in .env:
#   TEST_AUGMENTOR=static   (default — Rust/tarpaulin)
# ---------------------------------------------------------------------------

class TestAugmentor(ABC):
    """Language-agnostic interface for per-module coverage augmentation."""

    @abstractmethod
    def lib_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        """Check that the module's library target compiles. Returns (ok, error_output)."""

    @abstractmethod
    def test_harness_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        """Check that the full test harness (lib + tests) compiles. Returns (ok, error_output)."""

    @abstractmethod
    def measure_coverage(
        self, project_dir: Path, module_name: str, module_dir: Path
    ) -> tuple[float, list, int, int]:
        """Measure test coverage. Returns (coverage_pct, files_data, covered_lines, coverable_lines)."""

    @abstractmethod
    def is_applicable(self, crate_type: str) -> bool:
        """Return False if this augmentor cannot handle the given crate type."""


class StaticRustTestAugmentor(TestAugmentor):
    """Rust-specific coverage augmentation using cargo and cargo-tarpaulin."""

    def lib_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        result = subprocess.run(
            shlex.split(f"cargo build -p {module_name} --lib"),
            cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=_cargo_env(),
        )
        return result.returncode == 0, result.stdout + result.stderr

    def test_harness_compiles(self, project_dir: Path, module_name: str) -> tuple[bool, str]:
        result = subprocess.run(
            shlex.split(f"cargo test -p {module_name} --no-run"),
            cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=_cargo_env(),
        )
        return result.returncode == 0, result.stdout + result.stderr

    def measure_coverage(
        self, project_dir: Path, module_name: str, module_dir: Path
    ) -> tuple[float, list, int, int]:
        return _measure_coverage(project_dir, module_name)

    def is_applicable(self, crate_type: str) -> bool:
        return "proc-macro" not in crate_type.lower()


def _test_augmentor_factory() -> TestAugmentor:
    backend = os.getenv("TEST_AUGMENTOR", "static").lower()
    if backend == "static":
        return StaticRustTestAugmentor()
    _log(f"  WARNING: unknown TEST_AUGMENTOR='{backend}', falling back to static.")
    return StaticRustTestAugmentor()


def _task0c_pragma_pass(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    files_data: list[dict],
    model: str,
) -> None:
    """
    Called when coverage stalls: ask the LLM to add `// # tarpaulin coverage ignore`
    to lines that are genuinely unreachable from inline #[cfg(test)] blocks
    (cfg-gated nightly code, unsafe vtable dispatch, unreachable!() arms, etc.).

    This is legitimate — production codebases routinely exclude intrinsically
    unreachable paths from coverage measurement. Tarpaulin honours the pragma and
    stops counting those lines, allowing coverage to reach 100% on the coverable
    portion.
    """
    mod_prefix = str(module_dir) + "/"

    # Build map: abs_path → set of uncovered line numbers
    uncovered_by_file: dict[Path, set[int]] = {}
    for fdata in files_data:
        abs_path = Path(fdata["abs_path"]) if "abs_path" in fdata else Path(os.sep.join(fdata["path"]))
        if not str(abs_path).startswith(mod_prefix):
            continue
        if is_test_file(abs_path, project_dir):
            continue
        if abs_path.name == "build.rs":
            continue
        lines_uncov = {
            t["line"] for t in fdata.get("traces", [])
            if t["stats"].get("Line", 1) == 0
        }
        if lines_uncov:
            uncovered_by_file[abs_path] = lines_uncov

    if not uncovered_by_file:
        return

    for abs_path, uncov_lines in uncovered_by_file.items():
        rel = str(abs_path.relative_to(project_dir))
        try:
            source = abs_path.read_text(encoding="utf-8")
        except Exception:
            continue

        annotated = _annotate_uncovered(source, uncov_lines)
        prompt = f"""The following Rust source file has lines that remain uncovered after multiple
test-generation attempts. These lines may be genuinely unreachable from inline
`#[cfg(test)]` unit tests — for example:
- Code behind `#[cfg(error_generic_member_access)]` or other non-default cfg flags
- `unsafe` functions only called via vtable dispatch (never called directly in tests)
- `unreachable!()` arms in exhaustive matches
- Lines only reachable via OS-level error conditions

Your task: add `{COVERAGE_IGNORE_PRAGMA}` to each line that is intrinsically
unreachable from a `#[cfg(test)] mod gen_tests {{ use super::*; }}` block.

Rules:
- ONLY add the pragma to lines marked `// !! UNCOVERED` that you can confirm are
  unreachable from inline unit tests. Do NOT add it to lines that could be covered
  by a test.
- Do NOT modify any existing test code or logic.
- If a line is coverable (just needs a better test), do NOT add the pragma — leave
  it for the next generation attempt.
- Output the complete corrected file.

File: {rel}
```rust
{annotated}
```

<FILE path="{rel}">
corrected content here
</FILE>
"""
        response = ask(prompt, model=model)
        files_out = extract_files(response)
        if rel in files_out:
            new_content = files_out[rel]
            # Sanity: must still compile before writing.
            tmp = abs_path.with_suffix(".pragma_tmp.rs")
            try:
                tmp.write_text(new_content, encoding="utf-8")
                tmp.rename(abs_path)
                check = subprocess.run(
                    ["cargo", "test", "--no-run", "-p", module_name],
                    cwd=project_dir, capture_output=True, text=True,
                    timeout=BUILD_TIMEOUT, env=_cargo_env(),
                )
                if check.returncode != 0:
                    _log(f"  [pragma-pass] {rel}: compile failed after pragma addition — reverting.")
                    abs_path.write_text(source, encoding="utf-8")
                else:
                    added = new_content.count(COVERAGE_IGNORE_PRAGMA) - source.count(COVERAGE_IGNORE_PRAGMA)
                    _log(f"  [pragma-pass] {rel}: added {added} coverage ignore pragma(s).")
            except Exception as e:
                _log(f"  [pragma-pass] {rel}: error — {e}. Reverting.")
                with suppress(Exception):
                    abs_path.write_text(source, encoding="utf-8")
        else:
            _log(f"  [pragma-pass] {rel}: LLM returned no output — skipping.")


def _derive_gentest_prelude(project_dir: Path) -> tuple[str, str]:
    """Derive gen_tests allow attributes and prelude imports from project source — no LLM.

    Reads lib.rs/main.rs for crate-level #![deny/forbid] lints and #![no_std].
    cargo test always links std even for no_std crates, so prelude always uses std:: paths.

    Returns (allow_line, prelude_imports_block).
    """
    # Base lints to always allow — generated test code can't control these.
    allow_lints: set[str] = {"unused", "clippy::all", "non_snake_case", "unreachable_patterns"}

    lib_rs = project_dir / "src" / "lib.rs"
    if not lib_rs.exists():
        lib_rs = project_dir / "src" / "main.rs"

    if lib_rs.exists():
        text = lib_rs.read_text(encoding="utf-8")
        # Collect all crate-level deny/forbid lints → gen_tests must allow them.
        for m in re.finditer(r'#!\[(?:deny|forbid)\(([^)]+)\)\]', text):
            for lint in m.group(1).split(","):
                allow_lints.add(lint.strip())

    allow_line = f"#[allow({', '.join(sorted(allow_lints))})]"

    # cargo test always has std available (test harness links std) even in no_std crates.
    # Pre-inject the most commonly missing items so the LLM doesn't need to guess imports.
    prelude_lines = [
        "use std::string::{String, ToString};",
        "use std::vec::Vec;",
        "use std::boxed::Box;",
        "use std::format;",
        "use std::error::Error as StdError;",
    ]
    prelude_block = "\n    ".join(prelude_lines)

    return allow_line, prelude_block


def _prelude_import_names(prelude_block: str) -> str:
    """Extract comma-separated short names from `use ...;` lines in the prelude block.

    Used in task0c prompts so the INTERNAL MODULE RULES list reflects whatever
    the project's prelude actually provides (instead of hardcoding anyhow's set).
    """
    names: list[str] = []
    for line in prelude_block.splitlines():
        line = line.strip().rstrip(";")
        if not line.startswith("use "):
            continue
        # Take the last path segment of each import; expand {A, B} groups.
        # Examples:
        #   "use std::string::{String, ToString}" → ["String", "ToString"]
        #   "use std::vec::Vec"                   → ["Vec"]
        #   "use std::error::Error as StdError"   → ["StdError"]
        path = line[len("use "):].strip()
        m = re.match(r'(.*)::\{(.+)\}$', path)
        if m:
            for raw in m.group(2).split(","):
                raw = raw.strip()
                if " as " in raw:
                    raw = raw.split(" as ", 1)[1].strip()
                if raw:
                    names.append(raw)
        else:
            last = path.split("::")[-1].strip()
            if " as " in last:
                last = last.split(" as ", 1)[1].strip()
            if last:
                names.append(last)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ", ".join(ordered)


def _load_file_constraints(project_dir: Path) -> dict[str, str]:
    """Load per-file generation constraints from project_configs/{project}.constraints.toml.

    Keys are source file basenames (e.g. "error.rs"). Values are constraint text injected
    into the generation prompt after INTERNAL MODULE RULES. Returns {} if no config exists.

    The TOML file has [stable] and [nightly] tables; the nightly table entries override
    stable when a non-default cluster is active.
    """
    import tomllib
    config_path = Path(__file__).parent / "project_configs" / f"{project_dir.name}.constraints.toml"
    if not config_path.exists():
        return {}
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as _e:
        _log(f"  WARNING: could not load file constraints from {config_path}: {_e}")
        return {}
    is_nightly = _current_cluster is not None and not _current_cluster.is_default
    result: dict[str, str] = dict(data.get("stable", {}))
    if is_nightly:
        result.update(data.get("nightly", {}))
    return result


def _task0c_retrospective(
    rel_path: str,
    existing_text: str,
    fn_bodies: str,
    old_block: str,
    module_name: str,
    attempt_history: str = "",
    uncov_lines: "set[int] | None" = None,
    unproductive_blocks: "list[str] | None" = None,
    consecutive_giveups: int = 0,
) -> str:
    """Ask LLM to classify why remaining uncovered functions can't be reached.

    Returns a structured per-function classification to inject into the next attempt
    so the model knows whether to try a different approach or skip with a pragma.

    When `unproductive_blocks` is non-empty, the classifier is told that those
    specific gen_tests blocks have already been written, compiled and run, and
    did NOT improve coverage of the targeted lines. The LLM is then asked to
    revise any earlier TESTABLE call -- if multiple concrete attempts failed
    to move the metric, the line is probably structurally unreachable in this
    test environment.
    """
    if not fn_bodies.strip():
        return ""
    # Count the distinct function names so we can require all to be classified.
    _fn_count = sum(1 for ln in fn_bodies.splitlines() if re.match(r'\s*(pub\s+)?fn\s+', ln))
    _src_snippet = existing_text[:_SRC_SNIP_CAP]
    _block_snippet = old_block[:_SRC_SNIP_CAP // 2] if old_block else "(no previous block)"
    _history_section = (
        f"Compile error history for this file (what went wrong in previous attempts):\n{attempt_history}\n\n"
    ) if attempt_history.strip() else ""
    _count_line = (
        f"There are {_fn_count} function(s) with uncovered lines. "
        f"You MUST output exactly one FUNCTION line for EVERY function — do not skip any.\n\n"
    ) if _fn_count > 0 else ""
    # Build branch-guard hints for each uncovered line so the retrospective LLM
    # knows exactly what condition guards each line — improves TESTABLE vs UNREACHABLE accuracy.
    _guard_section = ""
    if uncov_lines:
        _src_lines = existing_text.splitlines()
        _guard_hints = []
        for _lno in sorted(uncov_lines):
            g = _extract_branch_guard(_src_lines, _lno)
            if g:
                _guard_hints.append(f"  line {_lno}: guarded by `{g}`")
        if _guard_hints:
            _guard_section = "Branch conditions for uncovered lines:\n" + "\n".join(_guard_hints) + "\n\n"
    # When called with evidence of prior failed attempts, surface them and ask
    # for reclassification with that evidence in mind.
    _unproductive_section = ""
    if unproductive_blocks:
        _BLOCK_CAP = max(800, _SRC_SNIP_CAP // 4)
        _shown = []
        for i, blk in enumerate(unproductive_blocks, 1):
            _shown.append(f"--- attempt {i} (compiled, ran, did NOT improve coverage of the targeted lines) ---\n"
                          f"{blk[:_BLOCK_CAP]}")
        _unproductive_section = (
            "PRIOR FAILED ATTEMPTS — these blocks compiled and ran but did NOT increase\n"
            "coverage of the still-uncovered lines. Treat them as concrete evidence that\n"
            "the strategy used to write them does NOT work in this test environment. If\n"
            "the same target was previously classified TESTABLE, the classification was\n"
            f"wrong — revise it now.\n\n{chr(10).join(_shown)}\n\n"
        )
    # The test-generation model itself has repeatedly declared these lines unreachable. That is
    # the strongest signal there's no public-API test path — weigh it heavily toward UNREACHABLE.
    _giveup_section = ""
    if consecutive_giveups >= 2:
        _giveup_section = (
            f"STRONG SIGNAL: the test-generation model has, on {consecutive_giveups} consecutive "
            "attempts, judged these lines to have NO reachable test path (it returned an empty "
            "block citing unreachability). If you cannot point to a concrete public-API call that "
            "would execute a line, classify it UNREACHABLE — do not keep it TESTABLE.\n\n"
        )
    _env_section = _describe_test_environment()
    prompt = (
        f"You generated tests for `{rel_path}` but some lines remain uncovered.\n"
        f"{_count_line}"
        f"For each function still marked `// !! UNCOVERED` below, classify it and suggest what to try.\n\n"
        f"{_env_section}\n"
        f"{_guard_section}"
        f"{_history_section}"
        f"{_unproductive_section}"
        f"{_giveup_section}"
        f"Previous test block (already committed):\n```rust\n{_block_snippet}\n```\n\n"
        f"Still-uncovered functions:\n```rust\n{fn_bodies}\n```\n\n"
        f"Source context:\n```rust\n{_src_snippet}\n```\n\n"
        f"For each uncovered function, output exactly one line:\n"
        f"FUNCTION <name>: <category> — <one sentence reason or concrete suggestion>\n\n"
        f"Categories:\n"
        f"(a) TESTABLE — coverable with a different approach; describe the specific approach\n"
        f"(b) CFG_GATED — inside #[cfg(feature=...)] or excluded by the test environment above; untestable in this run\n"
        f"(c) UNREACHABLE — unreachable!() arm, dead branch, or intrinsically dead code; add tarpaulin ignore\n"
        f"(d) NEEDS_INTERNALS — requires private state, unsafe vtable dispatch, env vars not set in the parent process, or internal construction not accessible from inline tests\n"
    )
    result = ask(prompt, model=_TASK0C_MODEL, think=None)
    lines = [ln.strip() for ln in result.splitlines() if ln.strip().startswith("FUNCTION")]
    return "\n".join(lines) if lines else result[:600].strip()


def _parse_retrospective_categories(retrospective: str) -> dict[str, list[str]]:
    """Parse retrospective FUNCTION lines into category → [fn_names] dict.

    Handles both the abbreviated form '(c) UNREACHABLE' and the bare keyword form.
    """
    cats: dict[str, list[str]] = {
        "UNREACHABLE": [], "CFG_GATED": [], "TESTABLE": [], "NEEDS_INTERNALS": [],
    }
    for line in retrospective.splitlines():
        line = line.strip()
        if not line.startswith("FUNCTION"):
            continue
        # e.g. "FUNCTION AdhocKind::anyhow_kind: (c) UNREACHABLE — reason"
        m = re.match(r'FUNCTION\s+([^:]+(?:::[^:]+)*):\s*(?:\([a-d]\)\s*)?(\w+)', line)
        if not m:
            continue
        fn_name = m.group(1).rstrip(':')
        keyword = m.group(2).upper()
        for known in cats:
            if known in keyword:
                cats[known].append(fn_name)
                break
    return cats


def _task0c_targeted_pragma(
    project_dir: Path,
    module_name: str,
    rel_path: str,
    uncov_lines: set[int],
    pragma_functions: list[str],
    model: str,
) -> bool:
    """Add tarpaulin ignore pragmas to specific classified-unreachable functions.

    Reads the current file state (including any committed gen_tests block), asks the
    LLM to add pragmas only to the listed functions, then compile-checks before
    writing.  Returns True on success.
    """
    abs_path = project_dir / rel_path
    try:
        current_source = abs_path.read_text(encoding="utf-8")
    except Exception:
        return False

    # Strip gen_* blocks before sending to LLM — files can become very large after many
    # augmentation rounds, causing the LLM to fail to return the complete file.
    # We reattach the stripped suffix after the LLM edits only the core source.
    # gen_tests bodies now live in sibling files (foo.gen_tests_N.rs); the source
    # file only contains short `mod gen_tests_N;` path-mod declarations. Strip those
    # before sending to the LLM and reattach the exact same bytes afterwards.
    _core_source = _strip_gen_tests_decls(current_source).rstrip() + "\n"
    _gen_suffix   = current_source[len(_core_source.rstrip("\n")):]

    annotated = _annotate_uncovered(_core_source, uncov_lines)
    fn_list = "\n".join(f"- {fn}" for fn in pragma_functions)

    prompt = (
        f"The following functions in `{rel_path}` have been classified as intrinsically "
        f"unreachable from inline `#[cfg(test)]` unit tests "
        f"(compile-time dispatch markers, cfg-gated nightly code, dead branches):\n"
        f"{fn_list}\n\n"
        f"Add `{COVERAGE_IGNORE_PRAGMA}` to ONLY the lines marked `// !! UNCOVERED` "
        f"that belong to these specific functions. "
        f"Do NOT add the pragma to any other line. Do NOT change any other code.\n\n"
        f"Source file (gen_* test blocks omitted for brevity):\n```rust\n{annotated}\n```\n\n"
        f"Output the COMPLETE corrected source (without the gen_* blocks) wrapped exactly "
        f"like this — no other text:\n"
        f'<FILE path="{rel_path}">\n'
        f"(complete file content here)\n"
        f"</FILE>"
    )

    response = ask(prompt, model=model, think=None)
    files_out = extract_files(response)
    if rel_path not in files_out:
        # Fallback: extract from the largest ```rust ... ``` code block.
        code_blocks = re.findall(r'```[a-zA-Z0-9_-]*\n(.*?)```', response, re.DOTALL)
        if code_blocks:
            candidate = max(code_blocks, key=len).strip()
            if len(candidate) > 100:
                files_out = {rel_path: candidate}
                _log(f"  [targeted-pragma] {rel_path}: fell back to code-block extraction.")
    if rel_path not in files_out:
        # Last-resort: programmatically insert pragma on the exact uncovered lines.
        _lines = _core_source.splitlines(keepends=True)
        _added = 0
        for _lno in sorted(uncov_lines):
            if 1 <= _lno <= len(_lines) and COVERAGE_IGNORE_PRAGMA not in _lines[_lno - 1]:
                _lines[_lno - 1] = _lines[_lno - 1].rstrip("\n") + f"  {COVERAGE_IGNORE_PRAGMA}\n"
                _added += 1
        if _added:
            files_out = {rel_path: "".join(_lines)}
            _log(f"  [targeted-pragma] {rel_path}: LLM failed — applied {_added} pragma(s) directly.")
        else:
            _log(f"  [targeted-pragma] {rel_path}: LLM returned no output.")
            return False

    # Strip any // !! UNCOVERED annotations the LLM may have copied from the annotated
    # prompt back into the output — these are prompt-only markers and must not persist
    # in the source file.
    _raw_llm_out = files_out[rel_path]
    _cleaned_out = re.sub(r'\s*//\s*!!\s*UNCOVERED[^\n]*', '', _raw_llm_out)
    if _cleaned_out != _raw_llm_out:
        files_out[rel_path] = _cleaned_out
        _log(f"  [targeted-pragma] {rel_path}: stripped leaked // !! UNCOVERED annotations from LLM output.")

    # Ensure all uncov_lines actually have the pragma — the LLM sometimes returns the
    # file unchanged.  Apply any missing pragmas programmatically to the LLM's output.
    _out_lines = files_out[rel_path].splitlines(keepends=True)
    _missed = 0
    for _lno in sorted(uncov_lines):
        if 1 <= _lno <= len(_out_lines) and COVERAGE_IGNORE_PRAGMA not in _out_lines[_lno - 1]:
            _out_lines[_lno - 1] = _out_lines[_lno - 1].rstrip("\n") + f"  {COVERAGE_IGNORE_PRAGMA}\n"
            _missed += 1
    if _missed:
        files_out[rel_path] = "".join(_out_lines)
        _log(f"  [targeted-pragma] {rel_path}: LLM missed {_missed} pragma(s) — added programmatically.")

    # Reattach gen_* suffix so no accumulated tests are lost.
    _core_out = files_out[rel_path].rstrip("\n") + "\n"
    new_content = _core_out + _gen_suffix
    abs_path.write_text(new_content, encoding="utf-8")
    check = subprocess.run(
        ["cargo", "test", "--no-run", "-p", module_name],
        cwd=project_dir, capture_output=True, text=True,
        timeout=BUILD_TIMEOUT, env=_cargo_env(),
    )
    if check.returncode != 0:
        _log(f"  [targeted-pragma] {rel_path}: compile failed after pragma addition — reverting.")
        abs_path.write_text(current_source, encoding="utf-8")
        return False

    added = new_content.count(COVERAGE_IGNORE_PRAGMA) - current_source.count(COVERAGE_IGNORE_PRAGMA)
    _log(f"  [targeted-pragma] {rel_path}: added {added} pragma(s) for {len(pragma_functions)} classified function(s).")
    return True


def _partition_uncovered_lines(content: str, uncovered: "set[int]", n_groups: int) -> "list[set[int]]":
    """Split a file's uncovered lines into up to `n_groups` disjoint buckets,
    keeping each function's lines together.

    Used by task0c split-and-merge so N concurrent generations each own a
    distinct subset of the file's uncovered functions (additive coverage, no
    redundant work). Functions are detected the same way _extract_uncovered_
    functions does (scan for `fn`, brace-match to the closing `}`); each
    function that contains uncovered lines becomes one unit, distributed
    round-robin (largest-first) so buckets are roughly balanced. Uncovered
    lines outside any function are pooled into one extra unit. Returns only
    non-empty buckets (so the result length may be < n_groups, e.g. when the
    file has fewer functions than groups).
    """
    n_groups = max(1, n_groups)
    if not uncovered or n_groups == 1:
        return [set(uncovered)] if uncovered else []

    code_lines = content.splitlines()
    n = len(code_lines)
    fn_pattern = re.compile(r'^\s*(?:pub\s+)?(?:async\s+)?fn\s+')
    remaining = set(uncovered)
    units: list[set[int]] = []  # one per function that owns uncovered lines

    i = 0
    while i < n:
        if fn_pattern.match(code_lines[i]):
            depth = 0
            fn_end = i
            for j in range(i, min(n, i + 300)):
                depth += code_lines[j].count('{') - code_lines[j].count('}')
                fn_end = j
                if j > i and depth <= 0:
                    break
            fn_lines = set(range(i + 1, fn_end + 2)) & remaining  # 1-indexed
            if fn_lines:
                units.append(fn_lines)
                remaining -= fn_lines
            i = fn_end + 1
        else:
            i += 1
    if remaining:  # uncovered lines outside any detected function
        units.append(remaining)

    if len(units) <= 1:
        return units if units else [set(uncovered)]

    # Distribute units largest-first into n_groups balanced buckets.
    buckets: list[set[int]] = [set() for _ in range(min(n_groups, len(units)))]
    for unit in sorted(units, key=len, reverse=True):
        smallest = min(buckets, key=len)
        smallest |= unit
    return [b for b in buckets if b]


def task0c_augment_module(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    crate_type: str = "",
    existing_test_files: set | None = None,
    project_context: str = "",
    coverage_strategy: str = "",
) -> tuple[float, int, bool, str]:
    # slice 5b: extracted to application.tasks.task0c; thin shim binding deps.
    return _task0c_impl(
        project_dir, module_name, module_dir, _TASK0C_DEPS,
        crate_type=crate_type, existing_test_files=existing_test_files,
        project_context=project_context, coverage_strategy=coverage_strategy,
    )


# ---------------------------------------------------------------------------
# Coverage sentinel helpers
# ---------------------------------------------------------------------------

def _sentinel_module_coverage(sentinel_path: Path) -> dict[str, float]:
    """
    Parse per-module coverage from .coverage_augmented.
    Returns {module_name: pct} for all non-'overall' lines.
    """
    result: dict[str, float] = {}
    if not sentinel_path.exists():
        return result
    try:
        text = sentinel_path.read_text()
    except Exception:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("overall:"):
            continue
        m = re.match(r"^([\w\-]+):\s*([\d.]+)%", line)
        if m:
            result[m.group(1)] = float(m.group(2))
    return result


def _sentinel_coverage(sentinel_path: Path) -> float:
    """
    Read the overall coverage percentage from .coverage_augmented.
    Returns the overall value if present, else the average of per-module lines.
    Returns 0.0 if the file is missing or unparseable.
    """
    if not sentinel_path.exists():
        return 0.0
    try:
        text = sentinel_path.read_text()
    except Exception:
        return 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("overall:"):
            m = re.search(r"([\d.]+)%", line)
            if m:
                return float(m.group(1))
    # Legacy format without overall line: average the per-module values.
    values = []
    for line in text.splitlines():
        m = re.search(r"([\d.]+)%", line)
        if m:
            values.append(float(m.group(1)))
    return sum(values) / len(values) if values else 0.0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def restart_from_augmented(root: Path, project_dir: Path) -> None:
    """Restore workspace to the task0c augmentation commit and clear only
    iteration state (strip / task1+ artifacts). Task0a/0b/0c outputs are kept.

    Single-module workspaces only. Errors loudly if the augmentation tag is
    missing or the workspace has >1 module.
    """
    # Find the single augmentation tag — multi-module is out of scope for v1.
    try:
        tags = subprocess.run(
            ["git", "tag", "-l", "task0c-augmented-*"],
            cwd=project_dir, capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git tag listing failed: {e}") from e
    tags = [t for t in tags if t]
    if not tags:
        raise RuntimeError(
            "No task0c-augmented-* tag in the workspace. "
            "There is no augmented restore point — use --clean for a full wipe instead."
        )
    if len(tags) > 1:
        raise RuntimeError(
            f"Multi-module restore not supported in this version. Found tags: {tags}. "
            "Use --clean for a full wipe."
        )
    restore_tag = tags[0]
    module_name = restore_tag.split("-")[-1]

    _log(f"  [restart-from-augmented] resetting workspace to {restore_tag}")
    subprocess.run(["git", "reset", "--hard", restore_tag],
                   cwd=project_dir, check=True, capture_output=True)

    # Wipe iteration state — strip sentinels onward. Keep task0a/0b/0c outputs.
    iteration_paths: list[Path] = [
        project_dir / MODULE_STRIPPED_DIR,         # strip sentinel
        project_dir / f"functional_requirements_{module_name}.md",
        project_dir / FAILURE_LOG_NAME,
        project_dir / ".task1_partials",
    ]
    iteration_paths += list(project_dir.glob(".generated_files_*.txt"))
    iteration_paths += list(project_dir.glob("tests_output_*.txt"))
    for p in iteration_paths:
        if p.is_dir():
            shutil.rmtree(p)
            _log(f"  Deleted {p.relative_to(root) if p.is_relative_to(root) else p}/")
        elif p.exists():
            p.unlink()
            _log(f"  Deleted {p.relative_to(root) if p.is_relative_to(root) else p}")

    # Pipeline state file: clear strip/task1+ entries so they re-run; keep
    # task0a/0b/0c entries so those tasks skip via sentinel check.
    state = _read_state(project_dir)
    iteration_prefixes = ("strip/", "task1/", "task2-4/", "task2/", "task3/", "task4/")
    state["phases"] = {
        k: v for k, v in state["phases"].items()
        if not k.startswith(iteration_prefixes)
    }
    state["current"] = "restart-from-augmented"
    _write_state(project_dir, state)
    _log("  Cleared iteration entries from pipeline_state.md")
    _log(f"  Workspace restored to {restore_tag}; task0a/0b/0c kept, strip + task1+ will re-run.")


def clean_state(root: Path, project_dir: Path, manifest_path: Path,
                genesis_path: Path, functional_req_path: Path):
    """Delete all pipeline-generated state so the next run starts fresh."""
    # Generated source files tracked in the manifest
    if manifest_path.exists():
        delete_source_files(project_dir, manifest_path)

    # Per-iteration documents, the project profile, and synthesized context (all per-project)
    profile_path       = project_dir / PROJECT_PROFILE_NAME
    context_path       = project_dir / PROJECT_CONTEXT_NAME
    hash_path          = project_dir / PROJECT_INPUTS_HASH_NAME
    strategy_path      = project_dir / COVERAGE_STRATEGY_NAME
    strategy_hash_path = project_dir / COVERAGE_STRATEGY_HASH_NAME
    state_path         = _state_path(project_dir)
    for p in [functional_req_path, genesis_path, profile_path, context_path, hash_path,
              strategy_path, strategy_hash_path, state_path]:
        if p.exists():
            p.unlink()
            _log(f"  Deleted {p.name}")

    # Test output files
    for p in project_dir.glob("tests_output_*.txt"):
        p.unlink()
        _log(f"  Deleted {p.name}")

    # Chunk partials from interrupted chunked task1
    partials_dir = project_dir / ".task1_partials"
    if partials_dir.exists():
        for p in partials_dir.glob("*.txt"):
            p.unlink()
        with suppress(OSError):
            partials_dir.rmdir()
        _log("  Deleted .task1_partials/")

    # Failure log
    failure_log_path = project_dir / FAILURE_LOG_NAME
    if failure_log_path.exists():
        failure_log_path.unlink()
        _log(f"  Deleted {FAILURE_LOG_NAME}")

    # Chunk cache (persistent across iterations — only wiped on explicit --clean)
    cache_dir = root / CHUNK_CACHE_DIR
    if cache_dir.exists():
        deleted = 0
        for p in cache_dir.glob("*.txt"):
            p.unlink()
            deleted += 1
        with suppress(OSError):
            cache_dir.rmdir()
        _log(f"  Deleted {CHUNK_CACHE_DIR}/ ({deleted} cached chunks)")

    # Cluster augmentation sentinels — must be wiped so task0c reruns on --clean
    cluster_aug_dir = project_dir / CLUSTER_AUGMENTED_DIR
    if cluster_aug_dir.exists():
        shutil.rmtree(cluster_aug_dir)
        _log(f"  Deleted {CLUSTER_AUGMENTED_DIR}/")

    # Module augmentation and strip sentinels
    for _sentinel_dir_name in (MODULE_AUGMENTED_DIR, MODULE_STRIPPED_DIR):
        _sentinel_dir = project_dir / _sentinel_dir_name
        if _sentinel_dir.exists():
            shutil.rmtree(_sentinel_dir)
            _log(f"  Deleted {_sentinel_dir_name}/")

    # Locked module backups (bottom-up integration)
    locked_dir = project_dir / LOCKED_MODULES_DIR
    if locked_dir.exists():
        shutil.rmtree(locked_dir)
        _log(f"  Deleted {LOCKED_MODULES_DIR}/")

    # Per-module generated-file manifests
    for p in project_dir.glob(".generated_files_*.txt"):
        p.unlink()
        _log(f"  Deleted {p.name}")

    # Stale per-run coverage artifacts in /tmp/ — these live outside the
    # workspace dir, so they survive every wipe of tmp/{project}/. Without
    # cleaning them, the monitor reads stale per-file coverage from
    # /tmp/grcov_filecache_*.json (and the lcov / json outputs themselves)
    # and shows misleading carry-over numbers until the next full coverage
    # measurement overwrites them.
    import glob as _glob
    _tmp_patterns = [
        "/tmp/grcov_*.json",
        "/tmp/grcov_filecache_*.json",
        "/tmp/llvmcov_*.lcov",
        "/tmp/llvmcov_*.lcov.json",
    ]
    _tmp_deleted = 0
    for _pat in _tmp_patterns:
        for _p in _glob.glob(_pat):
            try:
                os.unlink(_p)
                _tmp_deleted += 1
            except OSError:
                pass
    for _tarp in _glob.glob("/tmp/tarp_*"):
        try:
            if os.path.isdir(_tarp):
                shutil.rmtree(_tarp)
                _tmp_deleted += 1
        except OSError:
            pass
    if _tmp_deleted:
        _log(f"  Deleted {_tmp_deleted} stale coverage artifact(s) from /tmp/")

    # Strip all gen_tests artifacts from source files: delete sibling files and
    # remove path-mod declarations. Also clean up legacy inline blocks (from
    # pre-sibling-file design) so an old workspace can be salvaged with --clean.
    _legacy_inline_re = re.compile(
        r'\n+(?:// GRCOV_EXCL_START\n)?#\[cfg\(test\)\](?:\n#\[[^\]]*\])*\n(?:pub )?mod gen_\w+\s*\{.*',
        re.DOTALL,
    )
    stripped_files = stripped_siblings = 0
    # Delete sibling files first so the parent loop doesn't re-read them.
    for sibling in sorted(project_dir.rglob("*.gen_tests_*.rs")):
        if re.search(r'\.gen_tests_\d+\.rs$', sibling.name):
            try:
                sibling.unlink()
                stripped_siblings += 1
                _log(f"  Deleted sibling {sibling.relative_to(project_dir)}")
            except OSError:
                pass
    for src in sorted(project_dir.rglob("*.rs")):
        try:
            raw = src.read_text(encoding="utf-8")
            cleaned = _strip_gen_tests_decls(raw)
            cleaned = _legacy_inline_re.sub("", cleaned).rstrip() + "\n"
            if cleaned != raw:
                src.write_text(cleaned, encoding="utf-8")
                _log(f"  Stripped gen_tests artifacts from {src.relative_to(project_dir)}")
                stripped_files += 1
        except Exception:
            pass
    if stripped_files or stripped_siblings:
        _log(f"  Removed {stripped_siblings} sibling file(s); cleaned {stripped_files} source file(s).")

    _log("Clean complete. Starting from scratch.")


# ---------------------------------------------------------------------------
# Per-module pipeline helpers
# ---------------------------------------------------------------------------

def _module_augmented_path(project_dir: Path, module_name: str) -> Path:
    return project_dir / MODULE_AUGMENTED_DIR / module_name


def is_module_augmented(project_dir: Path, module_name: str) -> bool:
    return _module_augmented_path(project_dir, module_name).exists()


def mark_module_augmented(project_dir: Path, module_name: str, coverage: float) -> None:
    p = _module_augmented_path(project_dir, module_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{coverage:.1f}%\n")


def strip_module_source(project_dir: Path, module_dir: Path) -> None:
    """
    Strip implementation source from a single module directory, leaving test
    content intact. Equivalent to running strip_to_tests on just this subtree.
    Other modules on disk are not touched — their real source remains available.

    Saves the original source file list to .original_structure/<module>/files.txt
    BEFORE stripping, so TASK 2 can use it as a deterministic file plan instead
    of asking the LLM to guess what files to create.
    """
    lang = detect_language(project_dir)
    if lang == "rust":
        # Save original file list before any files are deleted.
        _save_original_structure(project_dir, module_dir)
        _log(f"  Stripping source from {module_dir.relative_to(project_dir)}/...")
        process_rust(module_dir, dry_run=False)
    else:
        _log(f"  WARNING: strip not supported for language '{lang}' — skipping.")


ORIGINAL_STRUCTURE_DIR = ".original_structure"


def _save_original_structure(project_dir: Path, module_dir: Path) -> None:
    """Save list of .rs source files in module_dir before stripping."""
    module_rel = str(module_dir.relative_to(project_dir))
    out_dir = project_dir / ORIGINAL_STRUCTURE_DIR
    out_dir.mkdir(exist_ok=True)
    files = sorted(
        str(p.relative_to(project_dir))
        for p in module_dir.rglob("*.rs")
        if not any(part in SKIP_DIR_NAMES for part in p.relative_to(project_dir).parts)
        and p.is_file()
    )
    (out_dir / f"{module_rel}.txt").write_text("\n".join(files))
    _log(f"  Saved original structure: {len(files)} source file(s) recorded.")


def _api_surface_path(project_dir: Path, module_name: str) -> Path:
    """Path where the pre-strip API surface for a module is saved."""
    return project_dir / ORIGINAL_STRUCTURE_DIR / f"{module_name}.api.md"


def _capture_api_surface(project_dir: Path, module_dir: Path, module_name: str) -> Path | None:
    """Snapshot the module's public API surface BEFORE strip.

    Walks every non-test .rs file in module_dir, runs _condense_rust_to_signatures
    on each (signatures + type definitions, function bodies elided), concatenates
    them under <FILE path="..."> markers, and writes to
    .original_structure/<module>.api.md. The directory survives strip (it lives at
    the project root, not under src/), so TASK 1 can read it after strip as a
    "structural ground truth" alongside the test-derived behavioral requirements.

    Returns the file path on success, or None if nothing could be captured.
    """
    out_path = _api_surface_path(project_dir, module_name)
    out_path.parent.mkdir(exist_ok=True)

    parts: list[str] = [
        f"# API surface — {module_name}",
        "",
        "Public-API skeleton captured before strip. Signatures and type definitions",
        "only (function bodies are elided as `{ /* ... */ }`). This is structural",
        "ground truth for TASK 1: any item NOT listed here did not exist in the",
        "original source. The behavioral semantics are still derived from tests —",
        "this document only defines the shape of the public API.",
        "",
    ]
    file_count = 0
    # Only walk the crate's src/ tree. examples/, benches/, tests/ are NOT
    # part of the public API — they are consumers of it. Including them in
    # the api_surface would tell TASK 1 to preserve signatures that don't
    # actually exist in the implementation.
    src_root = module_dir / "src"
    walk_root = src_root if src_root.is_dir() else module_dir
    for src_file in sorted(walk_root.rglob("*.rs")):
        rel = src_file.relative_to(project_dir)
        if any(part in SKIP_DIR_NAMES for part in rel.parts):
            continue
        if is_test_file(src_file, project_dir):
            continue
        # Defence in depth: skip example/bench/integration-test directories
        # if walk_root somehow ended up at the module root.
        if any(part in {"examples", "benches", "tests"} for part in rel.parts):
            continue
        try:
            text = src_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Drop any gen_tests declarations the pipeline added — they aren't part
        # of the original API surface.
        text = _strip_gen_tests_decls(text)
        condensed = _condense_rust_to_signatures(text, filename=src_file.name).rstrip()
        if not condensed:
            continue
        parts.append(f'<FILE path="{rel}">')
        parts.append(condensed)
        parts.append("</FILE>")
        parts.append("")
        file_count += 1

    if file_count == 0:
        return None
    out_path.write_text("\n".join(parts), encoding="utf-8")
    _log(f"  Captured API surface: {file_count} file(s) → "
         f"{out_path.relative_to(project_dir)} ({out_path.stat().st_size:,} chars).")
    return out_path


def load_api_surface(project_dir: Path, module_name: str) -> str:
    """Load the saved API surface for a module. Returns '' if not present."""
    p = _api_surface_path(project_dir, module_name)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


# slice 5b: deps for the extracted TASK 1 (defined after load_api_surface, its latest dep).
_TASK1_DEPS = Task1Deps(
    load_api_surface=load_api_surface,
    read=read,
    ask=ask,
    write_fr_preserving_notes=_write_fr_preserving_notes,
    synthesizer_factory=_task1_synthesizer_factory,
    filter_proc_macro_test_files=_filter_proc_macro_test_files,
    proc_macro_fallback_requirements=_proc_macro_fallback_requirements,
    format_api_surface_section=_format_api_surface_section,
    file_format_instruction=FILE_FORMAT_INSTRUCTION,
    single_call_char_limit=SINGLE_CALL_CHAR_LIMIT,
    profile_cap=_PROFILE_CAP,
    task1_model=_TASK1_MODEL,
)


def load_original_structure(project_dir: Path, module_name: str) -> list[str]:
    """Load the saved pre-strip file list for a module. Returns [] if not found.

    Matches by .txt filename stem (exact), not by content prefix. The stem-based match
    is unambiguous: 'argh' never collides with 'argh_shared', regardless of iteration order.
    """
    out_dir = project_dir / ORIGINAL_STRUCTURE_DIR
    target = module_name.replace("-", "_")
    for candidate in out_dir.glob("*.txt"):
        stem = candidate.stem.replace("-", "_")
        if stem == target:
            content = candidate.read_text()
            return [ln.strip() for ln in content.splitlines() if ln.strip()]
    return []


# slice 5b foundation: relocated to pipeline_core.domain.test_results (imported at top).


def _measure_baseline_coverage(project_dir: Path, modules: list[str],
                                dep_graph) -> dict[str, float]:
    """
    Run tarpaulin on each module once to measure baseline coverage.
    Used to determine per-module processing order (descending coverage = richer spec first).
    Returns {module_name: pct}. Falls back to 0.0 if tarpaulin unavailable.
    """
    result: dict[str, float] = {}
    if not TARPAULIN_BIN.exists() or len(modules) <= 1:
        return result
    _log("\nMeasuring baseline coverage to determine processing order...")
    for mod_name in modules:
        crate_type = dep_graph.crate_type(mod_name)
        if "proc-macro" in crate_type.lower():
            cov = 100.0
        else:
            cov, _, _, _ = _run_tarpaulin(project_dir, mod_name)
        _log(f"  {mod_name}: {cov:.1f}%")
        result[mod_name] = cov
    return result


def _extract_params_b(model_info) -> float | None:
    """Return parameter count in billions from HF ModelInfo, or None if unknown."""
    st = getattr(model_info, "safetensors", None)
    if st:
        total = getattr(st, "total", None)
        if isinstance(total, (int, float)) and total > 1e8:
            return total / 1e9
        pc = getattr(st, "parameter_count", None)
        if isinstance(pc, dict):
            s = sum(pc.values())
            if s > 1e8:
                return s / 1e9
    m = re.search(r'[/_\-](\d+(?:\.\d+)?)[Bb](?:[/_\-]|$)', model_info.id)
    if m:
        return float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)[Bb]$', model_info.id)
    if m:
        return float(m.group(1))
    return None


# Top coding models available via Ollama (ollama pull <name>), ranked by quality.
# Params are approximate; VRAM is estimated at Q4_K_M quantization.
_OLLAMA_MODELS = [
    ("qwen3:72b",            72, "Best coding+reasoning 2026"),
    ("qwen2.5-coder:72b",    72, "Code-specialized, excellent"),
    ("deepseek-r1:70b",      70, "Strong reasoning for code"),
    ("llama3.3:70b",         70, "Solid general + code"),
    ("glm4.7:latest",        20, "89% LiveCodeBench, top score"),
    ("qwen3:35b-a3b",        35, "73% SWE-bench, MoE efficient"),
    ("qwen3:32b",            32, "Best dense 32B available"),
    ("qwen2.5-coder:32b",    32, "Proven, strong baseline"),
    ("deepseek-coder-v2:16b",16, "Fast, good quality"),
    ("gemma4:27b",           27, "Google, strong code"),
    ("qwen3:14b",            14, "81.1 MMLU, punches above weight"),
    ("llama4:scout-17b",     17, "Meta, long context"),
    ("deepseek-r1:32b",      32, "Reasoning distill, good code"),
    ("phi4:14b",             14, "Microsoft, efficient"),
    ("qwen3:8b",              8, "Best small model for code"),
    ("mistral-small3.1:24b", 24, "Fast, strong instruction following"),
    ("qwen2.5-coder:7b",      7, "Lightweight fallback"),
]


def _fit_label(vram_4bit: float, vram_gb: float) -> str:
    if vram_4bit <= vram_gb:
        return "✓ full"
    elif vram_4bit <= vram_gb * 1.35:
        return "~ RAM"
    return "✗"


def _print_model_table(rows: list[dict], title: str) -> None:
    print(f"\n{title}\n")
    hdr = f"{'#':>3}  {'Model':<45} {'Params':>7}  {'4-bit':>6}  {'Fits':>7}"
    extra = "  Notes" if "notes" in rows[0] else f"  {'DL/mo':>9}  {'Likes':>6}"
    print(hdr + extra)
    print("─" * (len(hdr) + len(extra)))
    for i, r in enumerate(rows, 1):
        label = r["id"]
        if len(label) > 45:
            label = "…" + label[-44:]
        line = f"{i:>3}  {label:<45} {r['params']:>6.1f}B  {r['vram_4bit']:>5.1f}G  {r['fit']:>7}"
        if "notes" in r:
            line += f"  {r['notes']}"
        else:
            line += f"  {r['downloads']:>9,}  {r['likes']:>6,}"
        print(line)


def cmd_best_models(vram_gb: float = 32.0) -> None:
    """Print best models from both HuggingFace and Ollama, annotated for VRAM budget."""
    _legend = (
        "\nLegend:  ✓ full = fits entirely in GPU VRAM\n"
        "         ~ RAM  = fits with partial CPU-RAM offload (~20–35% slower)\n"
        "         ✗      = exceeds VRAM + 35% RAM headroom"
    )

    # --- Ollama section (curated list, no network needed) ---
    ollama_rows = []
    for name, params, notes in _OLLAMA_MODELS:
        vram_4bit = round(params * 0.55 + 2.5, 1)
        ollama_rows.append({
            "id": name,
            "params": float(params),
            "vram_4bit": vram_4bit,
            "fit": _fit_label(vram_4bit, vram_gb),
            "notes": notes,
        })
    order = {"✓ full": 0, "~ RAM": 1, "✗": 2}
    def _ollama_sort_key(r: dict) -> tuple:
        fit: str = str(r["fit"])
        rid: str = str(r["id"])
        rparams: int = int(float(str(r["params"])))
        rnotes: str = str(r["notes"])
        short_id = rid.rsplit(":", 1)[0] if rid.count(":") > 1 else rid
        entry = (short_id, rparams, rnotes)
        orig_idx = _OLLAMA_MODELS.index(entry) if entry in _OLLAMA_MODELS else 99
        return (order.get(fit, 99), orig_idx)
    ollama_rows.sort(key=_ollama_sort_key)

    # Check which are locally installed
    installed = set()
    try:
        import json as _json
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as resp:
            data = _json.loads(resp.read())
        installed = {m["name"].split(":")[0] for m in data.get("models", [])}
    except Exception:
        pass

    for r in ollama_rows:
        base = str(r["id"]).split(":")[0]
        if base in installed:
            r["notes"] = "✔ installed  " + str(r["notes"])

    _print_model_table(ollama_rows, f"── Ollama  (ollama pull <name>)  |  VRAM budget: {vram_gb:.0f} GB")
    print("  Benchmark scores: https://livecodebench.github.io/leaderboard.html")

    # --- HuggingFace section ---
    try:
        from huggingface_hub import list_models
    except ImportError:
        print("\nERROR: huggingface_hub not installed — skipping HF section.  Run: pip install huggingface_hub")
        print(_legend)
        return

    print("\nFetching HuggingFace Hub top models …")
    raw = list(list_models(
        pipeline_tag="text-generation",
        sort="likes",
        limit=1000,
        expand=["safetensors", "downloads", "likes"],
    ))

    hf_rows = []
    for m in raw:
        hf_params = _extract_params_b(m)
        if hf_params is None or hf_params < 1:
            continue
        vram_4bit = round(hf_params * 0.55 + 2.5, 1)
        hf_rows.append({
            "id": m.id,
            "params": hf_params,
            "vram_4bit": vram_4bit,
            "fit": _fit_label(vram_4bit, vram_gb),
            "downloads": m.downloads or 0,
            "likes": m.likes or 0,
        })

    hf_rows.sort(key=lambda r: (order.get(str(r["fit"]), 99), -int(r["likes"])))

    seen = {}
    deduped = []
    for r in hf_rows:
        base = re.sub(r'[-_](AWQ|GPTQ|GGUF|bnb|4bit|8bit|fp16|bf16|Int4|Int8|MLX|2507).*$', '', str(r["id"]), flags=re.IGNORECASE)
        if base not in seen:
            seen[base] = True
            deduped.append(r)

    _print_model_table(deduped[:30], f"── HuggingFace  (HF_MODEL=<id>)  |  VRAM budget: {vram_gb:.0f} GB")
    print("  Benchmark scores: https://huggingface.co/spaces/bigcode/bigcodebench-leaderboard")

    print(_legend)


def _ensure_vllm_ready(root: Path) -> None:
    """If BACKEND=vllm, auto-start docker_vllm.sh if the server is not responding,
    then wait until the inference endpoint accepts requests (health passes before
    CUDA graph compilation finishes, so an inference probe is required)."""
    import urllib.error
    import urllib.request

    vllm_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
    port = vllm_url.split(":")[-1].split("/")[0]
    health_url = f"http://localhost:{port}/health"
    infer_url  = f"http://localhost:{port}/v1/chat/completions"
    model      = os.getenv("VLLM_MODEL_NAME", "")

    def _health_ok() -> bool:
        try:
            urllib.request.urlopen(health_url, timeout=5)
            return True
        except Exception:
            return False

    if not _health_ok():
        _log(f"  vLLM not responding at {health_url} — launching docker_vllm.sh ...")
        script = root / "docker_vllm.sh"
        result = subprocess.run(["bash", str(script)], cwd=str(root))
        if result.returncode != 0:
            _log(f"  [warn] docker_vllm.sh exited {result.returncode} — proceeding anyway")
    else:
        _log(f"  vLLM health OK at {health_url} — checking inference readiness ...")

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    for i in range(1, 241):
        try:
            req = urllib.request.Request(
                infer_url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
            elapsed = i * 5
            _log(f"  vLLM inference ready ({elapsed // 60}m {elapsed % 60}s).")
            return
        except Exception:
            pass
        time.sleep(5)
        if i % 12 == 0:
            _log(f"  ... vLLM inference not ready yet ({i * 5 // 60}m elapsed)")
    _log("  [warn] vLLM inference not ready after 20 min — proceeding anyway")



# slice 5b: deps for the extracted TASK 0c engine (defined before main(), after every
# collaborator). subprocess/git injected as the module; _current_cluster via a call-time
# getter (mutable global); coverage provider as a value.
_TASK0C_DEPS = Task0cDeps(
    BUILD_TIMEOUT=BUILD_TIMEOUT, COVERAGE_TARGET=COVERAGE_AUGMENTATION_TARGET, TASK0C_PROMPT_CHAR_CAP=TASK0C_PROMPT_CHAR_CAP, FIX_ERR_CAP=_FIX_ERR_CAP, SIBLING_CAP=_SIBLING_CAP, SRC_SNIP_CAP=_SRC_SNIP_CAP, TASK0C_FIX_THINKING_DIRECTIVE=_TASK0C_FIX_THINKING_DIRECTIVE, TASK0C_MODEL=_TASK0C_MODEL, TASK0C_THINKING_DIRECTIVE=_TASK0C_THINKING_DIRECTIVE, WORKING_CAP=_WORKING_CAP, annotate_uncovered=_annotate_uncovered, block_is_prose=_block_is_prose, build_gen_tests_decl=_build_gen_tests_decl, build_task0c_prompt_sections=_build_task0c_prompt_sections, cargo_env=_cargo_env, cluster_cfg_attr=_cluster_cfg_attr, condense_rust_to_signatures=_condense_rust_to_signatures, count_test_attrs=_count_test_attrs, derive_gentest_prelude=_derive_gentest_prelude, describe_test_environment=_describe_test_environment, error_signatures_from_output=_error_signatures_from_output, extract_branch_guard=_extract_branch_guard, extract_gen_tests_body=_extract_gen_tests_body, extract_public_symbols=_extract_public_symbols, extract_uncovered_functions=_extract_uncovered_functions, filter_cfg_excluded_lines=_filter_cfg_excluded_lines, filter_cfg_incompatible_lines=_filter_cfg_incompatible_lines, flags_unreachable=_flags_unreachable, gen_tests_count=_gen_tests_count, gen_tests_path=_gen_tests_path, get_task0c_history=_get_task0c_history, has_nonempty_test_body=_has_nonempty_test_body, inject_allow_before_any_mod=_inject_allow_before_any_mod, inject_allow_before_gen_mod=_inject_allow_before_gen_mod, inject_gen_prelude_imports=_inject_gen_prelude_imports, is_gen_tests_file=_is_gen_tests_file, is_structural_line=_is_structural_line, load_file_constraints=_load_file_constraints, log_task0c_attempt=_log_task0c_attempt, mark_phase=_mark_phase, match_gen_tests_sibling=_match_gen_tests_sibling, next_gen_tests_n=_next_gen_tests_n, nightly_rules_for_cluster=_nightly_rules_for_cluster, parse_attempt_number=_parse_attempt_number, parse_retrospective_categories=_parse_retrospective_categories, partition_uncovered_lines=_partition_uncovered_lines, prelude_import_names=_prelude_import_names, quick_file_coverage=_quick_file_coverage, read_all_gen_tests=_read_all_gen_tests, read_state=_read_state, revert_gen_tests_pass=_revert_gen_tests_pass, select_relevant_siblings=_select_relevant_siblings, set_current=_set_current, source_hash=_source_hash, strip_all_gen_tests_artifacts=_strip_all_gen_tests_artifacts, strip_gen_tests_decl_n=_strip_gen_tests_decl_n, strip_gen_tests_decls=_strip_gen_tests_decls, strip_trailing_file_tag=_strip_trailing_file_tag, task0c_mutation_pass=_task0c_mutation_pass, task0c_pragma_pass=_task0c_pragma_pass, task0c_retrospective=_task0c_retrospective, task0c_targeted_pragma=_task0c_targeted_pragma, test_augmentor_factory=_test_augmentor_factory, ask=ask, extract_files=extract_files, extract_test_mod=extract_test_mod, generate_fix_loop=generate_fix_loop, generate_in_parallel=generate_in_parallel, is_test_file=is_test_file, write=write,
    subprocess=subprocess, get_current_cluster=lambda: _current_cluster,
    coverage_provider=_coverage_provider,
)


def main():
    parser = argparse.ArgumentParser(description="Deterministic process pipeline.")
    parser.add_argument("--project", default="agave",
                        help="Project subfolder name (default: agave)")
    parser.add_argument("--clean", action="store_true",
                        help="Delete all generated state and start from scratch")
    parser.add_argument("--progress", action="store_true",
                        help="Compact terminal progress bar (\\r-rewriting) instead of one line per chunk")
    parser.add_argument("--nolog", action="store_true",
                        help="Print to stdout only — do not write to run.log")
    parser.add_argument("--augment-tests", action="store_true",
                        help=(
                            "Force TASK 0c even if .coverage_augmented already exists. "
                            "Normally TASK 0c runs automatically when the sentinel is absent."
                        ))
    parser.add_argument("--repo", default=None,
                        help="Git URL or local path — cloned/copied into tmp/, git-managed")
    parser.add_argument("--best-models-list", action="store_true",
                        help="Fetch and display top open-source coding models from HuggingFace")
    parser.add_argument("--vram-gb", type=float, default=32.0,
                        help="Total GPU VRAM in GB used to annotate model fit (default: 32)")
    parser.add_argument("--from-spec", action="store_true",
                        help=(
                            "Forward (spec-driven) mode. Skip task0a/0b/0c/strip/task1. "
                            "Requires functional_requirements.md at the project root. "
                            "API surface (if present at .original_structure/{name}.api.md) "
                            "is used by TASK 2. Goes directly to module iteration loop "
                            "(TASK 2 → TASK 3 → TASK 4)."
                        ))
    parser.add_argument("--restart-from-augmented", action="store_true",
                        help=(
                            "Restore the workspace to the task0c augmentation commit "
                            "(tag: task0c-augmented-{module}) and clear only iteration "
                            "state (strip sentinels, FR file, tests_output, manifest, "
                            "failure log). Keeps task0a/0b/0c outputs. Strip + task1+ "
                            "re-run from scratch with the augmented test suite. "
                            "Single-module workspaces only for now."
                        ))
    parser.add_argument("--audit", action="store_true",
                        help=(
                            "Self-audit: run the flow auditor (pipeline_core.audit — "
                            "doc-channel / floor / plateau / no-wipe / ratchet "
                            "invariants) at every iteration boundary and every "
                            "AUDIT_INTERVAL_SEC (default 900) in the background; "
                            "verdicts go to the log ([audit] lines) and to "
                            "events.jsonl (flow_audit.report) for the monitor."
                        ))
    parser.add_argument("--phase-a-only", action="store_true",
                        help=(
                            "Standalone bake: discover clusters from live source, "
                            "run the Rust skeleton extractor per cluster, save the "
                            "skeleton + leaf-first ordering under "
                            "tmp/{project}/.skeleton/{cluster}/. Refuses to run if "
                            "strip has already happened (no source to read)."
                        ))
    parser.add_argument("--phase-b-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B: run Phase A then ask "
                            "the LLM to group the project's test files into cohesive units. "
                            "Writes the validated group manifest to "
                            "tmp/{project}/.skeleton/groups.json and reports group counts. "
                            "For testing the planner before wiring up Phase C/D."
                        ))
    parser.add_argument("--phase-c-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B + C: run A and B, then "
                            "fill the skeleton group-by-group (leaf-first). Filled source "
                            "lands at tmp/{project}/.skeleton-filled/. Reports bytes filled "
                            "per group. No final compile/test run."
                        ))
    parser.add_argument("--phase-d-only", action="store_true",
                        help=(
                            "TASK 2 skeleton backend, Phase A + B + C + D: full skeleton "
                            "pipeline including the residual-cleanup pass. Reports stubs "
                            "filled vs left untouched."
                        ))
    parser.add_argument("--skeleton-backend", action="store_true",
                        help=(
                            "Invoke StaticSkeletonTask2Generator.generate() directly "
                            "(per-cluster Plan 4 flow). Reads functional_requirements.md "
                            "and genesis.md from the project, runs Phase A→D per cluster, "
                            "then merges into project_dir/src/. Standalone test path."
                        ))
    args = parser.parse_args()

    if args.best_models_list:
        cmd_best_models(vram_gb=args.vram_gb)
        sys.exit(0)

    global _git_managed
    root = Path(__file__).parent
    workspace_dir = root / WORKSPACE_DIR

    if args.repo:
        # Full git-managed workspace: clone/copy into tmp/, checkout pipeline branch.
        project_dir = setup_workspace(args.repo, workspace_dir,
                                      force_fresh=args.clean)
        _git_managed = True
    else:
        # --project: check tmp/ first, then root (backward compat).
        candidate = workspace_dir / args.project
        project_dir = candidate if candidate.exists() else root / args.project
        _git_managed = _is_git_repo(project_dir)

    genesis_path = project_dir / "genesis.md"
    functional_req_path = project_dir / "functional_requirements.md"
    manifest_path = project_dir / MANIFEST_NAME  # legacy whole-workspace manifest

    if not project_dir.exists():
        print(f"ERROR: project directory '{project_dir}' not found.")
        sys.exit(1)

    init_log(root, nolog=args.nolog, progress_mode=args.progress)
    init_events(product=project_dir.name)

    # --audit: periodic + iteration-boundary flow self-audit (read-only).
    global _AUDIT_ENABLED, _AUDIT_PROJECT_DIR
    _AUDIT_ENABLED = bool(args.audit)
    _AUDIT_PROJECT_DIR = project_dir
    if _AUDIT_ENABLED:
        _start_audit_thread()
        _log(f"  --audit: flow self-audit active (every iteration + every "
             f"{os.getenv('AUDIT_INTERVAL_SEC', '900')}s).")

    # P3.13: scan gen_tests siblings inherited from prior runs; quarantine
    # any with unbalanced delimiters and strip their decls from the parent
    # source. Prevents a broken file (an LLM-corrupted task0c output) from
    # blocking every future cargo invocation. The systemic guard at write
    # time lives in _write_gen_tests_pass.
    try:
        _quarantine_broken_gen_tests(project_dir)
    except Exception as _e:
        _log(f"  [gen_tests] startup quarantine scan failed: {_e}")

    if args.phase_a_only:
        rc = _cmd_phase_a_only(project_dir)
        sys.exit(rc)

    if args.phase_b_only:
        rc = _cmd_phase_b_only(project_dir)
        sys.exit(rc)

    if args.phase_c_only:
        rc = _cmd_phase_c_only(project_dir)
        sys.exit(rc)

    if args.phase_d_only:
        rc = _cmd_phase_d_only(project_dir)
        sys.exit(rc)

    if args.skeleton_backend:
        rc = _cmd_skeleton_backend(project_dir)
        sys.exit(rc)

    # Forward (spec-driven) mode: skip reconstruction-phase tasks. The user
    # provides functional_requirements.md (+ optional API surface) and tests;
    # the pipeline goes straight to TASK 2 → TASK 3 → TASK 4.
    _from_spec = args.from_spec
    if _from_spec:
        _fr_root = project_dir / "functional_requirements.md"
        if not _fr_root.exists():
            print(f"ERROR: --from-spec requires {_fr_root} (project-level FR file).")
            sys.exit(1)
        _log(f"FORWARD MODE: spec-driven generation. "
             f"Skipping task0a/0b/0c/strip/task1. FR={_fr_root}")

    _emit("pipeline.start", data={
        "project_dir": str(project_dir),
        "clean": args.clean,
        "augment_tests": args.augment_tests,
        "from_spec": _from_spec,
        "model": MODEL,
        "backend": SETTINGS.backend,
        "coverage_backend": COVERAGE_BACKEND,
        "context_chars": MODEL_CONTEXT_CHARS,
    })

    _ph = _git_file_hash(root, "pipeline.py")
    _mh = _git_file_hash(root, "monitor.py")
    _params = []
    if args.repo:
        _params.append(f"--repo {args.repo}")
    if args.project and not args.repo:
        _params.append(f"--project {args.project}")
    if args.clean:
        _params.append("--clean")
    if args.augment_tests:
        _params.append("--augment-tests")
    if args.progress:
        _params.append("--progress")
    if args.nolog:
        _params.append("--nolog")
    if args.from_spec:
        _params.append("--from-spec")
    if args.restart_from_augmented:
        _params.append("--restart-from-augmented")
    _log(f"  pipeline.py [{_ph}]  monitor.py [{_mh}]  params: {' '.join(_params) or '(none)'}")

    _backend = SETTINGS.backend
    if MODEL.startswith("claude") or _backend is Backend.CLAUDE:
        provider = "Claude (Anthropic API)"
    elif _backend is Backend.VLLM:
        provider = f"vLLM ({os.getenv('VLLM_MODEL_NAME', MODEL)})"
    elif _backend is Backend.SGLANG:
        provider = f"SGLang ({os.getenv('SGLANG_MODEL_NAME', MODEL)})"
    elif _backend is Backend.HUGGINGFACE:
        provider = f"HuggingFace ({os.getenv('HF_MODEL', MODEL)})"
    else:
        provider = f"Ollama ({MODEL})"
    _log(f"Using model: {MODEL}  |  Provider: {provider}")
    _log(f"Context budget: {MODEL_CONTEXT_CHARS:,} chars  "
         f"(SINGLE_CALL={SINGLE_CALL_CHAR_LIMIT:,}  FILE={FILE_CHAR_LIMIT:,}  "
         f"INTERMEDIATE={INTERMEDIATE_CHAR_LIMIT:,})")

    if _backend is Backend.VLLM:
        _ensure_vllm_ready(root)

    if args.clean and args.restart_from_augmented:
        _log("\n-- ERROR: --clean and --restart-from-augmented are mutually exclusive --")
        sys.exit(2)

    if args.clean:
        _log("\n-- --clean requested: wiping all generated state --")
        if _git_managed:
            git_clean_reset(project_dir)
        clean_state(root, project_dir, manifest_path, genesis_path, functional_req_path)
        _state_path(project_dir).unlink(missing_ok=True)

    if args.restart_from_augmented:
        _log("\n-- --restart-from-augmented: restoring workspace to task0c augmented state --")
        try:
            restart_from_augmented(root, project_dir)
        except Exception as e:
            _log(f"  [ABORT] restart-from-augmented failed: {e}")
            sys.exit(2)

    _src_hash = _source_hash(project_dir)
    _log(f"  Source hash: {_src_hash}")
    _log_state_summary(project_dir, _src_hash)

    readme_path = project_dir / "README.md"
    if readme_path.exists():
        readme_text = read(readme_path)
    else:
        _log("WARNING: README.md not found -- proceeding without it.")
        readme_text = ""

    # Cluster scan is pure static analysis (no LLM) — run first so all downstream
    # LLM tasks (discover_test_command, task0b) have cluster context available.
    global _current_cluster, _all_clusters
    _clusters = task0_cfg_scan(project_dir)
    _all_clusters = list(_clusters)
    _log(f"\nCompiler clusters identified: {len(_clusters)}")
    for _c in _clusters:
        _env_desc = ", ".join(f"{k}+={v}" for k, v in _c.env_extra.items()) if _c.env_extra else "default build"
        _log(f"  cluster/{_c.name}  — {_env_desc}")
    _mark_phase(project_dir, "cfg_scan", "done",
                detail=f"{len(_clusters)} clusters: {', '.join(c.name for c in _clusters)}",
                source_hash=_src_hash)

    if _phase_valid(project_dir, "test_cmd"):
        test_command = _read_state(project_dir)["phases"]["test_cmd"].get("detail", "")
        _log(f"Test command: {test_command} (from pipeline state)")
    else:
        test_command = discover_test_command(readme_text, project_dir=project_dir)
        _mark_phase(project_dir, "test_cmd", "done", detail=test_command, source_hash=_src_hash)

    # Normalize: strip leading ./ from the executable so we use the PATH binary.
    parts_check = _parse_cmd(test_command)
    if parts_check and parts_check[0].startswith("./"):
        parts_check[0] = parts_check[0][2:]
        test_command = " ".join(parts_check)
        _log(f"  Normalized test command: {test_command}")

    _log("Collecting test files...")
    test_files = collect_test_files(project_dir)
    existing_test_files = collect_protected_test_files(project_dir)
    _log(f"  Found {len(test_files)} test file(s) ({len(existing_test_files)} write-protected).")

    # TASK 0a: identify project structure once; profile stored per-project (not pipeline root)
    # so running project B never loads project A's cached profile.
    # FORWARD MODE: task0a/0b/0c provide reconstruction-phase context (profile/
    # behavioral summary/coverage strategy). In spec-driven mode the user supplies
    # FR+API directly so this preprocessing is unnecessary — empty strings keep
    # downstream prompts well-formed without triggering a regeneration.
    if _from_spec:
        project_profile = ""
        project_context = ""
        coverage_strategy = ""
        _log("  [from-spec] Skipping task0a/0a-context/0b (FR+API are user-provided).")
    else:
        profile_path = project_dir / PROJECT_PROFILE_NAME
        project_profile = task0_project_profile(profile_path, test_files, readme_text)

        # TASK 0a (context): synthesize behavioral project summary from test content.
        # This is the "big picture" document injected into every task — especially TASK 0c
        # which currently receives no project-level context. Works even without README.
        context_path = project_dir / PROJECT_CONTEXT_NAME
        project_context = task0_project_context(context_path, test_files, readme_text, project_profile)

        # TASK 0b (discovery): produce coverage_strategy.md — import idioms, type construction
        # patterns, feature flags, test conventions. Injected into all TASK 0c, 1, and 2 prompts.
        # Skip if either pipeline_state marks task0b done, or the artifact already
        # exists from a prior run that predates state tracking (auto-backfill).
        strategy_path = project_dir / COVERAGE_STRATEGY_NAME
        if _phase_valid(project_dir, "task0b"):
            coverage_strategy = strategy_path.read_text().strip() if strategy_path.exists() else ""
            _log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        elif strategy_path.exists() and strategy_path.read_text().strip():
            # Artifact predates pipeline_state.md tracking — backfill and skip.
            coverage_strategy = strategy_path.read_text().strip()
            _mark_phase(project_dir, "task0b", "done",
                        detail=f"{len(coverage_strategy)} chars (existing artifact)")
            _log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        else:
            coverage_strategy = task0b_discovery(
                strategy_path, project_dir, test_files, readme_text, project_profile,
                clusters=_clusters,
            )
            _commit_and_mark(
                project_dir, "task0b", "done",
                commit_message="pipeline: task0b done",
                detail=f"{len(coverage_strategy)} chars",
            )

    # TASK 0b: build dependency graph for bottom-up module integration.
    # Implementation selected by DEPENDENCY_GRAPH in .env (default: static).
    dep_graph = _dependency_graph_factory(project_dir)
    modules = dep_graph.topological_order()
    is_multi_module = len(modules) > 1

    if is_multi_module:
        _log(f"\nDependency graph: {len(modules)} module(s) in topological order:")
        for m in modules:
            deps = dep_graph.workspace_deps(m)
            dep_str = f"  deps: {', '.join(sorted(deps))}" if deps else "  leaf"
            _log(f"  [{dep_graph.crate_type(m):10s}]  {m}  ({dep_str})")
    else:
        _log("  Single module or no dependency graph detected — whole-workspace mode.")

    # -----------------------------------------------------------------------
    # Per-module pipeline.
    # For each module in coverage-descending order (topology respected):
    #   0c. Augment THIS module's tests until 100% coverage (real source present)
    #   Strip. Remove ONLY this module's source — all others remain on disk
    #   1.  Extract requirements from THIS module's tests
    #   2-4. Reconstruct loop until tests pass
    #   Lock. Only when passed > 0 and failed == 0
    # -----------------------------------------------------------------------
    if not modules:
        modules = [project_dir.name]

    locked_modules: set[str] = set()
    if is_multi_module:
        locked_modules = load_locked_modules(project_dir)
        if locked_modules:
            _log(f"\nAlready locked: {', '.join(sorted(locked_modules))}")

    failure_log_path = project_dir / FAILURE_LOG_NAME

    # Measure baseline coverage once to determine processing order.
    # Coverage-first: richest test spec → easiest reconstruction → best context for later modules.
    module_coverage = _measure_baseline_coverage(project_dir, modules, dep_graph)

    def _next_module() -> str | None:
        """Pick the unlocked module with highest coverage.
        No topological constraint: unprocessed modules keep their real source on
        disk, so compilation always works regardless of processing order.
        """
        candidates = [
            (module_coverage.get(m, 0.0), m)
            for m in modules if m not in locked_modules
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    if is_multi_module and module_coverage:
        _log("\nProcessing order (coverage-descending, topology respected):")
        for m in modules:
            _log(f"  {m}: {module_coverage.get(m, 0.0):.1f}%")

    # -----------------------------------------------------------------------
    # TASK 0c — compiler cluster phase
    # _clusters and _current_cluster already set above (moved before task0b so
    # all LLM tasks receive cluster context). Loop below runs one pass per cluster.
    # -----------------------------------------------------------------------

    if _from_spec:
        _log("  [from-spec] Skipping task0c cluster loop (no test augmentation needed).")
        _clusters = []  # ensure the loop below is a no-op
    for _cluster in _clusters:
        _current_cluster = _cluster
        _cluster_t0 = time.monotonic()
        _log(f"\n{'━' * 60}")
        _log(f"  Starting compiler cluster: {_cluster.name}")
        _ceiling_pct, _ceiling_excl, _ceiling_total = _estimate_cluster_ceiling(project_dir, _cluster)
        _emit("cluster.start", cluster=_cluster.name,
              data={"ceiling_pct": _ceiling_pct,
                    "ceiling_excl": _ceiling_excl,
                    "ceiling_total": _ceiling_total,
                    "active_cfg": list(_cluster.active_cfg or []),
                    "excluded_cfg": list(_cluster.excluded_cfg or []),
                    "gated_files": sorted(_cluster.gated_files or [])})
        if _ceiling_excl > 0:
            _log(f"  Cluster ceiling estimate: ~{_ceiling_pct:.1f}% "
                 f"({_ceiling_excl} of {_ceiling_total} source lines inside excluded-cfg blocks — "
                 f"unreachable in this cluster)")
        _log(f"{'━' * 60}")
        if _cluster.gated_files:
            _log(f"  [cluster] Skipping {len(_cluster.gated_files)} gated file(s) not compiled in this cluster: "
                 f"{', '.join(sorted(_cluster.gated_files))}")
        _mark_phase(project_dir, f"task0c/{_cluster.name}", "in_progress",
                    detail="starting", source_hash=_src_hash)
        _set_current(project_dir, f"task0c/{_cluster.name} / starting")

        _module_states: dict[str, ModuleAugmentState] = {}
        _cluster_done: set[str] = set()

        # Pre-load which modules are already done for this cluster (resume support).
        _is_last_cluster = (_cluster == _clusters[-1])
        for _m in modules:
            if is_cluster_augmented(project_dir, _cluster.name, _m):
                _cluster_done.add(_m)
                _log(f"  cluster/{_cluster.name}: '{_m}' already done — skipping.")
                # Crash-recovery: if this is the last cluster and the main module sentinel
                # is missing (pipeline crashed after writing cluster sentinel but before
                # mark_module_augmented), write it now from the cluster sentinel value.
                if _is_last_cluster and not is_module_augmented(project_dir, _m):
                    _cov_str = _cluster_augmented_path(project_dir, _cluster.name, _m).read_text().strip()
                    try:
                        _cov_val = float(_cov_str.rstrip("%"))
                    except ValueError:
                        _cov_val = 0.0
                    mark_module_augmented(project_dir, _m, _cov_val)
                    _log(f"  cluster/{_cluster.name}: recovery — wrote main sentinel for '{_m}' ({_cov_val:.1f}%)")

        # Pre-check: verify base library compiles under this cluster's env.
        # Handles E0554 (nightly feature on stable) by auto-installing nightly toolchain.
        if _cluster_done != set(modules):
            _pc_mod = next((m for m in modules if m not in _cluster_done), modules[0])
            if not _cluster_pre_check(project_dir, _pc_mod, _cluster):
                _log(f"  cluster/{_cluster.name}: pre-check failed — skipping all modules.")
                _mark_phase(project_dir, f"task0c/{_cluster.name}", "error",
                            detail="pre-check failed — cluster skipped", source_hash=_src_hash)
                for _m in modules:
                    if _m not in _cluster_done:
                        mark_cluster_augmented(project_dir, _cluster.name, _m, 0.0)
                        if _is_last_cluster:
                            mark_module_augmented(project_dir, _m, 0.0)
                        _cluster_done.add(_m)

        while True:
            # Pick next module not yet done for this cluster, highest coverage first.
            _pending = [_m for _m in modules if _m not in _cluster_done]
            if not _pending:
                break
            _mod_name = max(_pending, key=lambda _m: module_coverage.get(_m, 0.0))
            _mod_dir = dep_graph.crate_dir(_mod_name) if is_multi_module else project_dir
            _crate_type = dep_graph.crate_type(_mod_name) if is_multi_module else "lib"

            if not TARPAULIN_BIN.exists():
                raise RuntimeError(
                    f"HARD STOP: cargo-tarpaulin not found at {TARPAULIN_BIN}. "
                    "Install with: cargo install cargo-tarpaulin"
                )

            _log(f"\nTASK 0c [{_cluster.name}]: Augmenting '{_mod_name}' to 100% coverage...")
            _mark_phase(project_dir, f"task0c/{_cluster.name}/{_mod_name}", "in_progress",
                        detail="starting", source_hash=_src_hash)
            _set_current(project_dir, f"task0c/{_cluster.name}/{_mod_name} / starting")
            _task0c_t0 = time.monotonic()
            _emit("task.start", task="task0c", cluster=_cluster.name, module=_mod_name)

            _etf_pre = {rel for rel, _ in collect_test_files(project_dir)}
            _cov, _covered, _at_ceiling, _blocked_reason = task0c_augment_module(
                project_dir, _mod_name, _mod_dir, _crate_type,
                existing_test_files=_etf_pre,
                project_context=project_context,
                coverage_strategy=coverage_strategy,
            )
            _log(f"  TASK 0c [{_cluster.name}]: '{_mod_name}' coverage: {_cov:.1f}%")
            _emit("task.done", task="task0c", cluster=_cluster.name, module=_mod_name,
                  duration_ms=int((time.monotonic() - _task0c_t0) * 1000),
                  data={"coverage_pct": _cov, "covered_lines": _covered,
                        "at_ceiling": _at_ceiling, "blocked_reason": _blocked_reason})
            _mark_phase(project_dir, f"task0c/{_cluster.name}/{_mod_name}",
                        "done" if _cov >= COVERAGE_AUGMENTATION_TARGET else "in_progress",
                        detail=f"{_cov:.1f}%", source_hash=_src_hash)
            _set_current(project_dir, f"task0c/{_cluster.name}/{_mod_name} / {_cov:.1f}%")

            _state = _module_states.setdefault(
                _mod_name, ModuleAugmentState(_mod_name, _cluster.name,
                                              target_pct=COVERAGE_AUGMENTATION_TARGET)
            )
            _state.record_pass(_cov, _covered, _at_ceiling)
            _status = _state.status(_crate_type)

            # A blocked pass is deterministic: the harness survived the LLM fix
            # attempts AND a full gen_tests strip (or is broken in protected
            # files), so re-entering augmentation from the identical tree state
            # cannot end differently. Accept and move on instead of burning
            # STALL_LIMIT identical retries (observed live: nightly ×10 no-ops).
            if _blocked_reason and _status not in ('done', 'ceiling'):
                _log(
                    f"  HARD BLOCK (FR-018): '{_mod_name}' [{_cluster.name}] augmentation "
                    f"blocked — {_blocked_reason}. Retry would be a deterministic no-op; "
                    f"accepting and moving on."
                )
                _status = 'hard_block'

            if _status in ('done', 'ceiling', 'hard_block'):
                if _status == 'ceiling':
                    _log(
                        f"  CEILING [{_cluster.name}]: '{_mod_name}' at {_cov:.1f}% — all remaining "
                        f"uncovered lines are cfg-filtered or LLVM artifacts. No further passes possible "
                        f"in this cluster. Accepting and moving on."
                    )
                elif _status == 'hard_block':
                    _log(
                        f"  HARD BLOCK (FR-018): '{_mod_name}' [{_cluster.name}] coverage stuck "
                        f"at {_cov:.1f}% (stall {_state.stall_passes}/{ModuleAugmentState.STALL_LIMIT}, "
                        f"total {_state.total_passes}). Accepting and moving on."
                    )
                mark_cluster_augmented(project_dir, _cluster.name, _mod_name, _cov)
                # Also write the main module sentinel when the last cluster finishes,
                # so the strip/task1-4 loop sees it as done.
                if _cluster == _clusters[-1]:
                    mark_module_augmented(project_dir, _mod_name, _cov)
                _cluster_done.add(_mod_name)
            else:
                _log(
                    f"  HARD RULE (FR-018): '{_mod_name}' [{_cluster.name}] coverage {_cov:.1f}% < {COVERAGE_AUGMENTATION_TARGET:.0f}% "
                    f"(stall {_state.stall_passes}/{ModuleAugmentState.STALL_LIMIT}, "
                    f"total {_state.total_passes}, best covered {_state.best_covered_lines} lines). "
                    f"Retrying."
                )
                _module_augmented_path(project_dir, _mod_name).unlink(missing_ok=True)
                continue

        _log(f"\n  cluster/{_cluster.name} complete — {len(_cluster_done)} module(s) done.")
        _emit("cluster.done", cluster=_cluster.name,
              duration_ms=int((time.monotonic() - _cluster_t0) * 1000),
              data={"modules_done": sorted(_cluster_done)})
        _cov_summary = ", ".join(
            f"{_m}:{_cluster_augmented_path(project_dir, _cluster.name, _m).read_text().strip()}"
            for _m in _cluster_done
            if _cluster_augmented_path(project_dir, _cluster.name, _m).exists()
        )
        _commit_and_mark(
            project_dir, f"task0c/{_cluster.name}", "done",
            commit_message=f"pipeline: task0c/{_cluster.name} — {_cov_summary}",
            detail=f"{len(_cluster_done)} modules",
            source_hash=_src_hash,
        )

    _current_cluster = None

    total_iters = 0
    while True:
        module_name = _next_module() if is_multi_module else (modules[0] if modules else None)
        if module_name is None:
            break

        module_dir = dep_graph.crate_dir(module_name) if is_multi_module else project_dir
        crate_type = dep_graph.crate_type(module_name) if is_multi_module else "lib"
        workspace_deps = dep_graph.workspace_deps(module_name) if is_multi_module else set()
        module_test_cmd = dep_graph.module_test_command(test_command, module_name) if is_multi_module else test_command
        module_manifest = project_dir / f".generated_files_{module_name}.txt"
        module_dir_rel = (
            str(module_dir.relative_to(project_dir)) + "/"
            if is_multi_module and module_dir != project_dir else ""
        )

        _log(f"\n{'=' * 60}")
        _log(f"MODULE: {module_name}  [{crate_type}]")
        ready_deps = workspace_deps & locked_modules
        if ready_deps:
            _log(f"  Locked deps  : {', '.join(sorted(ready_deps))}")
        _log(f"{'=' * 60}")

        # --- TASK 0c: all clusters complete — sentinel check only ---
        # All coverage augmentation (across all clusters) was done by the cluster
        # outer loop above. This block just verifies the sentinel and proceeds.
        # FORWARD MODE: skip — there is no augmentation phase in spec-driven mode.
        if not _from_spec:
            if not is_module_augmented(project_dir, module_name):
                _log(f"  WARNING: '{module_name}' has no augmentation sentinel — proceeding anyway.")
            _log(f"  TASK 0c: '{module_name}' all clusters done — proceeding to strip.")

        # Coverage check before strip — warn if below 100% but proceed anyway.
        # A hard stop here would abort the pipeline on coverage tool failures (bad sentinel,
        # grcov error, etc.) even when the test suite is intact.
        if crate_type != "proc-macro" and TARPAULIN_BIN.exists():
            _sentinel_path = _module_augmented_path(project_dir, module_name)
            try:
                _final_cov = float(_sentinel_path.read_text().strip().rstrip('%'))
            except (ValueError, OSError):
                _final_cov = 0.0
            if _final_cov < COVERAGE_AUGMENTATION_TARGET:
                _log(
                    f"\n  WARNING: '{module_name}' coverage is {_final_cov:.1f}% — "
                    f"proceeding to strip anyway. Reconstruction quality may be lower."
                )

        # --- Collect test files BEFORE stripping ---
        # Must happen before strip so we can accurately identify which tests belong
        # to this module. Strip removes implementation source but test content in
        # lib.rs would also be gone if we checked after.
        test_files_pre_strip = collect_test_files(project_dir)
        module_test_files = [
            (rel, content) for rel, content in test_files_pre_strip
            if rel.startswith(module_dir_rel)
        ] if module_dir_rel else test_files_pre_strip
        module_has_tests = len(module_test_files) > 0
        _log(f"  Module test files: {len(module_test_files)} (before strip)")

        # --- Strip: remove ONLY this module's implementation source ---
        # !IMPORTANT CAN'T STRIP CODE IF COVERAGE <100%, IF <100% go back to task 0c
        # Only strip once — if already stripped, the original structure file list
        # is intact and the implementation is already gone.
        # FORWARD MODE: no source to strip — the workspace starts with src/ empty
        # (or absent). The user-provided API file at .original_structure/{name}.api.md
        # already serves as structural ground truth.
        if _from_spec:
            _log(f"  [from-spec] Skipping strip for '{module_name}'.")
        else:
            if _phase_valid(project_dir, f"strip/{module_name}"):
                _log(f"  '{module_name}' already stripped — skipping.")
            else:
                if crate_type != "proc-macro" and TARPAULIN_BIN.exists():
                    _sentinel_path = _module_augmented_path(project_dir, module_name)
                    try:
                        _strip_cov = float(_sentinel_path.read_text().strip().rstrip('%'))
                    except (ValueError, OSError):
                        _strip_cov = 0.0
                    if _strip_cov < COVERAGE_AUGMENTATION_TARGET:
                        _log(
                            f"\n  WARNING at strip: '{module_name}' coverage is {_strip_cov:.1f}% "
                            f"— stripping anyway. Reconstruction may be incomplete."
                        )
                # Capture API surface BEFORE strip — structural ground truth for
                # TASK 1. The .original_structure/ dir survives strip (lives at the
                # project root, not under src/), so TASK 1 can read it post-strip.
                _capture_api_surface(project_dir, module_dir, module_name)
                # Hard Rule 11: bake the skeleton + leaf-first ordering +
                # cluster manifest from live source BEFORE strip. After
                # strip these artefacts are the only source-derived inputs
                # any downstream phase is allowed to read.
                _bake_res = _bake_skeleton_artifacts(project_dir, module_name)
                if not _bake_res.get("ok"):
                    _log(f"  [ABORT] skeleton bake failed for '{module_name}': "
                         f"{_bake_res.get('reason', 'unknown')} — refusing to strip.")
                    raise RuntimeError(
                        f"skeleton bake failed for {module_name}: "
                        f"{_bake_res.get('reason', 'unknown')}")
                _log(f"  Baked skeleton for {len(_bake_res['clusters'])} cluster(s): "
                     f"{', '.join(_bake_res['clusters'])}")
                # The bake's outputs (.skeleton/) are tracked in git. When
                # resuming from a task0c-augmented-{module} tag, the
                # rebake produces byte-identical files → nothing to
                # commit. That is success, not failure — the augmented
                # state IS already captured by the existing tag.
                _porc = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=project_dir, capture_output=True, text=True, timeout=10,
                ).stdout.strip()
                if _porc:
                    if not git_commit(project_dir,
                                      f"pipeline: task0c augmentation complete — "
                                      f"all unit tests added for {module_name}"):
                        _log(f"  [ABORT] augmentation commit failed for '{module_name}' — refusing to strip. Fix git state and retry.")
                        raise RuntimeError(f"augmentation commit failed for {module_name}")
                else:
                    _log("  [augmentation] tree already clean — augmented state preserved from prior run.")
                git_tag(project_dir, f"task0c-augmented-{module_name}")
                strip_module_source(project_dir, module_dir)
                # ATOMIC: commit the stripped tree FIRST, then mark state
                # done. Reverse of the legacy order — protects against
                # the lost-commit bug where a process dying between mark
                # and commit lost the strip commit forever.
                _commit_and_mark(
                    project_dir,
                    f"strip/{module_name}",
                    "done",
                    commit_message=f"pipeline: stripped — {module_name}",
                    source_hash=_src_hash,
                )
                _set_current(project_dir, f"strip/{module_name} / done")

        # Re-collect after strip — picks up any tests generated by TASK 0c.
        # test_files: full read set for TASK 1 (includes inline test blocks in src/)
        # existing_test_files: write-protection set for TASK 2 (pure test files only)
        test_files = collect_test_files(project_dir)
        existing_test_files = collect_protected_test_files(project_dir)
        _log(f"  Test files after strip: {len(test_files)} readable, "
             f"{len(existing_test_files)} write-protected.")

        # --- TASK 1: extract requirements from this module's tests ---
        module_req_path = project_dir / f"functional_requirements_{module_name}.md"

        # Initialize iteration counters here so task1's task4 escalation path can use them.
        module_iter = 1
        prev_pass_count = 0
        prev_test_output = ""
        prev_task2_source = ""  # generated source from previous iteration (for TASK 2 context)

        t1_input = module_test_files if module_has_tests else test_files
        if not module_has_tests:
            _log(f"  '{module_name}' has 0 tests — using full project tests for TASK 1.")

        # Append example files (examples/*.rs) as usage signal. They are NOT
        # assertions — the inline header makes that explicit so the LLM doesn't
        # derive requirements from example-only behavior.
        _t1_examples = collect_examples(project_dir, module_dir=module_dir if is_multi_module else None)
        if _t1_examples:
            _EXAMPLE_HEADER = (
                "// !!!! TASK 1 NOTE: This file is from the project's `examples/` directory.\n"
                "// !!!! It demonstrates idiomatic API usage. It is NOT a test and contains\n"
                "// !!!! no assertions to enforce. Use it to understand HOW the public API is\n"
                "// !!!! invoked, but do not derive requirements from it unless the same\n"
                "// !!!! behavior is also asserted by a test in the test suite.\n\n"
            )
            t1_input = t1_input + [
                (rel, _EXAMPLE_HEADER + content) for rel, content in _t1_examples
            ]
            _log(f"  Appended {len(_t1_examples)} example file(s) to TASK 1 input as usage signal.")

        # FORWARD MODE: copy the user-provided FR into the per-module slot so
        # TASK 2 picks it up via the existing module_req_path. Skip the full
        # TASK 1 inference loop — the user is the source of truth.
        if _from_spec:
            _fr_root = project_dir / "functional_requirements.md"
            if not module_req_path.exists():
                module_req_path.write_text(_fr_root.read_text(encoding="utf-8"),
                                           encoding="utf-8")
                _log(f"  [from-spec] Seeded {module_req_path.name} "
                     f"from {_fr_root.name} ({_fr_root.stat().st_size} chars).")
            else:
                _log(f"  [from-spec] {module_req_path.name} already present — leaving as-is.")
            t1_valid = True
        else:
            t1_valid = False

        # TASK 1 with anti-corruption retry (max 2 retries after initial attempt).
        T1_MAX_RETRIES = 2
        _task1_t0 = time.monotonic()
        if not _from_spec:
            _emit("task.start", task="task1", module=module_name)
        for t1_attempt in (range(0) if _from_spec else range(T1_MAX_RETRIES + 1)):
            if _phase_valid(project_dir, f"task1/{module_name}") and module_req_path.exists() and t1_attempt == 0:
                _log(f"\nTASK 1: requirements for '{module_name}' already done — skipping.")
            else:
                if t1_attempt > 0:
                    _log(f"\nTASK 1: retry {t1_attempt}/{T1_MAX_RETRIES} for '{module_name}'.")
                    module_req_path.unlink(missing_ok=True)
                if not task1(project_dir, genesis_path, module_req_path, t1_input,
                             readme_text, project_profile, crate_type=crate_type,
                             module_name=module_name, project_context=project_context,
                             coverage_strategy=coverage_strategy):
                    _log(f"  TASK 1 failed for '{module_name}' — skipping module.")
                    _emit("task.done", task="task1", module=module_name,
                          duration_ms=int((time.monotonic() - _task1_t0) * 1000),
                          data={"ok": False, "reason": "task1_failed"})
                    break

            ok, reason = validate_task1_output(module_req_path, t1_input)
            if ok:
                _log("  TASK 1 output validated OK.")
                t1_valid = True
                _commit_and_mark(
                    project_dir, f"task1/{module_name}", "done",
                    commit_message=f"pipeline: task1 — {module_name}",
                    detail=f"{module_req_path.stat().st_size} chars",
                    source_hash=_src_hash,
                )
                _set_current(project_dir, f"task1/{module_name} / done")
                _emit("task.done", task="task1", module=module_name,
                      duration_ms=int((time.monotonic() - _task1_t0) * 1000),
                      data={"ok": True, "retries": t1_attempt,
                            "req_chars": module_req_path.stat().st_size})
                break
            _log(f"  TASK 1 anti-corruption FAILED: {reason}")
            if t1_attempt >= T1_MAX_RETRIES:
                _log(f"  TASK 1 failed validation after {T1_MAX_RETRIES} retries — "
                     f"escalating to TASK 4 with failure_type='requirements_failure'.")
                genesis_before = read(genesis_path)
                failure_log_content = load_failure_log(failure_log_path)
                _t4_api_surface = load_api_surface(project_dir, module_name)
                task4(
                    genesis_path, module_req_path,
                    f"TASK 1 produced invalid requirements after {T1_MAX_RETRIES} retries. "
                    f"Last rejection reason: {reason}",
                    module_iter, project_dir,
                    failure_type="requirements_failure",
                    failure_log=failure_log_content,
                    prev_pass_count=prev_pass_count, prev_test_output=prev_test_output,
                    api_surface=_t4_api_surface,
                )
                break  # do not set t1_valid; fall through to `if not t1_valid: continue`

        if not t1_valid:
            continue

        task1_static_audit(module_req_path, module_test_files or test_files)

        # Workspace scan (done per-module since strip changes the file layout).
        workspace_structure = scan_workspace_structure(project_dir)
        build_check_cmd = _infer_build_check_command(test_command, project_dir)
        module_build_check = (
            dep_graph.module_build_check_command(build_check_cmd, module_name)
            if is_multi_module else build_check_cmd
        )

        # Slice 9: module-start lifecycle (save siblings → maybe-wipe → stub)
        # lives in application/workspace_lifecycle.py; the wipe is gated by
        # the codified wipe⟺rerun invariant (session-59 t2c→t3 fall-through).
        saved_gen_blocks: dict[str, list[tuple[int, str]]] = \
            _wl_save_gen_tests_siblings(
                module_dir, project_dir, existing_test_files,
                gen_tests_siblings=_gen_tests_siblings,
                is_gen_tests_file=_is_gen_tests_file)
        _wl_wipe_module_sources(
            module_dir, project_dir, existing_test_files,
            regenerating_phase_done=_phase_valid(
                project_dir, f"task2b/{module_name}"),
            write_stub=_partial(_write_workspace_stub, crate_type, module_dir))

        if not module_has_tests:
            _log(f"  '{module_name}' has 0 test files — will use whole-project context in TASK 2.")

        # --- TASK 2-4: reconstruction loop ---
        module_iter = 1  # reset in case task1 incremented it during escalation
        prev_pass_count = 0
        prev_test_output = ""
        no_improvement_streak = 0

        while True:
            _log(f"\n{'=' * 50}")
            _log(f"MODULE {module_name} — ITERATION {module_iter}")
            _log(f"{'=' * 50}")

            locked_dep_context = ""
            if is_multi_module and workspace_deps:
                locked_dep_context = load_locked_source_context(
                    project_dir, workspace_deps & locked_modules
                )

            label: int | str = f"{module_name}_{module_iter}" if is_multi_module else module_iter

            # Whole-project context: zero-test modules or stuck modules.
            if not module_has_tests:
                wpc_reason = (
                    f"Module '{module_name}' has no tests. "
                    f"Infer its public interface from how dependent modules import and use it."
                )
                whole_project_context = _format_whole_project_context(test_files, wpc_reason)
            elif no_improvement_streak >= NO_IMPROVEMENT_THRESHOLD:
                wpc_reason = (
                    f"No improvement for {no_improvement_streak} iterations on '{module_name}'. "
                    f"Full project test suite provided for additional signal."
                )
                whole_project_context = _format_whole_project_context(test_files, wpc_reason)
                _log(f"  Escalating to whole-project context ({no_improvement_streak} iters no improvement).")
            else:
                whole_project_context = ""

            # Append crate-type-specific constraints so the LLM generates valid code.
            if "proc-macro" in crate_type.lower():
                whole_project_context += """
--- RUST PROC-MACRO CRATE CONSTRAINTS (mandatory) ---
This is a Rust procedural macro crate. Strict rules apply:

1. lib.rs MUST ONLY export functions tagged with #[proc_macro_derive], #[proc_macro_attribute],
   or #[proc_macro]. NO pub structs, enums, traits, or type aliases at the crate root.

2. Proc-macro entry functions MUST use `proc_macro::TokenStream` (the stdlib crate),
   NOT `proc_macro2::TokenStream`, for their input and return types:
   CORRECT: pub fn my_derive(input: proc_macro::TokenStream) -> proc_macro::TokenStream
   WRONG:   pub fn my_derive(input: proc_macro2::TokenStream) -> proc_macro2::TokenStream

3. All helper types (structs, enums, traits) MUST live in private submodules (e.g. parse_attrs.rs,
   errors.rs). They should NOT be pub in lib.rs.

4. Inside private submodules, proc_macro2::TokenStream is fine for token manipulation.
--- END PROC-MACRO CONSTRAINTS ---
"""

            # TASK 2: generate source files.
            if not task2_inner_loop(
                project_dir, genesis_path, module_req_path, module_manifest,
                existing_test_files, project_profile, workspace_structure,
                module_build_check, locked_dep_context=locked_dep_context,
                current_module=module_name if is_multi_module else "",
                module_dir_rel=module_dir_rel,
                whole_project_context=whole_project_context,
                coverage_strategy=coverage_strategy,
                api_surface=load_api_surface(project_dir, module_name),
                prev_source_context=prev_task2_source,
            ):
                _log("TASK 2 inner loop exhausted — escalating to TASK 4.")
                test_output = "BUILD FAILED: inner loop exhausted all retries."
                failure_type = "build"
            else:
                # Anti-corruption: verify expected files are present before running tests.
                expected_paths = load_original_structure(project_dir, module_name)
                t2_ok, t2_reason = validate_task2_output(project_dir, expected_paths, req_path=module_req_path)
                if not t2_ok:
                    _log(f"  TASK 2 anti-corruption FAILED: {t2_reason} — treating as build failure.")
                    test_output = f"BUILD FAILED: {t2_reason}"
                    failure_type = "build"
                else:
                    # Reattach saved gen_tests siblings to generated source files.
                    # Idempotent: for each saved (pass_n, body) we ensure
                    #   (a) the sibling file exists with the saved body, and
                    #   (b) the source file contains a path-mod decl for it.
                    # The previous guard ("skip if any sibling already on disk")
                    # was a no-op once strip started preserving sibling files,
                    # leaving them orphaned by the regenerated source.
                    _reattached: list[tuple[Path, Path, int]] = []
                    # FR-038: source gen_tests PER-CLUSTER so a test's content and
                    # its cfg gate never separate. A gen_test generated under one
                    # cluster (e.g. nightly's `request_ref`) must be gated to THAT
                    # cluster and excluded from others. Emit ONE ungated sibling only
                    # when the identical test is present in EVERY cluster; otherwise
                    # emit one gated sibling per present cluster with a cluster-unique
                    # name (so neither the file content nor the decl collapses in the
                    # merge, and a single-cluster test stays fenced to its cluster).
                    _filled_root = project_dir / ".skeleton-filled"
                    _per_cluster_filled = [
                        c for c in _clusters
                        if (_filled_root / c.name).exists()
                    ]
                    if _per_cluster_filled:
                        _reattach_allow_line, _ = _derive_gentest_prelude(project_dir)
                        # (rel, n) -> {cluster_name: body}
                        _gt: dict[tuple[str, int], dict[str, str]] = {}
                        _cl_by_name = {c.name: c for c in _per_cluster_filled}
                        for _c in _per_cluster_filled:
                            _cdir = _filled_root / _c.name
                            for _sf in sorted(_cdir.rglob("*.rs")):
                                if re.search(r'\.gen_tests_\d+\.rs$', _sf.name):
                                    continue
                                _rel = str(_sf.relative_to(_cdir))
                                if _rel in existing_test_files:
                                    continue
                                for _n, _p in _gen_tests_siblings(_sf):
                                    try:
                                        _gt.setdefault((_rel, _n), {})[_c.name] = \
                                            _p.read_text(encoding="utf-8")
                                    except OSError:
                                        continue
                        _appends: dict[str, list[str]] = {}
                        _all_cluster_names = {c.name for c in _clusters}
                        _gt_origin = _load_gen_tests_origin(project_dir)
                        for (_rel, _n), _bodies in sorted(_gt.items()):
                            _tgt = project_dir / _rel
                            if not _tgt.exists():
                                continue
                            _origin_gate = _gt_origin.get(f"{_tgt.stem}.gen_tests_{_n}")
                            if _origin_gate is not None:
                                # Origin-cfg path (deterministic): one sibling gated by
                                # the cluster that GENERATED the test ('' = default/stable
                                # → ungated). A nightly-only test carries
                                # `#[cfg(error_generic_member_access)]` → excluded from
                                # stable/no_core, compiled in nightly. Bodies are copied
                                # verbatim across clusters, so any is representative.
                                _body = next(iter(_bodies.values()))
                                _sib = _gen_tests_path(_tgt, _n)
                                _sib.write_text(_body, encoding="utf-8")
                                _appends.setdefault(_rel, []).append(
                                    _build_gen_tests_decl(
                                        _sib.name, _n, _reattach_allow_line, _origin_gate))
                                _reattached.append((_tgt, _sib, _n))
                            elif (len(set(_bodies.values())) == 1
                                    and set(_bodies) == _all_cluster_names):
                                # Fallback (no origin manifest): present in EVERY cluster
                                # AND identical → safe ungated. A body found in only a
                                # subset (e.g. nightly-only `request_ref`) must NOT land
                                # here — it would compile under the other clusters' test
                                # build and break them.
                                _body = next(iter(_bodies.values()))
                                _sib = _gen_tests_path(_tgt, _n)
                                _sib.write_text(_body, encoding="utf-8")
                                _appends.setdefault(_rel, []).append(
                                    _build_gen_tests_decl(_sib.name, _n, _reattach_allow_line))
                                _reattached.append((_tgt, _sib, _n))
                            else:
                                # Fallback: divergent → per-cluster gated siblings, unique names
                                for _cname, _body in sorted(_bodies.items()):
                                    _gate = _cluster_cfg_attr(_cl_by_name.get(_cname))
                                    _sib = _tgt.with_name(
                                        f"{_tgt.stem}.gen_tests_{_n}_{_cname}.rs")
                                    _sib.write_text(_body, encoding="utf-8")
                                    _appends.setdefault(_rel, []).append(
                                        _build_gen_tests_decl(
                                            _sib.name, f"{_n}_{_cname}",
                                            _reattach_allow_line, _gate))
                                    _reattached.append((_tgt, _sib, _n))
                        for _rel, _decls in _appends.items():
                            _tgt = project_dir / _rel
                            _txt = _tgt.read_text(encoding="utf-8").rstrip()
                            _added = 0
                            for _d in _decls:
                                _path_line = _d.split("#[path = ")[-1].split("]")[0]
                                if _path_line not in _txt:
                                    _txt += _d
                                    _added += 1
                            if _added:
                                _tgt.write_text(_txt, encoding="utf-8")
                                _log(f"  Reattached {_added} per-cluster gen_tests "
                                     f"decl(s) to {_rel}.")
                    elif saved_gen_blocks:
                        # Non-cluster fallback (single tree): original behaviour.
                        _reattach_allow_line, _ = _derive_gentest_prelude(project_dir)
                        for rel, blocks in saved_gen_blocks.items():
                            src_path = project_dir / rel
                            if not src_path.exists():
                                continue
                            content = src_path.read_text(encoding="utf-8")
                            new_text = content.rstrip()
                            added_decls = 0
                            for pass_n, body in blocks:
                                sibling = _gen_tests_path(src_path, pass_n)
                                if not sibling.exists() or sibling.read_text(encoding="utf-8") != body:
                                    sibling.write_text(body, encoding="utf-8")
                                if not _has_gen_tests_decl(new_text, pass_n):
                                    new_text += _build_gen_tests_decl(
                                        sibling.name, pass_n, _reattach_allow_line
                                    )
                                    added_decls += 1
                                _reattached.append((src_path, sibling, pass_n))
                            if added_decls > 0:
                                src_path.write_text(new_text, encoding="utf-8")
                                _log(f"  Reattached {added_decls} gen_tests decl(s) to {rel}.")
                    if _reattached:

                        # Validate reattached gen_tests against the regenerated
                        # source. Compile failures are signal that the
                        # reconstruction is wrong, NOT that the gen_test is
                        # broken (gen_tests were validated at task0c time
                        # against the original). Log the names but leave them
                        # in place — Phase C / TDD picks up compile errors via
                        # the compile-frontier extractor and drives fills.
                        # Opt back into permanent quarantine with
                        # GEN_TESTS_REATTACH_QUARANTINE=1.
                        if _reattached:
                            try:
                                _vchk = subprocess.run(
                                    ["cargo", "check", "--tests"],
                                    cwd=project_dir, capture_output=True,
                                    text=True, timeout=300, env=_cargo_env(),
                                )
                            except Exception as _e:
                                _log(f"  [reattach] validate cargo check failed: {_e}")
                                _vchk = None
                            _legacy_quarantine = os.getenv(
                                "GEN_TESTS_REATTACH_QUARANTINE", "0",
                            ) == "1"
                            if _vchk is not None and _vchk.returncode != 0:
                                _out = (_vchk.stdout or "") + (_vchk.stderr or "")
                                _failing = [
                                    (sp, sb, pn) for sp, sb, pn in _reattached
                                    if sb.name in _out
                                ]
                                if _failing and not _legacy_quarantine:
                                    _names = ", ".join(sb.name for _, sb, _ in _failing)
                                    _log(f"  [reattach] {len(_failing)} gen_tests sibling(s) "
                                         f"fail compile against regenerated source: {_names}. "
                                         "Leaving in place so Phase C compile-frontier can act.")
                                    _emit("gen_tests.reattach_compile_fail", data={
                                        "module": module_name,
                                        "count": len(_failing),
                                        "files": [sb.name for _, sb, _ in _failing],
                                    })
                                elif _failing and _legacy_quarantine:
                                    quarantined = 0
                                    for src_path, sibling, pass_n in _failing:
                                        try:
                                            ptext = src_path.read_text(encoding="utf-8")
                                            decl_re = re.compile(
                                                r"\n*(?:#\[cfg\([^)]*\)\]\s*)?"
                                                r"#\s*\[\s*allow\s*\([^)]*\)\s*\][^\n]*\n*"
                                                r"#\s*\[\s*path\s*=\s*\""
                                                + re.escape(sibling.name)
                                                + r"\"\s*\][^\n]*\n*"
                                                r"(?:pub\s+)?mod\s+gen_tests_"
                                                + str(pass_n) + r"\s*;\s*",
                                            )
                                            ptext_new = decl_re.sub("\n", ptext)
                                            if ptext_new != ptext:
                                                src_path.write_text(ptext_new, encoding="utf-8")
                                        except Exception:
                                            pass
                                        try:
                                            sibling.rename(sibling.with_suffix(".rs.broken"))
                                        except Exception:
                                            with suppress(Exception):
                                                sibling.unlink()
                                        quarantined += 1
                                    if quarantined:
                                        _log(f"  [reattach] quarantined {quarantined} "
                                             "gen_tests sibling(s) (legacy mode).")
                                        _emit("gen_tests.reattach_quarantine", data={
                                            "module": module_name,
                                            "quarantined": quarantined,
                                        })

                    # API surface conformance: soft check — log drift but don't
                    # block. The report is appended to test_output below if
                    # TASK 3 fails, so TASK 4 sees structural drift alongside
                    # cargo's failure.
                    api_conformance_ok, api_conformance_report = (
                        validate_task2_api_surface_conformance(
                            project_dir, module_name, module_dir
                        )
                    )
                    if not api_conformance_ok:
                        _log(f"  ⚠️  {api_conformance_report}")

                    # TASK 3: run tests.
                    _set_current(project_dir, f"task3/{module_name} / running tests (iter {module_iter})")
                    passed, test_output = task3(
                        project_dir, module_test_cmd, label, project_dir, genesis_path
                    )
                    if not api_conformance_ok:
                        test_output = (
                            test_output
                            + "\n\n--- API surface drift (pre-test) ---\n"
                            + api_conformance_report
                            + "\n"
                        )
                    pass_count, fail_count, _ = _parse_test_counts(test_output)

                    # Anti-corruption: classify the test result before deciding what to do.
                    t3_status, t3_reason = validate_task3_output(
                        pass_count, fail_count, module_has_tests
                    )
                    _log(f"  TASK 3 result: {t3_status} — {t3_reason}")

                    if t3_status == "pass":
                        _commit_and_mark(
                            project_dir, f"task2-4/{module_name}", "done",
                            commit_message=f"pipeline: task3 iter {module_iter} — {module_name} tests pass",
                            tag=f"passing-{module_name}-{module_iter}",
                            detail=f"iteration {module_iter}",
                            source_hash=_src_hash,
                        )
                        _set_current(project_dir, f"task2-4/{module_name} / LOCKED iter {module_iter}")
                        if is_multi_module:
                            lock_module(project_dir, module_name, module_dir, module_manifest)
                            locked_modules.add(module_name)
                            git_commit(project_dir, f"pipeline: locked — {module_name}")
                        else:
                            _log(f"\nSUCCESS: all tests passed on iteration {module_iter}.")
                            git_commit(project_dir, f"pipeline: complete — all tests pass iter {module_iter}")
                            sys.exit(0)
                        break

                    elif t3_status == "no_files":
                        # Module has no test files — lock on build success.
                        _log("  Module has no test files — locking on build success.")
                        _commit_and_mark(
                            project_dir, f"task2-4/{module_name}", "done",
                            commit_message=f"pipeline: build passed (no tests) — {module_name} iter {module_iter}",
                            detail=f"no tests iter {module_iter}",
                            source_hash=_src_hash,
                        )
                        _set_current(project_dir, f"task2-4/{module_name} / LOCKED (no tests)")
                        if is_multi_module:
                            lock_module(project_dir, module_name, module_dir, module_manifest)
                            locked_modules.add(module_name)
                            git_commit(project_dir, f"pipeline: locked (no tests) — {module_name}")
                        else:
                            _log("\nSUCCESS: build passed with no module tests.")
                            sys.exit(0)
                        break

                    elif t3_status == "no_tests":
                        # Tests exist but 0 ran — check if the binary even compiles.
                        # If `--no-run` fails, the root cause is a build error, not a test
                        # harness issue; reclassify so TASK 4 gets accurate failure_type.
                        _log(f"  WARNING: no_tests — {t3_reason}. Running --no-run to diagnose.")
                        no_run_cmd = re.sub(
                            r'\bcargo\s+test\b',
                            'cargo test --no-run',
                            module_test_cmd, count=1
                        )
                        try:
                            nr_result = subprocess.run(
                                no_run_cmd, shell=True, cwd=str(project_dir),
                                capture_output=True, text=True, timeout=120
                            )
                            if nr_result.returncode != 0:
                                _log("  --no-run failed: build error causes zero tests.")
                                failure_type = "build"
                                test_output += (
                                    f"\n\nDIAGNOSIS (--no-run): build failed — "
                                    f"{nr_result.stderr[:_FIX_ERR_CAP]}"
                                )
                            else:
                                failure_type = "no_tests"
                                test_output += f"\n\nDIAGNOSIS: {t3_reason}"
                        except Exception as nr_err:
                            _log(f"  --no-run check failed: {nr_err}")
                            failure_type = "no_tests"
                            test_output += f"\n\nDIAGNOSIS: {t3_reason}"

                    else:  # "fail"
                        # Try targeted fix loop before full regeneration.
                        fix_passed, test_output = task2_fix_loop(
                            project_dir, module_manifest, existing_test_files,
                            test_output, module_test_cmd, module_req_path,
                            genesis_path=genesis_path,
                            module_dir_rel=module_dir_rel,
                        )
                        if fix_passed:
                            pass_count, fail_count, _ = _parse_test_counts(test_output)
                            t3_status, _ = validate_task3_output(
                                pass_count, fail_count, module_has_tests
                            )
                            if t3_status == "pass":
                                if is_multi_module:
                                    lock_module(project_dir, module_name, module_dir, module_manifest)
                                    locked_modules.add(module_name)
                                else:
                                    _log("\nSUCCESS: all tests passed after fix loop.")
                                    sys.exit(0)
                                break

                        failure_type = classify_failure(test_output)

            # TASK 4: analyze failure, update genesis.md.
            _log(f"  Failure classification: {failure_type}")
            genesis_before = read(genesis_path)
            failure_log_content = load_failure_log(failure_log_path)
            # Build source context: give TASK 4 the generated files so it can distinguish
            # a requirements gap (model was told the wrong thing) from a generation error
            # (model was told the right thing but produced wrong code).
            t4_source_context = ""
            for rel in load_manifest(module_manifest):
                if rel in existing_test_files:
                    continue
                sp = project_dir / rel
                if sp.exists():
                    t4_source_context += f'\n<FILE path="{rel}">\n{sp.read_text(encoding="utf-8")}\n</FILE>\n'
            _set_current(project_dir, f"task4/{module_name} / genesis update (iter {module_iter})")
            t4_ok, current_pass = task4(
                genesis_path, module_req_path, test_output, module_iter, project_dir,
                failure_type=failure_type, failure_log=failure_log_content,
                prev_pass_count=prev_pass_count, prev_test_output=prev_test_output,
                source_context=t4_source_context,
                api_surface=load_api_surface(project_dir, module_name),
            )
            genesis_after = read(genesis_path)

            # Anti-corruption: detect genesis stall (TASK 4 produced no update).
            t4_valid, t4_reason = validate_task4_output(genesis_before, genesis_after, test_output, failure_type)
            if not t4_valid:
                _log(f"  TASK 4 anti-corruption: {t4_reason}")
                t4_summary = f"genesis_stall: {t4_reason}"
                failure_type_logged = "genesis_stall"
            else:
                t4_summary = "genesis.md updated."
                failure_type_logged = failure_type
                # Genesis grew — regenerate coverage_strategy.md so TASK 0c and TASK 1
                # prompts reflect lessons learned this iteration. force=True bypasses
                # the hash cache (genesis update isn't captured in the inputs hash).
                _log("  Regenerating coverage_strategy.md after genesis update...")
                strategy_path = project_dir / COVERAGE_STRATEGY_NAME
                coverage_strategy = task0b_discovery(
                    strategy_path, project_dir, test_files, readme_text, project_profile,
                    clusters=_clusters, force=True,
                )

            append_failure_log(
                failure_log_path, label, failure_type_logged,
                current_pass, prev_pass_count, t4_summary,
            )
            if current_pass > prev_pass_count:
                no_improvement_streak = 0
            else:
                no_improvement_streak += 1
            prev_pass_count = current_pass
            prev_test_output = test_output
            total_iters += 1

            regression = current_pass < prev_pass_count
            if regression or total_iters % GENESIS_AUDIT_INTERVAL == 0:
                _log("\nGenesis audit triggered"
                     + (" (regression)" if regression else f" (every {GENESIS_AUDIT_INTERVAL} iters)"))
                task_genesis_audit(genesis_path)

            # Capture generated source for next iteration's TASK 2 (prev_source_context).
            # t4_source_context was already built from manifest; reuse it here so TASK 2
            # can compare against what failed rather than starting blind.
            prev_task2_source = t4_source_context

            # Repair iterations (2026-06-11): the tree is KEPT — no wipe, no
            # re-merge, no re-fill from scratch. The wipe-and-regenerate loop
            # measured "are the docs good enough for a clean generation?",
            # but the active skeleton/TDD generator never read those docs
            # (R13), so every regeneration landed on the same plateau while
            # discarding the fix loop's progress. Now t4's genesis update and
            # the FR reach the NEXT iteration's t3 fix loop through the
            # REPOSITORY_SPECIFICATIONS prompt section, and repair compounds
            # on the kept tree. task2a/2b/2c markers stay done → the module
            # loop skips t2 and goes straight to t3 + fix loop.
            #
            # Best-tree ratchet: snapshot on a new-best pass count; if this
            # iteration ended BELOW the best (regressed fix / broken build →
            # 0 passing), restore the best tree so the next iteration starts
            # at the floor, never beneath it.
            _src_rel = [rel for rel in load_manifest(module_manifest)
                        if rel not in existing_test_files]
            _best = best_tree_pass_count(project_dir)
            if current_pass > (_best if _best is not None else -1):
                _saved_n = save_best_tree(project_dir, _src_rel, current_pass,
                                          iteration=module_iter)
                _emit("ratchet.save", data={"pass_count": current_pass,
                                            "files": _saved_n, "iter": module_iter})
            elif _best is not None and current_pass < _best:
                _restored = restore_best_tree(project_dir)
                _emit("ratchet.restore", data={"best": _best, "current": current_pass,
                                               "files": len(_restored), "iter": module_iter})
            _log(f"\n  Keeping '{module_name}' tree for repair iteration "
                 f"{module_iter + 1} (no wipe — docs updated, fix loop continues).")
            _run_flow_audit(f"iter {module_iter}")

            module_iter += 1

    # -----------------------------------------------------------------------
    # Final: run full test suite to validate the integrated workspace.
    # Only reached in multi-module mode (single-module exits on pass above).
    # -----------------------------------------------------------------------
    _log(f"\n{'=' * 60}")
    _log("FINAL: Running full test suite to validate integrated workspace")
    _log(f"{'=' * 60}")
    passed, _ = task3(project_dir, test_command, "final", project_dir, genesis_path)
    if passed:
        _log("\nSUCCESS: all tests passed.")
        sys.exit(0)
    else:
        _log("\nWARNING: all modules locked individually but full suite failed.")
        _log("         Integration issues between modules may require attention.")
        sys.exit(1)


def _shutdown_on_finish_enabled() -> bool:
    """Runtime authority for power-off-on-finish: the sentinel file (written
    "1"/"0" by the monitor's [p] toggle) overrides .env SHUTDOWN_ON_FINISH.
    No sentinel → .env default. /tmp clears on reboot → back to .env."""
    try:
        return _SHUTDOWN_SENTINEL_PATH.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return os.getenv("SHUTDOWN_ON_FINISH", "0").strip() == "1"


def _poweroff_on_finish() -> None:
    """Power off when the run ends on its own (any sys.exit or crash). Not on
    Ctrl-C; not on SIGTERM (restart.sh kill terminates the process before the
    finally block runs). Gated by _shutdown_on_finish_enabled — toggleable
    live from the monitor, like graceful stop."""
    if not _shutdown_on_finish_enabled():
        return
    _log("shutdown-on-finish enabled — powering off (sudo -n systemctl poweroff).")
    subprocess.run(["sudo", "-n", "systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    _interrupted = False
    try:
        main()
    except KeyboardInterrupt:
        _interrupted = True
        raise
    finally:
        if not _interrupted:
            _poweroff_on_finish()

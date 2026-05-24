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
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

_RT = TypeVar("_RT")

import importlib

from dotenv import load_dotenv
import model_interact as _mi
from model_interact import ask as _ask_raw, MODEL
from strip_to_tests import detect_language, process_rust

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

# Per-task model overrides — each task uses the most appropriate model.
# Falls back to MODEL (the global default) if a specific override is not set.
# Rationale for each choice is documented in .env.
_TASK0_MODEL  = os.getenv("TASK0_MODEL",  MODEL)  # project profile — small/fast
_TASK0C_MODEL = os.getenv("TASK0C_MODEL", MODEL)  # coverage augmentation — best code model
_TASK1_MODEL  = os.getenv("TASK1_MODEL",  MODEL)  # requirements synthesis — code understanding
_TASK2_MODEL  = os.getenv("TASK2_MODEL",  MODEL)  # code generation — best code model
_TASK4_MODEL  = os.getenv("TASK4_MODEL",  MODEL)  # failure analysis — reasoning model

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
    """Return the safe content budget in chars for one model call."""
    backend = os.getenv("BACKEND", "ollama").lower()
    backend_var = {
        "vllm":        "VLLM_MAX_MODEL_LEN",
        "sglang":      "SGLANG_MAX_MODEL_LEN",
        "ollama":      "OLLAMA_NUM_CTX",
        "huggingface": None,
        "claude":      None,
    }.get(backend)
    if backend_var:
        val = os.getenv(backend_var)
        if val and val.strip().isdigit():
            tokens = int(val)
            return max((tokens - 6_000), 4_000) * 4
    if MODEL.startswith("claude") or backend == "claude":
        return (200_000 - 6_000) * 4
    return (8_192 - 6_000) * 4  # safe fallback


MODEL_CONTEXT_CHARS = _model_context_chars()

_GUARD_LOG        = os.getenv("PIPELINE_GUARD_LOG",   str(Path(__file__).parent / "logs" / "pipeline_guards.log"))
_PIPELINE_LOG     = os.getenv("PIPELINE_LOG",         str(Path(__file__).parent / "logs" / "pipeline_anyhow.log"))
_LOG_MAX_BYTES    = 512 * 1024   # keep last 512 KB in pipeline_anyhow.log
_LOG_TRIM_EVERY   = 50           # check file size every N _log() calls
_log_trim_counter = 0


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
    try:
        _PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _store_prompt_content(text: str) -> dict[str, int | str]:
    """Content-address a prompt/response. Returns {sha256, char_count}.

    Writes to logs/prompts/{sha256}.txt if not already present. Never raises.
    """
    if not text:
        return {"sha256": "", "char_count": 0}
    h = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    path = _PROMPTS_DIR / f"{h}.txt"
    if not path.exists():
        try:
            path.write_text(text, encoding="utf-8", errors="replace")
        except Exception:
            pass
    return {"sha256": h, "char_count": len(text)}


def _emit(event: str, **fields) -> None:
    """Append a structured event to logs/events.jsonl. Never raises."""
    try:
        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
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
                  data={"response": _store_prompt_content(_response or "")})
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
#   2. Replace _safe_input_char_limit() and its callers below with:
#        _THINKING_MODEL_CHAR_LIMIT = 120_000
#        SINGLE_CALL_CHAR_LIMIT = min(_THINKING_MODEL_CHAR_LIMIT, MODEL_CONTEXT_CHARS * 3 // 4)
#        TASK0C_PROMPT_CHAR_CAP = _THINKING_MODEL_CHAR_LIMIT
#        FILE_CHAR_LIMIT = min(_THINKING_MODEL_CHAR_LIMIT, MODEL_CONTEXT_CHARS * 85 // 100)
def _safe_input_char_limit(min_content_tok: int = 16_384) -> int:
    """
    Maximum input chars that guarantee at least min_content_tok output tokens.

    Derived from model_interact._cap():
      input_chars ≤ (ctx - thinking_budget - 512 - min_content_tok) × cpt_floor

    cpt_floor=3.0 matches the floor in model_interact._estimated_input_tokens so
    the estimate is consistent between pipeline and inference layer.

    min_content_tok controls the output reservation:
      - 16_384 for tasks that generate large outputs (full source files, requirements)
      - 4_096  for task0c which generates small test blocks only
    """
    CPT_FLOOR     = 3.0    # chars/token — matches model_interact.py cpt_floor
    VLLM_OVERHEAD = 512    # internal vLLM overhead tokens
    backend = os.getenv("BACKEND", "ollama").lower()
    if backend == "vllm":
        ctx      = int(os.getenv("VLLM_MAX_MODEL_LEN", "65536"))
        thinking = int(os.getenv("VLLM_THINKING_BUDGET", "0"))
    elif backend == "sglang":
        ctx      = int(os.getenv("SGLANG_MAX_MODEL_LEN", "65536"))
        thinking = int(os.getenv("SGLANG_THINKING_BUDGET", "0"))
    elif backend == "ollama":
        ctx      = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
        thinking = 0
    else:
        ctx, thinking = 8_192, 0
    return max(4_000, int((ctx - thinking - VLLM_OVERHEAD - min_content_tok) * CPT_FLOOR))

# General tasks (task1/2/3/4): large outputs (full files, requirements docs).
# task0c: small test blocks only → smaller output reservation → more input budget.
_SAFE_INPUT_CHAR_LIMIT      = _safe_input_char_limit(min_content_tok=16_384)
_SAFE_INPUT_CHAR_LIMIT_TASK0C = _safe_input_char_limit(min_content_tok=4_096)
SINGLE_CALL_CHAR_LIMIT = min(_SAFE_INPUT_CHAR_LIMIT, MODEL_CONTEXT_CHARS * 3 // 4)

# Supplementary material caps — scale with context so they don't freeze at
# values tuned for an old (small) context window.
_README_CAP    = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # ~5% of budget
_PROFILE_CAP   = max(2_000, SINGLE_CALL_CHAR_LIMIT // 30)   # ~3% of budget
_GENESIS_CAP   = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # methodology doc
_FIX_ERR_CAP   = max(2_000, SINGLE_CALL_CHAR_LIMIT // 10)   # per-attempt error in fix loop
_STRATEGY_CAP  = max(4_000, SINGLE_CALL_CHAR_LIMIT // 15)   # coverage strategy doc
_SIBLING_CAP   = max(3_000, SINGLE_CALL_CHAR_LIMIT // 30)   # per-sibling condensed source
_SRC_SNIP_CAP  = max(4_000, SINGLE_CALL_CHAR_LIMIT // 10)   # source snippet in task0c prompts
_EXTRA_CAP     = max(3_000, SINGLE_CALL_CHAR_LIMIT // 20)   # extra context files (Cargo.toml etc.)
_WORKING_CAP   = max(800,   SINGLE_CALL_CHAR_LIMIT // 100)  # "what worked before" hint


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

# Max retries for the inner build-fix loop (TASK 2 + compile check).
BUILD_CHECK_RETRIES = 5

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
COVERAGE_BACKEND = os.getenv("COVERAGE_BACKEND", "tarpaulin").lower()  # "tarpaulin" | "grcov" | "llvm-cov"

# Pragma comment each backend recognises to exclude a line from coverage.
COVERAGE_IGNORE_PRAGMA = (
    "// # tarpaulin coverage ignore" if COVERAGE_BACKEND == "tarpaulin"
    else "// GRCOV_EXCL_LINE"
)

FILE_PATTERN = re.compile(r'<FILE path="([^"]+)">(.*?)</FILE>', re.DOTALL)
# Single-quote variant: <FILE path='foo'>
_FILE_PATTERN_SQ = re.compile(r"<FILE path='([^']+)'>(.*?)</FILE>", re.DOTALL)
# Fallback: models sometimes use <path/to/file> ... </path/to/file> or ```lang blocks.
# We only support the canonical FILE format; this pattern helps detect the wrong format
# so we can log a useful warning instead of silently returning nothing.
_BAD_FILE_PATTERN = re.compile(r'<([^>]+\.[a-zA-Z0-9]+)>')
# Code-block-comment pattern: ```lang\n// FILE path="src/lib.rs"\n...\n```
_CODE_BLOCK_FILE_PATTERN = re.compile(
    r'```[^\n]*\n(?:(?://|#|--)\s*FILE\s+path=["\']?([^\s"\'>\n]+)["\']?\s*\n)(.*?)```',
    re.DOTALL,
)

# Delimiter used by task4 to return updated genesis.md (simpler than XML for small models).
GENESIS_DELIM_START = "===GENESIS_START==="
GENESIS_DELIM_END = "===GENESIS_END==="

# Separator written into tests_output_N.txt between genesis snapshot and output.
OUTPUT_SEPARATOR = "=== test output ==="

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
PIPELINE_STATE_NAME = "pipeline_state.md"

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
    try:
        result = subprocess.run(
            ["git", "log", "--format=%h", "-1", filename],
            cwd=root, capture_output=True, text=True, timeout=5,
        )
        h = result.stdout.strip()
        return h if h else "unknown"
    except Exception:
        return "unknown"


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
    _log(f"  PIPELINE STARTED — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC  pipeline.py={_py_hash}{_git_hash}")
    _log(f"{'=' * 60}")
    _log(f"{'=' * 60}")


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

def _normalize_file_markers(text: str) -> str:
    """
    Small models frequently prefix <FILE ...> tags with markdown artefacts:
      ### <FILE path="x">   ->  <FILE path="x">
      ### FILE path="x">    ->  <FILE path="x">
      <FILE path='x'>       ->  <FILE path="x">
    Normalise these so the primary regex can match them.
    """
    # Strip leading `###` (or `##`, `#`) before a <FILE ... > tag on the same line.
    text = re.sub(r'#{1,6}\s*(<FILE\s)', r'\1', text)
    # Handle `### FILE path=` (missing opening `<`)
    text = re.sub(r'#{1,6}\s*FILE\s+path=', '<FILE path=', text)
    # Single-quote to double-quote: <FILE path='x'> -> <FILE path="x">
    text = _FILE_PATTERN_SQ.sub(lambda m: f'<FILE path="{m.group(1)}">{m.group(2)}</FILE>', text)
    return text


def _strip_markdown_fences(content: str) -> str:
    """Remove markdown code fences that models sometimes include in file content.

    Handles three cases:
    1. Opening fence at start:  ```rust\\ncode\\n```  →  code
    2. Closing fence at end (after opening already stripped)
    3. Orphaned standalone fence lines anywhere in the content (e.g. the model
       wrapped only part of the output, leaving a stray ``` in the middle).
    Rust doc-comment fences (/// ```) start with slashes so are unaffected.
    """
    content = content.strip()
    # Strip opening fence: ```lang or ``` on its own line at the very start
    content = re.sub(r'^```[a-zA-Z0-9_-]*\s*\n', '', content)
    # Strip closing fence at the very end
    content = re.sub(r'\n```\s*$', '', content)
    # Strip any remaining standalone fence lines anywhere in the file
    # (lines that are exactly ``` or ```lang with optional trailing whitespace)
    content = re.sub(r'^```[a-zA-Z0-9_-]*\s*$', '', content, flags=re.MULTILINE)
    return content.strip()


def _looks_like_prose(content: str, path: str) -> bool:
    """Return True if file content appears to be prose description rather than source code.

    Guards against the model writing 'This snippet outlines...' style descriptions
    instead of actual code inside <FILE> blocks.
    """
    # Only apply to source code extensions
    if not any(path.endswith(ext) for ext in ('.rs', '.py', '.js', '.ts', '.go', '.java', '.c', '.cpp', '.h')):
        return False
    first_line = content.lstrip().split('\n')[0] if content.strip() else ''
    prose_starters = (
        'this ', 'the ', 'here ', 'below ', 'note:', 'note ', 'file ', 'module ',
        'this file', 'this module', 'this snippet', 'this code', 'this implementation',
        'the following', 'below is', 'here is', 'as outlined', 'per the',
    )
    return any(first_line.lower().startswith(p) for p in prose_starters)


def extract_files(text):
    """Parse all <FILE path="...">...</FILE> blocks from model output."""
    text = _normalize_file_markers(text)
    files = {}
    for path, content in FILE_PATTERN.findall(text):
        content = _strip_markdown_fences(content)
        if _looks_like_prose(content, path):
            _log(f"  WARNING: model wrote prose instead of code for `{path}` — skipping.")
            continue
        files[path] = content
    if not files and _BAD_FILE_PATTERN.search(text):
        _log("  WARNING: model used wrong file format (e.g. <path/to/file> instead of "
             '<FILE path="path/to/file">). Retrying so it uses the correct format.')
    return files


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


def extract_code_content(text: str) -> str:
    """Strip thinking blocks and outermost code fence, return bare code content.

    Used as last-resort fallback when the model ignores <FILE> markers.
    Works for whole-file extraction (TASK 2 source files, genesis, etc.)
    where brace-counting is not appropriate.
    Steps:
      1. Remove <think>...</think> blocks (compat path leaks these as content).
      2. Find the first code fence (```[lang]\\n...```) and return its body.
      3. If no fence, return the full text stripped of leading/trailing whitespace.
    """
    import re as _re
    cleaned = _re.sub(r'<think>.*?</think>\s*', '', text, flags=_re.DOTALL).strip()
    fence = _re.search(r'```[a-zA-Z0-9_-]*\n(.*?)```', cleaned, flags=_re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return cleaned


def _extract_genesis(text: str):
    """
    Extract genesis.md content from Task 4 response.
    1. Primary: content between GENESIS_DELIM_START / GENESIS_DELIM_END.
    2. Fallback: <FILE path="genesis.md">...</FILE> block.
    3. Last resort: content inside any markdown/text code fence (model wraps in ```markdown).
    Returns the stripped content string, or None if not found.
    """
    start = text.find(GENESIS_DELIM_START)
    end = text.find(GENESIS_DELIM_END)
    if start != -1 and end != -1 and end > start:
        return text[start + len(GENESIS_DELIM_START):end].strip()
    # Model output start delimiter but forgot the closing one — use rest of response.
    if start != -1:
        content = text[start + len(GENESIS_DELIM_START):].strip()
        if len(content) > 100:
            return content
    # Fallback: FILE tag format
    files = extract_files(text)
    if "genesis.md" in files:
        return files["genesis.md"]
    # Last resort: strip thinking + any code fence and take the bare content.
    content = extract_code_content(text)
    if content and len(content) > 100:
        return content
    return None


def read(path) -> str:
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


def write(path, content):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)


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


def trim_to_line(text: str, cap: int, marker: str = "\n// ... (truncated)") -> str:
    """Truncate text at the last newline before cap — never mid-line. FR-020."""
    if len(text) <= cap:
        return text
    cut = text.rfind("\n", 0, cap)
    return text[:cut if cut != -1 else cap] + marker


def smart_truncate(text, total_limit=8000, head=2000, tail=6000):
    """
    Keep the beginning (build errors, first failures) and end (summary) of
    output. Truncating from the middle avoids losing either signal.
    """
    if len(text) <= total_limit:
        return text
    omitted = len(text) - head - tail
    return text[:head] + f"\n\n[... {omitted:,} chars omitted ...]\n\n" + text[-tail:]


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
             datetime.now(timezone.utc).isoformat())
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
             fix_hint[:500], iteration, datetime.now(timezone.utc).isoformat())
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
             iteration, datetime.now(timezone.utc).isoformat())
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
             fail_count, datetime.now(timezone.utc).isoformat())
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


def extract_test_output_from_file(file_content: str) -> str:
    """
    tests_output_N.txt contains a genesis snapshot followed by OUTPUT_SEPARATOR
    and then the actual test output. Strip the genesis header so that only the
    test output is returned -- avoids truncation cutting into the genesis block.
    """
    marker = OUTPUT_SEPARATOR + "\n"
    idx = file_content.find(marker)
    if idx == -1:
        return file_content
    return file_content[idx + len(marker):]


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

                pkg_m = re.search(r'^\s*\[package\].*?name\s*=\s*"([^"]+)"', text, re.DOTALL | re.MULTILINE)
                if not pkg_m:
                    continue
                pkg_name = pkg_m.group(1)
                crate_dir = ct.parent
                rel_dir = str(crate_dir.relative_to(project_dir)) if crate_dir != project_dir else "."

                targets = []
                if re.search(r'^\s*\[lib\]', text, re.MULTILINE):
                    lib_path_m = re.search(r'^\s*\[lib\].*?path\s*=\s*"([^"]+)"', text, re.DOTALL | re.MULTILINE)
                    lib_path = lib_path_m.group(1) if lib_path_m else "src/lib.rs"
                    is_proc_macro = bool(re.search(r'proc-macro\s*=\s*true', text, re.MULTILINE))
                    label = "proc-macro target" if is_proc_macro else "lib target"
                    targets.append(f"  - {label} → required file: `{rel_dir}/{lib_path}`")
                elif re.search(r'^\s*src/lib\.rs', text, re.MULTILINE) or (crate_dir / "src" / "lib.rs").exists():
                    targets.append(f"  - lib target (implicit) → required file: `{rel_dir}/src/lib.rs`")

                for bin_m in re.finditer(r'\[\[bin\]\]([^\[]*)', text, re.DOTALL):
                    bin_block = bin_m.group(1)
                    bin_name_m = re.search(r'name\s*=\s*"([^"]+)"', bin_block)
                    bin_path_m = re.search(r'path\s*=\s*"([^"]+)"', bin_block)
                    bin_name = bin_name_m.group(1) if bin_name_m else pkg_name
                    bin_path = bin_path_m.group(1) if bin_path_m else "src/main.rs"
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

                # Extract [dependencies] section to expose library versions.
                dep_lines = []
                dep_m = re.search(r'^\[dependencies\](.*?)(?=^\[|\Z)', text, re.DOTALL | re.MULTILINE)
                if dep_m:
                    raw_deps = dep_m.group(1).strip()
                    for dep_line in raw_deps.splitlines():
                        dep_line = dep_line.strip()
                        if dep_line and not dep_line.startswith('#'):
                            dep_lines.append(f"    {dep_line}")

                if targets:
                    lines.append(f"**Crate `{pkg_name}`** (at `{rel_dir}/`):")
                    lines.extend(targets)
                    if tests_note:
                        lines.extend(tests_note)
                    if dep_lines:
                        lines.append(f"  - dependencies:")
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

            pkg_m = re.search(
                r'^\s*\[package\].*?name\s*=\s*"([^"]+)"', text, re.DOTALL | re.MULTILINE
            )
            if not pkg_m:
                continue
            pkg_name = pkg_m.group(1)
            self._modules[pkg_name] = ct.parent

            if re.search(r'proc-macro\s*=\s*true', text, re.MULTILINE):
                self._crate_types[pkg_name] = "proc-macro"
            elif re.search(r'^\s*\[\[bin\]\]', text, re.MULTILINE):
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
        in_deg: dict[str, int] = {n: 0 for n in self._modules}

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
        "ts": datetime.now(timezone.utc).isoformat(),
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
    has_failures  = any(e.get("outcome") != "success" for e in recent)
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


def count_passing_tests(test_output: str) -> int:
    """
    Extract the number of passing tests from test output.
    Handles cargo-nextest, cargo test, pytest, and jest output formats.
    Returns 0 if not parseable.
    """
    patterns = [
        r'(\d+)\s+passed',          # cargo nextest: "42 passed"
        r'test result:.*?(\d+)\s+passed',  # cargo test
        r'(\d+)\s+tests?\s+passed',
        r'passed\s+(\d+)',
        r'(\d+)\s+ok',
    ]
    for pat in patterns:
        m = re.search(pat, test_output, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


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
    "file", "path", "type", "field", "method", "function", "module",
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
    """Return {(kind, name)} for every pub item declared in text."""
    return {(m.group(1), m.group(2)) for m in _PUB_ITEM_RE.finditer(text)}


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

    src_root = module_dir / "src"
    walk_root = src_root if src_root.is_dir() else module_dir
    gen_items: set[tuple[str, str]] = set()
    for p in walk_root.rglob("*.rs"):
        rel_parts = p.relative_to(project_dir).parts
        if any(part in {"examples", "benches", "tests"} for part in rel_parts):
            continue
        if any(part in SKIP_DIR_NAMES for part in rel_parts):
            continue
        if is_test_file(p, project_dir):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        gen_items |= _extract_pub_items(text)

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
    Anti-corruption check on test results.
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


def validate_task4_output(genesis_before: str, genesis_after: str,
                          test_output: str) -> tuple[bool, str]:
    """
    Anti-corruption check on genesis.md update.
    Returns (ok, reason). A stall or duplicate is flagged but not fatal.
    """
    if genesis_after.strip() == genesis_before.strip():
        return False, "genesis_stall — TASK 4 produced no update to genesis.md"

    if len(genesis_after) <= len(genesis_before):
        return False, "genesis shrank — TASK 4 may have removed content"

    new_text = genesis_after[len(genesis_before):]

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
    existing = list(root.glob("tests_output_*.txt"))
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
    if re.search(r'\.gen_tests_\d+\.rs$', path.name):
        return True

    return False


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
    if module_dir is None:
        roots = [project_dir]
    else:
        roots = [module_dir]
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

    def emit():
        if current_lines:
            parts.append("".join(current_lines))
            current_lines.clear()

    i = 0
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
                current_chars = sum(len(l) for l in current_lines)
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
        lines = [l.strip() for l in response.splitlines()
                 if l.strip() and not l.strip().startswith("```")]
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

def _is_rate_limit_error(e) -> bool:
    """Return True (and sleep) if e is a provider rate-limit error."""
    if MODEL.startswith("claude"):
        try:
            import anthropic
            if isinstance(e, anthropic.RateLimitError):
                _log(f"  Rate limit hit: {e}. Waiting {RATE_LIMIT_WAIT}s before retrying...")
                time.sleep(RATE_LIMIT_WAIT)
                return True
        except ImportError:
            pass
    return False


def with_retry(fn: "Callable[[], _RT]", retries: int = MAX_RETRIES) -> "_RT":
    """
    Call fn() up to `retries` times. fn must return False to signal failure,
    or any non-False value to signal success.
    Rate limit errors (429) pause for RATE_LIMIT_WAIT seconds before retrying
    and do not consume a retry attempt.
    """
    attempt = 1
    while attempt <= retries:
        try:
            result = fn()
        except Exception as e:
            if _is_rate_limit_error(e):
                continue  # retry without consuming an attempt
            _ename = type(e).__name__
            if any(t in _ename for t in ("Timeout", "ConnectionError", "APIError", "ReadTimeout")):
                _wait = min(30, 5 * attempt)
                _log(f"  Network error ({_ename}) — waiting {_wait}s before retry ({attempt}/{retries})...")
                time.sleep(_wait)
                attempt += 1
                continue
            raise
        if result is not False:
            return result
        if attempt < retries:
            _log(f"  Attempt {attempt}/{retries} failed, retrying...")
        attempt += 1
    _log(f"  All {retries} attempts failed.")
    return False  # type: ignore[return-value]


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

@dataclasses.dataclass
class BuildCluster:
    """One compiler configuration (e.g. stable, nightly, feature/async)."""
    name: str                           # short identifier used in logs and state keys
    env_extra: dict                     # env vars merged into _cargo_env() for this cluster
    tarpaulin_exclude_files: list       # --exclude-files patterns for tarpaulin
    excluded_cfg: list                  # cfg predicates inactive in this cluster
    active_cfg: list                    # cfg predicates active (on top of default)
    prompt_nightly_rule: str            # nightly boundary sentence(s) for generation prompts
    is_default: bool = True             # True for the baseline stable cluster
    gated_files: set = None             # source files (rel paths) gated behind excluded_cfg — skip augmentation
    requires_nightly: bool = False      # cluster needs nightly toolchain (set at runtime when E0554 detected)


@dataclasses.dataclass
class ModuleAugmentState:
    """Per-(module, cluster) augmentation lifecycle.

    Consolidates the five outer-loop state variables that used to live as
    parallel dicts (_c_retries, _c_best_covered, _c_total + _stall + _at_ceiling).
    Every exit path goes through `status()`, so adding a new termination
    condition is a single-case edit instead of a five-place edit.

    Lifecycle:
      record_pass(...) is called after each task0c_augment_module() return.
      status(crate_type) returns one of:
        'done'       — coverage 100%, or crate is proc-macro (cannot augment)
        'ceiling'    — task0c reported at_ceiling (no actionable lines remain)
        'hard_block' — stall_passes >= STALL_LIMIT (genuinely stuck)
        'retry'      — keep going
    """
    module_name: str
    cluster_name: str
    total_passes: int = 0
    best_covered_lines: int = -1
    stall_passes: int = 0    # consecutive passes with no improvement in covered count
    ceiling_passes: int = 0  # consecutive passes where task0c reported at_ceiling
    last_pct: float = 0.0
    last_covered: int = 0
    last_at_ceiling: bool = False

    STALL_LIMIT: int = 10
    CEILING_CONFIRM: int = 2  # consecutive at_ceiling reports required before accepting ceiling

    def record_pass(self, pct: float, covered: int, at_ceiling: bool) -> None:
        self.total_passes += 1
        self.last_pct = pct
        self.last_covered = covered
        self.last_at_ceiling = at_ceiling
        if at_ceiling:
            self.ceiling_passes += 1
        else:
            self.ceiling_passes = 0
        if covered > self.best_covered_lines:
            self.best_covered_lines = covered
            self.stall_passes = 0
        else:
            self.stall_passes += 1

    def status(self, crate_type: str) -> str:
        if self.last_pct >= 100.0:
            return 'done'
        if crate_type == 'proc-macro':
            return 'done'
        if self.ceiling_passes >= self.CEILING_CONFIRM:
            return 'ceiling'
        if self.stall_passes >= self.STALL_LIMIT:
            return 'hard_block'
        return 'retry'


# Active cluster during task0c — set by main() before each cluster pass.
# Read by _cargo_env() and _run_tarpaulin() so every subprocess call inherits
# the cluster's env and exclusion list without touching every call site.
_current_cluster: "BuildCluster | None" = None

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


def _git_default_branch(project_dir: Path) -> str:
    """Return the default branch name (main, master, etc.)."""
    for cmd in [
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
    ]:
        try:
            out = subprocess.check_output(cmd, cwd=project_dir,
                                          stderr=subprocess.DEVNULL, text=True).strip()
            return out.split("/")[-1]
        except Exception:
            pass
    # Fallback: check which of main/master exists locally
    for branch in ("main", "master"):
        r = subprocess.run(["git", "rev-parse", "--verify", branch],
                           cwd=project_dir, capture_output=True)
        if r.returncode == 0:
            return branch
    return "main"


def git_commit(project_dir: Path, message: str) -> bool:
    """Stage all changes and commit. Returns True if a commit was made."""
    if not _git_managed:
        return False
    try:
        subprocess.run(["git", "add", "-A"], cwd=project_dir,
                       check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                              cwd=project_dir, capture_output=True)
        if diff.returncode == 0:
            return False  # nothing staged
        subprocess.run(
            ["git", "commit", "-m", message, "--no-gpg-sign"],
            cwd=project_dir, check=True, capture_output=True,
        )
        _log(f"  [git] committed: {message}")
        try:
            _sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                                  cwd=project_dir, capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except Exception:
            _sha = ""
        _emit("git.commit", data={"message": message, "workspace_sha": _sha})
        return True
    except Exception as e:
        _log(f"  [git] commit failed: {e}")
        _emit("git.error", data={"message": message, "error": str(e)[:300]})
        return False


def git_tag(project_dir: Path, tag: str) -> None:
    """Create or update a lightweight tag at HEAD."""
    if not _git_managed:
        return
    try:
        subprocess.run(["git", "tag", "-f", tag], cwd=project_dir,
                       check=True, capture_output=True)
        _log(f"  [git] tagged: {tag}")
    except Exception as e:
        _log(f"  [git] tag failed: {e}")


def git_clean_reset(project_dir: Path) -> None:
    """Reset workspace to default branch and recreate the pipeline branch."""
    try:
        default = _git_default_branch(project_dir)
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=project_dir, capture_output=True)
        subprocess.run(["git", "clean", "-fd"], cwd=project_dir, capture_output=True)
        subprocess.run(["git", "checkout", default], cwd=project_dir,
                       check=True, capture_output=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{default}"],
                       cwd=project_dir, capture_output=True)
        subprocess.run(["git", "branch", "-D", "pipeline"],
                       cwd=project_dir, capture_output=True)  # ok if not exists
        subprocess.run(["git", "checkout", "-b", "pipeline"],
                       cwd=project_dir, check=True, capture_output=True)
        _log(f"  [git] reset to {default}, recreated pipeline branch")
    except Exception as e:
        _log(f"  [git] clean reset failed: {e}")


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
            _log(f"  [git] no .git found — initialising git repo")
            subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "initial: source snapshot", "--no-gpg-sign"],
                cwd=dest, check=True, capture_output=True,
            )
            _log(f"  [git] initial commit done")

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
            _log(f"  [git] checked out existing pipeline branch")
        else:
            subprocess.run(["git", "checkout", "-b", "pipeline"],
                           cwd=dest, check=True, capture_output=True)
            _log(f"  [git] created pipeline branch")

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
        raw = cargo_toml.read_text(errors="replace")
        # Extract [features] section
        feat_m = re.search(r'\[features\](.*?)(?=\n\[|\Z)', raw, re.DOTALL)
        if feat_m:
            feat_block = feat_m.group(1)
            for line in feat_block.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                m = re.match(r'^(\w[\w-]*)\s*=', line)
                if m:
                    cargo_features.add(m.group(1))
            # Parse default = ["feat1", "feat2"]
            def_m = re.search(r'default\s*=\s*\[([^\]]*)\]', feat_block)
            if def_m:
                default_features = {f.strip().strip('"').strip("'") for f in def_m.group(1).split(',')}

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
            flags_desc = f"--features {pred}"
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
            flags_desc = f"RUSTFLAGS += --cfg {pred}"
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
        clusters.append(BuildCluster(
            name=cluster_name,
            env_extra=env_extra,
            tarpaulin_exclude_files=tarpaulin_excl,
            excluded_cfg=excl,
            active_cfg=active,
            prompt_nightly_rule=pr,
            is_default=False,
            gated_files=_gated_files_for_excluded(excl),
        ))

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
        f"# Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
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
        try:
            parts.append(_strip_gen_tests_decls(p.read_text(errors="replace")))
        except Exception:
            pass
    for p in sorted(project_dir.rglob("*.rs")):
        rel = str(p.relative_to(project_dir))
        if "tests/" in rel or rel.startswith("tests"):
            try:
                parts.append(p.read_text(errors="replace")[:500])
            except Exception:
                pass
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:16]


def _state_path(project_dir: Path) -> Path:
    return project_dir / PIPELINE_STATE_NAME


def _read_state(project_dir: Path) -> dict:
    """Return {'phases': {key: {status, hash, detail}}, 'current': str}."""
    import re as _re
    state: dict = {"phases": {}, "current": ""}
    p = _state_path(project_dir)
    if not p.exists():
        return state
    for line in p.read_text().splitlines():
        m = _re.match(r'\[([^\]]+)\]\s+(\S+)\s+hash=(\S+)\s*(.*)', line.strip())
        if m:
            state["phases"][m.group(1)] = {
                "status": m.group(2),
                "hash":   m.group(3),
                "detail": m.group(4).strip(),
            }
        if line.startswith("current:"):
            state["current"] = line[len("current:"):].strip()
    return state


def _write_state(project_dir: Path, state: dict) -> None:
    from datetime import datetime as _dt
    lines = [
        f"# pipeline_state — {project_dir.name}",
        f"# {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for key, v in state["phases"].items():
        status = v.get("status", "pending")
        h      = v.get("hash", "-")
        detail = v.get("detail", "")
        lines.append(f"[{key}]  {status:<12}  hash={h}  {detail}")
    lines.append("")
    lines.append(f"current: {state.get('current', '')}")
    _state_path(project_dir).write_text("\n".join(lines) + "\n")


def _mark_phase(project_dir: Path, phase_key: str, status: str,
                detail: str = "", source_hash: str | None = None) -> None:
    """Update a single phase entry and rewrite the state file."""
    state = _read_state(project_dir)
    state["phases"][phase_key] = {
        "status": status,
        "hash":   source_hash or "-",
        "detail": detail,
    }
    _write_state(project_dir, state)


def _set_current(project_dir: Path, current: str) -> None:
    """Update the 'current:' line without changing any phase entry."""
    state = _read_state(project_dir)
    state["current"] = current
    _write_state(project_dir, state)


def _phase_valid(project_dir: Path, phase_key: str, current_hash: str | None = None) -> bool:
    """Return True if phase is marked done in pipeline_state.md.

    pipeline_state.md is the single source of truth for which phases are
    complete. We do NOT compare against an input hash — test files can change
    legitimately (task0c augments them, the user edits them) without
    invalidating the structural artifacts task0a/0b produce. `current_hash` is
    accepted as a backward-compat shim and ignored.
    """
    state = _read_state(project_dir)
    p = state["phases"].get(phase_key, {})
    return bool(p.get("status") == "done")


def _log_state_summary(project_dir: Path, current_hash: str) -> None:
    """Log the state file at startup so the user sees what's valid/stale."""
    state = _read_state(project_dir)
    if not state["phases"]:
        _log("  Pipeline state: no state file found — fresh run.")
        return
    _log("  Pipeline state:")
    for key, v in state["phases"].items():
        h      = v.get("hash", "-")
        status = v.get("status", "pending")
        detail = v.get("detail", "")
        if status == "done":
            valid = "valid  " if h == current_hash else "STALE  "
            symbol = "✓" if h == current_hash else "✗"
        elif status == "in_progress":
            valid = "resumed"
            symbol = "~"
        else:
            valid = "pending"
            symbol = " "
        _log(f"    [{symbol}] {key:<22}  {valid}  {detail}")
    if state.get("current"):
        _log(f"    last position: {state['current']}")


def task0_project_profile(profile_path: Path, test_files, readme_text: str) -> str:
    """
    Run once at startup. Identifies the project's language, build system, and
    workspace/module structure from README + test file paths + content excerpts.
    Produces project_profile.md. Skip is governed by pipeline_state.md — once
    "task0a" is marked done there, this is treated as finalized; tests changing
    later does not invalidate it. Returns the profile content.
    """
    project_dir = profile_path.parent
    if profile_path.exists():
        content = profile_path.read_text().strip()
        if content:
            # Artifact exists → treat as done. Backfill the state entry if it
            # predates pipeline_state.md tracking for this phase.
            if not _phase_valid(project_dir, "task0a"):
                _mark_phase(project_dir, "task0a", "done",
                            detail=f"{len(content)} chars (existing artifact)")
            _log(f"  task0a already done — loading {profile_path.name}.")
            return content

    _log("\nTASK 0a: Identifying project structure (language, build system, workspace layout)")

    readme_section = f"README.md:\n{readme_text[:_README_CAP]}\n\n" if readme_text.strip() else ""
    _PROFILE_STATIC = 800  # static instruction text estimate
    _sample_files = test_files[:25]
    _per_file_budget = prompt_budget(readme_section, " " * _PROFILE_STATIC) // max(1, len(_sample_files))
    _per_file_budget = max(200, _per_file_budget)

    test_entries = "\n\n".join(
        f"  {rel}\n  (excerpt):\n  {trim_to_line(content, _per_file_budget).replace(chr(10), chr(10) + '  ')}"
        for rel, content in _sample_files
    )

    prompt = f"""You are identifying the structure of a software project from its test files and README.

{readme_section}Test files (path + content excerpt):
{test_entries}

Produce a concise project_profile.md that answers the following:
1. Primary language and version (if specified or detectable from test imports).
2. Build system and test runner (e.g. "Cargo + cargo-nextest", "npm + jest").
3. Project layout: is it a monorepo / workspace? List each top-level module/crate/package
   with its directory path and type (library, binary, proc-macro, or both).
4. Minimum required files for each module so the build system accepts it
   (e.g. for Rust: Cargo.toml with [lib] section + src/lib.rs).
5. Structural conventions visible from the test files (e.g. tests in <crate>/tests/).

Infer crate types from test content: `extern crate proc_macro` or `#[proc_macro_derive]`
patterns indicate a proc-macro crate. `fn main()` or binary test patterns indicate a bin.

Be specific and concrete. This profile is used to generate source files — accuracy matters.
Do not mention the experiment or iteration process. Output only the profile document.
Keep the output under 2,000 characters — be concise.

<FILE path="project_profile.md">
content here
</FILE>
"""

    def attempt():
        response = ask(prompt, model=_TASK0_MODEL)
        files = extract_files(response)
        if "project_profile.md" not in files:
            stripped = re.sub(r'^```[^\n]*\n', '', response.strip())
            stripped = re.sub(r'\n```$', '', stripped).strip()
            if len(stripped) > 100:
                files = {"project_profile.md": stripped}
            else:
                _log(f"  WARNING: {MODEL} did not return project_profile.md.")
                _log(f"  Raw response snippet: {response[:500]}")
                return False
        content = files["project_profile.md"].strip()
        write(profile_path, content)
        # Invalidate the context document whenever the profile is regenerated so
        # task0_project_context re-synthesizes against the updated inputs.
        (profile_path.parent / PROJECT_CONTEXT_NAME).unlink(missing_ok=True)
        _mark_phase(project_dir, "task0a-context", "pending", detail="profile regenerated")
        _log(f"  project_profile.md written ({len(content):,} chars).")
        return content

    result = with_retry(attempt)
    if result is False:
        _log("  WARNING: could not produce project_profile.md after all retries. Continuing without it.")
        return ""
    _mark_phase(project_dir, "task0a", "done",
                detail=f"{len(str(result))} chars")
    return str(result)


def task0b_discovery(strategy_path: Path, project_dir: Path, test_files,
                     readme_text: str, profile_content: str,
                     clusters: "list[BuildCluster] | None" = None,
                     force: bool = False) -> str:
    """
    BL-001: Discovery pass — produce coverage_strategy.md before TASK 0c.

    Feeds the LLM the entire project source tree (src/**/*.rs, tests/**/*.rs, Cargo.toml)
    so it can document:
      - Correct `use` import paths for this crate's own types
      - How each public type is constructed (fields, variants, builders)
      - cfg/feature flags active in the project
      - Test style conventions already present in the test suite

    Cached with a hash of all source content. Returns strategy text (empty on failure).
    """
    cargo_toml_path = project_dir / "Cargo.toml"
    cargo_text = ""
    if cargo_toml_path.exists():
        try:
            cargo_text = cargo_toml_path.read_text(encoding="utf-8")
        except Exception:
            pass

    # Load manifest-tracked generated files so they can be excluded from the hash.
    # On restart mid-run the pipeline may have stripped or rewritten source files;
    # including them would produce a different hash and force an unnecessary re-run.
    _generated: set[str] = set()
    for _mf in [project_dir / MANIFEST_NAME, *project_dir.glob(".generated_files_*.txt")]:
        try:
            if _mf.exists():
                _generated.update(l.strip() for l in _mf.read_text().splitlines() if l.strip())
        except Exception:
            pass

    src_texts: list[str] = []
    for src_file in sorted(project_dir.rglob("*.rs")):
        if is_test_file(src_file, project_dir):
            continue
        # Skip gen_tests sibling files entirely — they are pipeline-generated content
        # and including them would invalidate the hash on every pass.
        if re.search(r'\.gen_tests_\d+\.rs$', src_file.name):
            continue
        try:
            rel = str(src_file.relative_to(project_dir))
            if rel in _generated:
                continue
            text = src_file.read_text(encoding="utf-8")
            # Strip path-mod declarations (which point at sibling files) so the hash
            # remains stable across pipeline passes.
            src_texts.append(_strip_gen_tests_decls(text))
        except Exception:
            pass

    # Skip is governed by pipeline_state.md ("task0b" phase) — the outer caller
    # already checks _phase_valid before invoking task0b_discovery. Source/tests
    # changes (e.g. task0c augmentation) do not invalidate the strategy doc.

    _log("\nTASK 0b (discovery): Analyzing project for coverage strategy...")

    # BL-010: condense source files to signatures only — function bodies are irrelevant
    # for TASK 0b which only needs import patterns, type shapes, and cfg flags.
    #
    # Budget: measure fixed overhead first (boilerplate instruction text + Cargo.toml +
    # README + clusters), then divide the remaining budget 55/45 between src and tests.
    # Old approach (fixed 45%/40% of SINGLE_CALL) didn't account for large README/Cargo.toml
    # and triggered the context guard which drops chars from the middle incoherently.
    _BOILERPLATE_ESTIMATE = 2_500   # static instruction text + FILE markers + headings

    # Cap cargo.toml and README so they don't eat the whole budget.
    _cargo_cap   = min(len(cargo_text), 3_000)
    _readme_cap  = min(len(readme_text), 2_000)

    clusters_section = ""
    if clusters:
        _cluster_lines = ["\nCompiler clusters (each is a separate test pass with its own env/cfg):"]
        for c in clusters:
            env_desc = ", ".join(f"{k}={v}" for k, v in c.env_extra.items()) if c.env_extra else "default"
            active   = ", ".join(c.active_cfg)   if c.active_cfg   else "none"
            excluded = ", ".join(c.excluded_cfg) if c.excluded_cfg else "none"
            _cluster_lines.append(f"  {c.name}: env={env_desc}  active_cfg=[{active}]  excluded_cfg=[{excluded}]")
        clusters_section = "\n".join(_cluster_lines) + "\n"

    _fixed_overhead = _BOILERPLATE_ESTIMATE + _cargo_cap + _readme_cap + len(clusters_section)
    _content_budget = max(2_000, SINGLE_CALL_CHAR_LIMIT - _fixed_overhead)
    src_budget  = int(_content_budget * 0.55)
    test_budget_alloc = int(_content_budget * 0.45)

    src_section_parts: list[str] = []
    src_dropped: list[str] = []

    # Collect and condense all source files first, then sort by DESCENDING complexity
    # (unsafe/cfg count) so the hardest-to-test files are included first. Alphabetical
    # order was dropping error.rs, ptr.rs, wrapper.rs — the most important files.
    # Exclude target/ directory — rglob picks up build-script output .rs files otherwise.
    src_candidates: list[tuple[int, Path, str, str]] = []  # (priority, path, rel, condensed)
    target_dir = project_dir / "target"
    for src_file in project_dir.rglob("*.rs"):
        if is_test_file(src_file, project_dir):
            continue
        try:
            if src_file.is_relative_to(target_dir):
                continue  # skip build artifacts
        except Exception:
            pass
        try:
            raw = src_file.read_text(encoding="utf-8")
            rel = str(src_file.relative_to(project_dir))
            condensed = _condense_rust_to_signatures(raw, filename=src_file.name)
            # Cap per-file contribution — macro-heavy files (ensure.rs, error.rs) produce
            # 35-50k condensed despite stripping, consuming the entire budget alone.
            if len(condensed) > _SIBLING_CAP:
                _cut = condensed.rfind("\n", 0, _SIBLING_CAP)
                condensed = condensed[:_cut if _cut != -1 else _SIBLING_CAP] + "\n// ... (signatures truncated)"
            # Priority = count of complexity markers (unsafe, #[cfg(, #[macro_export)
            priority = raw.count("unsafe") + raw.count("#[cfg(") + raw.count("#[macro_export")
            src_candidates.append((priority, src_file, rel, condensed))
        except Exception:
            pass
    src_candidates.sort(key=lambda x: x[0], reverse=True)  # most complex first

    for _, _sf, rel, condensed in src_candidates:
        part = f'\n<FILE path="{rel}">\n{condensed}\n</FILE>'
        if len(part) <= src_budget:
            src_section_parts.append(part)
            src_budget -= len(part)
        else:
            src_dropped.append(rel)  # BL-017: track dropped files

    # BL-017: warn if any source files were dropped from the discovery pass.
    if src_dropped:
        _log(f"  [task0b] WARNING: {len(src_dropped)} source file(s) dropped (budget exhausted): "
             f"{', '.join(src_dropped)}")

    test_section_parts: list[str] = []
    _tb = test_budget_alloc
    for rel, content in test_files:
        part = f'\n<TESTFILE path="{rel}">\n{content}\n</TESTFILE>'
        if len(part) <= _tb:
            test_section_parts.append(part)
            _tb -= len(part)

    cargo_section  = f"\nCargo.toml:\n```toml\n{cargo_text[:_cargo_cap]}\n```\n" if cargo_text else ""
    readme_section = f"\nREADME.md:\n{readme_text[:_readme_cap]}\n" if readme_text.strip() else ""
    dropped_note   = (
        f"\nNOTE: The following source files were excluded due to context budget limits "
        f"— the strategy below may be incomplete for: {', '.join(src_dropped)}\n"
    ) if src_dropped else ""
    src_section  = "\nSource files (signatures and type definitions only):\n" + "".join(src_section_parts) if src_section_parts else ""
    test_section = "\nTest files:\n" + "".join(test_section_parts) if test_section_parts else ""

    prompt = f"""You are analyzing a software project to produce a compact coverage strategy reference.
This will be injected verbatim into every unit-test-generation prompt — token budget is scarce.
Output must be machine-readable compact notation, NOT prose paragraphs.
{cargo_section}{readme_section}{clusters_section}{dropped_note}{src_section}{test_section}
Produce coverage_strategy.md in this exact compact format (copy headings literally):

IMPORTS:
  # one use-statement per line, only what internal mod gen_tests {{ use super::*; }} actually needs
  # tests are INSIDE the crate — never use <crate_name>::... paths
  # example lines (replace with real observations):
  #   std_prelude: use std::{{format, string::String, vec::Vec, boxed::Box}};
  #   fmt: use std::fmt::{{self, Display, Debug}};
  NEVER: <list items that look importable but will fail, one per line with reason after #>

TYPES:
  # TypeName: construction_expression  # notes (no PartialEq / opaque / etc.)
  # enum variants on same line: EnumName: Variant1|Variant2(T)|Variant3{{field:T}}
  # builder pattern: TypeName::builder().field(x).build()

CFG:
  # feature=name: default=on|off  # which code it gates
  # cfg(target_os/arch/...): ... if relevant

CONVENTIONS:
  # fn_sig: #[test] fn name() or #[test] fn name() -> Result<(),E>
  # assert: assert_eq!, assert!(x), assert!(matches!(x, Pat))
  # error_helper: inline pattern if tests need a custom error type

PITFALLS:
  # item → reason_it_fails → correct_alternative  (one per line)

RULES:
  # Hard constraints observed from source/Cargo.toml (one per line, terse):
  # Only include rules you can verify from the files above. Format examples (do NOT copy):
  #   NEVER: SomeType  # private / sealed — not accessible from test module
  #   NEVER: some_cfg_feature  # cfg-gated, nightly-only on this project
  #   NEVER: some_macro  # #[macro_export], not importable from inside crate

Only include lines you can directly observe in the files. Omit sections with nothing to say.
Each line must be self-contained — no cross-references between sections.
No prose. No markdown headers beyond the section names above.
Keep total output under 5,000 characters. Stop after RULES — do not add more sections.

<FILE path="coverage_strategy.md">
content here
</FILE>
"""

    def attempt():
        response = ask(prompt, model=_TASK0_MODEL, max_tokens=4096)
        files = extract_files(response)
        if "coverage_strategy.md" not in files:
            stripped = re.sub(r'^```[^\n]*\n', '', response.strip())
            stripped = re.sub(r'\n```$', '', stripped).strip()
            if len(stripped) > 100:
                files = {"coverage_strategy.md": stripped}
            else:
                _log("  WARNING: could not produce coverage_strategy.md.")
                return False
        content = files["coverage_strategy.md"].strip()
        # Deduplicate lines — model sometimes repeats NEVER/ALWAYS entries.
        seen, deduped = set(), []
        for line in content.splitlines():
            key = line.strip()
            if key and key in seen:
                continue
            seen.add(key)
            deduped.append(line)
        content = "\n".join(deduped)
        write(strategy_path, content)
        _log(f"  coverage_strategy.md written ({len(content):,} chars).")
        return content

    result = with_retry(attempt)
    if result is False:
        _log("  WARNING: discovery pass failed — proceeding without coverage strategy.")
        return ""
    return str(result)


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
) -> dict:
    """
    BL-007: Build context sections for one TASK 0c per-file prompt.

    Prioritized degradation — if total exceeds budget, drop lowest-priority sections first:
      1. Always: Cargo.toml + full source + uncovered fn bodies + public symbols
      2. Add if fits: coverage_strategy, attempt_history, existing inline tests, project context
      3. Add if fits: integration tests (shortest first)
      4. Add if fits: sibling source files (shortest first)

    Returns dict of section_name -> content string.
    """
    OVERHEAD = 8_000  # chars reserved for instructions, format markers, prompt boilerplate
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
    """
    Synthesize a behavioral summary of the project from test content + README + profile.
    Produces project_context.md — the portable big-picture document injected into every
    task, especially TASK 0c which receives no other project-level context.

    Skip is governed by pipeline_state.md (phase key "task0a-context"). Returns the
    summary text. If the upstream profile is regenerated, task0a clears this phase
    so it re-runs against fresh inputs.
    """
    project_dir = context_path.parent
    if context_path.exists():
        content = context_path.read_text().strip()
        if content:
            if not _phase_valid(project_dir, "task0a-context"):
                _mark_phase(project_dir, "task0a-context", "done",
                            detail=f"{len(content)} chars (existing artifact)")
            _log(f"  task0a-context already done — loading {context_path.name}.")
            return content

    _log("\nTASK 0a (context): Synthesizing behavioral project summary...")

    readme_section = f"README.md:\n{readme_text[:_README_CAP]}\n\n" if readme_text.strip() else ""
    profile_section = f"Project profile:\n{profile_content[:_PROFILE_CAP]}\n\n" if profile_content.strip() else ""
    _CONTEXT_STATIC = 600
    _sample_files = test_files[:25]
    _per_file_budget = prompt_budget(readme_section, profile_section, " " * _CONTEXT_STATIC) // max(1, len(_sample_files))
    _per_file_budget = max(200, _per_file_budget)

    test_excerpts = "\n\n".join(
        f"--- {rel} ---\n{trim_to_line(content, _per_file_budget)}"
        for rel, content in _sample_files
    )

    prompt = f"""You are writing a concise behavioral summary of a software project.
This summary will be used by AI agents that must write code for this project without
access to the original implementation — they need to understand what the project does
and what each module's role is.

{readme_section}{profile_section}Test file excerpts:
{test_excerpts}

Write a project_context.md that answers:
1. What does this project do? (2-3 sentences, concrete and specific)
2. What is each module/crate/package responsible for? (one line per module)
3. What are the key public abstractions — the types, traits, or interfaces that most
   code in the project depends on? (names + one-line description each)
4. What conventions does this project follow for error handling, data formats, or
   important invariants? (inferred from test assertions and naming patterns)

Write in plain prose. Be specific — use actual type names, function names, and module
names observed in the test files. Do not be vague. Do not describe the pipeline process.
Keep the output under 2,000 characters — be concise.

<FILE path="project_context.md">
content here
</FILE>
"""

    def attempt():
        response = ask(prompt, model=_TASK0_MODEL)
        files = extract_files(response)
        if "project_context.md" not in files:
            stripped = re.sub(r'^```[^\n]*\n', '', response.strip())
            stripped = re.sub(r'\n```$', '', stripped).strip()
            if len(stripped) > 100:
                files = {"project_context.md": stripped}
            else:
                _log(f"  WARNING: could not synthesize project_context.md.")
                return False
        content = files["project_context.md"].strip()
        write(context_path, content)
        _log(f"  project_context.md written ({len(content):,} chars).")
        return content

    result = with_retry(attempt)
    if result is False:
        _log("  WARNING: could not produce project_context.md — using README/profile fallback.")
        fallback = readme_text or profile_content or ""
        return fallback
    _mark_phase(project_dir, "task0a-context", "done",
                detail=f"{len(str(result))} chars")
    return str(result)


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
_API_SURFACE_CAP = max(8_000, SINGLE_CALL_CHAR_LIMIT // 4)


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
        genesis_exists = bool(existing_genesis.strip())
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
        tests_section = format_test_files(test_files)
        profile_section = (f"\n--- Project profile (language, build system, workspace layout) ---\n"
                           f"{project_profile}\n") if project_profile.strip() else ""
        context_section = (f"\n--- Project context (behavioral summary) ---\n"
                           f"{project_context}\n") if project_context.strip() else ""

        if not genesis_exists and not genesis_path.exists():
            write(genesis_path, "# genesis.md\n\n(populated by TASK 4 after first iteration)\n")

        prompt = self._single_prompt(
            tests_section, readme_text, profile_section, existing_genesis,
            coverage_strategy=coverage_strategy, api_surface=api_surface,
            context_section=context_section,
        )
        response = ask(prompt, model=_TASK1_MODEL)
        files = extract_files(response)
        if "functional_requirements.md" in files:
            write(functional_req_path, files["functional_requirements.md"])
            return True
        # Fallback: model returned content without FILE markers.
        stripped = response.strip()
        if stripped and len(stripped) > 200:
            _log("  WARNING: no FILE markers — using raw response as requirements.")
            write(functional_req_path, stripped)
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
{FILE_FORMAT_INSTRUCTION}
Only output the file `functional_requirements.md` using the markers above.
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

        # Fill-down: measure fixed overhead, give remainder to the partials being merged.
        _fixed_overhead = len(genesis_section) + len(api_section) + 3_000
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
{readme_text[:_README_CAP]}

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
                write(functional_req_path, files["functional_requirements.md"])
                return True
            # Fallback: model returned content without FILE markers.
            # Accept the raw response if it's non-empty and looks like requirements
            # (not an error message). The model produces correct content — wrong format.
            stripped = response.strip()
            if stripped and len(stripped) > 200:
                _log("  WARNING: no FILE markers in synthesis response — using raw response as requirements.")
                write(functional_req_path, stripped)
                return True
            _log("  ERROR: synthesis returned empty or unusable response.")
            _log(f"  Raw response snippet: {response[:300]}")
            return False

        ok = with_retry(synthesis_attempt)
        if ok:
            for f in partials_dir.glob("*.txt"):
                f.unlink()
            try:
                partials_dir.rmdir()
            except OSError:
                pass
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
{FILE_FORMAT_INSTRUCTION}
Only output the file `functional_requirements.md` using the markers above.
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

This is chunk {chunk_idx} of {total_chunks} -- there are more test files not shown here.
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
{FILE_FORMAT_INSTRUCTION}
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
  - Exact module path or import (e.g. `argh::from_env`, `argh_derive::ArghParser`)
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
        _fixed_contract_overhead = (
            len(profile_section) + len(strategy_section) + len(api_section)
            + len(existing_genesis) + 3_000
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

This is chunk {chunk_idx} of {total_chunks}. All chunks will be combined into a single
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

        _fixed_overhead = len(genesis_section) + len(api_section) + 3_000
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


def _task1_synthesizer_factory() -> Task1Synthesizer:
    backend = os.getenv("TASK1_SYNTHESIZER", "llm").lower()
    if backend == "static":
        return StaticTask1Synthesizer()
    return LLMTask1Synthesizer()


def _task1_proc_macro(project_dir, genesis_path, functional_req_path, test_files,
                      readme_text, project_profile, crate_name: str = "",
                      project_context: str = "", api_surface: str = ""):
    """
    Specialised TASK 1 for proc-macro crates.
    Instead of extracting runtime API contracts, extracts the proc-macro interface:
    - Which #[derive(...)] macros are provided
    - Which helper attributes (#[module(...)]) are used on fields/variants
    - What trait implementations the derives must generate
    """
    _log("  [proc-macro mode] Extracting derive macro interface from test usage.")

    # Prefilter: keep files that use #[derive(...)] (universal proc-macro signal) or
    # any attribute whose name matches the crate being implemented (e.g. #[my_crate(...)]).
    # The crate_name pattern is derived from the actual Cargo package name, not hardcoded.
    attr_pattern = r'#\[derive\('
    if crate_name:
        # Also catch #[crate_name(...)] attribute usage (dashes normalised to underscores).
        safe = re.escape(crate_name.replace("-", "_"))
        attr_pattern += rf'|#\[{safe}\('
    relevant = [
        (rel, content) for rel, content in test_files
        if re.search(attr_pattern, content)
    ]
    if not relevant:
        relevant = test_files  # Fallback: use all files
    _log(f"  [proc-macro mode] Using {len(relevant)} file(s) with derive/attribute usage.")

    existing_genesis = read(genesis_path)
    genesis_section = (
        f"\n--- genesis.md ---\n{existing_genesis}\n"
        if existing_genesis.strip() else ""
    )
    effective_context = project_context or readme_text

    # Budget: fixed overhead (~1500 boilerplate + genesis + context) then remainder for tests.
    _fixed = 1_500 + len(genesis_section) + min(len(effective_context), 2_000)
    _tests_budget = max(2_000, SINGLE_CALL_CHAR_LIMIT - _fixed)
    _raw_tests = "\n\n".join(
        f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>'
        for rel, content in relevant
    )
    tests_section = smart_truncate(_raw_tests, total_limit=_tests_budget,
                                   head=_tests_budget // 3, tail=2 * _tests_budget // 3)
    effective_context = effective_context[:_PROFILE_CAP]

    api_section = _format_api_surface_section(api_surface)
    prompt = f"""You are analysing test files to extract the interface of a Rust proc-macro crate.

A proc-macro crate provides #[derive(...)] macros and/or #[attribute] macros used in the
test files below. Your task is to produce functional_requirements.md describing ONLY what
this proc-macro crate must implement — not the runtime API of dependent crates.

{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
{genesis_section}{api_section}
Project context:
{effective_context}

Test files:
{tests_section}

Produce functional_requirements.md with:

1. **Derive macros provided** — list every `#[derive(MacroName)]` used in the tests
   that comes from this crate (not std derives like Clone, Debug, PartialEq).
   For each:
   - The derive name (e.g. `FromArgs`)
   - Which trait(s) it must implement on the annotated type
   - The signature of trait methods that tests call

2. **Helper attributes** — list every `#[crate_name(key = "value")]` attribute used
   on struct fields, enum variants, or items. For each:
   - The attribute path (e.g. `#[argh(option)]`, `#[argh(description = "...")]`)
   - Which field/item types it applies to
   - What it controls in the generated code

3. **proc-macro entry points** — for each derive/attribute macro:
   - The `#[proc_macro_derive(Name, attributes(...))]` registration
   - Input type: `TokenStream` (proc_macro::TokenStream)
   - Output type: `TokenStream`

4. **Generated trait implementations** — what `impl TraitName for T` blocks the macro
   must emit, and what methods those impls must contain, inferred from test assertions.

Do NOT include: runtime functions (from_env, from_file), stdlib types, or any API
that is NOT a proc-macro derive or attribute provided by this crate.
"""
    response = ask(prompt, model=_TASK1_MODEL)
    files = extract_files(response)
    req_content = files.get("functional_requirements.md", "").strip()
    if not req_content:
        # Model may have returned content without FILE markers — use raw response.
        # Strip any leading/trailing markdown fences.
        raw = re.sub(r'^```\w*\n?|```$', '', response.strip(), flags=re.MULTILINE).strip()
        if len(raw) > 200:
            req_content = raw
            _log(f"  [proc-macro TASK 1] Using raw response as requirements ({len(raw)} chars).")
    if not req_content:
        _log("  [proc-macro TASK 1] No output — writing static fallback requirements.")
        # Static fallback: scan tests for derive usage and write minimal requirements.
        derives = set()
        attrs = set()
        for _, content in relevant:
            for m in re.finditer(r'#\[derive\(([^)]+)\)', content):
                for d in m.group(1).split(','):
                    derives.add(d.strip())
            for m in re.finditer(r'#\[argh\(([^)]+)\)', content):
                attrs.add(m.group(1).strip())
        req_content = "# Proc-macro interface\n\n"
        req_content += "## Derive macros\n"
        for d in sorted(derives):
            req_content += f"- `#[derive({d})]` — implement the `{d}` trait on annotated types\n"
        if attrs:
            req_content += "\n## Helper attributes\n"
            for a in sorted(attrs):
                req_content += f"- `#[argh({a})]`\n"
        req_content += (
            "\n## Entry points\n"
            "Each derive macro must be registered with `#[proc_macro_derive(Name)]`.\n"
            "Input: `proc_macro::TokenStream`, Output: `proc_macro::TokenStream`.\n"
            "Parse input with `syn::parse_macro_input!(input as syn::DeriveInput)`.\n"
            "Generate output with `quote::quote! { impl Name for #ident { ... } }`.\n"
        )
    functional_req_path.write_text(req_content)
    _log(f"  [proc-macro TASK 1] functional_requirements written ({len(req_content)} chars).")
    return True


def task1(project_dir, genesis_path, functional_req_path, test_files, readme_text,
          project_profile: str = "", crate_type: str = "", module_name: str = "",
          project_context: str = "", coverage_strategy: str = ""):
    _log("\nTASK 1: Analyzing tests + README, building functional_requirements.md")

    # Pre-strip API surface snapshot (signatures + type defs of the original
    # source). Used as structural ground truth — TASK 1 derives behaviour from
    # the tests but must respect the shapes captured here.
    api_surface = load_api_surface(project_dir, module_name) if module_name else ""
    if api_surface:
        _log(f"  Loaded API surface: {len(api_surface):,} chars (pre-strip skeleton).")

    # For proc-macro crates with no own tests, extract proc-macro interface
    # (derive macros, helper attributes) from how they're used in the test suite.
    if crate_type == "proc-macro":
        return _task1_proc_macro(project_dir, genesis_path, functional_req_path,
                                 test_files, readme_text, project_profile,
                                 crate_name=module_name, project_context=project_context,
                                 api_surface=api_surface)

    existing_genesis = read(genesis_path)
    genesis_exists = bool(existing_genesis.strip())
    if genesis_exists:
        _log("  genesis.md exists — will use as-is; only producing functional_requirements.md.")
    else:
        _log("  No genesis.md yet — will create it alongside functional_requirements.md.")
    total_chars = sum(len(c) for _, c in test_files)
    _log(f"  Total test content: {total_chars:,} chars across {len(test_files)} file(s).")

    synthesizer = _task1_synthesizer_factory()
    _log(f"  Using {synthesizer.__class__.__name__}.")
    ok = synthesizer.synthesize(
        project_dir, genesis_path, functional_req_path, test_files, readme_text,
        project_profile, project_context=project_context, coverage_strategy=coverage_strategy,
        api_surface=api_surface,
    )

    if not ok:
        return False
    existing_genesis = read(genesis_path)
    if existing_genesis.strip():
        _log("  functional_requirements.md written (genesis.md unchanged).")
    else:
        _log("  genesis.md and functional_requirements.md written.")
    return True


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
                f"\n⚠️  PROC-MACRO CRATE — mandatory rules (syn = \"2.0\", default features only):\n"
                f"\n  IMPORTS — lib.rs must have these at the top:\n"
                f"    use proc_macro::TokenStream;\n"
                f"    use proc_macro2::{{TokenStream as TokenStream2, Span}};\n"
                f"    use syn::{{parse_macro_input, DeriveInput, Data, Fields, FieldsNamed, FieldsUnnamed,\n"
                f"              Field, Ident, Type, Attribute, Meta, Expr, Lit, LitStr, LitInt,\n"
                f"              Path, PathSegment, Generics, Error as SynError}};\n"
                f"    use quote::{{quote, quote_spanned, ToTokens}};\n"
                f"    use syn::spanned::Spanned;\n"
                f"\n  AVAILABLE syn 2.0 types (default features, no 'full' needed):\n"
                f"    DeriveInput, Data, DataStruct, DataEnum, DataUnion\n"
                f"    Fields, FieldsNamed, FieldsUnnamed, Field\n"
                f"    Attribute, Meta, MetaList, MetaNameValue\n"
                f"    Ident, Type, TypePath, Generics, GenericParam\n"
                f"    Path, PathSegment, PathArguments, AngleBracketedGenericArguments\n"
                f"    Expr, ExprLit, Lit, LitStr, LitInt, LitBool\n"
                f"    syn::Error (for compile-time errors via .to_compile_error())\n"
                f"    Span comes from proc_macro2::Span, NOT syn::Span\n"
                f"\n  NOT AVAILABLE without features = [\"full\"]:\n"
                f"    ItemEnum, ItemFn, ItemStruct, ItemImpl — DO NOT USE\n"
                f"    PatIdent, Stmt, Block, Arm — DO NOT USE\n"
                f"\n  STRUCTURE rules:\n"
                f"  - Entry-point fn MUST be tagged `#[proc_macro_derive(TraitName)]` or `#[proc_macro_attribute]`.\n"
                f"  - proc-macro crates can ONLY export #[proc_macro_derive]/#[proc_macro_attribute]/#[proc_macro] fns.\n"
                f"  - ALL other functions and types MUST be private (no `pub`). Example:\n"
                f"      WRONG: `pub fn helper(...)` or `pub struct Foo` — these cause 'cannot export' compile error\n"
                f"      RIGHT: `fn helper(...)` or `struct Foo` — private, no pub keyword\n"
                f"  - Use `std::env` not bare `env::` for environment variables.\n"
                f"  - Do NOT define EarlyExit, FromArgs, SubCommand, SubCommands, or any argh runtime types here.\n"
                f"    These types are defined in the argh/argh_shared crates. This crate only GENERATES CODE that uses them.\n"
                f"    In generated quote! blocks, reference them as `argh::EarlyExit`, `argh::FromArgs`, etc.\n"
                f"  - Parse input as `DeriveInput` (not `ItemStruct`/`ItemEnum`/`ItemFn` — those require 'full' feature):\n"
                f"      let input = parse_macro_input!(input as DeriveInput);\n"
                f"      match input.data {{ Data::Struct(s) => ..., Data::Enum(e) => ..., _ => ... }}\n"
                f"  - `syn::AttributeArgs` does not exist in syn 2.x — parse attributes via `Attribute::parse_nested_meta`.\n"
                f"  - Iterating FieldsNamed: use `fields.named.iter()` not `fields.iter()`.\n"
                f"    FieldsNamed has a `.named` field (Punctuated<Field, Comma>), not a direct iterator.\n"
                f"  - Do NOT declare `extern crate proc_macro;` — it is implicit in proc-macro crates.\n"
                f"  - Runtime types from argh (CommandInfo, EarlyExit, FromArgs) must be referenced\n"
                f"    inside quote! blocks as `argh::CommandInfo`, `argh::EarlyExit`, etc. — do not import them.\n"
                f"  - Structs are data only — NO function signatures inside struct bodies.\n"
                f"  - References in struct fields MUST have lifetimes: `struct Foo<'a> {{ x: &'a Bar }}`\n"
                f"  - Do NOT use inline `mod helpers {{ }}` blocks — define helpers directly in lib.rs or in a separate helpers.rs file.\n"
                f"  - If a submodule file (errors.rs, helpers.rs) is generated, declare it as `mod errors;` (no braces) in lib.rs.\n"
                f"  - Every `mod foo;` declaration needs a corresponding foo.rs file.\n"
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
            prompt = f"""You are implementing a software project from functional requirements.

Process (genesis.md):
{genesis}
{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{pitfalls_section}{rules_section}{api_section}{prev_source_section}
Functional requirements:
{functional_req}
{assertions_section}{whole_project_context}
Produce ALL source files needed to satisfy these requirements.
Every module/crate/package in the workspace structure above MUST have its required entry-point
file (e.g. each Rust crate declaring a lib target needs src/lib.rs; a bin target needs src/main.rs).
Do NOT output test files or any file that belongs in the project's test directories.

{FILE_FORMAT_INSTRUCTION}
Output ALL files one after another using only those markers.
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
                f"\n⚠️  RUST MODULE FILE RULES:\n"
                f"1. Each file (args.rs, dynamic_subcommand.rs, from_arg_value.rs) must contain module CONTENTS directly — NOT wrapped in `pub mod xyz {{ }}`. "
                f"Writing `pub mod args {{ ... }}` inside args.rs creates double nesting and won't compile.\n"
                f"2. EarlyExit must be defined ONCE in lib.rs as `pub struct EarlyExit {{ ... }}`. "
                f"Do NOT define EarlyExit in any submodule file.\n"
                f"3. Submodules reference it as `use crate::EarlyExit;` at the top of the file.\n"
                f"4. Types imported directly from the crate root "
                f"(CommandInfoWithArgs, FlagInfo, FlagInfoKind, Optionality, SubCommandInfo) "
                f"must be defined with real fields in lib.rs — not empty stubs.\n"
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
        _plan_fixed_size = (
            len(_genesis_truncated_plan) + len(profile_section) + len(workspace_section) +
            len(module_scope_section) + len(locked_dep_section) + len(build_error_section) +
            len(pitfalls_section_llm) + len(rules_section_llm) + len(api_section) +
            len(assertions_section_llm) + 2_000
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
            _log(f"  ERROR: model returned no file list.")
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
            prev_files_section = ""
            if all_files:
                parts = ["\nAlready-generated files for this module (for context — do NOT regenerate them):"]
                for prev_path, prev_content in all_files.items():
                    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content}\n</FILE>')
                prev_files_section = "\n".join(parts) + "\n"

            file_prompt = f"""You are implementing a single source file for a software project.

Process (genesis.md):
{trim_to_line(genesis, _GENESIS_CAP, "\n... (genesis truncated)")}
{profile_section}{module_scope_section}{locked_dep_section}{api_section}
Functional requirements:
{_functional_req_capped}
{_whole_ctx_capped}{prev_files_section}
Generate the file: `{file_path}`

{FILE_FORMAT_INSTRUCTION}
Only output the single file `{file_path}` using the markers above.
"""
            response = ask(file_prompt, model=_TASK2_MODEL)
            files = extract_files(response)
            if file_path in files:
                all_files[file_path] = files[file_path]
            else:
                preview = response.strip()[:200].replace('\n', '↵') if response else "(empty)"
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
                _is_proc_macro = bool(re.search(r'proc-macro\s*=\s*true', _cargo_text, re.MULTILINE))

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
            prev_files_section = ""
            if all_files:
                parts = ["\nAlready-generated files for this module (do NOT regenerate):"]
                for prev_path, prev_content in all_files.items():
                    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content}\n</FILE>')
                prev_files_section = "\n".join(parts) + "\n"

            proc_macro_note = ""
            if _is_proc_macro:
                proc_macro_note = f"""
⚠️  PROC-MACRO CRATE (syn = "2.0", default features only):

IMPORTS for lib.rs:
  use proc_macro::TokenStream;
  use proc_macro2::{{TokenStream as TokenStream2, Span}};
  use syn::{{parse_macro_input, DeriveInput, Data, DataStruct, DataEnum, Fields,
            FieldsNamed, FieldsUnnamed, Field, Ident, Type, Attribute, Meta,
            Expr, Lit, LitStr, LitInt, Path, Generics, Error as SynError}};
  use syn::spanned::Spanned;
  use quote::{{quote, quote_spanned, ToTokens}};

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
  - References in struct fields MUST have lifetimes: `struct Foo<'a> {{ x: &'a Bar }}`
  - Iterate FieldsNamed: `fields.named.iter()` (NOT `fields.iter()`)
  - `syn::AttributeArgs` removed in syn 2.x — parse attrs via `Attribute::parse_nested_meta`
  - Do NOT use inline `mod helpers {{ }}` blocks — helper functions go directly in the file.
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

            # Split test files.
            _primary_tests: list[tuple[str, str]] = []
            _secondary_tests: list[tuple[str, str]] = []
            for _rel in sorted(existing_test_files):
                if module_dir_rel and not _rel.startswith(module_dir_rel):
                    continue
                _tp = project_dir / _rel
                if not _tp.exists():
                    continue
                _content = _tp.read_text(encoding="utf-8")
                _rel_stem = Path(_rel).name.split('.')[0]
                if _rel_stem == _file_stem or (_is_entry and _rel.startswith("tests/")):
                    _primary_tests.append((_rel, _content))
                else:
                    _secondary_tests.append((_rel, _content))

            # Build primary test section (always included in full).
            _primary_test_parts = ["\nTest files — primary (must pass for this file):"]
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
            _w_ctx = smart_truncate(whole_project_context, total_limit=SINGLE_CALL_CHAR_LIMIT // 4,
                                    head=SINGLE_CALL_CHAR_LIMIT // 16,
                                    tail=3 * SINGLE_CALL_CHAR_LIMIT // 16)

            # Measure primary content to compute remaining fill budget.
            _primary_size = (
                len(_genesis_section)
                + len(profile_section)
                + len(strategy_section)
                + len(_primary_api)
                + len(_primary_test_section)
                + len(_f_req)
                + len(_w_ctx)
                + len(prev_files_section)
                + 3_000  # prompt scaffolding overhead
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

            _sec_test_parts: list[str] = []
            if _secondary_tests and _fill_used < _fill_budget:
                _sec_test_parts.append("\nTest files — additional context:")
                for _rel, _content in _secondary_tests:
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
{FILE_FORMAT_INSTRUCTION}
Output ONLY the file `{file_path}` using the markers above. Nothing else.
{_genesis_section}{profile_section}{workspace_section}{strategy_section}{api_section}
⚠️  MODULE SCOPE: This file belongs to the `{current_module}` crate.
    Path: `{file_path}`
{proc_macro_note}{locked_dep_section}{build_error_section}
Functional requirements:
{_f_req}
{test_files_section}{_w_ctx}{prev_files_section}
Generate the complete implementation for `{file_path}`.
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
                try:
                    pending = _topo_sort_files(pending, all_files)
                except Exception:
                    pass  # cycle or error — keep current order

        if not all_files:
            _log("  ERROR: no files generated.")
            return False

        written = write_source_files(all_files, project_dir, manifest_path, existing_test_files)
        _log(f"  {written} source file(s) written.")
        return True


def _task2_generator_factory() -> Task2Generator:
    backend = os.getenv("TASK2_GENERATOR", "filelist").lower()
    if backend == "llm":
        return LLMTask2Generator()
    if backend == "static":
        return StaticTask2Generator()
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
    """
    Generate source files from functional requirements.
    Implementation is selected by TASK2_GENERATOR in .env (default: static).
    """
    return _task2_generator_factory().generate(
        project_dir, genesis_path, functional_req_path, manifest_path,
        existing_test_files, project_profile, workspace_structure, build_error,
        locked_dep_context, current_module, module_dir_rel,
        whole_project_context=whole_project_context,
        coverage_strategy=coverage_strategy,
        api_surface=api_surface,
        prev_source_context=prev_source_context,
    )


def task2_inner_loop(project_dir, genesis_path, functional_req_path, manifest_path,
                     existing_test_files, project_profile, workspace_structure,
                     build_check_cmd: str | None, locked_dep_context: str = "",
                     current_module: str = "", module_dir_rel: str = "",
                     whole_project_context: str = "", coverage_strategy: str = "",
                     api_surface: str = "", prev_source_context: str = ""):
    """
    Inner loop: TASK 2 (code generation) followed by a compile-only check.
    If the build check fails, feeds the compiler error back into TASK 2 and retries.
    Avoids burning full outer-loop iterations on structural code errors.

    Returns True if code was generated and (if a build check exists) compiles cleanly.
    Returns False if all retries are exhausted.
    """
    # Pre-detect proc-macro so fix-loop can pass the constraint as extra_context.
    _fix_extra_context = ""
    if module_dir_rel:
        _cargo_toml_check = project_dir / module_dir_rel / "Cargo.toml"
        if _cargo_toml_check.exists():
            _ct = _cargo_toml_check.read_text(encoding="utf-8")
            if re.search(r'proc-macro\s*=\s*true', _ct, re.MULTILINE):
                _fix_extra_context = (
                    "\n⚠️  PROC-MACRO CRATE: `#[proc_macro_derive]`, `#[proc_macro]`, and "
                    "`#[proc_macro_attribute]` functions MUST be at the TOP LEVEL of lib.rs — "
                    "NEVER inside any `mod` block. Do NOT wrap the file in `mod lib { ... }`.\n"
                )

    build_error = ""
    _t2_t0 = time.monotonic()
    _emit("task.start", task="task2", module=current_module,
          data={"max_attempts": BUILD_CHECK_RETRIES,
                "has_build_check": bool(build_check_cmd)})
    for attempt_num in range(1, BUILD_CHECK_RETRIES + 1):
        if attempt_num > 1:
            _log(f"  Inner loop attempt {attempt_num}/{BUILD_CHECK_RETRIES} "
                 f"(fixing build error from previous attempt)...")

        ok = task2(project_dir, genesis_path, functional_req_path, manifest_path,
                   existing_test_files, project_profile, workspace_structure, build_error,
                   locked_dep_context, current_module, module_dir_rel,
                   whole_project_context=whole_project_context,
                   coverage_strategy=coverage_strategy,
                   api_surface=api_surface,
                   prev_source_context=prev_source_context if attempt_num == 1 else "")
        if not ok:
            # When module scope filtering discards all planned files the model listed
            # files for the wrong crates. Inject an explicit corrective error and retry
            # rather than aborting — the model needs one more chance with clear guidance.
            if module_dir_rel and attempt_num < BUILD_CHECK_RETRIES:
                scope_msg = (
                    f"CRITICAL SCOPE ERROR: Every file path you output was WRONG and was discarded.\n"
                    f"You MUST ONLY generate files for the `{current_module}` crate inside this Cargo workspace.\n"
                    f"Every file path MUST start with `{module_dir_rel}` (note: this includes the `src/` subdirectory).\n"
                    f"CORRECT examples:  {module_dir_rel}src/lib.rs  |  {module_dir_rel}src/parse_attrs.rs\n"
                    f"WRONG examples:    src/lib.rs  |  src/{current_module}/lib.rs  |  {current_module}/lib.rs\n"
                    f"Output ONLY files whose path starts with `{module_dir_rel}`."
                )
                _log(f"  TASK 2 produced no in-scope files — injecting scope correction for retry...")
                build_error = scope_msg
                continue
            _log("  TASK 2 failed to generate files — aborting inner loop.")
            _emit("task.done", task="task2", module=current_module,
                  duration_ms=int((time.monotonic() - _t2_t0) * 1000),
                  attempt=attempt_num,
                  data={"ok": False, "reason": "no_files_generated"})
            return False

        if not build_check_cmd:
            _emit("task.done", task="task2", module=current_module,
                  duration_ms=int((time.monotonic() - _t2_t0) * 1000),
                  attempt=attempt_num,
                  data={"ok": True, "build_checked": False})
            return True  # No build check available; proceed to full tests.

        # Use generate_fix_loop: run build check, if it fails ask LLM to fix only
        # the broken files rather than regenerating everything on the next attempt.
        # Keeps files that compiled correctly, targets only the broken ones.
        generated_paths = [
            project_dir / rel for rel in load_manifest(manifest_path)
            if rel not in existing_test_files and (project_dir / rel).exists()
        ]
        build_ok, build_output = generate_fix_loop(
            project_dir=project_dir,
            check_cmd=build_check_cmd,
            source_paths=generated_paths,
            existing_test_files=existing_test_files,
            extra_context=_fix_extra_context,
            model=_TASK2_MODEL,
            max_fix_attempts=4,
            timeout=BUILD_TIMEOUT,
            allowed_dir_prefix=module_dir_rel,
        )
        if build_ok:
            _log("  Build check PASSED.")
            _emit("compile.success", module=current_module, attempt=attempt_num)
            _emit("task.done", task="task2", module=current_module,
                  duration_ms=int((time.monotonic() - _t2_t0) * 1000),
                  attempt=attempt_num,
                  data={"ok": True, "build_checked": True})
            return True

        _log(f"  Build still failing after fix attempts (inner loop attempt {attempt_num}).")
        _emit("compile.fail", module=current_module, attempt=attempt_num,
              data={"output": _store_prompt_content(build_output or "")})
        # Pass the error into the next full task2 regeneration so the model
        # understands what structural issues need to be addressed.
        build_error = build_output

    _log(f"  Inner loop exhausted after {BUILD_CHECK_RETRIES} attempts — escalating to outer loop.")
    _emit("task.done", task="task2", module=current_module,
          duration_ms=int((time.monotonic() - _t2_t0) * 1000),
          data={"ok": False, "reason": "retries_exhausted"})
    return False


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
) -> tuple[bool, str]:
    """
    Shared iterative fix loop for any code generation step.

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
            fns = sum(1 for l in text.splitlines() if fn_re.match(l))
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

    env = _cargo_env()
    last_output = ""
    prev_sigs: frozenset = frozenset()
    attempt = 0
    _attempt_history: list[str] = []  # running narrative of what was tried and what happened

    while True:
        # Run the check.
        try:
            result = subprocess.run(
                _parse_cmd(check_cmd), cwd=project_dir,
                capture_output=True, text=True, timeout=timeout, env=env,
            )
            last_output = result.stdout + (
                "\n--- stderr ---\n" + result.stderr if result.stderr else ""
            )
            if result.returncode == 0:
                if attempt > 0:
                    _log(f"  [fix loop] Check PASSED after fix attempt {attempt}.")
                return True, last_output
        except subprocess.TimeoutExpired:
            last_output = f"TIMEOUT after {timeout}s"
            return False, last_output

        cur_sigs = _error_sigs(last_output)
        _log_compile_overview(last_output, attempt)
        if attempt > 0:
            resolved = prev_sigs - cur_sigs
            if cur_sigs == prev_sigs:
                _log(f"  [fix loop] No error improvement after attempt {attempt} — giving up.")
                break
            _log(f"  [fix loop] {len(resolved)} error(s) resolved, {len(cur_sigs)} remaining — continuing.")

        # Graceful stop check — mirrors task0c behaviour: finish current attempt, then exit.
        _graceful_sentinel = Path("/tmp/pipeline_graceful_stop")
        if _graceful_sentinel.exists():
            try:
                _graceful_sentinel.unlink()
            except OSError:
                pass
            _log(f"  [fix loop] Graceful stop requested — attempt {attempt} complete. Exiting.")
            sys.exit(0)

        attempt += 1
        prev_sigs = cur_sigs

        # BL-025: record the error category before attempting a fix.
        _fix_error_before = last_output

        _log(f"  [fix loop] Attempt {attempt} — asking LLM to fix specific failures.")

        # Build running narrative of prior fix attempts for this loop.
        _history_section = ""
        if _attempt_history:
            n = len(_attempt_history)
            _history_section = (
                f"You have already made {n} fix attempt{'s' if n != 1 else ''} in this loop. "
                f"Here is what happened — do NOT repeat the same changes:\n\n"
                + "\n\n".join(_attempt_history)
                + "\n\nNow make a different fix that avoids every approach above.\n"
            )

        # Build source context — read current content from disk in full.
        # Truncating source files or capping total size causes the model to fix errors
        # in code it cannot see, often producing new errors. Pass all files completely.
        source_block = ""
        for p in source_paths:
            rel = str(p.relative_to(project_dir))
            if rel in existing_test_files or not p.exists():
                continue
            content = p.read_text(encoding="utf-8")
            source_block += f'\n<FILE path="{rel}">\n{content}\n</FILE>\n'

        # Fill-down: extra_context may be large (test files + requirements from
        # task2_fix_loop) or small (proc-macro rules). Give it the full budget minus
        # a minimum reservation for error and source, then fill error and source
        # with what remains.
        _fl_overhead = 3_000
        _fl_min_err  = 10_000
        _fl_min_src  = 30_000
        _extra_budget = max(0, SINGLE_CALL_CHAR_LIMIT - _fl_overhead - _fl_min_err - _fl_min_src)
        _extra_ctx_capped = smart_truncate(extra_context, total_limit=_extra_budget,
                                           head=_extra_budget // 3, tail=2 * _extra_budget // 3)
        _after_extra = max(_fl_min_err + _fl_min_src,
                           SINGLE_CALL_CHAR_LIMIT - _fl_overhead - len(_extra_ctx_capped))
        _err_cap = _after_extra // 3   # errors are concise; 1/3 is generous
        _src_cap = _after_extra * 2 // 3  # source gets the larger share
        # Errors: tail-weighted (most recent matters most). Source: head-weighted
        # (imports/type defs at the top inform the fix).
        _last_output_capped = smart_truncate(last_output, total_limit=_err_cap,
                                             head=_err_cap // 4, tail=3 * _err_cap // 4)
        _source_block_capped = smart_truncate(source_block, total_limit=_src_cap,
                                              head=_src_cap // 3, tail=2 * _src_cap // 3)
        prompt = f"""Code generation produced files that fail the check below.
Fix ONLY the files causing the failure — do NOT regenerate correct files.
{_history_section}
Error output:
```
{_last_output_capped}
```

Current source files:
{_source_block_capped}
{_extra_ctx_capped}
{FILE_FORMAT_INSTRUCTION}
"""
        response = ask(prompt, model=model)
        fixed = extract_files(response)

        if not fixed:
            _log(f"  [fix loop] LLM produced no fixes on attempt {attempt + 1}.")
            continue

        for rel, content in fixed.items():
            if rel in existing_test_files:
                continue
            if Path(rel).suffix not in SOURCE_EXTENSIONS:
                continue
            # Reject files outside the allowed module directory (prevents cross-module corruption).
            if allowed_dir_prefix and not rel.startswith(allowed_dir_prefix):
                _log(f"  [fix loop] REJECTED {rel}: outside allowed dir '{allowed_dir_prefix}'.")
                continue
            # Reject content that looks like compiler error output, not source code.
            stripped = content.lstrip()
            if re.match(r'^error(\[E\d+\])?:', stripped) or re.match(r'^warning(\[W\d+\])?:', stripped):
                _log(f"  [fix loop] REJECTED {rel}: content looks like compiler output, not code.")
                continue
            write(project_dir / rel, content)
            _log(f"  [fix loop] Fixed: {rel}")

        # Record this attempt in the running narrative for subsequent fix prompts.
        _err_capped = last_output[-_FIX_ERR_CAP:] if len(last_output) > _FIX_ERR_CAP else last_output
        _files_changed = ", ".join(list(fixed.keys())[:4]) if fixed else "no files changed"
        _attempt_history.append(
            f"Attempt {attempt}: changed [{_files_changed}] → still failing.\n"
            f"Error was:\n{_err_capped}"
        )

        # BL-025: if the next check passes, record what error was fixed and how.
        # We check after writing files; if the loop continues, the next iteration's
        # initial check will detect success and we'll have stored the pattern.
        if fixed:
            _bl025_module = allowed_dir_prefix.rstrip('/').split('/')[-2] if allowed_dir_prefix else ""
            if _bl025_module:
                _error_category = _categorize_compile_error(_fix_error_before)
                _fix_hint = "; ".join(f"{p}" for p in list(fixed.keys())[:3])
                compile_patterns_write(
                    project_dir, _bl025_module, _error_category,
                    example=_fix_error_before[:200],
                    fix_hint=f"fixed in {_fix_hint}",
                )

    _log(f"  [fix loop] All {max_fix_attempts} fix attempts exhausted.")
    return False, last_output


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
    """
    After TASK 3 failure: targeted fix before full TASK 4 + regeneration.
    Thin wrapper around generate_fix_loop using the manifest's source files.
    """
    current_files = load_manifest(manifest_path)
    if not current_files:
        return False, test_output

    source_paths = [
        project_dir / rel for rel in current_files
        if rel not in existing_test_files and (project_dir / rel).exists()
    ]

    functional_req = read(functional_req_path)

    # Fill-down for extra_context: genesis → functional_req → test files (stem-prioritized).
    # generate_fix_loop gives extra_context the full budget minus minimums for error+source.
    _extra_budget = max(10_000, SINGLE_CALL_CHAR_LIMIT - 3_000 - 10_000 - 30_000)

    # 1. Genesis — methodology, always first.
    _genesis_section = ""
    if genesis_path and genesis_path.exists():
        _genesis = read(genesis_path)
        if _genesis.strip():
            _genesis_section = (
                f"\n--- genesis.md (methodology) ---\n"
                f"{trim_to_line(_genesis, _GENESIS_CAP, chr(10) + '... (genesis truncated)')}\n"
            )

    # 2. Functional requirements — fill after genesis.
    _req_budget = max(0, _extra_budget - len(_genesis_section) - 2_000)
    _req_truncated = smart_truncate(functional_req, total_limit=_req_budget,
                                    head=_req_budget // 3, tail=2 * _req_budget // 3)
    _req_section = f"\nFunctional requirements:\n{_req_truncated}\n"

    # 3. Test files — stem-prioritized, fill remaining budget.
    _test_budget = max(0, _extra_budget - len(_genesis_section) - len(_req_section))
    _file_stem = Path(module_dir_rel.rstrip("/")).name if module_dir_rel else ""
    _primary_tests: list[str] = []
    _secondary_tests: list[str] = []
    for _rel in sorted(existing_test_files):
        if module_dir_rel and not _rel.startswith(module_dir_rel):
            continue
        _tp = project_dir / _rel
        if not _tp.exists():
            continue
        _blk = f'\n<TESTFILE path="{_rel}">\n{_tp.read_text(encoding="utf-8")}\n</TESTFILE>'
        if _file_stem and Path(_rel).name.split(".")[0] == _file_stem:
            _primary_tests.append(_blk)
        else:
            _secondary_tests.append(_blk)
    _test_parts = ["\nTest files (the tests your fix must pass):"]
    _test_used = 0
    for _blk in _primary_tests + _secondary_tests:
        if _test_used + len(_blk) > _test_budget:
            break
        _test_parts.append(_blk)
        _test_used += len(_blk)
    _test_section = ("\n".join(_test_parts) + "\n") if len(_test_parts) > 1 else ""

    extra = _genesis_section + _req_section + _test_section

    return generate_fix_loop(
        project_dir=project_dir,
        check_cmd=test_command,
        source_paths=source_paths,
        existing_test_files=existing_test_files,
        extra_context=extra,
        model=_TASK2_MODEL,
        max_fix_attempts=TASK2_FIX_RETRIES,
        timeout=TEST_TIMEOUT,
    )


def task3(project_dir, test_command, iteration, output_dir, genesis_path):
    _log(f"\nTASK 3: Running tests (iteration {iteration})")
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
        _pass_n, _fail_n = _parse_test_counts(test_output)
        _log(f"  Tests {'PASSED' if passed else 'FAILED'}. Output -> {output_file}")
        _emit("test.pass" if passed else "test.fail",
              duration_ms=int((time.monotonic() - _t_start) * 1000),
              data={"returncode": result.returncode,
                    "pass_count": _pass_n, "fail_count": _fail_n,
                    "output": _store_prompt_content(test_output)})
        done["data"] = {"passed": passed, "pass_count": _pass_n, "fail_count": _fail_n}
        return passed, test_output


def task4(genesis_path, functional_req_path, test_output, iteration, output_dir,
          failure_type: str = "test", failure_log: str = "",
          prev_pass_count: int = 0, prev_test_output: str = "",
          source_context: str = "", api_surface: str = ""):
    """
    Analyze failures and update genesis.md.

    failure_type: "build" (code didn't compile) or "test" (tests failed at runtime).
      - build failures: the model is asked to identify what STRUCTURAL information the
        requirements missed (workspace layout, entry-point files, module declarations).
        genesis.md is updated to require capturing that structural class of information.
      - test failures: the model performs a full reasoning-gap analysis and updates
        genesis.md with general lessons about what categories of test signal were missed.

    failure_log: running record of previous iteration outcomes. Fed in so the model
      can avoid repeating approaches that already failed.

    prev_pass_count / prev_test_output: when provided by the caller (per-module mode),
      used directly for regression comparison instead of reading tests_output_{N-1}.txt.
    """
    # BL-019: map sub-categories back to the two TASK 4 paths.
    _is_build = (failure_type == "build")
    _log(f"\nTASK 4: Analyzing failure [{failure_type}], updating genesis.md")
    _t4_t0 = time.monotonic()
    _emit("task.start", task="task4",
          attempt=iteration if isinstance(iteration, int) else None,
          data={"failure_type": failure_type, "iteration": iteration,
                "prev_pass_count": prev_pass_count})

    genesis = read(genesis_path)
    functional_req = read(functional_req_path)
    # Fill-down: genesis and functional_req are typically small (10-20k each).
    # Include them in full (up to hard safety caps), then give ALL remaining budget
    # to test_output — the actual failure signal the model must reason about.
    _t4_genesis_cap = INTERMEDIATE_CHAR_LIMIT
    _t4_req_cap     = SINGLE_CALL_CHAR_LIMIT // 3
    # genesis: head-weighted (methodology intro at top). req: head-weighted (core types first).
    genesis        = smart_truncate(genesis, total_limit=_t4_genesis_cap,
                                    head=2 * _t4_genesis_cap // 3, tail=_t4_genesis_cap // 3)
    functional_req = smart_truncate(functional_req, total_limit=_t4_req_cap,
                                    head=_t4_req_cap // 3, tail=2 * _t4_req_cap // 3)
    # test_output gets everything left after genesis + req + overhead.
    _t4_output_cap = max(10_000,
                         SINGLE_CALL_CHAR_LIMIT - len(genesis) - len(functional_req) - 10_000)
    # test_output: tail-weighted (most recent failures are most actionable).
    truncated_output = smart_truncate(test_output, total_limit=_t4_output_cap,
                                      head=_t4_output_cap // 4, tail=3 * _t4_output_cap // 4)

    # Regression / comparison section.
    # Prefer caller-supplied prev data (per-module mode); fall back to file lookup (legacy mode).
    current_pass = count_passing_tests(test_output)
    comparison_section = ""

    _prev_output = prev_test_output
    _prev_pass = prev_pass_count
    if not _prev_output and isinstance(iteration, int) and iteration > 1:
        prev_file = Path(output_dir) / f"tests_output_{iteration - 1}.txt"
        prev_content = read(prev_file)
        if prev_content:
            _prev_output = extract_test_output_from_file(prev_content)
            _prev_pass = count_passing_tests(_prev_output)

    if _prev_output:
        _t4_used = len(genesis) + len(functional_req) + len(truncated_output)
        _t4_secondary_budget = max(5_000, SINGLE_CALL_CHAR_LIMIT - _t4_used - 10_000)
        _prev_cap = _t4_secondary_budget // 2
        prev_truncated = smart_truncate(_prev_output, total_limit=_prev_cap,
                                        head=_prev_cap // 4, tail=3 * _prev_cap // 4)
        delta = current_pass - _prev_pass
        trend = (f"IMPROVED by {delta} tests" if delta > 0
                 else f"REGRESSED by {abs(delta)} tests" if delta < 0
                 else "NO CHANGE")
        comparison_section = f"""
Previous iteration result: {_prev_pass} tests passing.
Current iteration result: {current_pass} tests passing.
Trend: {trend}.

{"⚠️  The last genesis.md change made things WORSE. You must take a completely different approach — do not refine the previous change, reverse it and try something different." if delta < 0 else ""}
{"⚠️  The last genesis.md change had NO EFFECT. The previous approach is not working. Try a substantively different reasoning strategy." if delta == 0 else ""}

Previous test output (for comparison):
{prev_truncated}
"""

    # failure_log shares the secondary budget with prev_output.
    _t4_used2 = len(genesis) + len(functional_req) + len(truncated_output)
    _t4_log_budget = max(5_000, SINGLE_CALL_CHAR_LIMIT - _t4_used2 - 10_000) // 2
    _t4_failure_log_cap = _t4_log_budget
    failure_log_capped = smart_truncate(
        failure_log, total_limit=_t4_failure_log_cap,
        head=_t4_failure_log_cap // 4, tail=3 * _t4_failure_log_cap // 4,
    )
    failure_log_section = (
        f"\nYou have already attempted to fix this {failure_type} failure multiple times. "
        f"Here is the history of what was tried and what happened each time — "
        f"do NOT repeat any approach that already failed. Each iteration that did not improve "
        f"the pass count means that approach is wrong:\n{failure_log_capped}\n"
        f"Study this history carefully. The next update to genesis.md must take a substantively "
        f"different angle than anything already tried above.\n"
    ) if failure_log.strip() else ""

    # Different prompt depending on whether this is a build or test failure.
    delim_instruction = (
        f"IMPORTANT: Output the updated genesis.md ONLY between these exact delimiters — "
        f"no other format accepted:\n{GENESIS_DELIM_START}\n...content...\n{GENESIS_DELIM_END}\n"
    )

    source_section = (
        f"\nGenerated source files (the code that was compiled/tested):\n{source_context}\n"
    ) if source_context.strip() else ""

    # If a pre-strip API surface was captured, TASK 1 received it as structural
    # ground truth. The methodology in genesis.md must reflect that contract:
    # we are NOT asking TASK 1 to derive signatures from tests anymore -- only
    # behaviour. A regression caused by signature drift is a TASK 2 / LLM
    # discipline issue, not a methodology gap that genesis should chase.
    api_surface_section = ""
    if api_surface and api_surface.strip():
        api_surface_section = (
            "\n⚠️  CONTEXT — pipeline contract has changed:\n"
            "TASK 1 received an explicit API surface (signatures + type definitions)\n"
            "captured before strip. That is structural GROUND TRUTH. The methodology\n"
            "you are updating must therefore focus on BEHAVIOURAL extraction from tests\n"
            "(what each function returns, invariants, error paths) — not on rediscovering\n"
            "signatures or struct shapes from test-call patterns. If the failure looks\n"
            "like the LLM ignored the provided API surface (wrong signature, missing\n"
            "pub item, renamed field), the lesson is for the GENERATION step's rule set,\n"
            "not a new test-extraction rule.\n"
        )

    # BL-019: build sub-category hint for the prompt.
    _failure_hint = {
        "assertion": "Tests ran. The failure is an ASSERTION mismatch — the function returned "
                     "a wrong value or the behavior does not match what the test expects. "
                     "Focus on: what was the expected value vs. actual, and what requirement "
                     "was incomplete or incorrect.",
        "panic":     "Tests ran but the code PANICKED (not from an assertion). This typically "
                     "means an unwrap() on None/Err, index out of bounds, or an invariant "
                     "violation. Focus on: what invariant or error condition was not captured "
                     "in the requirements.",
        "timeout":   "The test runner TIMED OUT. This usually means an infinite loop, "
                     "deadlock, or unexpectedly slow operation. Focus on: what algorithmic "
                     "or termination condition was missing from the requirements.",
        "no_tests":  "ZERO tests ran despite test files existing. This is a test-harness "
                     "misconfiguration — check module declarations, feature flags, and "
                     "test discovery conventions.",
    }.get(failure_type, "")

    if _is_build:
        prompt = f"""You are refining a methodology for translating test suites into functional requirements.
{delim_instruction}
The implementation failed to COMPILE this iteration — the tests could not even run.
This is a STRUCTURAL failure: the generated code was missing required files, had wrong
module declarations, or had incorrect workspace layout. This is NOT a logic error.
{api_surface_section}
Your task: update genesis.md so that its methodology explicitly captures the structural
information that leads to correct compilation. The methodology must teach readers how to
derive workspace layout, required entry-point files, and module structure from tests and
README alone — as general rules, not project-specific fixes.

genesis.md (current methodology):
{genesis}

functional_requirements.md (requirements that led to broken code):
{functional_req}
{source_section}
Compiler/build output (what broke):
{truncated_output}
{comparison_section}{failure_log_section}
Analyze in this order:
1. What structural information was absent from functional_requirements.md that caused the build to fail?
2. Which step in genesis.md failed to extract or require that structural information?
3. What general rule should genesis.md add so that any project's workspace structure,
   entry-point files, and module declarations are always captured in requirements?

Produce an updated genesis.md. Rules must be:
- General and language-agnostic (not "always generate src/lib.rs" but "for any module
  system, identify and require every declared entry-point file")
- Never mention specific tests, files, iteration numbers, or this experiment
- More precise and complete than the current version

Output the updated genesis.md between these exact delimiters (required — do not use any other format):

{GENESIS_DELIM_START}
updated genesis.md content here
{GENESIS_DELIM_END}
"""
    else:
        failure_hint_section = (
            f"\n⚠️  Failure sub-type: {failure_type.upper()}\n{_failure_hint}\n"
        ) if _failure_hint else ""
        prompt = f"""You are refining a general methodology for translating test suites into functional requirements.
{delim_instruction}
genesis.md is not a patch document — it is a project-independent, language-agnostic reasoning
process that, when mature, should work on any test suite without needing iteration. Every update
must move it toward greater generality and precision, not toward narrow fixes for the specific
failures just observed.
{api_surface_section}{failure_hint_section}
genesis.md (the reasoning process followed this iteration):
{genesis}

functional_requirements.md (produced by that process):
{functional_req}
{source_section}
Test output (iteration {iteration}):
{truncated_output}
{comparison_section}{failure_log_section}
Analyze in this order:
1. Which tests failed and what do the failures reveal about what was missing or wrong in
   the functional requirements?
2. Trace each gap back to the reasoning process in genesis.md: which step produced an
   incomplete or incorrect requirement, and why?
3. What is the general CLASS of reasoning error — not "I missed function X" but
   "the process did not account for category Y of test signal"?
4. How should genesis.md be updated to permanently close that class of error for
   any future project, in any language?

Produce an updated genesis.md. It must:
- Remain a general, language-agnostic methodology, not a list of project-specific patches
- Be more precise and complete than the previous version
- Reflect the class of error, not just the symptom
- Never mention specific tests, test file names, test failures, iteration numbers, or
  the iterative process by which it was built

Output the updated genesis.md between these exact delimiters (required — do not use any other format):

{GENESIS_DELIM_START}
updated genesis.md content here
{GENESIS_DELIM_END}
"""

    def attempt():
        response = ask(prompt, model=_TASK4_MODEL)
        content = _extract_genesis(response)
        if content is None:
            _log(f"  WARNING: {MODEL} did not return an updated genesis.md.")
            _log(f"  Raw response snippet: {response[:600]}")
            return False
        write(genesis_path, content)
        _log("  genesis.md updated.")
        return True

    ok = with_retry(attempt)
    if not ok:
        _log("  task4 failed after all retries -- genesis.md unchanged this iteration.")

    # BL-024: write structured rules to the pipeline knowledge store.
    # Extract bullet-point rules from the new genesis.md content.
    if ok:
        new_genesis = read(genesis_path)
        _task4_extract_and_store_rules(
            genesis_path.parent, new_genesis,
            module_scope="*",  # genesis rules are global
            failure_type=failure_type,
            iteration=iteration if isinstance(iteration, int) else 0,
        )

    _emit("task.done", task="task4",
          attempt=iteration if isinstance(iteration, int) else None,
          duration_ms=int((time.monotonic() - _t4_t0) * 1000),
          data={"ok": ok, "current_pass_count": current_pass,
                "failure_type": failure_type})
    return ok, current_pass


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


def task_genesis_audit(genesis_path: Path) -> bool:
    """
    Ask the LLM to review genesis.md for internal contradictions or redundant
    rules and return a cleaned version if issues are found.

    Returns True if genesis.md was updated, False if it was clean or the call
    failed. Called periodically (every GENESIS_AUDIT_INTERVAL iterations) or
    immediately after a regression is detected.
    """
    genesis = read(genesis_path)
    if len(genesis.strip()) < 200:
        _log("  Genesis audit: document too short to audit.")
        return False

    prompt = f"""Review this methodology document for internal contradictions, redundant rules,
or rules that directly conflict with each other.

genesis.md:
{genesis}

If no issues found, respond with exactly: AUDIT_CLEAN

If issues are found, output the corrected document with contradictions resolved
and redundant rules removed. Do not add new rules — only clean up existing ones.

{GENESIS_DELIM_START}
corrected content here
{GENESIS_DELIM_END}
"""

    def attempt():
        response = ask(prompt, model=_TASK4_MODEL)
        if 'AUDIT_CLEAN' in response:
            _log("  Genesis audit: no issues found.")
            return 'clean'
        content = _extract_genesis(response)
        if content:
            write(genesis_path, content)
            _log("  Genesis audit: contradictions resolved — genesis.md updated.")
            return 'updated'
        return False

    result = with_retry(attempt)
    return bool(result == 'updated')


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
        m = re.search(r'^name\s*=\s*"([^"]+)"', toml.read_text(), re.MULTILINE)
        if m:
            return m.group(1).replace("-", "_")
    return fallback.replace("-", "_")


def _run_tarpaulin(project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
    """
    Run cargo tarpaulin for one module.
    Returns (coverage_pct 0-100, files_list, covered_lines, coverable_lines).
    Returns (0.0, [], 0, 0) on any failure.
    """
    out_dir = Path("/tmp") / f"tarp_{module_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Exclude files based on current cluster:
    # - stable/default: exclude nightly.rs (cfg-excluded, not in coverage denominator)
    # - non-default cluster: use cluster's own exclusion list (may be empty to include nightly.rs)
    if _current_cluster is None or _current_cluster.is_default:
        _exclude_patterns = ["*/nightly.rs"]
    else:
        _exclude_patterns = list(_current_cluster.tarpaulin_exclude_files)
    cmd = [
        str(TARPAULIN_BIN), "tarpaulin",
        "-p", module_name,
        "--tests",         # run integration tests (tests/ dir) but exclude from coverage denominator
        "--no-fail-fast",  # produce report even if some tests fail at runtime
        "--out", "Json",
        "--output-dir", str(out_dir),
        "--skip-clean",
        "--exclude-files", "*/tests/*",   # measure src/ coverage only
        "--exclude-files", "build.rs",    # build scripts run at compile time, not under test
    ]
    for pat in _exclude_patterns:
        cmd += ["--exclude-files", pat]
    try:
        result = subprocess.run(
            cmd, cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=_cargo_env(),
        )
        json_path = out_dir / "tarpaulin-report.json"
        if not json_path.exists():
            _log(f"  tarpaulin: no JSON report produced. stderr: {result.stderr[:300]}")
            return 0.0, [], 0, 0
        data = json.loads(json_path.read_text())
        return (
            data.get("coverage", 0.0),
            data.get("files", []),
            data.get("covered", 0),
            data.get("coverable", 0),
        )
    except subprocess.TimeoutExpired:
        _log(f"  tarpaulin: timeout after {BUILD_TIMEOUT}s")
        return 0.0, [], 0, 0
    except Exception as e:
        _log(f"  tarpaulin: error: {e}")
        return 0.0, [], 0, 0


def _quick_file_coverage(project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
    """Per-file coverage snapshot. Delegates to the active CoverageProvider."""
    return _coverage_provider.measure_file(project_dir, module_name, rel_path)


def _quick_file_coverage_grcov(project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
    """
    Fast per-file coverage snapshot using grcov + LLVM instrumentation.
    Returns (covered, coverable) for rel_path only, or None on failure.
    """
    if not GRCOV_BIN.exists():
        return None

    profraw_dir = project_dir / "target" / "coverage" / f"{module_name}_quick"
    profraw_dir.mkdir(parents=True, exist_ok=True)
    for f in profraw_dir.glob("*.profraw"):
        f.unlink(missing_ok=True)

    env = _cargo_env()
    # Add -C instrument-coverage; do NOT set CARGO_INCREMENTAL=0 so incremental
    # artifacts from the compile-check step are reused where possible.
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -C instrument-coverage").strip()
    env["LLVM_PROFILE_FILE"] = str(profraw_dir / "%p-%m.profraw")

    try:
        subprocess.run(
            ["cargo", "test", "-p", module_name, "--no-fail-fast", "--", "--test-threads=1"],
            cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=env,
        )
        if not list(profraw_dir.glob("*.profraw")):
            return None

        out_json = Path("/tmp") / f"grcov_quick_{module_name}.json"
        cmd = [
            str(GRCOV_BIN),
            str(profraw_dir),
            "--binary-path", str(project_dir / "target" / "debug"),
            "--source-dir", str(project_dir),
            "--output-type", "coveralls",
            "--token", "unused",
            "--branch",
            "--ignore-not-existing",
            "--output-path", str(out_json),
            "--ignore", str(Path.home() / ".cargo" / "registry" / "*"),
            "--ignore", str(Path.home() / ".cargo" / "git" / "*"),
            "--ignore", str(Path.home() / ".rustup" / "*"),
            "--ignore", "*/target/*",
            "--ignore", "*/tests/*",
            "--ignore", "tests/*",
            "--ignore", "build.rs",
            "--ignore", "*.gen_tests_*.rs",   # pipeline-generated test siblings
            "--excl-start", "GRCOV_EXCL_START",
            "--excl-stop",  "GRCOV_EXCL_STOP",
            "--excl-line",  "GRCOV_EXCL_LINE",
        ]
        subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, timeout=60, check=True)

        data = json.loads(out_json.read_text())
        abs_target = str(project_dir / rel_path)
        for sf in data.get("source_files", []):
            sf_abs = str(project_dir / sf.get("name", ""))
            if sf_abs == abs_target or sf.get("name", "") == rel_path:
                cov_arr = sf.get("coverage", [])
                covered   = sum(1 for c in cov_arr if c is not None and c > 0)
                coverable = sum(1 for c in cov_arr if c is not None)
                return covered, coverable
        return None
    except Exception:
        return None


def _run_grcov(project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
    """
    Run grcov (LLVM source-based coverage) for one module.
    Returns (coverage_pct 0-100, files_list compatible with tarpaulin format, covered, coverable).
    Returns (0.0, [], 0, 0) on any failure.

    Requires:
      - rustup component add llvm-tools-preview
      - cargo install grcov
      - COVERAGE_BACKEND=grcov in .env
    """
    profraw_dir = project_dir / "target" / "coverage" / module_name
    profraw_dir.mkdir(parents=True, exist_ok=True)
    # Clean stale profraw files so old runs don't pollute results.
    for f in profraw_dir.glob("*.profraw"):
        f.unlink(missing_ok=True)

    env = _cargo_env()
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -C instrument-coverage").strip()
    env["LLVM_PROFILE_FILE"] = str(profraw_dir / "%p-%m.profraw")
    # Required for correct coverage: incremental compilation can reuse object files
    # compiled without -C instrument-coverage, causing integration test binaries to
    # produce no profraw data.
    env["CARGO_INCREMENTAL"] = "0"

    # Cluster exclude patterns reused from tarpaulin path.
    if _current_cluster is None or _current_cluster.is_default:
        _exclude_patterns = ["*/nightly.rs"]
    else:
        _exclude_patterns = list(_current_cluster.tarpaulin_exclude_files)

    try:
        # Step 1: run tests with instrumentation.
        result = subprocess.run(
            ["cargo", "test", "-p", module_name, "--no-fail-fast", "--", "--test-threads=1", "--no-capture"],
            cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=env,
        )
        if result.returncode != 0 and not list(profraw_dir.glob("*.profraw")):
            _log(f"  grcov: cargo test failed and produced no profraw: {result.stderr[:200]}")
            return 0.0, [], 0, 0

        # Step 2: run grcov to produce coveralls JSON.
        out_json = Path("/tmp") / f"grcov_{module_name}.json"
        cmd = [
            str(GRCOV_BIN),
            str(profraw_dir),
            "--binary-path", str(project_dir / "target" / "debug"),
            "--source-dir", str(project_dir),
            "--output-type", "coveralls",
            "--token", "unused",   # required by grcov 0.10+ for coveralls output
            "--branch",
            "--ignore-not-existing",
            "--output-path", str(out_json),
            # Exclude dependency and toolchain sources — only measure project code.
            "--ignore", str(Path.home() / ".cargo" / "registry" / "*"),
            "--ignore", str(Path.home() / ".cargo" / "git" / "*"),
            "--ignore", str(Path.home() / ".rustup" / "*"),
            "--ignore", "*/target/*",
            "--ignore", "*/build.rs",  # build scripts run at compile time, not under test
            "--ignore", "build.rs",    # root-level build.rs (no parent dir prefix)
            "--ignore", "*/tests/*",   # integration test files — measure src/ coverage only
            "--ignore", "tests/*",     # relative-path variant (grcov * doesn't cross /)
            "--ignore", "*.gen_tests_*.rs",  # pipeline-generated test siblings
            "--excl-start", "GRCOV_EXCL_START",
            "--excl-stop",  "GRCOV_EXCL_STOP",
            "--excl-line",  "GRCOV_EXCL_LINE",
        ]
        for pat in _exclude_patterns:
            cmd += ["--ignore", pat]
        subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True,
                       timeout=120, check=True)

        data = json.loads(out_json.read_text())
        source_files = data.get("source_files", [])

        total_covered = total_coverable = 0
        # Build files_data in tarpaulin-compatible format so all downstream
        # code (pragma pass, uncovered-line extraction) works unchanged.
        files_data = []
        for sf in source_files:
            cov_arr = sf.get("coverage", [])
            # null = not coverable, 0 = uncovered, N = hit count
            covered   = sum(1 for c in cov_arr if c is not None and c > 0)
            coverable = sum(1 for c in cov_arr if c is not None)
            total_covered   += covered
            total_coverable += coverable
            # Build traces list matching tarpaulin schema: [{line, stats:{Line:N}}]
            traces = [
                {"line": i + 1, "stats": {"Line": (c if c is not None else 0)}}
                for i, c in enumerate(cov_arr)
                if c is not None
            ]
            abs_path = str(project_dir / sf.get("name", ""))
            files_data.append({
                "path": abs_path.split(os.sep),  # tarpaulin-compat schema
                "abs_path": abs_path,             # reliable full path for consumers
                "traces": traces,
                "covered": covered,
                "coverable": coverable,
            })

        pct = (total_covered / total_coverable * 100.0) if total_coverable > 0 else 0.0
        return pct, files_data, total_covered, total_coverable

    except subprocess.TimeoutExpired:
        _log(f"  grcov: timeout after {BUILD_TIMEOUT}s")
        return 0.0, [], 0, 0
    except Exception as e:
        _log(f"  grcov: error: {e}")
        return 0.0, [], 0, 0


def _load_grcov_json(project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int] | None:
    """Parse the existing grcov JSON + filecache without re-running grcov.

    Returns (pct, files_data, covered, coverable) if the JSON exists, else None.
    Used on resume to restore coverage state without an expensive re-run.
    """
    out_json = Path("/tmp") / f"grcov_{module_name}.json"
    if not out_json.exists():
        return None
    try:
        main_mtime = out_json.stat().st_mtime
        data = json.loads(out_json.read_text())
        source_files = data.get("source_files", [])
        if not source_files:
            return None

        # Build per-file map from main JSON.
        file_map: dict[str, dict] = {}
        for sf in source_files:
            cov_arr = sf.get("coverage", [])
            covered   = sum(1 for c in cov_arr if c is not None and c > 0)
            coverable = sum(1 for c in cov_arr if c is not None)
            abs_path  = str(project_dir / sf.get("name", ""))
            traces    = [
                {"line": i + 1, "stats": {"Line": (c if c is not None else 0)}}
                for i, c in enumerate(cov_arr) if c is not None
            ]
            file_map[sf.get("name", "")] = {
                "path": abs_path.split(os.sep),
                "abs_path": abs_path,
                "traces": traces,
                "covered": covered,
                "coverable": coverable,
            }

        # Merge filecache entries newer than the main JSON (written by quick-cov).
        filecache = Path("/tmp") / f"grcov_filecache_{module_name}.json"
        if filecache.exists():
            try:
                for rel, d in json.loads(filecache.read_text()).items():
                    if d.get("ts", 0) > main_mtime and d.get("coverable", 0) > 0 and rel in file_map:
                        entry = file_map[rel]
                        entry["covered"]   = d["covered"]
                        entry["coverable"] = d["coverable"]
            except Exception:
                pass

        files_data = list(file_map.values())
        total_covered   = sum(f["covered"]   for f in files_data)
        total_coverable = sum(f["coverable"] for f in files_data)
        pct = (total_covered / total_coverable * 100.0) if total_coverable > 0 else 0.0
        return pct, files_data, total_covered, total_coverable
    except Exception:
        return None


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


# ── Coverage provider interface ───────────────────────────────────────────────

def _cluster_exclude_patterns() -> list[str]:
    if _current_cluster is None or _current_cluster.is_default:
        return ["*/nightly.rs"]
    return list(_current_cluster.tarpaulin_exclude_files)


def _glob_to_filename_regex(pat: str) -> str:
    """Convert a simple glob (*/foo.rs) to a regex fragment for --ignore-filename-regex."""
    return re.escape(os.path.basename(pat.lstrip("*/")))


class CoverageProvider(ABC):
    """Pluggable coverage measurement backend.

    All implementations return files_data entries in the tarpaulin-compatible schema:
      {"abs_path": str, "path": list[str], "traces": [{"line": N, "stats": {"Line": N}}],
       "covered": int, "coverable": int}
    """
    ignore_pragma: str = COVERAGE_IGNORE_PRAGMA

    @abstractmethod
    def measure(self, project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
        """Run instrumented tests. Returns (pct, files_data, covered, coverable)."""

    @abstractmethod
    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
        """Quick per-file snapshot. Returns (covered, coverable) or None on failure."""

    @abstractmethod
    def load_cached(self, project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int] | None:
        """Load previous measurement without re-running. Returns None if unavailable."""


class TarpaulinProvider(CoverageProvider):
    def measure(self, project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
        return _run_tarpaulin(project_dir, module_name)

    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
        return None  # tarpaulin is too slow for per-file quick checks

    def load_cached(self, project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int] | None:
        return None


class GrcovProvider(CoverageProvider):
    def measure(self, project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
        if not GRCOV_BIN.exists():
            _log("  [warn] COVERAGE_BACKEND=grcov but grcov not found — falling back to tarpaulin")
            return _run_tarpaulin(project_dir, module_name)
        return _run_grcov(project_dir, module_name)

    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
        return _quick_file_coverage_grcov(project_dir, module_name, rel_path)

    def load_cached(self, project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int] | None:
        return _load_grcov_json(project_dir, module_name)


class LlvmCovProvider(CoverageProvider):
    """Coverage backend using cargo-llvm-cov (lcov output, src-based coverage)."""

    def _lcov_path(self, module_name: str, suffix: str = "") -> Path:
        return Path("/tmp") / f"llvmcov_{module_name}{suffix}.lcov"

    def _run(self, project_dir: Path, module_name: str, suffix: str = "") -> tuple[float, list[dict], int, int]:
        lcov_path = self._lcov_path(module_name, suffix)
        env = _cargo_env()
        # llvm-cov uses LLVM's regex (POSIX-ERE-ish): no `\d`, use `[0-9]` instead.
        ignore_patterns = [
            r"tests[/\\]",
            r"build\.rs",
            r"\.gen_tests_[0-9]+\.rs$",   # pipeline-generated test siblings
        ]
        ignore_patterns += [_glob_to_filename_regex(p) for p in _cluster_exclude_patterns()]
        cmd = [
            "cargo", "llvm-cov",
            "--lcov",
            "--output-path", str(lcov_path),
            "-p", module_name,
            # Keep generating the coverage report even if some individual tests
            # fail. Without this, a single failing test (e.g. a buggy gen_tests
            # block) zeroes out the entire measurement. cargo-llvm-cov rejects
            # --no-fail-fast and --ignore-run-fail together; --ignore-run-fail
            # already covers the "report even on failure" case we need here.
            "--ignore-run-fail",
            "--ignore-filename-regex", "|".join(ignore_patterns),
        ]
        cmd += ["--", "--test-threads=1", "--no-capture"]
        try:
            result = subprocess.run(
                cmd, cwd=project_dir, capture_output=True, text=True,
                timeout=BUILD_TIMEOUT, env=env,
            )
            if result.returncode != 0 and not lcov_path.exists():
                # Capture more of stderr (the relevant error usually appears in
                # the LAST kilobyte after pages of "Compiling …" messages).
                _err_tail = result.stderr[-1500:] if len(result.stderr) > 1500 else result.stderr
                _log(f"  llvm-cov: failed (exit={result.returncode}):\n{_err_tail}")
                return 0.0, [], 0, 0
        except subprocess.TimeoutExpired:
            _log(f"  llvm-cov: timeout after {BUILD_TIMEOUT}s")
            return 0.0, [], 0, 0
        except Exception as e:
            _log(f"  llvm-cov: error: {e}")
            return 0.0, [], 0, 0
        return self._parse_lcov(lcov_path, project_dir)

    def _parse_lcov(self, lcov_path: Path, project_dir: Path) -> tuple[float, list[dict], int, int]:
        try:
            text = lcov_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return 0.0, [], 0, 0
        files_data: list[dict] = []
        current_file: str | None = None
        line_hits: dict[int, int] = {}
        project_str = str(project_dir)
        for raw in text.splitlines():
            if raw.startswith("SF:"):
                current_file = raw[3:]
                line_hits = {}
            elif raw.startswith("DA:") and current_file is not None:
                parts = raw[3:].split(",")
                if len(parts) >= 2:
                    try:
                        ln = int(parts[0])
                        ct = int(parts[1])
                        line_hits[ln] = max(line_hits.get(ln, 0), ct)
                    except ValueError:
                        pass
            elif raw == "end_of_record" and current_file is not None:
                abs_path = current_file if os.path.isabs(current_file) else str(project_dir / current_file)
                rel = os.path.relpath(abs_path, project_str)
                if rel.startswith("src" + os.sep) or rel.startswith("src/"):
                    if COVERAGE_IGNORE_PRAGMA:
                        try:
                            src_lines = Path(abs_path).read_text(encoding="utf-8", errors="replace").splitlines()
                            pragma_lnos = {i + 1 for i, ln in enumerate(src_lines) if COVERAGE_IGNORE_PRAGMA in ln}
                            if pragma_lnos:
                                line_hits = {ln: ct for ln, ct in line_hits.items() if ln not in pragma_lnos}
                        except Exception:
                            pass
                    covered   = sum(1 for c in line_hits.values() if c > 0)
                    coverable = len(line_hits)
                    traces    = [{"line": ln, "stats": {"Line": ct}} for ln, ct in sorted(line_hits.items())]
                    files_data.append({
                        "abs_path":  abs_path,
                        "path":      abs_path.split(os.sep),
                        "traces":    traces,
                        "covered":   covered,
                        "coverable": coverable,
                    })
                current_file = None
                line_hits = {}
        total_covered   = sum(f["covered"]   for f in files_data)
        total_coverable = sum(f["coverable"] for f in files_data)
        pct = (total_covered / total_coverable * 100.0) if total_coverable > 0 else 0.0
        return pct, files_data, total_covered, total_coverable

    def measure(self, project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int]:
        return self._run(project_dir, module_name)

    def measure_file(self, project_dir: Path, module_name: str, rel_path: str) -> tuple[int, int] | None:
        pct, files_data, _, _ = self._run(project_dir, module_name, suffix="_quick")
        abs_target = str(project_dir / rel_path)
        for fd in files_data:
            if fd["abs_path"] == abs_target:
                return fd["covered"], fd["coverable"]
        return None

    def load_cached(self, project_dir: Path, module_name: str) -> tuple[float, list[dict], int, int] | None:
        lcov_path = self._lcov_path(module_name)
        if not lcov_path.exists():
            return None
        try:
            return self._parse_lcov(lcov_path, project_dir)
        except Exception:
            return None


def _make_coverage_provider() -> CoverageProvider:
    if COVERAGE_BACKEND == "grcov":
        return GrcovProvider()
    if COVERAGE_BACKEND == "llvm-cov":
        return LlvmCovProvider()
    return TarpaulinProvider()


_coverage_provider: CoverageProvider = _make_coverage_provider()


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

    Empty string for the default (stable) cluster — no extra gate needed.
    For clusters with one active_cfg flag: returns the bare flag name.
    For multiple flags: combines via all(...).
    """
    if cluster is None or getattr(cluster, "is_default", True):
        return ""
    flags = list(getattr(cluster, "active_cfg", []) or [])
    if not flags:
        return ""
    if len(flags) == 1:
        return str(flags[0])
    return "all(" + ", ".join(str(f) for f in flags) + ")"


def _build_gen_tests_decl(sibling_name: str, n: int, allow_line: str,
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
                non_empty = [l for l in lines if l.strip()]
                if non_empty:
                    indent = min(len(l) - len(l.lstrip()) for l in non_empty)
                    if indent > 0:
                        body = "\n".join(
                            (l[indent:] if len(l) >= indent and l[:indent].isspace() else l)
                            for l in lines
                        )
                return body.strip() + "\n"
        i += 1
    return block.strip() + "\n"


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
    """
    sibling = _gen_tests_path(src_file, pass_n)
    body = _extract_gen_tests_body(block)
    sibling.write_text(body, encoding="utf-8")
    decl = _build_gen_tests_decl(sibling.name, pass_n, allow_line,
                                  cluster_cfg_attr=cluster_cfg_attr)
    src_file.write_text(src_text.rstrip() + decl, encoding="utf-8")
    return sibling


def _revert_gen_tests_pass(src_file: Path, src_text: str, sibling: Path) -> None:
    """Restore src_file to src_text (no decl) and delete the sibling file."""
    src_file.write_text(src_text, encoding="utf-8")
    try:
        sibling.unlink()
    except OSError:
        pass


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
        for pred in active_set:
            if re.search(rf'\bnot\s*\(\s*{re.escape(pred)}\s*\)', cfg_expr):
                return True
        return False

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
    """
    lines: list[str] = []

    # Structs with field types (handles both named and tuple structs)
    for m in re.finditer(
        r'pub\s+struct\s+(\w+)(?:<[^{>]*>)?\s*\{([^}]*)\}', content, re.DOTALL
    ):
        name = m.group(1)
        body = m.group(2)
        fields = re.findall(r'(?:pub\s+)?(\w+)\s*:\s*([^,\n]+)', body)
        if fields:
            field_str = ", ".join(
                f"{f}: {t.strip().rstrip(',')}" for f, t in fields
                if f not in ('use', 'fn', 'let', 'const', 'pub')
            )
            lines.append(f"pub struct {name} {{ {field_str} }}")
        else:
            lines.append(f"pub struct {name}")

    # Enums with variants (including tuple and struct variants)
    for m in re.finditer(
        r'pub\s+enum\s+(\w+)(?:<[^{>]*>)?\s*\{([^}]*)\}', content, re.DOTALL
    ):
        name = m.group(1)
        body = m.group(2)
        # Extract variant names (may be bare, tuple, or struct variants)
        variants = re.findall(r'^\s*(\w+)(?:\([^)]*\)|\s*\{[^}]*\})?', body, re.MULTILINE)
        variants = [v for v in variants if v and not v.startswith(('#', '/', 'pub', 'use'))]
        if variants:
            lines.append(f"pub enum {name} {{ {', '.join(variants)} }}")
        else:
            lines.append(f"pub enum {name}")

    # Traits
    for m in re.finditer(r'pub\s+trait\s+(\w+)', content):
        lines.append(f"pub trait {m.group(1)}")

    # Function signatures (full: name, params, return type)
    for m in re.finditer(
        r'pub\s+(?:async\s+)?fn\s+(\w+\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?:->\s*[^{;]+)?)',
        content
    ):
        sig = re.sub(r'\s+', ' ', m.group(0).strip().rstrip('{').strip())
        lines.append(sig)

    # Constants and statics with types
    for m in re.finditer(
        r'pub\s+(?:const|static)\s+(?:mut\s+)?(\w+)\s*:\s*([^=\n]+)', content
    ):
        lines.append(f"pub const {m.group(1)}: {m.group(2).strip()}")

    return "\n".join(lines) if lines else ""


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
            import tempfile
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
                try:
                    abs_path.write_text(source, encoding="utf-8")
                except Exception:
                    pass
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
    _fn_count = sum(1 for l in fn_bodies.splitlines() if re.match(r'\s*(pub\s+)?fn\s+', l))
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
    _env_section = _describe_test_environment()
    prompt = (
        f"You generated tests for `{rel_path}` but some lines remain uncovered.\n"
        f"{_count_line}"
        f"For each function still marked `// !! UNCOVERED` below, classify it and suggest what to try.\n\n"
        f"{_env_section}\n"
        f"{_guard_section}"
        f"{_history_section}"
        f"{_unproductive_section}"
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
    result = ask(prompt, model=_TASK0C_MODEL, max_tokens=4096, think=None)
    lines = [l.strip() for l in result.splitlines() if l.strip().startswith("FUNCTION")]
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

    response = ask(prompt, model=model, max_tokens=16384, think=None)
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


def task0c_augment_module(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    crate_type: str = "",
    existing_test_files: set | None = None,
    project_context: str = "",
    coverage_strategy: str = "",
) -> tuple[float, int, bool]:
    """
    Generate unit tests (#[cfg(test)] blocks appended inline) for a module
    until tarpaulin reports 100% coverage or MAX_RETRIES is exhausted.
    Returns final coverage percentage.
    """
    if existing_test_files is None:
        existing_test_files = set()

    # Cluster label for log messages — shows which compiler configuration is active.
    _cluster_label = _current_cluster.name if _current_cluster else "stable"

    augmentor = _test_augmentor_factory()

    if not augmentor.is_applicable(crate_type):
        _log(f"  [{module_name}] crate type '{crate_type}' not applicable for augmentation — skipping.")
        return 100.0, 0, False  # treat as done

    # Pre-flight: two-stage compile check.
    # Stage 1: library only — if broken, skip (not TASK 0c's job to fix implementation).
    # Stage 2: full test harness — if broken but stage 1 passes, existing test code is
    #   broken and the LLM is asked to fix it before augmentation begins.
    lib_ok, lib_err = augmentor.lib_compiles(project_dir, module_name)
    if not lib_ok:
        # Check whether the failure is caused by a gen_* block we wrote
        # (syntax errors inside #[cfg(test)] blocks still break cargo build --lib).
        # If so, fall through to the harness fix loop which passes the error to the
        # LLM and only strips as a last resort — preserving generated test progress.
        # If the failure is in the real implementation, skip immediately.
        _lib_err_in_gen = any(
            "mod gen_" in lib_err or
            ("gen_tests" in lib_err) or
            (src_file.name in lib_err and
             "mod gen_" in src_file.read_text(encoding="utf-8", errors="ignore"))
            for src_file in module_dir.rglob("*.rs")
            if not is_test_file(src_file, project_dir)
        )
        if not _lib_err_in_gen:
            # Check whether any of the broken files are writable (i.e. we wrote them).
            # If the error is only in write-protected originals, we truly cannot fix it.
            _broken_in_err = [
                src_file for src_file in module_dir.rglob("*.rs")
                if (str(src_file.relative_to(project_dir)) in lib_err
                    or src_file.name in lib_err)
                and not is_test_file(src_file, project_dir)
                and os.access(src_file, os.W_OK)
            ]
            if not _broken_in_err:
                err_snippet = lib_err[:400]
                _log(f"  [{module_name}] Library does not compile in write-protected files — skipping.")
                _log(f"    Error: {err_snippet}")
                return 0.0, 0, False
            _log(f"  [{module_name}] Library compile failure in writable source — routing to harness fix loop.")
        else:
            _log(f"  [{module_name}] Library compile failure in gen_* block — routing to harness fix loop.")

    def _error_signatures(cargo_output: str) -> frozenset:
        """Extract stable error signatures — error code + message, no file/line numbers."""
        return frozenset(re.findall(r'error\[E\d+\]: .+', cargo_output))

    harness_ok, compile_err_out = augmentor.test_harness_compiles(project_dir, module_name)
    if not harness_ok:
        compile_err = compile_err_out
        prev_signatures: frozenset = frozenset()
        fix_attempt = 0

        while True:
            fix_attempt += 1
            _log(f"  [{module_name}] Harness fix attempt {fix_attempt} — sending compile error to LLM.")

            # Find source files mentioned in the error that belong to this module.
            broken_files: list[Path] = []
            for src_file in sorted(module_dir.rglob("*.rs")):
                rel = str(src_file.relative_to(project_dir))
                if rel in compile_err or src_file.name in compile_err:
                    broken_files.append(src_file)
            if not broken_files:
                for src_file in sorted(module_dir.rglob("*.rs")):
                    try:
                        if "#[cfg(test)]" in src_file.read_text(encoding="utf-8"):
                            broken_files.append(src_file)
                    except Exception:
                        pass

            if not broken_files:
                _log(f"  [{module_name}] Cannot identify broken file — skipping augmentation.")
                return 0.0, 0, False

            # Pass 1: ask LLM to fix each broken file — preserve as much generated
            # test code as possible rather than discarding it immediately.
            for src_file in broken_files:
                if is_test_file(src_file, project_dir):
                    continue
                try:
                    current_content = src_file.read_text(encoding="utf-8")
                except Exception:
                    continue
                rel = str(src_file.relative_to(project_dir))

                prompt = f"""A Rust source file has a test block that fails to compile.

File: {rel}

Compile error:
```
{compile_err}
```

Current file content:
{current_content}

Fix the test code so it compiles. Common issues to fix:
- Remove or correct calls to non-existent methods or types.
- Fix type inference errors (e.g. replace `assert_eq!(x, [])` with `assert!(x.is_empty())`).
- Remove `#[test]` from helper functions that take arguments.
- Remove imports of crates not listed in Cargo.toml.

Output the complete corrected file.

<FILE path="{rel}">
corrected content here
</FILE>
"""
                response = ask(prompt, model=_TASK0C_MODEL)
                files = extract_files(response)
                if rel in files:
                    write(project_dir / rel, files[rel])
                    _log(f"    LLM fixed test code in {rel}")
                else:
                    _log(f"    LLM produced no output for {rel}")

            harness_ok, compile_err_out = augmentor.test_harness_compiles(project_dir, module_name)
            if harness_ok:
                _log(f"  [{module_name}] Harness fixed by LLM after {fix_attempt} attempt(s).")
                break

            new_signatures = _error_signatures(compile_err_out)
            resolved = prev_signatures - new_signatures
            _log(f"  [{module_name}] Still broken — {len(new_signatures)} error signature(s), "
                 f"{len(resolved)} resolved since last attempt.")

            if new_signatures == prev_signatures:
                # Truly stuck — LLM made no progress.
                # Targeted strip: remove only the specific gen_* block(s) whose line
                # ranges overlap with error lines — preserving all other gen_* blocks.
                _log(f"  [{module_name}] No error improvement — targeted strip of broken gen_* block(s).")

                def _strip_broken_gen_blocks(src_file: Path, error_text: str) -> bool:
                    """Remove gen_tests artifacts that cover broken code. Returns True if changed.

                    With the sibling-file design, each `foo.gen_tests_N.rs` IS one block.
                    If src_file is a sibling, delete it and remove its parent's decl.
                    If src_file is a real source file, strip all its gen_tests artifacts
                    (siblings + decls) — we can no longer target an individual block within
                    a source file because none live there anymore.
                    """
                    sibling_match = re.match(r'^(.+)\.gen_tests_(\d+)\.rs$', src_file.name)
                    if sibling_match:
                        parent_stem = sibling_match.group(1)
                        pass_n = int(sibling_match.group(2))
                        parent = src_file.with_name(f"{parent_stem}.rs")
                        try:
                            src_file.unlink()
                        except OSError:
                            return False
                        if parent.exists():
                            ptxt = parent.read_text(encoding="utf-8")
                            decl_re = re.compile(
                                r'\n*#\[cfg\(test\)\][\s\n]*(?:#\[[^\]]*\][\s\n]*)*'
                                r'#\[path\s*=\s*"[^"]+"\][\s\n]*'
                                rf'mod\s+gen_tests_{pass_n}\s*;',
                            )
                            cleaned = decl_re.sub("", ptxt)
                            if cleaned != ptxt:
                                parent.write_text(cleaned, encoding="utf-8")
                        _log(f"  [{module_name}] Deleted broken sibling "
                             f"{src_file.relative_to(project_dir)} (gen_tests_{pass_n}).")
                        return True
                    # Real source file — strip all gen_tests artifacts.
                    n = _strip_all_gen_tests_artifacts(src_file)
                    if n > 0:
                        _log(f"  [{module_name}] Stripped {n} gen_tests sibling(s) for "
                             f"{src_file.relative_to(project_dir)}.")
                        return True
                    return False

                any_stripped = False
                for src_file in broken_files:
                    if is_test_file(src_file, project_dir):
                        continue
                    if _strip_broken_gen_blocks(src_file, compile_err_out):
                        any_stripped = True

                harness_ok, _broken_err = augmentor.test_harness_compiles(project_dir, module_name)
                if not harness_ok and any_stripped:
                    # Targeted strip removed some blocks but harness is still broken.
                    # Fall through to a full strip of ALL gen_tests artifacts from broken files.
                    _log(f"  [{module_name}] Targeted strip insufficient — doing full gen_tests strip.")
                    for src_file in broken_files:
                        if is_test_file(src_file, project_dir):
                            continue
                        # Skip gen_tests siblings (already deleted in targeted pass).
                        if re.search(r'\.gen_tests_\d+\.rs$', src_file.name):
                            continue
                        try:
                            n_removed = _strip_all_gen_tests_artifacts(src_file)
                            if n_removed > 0:
                                _log(f"    Full strip: {src_file.relative_to(project_dir)} "
                                     f"({n_removed} sibling(s))")
                        except Exception:
                            pass
                    harness_ok, _broken_err = augmentor.test_harness_compiles(project_dir, module_name)
                if not harness_ok:
                    # Check if even `cargo build` (no tests) fails — means LLM damaged source.
                    _lib_only = subprocess.run(
                        ["cargo", "build", "-p", module_name],
                        cwd=project_dir, capture_output=True, text=True,
                        timeout=BUILD_TIMEOUT, env=_cargo_env(),
                    )
                    if _lib_only.returncode != 0:
                        _log(f"  [{module_name}] Source files damaged by LLM fix attempts — restoring from git HEAD.")
                        # Restore non-test source files from git HEAD (not test files — those we own).
                        _restore_ok = True
                        for _sf in sorted(module_dir.rglob("*.rs")):
                            if is_test_file(_sf, project_dir):
                                continue
                            _rel = str(_sf.relative_to(project_dir))
                            _r = subprocess.run(
                                ["git", "checkout", "HEAD", "--", _rel],
                                cwd=project_dir, capture_output=True, text=True,
                            )
                            if _r.returncode != 0:
                                _log(f"    git restore failed for {_rel}: {_r.stderr[:100]}")
                                _restore_ok = False
                        if _restore_ok:
                            harness_ok, _ = augmentor.test_harness_compiles(project_dir, module_name)
                            if harness_ok:
                                _log(f"  [{module_name}] Source restored — harness now compiles.")
                                break
                    _log(f"  [{module_name}] Harness still broken after targeted strip — skipping augmentation.")
                    return 0.0, 0, False
                _log(f"  [{module_name}] Harness fixed by targeted strip.")
                break

            prev_signatures = new_signatures
            compile_err = compile_err_out

    # Derive allow attributes and prelude imports from project source — no LLM.
    _allow_line, _prelude_block = _derive_gentest_prelude(project_dir)

    # Pre-clean only if the harness is broken — if existing gen_tests compile,
    # keep them so accumulated coverage is never lost on re-entry.
    _precheck_ok, _ = augmentor.test_harness_compiles(project_dir, module_name)
    if not _precheck_ok:
        for src_file in sorted(module_dir.rglob("*.rs")):
            if is_test_file(src_file, project_dir):
                continue
            # Don't recurse into siblings here — they're handled via their parent.
            if re.search(r'\.gen_tests_\d+\.rs$', src_file.name):
                continue
            try:
                n_removed = _strip_all_gen_tests_artifacts(src_file)
                if n_removed > 0:
                    _log(f"  [{module_name}] Pre-cleaned {n_removed} broken gen_tests "
                         f"sibling(s) for {src_file.relative_to(project_dir)}")
            except Exception:
                pass

    max_attempts = 10
    attempt = 0
    # Resume attempt number if interrupted mid-run.
    _saved_phase = _read_state(project_dir)["phases"].get(f"task0c/{_cluster_label}/{module_name}", {})
    if _saved_phase.get("status") == "in_progress":
        _m = re.search(r"attempt (\d+)", _saved_phase.get("detail", ""))
        if _m:
            attempt = int(_m.group(1)) - 1  # loop does attempt += 1 at top
            _log(f"  [{module_name}/{_cluster_label}] Resuming from attempt {int(_m.group(1))}/{max_attempts}")
    coverage = 0.0
    covered_lines = 0
    coverable_lines = 0
    files_data: list[dict] = []
    _skip_first_measure = False
    # On resume, reload coverage from the previous run's grcov JSON + filecache
    # to avoid an expensive full re-run before the first gen attempt.
    if attempt > 0:
        _cached = _coverage_provider.load_cached(project_dir, module_name)
        if _cached is not None:
            coverage, files_data, covered_lines, coverable_lines = _cached
            _skip_first_measure = True
            _log(f"  [{module_name}/{_cluster_label}] Loaded coverage from previous run: "
                 f"{coverage:.1f}% ({covered_lines}/{coverable_lines} lines)")
    prev_covered = covered_lines if _skip_first_measure else -1
    no_improvement_count = 0
    MAX_NO_IMPROVEMENT = 10  # per-file retrospective runs first; global stall is last resort only
    FILE_STALL_LIMIT = 10   # consecutive no-improvement attempts before skipping a file
    RETRO_DEEP_AT_STALL = 5 # re-fire retrospective with concrete failed-attempt evidence
    file_stall_counts: dict[str, int] = {}   # consecutive no-improvement attempts per file
    file_best_uncov: dict[str, int] = {}     # fewest uncovered lines seen per file
    file_prev_uncov_lines: dict[str, set] = {}  # uncov_lines snapshot from previous attempt per file
    skipped_files: set[str] = set()          # files permanently skipped due to stall
    file_what_worked: dict[str, str] = {}    # confirmed-working patterns per file (from improving attempts)
    file_retrospectives: dict[str, str] = {} # per-file classification of why coverage stalled
    # Track concrete attempts that compiled but added zero coverage to the
    # targeted lines. Fed to the deep retrospective so the classifier can see
    # which strategies the LLM has already tried — and revise its TESTABLE
    # call to UNREACHABLE when evidence demands it.
    file_last_qcov: dict[str, int] = {}          # last quick-cov 'covered' count per file
    file_failed_blocks: dict[str, list[str]] = {}  # gen blocks that compiled but didn't move the metric
    file_retro_deep_fired: set[str] = set()      # files where the deep retrospective has already run
    _at_ceiling = False                      # True when a full pass produced no actionable lines
    while attempt < max_attempts:
        attempt += 1
        _log(f"  [{module_name}/{_cluster_label}] Attempt {attempt}/{max_attempts}")

        # --- Phase A: compile check + targeted fix ---
        # Use generate_fix_loop: run compile check, if it fails ask LLM to fix
        # specific files rather than regenerating from scratch.
        # Only source files with #[cfg(test)] blocks are candidates — these hold
        # the generated tests that may have syntax errors.
        module_source_paths = [
            f for f in sorted(module_dir.rglob("*.rs"))
            if not is_test_file(f, project_dir)
        ]
        compile_ok, _ = generate_fix_loop(
            project_dir=project_dir,
            check_cmd=f"cargo test -p {module_name} --no-run",
            source_paths=module_source_paths,
            existing_test_files=existing_test_files,
            model=_TASK0C_MODEL,
            max_fix_attempts=1,  # one fix attempt per outer attempt; outer loop retries
            timeout=BUILD_TIMEOUT,
        )
        if not compile_ok:
            _log(f"  [{module_name}] Still does not compile after fix — retrying outer loop.")
            continue

        # --- Phase B: measure coverage ---
        if _skip_first_measure:
            _skip_first_measure = False  # only skip once; always re-measure after first gen
        else:
            coverage, files_data, covered_lines, coverable_lines = augmentor.measure_coverage(project_dir, module_name, module_dir)
            _log(f"  [{module_name}/{_cluster_label}] Coverage: {coverage:.1f}% ({covered_lines}/{coverable_lines} lines)")
        _mark_phase(project_dir, f"task0c/{_cluster_label}/{module_name}", "in_progress",
                    detail=f"{coverage:.1f}% attempt {attempt}", source_hash=_source_hash(project_dir))
        _set_current(project_dir, f"task0c/{_cluster_label}/{module_name} / {coverage:.1f}% attempt {attempt}")

        if coverage >= 100.0:
            _log(f"  [{module_name}] 100% coverage reached.")
            break

        # Stall detection: compare integer covered-line counts, not float percentages.
        # Float coverage can oscillate by ±0.01% between runs (denominator shifts when
        # generated test blocks change which lines tarpaulin counts). Integers are stable.
        delta_lines = covered_lines - prev_covered

        # --- Regression guard: if coverage dropped significantly, gen_tests likely have
        # runtime failures that corrupt grcov profraw data.  Diagnose and fix before
        # generating more tests (which would only pile more failing tests on top). ---
        if prev_covered > 0 and delta_lines < -50:
            _log(f"  [{module_name}] Coverage regression {delta_lines:+d} lines — running tests to detect runtime failures.")
            _reg_env = _cargo_env()
            _reg_result = subprocess.run(
                ["cargo", "test", "-p", module_name, "--no-fail-fast", "--", "--test-threads=1", "--no-capture"],
                cwd=project_dir, capture_output=True, text=True,
                timeout=BUILD_TIMEOUT, env=_reg_env,
            )
            if _reg_result.returncode != 0:
                _reg_output = (_reg_result.stdout + "\n" + _reg_result.stderr)[-_FIX_ERR_CAP:]
                _log(f"  [{module_name}] Test failures confirmed — running runtime fix loop.")
                generate_fix_loop(
                    project_dir=project_dir,
                    check_cmd=f"cargo test -p {module_name} -- --test-threads=1",
                    source_paths=module_source_paths,
                    existing_test_files=existing_test_files,
                    model=_TASK0C_MODEL,
                    max_fix_attempts=3,
                    timeout=BUILD_TIMEOUT,
                )
            else:
                _log(f"  [{module_name}] Tests pass but coverage regressed — likely stale profraw; retrying.")
            prev_covered = covered_lines
            continue  # skip Phase C (no new tests until runtime failures are resolved)

        if delta_lines <= 0:
            no_improvement_count += 1
            _log(f"  [{module_name}] No improvement ({covered_lines}/{coverable_lines} lines, delta {delta_lines:+d}) — stall count {no_improvement_count}/{MAX_NO_IMPROVEMENT}")
            if no_improvement_count >= MAX_NO_IMPROVEMENT:
                _log(f"  [{module_name}] Coverage stalled at {coverage:.1f}% — switching to pragma pass.")
                _task0c_pragma_pass(
                    project_dir, module_name, module_dir, files_data, _TASK0C_MODEL
                )
                # Re-measure after pragma pass — if 100%, we're done; if not, outer
                # loop will keep trying (or the main loop's FR-018 guard fires).
                break
        else:
            no_improvement_count = 0
        prev_covered = covered_lines


        # --- Phase C: generate/augment tests for uncovered lines ---
        # Filter to source files in this module with uncovered lines.
        # Skip test files — adding tests inside test files causes circular issues.
        mod_prefix = str(module_dir) + "/"
        uncovered_by_file: dict[str, tuple[str, set[int]]] = {}
        for fdata in files_data:
            abs_path_str = fdata["abs_path"] if "abs_path" in fdata else os.sep.join(fdata["path"])
            if not abs_path_str.startswith(mod_prefix):
                continue
            rel = str(Path(abs_path_str).relative_to(project_dir))
            lines_uncov = {
                t["line"] for t in fdata.get("traces", [])
                if t["stats"].get("Line", 1) == 0
            }
            _gated = _current_cluster.gated_files if _current_cluster and _current_cluster.gated_files else set()
            if lines_uncov and not is_test_file(Path(abs_path_str), project_dir) \
                    and Path(abs_path_str).name != "build.rs" \
                    and rel not in _gated:
                uncovered_by_file[rel] = (fdata.get("content", ""), lines_uncov)

        if not uncovered_by_file:
            _log(f"  [{module_name}] No uncovered lines in module source files.")
            break

        # Collect per-attempt context once (not per-file) to avoid redundant disk reads.
        # BL-008: Cargo.toml — model must know what dependencies are available.
        cargo_toml_path = module_dir / "Cargo.toml"
        if not cargo_toml_path.exists():
            cargo_toml_path = project_dir / "Cargo.toml"
        cargo_toml_content = ""
        if cargo_toml_path.exists():
            try:
                cargo_toml_content = cargo_toml_path.read_text(encoding="utf-8")
            except Exception:
                pass

        # BL-004: collect integration test files (tests/*.rs) for pattern reference.
        integration_tests: list[tuple[str, str]] = []
        tests_dir = module_dir / "tests"
        if not tests_dir.exists():
            tests_dir = project_dir / "tests"
        if tests_dir.exists():
            for tf in sorted(tests_dir.rglob("*.rs")):
                try:
                    rel = str(tf.relative_to(project_dir))
                    content = tf.read_text(encoding="utf-8")
                    integration_tests.append((rel, content))
                except Exception:
                    pass

        # Sibling source files: all module .rs files except the current target.
        # Include other targeted files too — they define the types the target uses.
        # Only the file being processed is excluded (its content is in the main prompt).
        sibling_files_all: list[tuple[str, str]] = []
        _target_dir = project_dir / "target"
        for sib_file in sorted(module_dir.rglob("*.rs")):
            if is_test_file(sib_file, project_dir):
                continue
            if sib_file.name == "build.rs":
                continue  # build scripts are not library code — no useful type context
            try:
                if sib_file.is_relative_to(_target_dir):
                    continue  # skip build artifacts
            except Exception:
                pass
            try:
                sib_rel = str(sib_file.relative_to(project_dir))
                sib_text = sib_file.read_text(encoding="utf-8")
                sibling_files_all.append((sib_rel, sib_text))
            except Exception:
                pass

        _file_order_strategy = os.getenv("TASK0C_FILE_ORDER", "easy_first").strip().lower()
        if _file_order_strategy == "easy_first":
            _ordered_files = sorted(
                uncovered_by_file.items(),
                key=lambda kv: (file_stall_counts.get(kv[0], 0), len(kv[1][1]))
            )
        else:
            _ordered_files = list(uncovered_by_file.items())

        _queue_summary = ", ".join(
            f"{rel.split('/')[-1]}({len(unc)}raw{'*' if file_stall_counts.get(rel, 0) > 0 else ''})"
            for rel, (_, unc) in _ordered_files
            if rel not in skipped_files
        )
        _log(f"  [{module_name}] Queue: {len([r for r,_ in _ordered_files if r not in skipped_files])} file(s) — {_queue_summary} (raw=pre-filter grcov count)")

        _active_files = [r for r, _ in _ordered_files if r not in skipped_files]
        attempt_n_within_outer = 0
        _pass_had_work = False   # set True if any file makes a real LLM call this pass
        for rel_path, (content, uncov_lines) in _ordered_files:
            attempt_n_within_outer += 1

            if rel_path in skipped_files:
                _log(f"  [{module_name}] Skipping {rel_path} — stalled for {FILE_STALL_LIMIT} consecutive attempts.")
                continue

            _file_idx = _active_files.index(rel_path) + 1 if rel_path in _active_files else attempt_n_within_outer
            _log(f"  [{module_name}] File {_file_idx}/{len(_active_files)}: {rel_path} ({len(uncov_lines)} raw uncovered lines)")

            abs_path = project_dir / rel_path
            # _raw_text = current on-disk content (including existing gen_tests decls).
            # This is the revert target: on a failed pass we restore _raw_text and delete
            # the just-written sibling, so previously-committed passes survive intact.
            _raw_text = abs_path.read_text(encoding="utf-8") if abs_path.exists() else content
            # gen_tests bodies live in sibling files. Read them all (concatenated as
            # `mod gen_tests_N { ... }` blocks) so the LLM sees what it generated
            # before and can extend rather than restart.
            _old_block = _read_all_gen_tests(abs_path)
            # The view shown to the LLM (and used for uncov_lines line counting) strips
            # the path-mod declaration lines so the line numbers match the original src.
            existing_text = _strip_gen_tests_decls(_raw_text).rstrip() + "\n"

            _src_line_count = existing_text.count('\n')
            uncov_lines = {l for l in uncov_lines if l <= _src_line_count}

            # Filter LLVM/grcov instrumentation artifacts:
            # 1. Comment-only lines (// …) — grcov sometimes marks these coverable, they never are
            # 2. Structural-only tokens (}, } else {) — LLVM region counter stays 0 even when
            #    the surrounding code executes (known grcov/LLVM quirk)
            # Only strip if at least one adjacent line IS covered (confirming the block runs).
            _file_cov_hits: dict[int, int] = {}
            for _fd in files_data:
                _fd_abs = _fd.get("abs_path", os.sep.join(_fd.get("path", [])))
                if _fd_abs == str(abs_path):
                    _file_cov_hits = {t["line"]: t["stats"].get("Line", 0) for t in _fd.get("traces", [])}
                    break
            _structural_re = re.compile(r'^\}(\s*else\s*\{)?\s*[;,]?\s*$')
            _src_lines_list = existing_text.splitlines()
            _artifact_lines: set[int] = set()
            for _al in sorted(uncov_lines):
                if _al > len(_src_lines_list):
                    continue
                _al_txt = _src_lines_list[_al - 1].strip()
                _is_comment    = _al_txt.startswith("//")
                _is_structural = bool(_structural_re.match(_al_txt)) and bool(_al_txt)
                if _is_comment or _is_structural:
                    # Confirm by checking that at least one adjacent line is covered
                    if any(_file_cov_hits.get(n, 0) > 0 for n in range(max(1, _al - 5), _al + 6) if n != _al):
                        _artifact_lines.add(_al)
            if _artifact_lines:
                _log(f"  [{module_name}] {rel_path} — filtered {len(_artifact_lines)} LLVM artifact line(s): {sorted(_artifact_lines)}")
                uncov_lines -= _artifact_lines

            # Filter lines inside cfg blocks incompatible with the current cluster
            # (e.g. #[cfg(not(error_generic_member_access))] under the nightly cluster).
            # These lines don't compile here — no test can cover them. Filter only, no EXCL.
            if _current_cluster and _current_cluster.active_cfg:
                _incompat = _filter_cfg_incompatible_lines(
                    existing_text, uncov_lines, _current_cluster.active_cfg
                )
                if _incompat:
                    _log(f"  [{module_name}] {rel_path} — filtered {len(_incompat)} cfg-incompatible line(s) "
                         f"(compiled out under {_current_cluster.name}): {sorted(_incompat)}")
                    uncov_lines -= _incompat

            # Filter lines inside cfg blocks that are excluded in this cluster
            # (e.g. #[cfg(error_generic_member_access)] under the stable cluster).
            # No EXCL pragmas written — these lines ARE coverable in the nightly cluster
            # pass and must not be permanently marked ignored.
            if _current_cluster and _current_cluster.excluded_cfg:
                _cfg_excl = _filter_cfg_excluded_lines(
                    existing_text, uncov_lines, _current_cluster.excluded_cfg
                )
                if _cfg_excl:
                    _log(f"  [{module_name}] {rel_path} — filtered {len(_cfg_excl)} excluded-cfg line(s) "
                         f"(compiled out in {_current_cluster.name}): {sorted(_cfg_excl)}")
                    uncov_lines -= _cfg_excl

            if not uncov_lines:
                _log(f"  [{module_name}] {rel_path} — no actionable uncovered lines after filtering, skipping.")
                continue

            _log(f"  [{module_name}] {rel_path} — {len(uncov_lines)} actionable uncovered lines (post-filter)")
            fn_bodies = _extract_uncovered_functions(existing_text, uncov_lines)
            pub_symbols = _extract_public_symbols(existing_text)

            # BL-002: retrieve prior attempt history for this file.
            attempt_history = _get_task0c_history(project_dir, rel_path)

            # BL-014: Python planner — rank siblings by type-name overlap.
            # Exclude the current target from its own sibling list.
            relevant_siblings = _select_relevant_siblings(
                rel_path, existing_text,
                [(srel, sc) for srel, sc in sibling_files_all if srel != rel_path],
                project_dir
            )
            # Condense sibling content to signatures (max 3k each) so large files like
            # error.rs (35k full) and lib.rs (20k full) actually fit in the context budget.
            # Exceptions: kind.rs and error.rs are never condensed when selected as siblings
            # because their critical mechanisms (autoref specialization, vtable layout) live
            # in function bodies — condensing strips exactly the context the LLM needs.
            _FULL_SOURCE_SIBLINGS = {"kind.rs", "error.rs"}
            relevant_siblings = [
                (srel, sc if Path(srel).name in _FULL_SOURCE_SIBLINGS
                       else _condense_rust_to_signatures(sc, Path(srel).name)[:_SIBLING_CAP])
                for srel, sc in relevant_siblings
            ]

            # BL-007: build whole-folder context with prioritized degradation.
            secs = _build_task0c_prompt_sections(
                rel_path=rel_path,
                existing_text=existing_text,
                fn_bodies=fn_bodies,
                pub_symbols=pub_symbols,
                cargo_toml_content=cargo_toml_content,
                integration_tests=integration_tests,
                sibling_files=relevant_siblings,
                coverage_strategy=coverage_strategy,
                attempt_history=attempt_history,
                project_context=project_context,
                old_block=_old_block,
                what_worked=file_what_worked.get(rel_path, ""),
                retrospective=file_retrospectives.get(rel_path, ""),
                uncov_lines=uncov_lines,
            )

            # Per-file negative constraints — loaded from project_configs/{project}.constraints.toml.
            # Keys are source file basenames. Values are injected after INTERNAL MODULE RULES.
            _file_constraints = _load_file_constraints(project_dir)
            _target_basename = Path(rel_path).name
            _file_constraints_section = _file_constraints.get(_target_basename, "")

            gen_count = _gen_tests_count(abs_path)
            _pass_n = _next_gen_tests_n(abs_path)
            mod_name = f"gen_tests_{_pass_n}"
            output_format = (
                f'<FILE path="gen_tests_block">\n'
                f'#[cfg(test)]\n{_allow_line}\nmod {mod_name} {{{{\n'
                f'    use super::*;\n'
                f'    {_prelude_block}\n\n'
                f'    #[test]\n    fn test_example() {{{{\n        // tests\n    }}}}\n}}}}\n'
                f'</FILE>'
            )

            _coverage_status = (
                f"\nCoverage status: {coverage:.1f}% covered. "
                f"The {len(uncov_lines)} line(s) in fn_bodies below are the ONLY remaining uncovered targets — "
                f"all other lines are already covered. Focus exclusively on these.\n"
                + (
                    f"IMPORTANT: grcov sometimes misreports dead branches, closing braces, or comments as "
                    f"uncovered even when the surrounding code executes fine. If after careful analysis "
                    f"the uncovered line(s) appear to be a dead code path (e.g. an if-branch whose "
                    f"condition is structurally never true on modern Rust), a comment, or a closing brace "
                    f"— do NOT attempt to cover them. Instead output an empty test block with a comment "
                    f"explaining why the line is unreachable. Do not waste tests on genuinely dead code.\n"
                    if len(uncov_lines) <= 3 else ""
                )
            ) if coverage > 0 else ""

            _env_section = _describe_test_environment()
            prompt = f"""You are writing Rust unit tests that specify the behavioral contract of source code.
{secs['strategy']}{secs['history']}{secs['what_worked']}{secs['retrospective']}{secs['context']}{_coverage_status}
Target file: {rel_path}
{_env_section}{secs['cargo']}{secs['full_source']}{secs['fn_bodies']}{secs['symbols']}{secs['examples']}{secs['integ_tests']}{secs['siblings']}{secs['old_block']}
Write a `#[cfg(test)] mod {mod_name}` block for `{rel_path}`.
{"Write ONLY new test functions for the uncovered lines in fn_bodies above. The existing test module(s) shown are already on disk — do NOT copy or repeat any test from them." if secs['old_block'] else "Write a new test block covering the uncovered lines shown in fn_bodies above."}

Coverage guidance:
- Prefer tests with at least one `assert!`, `assert_eq!`, `assert_ne!`, or `.unwrap()` —
  asserting on return values adds spec value. If asserting is genuinely awkward (void
  returns, side effects only), simply exercising the code path is acceptable.
- Prefer testing edge cases and error conditions, not just happy paths.
- When testing a function that returns `Result` or `Option`, test BOTH the Ok/Some path
  AND the Err/None path if the code shows both branches.

Rust rules:
- Each `#[test]` fn must have ZERO arguments.
- Use `use super::*;` to access module items.
- Read field types from the struct definitions in the source file — match EXACTLY.

INTERNAL MODULE RULES — these tests live inside the source file, not in an external test crate:
- Do NOT write `use <crate_name>::...` — you are already inside the crate.
- The template prelude already imports String, Vec, Box, format!, StdError, and fmt — use them directly.
- Do NOT use `#[macro_export]` macros inside this module — they cannot be imported from within the crate.
  Construct values through direct API calls instead.
- Do NOT use `assert_eq!` or `assert_ne!` on types that do not implement `PartialEq`.
  Use `format!("{{}}",  val)`, boolean checks, or `matches!()` instead.
{_nightly_rules_for_cluster()}
See the IMPORTS, PITFALLS, and RULES sections of coverage_strategy.md (shown above) for
project-specific import paths, type construction patterns, and hard constraints.
{_file_constraints_section}
{output_format}
"""
            # FR-012: retry generation — qwen3 thinking mode sometimes returns empty output.
            # On retry, send a short explicit follow-up instead of the full prompt again:
            # repeating the same large prompt produces the same empty result.
            def _save_stream_log(gen_attempt: int, query: str, resp: str, reason: str) -> None:
                _fname = rel_path.replace("/", "_").replace(".", "_")
                _path = Path(f"/tmp/stream_loop_{attempt}_{_fname}_gen{gen_attempt}.log")
                try:
                    _path.write_text(
                        f"# reason: {reason}\n"
                        f"# file: {rel_path}  outer_attempt: {attempt}  gen_attempt: {gen_attempt}\n"
                        f"# prompt_chars: {len(query)}\n"
                        f"{'='*60}\n## PROMPT\n{'='*60}\n{query}\n"
                        f"{'='*60}\n## RESPONSE\n{'='*60}\n{resp}\n",
                        encoding="utf-8",
                    )
                    _log(f"  [{module_name}] Stream saved → {_path}")
                except Exception as _e:
                    _log(f"  [{module_name}] Could not save stream log: {_e}")

            block = ""
            _block_committed = False
            for _gen_attempt in range(2):
                if _gen_attempt == 0:
                    _query = prompt
                else:
                    # Medium retry — full prompt triggered an empty response (model silently
                    # refuses on ~120K+ char inputs). Drop heavy context sections (siblings,
                    # integration tests, strategy, history, old block) but keep source,
                    # uncovered functions, symbols, and file constraints. Targets ~40-60K
                    # chars — well below the silent-failure threshold.
                    _medium_rules = (
                        "RULES (mandatory):\n"
                        "- Do NOT write `use <crate_name>::...` — you are already inside the crate.\n"
                        "- Do NOT use `#[macro_export]` macros — unavailable inside the crate.\n"
                        + _nightly_rules_for_cluster() + "\n"
                        + "- Do NOT use `assert_eq!`/`assert_ne!` on types without `PartialEq` — use `format!` or `matches!()`.\n"
                        "- Each `#[test]` fn must have ZERO arguments.\n"
                        f"{_file_constraints_section}"
                    )
                    _query = (
                        f"You are writing Rust unit tests for `{rel_path}`.\n"
                        f"Target file: {rel_path}\n"
                        f"{_describe_test_environment()}"
                        f"{secs['cargo']}{secs['full_source']}{secs['fn_bodies']}{secs['symbols']}"
                        f"{_medium_rules}"
                        f"\nWrite a `#[cfg(test)] mod {mod_name}` block covering the uncovered lines shown above.\n"
                        f"{output_format}\n"
                    )
                # Use think=None — lets MODEL_CAPS decide (think=False for qwen3.6:27b-q8).
                # think=True on the compat path is unreliable: sometimes generates 40K chars
                # of thinking, sometimes generates raw_len=0. think=False (via /no_think)
                # is consistently reliable for this model.
                response = ask(_query, model=_TASK0C_MODEL, think=None)

                # Brace-counter extraction — no regex, handles any attribute/whitespace variation.
                block = extract_test_mod(response)
                if block:
                    block = re.sub(r'\s*</FILE>\s*$', '', block).strip()
                if block and re.search(r'block to our Rust file|Here\'s how|You can add|here is', block, re.IGNORECASE):
                    block = ""
                # Reject skeleton-only: model echoed the scaffold template unchanged —
                # all #[test] functions have empty or comment-only bodies.
                if block and not re.search(r'#\[test\][^{]*\{[^}]*\S', block, re.DOTALL):
                    if re.search(
                        r'dead.code|unreachable|genuinely|not coverable|cannot be covered|grcov artifact',
                        (block or "") + (response or ""), re.IGNORECASE,
                    ):
                        _log(f"  [{module_name}] LLM flagged {rel_path} as unreachable — discarding (no EXCL, stall counter applies).")
                        _save_stream_log(_gen_attempt, _query, response or "", "llm_giveup")
                    else:
                        _log(f"  [{module_name}] Skeleton-only response for {rel_path} — discarding.")
                        _save_stream_log(_gen_attempt, _query, response or "", "skeleton_only")
                    block = ""
                if block:
                    break
                _raw_snippet = response[:300].replace('\n', ' ') if response else "(no output)"
                if _gen_attempt == 0:
                    _log(f"  [{module_name}] Empty generation for {rel_path} — retrying. Raw: {_raw_snippet}")
                    _save_stream_log(_gen_attempt, _query, response or "", "empty_generation")
                else:
                    _log(f"  [{module_name}] Empty generation for {rel_path} (retry also failed). Raw: {_raw_snippet}")
                    _save_stream_log(_gen_attempt, _query, response or "", "empty_generation_retry")
            if block:
                # Safety net: re-inject allow line if LLM dropped it from the template.
                # Check only for the exact full allow line — a partial #[allow(unused_imports)]
                # from the LLM does NOT satisfy the guard; we need all lints covered.
                if _allow_line not in block:
                    block = re.sub(
                        r'((?:pub\s+)?mod\s+gen_\w+\s*\{)',
                        f'{_allow_line}\n\\1',
                        block, count=1,
                    )
                # Safety net: re-inject prelude imports if LLM dropped or partially wrote them.
                # Use regex presence checks — exact-string matching causes double-injection
                # when the LLM writes a partial form (e.g. `use std::string::String;` instead
                # of `use std::string::{String, ToString};`) producing "String defined multiple times".
                _PRELUDE_ITEMS = [
                    ("use std::string::{String, ToString};", r'use (?:std|alloc)::[^;]*\bString\b'),
                    ("use std::vec::Vec;",                   r'use (?:std|alloc)::[^;]*\bVec\b'),
                    ("use std::boxed::Box;",                 r'use (?:std|alloc)::[^;]*\bBox\b'),
                    ("use std::format;",                     r'use (?:std|alloc)::[^;]*\bformat\b'),
                    ("use std::error::Error as StdError;",   r'use std::error::Error'),
                    ("use std::fmt::{self, Display, Debug};",r'use (?:std|core)::fmt'),
                ]
                missing_preludes = [
                    stmt for stmt, pat in _PRELUDE_ITEMS
                    if not re.search(pat, block)
                ]
                if missing_preludes:
                    inject = "\n    ".join(missing_preludes)
                    if "use super::*;" in block:
                        block = block.replace("use super::*;", f"use super::*;\n    {inject}", 1)
                    else:
                        block = re.sub(
                            r'((?:pub\s+)?mod\s+gen_\w+\s*\{\s*\n)',
                            f'\\1    {inject}\n',
                            block, count=1,
                        )

                # Reject blocks with no #[test] functions before writing. The LLM
                # occasionally returns a structurally valid mod block whose body
                # is all imports / helpers / commentary and no actual test fn.
                # Accepting it would clutter the source with a dead declaration,
                # burn the next pass_n slot, and force a compile+quick-cov cycle
                # that can't produce coverage. Counting #[test] and namespaced
                # variants (#[tokio::test], #[async_std::test], ...) catches the
                # common Rust test attributes without flagging #[cfg(test)].
                _test_attr_count = len(re.findall(r'#\[(?:\w+::)*test\]', block))
                if _test_attr_count == 0:
                    _log(f"  [{module_name}] {rel_path} — LLM block has 0 #[test] functions; "
                         f"skipping write (no productive content).")
                    _log_task0c_attempt(project_dir, rel_path, attempt, block, "",
                                       "no_tests_in_block")
                    continue

                # Write, compile-check, fix loop — keep iterating as long as errors
                # are being resolved. Only revert when truly stuck (no improvement).
                _block_prev_sigs: frozenset = frozenset()
                _block_fix_attempt = 0
                _block_committed = False
                # gen_tests live in a sibling file (foo.gen_tests_N.rs) with a
                # path-mod declaration appended to abs_path. This isolates test
                # code from the coverage tool's denominator without EXCL markers.
                _sibling_path = _gen_tests_path(abs_path, _pass_n)
                _cluster_attr = _cluster_cfg_attr(_current_cluster)
                _decl = _build_gen_tests_decl(_sibling_path.name, _pass_n, _allow_line,
                                              cluster_cfg_attr=_cluster_attr)
                _src_with_decl = _raw_text.rstrip() + _decl
                while True:
                    abs_path.write_text(_src_with_decl, encoding="utf-8")
                    _sibling_path.write_text(_extract_gen_tests_body(block), encoding="utf-8")
                    check = subprocess.run(
                        ["cargo", "test", "--no-run", "-p", module_name],
                        cwd=project_dir, capture_output=True, text=True,
                        timeout=BUILD_TIMEOUT, env=_cargo_env(),
                    )
                    if check.returncode == 0:
                        _log_task0c_attempt(project_dir, rel_path, attempt + _block_fix_attempt,
                                           block, "", "success")
                        _log(f"  [{module_name}] Appended {mod_name} to {rel_path} "
                             f"({len(uncov_lines)} uncovered lines targeted)")
                        _block_committed = True
                        _pass_had_work = True
                        break

                    compile_err = (check.stdout + check.stderr).strip()
                    cur_sigs = frozenset(re.findall(r'error\[E\d+\]: .+', compile_err))

                    # Unclosed delimiter is structurally broken — revert immediately.
                    if "unclosed delimiter" in compile_err or "mismatched closing delimiter" in compile_err:
                        _log(f"  [{module_name}] Unclosed delimiter — reverting {rel_path}.")
                        _revert_gen_tests_pass(abs_path, _raw_text, _sibling_path)
                        break

                    if _block_fix_attempt > 0:
                        resolved = _block_prev_sigs - cur_sigs
                        if cur_sigs == _block_prev_sigs:
                            _log(f"  [{module_name}] No error improvement in {rel_path} — reverting.")
                            _log_task0c_attempt(project_dir, rel_path, attempt + _block_fix_attempt,
                                               block, compile_err, "compile_error")
                            _revert_gen_tests_pass(abs_path, _raw_text, _sibling_path)
                            break
                        _log(f"  [{module_name}] {len(resolved)} error(s) resolved in {rel_path}, "
                             f"{len(cur_sigs)} remaining — continuing.")

                    _block_fix_attempt += 1
                    _block_prev_sigs = cur_sigs
                    _log(f"  [{module_name}] Compile error in {rel_path} "
                         f"(fix attempt {_block_fix_attempt}) — sending to LLM.")
                    _log_task0c_attempt(project_dir, rel_path, attempt + _block_fix_attempt,
                                       block, compile_err, "compile_error")

                    _fix_history = _get_task0c_history(project_dir, rel_path)
                    _fix_history_section = (
                        f"\n{_fix_history}\n"
                    ) if _fix_history.strip() else ""
                    # Fix prompt: compile error + bad block + source context only.
                    # Excludes examples (sibling code) — not needed for fixing compile errors,
                    # and was the main cause of fix prompts exceeding the 120K char limit.
                    # Drop low-priority sections if compile_err+block would push total
                    # past TASK0C_PROMPT_CHAR_CAP (120K chars / ~30K tokens).
                    _compile_err_capped = compile_err[-3_000:] if len(compile_err) > 3_000 else compile_err
                    _fix_extra = len(_compile_err_capped) + len(block) + len(_fix_history_section) + 4000
                    _fix_core = secs['cargo'] + secs['full_source'] + secs['fn_bodies'] + secs['symbols']
                    _fix_optional = secs['integ_tests'] + secs['siblings']
                    _FIX_CHAR_LIMIT = TASK0C_PROMPT_CHAR_CAP
                    _fix_ctx = _fix_core + (_fix_optional if len(_fix_core) + len(_fix_optional) + _fix_extra < _FIX_CHAR_LIMIT else "")
                    fix_prompt = f"""The Rust test block you generated for `{rel_path}` does not compile.
Fix ONLY the test block so it compiles. Return the corrected <FILE path="gen_tests_block"> block.
{_fix_history_section}
Compile error (read carefully — this is the ONLY thing to fix):
{_compile_err_capped}

Current test block (fix this):
{block}

--- Full context below — same constraints apply as when you generated the block ---
{_fix_ctx}
Rust rules:
- Each `#[test]` fn must have ZERO arguments.
- Use `use super::*;` to access module items.
- Read field types from the struct definitions in the source file — match EXACTLY.

INTERNAL MODULE RULES — these tests live inside the source file, not in an external test crate:
- Do NOT write `use <crate_name>::...` — you are already inside the crate.
- The template prelude already imports String, Vec, Box, format!, StdError, and fmt — use them directly.
- Do NOT use `#[macro_export]` macros inside this module — they cannot be imported from within the crate.
  Construct values through direct API calls instead.
- Do NOT use `assert_eq!` or `assert_ne!` on types that do not implement `PartialEq`.
  Use `format!("{{}}",  val)`, boolean checks, or `matches!()` instead.
{_nightly_rules_for_cluster()}
See the IMPORTS, PITFALLS, and RULES sections of coverage_strategy.md (shown above) for
project-specific import paths, type construction patterns, and hard constraints.
{_file_constraints_section}"""
                    fix_response = ask(fix_prompt, model=_TASK0C_MODEL, think=None)
                    fixed = extract_test_mod(fix_response)
                    if fixed:
                        block = re.sub(r'\s*</FILE>\s*$', '', fixed).strip()
                        # Use the full _allow_line from _derive_gentest_prelude — not a
                        # hardcoded subset. The crate's #![deny(...)] may require lints
                        # (clippy::all, unsafe_op_in_unsafe_fn) beyond unused_imports.
                        if _allow_line not in block:
                            block = re.sub(
                                r'((?:pub\s+)?mod\s+gen_\w+\s*\{)',
                                f'{_allow_line}\n\\1',
                                block, count=1,
                            )
                    else:
                        _log(f"  [{module_name}] LLM returned no fix — reverting {rel_path}.")
                        _revert_gen_tests_pass(abs_path, _raw_text, _sibling_path)
                        break
            else:
                _log_task0c_attempt(project_dir, rel_path, attempt, "", "", "no_block")
                _log(f"  [{module_name}] No test block extracted for {rel_path}.")

            # Quick per-file coverage snapshot after a successful append.
            _qcov_after: int | None = None
            if _block_committed:
                _qcov_result = _quick_file_coverage(project_dir, module_name, rel_path)
                if _qcov_result is not None:
                    _qcov, _qcov_able = _qcov_result
                    _qcov_after = _qcov
                    _qpct = (_qcov / _qcov_able * 100.0) if _qcov_able else 0.0
                    _log(f"  [{module_name}] {rel_path} quick-cov: {_qcov}/{_qcov_able} ({_qpct:.1f}%)")
                    # Persist result so the monitor can merge it into the files overview.
                    _qcache = Path("/tmp") / f"grcov_filecache_{module_name}.json"
                    try:
                        _cache = json.loads(_qcache.read_text()) if _qcache.exists() else {}
                        _cache[rel_path] = {"covered": _qcov, "coverable": _qcov_able, "ts": time.time()}
                        _qcache.write_text(json.dumps(_cache))
                    except Exception:
                        pass
                    # Track whether THIS block actually moved the metric. If the
                    # previous quick-cov for this file was already known and the
                    # new value didn't increase, this block is concrete evidence
                    # that the LLM's current strategy isn't working — feed it
                    # to the deep retrospective at stall_n == RETRO_DEEP_AT_STALL,
                    # and immediately REVERT the block so the source file isn't
                    # cluttered with dead gen_tests siblings that never produced
                    # coverage. _pass_n is recomputed from disk on the next
                    # generation attempt, so the slot is naturally reused.
                    _prev_qcov = file_last_qcov.get(rel_path)
                    file_last_qcov[rel_path] = _qcov
                    if _prev_qcov is not None and _qcov <= _prev_qcov:
                        file_failed_blocks.setdefault(rel_path, []).append(block)
                        _log(f"  [{module_name}] {rel_path} — block produced no coverage "
                             f"increase (was {_prev_qcov}, now {_qcov}); reverting.")
                        _revert_gen_tests_pass(abs_path, _raw_text, _sibling_path)
                        _block_committed = False
                        file_last_qcov[rel_path] = _prev_qcov  # restore tracker too

            # Inner quick-retry: if quick-cov didn't reach 100% on a stalled or
            # few-lines-remaining file, immediately try a second focused generation
            # targeting only the still-uncovered lines with explicit branch hints.
            # Cheaper than an outer retry (no full grcov run, smaller prompt).
            _INNER_RETRY_LIMIT = 2
            if (
                _block_committed
                and _qcov_after is not None
                and _qcov_able > 0
                and _qcov_after < _qcov_able
                and file_stall_counts.get(rel_path, 0) > 0
            ):
                _src_lines_for_guard = existing_text.splitlines()
                _guard_hints = []
                for _lno in sorted(uncov_lines):
                    _g = _extract_branch_guard(_src_lines_for_guard, _lno)
                    if _g:
                        _guard_hints.append(f"  line {_lno}: `{_g}`")
                _guard_block = (
                    "Branch conditions to exercise:\n" + "\n".join(_guard_hints) + "\n"
                ) if _guard_hints else ""

                for _ir in range(_INNER_RETRY_LIMIT):
                    _log(f"  [{module_name}] {rel_path} quick-retry {_ir + 1}/{_INNER_RETRY_LIMIT} "
                         f"— {len(uncov_lines)} line(s) still uncovered.")
                    _mod_name_retry = f"gen_tests_retry{attempt}_{_ir}"
                    _retry_prompt = (
                        f"The test block you just wrote for `{rel_path}` compiled and ran "
                        f"but did NOT cover these specific lines: {sorted(uncov_lines)}\n\n"
                        f"{_guard_block}"
                        f"The function ran but took the OTHER branch. "
                        f"Write a NEW `#[cfg(test)] mod {_mod_name_retry}` block containing "
                        f"ONLY tests that exercise the specific branch conditions above. "
                        f"Do not repeat tests from the existing block.\n\n"
                        f"Source (uncovered lines marked):\n```rust\n"
                        f"{_annotate_uncovered(existing_text, uncov_lines)[:_SRC_SNIP_CAP]}\n```\n\n"
                        f"Existing test block (already on disk — do NOT duplicate):\n```rust\n"
                        f"{_old_block[:2000] if _old_block else '(none)'}\n```\n\n"
                        f"Output ONLY the new mod block wrapped in <FILE path=\"{rel_path}\">...</FILE>."
                    )
                    _retry_resp = ask(_retry_prompt, model=_TASK0C_MODEL,
                                      max_tokens=8192, think=None)
                    _retry_block = extract_test_mod(_retry_resp)
                    if not _retry_block:
                        _log(f"  [{module_name}] Quick-retry {_ir + 1}: no block extracted.")
                        break
                    _retry_block = re.sub(r'\s*</FILE>\s*$', '', _retry_block).strip()
                    if _allow_line not in _retry_block:
                        _retry_block = re.sub(
                            r'((?:pub\s+)?mod\s+\w+\s*\{)',
                            f'{_allow_line}\n\\1',
                            _retry_block, count=1,
                        )
                    _current_text = abs_path.read_text(encoding="utf-8")
                    _retry_pass_n = _next_gen_tests_n(abs_path)
                    _retry_sibling = _gen_tests_path(abs_path, _retry_pass_n)
                    _retry_decl = _build_gen_tests_decl(_retry_sibling.name, _retry_pass_n, _allow_line,
                                                        cluster_cfg_attr=_cluster_cfg_attr(_current_cluster))
                    _retry_sibling.write_text(_extract_gen_tests_body(_retry_block), encoding="utf-8")
                    abs_path.write_text(_current_text.rstrip() + _retry_decl, encoding="utf-8")
                    _retry_check = subprocess.run(
                        ["cargo", "test", "--no-run", "-p", module_name],
                        cwd=project_dir, capture_output=True, text=True,
                        timeout=BUILD_TIMEOUT, env=_cargo_env(),
                    )
                    if _retry_check.returncode != 0:
                        _log(f"  [{module_name}] Quick-retry {_ir + 1}: compile failed — reverting.")
                        _revert_gen_tests_pass(abs_path, _current_text, _retry_sibling)
                        break
                    _log(f"  [{module_name}] Quick-retry {_ir + 1}: compiled — checking coverage.")
                    _qr_result = _quick_file_coverage(project_dir, module_name, rel_path)
                    if _qr_result is not None:
                        _qr_cov, _qr_able = _qr_result
                        _qr_pct = (_qr_cov / _qr_able * 100.0) if _qr_able else 0.0
                        _log(f"  [{module_name}] {rel_path} quick-retry {_ir + 1} cov: "
                             f"{_qr_cov}/{_qr_able} ({_qr_pct:.1f}%)")
                        _qcache = Path("/tmp") / f"grcov_filecache_{module_name}.json"
                        try:
                            _cache = json.loads(_qcache.read_text()) if _qcache.exists() else {}
                            _cache[rel_path] = {"covered": _qr_cov, "coverable": _qr_able,
                                                "ts": time.time()}
                            _qcache.write_text(json.dumps(_cache))
                        except Exception:
                            pass
                        if _qr_cov > _qcov_after:
                            _log(f"  [{module_name}] Quick-retry {_ir + 1}: improvement "
                                 f"+{_qr_cov - _qcov_after} lines — stopping inner retries.")
                            _qcov_after = _qr_cov
                            _pass_had_work = True
                            break
                    else:
                        break

            # Per-file stall detection: track whether uncovered line count improved.
            prev_best = file_best_uncov.get(rel_path, len(uncov_lines) + 1)
            if len(uncov_lines) < prev_best:
                newly_covered = file_prev_uncov_lines.get(rel_path, set()) - uncov_lines
                file_best_uncov[rel_path] = len(uncov_lines)
                file_stall_counts[rel_path] = 0
                # Capture what worked so next attempt can reuse the same patterns.
                if _old_block:
                    _condensed_working = _condense_rust_to_signatures(_old_block, "gen_tests.rs")
                    _covered_sample = sorted(newly_covered)[:8]
                    _covered_note = (f" Lines newly covered: {_covered_sample}." if _covered_sample else "")
                    file_what_worked[rel_path] = (
                        f"Previous attempt covered {prev_best - len(uncov_lines)} more line(s).{_covered_note} "
                        f"Reuse these construction patterns:\n{_condensed_working[:_WORKING_CAP]}"
                    )
                file_retrospectives.pop(rel_path, None)  # stale retrospective no longer valid
            else:
                file_stall_counts[rel_path] = file_stall_counts.get(rel_path, 0) + 1
                stall_n = file_stall_counts[rel_path]
                # Retrospective is fired twice:
                #   stall_n == 1                — initial classification from source alone
                #   stall_n == RETRO_DEEP_AT_STALL — re-classification with concrete
                #     evidence of failed attempts. Closes the feedback loop: when the
                #     first call said TESTABLE and the LLM has since committed several
                #     blocks that didn't move the metric, the classifier should revise
                #     that call to UNREACHABLE / NEEDS_INTERNALS instead.
                _fire_deep = (
                    stall_n == RETRO_DEEP_AT_STALL
                    and rel_path not in file_retro_deep_fired
                    and bool(file_failed_blocks.get(rel_path))
                )
                if (stall_n == 1 or _fire_deep) and fn_bodies.strip():
                    if _fire_deep:
                        _log(f"  [{module_name}] {rel_path} stalled at {stall_n} with "
                             f"{len(file_failed_blocks[rel_path])} failed attempt(s) — "
                             f"running deep retrospective with concrete evidence.")
                        file_retro_deep_fired.add(rel_path)
                    else:
                        _log(f"  [{module_name}] No improvement for {rel_path} — running retrospective.")
                    # Pass ALL uncovered functions to the retrospective (no max_fns cap)
                    # so the LLM classifies every function in one shot, not one per attempt.
                    _retro_fn_bodies = _extract_uncovered_functions(existing_text, uncov_lines, max_fns=0)
                    retro = _task0c_retrospective(
                        rel_path, existing_text, _retro_fn_bodies, _old_block, module_name,
                        attempt_history=attempt_history,
                        uncov_lines=uncov_lines,
                        unproductive_blocks=file_failed_blocks.get(rel_path, []) if _fire_deep else None,
                    )
                    if retro:
                        file_retrospectives[rel_path] = retro
                        _log(f"  [{module_name}] Retrospective: {retro[:300]}")
                        # Immediately pragma UNREACHABLE/CFG_GATED functions — no point retrying
                        # test generation for code that is intrinsically unmeasurable.
                        _retro_cats = _parse_retrospective_categories(retro)
                        _pragma_fns = _retro_cats["UNREACHABLE"] + _retro_cats["CFG_GATED"]
                        if _pragma_fns:
                            _log(f"  [{module_name}] Targeted pragma for: {_pragma_fns}")
                            _pragma_ok = _task0c_targeted_pragma(
                                project_dir, module_name, rel_path,
                                uncov_lines, _pragma_fns, _TASK0C_MODEL,
                            )
                            if _pragma_ok:
                                # Run quick-cov immediately with the new pragmas on disk.
                                # If the file is now fully covered/excluded, skip it for
                                # all future generation passes — no point burning LLM calls.
                                _post_pragma = _quick_file_coverage(project_dir, module_name, rel_path)
                                if _post_pragma is not None:
                                    _pp_cov, _pp_able = _post_pragma
                                    _pp_pct = (_pp_cov / _pp_able * 100.0) if _pp_able else 100.0
                                    _log(f"  [{module_name}] {rel_path} post-pragma quick-cov: "
                                         f"{_pp_cov}/{_pp_able} ({_pp_pct:.1f}%)")
                                    _qcache = Path("/tmp") / f"grcov_filecache_{module_name}.json"
                                    try:
                                        _cache = json.loads(_qcache.read_text()) if _qcache.exists() else {}
                                        _cache[rel_path] = {"covered": _pp_cov, "coverable": _pp_able, "ts": time.time()}
                                        _qcache.write_text(json.dumps(_cache))
                                    except Exception:
                                        pass
                                    if _pp_cov >= _pp_able:
                                        _log(f"  [{module_name}] {rel_path} fully covered after pragma — "
                                             f"skipping further generation.")
                                        skipped_files.add(rel_path)
                                # Reset stall so the file gets another generation pass for
                                # any remaining TESTABLE/NEEDS_INTERNALS lines.
                                file_stall_counts[rel_path] = 0
                if stall_n >= FILE_STALL_LIMIT:
                    _log(f"  [{module_name}] {rel_path} stalled for {FILE_STALL_LIMIT} consecutive "
                         f"attempts with no improvement — skipping this file.")
                    skipped_files.add(rel_path)

            # Update snapshot for next attempt's line-coverage delta.
            file_prev_uncov_lines[rel_path] = set(uncov_lines)

        # If this entire pass had no LLM calls (all files filtered by cfg/artifact with
        # nothing actionable), there is nothing more the pipeline can do — stop immediately.
        if not _pass_had_work:
            _log(f"  [{module_name}] All files filtered (cfg/artifact) — no actionable uncovered lines. Stopping.")
            _at_ceiling = True
            break

        # If every remaining uncovered file is now skipped, die with a report.
        remaining = set(uncovered_by_file.keys()) - skipped_files
        if not remaining and skipped_files and coverage < 100.0:
            _log(f"\n  [{module_name}] HARD BLOCK: all uncovered files are stalled. "
                 f"Coverage: {coverage:.1f}%. Cannot reach 100%.")
            _log(f"  Stalled files and their uncovered line counts:")
            for sf in sorted(skipped_files):
                n_uncov = len(uncovered_by_file.get(sf, (None, set()))[1])
                history = _get_task0c_history(project_dir, sf)
                retro = file_retrospectives.get(sf, "")
                retro_note = f"\n      retrospective: {retro[:300]}" if retro else ""
                _log(f"    {sf}: {n_uncov} uncovered lines. Last attempts:\n{history}{retro_note}")
            _log(f"  Action required: improve the generation prompt or add manual tests for these files.")
            return coverage, covered_lines, False

        # --- Graceful stop check ---
        # If /tmp/pipeline_graceful_stop exists, finish this attempt cleanly and exit.
        _graceful_sentinel = Path("/tmp/pipeline_graceful_stop")
        if _graceful_sentinel.exists():
            try:
                _graceful_sentinel.unlink()
            except OSError:
                pass
            _log(f"  [{module_name}] Graceful stop requested — attempt {attempt} complete. Exiting.")
            sys.exit(0)

    _log(f"  [{module_name}/{_cluster_label}] Finished augmentation: {coverage:.1f}% coverage after {attempt} attempt(s).")
    return coverage, covered_lines, _at_ceiling


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
        raise RuntimeError(f"git tag listing failed: {e}")
    tags = [t for t in tags if t]
    if not tags:
        # Fall back to legacy pre-strip tag if present.
        tags = subprocess.run(
            ["git", "tag", "-l", "pre-strip-*"],
            cwd=project_dir, capture_output=True, text=True,
        ).stdout.strip().splitlines()
        tags = [t for t in tags if t]
    if not tags:
        raise RuntimeError(
            "No task0c-augmented-* or pre-strip-* tag in the workspace. "
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
        root / f"functional_requirements_{module_name}.md",
        root / FAILURE_LOG_NAME,
        root / ".task1_partials",
    ]
    iteration_paths += list(project_dir.glob(".generated_files_*.txt"))
    iteration_paths += list(root.glob("tests_output_*.txt"))
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
    _log(f"  Cleared iteration entries from pipeline_state.md")
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
    for p in root.glob("tests_output_*.txt"):
        p.unlink()
        _log(f"  Deleted {p.name}")

    # Chunk partials from interrupted chunked task1
    partials_dir = root / ".task1_partials"
    if partials_dir.exists():
        for p in partials_dir.glob("*.txt"):
            p.unlink()
        try:
            partials_dir.rmdir()
        except OSError:
            pass
        _log("  Deleted .task1_partials/")

    # Failure log
    failure_log_path = root / FAILURE_LOG_NAME
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
        try:
            cache_dir.rmdir()
        except OSError:
            pass
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
            return [l.strip() for l in content.splitlines() if l.strip()]
    return []


def _parse_test_counts(test_output: str) -> tuple[int, int]:
    """
    Parse the number of passed and failed tests from cargo test / pytest output.
    Returns (passed, failed). Returns (0, 0) if not parseable.
    """
    passed = failed = 0
    # Cargo: "test result: ok. 3 passed; 0 failed; ..."
    # Cargo: "test result: FAILED. 1 passed; 2 failed; ..."
    for m in re.finditer(r'(\d+)\s+passed', test_output):
        passed += int(m.group(1))
    for m in re.finditer(r'(\d+)\s+failed', test_output):
        failed += int(m.group(1))
    return passed, failed


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
        import urllib.request, json as _json
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

    print(f"\nFetching HuggingFace Hub top models …")
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
    import urllib.request, urllib.error

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
        if candidate.exists():
            project_dir = candidate
        else:
            project_dir = root / args.project
        _git_managed = _is_git_repo(project_dir)

    genesis_path = project_dir / "genesis.md"
    functional_req_path = project_dir / "functional_requirements.md"
    manifest_path = project_dir / MANIFEST_NAME  # legacy whole-workspace manifest

    if not project_dir.exists():
        print(f"ERROR: project directory '{project_dir}' not found.")
        sys.exit(1)

    init_log(root, nolog=args.nolog, progress_mode=args.progress)
    init_events(product=project_dir.name)

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
        "backend": os.getenv("BACKEND", "ollama").lower(),
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

    _backend_name = os.getenv("BACKEND", "ollama").lower()
    if MODEL.startswith("claude") or _backend_name == "claude":
        provider = "Claude (Anthropic API)"
    elif _backend_name == "vllm":
        provider = f"vLLM ({os.getenv('VLLM_MODEL_NAME', MODEL)})"
    elif _backend_name == "sglang":
        provider = f"SGLang ({os.getenv('SGLANG_MODEL_NAME', MODEL)})"
    elif _backend_name == "huggingface":
        provider = f"HuggingFace ({os.getenv('HF_MODEL', MODEL)})"
    else:
        provider = f"Ollama ({MODEL})"
    _log(f"Using model: {MODEL}  |  Provider: {provider}")
    _log(f"Context budget: {MODEL_CONTEXT_CHARS:,} chars  "
         f"(SINGLE_CALL={SINGLE_CALL_CHAR_LIMIT:,}  FILE={FILE_CHAR_LIMIT:,}  "
         f"INTERMEDIATE={INTERMEDIATE_CHAR_LIMIT:,})")

    if _backend_name == "vllm":
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
    global _current_cluster
    _clusters = task0_cfg_scan(project_dir)
    _log(f"\nCompiler clusters identified: {len(_clusters)}")
    for _c in _clusters:
        _env_desc = ", ".join(f"{k}+={v}" for k, v in _c.env_extra.items()) if _c.env_extra else "default build"
        _log(f"  cluster/{_c.name}  — {_env_desc}")
    _mark_phase(project_dir, "cfg_scan", "done",
                detail=f"{len(_clusters)} clusters: {', '.join(c.name for c in _clusters)}",
                source_hash=_src_hash)

    test_command = discover_test_command(readme_text, project_dir=project_dir)

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
        if strategy_path.exists() and strategy_path.read_text().strip():
            coverage_strategy = strategy_path.read_text().strip()
            if not _phase_valid(project_dir, "task0b"):
                _mark_phase(project_dir, "task0b", "done",
                            detail=f"{len(coverage_strategy)} chars (existing artifact)")
            _log(f"  task0b already done — loading coverage_strategy.md ({len(coverage_strategy):,} chars).")
        else:
            coverage_strategy = task0b_discovery(
                strategy_path, project_dir, test_files, readme_text, project_profile,
                clusters=_clusters,
            )
            _mark_phase(project_dir, "task0b", "done",
                        detail=f"{len(coverage_strategy)} chars")
            git_commit(project_dir, "pipeline: task0b done")

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

    failure_log_path = root / FAILURE_LOG_NAME

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
            _cov, _covered, _at_ceiling = task0c_augment_module(
                project_dir, _mod_name, _mod_dir, _crate_type,
                existing_test_files=_etf_pre,
                project_context=project_context,
                coverage_strategy=coverage_strategy,
            )
            _log(f"  TASK 0c [{_cluster.name}]: '{_mod_name}' coverage: {_cov:.1f}%")
            _emit("task.done", task="task0c", cluster=_cluster.name, module=_mod_name,
                  duration_ms=int((time.monotonic() - _task0c_t0) * 1000),
                  data={"coverage_pct": _cov, "covered_lines": _covered,
                        "at_ceiling": _at_ceiling})
            _mark_phase(project_dir, f"task0c/{_cluster.name}/{_mod_name}",
                        "done" if _cov >= 100.0 else "in_progress",
                        detail=f"{_cov:.1f}%", source_hash=_src_hash)
            _set_current(project_dir, f"task0c/{_cluster.name}/{_mod_name} / {_cov:.1f}%")

            _state = _module_states.setdefault(
                _mod_name, ModuleAugmentState(_mod_name, _cluster.name)
            )
            _state.record_pass(_cov, _covered, _at_ceiling)
            _status = _state.status(_crate_type)

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
                    f"  HARD RULE (FR-018): '{_mod_name}' [{_cluster.name}] coverage {_cov:.1f}% < 100% "
                    f"(stall {_state.stall_passes}/{ModuleAugmentState.STALL_LIMIT}, "
                    f"total {_state.total_passes}, best covered {_state.best_covered_lines} lines). "
                    f"Retrying."
                )
                _module_augmented_path(project_dir, _mod_name).unlink(missing_ok=True)
                continue

        _mark_phase(project_dir, f"task0c/{_cluster.name}", "done",
                    detail=f"{len(_cluster_done)} modules", source_hash=_src_hash)
        _log(f"\n  cluster/{_cluster.name} complete — {len(_cluster_done)} module(s) done.")
        _emit("cluster.done", cluster=_cluster.name,
              duration_ms=int((time.monotonic() - _cluster_t0) * 1000),
              data={"modules_done": sorted(_cluster_done)})
        _cov_summary = ", ".join(
            f"{_m}:{_cluster_augmented_path(project_dir, _cluster.name, _m).read_text().strip()}"
            for _m in _cluster_done
            if _cluster_augmented_path(project_dir, _cluster.name, _m).exists()
        )
        git_commit(project_dir, f"pipeline: task0c/{_cluster.name} — {_cov_summary}")

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
            if _final_cov < 100.0:
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
            strip_sentinel = project_dir / MODULE_STRIPPED_DIR / module_name
            if strip_sentinel.exists():
                _log(f"  '{module_name}' already stripped — skipping.")
            else:
                if crate_type != "proc-macro" and TARPAULIN_BIN.exists():
                    _sentinel_path = _module_augmented_path(project_dir, module_name)
                    try:
                        _strip_cov = float(_sentinel_path.read_text().strip().rstrip('%'))
                    except (ValueError, OSError):
                        _strip_cov = 0.0
                    if _strip_cov < 100.0:
                        _log(
                            f"\n  WARNING at strip: '{module_name}' coverage is {_strip_cov:.1f}% "
                            f"— stripping anyway. Reconstruction may be incomplete."
                        )
                # Capture API surface BEFORE strip — structural ground truth for
                # TASK 1. The .original_structure/ dir survives strip (lives at the
                # project root, not under src/), so TASK 1 can read it post-strip.
                _capture_api_surface(project_dir, module_dir, module_name)
                if not git_commit(project_dir,
                                  f"pipeline: task0c augmentation complete — "
                                  f"all unit tests added for {module_name}"):
                    _log(f"  [ABORT] augmentation commit failed for '{module_name}' — refusing to strip. Fix git state and retry.")
                    raise RuntimeError(f"augmentation commit failed for {module_name}")
                # Tag the augmented commit. The new tag name is the canonical
                # restore point used by --restart-from-augmented; the legacy
                # pre-strip-{module} tag is kept as an alias for anything that
                # already pinned to it.
                git_tag(project_dir, f"task0c-augmented-{module_name}")
                git_tag(project_dir, f"pre-strip-{module_name}")
                strip_module_source(project_dir, module_dir)
                strip_sentinel.parent.mkdir(parents=True, exist_ok=True)
                strip_sentinel.touch()
                _mark_phase(project_dir, f"strip/{module_name}", "done",
                            source_hash=_src_hash)
                _set_current(project_dir, f"strip/{module_name} / done")
                git_commit(project_dir, f"pipeline: stripped — {module_name}")

        # Re-collect after strip — picks up any tests generated by TASK 0c.
        # test_files: full read set for TASK 1 (includes inline test blocks in src/)
        # existing_test_files: write-protection set for TASK 2 (pure test files only)
        test_files = collect_test_files(project_dir)
        existing_test_files = collect_protected_test_files(project_dir)
        _log(f"  Test files after strip: {len(test_files)} readable, "
             f"{len(existing_test_files)} write-protected.")

        # --- TASK 1: extract requirements from this module's tests ---
        module_req_path = root / f"functional_requirements_{module_name}.md"

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
            if module_req_path.exists() and t1_attempt == 0:
                _log(f"\nTASK 1: requirements for '{module_name}' already extracted — skipping.")
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
                _log(f"  TASK 1 output validated OK.")
                t1_valid = True
                _mark_phase(project_dir, f"task1/{module_name}", "done",
                            detail=f"{module_req_path.stat().st_size} chars",
                            source_hash=_src_hash)
                _set_current(project_dir, f"task1/{module_name} / done")
                git_commit(project_dir, f"pipeline: task1 — {module_name}")
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
                    module_iter, root,
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

        # Save any gen_tests siblings + their declarations BEFORE wiping.
        # TASK 0c writes test code to foo.gen_tests_N.rs siblings; TASK 2 regenerates
        # source files. We reattach siblings + decls after each TASK 2 write so TASK 3
        # can actually run the tests.
        # Schema: rel_path of parent → list of (pass_n, sibling_body)
        saved_gen_blocks: dict[str, list[tuple[int, str]]] = {}
        if module_dir.exists():
            for src_file in sorted(module_dir.rglob("*.rs")):
                rel = str(src_file.relative_to(project_dir))
                if rel in existing_test_files:
                    continue
                if re.search(r'\.gen_tests_\d+\.rs$', src_file.name):
                    continue  # siblings handled via their parent
                try:
                    siblings = _gen_tests_siblings(src_file)
                    if siblings:
                        saved_gen_blocks[rel] = [
                            (n, p.read_text(encoding="utf-8"))
                            for n, p in siblings
                        ]
                except Exception:
                    pass
        if saved_gen_blocks:
            _total = sum(len(v) for v in saved_gen_blocks.values())
            _log(f"  Saved {_total} gen_tests sibling(s) from {len(saved_gen_blocks)} file(s) for reattachment.")

        # Wipe any previously generated files for this module (interrupted run).
        if module_dir.exists():
            wiped = []
            for src_file in sorted(module_dir.rglob("*.rs")):
                rel = str(src_file.relative_to(project_dir))
                if rel not in existing_test_files:
                    src_file.unlink()
                    wiped.append(rel)
            if wiped:
                _log(f"  Wiped {len(wiped)} leftover generated file(s): {', '.join(wiped)}")
            # Stub so workspace parses while we generate.
            _write_workspace_stub(crate_type, module_dir)

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
                    if saved_gen_blocks:
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
                            if added_decls > 0:
                                src_path.write_text(new_text, encoding="utf-8")
                                _log(f"  Reattached {added_decls} gen_tests decl(s) to {rel}.")

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
                    passed, test_output = task3(
                        project_dir, module_test_cmd, label, root, genesis_path
                    )
                    if not api_conformance_ok:
                        test_output = (
                            test_output
                            + "\n\n--- API surface drift (pre-test) ---\n"
                            + api_conformance_report
                            + "\n"
                        )
                    pass_count, fail_count = _parse_test_counts(test_output)

                    # Anti-corruption: classify the test result before deciding what to do.
                    t3_status, t3_reason = validate_task3_output(
                        pass_count, fail_count, module_has_tests
                    )
                    _log(f"  TASK 3 result: {t3_status} — {t3_reason}")

                    if t3_status == "pass":
                        _mark_phase(project_dir, f"task2-4/{module_name}", "done",
                                    detail=f"iteration {module_iter}", source_hash=_src_hash)
                        _set_current(project_dir, f"task2-4/{module_name} / LOCKED iter {module_iter}")
                        git_commit(project_dir, f"pipeline: task3 iter {module_iter} — {module_name} tests pass")
                        git_tag(project_dir, f"passing-{module_name}-{module_iter}")
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
                        _log(f"  Module has no test files — locking on build success.")
                        _mark_phase(project_dir, f"task2-4/{module_name}", "done",
                                    detail=f"no tests iter {module_iter}", source_hash=_src_hash)
                        _set_current(project_dir, f"task2-4/{module_name} / LOCKED (no tests)")
                        if is_multi_module:
                            lock_module(project_dir, module_name, module_dir, module_manifest)
                            locked_modules.add(module_name)
                            git_commit(project_dir, f"pipeline: locked (no tests) — {module_name}")
                        else:
                            _log(f"\nSUCCESS: build passed with no module tests.")
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
                            pass_count, fail_count = _parse_test_counts(test_output)
                            t3_status, _ = validate_task3_output(
                                pass_count, fail_count, module_has_tests
                            )
                            if t3_status == "pass":
                                if is_multi_module:
                                    lock_module(project_dir, module_name, module_dir, module_manifest)
                                    locked_modules.add(module_name)
                                else:
                                    _log(f"\nSUCCESS: all tests passed after fix loop.")
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
            t4_ok, current_pass = task4(
                genesis_path, module_req_path, test_output, module_iter, root,
                failure_type=failure_type, failure_log=failure_log_content,
                prev_pass_count=prev_pass_count, prev_test_output=prev_test_output,
                source_context=t4_source_context,
                api_surface=load_api_surface(project_dir, module_name),
            )
            genesis_after = read(genesis_path)

            # Anti-corruption: detect genesis stall (TASK 4 produced no update).
            t4_valid, t4_reason = validate_task4_output(genesis_before, genesis_after, test_output)
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

            # Clean generated files; restore stub so workspace stays parseable.
            _log(f"\n  Cleaning up '{module_name}' generated files for retry...")
            delete_source_files(project_dir, module_manifest,
                                module_dir=module_dir,
                                existing_test_files=existing_test_files)
            if is_multi_module:
                _write_workspace_stub(crate_type, module_dir)

            module_iter += 1

    # -----------------------------------------------------------------------
    # Final: run full test suite to validate the integrated workspace.
    # Only reached in multi-module mode (single-module exits on pass above).
    # -----------------------------------------------------------------------
    _log(f"\n{'=' * 60}")
    _log("FINAL: Running full test suite to validate integrated workspace")
    _log(f"{'=' * 60}")
    passed, _ = task3(project_dir, test_command, "final", root, genesis_path)
    if passed:
        _log("\nSUCCESS: all tests passed.")
        sys.exit(0)
    else:
        _log("\nWARNING: all modules locked individually but full suite failed.")
        _log("         Integration issues between modules may require attention.")
        sys.exit(1)


if __name__ == "__main__":
    main()

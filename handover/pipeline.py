#!/usr/bin/env python3
"""
Pipeline for the deterministic process loop.

Usage:
  python pipeline.py --project <subfolder>
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from model_interact import ask, MODEL
from strip_to_tests import detect_language, process_rust

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

# ~4 chars per token. With num_ctx=8192 and prompt overhead of ~1k tokens,
# the safe content budget is ~7000 tokens = ~28k chars per call.
SINGLE_CALL_CHAR_LIMIT = 20_000

# Max chars per chunk when splitting test files across multiple calls.
# Sized to fit in num_ctx=8192 with room for prompt and response.
CHUNK_CHAR_LIMIT = 20_000

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
# Sized to leave room for prompt overhead within the 8192-token context window.
INTERMEDIATE_CHAR_LIMIT = 16_000

# Number of chunk partials merged per intermediate group in stage-1 synthesis.
# 276 chunks / 10 per group = ~28 intermediates → ~28 * ~800 chars = ~22k for final synthesis.
SYNTHESIS_GROUP_SIZE = 10

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
TASK4_DOC_LIMIT = 20_000

# Failure log: running record of every iteration's outcome. Fed into TASK 4.
FAILURE_LOG_NAME = "failure_log.md"

# Directory for locked module source backups (bottom-up integration).
LOCKED_MODULES_DIR = ".locked_modules"

# Sentinel file written by TASK 0c when coverage augmentation is complete.
COVERAGE_AUGMENT_SENTINEL = ".coverage_augmented"

# Path to cargo-tarpaulin binary (installed via `cargo install cargo-tarpaulin`).
TARPAULIN_BIN = Path.home() / ".cargo" / "bin" / "cargo-tarpaulin"

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
    print(msg)
    sys.stdout.flush()
    if not _nolog and _log_path is not None:
        with _log_path.open("a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def init_log(root: Path, nolog: bool = False, progress_mode: bool = False):
    global _log_path, _nolog, _progress_mode
    _nolog = nolog
    _progress_mode = progress_mode
    if not nolog:
        _log_path = root / LOG_NAME
    _log(f"\n{'=' * 60}")
    _log(f"{'=' * 60}")
    _log(f"  PIPELINE STARTED — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
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
    # Last resort: extract from code fence — model often wraps genesis in ```markdown ... ```
    fence = re.search(r'```(?:markdown|md|text)?\n(.*?)```', text, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
        if len(content) > 100:
            return content
    return None


def read(path):
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


def smart_truncate(text, total_limit=8000, head=2000, tail=6000):
    """
    Keep the beginning (build errors, first failures) and end (summary) of
    output. Truncating from the middle avoids losing either signal.
    """
    if len(text) <= total_limit:
        return text
    omitted = len(text) - head - tail
    return text[:head] + f"\n\n[... {omitted:,} chars omitted ...]\n\n" + text[-tail:]


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
                results.append((str(rel), content[:4000]))  # cap per-file to avoid overflow
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

    def _parse(self):
        cargo_tomls = sorted(self._project_dir.rglob("Cargo.toml"))
        if not cargo_tomls:
            return

        for ct in cargo_tomls:
            rel = ct.relative_to(self._project_dir)
            if any(part in SKIP_DIR_NAMES for part in rel.parts):
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
                results.append((str(rel), content[:4000]))
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
    Classify whether a test run failure is a BUILD error (code did not compile)
    or a TEST failure (code compiled but tests failed at runtime).

    Returns "build" or "test".

    This distinction drives the recovery path:
      - build → fix code generation (TASK 2 inner loop), do NOT touch genesis.md
      - test  → analyze requirements gap (TASK 4), update genesis.md
    """
    build_signals = [
        "error[E",                 # Rust compiler error
        "error: could not compile",
        "failed to compile",
        "failed to load manifest",
        "failed to parse manifest",
        "error: aborting due to",
        "cannot find",             # Rust: cannot find type/function X
        "unresolved import",
        "module not found",
        "ModuleNotFoundError",     # Python
        "SyntaxError",             # Python
        "Cannot find module",      # Node
        "TSError",                 # TypeScript
        "build failed",
        "BUILD FAILED",
        "CompilationError",
    ]
    test_signals = [
        "FAILED",
        "test result: FAILED",
        "failures:",
        "panicked at",
        "FAIL\t",
        "AssertionError",
        "pytest",
        "test ... FAILED",
    ]

    output_lower = test_output.lower()
    build_score = sum(1 for s in build_signals if s.lower() in output_lower)
    test_score = sum(1 for s in test_signals if s.lower() in output_lower)

    return "build" if build_score > test_score else "test"


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


def load_failure_log(log_path: Path, max_chars: int = 8000) -> str:
    """Return the tail of the failure log, truncated to max_chars."""
    content = read(log_path)
    if not content:
        return ""
    if len(content) > max_chars:
        content = "[... earlier entries omitted ...]\n\n" + content[-max_chars:]
    return content


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
            result.append((str(rel), content))
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
    current = []
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


# ---------------------------------------------------------------------------
# Startup: discover and validate the test command from README.md
# ---------------------------------------------------------------------------

def discover_test_command(readme_text: str, project_dir: Path | None = None) -> str:
    """
    Ask the configured model to extract the primary test command from README.md.
    If README.md is empty/missing, ask the model to infer a sensible default from
    common conventions (cargo test, pytest, npm test, etc.).
    Retries if the result is empty.
    """
    if readme_text.strip():
        _log(f"Initializing: asking {MODEL} to extract test command from README.md...")
        prompt = f"""Read this README.md and extract the single primary shell command used to run the full test suite.
Output only the command on one line. No explanation, no markdown fences, no alternatives.
If multiple test commands exist, pick the one that runs all tests.

README.md:
{readme_text}
"""
    else:
        _log(f"Initializing: no README.md -- asking {MODEL} to infer test command from project conventions...")
        prompt = """No README.md is available. Output the single most common shell command used to run
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
    return cmd


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


def with_retry(fn, retries=MAX_RETRIES):
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
            raise
        if result is not False:
            return result
        if attempt < retries:
            _log(f"  Attempt {attempt}/{retries} failed, retrying...")
        attempt += 1
    _log(f"  All {retries} attempts failed.")
    return False


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

def task0_project_profile(profile_path: Path, test_files, readme_text: str) -> str:
    """
    Run once at startup. Identifies the project's language, build system, and
    workspace/module structure from README.md and test file paths alone.
    Produces project_profile.md which is passed to every subsequent task so the
    model never has to re-derive (and potentially mis-derive) the structure.
    Returns the profile content.
    """
    if profile_path.exists():
        content = profile_path.read_text().strip()
        if content:
            _log(f"  Project profile already exists ({profile_path.name}), loading.")
            return content

    _log("\nTASK 0: Identifying project structure (language, build system, workspace layout)")

    # Pass only file paths — not content — to keep the prompt small and focused.
    test_paths = "\n".join(rel for rel, _ in test_files)

    prompt = f"""You are identifying the structure of a software project from its README and test file paths.

README.md:
{readme_text}

Test file paths (relative to project root):
{test_paths}

Produce a concise project_profile.md that answers the following:
1. Primary language and version (if specified).
2. Build system and test runner (e.g. "Cargo + cargo-nextest", "npm + jest", "gradle + junit").
3. Project layout: is it a monorepo / workspace? If so, list each top-level module/crate/package
   with its directory path and whether it is a library, binary, or both.
4. Minimum required files for each module/crate/package so that the build system accepts it
   (e.g. for Rust: Cargo.toml with [lib] section + src/lib.rs; for Node: package.json + index.js).
5. Any other structural conventions visible from the test paths (e.g. test files live in
   <crate>/tests/, source in <crate>/src/).

Be specific and concrete. This profile will be used to generate source files, so accuracy matters.
Do not mention the experiment or iteration process. Output only the profile document.

<FILE path="project_profile.md">
content here
</FILE>
"""

    def attempt():
        response = ask(prompt, model=_TASK0_MODEL)
        files = extract_files(response)
        if "project_profile.md" not in files:
            # Fallback: model wrapped content in markdown fences instead of <FILE> tags.
            # Strip fences and use the raw response if it looks like profile content.
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
        _log(f"  project_profile.md written ({len(content):,} chars).")
        return content

    result = with_retry(attempt)
    if result is False:
        _log("  WARNING: could not produce project_profile.md after all retries. Continuing without it.")
        return ""
    return result


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
    ) -> bool:
        """Write functional_requirements.md (and genesis stub if absent). Returns True on success."""


class StaticTask1Synthesizer(Task1Synthesizer):
    """
    Current implementation: extract behavioral requirements in natural language,
    then synthesize into functional_requirements.md.
    Uses a single LLM call for small projects and a chunked+hierarchical approach
    for large ones.
    """

    def synthesize(self, project_dir, genesis_path, functional_req_path, test_files,
                   readme_text, project_profile=""):
        existing_genesis = read(genesis_path)
        genesis_exists = bool(existing_genesis.strip())
        total_chars = sum(len(c) for _, c in test_files)

        if total_chars <= SINGLE_CALL_CHAR_LIMIT:
            ok = with_retry(lambda: self._single(
                genesis_path, functional_req_path, test_files, readme_text,
                existing_genesis, project_profile
            ))
        else:
            ok = self._chunked(
                genesis_path, functional_req_path, test_files, readme_text,
                existing_genesis, project_profile
            )
        return ok

    def _single(self, genesis_path, functional_req_path, test_files, readme_text,
                existing_genesis, project_profile=""):
        genesis_exists = bool(existing_genesis.strip())
        tests_section = format_test_files(test_files)
        profile_section = (f"\n--- Project profile (language, build system, workspace layout) ---\n"
                           f"{project_profile}\n") if project_profile.strip() else ""

        if not genesis_exists and not genesis_path.exists():
            write(genesis_path, "# genesis.md\n\n(populated by TASK 4 after first iteration)\n")

        prompt = self._single_prompt(
            tests_section, readme_text, profile_section, existing_genesis
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
                       existing_genesis: str) -> str:
        """Return the prompt for single-call requirements extraction (overridable)."""
        genesis_exists = bool(existing_genesis.strip())
        if genesis_exists:
            return f"""You are working on a deterministic AI-driven software reconstruction experiment.

The goal: given a project's test files and README, produce functional requirements precise
enough that any implementation satisfying them will make all tests pass.

Use the methodology in genesis.md below to guide your analysis.
Use the README and test files to understand the language, framework, and project structure.

README.md:
{readme_text}
{profile_section}
Test files:
{tests_section}

--- genesis.md (read-only -- do not output or modify it) ---
{existing_genesis}

Your task: produce functional_requirements.md by applying the genesis.md methodology to
the test files above. Requirements must be complete enough that any implementation
satisfying them will pass all tests.

{FILE_FORMAT_INSTRUCTION}
Only output the file `functional_requirements.md` using the markers above.
"""
        return f"""You are working on a deterministic AI-driven software reconstruction experiment.

Given a project's test files and README, produce functional requirements precise enough
that any implementation satisfying them will make all tests pass.

Use the README and test files to understand the language, framework, and project context.
Do not assume any specific language or technology -- derive everything from what you read.

README.md:
{readme_text}
{profile_section}
Test files:
{tests_section}

Your task: produce functional_requirements.md -- complete functional requirements for this
project that guarantee a passing implementation.

{FILE_FORMAT_INSTRUCTION}
Only output the file `functional_requirements.md` using the markers above.
"""

    def _synthesize_group(self, group_partials: list[str], group_idx: int, total_groups: int,
                          readme_text: str, profile_section: str) -> str | None:
        """
        Stage-1 hierarchical synthesis: merge a group of raw chunk partials into a
        single condensed intermediate summary. Called once per group.
        Returns the summary text, or None on failure.
        """
        combined = "\n\n".join(group_partials)
        if len(combined) > INTERMEDIATE_CHAR_LIMIT * 2:
            combined = smart_truncate(combined, total_limit=INTERMEDIATE_CHAR_LIMIT * 2,
                                      head=INTERMEDIATE_CHAR_LIMIT // 2,
                                      tail=INTERMEDIATE_CHAR_LIMIT * 3 // 2)

        prompt = f"""You are merging a group of partial functional requirements extracted from test files.

This is group {group_idx} of {total_groups}. Each entry was extracted independently from a chunk
of test files. Your task: produce a single, deduplicated, concise summary of all requirements
in this group. Preserve all specific types, function signatures, error conditions, and invariants.
Remove duplicate entries. Do not invent new requirements not present in the input.
{profile_section}
README.md (for language/framework context):
{readme_text}

Partial requirements to merge:
{combined}

Output a plain structured list of the merged requirements. No filler, no code, no headers.
"""

        def attempt():
            result = ask(prompt, model=_TASK1_MODEL)
            if not result or not result.strip():
                return False
            return result

        return with_retry(attempt)

    def _chunked(self, genesis_path, functional_req_path, test_files, readme_text,
                 existing_genesis, project_profile=""):
        chunks = chunk_test_files(test_files, CHUNK_CHAR_LIMIT)
        total_chunks = len(chunks)
        _log(f"  Test files too large for a single call — split into {total_chunks} chunk(s).")
        _log("  Calibration in progress, progress estimations will be available soon...")

        root = genesis_path.parent
        partials_dir = root / ".task1_partials"
        cache_dir = root / CHUNK_CACHE_DIR
        partials_dir.mkdir(exist_ok=True)
        cache_dir.mkdir(exist_ok=True)

        partial_requirements = []
        progress = ChunkProgress(total=total_chunks)
        cache_hits = 0

        for i, chunk in enumerate(chunks, 1):
            partial_file = partials_dir / f"chunk_{i:03d}_of_{total_chunks:03d}.txt"
            tests_section = format_test_files(chunk)
            h = _chunk_hash(tests_section)

            if partial_file.exists():
                entry = partial_file.read_text()
                partial_requirements.append(entry)
                progress.finish_chunk(i)
                continue

            cached = _load_cached_chunk(cache_dir, h)
            if cached:
                cache_hits += 1
                partial_file.write_text(cached)
                partial_requirements.append(cached)
                _log(f"  Chunk {i}/{total_chunks}: cache hit (skipping model call).")
                progress.finish_chunk(i)
                continue

            profile_header = (f"\nProject context:\n{project_profile}\n") if project_profile.strip() else ""
            prompt = self._chunk_prompt(i, total_chunks, readme_text, tests_section, profile_header)

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
            partial_requirements.append(entry)

        progress.finish()

        if cache_hits:
            _log(f"  Cache: {cache_hits}/{total_chunks} chunks reused from previous iterations.")

        genesis_exists = bool(existing_genesis.strip())
        profile_section = (f"\n--- Project profile (language, build system, workspace layout) ---\n"
                           f"{project_profile}\n") if project_profile.strip() else ""

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
                summary = self._synthesize_group(group, g_idx, total_groups, readme_text, profile_section)
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
            readme_text, profile_section, existing_genesis, all_for_synthesis
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
                          existing_genesis: str, all_for_synthesis: str) -> str:
        """Return the Stage-2 synthesis prompt (overridable by subclasses)."""
        genesis_exists = bool(existing_genesis.strip())
        if genesis_exists:
            return f"""You are synthesizing requirements into a final requirements document.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

Use the methodology in genesis.md below to guide synthesis.
Derive the language and framework from the README and requirements below.

README.md:
{readme_text}
{profile_section}
--- genesis.md (read-only -- do not output or modify it) ---
{existing_genesis}

Requirements (merged from all test chunks):
{all_for_synthesis}

Your task: produce functional_requirements.md -- the complete, deduplicated, final
requirements for this specific project.
"""
        return f"""You are synthesizing requirements into a final requirements document.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.

Derive the language and framework from the README and requirements below.

README.md:
{readme_text}
{profile_section}
Requirements (merged from all test chunks):
{all_for_synthesis}

Your task: produce functional_requirements.md -- the complete, deduplicated, final
requirements for this specific project.

{FILE_FORMAT_INSTRUCTION}
Only output the file `functional_requirements.md` using the markers above.
"""

    def _chunk_prompt(self, chunk_idx: int, total_chunks: int, readme_text: str,
                      tests_section: str, profile_header: str) -> str:
        """Return the per-chunk extraction prompt (overridable by subclasses)."""
        return f"""You are extracting functional requirements from a subset of test files for a software project.

This is chunk {chunk_idx} of {total_chunks} -- there are more test files not shown here.
All chunks will be synthesized into one complete requirements document after all chunks are processed.
Focus only on what you can directly observe in the tests below -- do not invent behavior.
{profile_header}
README.md:
{readme_text}

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
                       existing_genesis: str) -> str:
        genesis_exists = bool(existing_genesis.strip())
        genesis_section = (f"\n--- genesis.md (read-only — do not output or modify it) ---\n"
                           f"{existing_genesis}\n") if genesis_exists else ""
        return f"""You are performing contract extraction from test files as part of a
contract-first TDD reconstruction experiment.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
Do not output anything outside those markers.
{profile_section}
README.md:
{readme_text}
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

Rules:
  - Only include what the tests literally call or assert — do not invent.
  - Use exact names, types, and paths as written in the tests.
  - Include concrete type parameters where generics appear in the tests.
  - Group by module/crate. Use concise structured notation, not prose.
  - Do NOT include `fn test_*` or `#[test]` functions — these are the tests themselves, not the code under test.
    Extract the APIs that the test functions CALL (e.g. `Bash::generate(...)`, not `fn test_bash_generator()`).
  - Do NOT include `make_mock_command` or other test helpers — extract the real production API being tested.
"""

    def _synthesis_prompt(self, readme_text: str, profile_section: str,
                          existing_genesis: str, all_for_synthesis: str) -> str:
        genesis_exists = bool(existing_genesis.strip())
        genesis_section = (f"\n--- genesis.md (read-only — do not output or modify it) ---\n"
                           f"{existing_genesis}\n") if genesis_exists else ""
        return f"""You are producing the final API contract document from contract extractions
across all test chunks. This is the last step of a contract-first TDD reconstruction.
{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
Do not output anything outside those markers.
{profile_section}
README.md:
{readme_text}
{genesis_section}
Contract extractions (merged from all test chunks):
{all_for_synthesis}

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
                      tests_section: str, profile_header: str) -> str:
        return f"""You are performing contract extraction from test files as the first step of
a contract-first TDD reconstruction experiment.

This is chunk {chunk_idx} of {total_chunks}. All chunks will be combined into a single
implementation contract document.
{profile_header}
README.md:
{readme_text}

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
                          readme_text: str, profile_section: str) -> str | None:
        """
        Merge a group of contract partials into a deduplicated intermediate contract list.
        Overrides the behavioral merge prompt with a contract-aware one.
        """
        combined = "\n\n".join(group_partials)
        if len(combined) > INTERMEDIATE_CHAR_LIMIT * 2:
            combined = smart_truncate(combined, total_limit=INTERMEDIATE_CHAR_LIMIT * 2,
                                      head=INTERMEDIATE_CHAR_LIMIT // 2,
                                      tail=INTERMEDIATE_CHAR_LIMIT * 3 // 2)

        prompt = f"""You are merging contract extractions from multiple test file chunks.

This is group {group_idx} of {total_groups}. Each entry lists API contracts extracted
from a subset of test files. Your task: produce a single deduplicated contract list.

Rules:
  - Preserve exact module paths, function signatures, types, and invariants.
  - Merge duplicate entries (same symbol, same signature) into one.
  - Do not add, invent, or generalise — only what is present in the input.
  - If two entries conflict (same name, different signature), keep both and annotate with "(conflict)".
{profile_section}
README.md (for language/framework context):
{readme_text}

Contract partials to merge:
{combined}

Output a plain structured list of the merged contracts. No prose, no code blocks, no headers.
"""

        def attempt():
            result = ask(prompt, model=_TASK1_MODEL)
            if not result or not result.strip():
                return False
            return result

        return with_retry(attempt)


def _task1_synthesizer_factory() -> Task1Synthesizer:
    backend = os.getenv("TASK1_SYNTHESIZER", "llm").lower()
    if backend == "static":
        return StaticTask1Synthesizer()
    return LLMTask1Synthesizer()


def _task1_proc_macro(project_dir, genesis_path, functional_req_path, test_files,
                      readme_text, project_profile):
    """
    Specialised TASK 1 for proc-macro crates.
    Instead of extracting runtime API contracts, extracts the proc-macro interface:
    - Which #[derive(...)] macros are provided
    - Which helper attributes (#[module(...)]) are used on fields/variants
    - What trait implementations the derives must generate
    """
    _log("  [proc-macro mode] Extracting derive macro interface from test usage.")

    # Prefilter: only include files that use #[derive(...)] or #[module_attr(...)].
    # This avoids blowing up the context with all 16 test files.
    relevant = [
        (rel, content) for rel, content in test_files
        if re.search(r'#\[derive\(|#\[argh\(|#\[clap\(|#\[structopt\(', content)
    ]
    if not relevant:
        relevant = test_files[:3]  # Fallback: first 3 files
    _log(f"  [proc-macro mode] Using {len(relevant)} file(s) with derive/attribute usage.")

    tests_section = "\n\n".join(
        f'<TESTFILE path="{rel}">\n{content}\n</TESTFILE>'
        for rel, content in relevant
    )
    # Truncate if still large
    if len(tests_section) > 24_000:
        tests_section = tests_section[:24_000] + "\n...[truncated]"
    existing_genesis = read(genesis_path)
    genesis_section = (
        f"\n--- genesis.md ---\n{existing_genesis}\n"
        if existing_genesis.strip() else ""
    )

    prompt = f"""You are analysing test files to extract the interface of a Rust proc-macro crate.

A proc-macro crate provides #[derive(...)] macros and/or #[attribute] macros used in the
test files below. Your task is to produce functional_requirements.md describing ONLY what
this proc-macro crate must implement — not the runtime API of dependent crates.

{FILE_FORMAT_INSTRUCTION}
Your output MUST be the file `functional_requirements.md` wrapped in the FILE markers above.
{genesis_section}
README.md:
{readme_text}

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
          project_profile: str = "", crate_type: str = ""):
    _log("\nTASK 1: Analyzing tests + README, building functional_requirements.md")

    # For proc-macro crates with no own tests, extract proc-macro interface
    # (derive macros, helper attributes) from how they're used in the test suite.
    if crate_type == "proc-macro":
        return _task1_proc_macro(project_dir, genesis_path, functional_req_path,
                                 test_files, readme_text, project_profile)

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
        project_dir, genesis_path, functional_req_path, test_files, readme_text, project_profile
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
    ) -> bool:
        """Write source files to disk. Returns True on success."""


class StaticTask2Generator(Task2Generator):
    """
    Current implementation: one LLM call per attempt that produces all source
    files in a single response using <FILE path="..."> markers.
    """

    def generate(self, project_dir, genesis_path, functional_req_path, manifest_path,
                 existing_test_files, project_profile="", workspace_structure="", build_error="",
                 locked_dep_context="", current_module="", module_dir_rel="",
                 whole_project_context=""):
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

        def attempt():
            prompt = f"""You are implementing a software project from functional requirements.

Process (genesis.md):
{genesis}
{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{whole_project_context}
Functional requirements:
{functional_req}

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
                 whole_project_context=""):
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

        # Step 1: ask model for the list of files to create.
        plan_prompt = f"""You are planning the implementation of a software project.

Process (genesis.md):
{genesis}
{profile_section}{workspace_section}{module_scope_section}{locked_dep_section}{build_error_section}{whole_project_context}
Functional requirements:
{functional_req}

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
{genesis}
{profile_section}{module_scope_section}{locked_dep_section}{whole_project_context}{prev_files_section}
Functional requirements:
{functional_req}

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
                 whole_project_context=""):
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
            )

        # Filter to module scope and exclude pure test files.
        if module_dir_rel:
            file_paths = [p for p in file_paths if p.startswith(module_dir_rel)]
        file_paths = [p for p in file_paths if p not in existing_test_files]

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

        all_files: dict[str, str] = {}

        for file_path in file_paths:
            prev_files_section = ""
            if all_files:
                parts = ["\nAlready-generated files for this module (do NOT regenerate):"]
                for prev_path, prev_content in all_files.items():
                    parts.append(f'\n<FILE path="{prev_path}">\n{prev_content[:1500]}\n</FILE>')
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

STRUCT/ENUM RULES:
  - Do NOT declare `extern crate proc_macro;` — it is implicit.
  - ALL helpers must be private (no `pub`) except #[proc_macro_derive]/[proc_macro_attribute] fns.
  - Do NOT define EarlyExit, FromArgs, SubCommand, CommandInfo or any argh types here.
    Reference them inside quote! as `argh::EarlyExit`, `argh::FromArgs`, etc.
  - Entry points at TOP LEVEL of lib.rs:
      #[proc_macro_derive(FromArgs, attributes(argh))]
      pub fn derive_from_args(input: TokenStream) -> TokenStream {{ ... }}
  - References in struct fields MUST have lifetimes: `struct Foo<'a> {{ x: &'a Bar }}`
  - Iterate FieldsNamed: `fields.named.iter()` (NOT `fields.iter()`)
  - `syn::AttributeArgs` removed in syn 2.x — parse attrs via `Attribute::parse_nested_meta`
  - Do NOT use inline `mod helpers {{ }}` blocks — helper functions go directly in the file.
  - Each `mod foo;` in lib.rs needs a corresponding foo.rs file (already in file list).
"""

            prompt = f"""You are implementing a single source file for a Rust project.
{FILE_FORMAT_INSTRUCTION}
Output ONLY the file `{file_path}` using the markers above. Nothing else.
{profile_section}{workspace_section}
⚠️  MODULE SCOPE: This file belongs to the `{current_module}` crate.
    Path: `{file_path}`
{proc_macro_note}{locked_dep_section}{build_error_section}{whole_project_context}{prev_files_section}
Functional requirements:
{functional_req}

Generate the complete implementation for `{file_path}`.
"""
            response = ask(prompt, model=_TASK2_MODEL)
            files = extract_files(response)
            if file_path in files:
                all_files[file_path] = files[file_path]
            else:
                # Try relaxed match — model may have used slightly different path
                matched = next((v for k, v in files.items()
                                if Path(k).name == Path(file_path).name), None)
                if matched:
                    all_files[file_path] = matched
                    _log(f"  Matched '{file_path}' by filename.")
                else:
                    # Last resort: extract from code fence.
                    # The model often wraps content in ```lang ... ``` instead of FILE tags.
                    # Since we asked for exactly one file, any code block IS that file.
                    fence_match = re.search(
                        r'```(?:[a-zA-Z0-9_]*)\n(.*?)```', response, re.DOTALL
                    )
                    if fence_match:
                        all_files[file_path] = fence_match.group(1)
                        _log(f"  Extracted '{file_path}' from code fence (model ignored FILE markers).")
                    else:
                        preview = response.strip()[:200].replace('\n', '↵') if response else "(empty)"
                        _log(f"  WARNING: no output for `{file_path}`. Preview: {preview}")

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
          whole_project_context: str = ""):
    """
    Generate source files from functional requirements.
    Implementation is selected by TASK2_GENERATOR in .env (default: static).
    """
    return _task2_generator_factory().generate(
        project_dir, genesis_path, functional_req_path, manifest_path,
        existing_test_files, project_profile, workspace_structure, build_error,
        locked_dep_context, current_module, module_dir_rel,
        whole_project_context=whole_project_context,
    )


def task2_inner_loop(project_dir, genesis_path, functional_req_path, manifest_path,
                     existing_test_files, project_profile, workspace_structure,
                     build_check_cmd: str | None, locked_dep_context: str = "",
                     current_module: str = "", module_dir_rel: str = "",
                     whole_project_context: str = ""):
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
    for attempt_num in range(1, BUILD_CHECK_RETRIES + 1):
        if attempt_num > 1:
            _log(f"  Inner loop attempt {attempt_num}/{BUILD_CHECK_RETRIES} "
                 f"(fixing build error from previous attempt)...")

        ok = task2(project_dir, genesis_path, functional_req_path, manifest_path,
                   existing_test_files, project_profile, workspace_structure, build_error,
                   locked_dep_context, current_module, module_dir_rel,
                   whole_project_context=whole_project_context)
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
            return False

        if not build_check_cmd:
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
            return True

        _log(f"  Build still failing after fix attempts (inner loop attempt {attempt_num}).")
        # Pass the error into the next full task2 regeneration so the model
        # understands what structural issues need to be addressed.
        build_error = smart_truncate(build_output, total_limit=6000, head=3000, tail=3000)

    _log(f"  Inner loop exhausted after {BUILD_CHECK_RETRIES} attempts — escalating to outer loop.")
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
    env = _cargo_env()
    last_output = ""

    for attempt in range(max_fix_attempts + 1):  # +1: initial check counts as attempt 0
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

        if attempt == max_fix_attempts:
            break  # exhausted fix attempts

        _log(f"  [fix loop] Attempt {attempt + 1}/{max_fix_attempts} — "
             f"asking LLM to fix specific failures.")

        # Build source context — read current content from disk.
        source_block = ""
        for p in source_paths:
            rel = str(p.relative_to(project_dir))
            if rel in existing_test_files or not p.exists():
                continue
            content = p.read_text(encoding="utf-8")
            if len(content) > 2000:
                content = content[:2000] + "\n// ... (truncated)"
            source_block += f'\n<FILE path="{rel}">\n{content}\n</FILE>\n'
            if len(source_block) > 12_000:
                break  # stay within context budget

        prompt = f"""Code generation produced files that fail the check below.
Fix ONLY the files causing the failure — do NOT regenerate correct files.

Error output:
```
{smart_truncate(last_output, total_limit=2000, head=500, tail=1500)}
```

Current source files:
{source_block}
{extra_context}
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

    _log(f"  [fix loop] All {max_fix_attempts} fix attempts exhausted.")
    return False, last_output


def task2_fix_loop(
    project_dir: Path,
    manifest_path: Path,
    existing_test_files: set,
    test_output: str,
    test_command: str,
    functional_req_path: Path,
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
    functional_req = smart_truncate(read(functional_req_path),
                                    total_limit=2000, head=500, tail=1500)
    extra = f"\nFunctional requirements (for reference):\n{functional_req}\n"

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

    try:
        result = subprocess.run(
            _parse_cmd(test_command),
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT,
            env=env,
        )
        test_output = result.stdout
        if result.stderr:
            test_output += "\n--- stderr ---\n" + result.stderr

        genesis = read(genesis_path)
        file_content = (
            f"=== genesis.md (iteration {iteration}) ===\n"
            f"{genesis}\n"
            f"{OUTPUT_SEPARATOR}\n"
            f"{test_output}"
        )
        write(output_file, file_content)
        passed = result.returncode == 0
        _log(f"  Tests {'PASSED' if passed else 'FAILED'}. Output -> {output_file}")
        return passed, test_output

    except subprocess.TimeoutExpired:
        msg = f"TIMEOUT: tests exceeded {TEST_TIMEOUT} seconds."
        genesis = read(genesis_path)
        write(output_file,
              f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{msg}")
        _log(f"  {msg}")
        return False, msg

    except FileNotFoundError as e:
        msg = (f"ERROR: test command not found: {e}\n"
               f"Command was: {test_command}\n"
               "Ensure the required runtime is installed and on PATH.")
        genesis = read(genesis_path)
        write(output_file,
              f"=== genesis.md (iteration {iteration}) ===\n{genesis}\n{OUTPUT_SEPARATOR}\n{msg}")
        _log(f"  {msg}")
        sys.exit(1)


def task4(genesis_path, functional_req_path, test_output, iteration, output_dir,
          failure_type: str = "test", failure_log: str = "",
          prev_pass_count: int = 0, prev_test_output: str = ""):
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
    _log(f"\nTASK 4: Analyzing {'build' if failure_type == 'build' else 'test'} "
         f"failure, updating genesis.md")

    genesis = smart_truncate(read(genesis_path), total_limit=TASK4_DOC_LIMIT,
                             head=TASK4_DOC_LIMIT // 4, tail=3 * TASK4_DOC_LIMIT // 4)
    functional_req = smart_truncate(read(functional_req_path), total_limit=TASK4_DOC_LIMIT,
                                    head=TASK4_DOC_LIMIT // 4, tail=3 * TASK4_DOC_LIMIT // 4)
    truncated_output = smart_truncate(test_output)

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
        prev_truncated = smart_truncate(_prev_output, total_limit=4000, head=500, tail=3500)
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

    failure_log_section = (
        f"\nHistory of previous attempts (most recent last — avoid repeating failed approaches):\n"
        f"{failure_log}\n"
    ) if failure_log.strip() else ""

    # Different prompt depending on whether this is a build or test failure.
    delim_instruction = (
        f"IMPORTANT: Output the updated genesis.md ONLY between these exact delimiters — "
        f"no other format accepted:\n{GENESIS_DELIM_START}\n...content...\n{GENESIS_DELIM_END}\n"
    )

    if failure_type == "build":
        prompt = f"""You are refining a methodology for translating test suites into functional requirements.
{delim_instruction}
The implementation failed to COMPILE this iteration — the tests could not even run.
This is a STRUCTURAL failure: the generated code was missing required files, had wrong
module declarations, or had incorrect workspace layout. This is NOT a logic error.

Your task: update genesis.md so that its methodology explicitly captures the structural
information that leads to correct compilation. The methodology must teach readers how to
derive workspace layout, required entry-point files, and module structure from tests and
README alone — as general rules, not project-specific fixes.

genesis.md (current methodology):
{genesis}

functional_requirements.md (requirements that led to broken code):
{functional_req}

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
        prompt = f"""You are refining a general methodology for translating test suites into functional requirements.
{delim_instruction}
genesis.md is not a patch document — it is a project-independent, language-agnostic reasoning
process that, when mature, should work on any test suite without needing iteration. Every update
must move it toward greater generality and precision, not toward narrow fixes for the specific
failures just observed.

genesis.md (the reasoning process followed this iteration):
{genesis}

functional_requirements.md (produced by that process):
{functional_req}

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
    return ok, current_pass


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

    truncated = smart_truncate(genesis, total_limit=TASK4_DOC_LIMIT,
                               head=TASK4_DOC_LIMIT // 4, tail=3 * TASK4_DOC_LIMIT // 4)

    prompt = f"""Review this methodology document for internal contradictions, redundant rules,
or rules that directly conflict with each other.

genesis.md:
{truncated}

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
    return result == 'updated'


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
    return env


def _crate_name_from_toml(module_dir: Path, fallback: str) -> str:
    """Extract `name` from Cargo.toml and normalise hyphens to underscores."""
    toml = module_dir / "Cargo.toml"
    if toml.exists():
        m = re.search(r'^name\s*=\s*"([^"]+)"', toml.read_text(), re.MULTILINE)
        if m:
            return m.group(1).replace("-", "_")
    return fallback.replace("-", "_")


def _run_tarpaulin(project_dir: Path, module_name: str) -> tuple[float, list[dict]]:
    """
    Run cargo tarpaulin for one module.
    Returns (coverage_pct 0-100, files_list from JSON report).
    Returns (0.0, []) on any failure.
    """
    out_dir = Path("/tmp") / f"tarp_{module_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(TARPAULIN_BIN), "tarpaulin",
        "-p", module_name,
        "--lib",           # only instrument library target — avoids full workspace build
        "--no-fail-fast",  # produce report even if some tests fail at runtime
        "--out", "Json",
        "--output-dir", str(out_dir),
        "--skip-clean",
    ]
    try:
        result = subprocess.run(
            cmd, cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=_cargo_env(),
        )
        json_path = out_dir / "tarpaulin-report.json"
        if not json_path.exists():
            _log(f"  tarpaulin: no JSON report produced. stderr: {result.stderr[:300]}")
            return 0.0, []
        data = json.loads(json_path.read_text())
        return data.get("coverage", 0.0), data.get("files", [])
    except subprocess.TimeoutExpired:
        _log(f"  tarpaulin: timeout after {BUILD_TIMEOUT}s")
        return 0.0, []
    except Exception as e:
        _log(f"  tarpaulin: error: {e}")
        return 0.0, []


def _annotate_uncovered(content: str, uncovered: set[int]) -> str:
    """Return source annotated with '!!' markers on uncovered lines."""
    lines = content.splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        marker = "  // !! UNCOVERED" if i in uncovered else ""
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
        marker = "  // !! UNCOVERED" if i in uncovered else ""
        result.append(f"{i:4d} | {lines[i - 1]}{marker}")
        prev = i
    return "\n".join(result)


def _extract_existing_tests(source_text: str) -> str:
    """Extract the existing #[cfg(test)] mod tests block from a source file.

    Showing the existing tests as an example prevents the model from inventing
    types that don't exist: it can see exactly how to construct e.g. CommandInfo
    by looking at test code that already compiles correctly.

    Only extracts `mod tests` (the original hand-written tests), NOT `mod gen_tests`
    (our generated block) — gen_tests may contain broken code that would teach the
    model to hallucinate wrong types if used as an example.
    """
    # Find `mod tests {` specifically, not `mod gen_tests`
    for marker in ("#[cfg(test)]\nmod tests", "#[cfg(test)]\npub mod tests"):
        if marker in source_text:
            idx = source_text.index(marker)
            snippet = source_text[idx:idx + 2000]  # cap at 2000 chars to save context
            return snippet.strip()
    return ""


def _extract_public_symbols(content: str) -> str:
    """Extract public type/function/const names from source to ground the LLM prompt."""
    names: list[str] = []
    for pat in [
        r'pub\s+(?:struct|enum|trait|type)\s+(\w+)',
        r'pub\s+(?:fn|const|static)\s+(\w+)',
    ]:
        names.extend(re.findall(pat, content))
    return ", ".join(sorted(set(names)))


def task0c_augment_module(
    project_dir: Path,
    module_name: str,
    module_dir: Path,
    crate_type: str = "",
    existing_test_files: set | None = None,
) -> float:
    """
    Generate unit tests (#[cfg(test)] blocks appended inline) for a module
    until tarpaulin reports 100% coverage or MAX_RETRIES is exhausted.
    Returns final coverage percentage.
    """
    if existing_test_files is None:
        existing_test_files = set()

    # proc-macro crates cannot have integration tests and unit tests require
    # a separate test harness — skip rather than generate invalid code.
    if "proc-macro" in crate_type.lower():
        _log(f"  [{module_name}] proc-macro — unit tests not applicable, skipping.")
        return 100.0  # treat as done

    env = _cargo_env()

    # Pre-flight: two-stage compile check.
    #
    # Stage 1: compile the library only. If this fails, the implementation itself
    # is broken — skip the module entirely (not our problem to fix in TASK 0c).
    #
    # Stage 2: compile the full test harness. If THIS fails but stage 1 passed,
    # the problem is in existing test code (inline #[cfg(test)] blocks or test files).
    # The LLM is asked to fix the broken test code — it reads the compile error and
    # the affected file, then outputs corrected content. This is the LLM's job;
    # the autopilot must never touch project files directly.
    lib_check = subprocess.run(
        shlex.split(f"cargo build -p {module_name} --lib"),
        cwd=project_dir, capture_output=True, text=True,
        timeout=BUILD_TIMEOUT, env=env,
    )
    if lib_check.returncode != 0:
        err_snippet = (lib_check.stdout + lib_check.stderr)[:400]
        _log(f"  [{module_name}] Library does not compile — skipping augmentation.")
        _log(f"    Error: {err_snippet}")
        return 0.0

    for fix_attempt in range(1, MAX_RETRIES + 1):
        test_check = subprocess.run(
            shlex.split(f"cargo test -p {module_name} --no-run"),
            cwd=project_dir, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT, env=env,
        )
        if test_check.returncode == 0:
            break  # test harness compiles — proceed to augmentation

        # Lib compiles but tests don't. Ask the LLM to fix the broken test code.
        # Extract the file paths mentioned in the compiler error so we know what to fix.
        compile_err = test_check.stdout + test_check.stderr
        _log(f"  [{module_name}] Test harness broken (fix attempt {fix_attempt}/{MAX_RETRIES}) "
             f"— asking LLM to repair test code.")

        # Find source files mentioned in the error that belong to this module.
        broken_files: list[Path] = []
        for src_file in sorted(module_dir.rglob("*.rs")):
            rel = str(src_file.relative_to(project_dir))
            if rel in compile_err or src_file.name in compile_err:
                broken_files.append(src_file)
        if not broken_files:
            # Fallback: try every .rs file in the module that has a #[cfg(test)] block.
            for src_file in sorted(module_dir.rglob("*.rs")):
                try:
                    if "#[cfg(test)]" in src_file.read_text(encoding="utf-8"):
                        broken_files.append(src_file)
                except Exception:
                    pass

        if not broken_files:
            _log(f"  [{module_name}] Cannot identify broken file — skipping augmentation.")
            return 0.0

        for src_file in broken_files:
            try:
                current_content = src_file.read_text(encoding="utf-8")
            except Exception:
                continue
            rel = str(src_file.relative_to(project_dir))

            prompt = f"""A Rust source file has a test block that fails to compile.

File: {rel}

Compile error:
```
{compile_err[:800]}
```

Current file content:
{current_content[:6000]}

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
    else:
        _log(f"  [{module_name}] Test harness still broken after {MAX_RETRIES} LLM fix attempts "
             f"— skipping augmentation.")
        return 0.0

    # Pre-clean: remove any gen_* blocks left over from a previous interrupted run.
    # Without this, tarpaulin's first measurement sees a compilation failure and reports
    # 0% even when the module has real tests — masking the true baseline coverage.
    # Uses regex to catch any `mod gen_*` variant (e.g. gen_tests, gen_nushell_tests)
    # since earlier runs wrote modules with module-specific names.
    gen_marker = "#[cfg(test)]\nmod gen_tests"  # used for strip in the attempt loop below
    # Match any #[cfg(test)] block regardless of module name — the LLM sometimes
    # writes `mod tests` instead of `mod gen_tests`, which the narrower pattern misses.
    # Regex to detect any pipeline-generated #[cfg(test)] block (any module name).
    # Used for pre-clean on first entry only — during the attempt loop we no longer
    # strip automatically; instead we fix compile errors iteratively.
    _gen_block_re = re.compile(r'\n+#\[cfg\(test\)\]\n(?:pub )?mod \w+\s*\{.*', re.DOTALL)

    # Pre-clean only on first entry: remove blocks left by a previous interrupted run.
    # We do NOT strip on every attempt anymore — the new loop fixes rather than discards.
    for src_file in sorted(module_dir.rglob("*.rs")):
        if is_test_file(src_file, project_dir):
            continue
        try:
            text = src_file.read_text(encoding="utf-8")
            cleaned = _gen_block_re.sub("", text).rstrip() + "\n"
            if cleaned != text:
                src_file.write_text(cleaned, encoding="utf-8")
                _log(f"  [{module_name}] Pre-cleaned leftover gen block from "
                     f"{src_file.relative_to(project_dir)}")
        except Exception:
            pass

    for attempt in range(1, MAX_RETRIES + 1):
        _log(f"  [{module_name}] Attempt {attempt}/{MAX_RETRIES}")

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
        coverage, files_data = _run_tarpaulin(project_dir, module_name)
        _log(f"  [{module_name}] Coverage: {coverage:.1f}%")

        if coverage >= 100.0:
            _log(f"  [{module_name}] 100% coverage reached.")
            return 100.0

        # --- Phase C: generate/augment tests for uncovered lines ---
        # Filter to source files in this module with uncovered lines.
        # Skip test files — adding tests inside test files causes circular issues.
        mod_prefix = str(module_dir) + "/"
        uncovered_by_file: dict[str, tuple[str, set[int]]] = {}
        for fdata in files_data:
            abs_path = str(Path(*fdata["path"]))
            if not abs_path.startswith(mod_prefix):
                continue
            rel = str(Path(abs_path).relative_to(project_dir))
            lines_uncov = {
                t["line"] for t in fdata.get("traces", [])
                if t["stats"].get("Line", 1) == 0
            }
            if lines_uncov and not is_test_file(Path(abs_path), project_dir):
                uncovered_by_file[rel] = (fdata.get("content", ""), lines_uncov)

        if not uncovered_by_file:
            _log(f"  [{module_name}] No uncovered lines in module source files.")
            break

        for rel_path, (content, uncov_lines) in uncovered_by_file.items():
            abs_path = project_dir / rel_path
            existing_text = abs_path.read_text(encoding="utf-8") if abs_path.exists() else content

            # Use focused annotation — only show uncovered lines + context window.
            # Showing the full file wastes context and leaves less room for test output.
            annotated = _focused_annotation(content, uncov_lines)

            pub_symbols = _extract_public_symbols(existing_text)
            symbols_line = (
                f"Public symbols available: {pub_symbols}\n"
                f"ONLY use these — do NOT invent types or methods not listed above.\n"
            ) if pub_symbols else ""

            existing_tests = _extract_existing_tests(existing_text)
            examples_section = (
                f"\nExisting tests (reference for correct type construction):\n"
                f"```rust\n{existing_tests}\n```\n"
            ) if existing_tests else ""

            # Check if a gen_tests block already exists — if so, extend it rather
            # than append a new one (avoids duplicate module name errors).
            has_gen = "#[cfg(test)]\nmod gen_tests" in existing_text
            if has_gen:
                instruction = ("A `#[cfg(test)] mod gen_tests` block already exists. "
                               "Output the COMPLETE updated file with MORE tests added to that block.")
                output_format = f'<FILE path="{rel_path}">\ncomplete file here\n</FILE>'
            else:
                instruction = "Append a new `#[cfg(test)] mod gen_tests` block at the end."
                output_format = (
                    '<FILE path="gen_tests_block">\n'
                    '#[cfg(test)]\nmod gen_tests {{\n    use super::*;\n\n'
                    '    #[test]\n    fn test_example() {{\n        // tests\n    }}\n}}\n'
                    '</FILE>'
                )

            prompt = f"""You are writing Rust unit tests to increase code coverage.

Source file {rel_path} — uncovered lines marked !! UNCOVERED:

{annotated}
{symbols_line}{examples_section}
{instruction}

Rules:
- Write `#[test]` functions that CALL the uncovered functions — do NOT copy them.
- Each `#[test]` fn must have ZERO arguments.
- Use `use super::*;` to access private items.
- Prefer `assert!(x.is_empty())` over `assert_eq!(x, [])` for slices.
- Do NOT add new Cargo.toml dependencies.
- Do NOT write markdown (no triple backticks) inside Rust code.
- Struct fields typed `&str` must be initialized with string literals — NOT `.to_string()` or `String::from()`.
  WRONG: `name: "foo".to_string()` — RIGHT: `name: "foo"`
- Struct fields typed `char` must be initialized with char literals — NOT `&char`.
  WRONG: `short: &'a'` — RIGHT: `short: 'a'`
- Read struct field types from the source above before constructing any struct.
  Always match the EXACT type shown in the struct definition.

{output_format}
"""
            response = ask(prompt, model=_TASK0C_MODEL)
            files_out = extract_files(response)

            if has_gen and rel_path in files_out:
                # Full file replacement — model extended existing gen_tests block.
                write(project_dir / rel_path, files_out[rel_path])
                _log(f"  [{module_name}] Extended gen_tests in {rel_path} "
                     f"({len(uncov_lines)} uncovered lines targeted)")
            elif not has_gen:
                block = files_out.get("gen_tests_block", "")
                if not block:
                    fence_match = re.search(r'```(?:rust)?\n(#\[cfg\(test\)\].*?)```',
                                            response, re.DOTALL)
                    if fence_match:
                        block = fence_match.group(1).strip()
                # Fallback: find a proper #[cfg(test)] mod block (not prose with backtick).
                if not block:
                    mod_match = re.search(
                        r'(#\[cfg\(test\)\]\s*\n(?:pub\s+)?mod\s+\w+\s*\{.*)',
                        response, re.DOTALL
                    )
                    if mod_match:
                        raw = mod_match.group(1)
                        # Strip any trailing markdown fence lines.
                        block = re.sub(r'\n```\s*$', '', raw.rstrip()).strip()
                # Discard block if it contains obvious markdown prose (not just backticks).
                if block and re.search(r'block to our Rust file|Here\'s how|You can add|here is', block, re.IGNORECASE):
                    block = ""
                if block:
                    new_content = existing_text.rstrip() + "\n\n" + block + "\n"
                    abs_path.write_text(new_content, encoding="utf-8")
                    _log(f"  [{module_name}] Appended gen_tests to {rel_path} "
                         f"({len(uncov_lines)} uncovered lines targeted)")
                else:
                    _log(f"  [{module_name}] No test block extracted for {rel_path}.")
            else:
                _log(f"  [{module_name}] Model returned nothing for {rel_path}.")

        # compile check happens at the top of the next attempt — no separate run needed

    # Final measurement
    final_cov, _ = _run_tarpaulin(project_dir, module_name)
    _log(f"  [{module_name}] Final coverage: {final_cov:.1f}%")
    return final_cov


def task0c_coverage_augmentation(
    project_dir: Path,
    dep_graph: DependencyGraph,
    modules: list[str],
    test_command: str,
) -> None:
    """
    TASK 0c: Drive each module to 100% unit-test coverage using cargo tarpaulin.
    Requires full implementation to be present — run before strip_to_tests.py.
    Writes COVERAGE_AUGMENT_SENTINEL when complete.
    """
    sentinel = project_dir / COVERAGE_AUGMENT_SENTINEL
    if sentinel.exists():
        _log("\nTASK 0c: Already done (.coverage_augmented exists) — skipping.")
        return

    if not TARPAULIN_BIN.exists():
        _log(f"\nTASK 0c: ERROR — cargo-tarpaulin not found at {TARPAULIN_BIN}.")
        _log("  Install with: cargo install cargo-tarpaulin --features vendored-openssl")
        return

    _log("\nTASK 0c: Coverage augmentation — driving all modules to 100% unit-test coverage")

    is_multi = len(modules) > 1
    if not is_multi:
        mod_name = modules[0] if modules else project_dir.name
        work_list = [(mod_name, project_dir, "lib")]
    else:
        work_list = [
            (mod_name, dep_graph.crate_dir(mod_name), dep_graph.crate_type(mod_name))
            for mod_name in modules
        ]

    # Measure baseline coverage for each module and sort descending.
    # Modules with more existing tests give the LLM more signal to pattern-match
    # against when generating new tests. 0%-coverage modules go last — they require
    # pure invention and benefit from the LLM having "warmed up" on easier cases.
    # Topo order is irrelevant here: the full implementation is present, so all
    # modules compile regardless of which order we process them.
    if len(work_list) > 1:
        _log("\nTASK 0c: Measuring baseline coverage to determine augmentation order...")
        baseline: list[tuple[float, str, Path, str]] = []
        for mod_name, mod_dir, crate_type in work_list:
            if "proc-macro" in crate_type.lower():
                cov = 100.0  # will be skipped anyway
            else:
                cov, _ = _run_tarpaulin(project_dir, mod_name)
            _log(f"  {mod_name}: {cov:.1f}%")
            baseline.append((cov, mod_name, mod_dir, crate_type))
        baseline.sort(key=lambda x: x[0], reverse=True)
        work_list = [(mod_name, mod_dir, crate_type) for _, mod_name, mod_dir, crate_type in baseline]
        _log(f"  Augmentation order: {[m for m, _, _ in work_list]}")

    existing_test_files = collect_protected_test_files(project_dir)
    results: list[tuple[str, float]] = []

    for mod_name, mod_dir, crate_type in work_list:
        _log(f"\n  {'='*50}")
        _log(f"  Module: {mod_name}  [{crate_type}]")
        final_cov = task0c_augment_module(project_dir, mod_name, mod_dir, crate_type,
                                          existing_test_files=existing_test_files)
        results.append((mod_name, final_cov))

    _log(f"\nTASK 0c complete. Coverage summary:")
    for mod_name, cov in results:
        status = "OK" if cov >= 100.0 else f"PARTIAL ({cov:.1f}%)"
        _log(f"  {mod_name}: {status}")

    overall = sum(c for _, c in results) / len(results) if results else 0.0
    lines = [f"{name}: {cov:.1f}%" for name, cov in results]
    lines.append(f"overall: {overall:.1f}%")
    sentinel.write_text("\n".join(lines))
    _log(f"  Sentinel written: {COVERAGE_AUGMENT_SENTINEL} (overall {overall:.1f}%)")


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

def clean_state(root: Path, project_dir: Path, manifest_path: Path,
                genesis_path: Path, functional_req_path: Path):
    """Delete all pipeline-generated state so the next run starts fresh."""
    # Generated source files tracked in the manifest
    if manifest_path.exists():
        delete_source_files(project_dir, manifest_path)

    # Per-iteration documents and the project profile
    profile_path = root / PROJECT_PROFILE_NAME
    for p in [functional_req_path, genesis_path, profile_path]:
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

    # Locked module backups (bottom-up integration)
    locked_dir = project_dir / LOCKED_MODULES_DIR
    if locked_dir.exists():
        shutil.rmtree(locked_dir)
        _log(f"  Deleted {LOCKED_MODULES_DIR}/")

    # Per-module generated-file manifests
    for p in project_dir.glob(".generated_files_*.txt"):
        p.unlink()
        _log(f"  Deleted {p.name}")

    _log("Clean complete. Starting from scratch.")


# ---------------------------------------------------------------------------
# Per-module pipeline helpers
# ---------------------------------------------------------------------------

MODULE_AUGMENTED_DIR = ".module_augmented"


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
MODULE_STRIPPED_DIR = ".module_stripped"


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


def load_original_structure(project_dir: Path, module_name: str) -> list[str]:
    """Load the saved pre-strip file list for a module. Returns [] if not found."""
    # Try both module_name and crate dir relative path as key.
    out_dir = project_dir / ORIGINAL_STRUCTURE_DIR
    for candidate in out_dir.glob("*.txt"):
        stem = candidate.stem.replace("/", "_").replace("-", "_")
        if module_name in candidate.read_text().split("\n")[0] if candidate.exists() else False:
            pass
        content = candidate.read_text()
        paths = [l.strip() for l in content.splitlines() if l.strip()]
        if paths and paths[0].startswith(module_name.replace("-", "_")):
            return paths
    # Fallback: find by directory prefix
    for candidate in out_dir.glob("*.txt"):
        content = candidate.read_text()
        paths = [l.strip() for l in content.splitlines() if l.strip()]
        if paths and module_name.replace("-", "_") in paths[0].split("/")[0]:
            return paths
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
            cov, _ = _run_tarpaulin(project_dir, mod_name)
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
    ollama_rows.sort(key=lambda r: (order[r["fit"]], _OLLAMA_MODELS.index((r["id"].split(":")[0] + ":" + r["id"].split(":")[1] if ":" in r["id"] else r["id"], int(r["params"]), r["notes"])) if (r["id"].split(":")[0] + ":" + r["id"].split(":")[1] if ":" in r["id"] else r["id"], int(r["params"]), r["notes"]) in [(n, p, d) for n, p, d in _OLLAMA_MODELS] else 99))

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
        base = r["id"].split(":")[0]
        if base in installed:
            r["notes"] = "✔ installed  " + r["notes"]

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
        params = _extract_params_b(m)
        if params is None or params < 1:
            continue
        vram_4bit = round(params * 0.55 + 2.5, 1)
        hf_rows.append({
            "id": m.id,
            "params": params,
            "vram_4bit": vram_4bit,
            "fit": _fit_label(vram_4bit, vram_gb),
            "downloads": m.downloads or 0,
            "likes": m.likes or 0,
        })

    hf_rows.sort(key=lambda r: (order[r["fit"]], -r["likes"]))

    seen = {}
    deduped = []
    for r in hf_rows:
        base = re.sub(r'[-_](AWQ|GPTQ|GGUF|bnb|4bit|8bit|fp16|bf16|Int4|Int8|MLX|2507).*$', '', r["id"], flags=re.IGNORECASE)
        if base not in seen:
            seen[base] = True
            deduped.append(r)

    _print_model_table(deduped[:30], f"── HuggingFace  (HF_MODEL=<id>)  |  VRAM budget: {vram_gb:.0f} GB")
    print("  Benchmark scores: https://huggingface.co/spaces/bigcode/bigcodebench-leaderboard")

    print(_legend)


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
    parser.add_argument("--best-models-list", action="store_true",
                        help="Fetch and display top open-source coding models from HuggingFace")
    parser.add_argument("--vram-gb", type=float, default=32.0,
                        help="Total GPU VRAM in GB used to annotate model fit (default: 32)")
    args = parser.parse_args()

    if args.best_models_list:
        cmd_best_models(vram_gb=args.vram_gb)
        sys.exit(0)

    root = Path(__file__).parent
    project_dir = root / args.project
    genesis_path = project_dir / "genesis.md"
    functional_req_path = project_dir / "functional_requirements.md"
    manifest_path = project_dir / MANIFEST_NAME  # legacy whole-workspace manifest

    if not project_dir.exists():
        print(f"ERROR: project directory '{project_dir}' not found.")
        sys.exit(1)

    init_log(root, nolog=args.nolog, progress_mode=args.progress)

    provider = "Claude (Anthropic API)" if MODEL.startswith("claude") else f"Ollama ({MODEL})"
    _log(f"Using model: {MODEL}  |  Provider: {provider}")

    if args.clean:
        _log("\n-- --clean requested: wiping all generated state --")
        clean_state(root, project_dir, manifest_path, genesis_path, functional_req_path)

    readme_path = project_dir / "README.md"
    if readme_path.exists():
        readme_text = read(readme_path)
    else:
        _log("WARNING: README.md not found -- proceeding without it.")
        readme_text = ""

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

    # TASK 0a: identify project structure once; result is stable across all iterations.
    profile_path = root / PROJECT_PROFILE_NAME
    project_profile = task0_project_profile(profile_path, test_files, readme_text)

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

    total_iters = 0
    _task0c_retries: dict[str, int] = {}  # track TASK 0c low-coverage retries per module
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

        # --- TASK 0c: augment this module until 100% coverage ---
        # Real source of all other modules is still on disk — no stubs needed.
        if is_module_augmented(project_dir, module_name):
            # Re-check stored coverage — if below 50%, force re-augmentation.
            _sentinel = _module_augmented_path(project_dir, module_name)
            try:
                _stored_cov = float(_sentinel.read_text().strip().rstrip('%'))
            except (ValueError, OSError):
                _stored_cov = 100.0
            if _stored_cov < 20.0 and crate_type != "proc-macro" and TARPAULIN_BIN.exists():
                _log(f"  TASK 0c: '{module_name}' stored coverage {_stored_cov:.1f}% < 50% — re-augmenting.")
                _sentinel.unlink(missing_ok=True)
            else:
                _log(f"  TASK 0c: '{module_name}' already augmented — skipping.")
        elif not TARPAULIN_BIN.exists():
            _log(f"  TASK 0c: tarpaulin not found — skipping augmentation for '{module_name}'.")
            mark_module_augmented(project_dir, module_name, 0.0)
        else:
            _log(f"\nTASK 0c: Augmenting '{module_name}' to 100% coverage...")
            all_test_files_pre = collect_test_files(project_dir)
            etf_pre = {rel for rel, _ in all_test_files_pre}
            cov = task0c_augment_module(
                project_dir, module_name, module_dir, crate_type,
                existing_test_files=etf_pre,
            )
            mark_module_augmented(project_dir, module_name, cov)
            _log(f"  TASK 0c: '{module_name}' final coverage: {cov:.1f}%")
            if cov < 20.0 and crate_type != "proc-macro":
                _log(
                    f"  WARNING: '{module_name}' coverage {cov:.1f}% is below 20% minimum. "
                    f"Requirements extraction will be unreliable. "
                    f"Consider running with --augment-tests after fixing TASK 0c prompts."
                )
                _task0c_retries[module_name] = _task0c_retries.get(module_name, 0) + 1
                attempt_n = _task0c_retries[module_name]
                if attempt_n < 10:
                    # Clear the augmented sentinel so the next pass retries TASK 0c.
                    _module_augmented_path(project_dir, module_name).unlink(missing_ok=True)
                    _log(f"  Cleared augmented sentinel — retry {attempt_n}/10 next pass.")
                    continue  # Skip this module this pass; try again next outer iteration.
                else:
                    _log(
                        f"  HARD BLOCK: '{module_name}' coverage {cov:.1f}% never reached 20% "
                        f"after 10 TASK 0c attempts. Cannot proceed — requirements would be "
                        f"too unreliable. Fix TASK 0c prompt or provide manual test coverage."
                    )
                    sys.exit(1)  # Hard stop.

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
        # Only strip once — if already stripped, the original structure file list
        # is intact and the implementation is already gone.
        strip_sentinel = project_dir / MODULE_STRIPPED_DIR / module_name
        if strip_sentinel.exists():
            _log(f"  '{module_name}' already stripped — skipping.")
        else:
            strip_module_source(project_dir, module_dir)
            strip_sentinel.parent.mkdir(parents=True, exist_ok=True)
            strip_sentinel.touch()

        # Re-collect after strip — picks up any tests generated by TASK 0c.
        # test_files: full read set for TASK 1 (includes inline test blocks in src/)
        # existing_test_files: write-protection set for TASK 2 (pure test files only)
        test_files = collect_test_files(project_dir)
        existing_test_files = collect_protected_test_files(project_dir)
        _log(f"  Test files after strip: {len(test_files)} readable, "
             f"{len(existing_test_files)} write-protected.")

        # --- TASK 1: extract requirements from this module's tests ---
        module_req_path = root / f"functional_requirements_{module_name}.md"

        if module_req_path.exists():
            _log(f"\nTASK 1: requirements for '{module_name}' already extracted — skipping.")
        else:
            # Zero-test modules: use all project tests as spec (only way to infer interface).
            t1_input = module_test_files if module_has_tests else test_files
            if not module_has_tests:
                _log(f"  '{module_name}' has 0 tests — using full project tests for TASK 1.")
            if not task1(project_dir, genesis_path, module_req_path, t1_input,
                         readme_text, project_profile, crate_type=crate_type):
                _log(f"  TASK 1 failed for '{module_name}' — skipping module.")
                continue

        task1_static_audit(module_req_path, module_test_files or test_files)

        # Workspace scan (done per-module since strip changes the file layout).
        workspace_structure = scan_workspace_structure(project_dir)
        build_check_cmd = _infer_build_check_command(test_command, project_dir)
        module_build_check = (
            dep_graph.module_build_check_command(build_check_cmd, module_name)
            if is_multi_module else build_check_cmd
        )

        # Save any gen_tests blocks from stripped files BEFORE wiping.
        # TASK 0c appends these blocks inline; TASK 2 regenerates the file without them.
        # We reattach them after each TASK 2 write so TASK 3 can actually run the tests.
        _gen_blocks_re = re.compile(
            r'\n+#\[cfg\(test\)\]\n(?:pub\s+)?mod\s+gen_\w+\s*\{.*',
            re.DOTALL
        )
        saved_gen_blocks: dict[str, str] = {}  # rel_path -> gen_tests block text
        if module_dir.exists():
            for src_file in sorted(module_dir.rglob("*.rs")):
                rel = str(src_file.relative_to(project_dir))
                if rel in existing_test_files:
                    continue
                try:
                    content = src_file.read_text(encoding="utf-8")
                    m = _gen_blocks_re.search(content)
                    if m:
                        saved_gen_blocks[rel] = m.group(0)
                except Exception:
                    pass
        if saved_gen_blocks:
            _log(f"  Saved gen_tests blocks from {len(saved_gen_blocks)} file(s) for reattachment.")

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
        module_iter = 1
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
            ):
                _log("TASK 2 inner loop exhausted — escalating to TASK 4.")
                test_output = "BUILD FAILED: inner loop exhausted all retries."
                failure_type = "build"
            else:
                # Reattach saved gen_tests blocks to generated source files.
                if saved_gen_blocks:
                    for rel, block in saved_gen_blocks.items():
                        src_path = project_dir / rel
                        if src_path.exists():
                            content = src_path.read_text(encoding="utf-8")
                            # Only append if the block isn't already present.
                            if "mod gen_" not in content:
                                src_path.write_text(content.rstrip() + "\n" + block + "\n")
                                _log(f"  Reattached gen_tests block to {rel}.")

                # TASK 3: run tests.
                passed, test_output = task3(
                    project_dir, module_test_cmd, label, root, genesis_path
                )
                pass_count, fail_count = _parse_test_counts(test_output)

                def _try_lock(reason: str) -> bool:
                    """Lock only when at least 1 test ran and 0 failed."""
                    if pass_count > 0 and fail_count == 0:
                        if is_multi_module:
                            lock_module(project_dir, module_name, module_dir, module_manifest)
                            locked_modules.add(module_name)
                        else:
                            _log(f"\nSUCCESS: all tests passed {reason}.")
                            sys.exit(0)
                        return True
                    elif pass_count == 0 and fail_count == 0:
                        # Proc-macro crates have no own tests — their tests live in
                        # dependent crates. If the build passes, lock on build success.
                        if crate_type == "proc-macro" and module_has_tests is False:
                            _log(f"  proc-macro crate with 0 tests — locking on build success.")
                            if is_multi_module:
                                lock_module(project_dir, module_name, module_dir, module_manifest)
                                locked_modules.add(module_name)
                                return True
                        _log(f"  WARNING: 0 tests ran — not locking '{module_name}'. "
                             f"Treating as failure to force real validation.")
                    return False

                if passed:
                    if _try_lock(f"on iteration {module_iter}"):
                        break
                    # 0 tests ran — treat as failure, don't lock.
                    failure_type = "no_tests"
                    test_output += "\nNO TESTS RAN: implementation may be empty or test harness broken."
                else:
                    # Try targeted fix loop before full regeneration.
                    fix_passed, test_output = task2_fix_loop(
                        project_dir, module_manifest, existing_test_files,
                        test_output, module_test_cmd, module_req_path,
                    )
                    if fix_passed:
                        pass_count, fail_count = _parse_test_counts(test_output)
                        if _try_lock("after fix loop"):
                            break

                    failure_type = classify_failure(test_output)

            # TASK 4: analyze failure, update genesis.md.
            _log(f"  Failure classification: {failure_type}")
            failure_log_content = load_failure_log(failure_log_path)
            t4_ok, current_pass = task4(
                genesis_path, module_req_path, test_output, module_iter, root,
                failure_type=failure_type, failure_log=failure_log_content,
                prev_pass_count=prev_pass_count, prev_test_output=prev_test_output,
            )
            t4_summary = "genesis.md updated." if t4_ok else "genesis.md unchanged."
            append_failure_log(
                failure_log_path, label, failure_type,
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

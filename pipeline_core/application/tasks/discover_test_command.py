"""TASK 0 — discover the project's test command (LLM use-case).

Extracted from pipeline.py. Collects project file
signals (README + Cargo.toml/Makefile/package.json/.cargo config), asks the
configured model for the one command that runs the suite, then parses and
normalizes the answer through :mod:`pipeline_core.domain.test_command` (pure).
No import of pipeline.py; collaborators are injected via :class:`DiscoverTestCommandDeps`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from ...domain.test_command import (
    EXTRA_SIGNAL_FILES,
    INFERENCE_PROMPT,
    build_discovery_prompt,
    extract_command_from_response,
    looks_like_command,
    normalize_bare_tool,
)
from ...log import log
from ...retry import with_retry


@dataclass(frozen=True, slots=True)
class DiscoverTestCommandDeps:
    """Collaborators discovery needs, injected by the composition root."""

    ask: Callable[..., str]
    task0_model: str
    model_name: str
    extra_cap: int
    sys_exit: Callable[[int], NoReturn]


def _collect_extra_sections(project_dir: Path | None, extra_cap: int) -> list[str]:
    """Read the non-README signal files that exist, capped, as labelled blocks."""
    sections: list[str] = []
    if not project_dir:
        return sections
    for rel_parts in EXTRA_SIGNAL_FILES:
        extra_path = project_dir.joinpath(*rel_parts)
        label = "/".join(rel_parts)
        if extra_path.exists():
            try:
                content = extra_path.read_text(encoding="utf-8")[:extra_cap]
                sections.append(f"{label}:\n```\n{content}\n```")
            except Exception:
                pass
    return sections


def discover_test_command(readme_text: str, project_dir: Path | None,
                          deps: DiscoverTestCommandDeps) -> str:
    """Ask the model for the primary test command, validate and normalize it.
    Exits the process (via injected `sys_exit`) if no command can be extracted."""
    extra_sections = _collect_extra_sections(project_dir, deps.extra_cap)

    if readme_text.strip() or extra_sections:
        log(f"Initializing: asking {deps.model_name} to discover test command from project files...")
        prompt = build_discovery_prompt(readme_text, extra_sections)
    else:
        log(f"Initializing: no project files found -- asking {deps.model_name} to infer test command from conventions...")
        prompt = INFERENCE_PROMPT

    def attempt() -> str | Literal[False]:
        response = deps.ask(prompt, model=deps.task0_model).strip()
        cmd = extract_command_from_response(response)
        if not cmd:
            log(f"  WARNING: {deps.model_name} returned an empty test command.")
            return False
        if not looks_like_command(cmd):
            log(f"  WARNING: could not extract a command from response: {response[:120]!r}")
            return False
        return cmd

    cmd = with_retry(attempt)
    if cmd is False:
        log("ERROR: could not extract a test command from README.md after all retries.")
        deps.sys_exit(1)

    normalized = normalize_bare_tool(cmd)
    if normalized is not None:
        log(f"  Normalized bare tool name '{cmd}' → '{normalized}'")
        cmd = normalized

    # If clearly a Rust project but the command doesn't use cargo, fall back.
    if project_dir and (project_dir / "Cargo.toml").exists() and not cmd.startswith("cargo"):
        log(f"  WARNING: extracted command '{cmd}' looks wrong for a Rust project — "
            f"defaulting to 'cargo test'.")
        cmd = "cargo test"

    log(f"Test command: {cmd}")
    return cmd


def infer_build_check_command(test_command: str, project_dir: Path) -> str | None:
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

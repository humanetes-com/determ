"""Prompt-context: a factual description of the cargo subprocess test
environment (P6 relocation).

The pipeline knows which env vars and cfg predicates the test process sees; the
model doesn't — without this signal it proposes strategies that can't work
(enabling a feature whose env var was never set, relying on env-gated code
paths). Pure assembly: state what IS / IS NOT set + the active cfg + the runner;
let the model draw its own implications. Generic across projects.
"""
from __future__ import annotations

from collections.abc import Mapping

# Vars the model should always see if present.
_VISIBLE = ("RUSTFLAGS", "CARGO_TERM_COLOR", "RUSTUP_TOOLCHAIN")
# Vars whose ABSENCE is informative — std libs gate behaviour on these and cache
# the lookup at process start, so a per-test set_var doesn't help.
_NOTABLE = ("RUST_BACKTRACE", "RUST_LIB_BACKTRACE", "RUST_LOG")


def describe_test_environment(
    env: Mapping[str, str],
    active_cfg: list[str] | None,
    excluded_cfg: list[str] | None,
) -> str:
    """Prompt-ready description of the cargo subprocess env and active cluster
    cfg. ``active_cfg``/``excluded_cfg`` are ``None`` when no cluster is active."""
    lines = [
        "--- Test environment (cargo subprocess under coverage instrumentation) ---",
    ]
    for k in _VISIBLE:
        if k in env:
            lines.append(f"  {k} = {env[k]!r}")
    unset = [k for k in _NOTABLE if k not in env]
    if unset:
        lines.append(f"  NOT SET in parent process: {', '.join(unset)}")
        lines.append(
            "  Code paths whose execution depends on these variables (env-gated "
            "logging, backtrace capture, etc.) are unreachable from this test run."
        )

    if active_cfg is not None or excluded_cfg is not None:
        active = ", ".join(active_cfg) if active_cfg else "(none)"
        excluded = ", ".join(excluded_cfg) if excluded_cfg else "(none)"
        lines.append(f"  active cfg predicates: {active}")
        lines.append(f"  excluded cfg predicates: {excluded}")

    lines.append("  test runner: `cargo test --test-threads=1` (under cargo-llvm-cov instrumentation)")
    return "\n".join(lines) + "\n"

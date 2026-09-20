"""Integration: a compiling sibling-red retry prompt is not cargo-check.

Real cargo, the pipeline's runner, `test_regression_gate`, then the live
per-fn retry builder. No model.

The class, run `2978-afabe8cb` (2026-09-20): a body compiled, reddened a
previously-passing test, and `build_per_fn_fix_prompt` opened `broke cargo
check` with the excerpt in COMPILE_ERRORS even after the attempt-site
quote (entry 272) sat in that excerpt. Invented crate: `Wide::widen`
passes on the tree; an attempt indexes past the slice; the gate quotes
that attempt; the retry prompt must use the compiled-then prefix.

Negative control: the same excerpt with kind=COMPILE (the pre-change
opener) still says broke cargo check.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from pipeline_core.adapters.cargo import CargoCli
from pipeline_core.application.reconstruct.cargo_runner import (
    measure_test_state,
    run_cargo,
)
from pipeline_core.application.reconstruct.cargo_runner import (
    test_regression_gate as regression_gate,
)
from pipeline_core.domain.last_error import LastErrorKind, last_error_prefix
from pipeline_core.prompts.context import PerFnFixSections
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_TARGET = (Path(tempfile.gettempdir()) / "determ-integration-target"
           / "compiling-red-retry")
_CMD = "cargo test --offline --no-fail-fast"

_GOOD = """pub struct Wide(pub Vec<u8>);

impl Wide {
    pub fn widen(&self) -> u8 {
        self.0[0]
    }
}
"""

_ATTEMPT = """pub struct Wide(pub Vec<u8>);

impl Wide {
    pub fn widen(&self) -> u8 {
        let n = self.0.len();
        self.0[n + 3]
    }
}
"""

_TESTS = """use wides::Wide;

#[test]
fn widens() {
    assert_eq!(Wide(vec![7]).widen(), 7);
}
"""


def _run(cmd: str, cwd: Path, env: dict[str, str], timeout: int = 600):
    return run_cargo(CargoCli(), cmd, cwd, env, timeout=timeout)


def _crate(tmp: Path) -> Path:
    root = tmp / "wides"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(
        "[package]\nname = \"wides\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
        encoding="utf-8")
    (root / "src" / "lib.rs").write_text(_GOOD, encoding="utf-8")
    (root / "tests" / "wide.rs").write_text(_TESTS, encoding="utf-8")
    return root


def test_retry_prompt_after_a_compiling_red_is_not_cargo_check(
        tmp_path: Path) -> None:
    root = _crate(tmp_path)
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET)
    env["RUST_BACKTRACE"] = "0"
    base = measure_test_state(_run, _CMD, root, env, timeout=600)
    assert base is not None and base.pass_count >= 1
    (root / "src" / "lib.rs").write_text(_ATTEMPT, encoding="utf-8")
    res = regression_gate(_run, _CMD, root, env, base, enabled=True,
                          timeout=600, guard=lambda *_: None)
    assert res is not None, "the attempt must regress widens"
    cmp_, detail = res
    assert cmp_.newly_red
    assert "in the text of the attempt that printed this" in detail
    sections = PerFnFixSections(
        macros="", notes="", container="impl Wide", types="",
        invariants="", siblings="", crate_idioms="", bound_methods="")
    prompt = build_per_fn_fix_prompt(
        file_text=_GOOD, fn_name="widen",
        sig_text="pub fn widen(&self) -> u8",
        test_blocks=[(str(root / "tests" / "wide.rs"), _TESTS)],
        module_name="wides", cluster_name="stable", file_rel="src/lib.rs",
        prev_body="let n = self.0.len();\n        self.0[n + 3]",
        error_excerpt=detail, sections=sections, fr_path=None,
        kind=LastErrorKind.REDDENED)
    assert "broke cargo check" not in prompt
    assert "<COMPILE_ERRORS>" not in prompt
    assert last_error_prefix(detail, LastErrorKind.REDDENED).rstrip() in prompt
    assert "in the text of the attempt that printed this" in prompt
    control = build_per_fn_fix_prompt(
        file_text=_GOOD, fn_name="widen",
        sig_text="pub fn widen(&self) -> u8",
        test_blocks=[], module_name="wides", cluster_name="stable",
        file_rel="src/lib.rs", prev_body="x", error_excerpt=detail,
        sections=sections, fr_path=None, kind=LastErrorKind.COMPILE)
    assert "broke cargo check" in control

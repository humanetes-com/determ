"""Integration: the regression gate quotes a reddened test's crate panic line
from the attempt that printed it, and that quote survives the revert.

Real cargo, the pipeline's runner (`run_cargo`), `measure_test_state` and
`test_regression_gate`. No model.

The class, run `4029650-3353ba0c` (2026-09-19, fill 5): a passing test
reddened with `panicked at src/error.rs:498:38: misaligned pointer
dereference`; the attempt was reverted, and every retry was told that
coordinate while shown the reverted file, whose line 498 was other code.
The shape here is invented: `Wide::widen` passes on the tree, an attempt
rewrites it to index past a slice (a panic at a crate line inside `widen`),
the tree is then put back.

Asserted: the gate's reading for the reddened test quotes the attempt's
panicking line and names `widen`, and still does after the file is reverted.
Negative control: the reverted file's line at that number is not the
attempt's line — a quote read after the revert names other code.

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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real cargo; set INTEGRATION=1"),
]

_TARGET = (Path(tempfile.gettempdir()) / "determ-integration-target"
           / "regression-attempt-quote")
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


def test_the_quote_is_the_attempts_line_after_the_revert(tmp_path: Path) -> None:
    root = tmp_path / "c"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "wides"\nversion = "0.1.0"\nedition = "2021"\n')
    (root / "tests" / "w.rs").write_text(_TESTS)
    lib = root / "src" / "lib.rs"
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(_TARGET)
    env["RUST_BACKTRACE"] = "0"

    lib.write_text(_GOOD)
    base = measure_test_state(_run, _CMD, root, env, timeout=600)
    assert base is not None and "widens" in {n.rsplit("::", 1)[-1]
                                             for n in base.passed_names}

    lib.write_text(_ATTEMPT)
    res = regression_gate(_run, _CMD, root, env, base, enabled=True,
                          timeout=600, guard=lambda *_: None)
    assert res is not None, "the attempt must regress `widens`"
    cmp, detail = res
    lib.write_text(_GOOD)                     # the revert

    said = cmp.newly_red_output["widens"]
    assert "panicked at src/lib.rs:6:" in said
    assert "inside `fn widen`" in said and "self.0[n + 3]" in said
    assert "self.0[n + 3]" in detail
    # Negative control: the reverted file at that line is other code.
    assert "self.0[n + 3]" not in _GOOD.splitlines()[5]

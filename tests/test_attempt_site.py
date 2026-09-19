"""A regressed test's crate panic site is quoted from the attempt that
printed it, at the measurement, before the revert.

Run `4029650-3353ba0c` (2026-09-19, fill 5): every retry of `src/error.rs`
was told `test_large_alignment … panicked at src/error.rs:498:38:
misaligned pointer dereference` and shown the reverted file, whose line 498
was other code. The shape below is invented: an attempt whose `widen` body
dereferences at line 7, reverted to a body where line 7 is something else.

Negative control: the pre-fix gate (no quote) and a quote taken from the
reverted text, which names the wrong line.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.domain.attempt_site import (
    ATTEMPT_QUOTE_MARK,
    quote_attempt_sites,
)
from pipeline_core.domain.refill_evidence import quote_panic_site
from pipeline_core.domain.test_results import TestStateReading, parse_binary_results

_ATTEMPT = """pub struct Wide(pub u64);

impl Wide {
    pub fn widen(&self) -> u64 {
        let p = &self.0 as *const u64 as *const u8;
        let q = unsafe { p.add(1) } as *const u64;
        unsafe { *q }
    }
}
"""

_REVERTED = """pub struct Wide(pub u64);

impl Wide {
    pub fn widen(&self) -> u64 {
        self.0
    }
}

pub fn unrelated() {}
"""

_SAID = ("thread 'aligned' (7) panicked at src/lib.rs:7:18:\n"
         "misaligned pointer dereference: address must be a multiple of 0x8 "
         "but is 0x7ffd1")


def test_the_quote_names_the_attempts_line_and_fn() -> None:
    out = quote_attempt_sites(_SAID, {"src/lib.rs": _ATTEMPT}.get)
    assert "at src/lib.rs:7, inside `fn widen`" in out
    assert ">    7 |         unsafe { *q }" in out
    assert ATTEMPT_QUOTE_MARK in out
    # Idempotent: a second pass adds nothing.
    assert quote_attempt_sites(out, {"src/lib.rs": _ATTEMPT}.get) == out


def test_test_and_runtime_sites_are_not_quoted() -> None:
    said = "thread 't' (1) panicked at tests/a.rs:3:5:\nassertion failed"
    assert quote_attempt_sites(said, {"tests/a.rs": "x\ny\nz\n"}.get) == said
    rt = "thread 't' (1) panicked at /rustc/abc/library/core/src/x.rs:3:5:\nboom"
    assert quote_attempt_sites(rt, lambda _p: "x\n" * 9) == rt


def test_negative_control_quoting_after_the_revert_names_other_code() -> None:
    wrong = quote_panic_site(_SAID, {"src/lib.rs": _REVERTED}.get)
    assert "unsafe { *q }" not in wrong


def _capture() -> str:
    return (
        "running 1 test\n"
        "test aligned ... FAILED\n\nfailures:\n\n"
        "---- aligned stdout ----\n\n" + _SAID + "\n"
        "note: run with `RUST_BACKTRACE=1` environment variable to display a "
        "backtrace\n\n\nfailures:\n    aligned\n\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored\n")


def test_the_gate_quotes_while_the_attempt_is_on_the_tree(tmp_path: Path) -> None:
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(_ATTEMPT)
    base = TestStateReading(parse_binary_results(
        "running 1 test\ntest aligned ... ok\n"
        "test result: ok. 1 passed; 0 failed; 0 ignored\n"))

    def run(cmd: str, cwd: Path, env: dict[str, str], timeout: int = 0):
        return 101, _capture()

    res = test_regression_gate(run, "cargo test", tmp_path, {}, base,
                               enabled=True, timeout=60, guard=lambda *_: None)
    assert res is not None
    cmp, detail = res
    assert "inside `fn widen`" in cmp.newly_red_output["aligned"]
    assert "unsafe { *q }" in detail
    # The revert happens after; the stored reading keeps the attempt's line.
    (tmp_path / "src" / "lib.rs").write_text(_REVERTED)
    assert "unsafe { *q }" in cmp.newly_red_output["aligned"]


def test_the_renderers_keep_the_quote_past_the_line_cap() -> None:
    """A real panic plus its message runs to the 8-line cap; the quote sits
    after it and must survive both renderers."""
    from pipeline_core.application.reconstruct.block_evidence import (
        render_prior_regression,
    )
    from pipeline_core.domain.test_results import render_newly_red_outputs
    long_said = _SAID + "\n" + "\n".join(f"note line {i}" for i in range(10))
    quoted = quote_attempt_sites(long_said, {"src/lib.rs": _ATTEMPT}.get)
    for text in (render_newly_red_outputs(["aligned"], {"aligned": quoted}),
                 render_prior_regression(["aligned"], "", {"aligned": quoted})):
        assert "note line 9" not in text          # the printed part is capped
        assert "unsafe { *q }" in text and "inside `fn widen`" in text

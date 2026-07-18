"""Pins for application/reconstruct/cargo_runner.py (P2 slice 6). The runner
composes the Cargo PORT + diagnostics_from_json; a fake Cargo lets us assert
the flag insertion, rendered-text reconstruction, and rc=-1 parity without a
real cargo binary."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline_core.application.reconstruct.cargo_runner import (
    run_cargo,
    run_check_diag,
    run_scoped_tests,
    u2_test_gate,
)
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.test_report import U2Decision


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str


class _FakeCargo:
    """Records the cmd it was handed and returns a canned Process."""

    def __init__(self, proc: _Proc) -> None:
        self.proc = proc
        self.seen_cmd: str | None = None

    def run(self, cmd: str, cwd: Path, *, env: dict[str, str], timeout: int = 180) -> _Proc:
        self.seen_cmd = cmd
        return self.proc


_ERR_JSON = (
    '{"reason":"compiler-message","message":{"level":"error",'
    '"code":{"code":"E0308"},"message":"mismatched","spans":'
    '[{"is_primary":true,"file_name":"src/e.rs","line_start":10}],'
    '"rendered":"error[E0308]: mismatched\\n --> src/e.rs:10"}}'
)


def test_inserts_message_format_after_subcommand() -> None:
    fake = _FakeCargo(_Proc(0, "", ""))
    run_check_diag(fake, "cargo +nightly check --all", Path("."), {})
    assert fake.seen_cmd is not None
    toks = fake.seen_cmd.split()
    assert toks[:3] == ["cargo", "+nightly", "check"]
    assert toks[3] == "--message-format=json-diagnostic-rendered-ansi"


def test_does_not_double_insert_flag() -> None:
    fake = _FakeCargo(_Proc(0, "", ""))
    run_check_diag(fake, "cargo check --message-format=json", Path("."), {})
    assert fake.seen_cmd is not None
    assert fake.seen_cmd.count("--message-format") == 1


def test_parses_diagnostics_and_rebuilds_rendered() -> None:
    fake = _FakeCargo(_Proc(101, _ERR_JSON + "\nnon-json tail line", ""))
    rc, rendered, diags = run_check_diag(fake, "cargo check", Path("."), {})
    assert rc == 101
    assert len(diags) == 1 and diags[0].code == "E0308" and diags[0].file == "src/e.rs"
    # non-JSON lines kept; each diagnostic's rendered field appended
    assert "non-json tail line" in rendered
    assert "error[E0308]: mismatched" in rendered
    # the raw JSON line must NOT survive into the rendered view
    assert '"reason"' not in rendered


def test_timeout_and_not_found_parity() -> None:
    # adapter encodes timeout / binary-not-found as rc=-1 with the message in stderr
    fake = _FakeCargo(_Proc(-1, "", "TIMEOUT after 180s"))
    rc, text, diags = run_check_diag(fake, "cargo check", Path("."), {})
    assert (rc, text, diags) == (-1, "TIMEOUT after 180s", [])


def test_run_cargo_combined_layout() -> None:
    fake = _FakeCargo(_Proc(0, "out text", "warn text"))
    assert run_cargo(fake, "cargo build", Path("."), {}) == (0, "out text\n--- stderr ---\nwarn text")
    # no stderr → no separator block
    fake2 = _FakeCargo(_Proc(101, "only out", ""))
    assert run_cargo(fake2, "cargo build", Path("."), {}) == (101, "only out")
    # rc=-1 → bare stderr message, no combine
    fake3 = _FakeCargo(_Proc(-1, "", "FILE NOT FOUND: cargo"))
    assert run_cargo(fake3, "cargo build", Path("."), {}) == (-1, "FILE NOT FOUND: cargo")


def _const_run_cargo(rc: int, out: str):
    """A fake `run_cargo` callable: records the cmd, returns canned (rc, out)."""
    seen: dict[str, str] = {}

    def _fn(cmd: str, cwd: Path, env: dict[str, str], *, timeout: int = 180) -> tuple[int, str]:
        seen["cmd"] = cmd
        return rc, out

    _fn.seen = seen  # type: ignore[attr-defined]
    return _fn


def test_run_scoped_tests_pass_fail_timeout() -> None:
    # all pass: rc==0, no FAILED lines
    assert run_scoped_tests(_const_run_cargo(0, "test a ... ok\ntest b ... ok"),
                            "cargo test", Path("."), {}, err_cap=500) == (True, 0, "", set())
    # failures: rc!=0 + FAILED rows → not passed, detail tail, names parsed
    f = _const_run_cargo(101, "test a ... ok\ntest b ... FAILED\npanic: boom")
    passed, n, detail, names = run_scoped_tests(f, "cargo test", Path("."), {}, err_cap=500)
    assert passed is False and n == 1 and names == {"b"} and detail.endswith("panic: boom")
    assert f.seen["cmd"] == "cargo test"  # type: ignore[attr-defined]
    # rc==0 but a FAILED line present → still not passed (defensive)
    p2, n2, _, names2 = run_scoped_tests(_const_run_cargo(0, "test x ... FAILED"),
                                         "cargo test", Path("."), {}, err_cap=500)
    assert p2 is False and n2 == 1 and names2 == {"x"}
    # timeout sentinel (rc=-1) → inconclusive
    assert run_scoped_tests(_const_run_cargo(-1, ""), "cargo test", Path("."), {},
                            err_cap=500) == (False, -1, "scoped test run timed out", set())
    # result is a named TestRunOutcome (drop-in for the old tuple).
    from pipeline_core.domain.test_results import TestRunOutcome
    out = run_scoped_tests(_const_run_cargo(101, "test b ... FAILED"),
                           "cargo test", Path("."), {}, err_cap=500)
    assert isinstance(out, TestRunOutcome)
    assert (out.passed, out.fail_count, out.failed) == (False, 1, {"b"})
    # err_cap truncates the detail tail
    _, _, d, _ = run_scoped_tests(_const_run_cargo(101, "x" * 100 + "\ntest z ... FAILED"),
                                  "cargo test", Path("."), {}, err_cap=10)
    assert len(d) == 10


class _FakeStructure:
    def __init__(self, recs=None):
        self._recs = recs or []

    def enumerate_fns(self, src_dir):
        return self._recs


def _gate(run_cargo_fn, fn_tests, unit_fns=frozenset(), test_cmd="cargo test",
          enabled=True, guard=None, structure=None, src_dir=Path(".")):
    return u2_test_gate(
        run_cargo_fn, fn_tests, unit_fns, test_cmd, src_dir, {},
        structure=structure or _FakeStructure(),
        enabled=enabled, timeout=120, err_cap=500,
        guard=guard or (lambda *_: None))


# assertion failure with a proper stdout section (stays self → REJECT)
_ASSERT_OUT = (
    "test a ... ok\ntest b ... FAILED\n\n"
    "---- b stdout ----\n"
    "thread 'b' panicked at src/lib.rs:1:1:\nassertion failed: boom\n")


def test_u2_gate_accepts_when_disabled_or_no_tests() -> None:
    boom = _const_run_cargo(101, "test a ... FAILED")
    assert _gate(boom, {"a"}, enabled=False).decision is U2Decision.ACCEPT
    assert _gate(boom, set()).decision is U2Decision.ACCEPT
    assert _gate(boom, {"a"}, test_cmd=None).decision is U2Decision.ACCEPT


def test_u2_gate_accepts_when_tests_pass() -> None:
    v = _gate(_const_run_cargo(0, "test a ... ok"), {"a"})
    assert v.decision is U2Decision.ACCEPT


def test_u2_gate_inconclusive_on_timeout() -> None:
    v = _gate(_const_run_cargo(-1, ""), {"a"})
    assert v.decision is U2Decision.ACCEPT


def test_u2_gate_blocks_on_own_failing_test() -> None:
    v = _gate(_const_run_cargo(101, _ASSERT_OUT), {"a", "b"})
    assert v.decision is U2Decision.REJECT
    assert v.self_failures == frozenset({"b"})
    assert "still FAIL" in v.detail and "assertion failed: boom" in v.detail


def test_u2_gate_ignores_unrelated_failures() -> None:
    # a failing test NOT in fn_tests must not block this unit (scoped out)
    v = _gate(_const_run_cargo(101, "test other ... FAILED"), {"a"})
    assert v.decision is U2Decision.ACCEPT


def test_u2_gate_external_stub_panic_accepts(tmp_path) -> None:
    # the unit's test compiles but panics in ANOTHER file's still-stubbed fn →
    # ACCEPT_EXTERNAL_ONLY (keep the fill), NOT a rejection.
    (tmp_path / "src").mkdir()
    err = tmp_path / "src" / "error.rs"
    err.write_text("fn context() {\n    unimplemented!()\n}\n", encoding="utf-8")
    recs = [{"file": "src/error.rs", "fn_name": "context", "container": "impl Error",
             "sig_byte_start": 0, "body_byte_end": len(err.read_text())}]
    out = ("test a ... FAILED\n\n---- a stdout ----\n"
           "thread 'a' panicked at src/error.rs:2:5:\nnot yet implemented\n")
    v = _gate(_const_run_cargo(101, out), {"a"},
              unit_fns=frozenset({FnSite("src/lib.rs", "mine", "")}),
              structure=_FakeStructure(recs), src_dir=tmp_path)
    assert v.decision is U2Decision.ACCEPT_EXTERNAL_ONLY
    assert FnSite("src/error.rs", "context", "impl Error") in v.blocking_fns


def test_u2_gate_surfaces_missing_verdicts_on_crashed_scoped_binary() -> None:
    # rc!=0 with the unit's own test absent from the output = the scoped
    # binary crashed/failed to compile. Verdict stays fail-safe ACCEPT (the
    # regression gate owns executed-count drops) but the deficit must reach
    # the guard channel, never pass silently.
    fired = []
    v = _gate(_const_run_cargo(101, "error: process didn't exit successfully"),
              {"a", "b"}, guard=lambda loc, m: fired.append((loc, m)))
    assert v.decision is U2Decision.ACCEPT
    assert fired and fired[0][0] == "phase-c/u2_missing_verdict"
    assert "'a'" in fired[0][1] and "'b'" in fired[0][1]


def test_u2_gate_failsafe_and_inconclusive() -> None:
    # run_cargo raises → guard fires, ACCEPT (fail-safe)
    fired = []

    def _raise(*_a, **_k):
        raise RuntimeError("cargo blew up")

    assert _gate(_raise, {"a"},
                 guard=lambda loc, m: fired.append(loc)).decision is U2Decision.ACCEPT
    assert fired == ["phase-c/u2_skip"]
    # timeout (rc=-1) → INCONCLUSIVE → ACCEPT
    assert _gate(_const_run_cargo(-1, ""), {"a"}).decision is U2Decision.ACCEPT


# --- measure_test_state / test_regression_gate (monotone ratchet) ------------

_RES = "test result: FAILED. {p} passed; {f} failed; 0 ignored"


def _cargo_suite(rc: int, passed: int, failed_names: list[str]):
    body = "".join(f"test {n} ... FAILED\n" for n in failed_names)
    body += _RES.format(p=passed, f=len(failed_names))
    return _const_run_cargo(rc, body)


def test_measure_test_state_counts_and_inconclusive() -> None:
    from pipeline_core.application.reconstruct.cargo_runner import measure_test_state
    # green suite: rc==0, pass count parsed, no failures
    assert measure_test_state(
        _const_run_cargo(0, "test result: ok. 5 passed; 0 failed; 0 ignored"),
        "cargo test", Path("."), {}) == (5, frozenset())
    # some failures: pass count + failing names
    assert measure_test_state(_cargo_suite(101, 3, ["b", "c"]),
                              "cargo test", Path("."), {}) == (3, frozenset({"b", "c"}))
    # crate did not compile (no test-result lines, rc!=0) → inconclusive None
    assert measure_test_state(_const_run_cargo(101, "error[E0308]: mismatched"),
                              "cargo test", Path("."), {}) is None
    # timeout → inconclusive None
    assert measure_test_state(_const_run_cargo(-1, ""), "cargo test", Path("."), {}) is None


def _regr(run_cargo_fn, baseline, enabled=True):
    from pipeline_core.application.reconstruct.cargo_runner import test_regression_gate
    return test_regression_gate(
        run_cargo_fn, "cargo test", Path("."), {}, baseline,
        enabled=enabled, timeout=120, guard=lambda *_: None)


def test_regression_gate_accepts_when_no_new_red() -> None:
    base: tuple[int, frozenset[str]] = (5, frozenset({"b"}))  # b already failing
    # b still failing, pass count held → NOT a regression
    assert _regr(_cargo_suite(101, 5, ["b"]), base) is None
    # a formerly-failing test turned green (pass up) → accept
    assert _regr(_const_run_cargo(0, "test result: ok. 6 passed; 0 failed; 0 ignored"),
                 base) is None


def test_regression_gate_blocks_new_red_and_pass_drop() -> None:
    base: tuple[int, frozenset[str]] = (5, frozenset({"b"}))
    # a NEW test (z) went red though b is fixed → regression names z
    res = _regr(_cargo_suite(101, 5, ["z"]), base)
    assert res is not None
    newly, msg = res
    assert newly == frozenset({"z"}) and "REGRESSED" in msg
    # no new named failure but pass count dropped (tests vanished) → regression;
    # with the failed set unchanged this IS an executed-count drop, so the
    # detail now names the crashed/non-compiling-binary mechanism.
    res2 = _regr(_cargo_suite(101, 3, ["b"]), base)
    assert res2 is not None
    newly2, msg2 = res2
    assert newly2 == frozenset() and "EXECUTE" in msg2


def test_regression_gate_self_disables() -> None:
    base: tuple[int, frozenset[str]] = (5, frozenset())
    assert _regr(_cargo_suite(101, 5, ["z"]), base, enabled=False) is None   # off
    assert _regr(_cargo_suite(101, 5, ["z"]), None) is None                  # no baseline
    # post-fill suite inconclusive (crate red now) → cannot judge → accept
    assert _regr(_const_run_cargo(101, "error: could not compile"), base) is None


def test_regression_gate_blocks_executed_count_drop() -> None:
    # A fill that crashes a test BINARY removes its tests from the output:
    # the pass count can HOLD (the lost binary was mostly failing) and no new
    # name goes red — but fewer tests execute than at baseline. That is a
    # regression (the anyhow oracle-shrink 68→50 that kept a segfaulting body).
    base: tuple[int, frozenset[str]] = (5, frozenset({"b", "c", "d"}))  # 8 ran
    res = _regr(_cargo_suite(101, 5, ["b"]), base)  # only 6 ran, pass held
    assert res is not None
    newly, msg = res
    assert newly == frozenset()
    assert "EXECUTE" in msg
    # Equal executed totals with pass held stays accepted (no false positive
    # when a failing test merely flips to passing).
    assert _regr(_cargo_suite(101, 6, ["b", "c"]), base) is None

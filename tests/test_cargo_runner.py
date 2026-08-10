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
          enabled=True, guard=None, structure=None, src_dir=Path("."),
          baseline=None):
    return u2_test_gate(
        run_cargo_fn, fn_tests, unit_fns, test_cmd, src_dir, {},
        structure=structure or _FakeStructure(),
        enabled=enabled, timeout=120, err_cap=500,
        guard=guard or (lambda *_: None), baseline=baseline)


def _judged(names, *, completed=True):
    """Baseline reading in which every `names` entry got a real verdict."""
    from pipeline_core.domain.test_results import (
        TestStateReading,
        parse_binary_results,
    )
    body = "".join(f"test {n} ... ok\n" for n in names)
    if completed:
        body += f"test result: ok. {len(names)} passed; 0 failed; 0 ignored"
    return TestStateReading(parse_binary_results(body))


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


def test_u2_gate_charges_verdicts_the_body_destroyed() -> None:
    # The worst case prints NOTHING: a body that aborts instantly leaves an
    # empty failure list, which the "no failures" early return reads as
    # success. Held against a baseline that judged these tests, the silence
    # is the verdict — REJECT, and say so in the words the model reads.
    v = _gate(_const_run_cargo(101, "error: process didn't exit successfully"),
              {"a", "b"}, baseline=_judged(["a", "b"]))
    assert v.decision is U2Decision.REJECT
    assert v.self_failures == frozenset({"a", "b"})
    assert "ABORTED the test process" in v.detail
    assert "a" in v.detail and "b" in v.detail


def test_u2_gate_abort_outranks_leftover_assertions() -> None:
    # An assertion that still printed is real but secondary: chasing it while
    # the process aborts spends the attempt on the wrong defect.
    out = ("test b ... FAILED\n\n---- b stdout ----\n"
           "thread 'b' panicked at src/lib.rs:1:1:\nassertion failed: boom\n")
    v = _gate(_const_run_cargo(101, out), {"a", "b", "c"},
              baseline=_judged(["a", "b", "c"]))
    assert v.decision is U2Decision.REJECT
    assert v.self_failures == frozenset({"a", "b", "c"})
    assert v.detail.index("ABORTED") < v.detail.index("Secondary")
    assert "assertion failed: boom" in v.detail


def test_u2_gate_does_not_charge_a_verdict_the_baseline_never_had() -> None:
    # A baseline whose binary crashed judged nothing, so a test missing now
    # was never observed to have a verdict — no evidence, no charge. Same
    # rule `passed_names` applies to a crashed binary's partial output.
    v = _gate(_const_run_cargo(101, "error: process didn't exit successfully"),
              {"a", "b"}, baseline=_judged(["a", "b"], completed=False))
    assert v.decision is U2Decision.ACCEPT


def test_u2_gate_compile_failure_is_not_an_abort() -> None:
    # No binary ran, so no verdict was destroyed — this is the compile path,
    # which the fix loop already feeds real diagnostics.
    v = _gate(_const_run_cargo(101, "error: could not compile `crate`"),
              {"a", "b"}, baseline=_judged(["a", "b"]))
    assert v.decision is U2Decision.ACCEPT


def test_u2_gate_without_baseline_keeps_failsafe_accept() -> None:
    # Callers that pass no baseline cannot distinguish a destroyed verdict
    # from one that never existed; behaviour must stay as it was.
    v = _gate(_const_run_cargo(101, "error: process didn't exit successfully"),
              {"a", "b"})
    assert v.decision is U2Decision.ACCEPT


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


def _reading(passed: int, failed_names: list[str]):
    """Baseline TestStateReading with one completed implicit binary."""
    from pipeline_core.domain.test_results import TestStateReading, parse_binary_results
    body = "".join(f"test {n} ... FAILED\n" for n in failed_names)
    body += _RES.format(p=passed, f=len(failed_names))
    return TestStateReading(parse_binary_results(body))


def test_measure_test_state_counts_and_inconclusive() -> None:
    from pipeline_core.application.reconstruct.cargo_runner import measure_test_state
    # green suite: rc==0, pass count parsed, no failures
    r = measure_test_state(
        _const_run_cargo(0, "test result: ok. 5 passed; 0 failed; 0 ignored"),
        "cargo test", Path("."), {})
    assert r is not None and (r.pass_count, r.failed_names) == (5, frozenset())
    # some failures: pass count + failing names
    r2 = measure_test_state(_cargo_suite(101, 3, ["b", "c"]),
                            "cargo test", Path("."), {})
    assert r2 is not None
    assert (r2.pass_count, r2.failed_names) == (3, frozenset({"b", "c"}))
    # crate did not compile (no test-result lines, rc!=0) → inconclusive None
    assert measure_test_state(_const_run_cargo(101, "error[E0308]: mismatched"),
                              "cargo test", Path("."), {}) is None
    # timeout → inconclusive None
    assert measure_test_state(_const_run_cargo(-1, ""), "cargo test", Path("."), {}) is None


def _regr(run_cargo_fn, baseline, enabled=True, guard=lambda *_: None):
    from pipeline_core.application.reconstruct.cargo_runner import test_regression_gate
    return test_regression_gate(
        run_cargo_fn, "cargo test", Path("."), {}, baseline,
        enabled=enabled, timeout=120, guard=guard)


def test_regression_gate_accepts_when_no_new_red() -> None:
    base = _reading(5, ["b"])  # b already failing
    # b still failing, pass count held → NOT a regression
    assert _regr(_cargo_suite(101, 5, ["b"]), base) is None
    # a formerly-failing test turned green (pass up) → accept
    assert _regr(_const_run_cargo(0, "test result: ok. 6 passed; 0 failed; 0 ignored"),
                 base) is None


def test_regression_gate_blocks_new_red_and_pass_drop() -> None:
    base = _reading(5, ["b"])
    # a NEW test (z) went red though b is fixed → regression names z
    res = _regr(_cargo_suite(101, 5, ["z"]), base)
    assert res is not None
    cmp, msg = res
    newly = cmp.newly_red
    assert newly == frozenset({"z"}) and "REGRESSED" in msg
    # no new named failure but pass count dropped within the completed binary
    res2 = _regr(_cargo_suite(101, 3, ["b"]), base)
    assert res2 is not None
    cmp2, msg2 = res2
    assert cmp2.newly_red == frozenset() and "EXECUTE" in msg2
    # count-drop and crash are distinct classes: distinct stall signatures
    assert cmp2.signature == frozenset({"__pass_count_drop__"})


def test_regression_gate_self_disables() -> None:
    base = _reading(5, [])
    assert _regr(_cargo_suite(101, 5, ["z"]), base, enabled=False) is None   # off
    assert _regr(_cargo_suite(101, 5, ["z"]), None) is None                  # no baseline
    # post-fill suite inconclusive (crate red now) → cannot judge → accept
    assert _regr(_const_run_cargo(101, "error: could not compile"), base) is None


def test_regression_gate_blocks_executed_count_drop() -> None:
    # A fill that shrinks a COMPLETED binary's executed count is a regression
    # even when the pass count holds and no new name goes red.
    base = _reading(5, ["b", "c", "d"])  # 8 ran
    res = _regr(_cargo_suite(101, 5, ["b"]), base)  # only 6 ran, pass held
    assert res is not None
    cmp, msg = res
    newly = cmp.newly_red
    assert newly == frozenset()
    assert "EXECUTE" in msg
    # Equal executed totals with pass held stays accepted (no false positive
    # when a failing test merely flips to passing).
    assert _regr(_cargo_suite(101, 6, ["b", "c"]), base) is None


def test_regression_gate_binary_scoped_crash_exclusion() -> None:
    # THE plateau class: a binary already CRASHED at baseline emits a
    # nondeterministic verdict subset — its tests may neither convict nor
    # exonerate a fill. Baseline: binary A complete (2 pass), binary B
    # crashed mid-run. After: A unchanged, B's flicker shows a FAILED name
    # that never reported at baseline → must NOT be newly-red, gate accepts.
    from pipeline_core.domain.test_results import TestStateReading, parse_binary_results
    base_out = (
        "    Running tests/a.rs (t/a-1)\n"
        "running 2 tests\ntest x ... ok\ntest y ... ok\n"
        "test result: ok. 2 passed; 0 failed; 0 ignored\n"
        "    Running tests/b.rs (t/b-2)\n"
        "running 3 tests\ntest p ... ok\ntest q ...\n")
    cur_out = (
        "    Running tests/a.rs (t/a-1)\n"
        "running 2 tests\ntest x ... ok\ntest y ... ok\n"
        "test result: ok. 2 passed; 0 failed; 0 ignored\n"
        "    Running tests/b.rs (t/b-2)\n"
        "running 3 tests\ntest q ... FAILED\ntest r ...\n")
    base = TestStateReading(parse_binary_results(base_out))
    fired: list[tuple[str, str]] = []
    from pipeline_core.application.reconstruct.cargo_runner import test_regression_gate
    res = test_regression_gate(
        _const_run_cargo(101, cur_out), "cargo test", Path("."), {}, base,
        enabled=True, timeout=120, guard=lambda loc, m: fired.append((loc, m)))
    assert res is None  # crashed-at-baseline binary excluded entirely
    assert fired and fired[0][0] == "phase-c/u2_missing_verdict"
    assert "tests/b.rs" in fired[0][1]


def test_regression_gate_detects_but_does_not_charge_a_bare_crash() -> None:
    # A binary COMPLETE at baseline that crashes after the fill, with no
    # newly-red verdict in any completed binary: DETECTED (the comparison
    # names the crash) but no longer CHARGED — a crash is not a verdict from
    # an attributed test, so the gate keeps the fill and the sweep-level
    # machinery (oracle accounting, localizer, batch ratchet) owns it. The
    # rendering pins move to `compare_readings` directly, which still feeds
    # the revert log lines on the paths that DO charge.
    from pipeline_core.application.reconstruct.cargo_runner import (
        compare_readings,
    )
    from pipeline_core.domain.test_results import TestStateReading, parse_binary_results
    base_out = (
        "    Running tests/a.rs (t/a-1)\n"
        "running 2 tests\ntest x ... ok\ntest y ... ok\n"
        "test result: ok. 2 passed; 0 failed; 0 ignored\n")
    cur_out = (
        "    Running tests/a.rs (t/a-1)\n"
        "running 2 tests\ntest x ... ok\ntest y ...\n")
    base = TestStateReading(parse_binary_results(base_out))
    after = TestStateReading(parse_binary_results(cur_out))
    cmp = compare_readings(base, after)
    assert cmp.newly_red == frozenset()
    assert cmp.newly_crashed == ("tests/a.rs",)
    # the log summary must name the CRASH, not fall back to "pass-count drop"
    assert cmp.summary == "binary crashed: ['tests/a.rs'] (net -2 test(s))"
    assert cmp.signature == frozenset({"__binary_crash__:tests/a.rs"})
    fired: list[tuple[str, str]] = []
    res = _regr(_const_run_cargo(101, cur_out), base,
                guard=lambda loc, m: fired.append((loc, m)))
    assert res is None
    assert any(loc == "phase-c/crash_only_unmeasurable_kept"
               and "tests/a.rs" in m for loc, m in fired)


def test_u2_gate_rc_zero_missing_test_is_a_filter_miss_not_an_abort() -> None:
    # The binary completed (rc == 0). A name absent from its output was never
    # selected by libtest's filter — no verdict was destroyed.
    v = _gate(_const_run_cargo(0, "test a ... ok\ntest result: ok. 1 passed"),
              {"a", "b"}, baseline=_judged(["a", "b"]))
    assert v.decision is U2Decision.ACCEPT


# --- crash attribution: a stub panic is the hollow tree speaking -------------
# Run `1048052-2feafd52` (2026-08-09, cluster `stable`): `from_boxed` burned 5
# attempts on "U2 test failed ... (binary crashed)" and `context.rs`'s
# file-first fill was reverted "binary crashed: ['unittests src/lib.rs']
# (net -2 test(s))" — in both cases the aborting panic was `not implemented`
# inside a still-stubbed sibling (`src/error.rs` drop-path fns), so the fill
# under judgment was never the fault. While any drop-path stub remains, every
# correct fill crashes the binary, the guard reverts it, and the component can
# only close by one lottery sweep that fills everything at once.


def test_u2_gate_abort_by_external_stub_panic_keeps_fill(tmp_path) -> None:
    # The unit's judged tests vanished (abort), but the capture proves the
    # aborting panic fired inside ANOTHER fn's `unimplemented!()` — e.g. a
    # correct vtable body finally lets Error::drop reach hollow object_drop.
    # Keep the fill, name the blocker, defer to the outer loop.
    (tmp_path / "src").mkdir()
    err = tmp_path / "src" / "error.rs"
    err.write_text("fn object_drop() {\n    unimplemented!()\n}\n",
                   encoding="utf-8")
    recs = [{"file": "src/error.rs", "fn_name": "object_drop",
             "container": "impl Error", "sig_byte_start": 0,
             "body_byte_end": len(err.read_text())}]
    out = ("running 2 tests\n"
           "test a ... \n"
           "thread 'a' panicked at src/error.rs:2:5:\n"
           "not implemented\n"
           "error: process didn't exit successfully")
    v = _gate(_const_run_cargo(101, out), {"a", "b"},
              baseline=_judged(["a", "b"]),
              unit_fns=frozenset({FnSite("src/lib.rs", "mine", "")}),
              structure=_FakeStructure(recs), src_dir=tmp_path)
    assert v.decision is U2Decision.ACCEPT_EXTERNAL_ONLY
    assert FnSite("src/error.rs", "object_drop", "impl Error") in v.blocking_fns


def test_u2_gate_abort_with_own_panic_still_charges(tmp_path) -> None:
    # Negative control for the keep: the aborting panic locates INSIDE the
    # unit itself — that is the unit's own fault, REJECT exactly as before.
    (tmp_path / "src").mkdir()
    lib = tmp_path / "src" / "lib.rs"
    lib.write_text("fn mine() {\n    unimplemented!()\n}\n", encoding="utf-8")
    recs = [{"file": "src/lib.rs", "fn_name": "mine", "container": "",
             "sig_byte_start": 0, "body_byte_end": len(lib.read_text())}]
    out = ("running 2 tests\n"
           "test a ... \n"
           "thread 'a' panicked at src/lib.rs:2:5:\n"
           "not implemented\n")
    v = _gate(_const_run_cargo(101, out), {"a", "b"},
              baseline=_judged(["a", "b"]),
              unit_fns=frozenset({FnSite("src/lib.rs", "mine", "")}),
              structure=_FakeStructure(recs), src_dir=tmp_path)
    assert v.decision is U2Decision.REJECT


def test_u2_gate_abort_without_stub_evidence_still_charges() -> None:
    # Negative control: a silent abort (segfault — no panic text at all) keeps
    # the pre-existing REJECT; forgiveness rides ONLY on stub-panic proof.
    v = _gate(_const_run_cargo(101, "error: process didn't exit successfully"),
              {"a", "b"}, baseline=_judged(["a", "b"]))
    assert v.decision is U2Decision.REJECT
    assert "ABORTED the test process" in v.detail


def _crashed_after(panic_lines: str) -> str:
    """After-reading for the regression gate: the one baseline binary opens
    its section and dies before the trailer."""
    return ("    Running unittests src/lib.rs (t/lib-1)\n"
            "running 2 tests\n"
            "test x ... \n" + panic_lines)


def _completed_baseline() -> object:
    from pipeline_core.domain.test_results import (
        TestStateReading,
        parse_binary_results,
    )
    base_out = ("    Running unittests src/lib.rs (t/lib-1)\n"
                "running 2 tests\ntest x ... ok\ntest y ... ok\n"
                "test result: ok. 2 passed; 0 failed; 0 ignored\n")
    return TestStateReading(parse_binary_results(base_out))


def test_regression_gate_keeps_fill_when_crash_is_stub_panic_only() -> None:
    # A previously-completing binary crashes after the fill, and the capture's
    # ONLY panic is `not implemented` in a stub: the crash is a still-hollow
    # sibling speaking. Keep the fill (return None), surface via guard.
    fired: list[tuple[str, str]] = []
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    res = test_regression_gate(
        _const_run_cargo(
            101, _crashed_after("thread 'x' panicked at src/error.rs:64:17:\n"
                                "not implemented\n")),
        "cargo test", Path("."), {}, _completed_baseline(),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired.append((loc, m)))
    assert res is None
    assert any(loc == "phase-c/crash_blocked_kept" for loc, _m in fired)


def test_regression_gate_keeps_crash_without_stub_evidence_as_unmeasurable() -> None:
    # The inversion at this gate: a crash-only regression with NO stub-panic
    # attribution — a segfault, or an assertion panic beside a stub panic —
    # is UNMEASURABLE, not chargeable. Four `src/context.rs::StdErrorforE`
    # re-fills were reverted `net -6, binary crashed` (run `1824387`,
    # 2026-08-10) for a crash the localizer attributed to nothing; the gate
    # now keeps such fills under its own guard tag and the sweep-level
    # machinery judges the joint tree. The stub-panic case keeps its more
    # specific tag; everything crash-only lands in one of the two.
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    fired: list[tuple[str, str]] = []
    res = test_regression_gate(
        _const_run_cargo(101, _crashed_after("Segmentation fault\n")),
        "cargo test", Path("."), {}, _completed_baseline(),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired.append((loc, m)))
    assert res is None
    assert any(loc == "phase-c/crash_only_unmeasurable_kept"
               for loc, _m in fired)
    fired2: list[tuple[str, str]] = []
    res2 = test_regression_gate(
        _const_run_cargo(
            101, _crashed_after(
                "thread 'x' panicked at tests/t.rs:9:5:\n"
                "assertion failed: boom\n"
                "thread 'x' panicked at src/error.rs:64:17:\n"
                "not implemented\n")),
        "cargo test", Path("."), {}, _completed_baseline(),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired2.append((loc, m)))
    assert res2 is None
    assert any(loc == "phase-c/crash_only_unmeasurable_kept"
               for loc, _m in fired2)


def test_regression_gate_still_reverts_on_newly_red_verdict() -> None:
    # The control that keeps the inversion honest: implicating evidence — a
    # previously-passing test reporting a RED VERDICT in a COMPLETED binary —
    # still reverts absolutely, crash or no crash beside it.
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    cur_out = ("    Running unittests src/lib.rs (t/lib-1)\n"
               "running 2 tests\ntest x ... ok\ntest y ... FAILED\n"
               "test result: FAILED. 1 passed; 1 failed; 0 ignored\n")
    res = test_regression_gate(
        _const_run_cargo(101, cur_out),
        "cargo test", Path("."), {}, _completed_baseline(),
        enabled=True, timeout=120, guard=lambda *_: None)
    assert res is not None
    assert "y" in res[0].newly_red

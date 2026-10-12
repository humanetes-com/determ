"""Pins for application/reconstruct/cargo_runner.py. The runner
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


def test_regression_gate_charges_a_bare_crash_as_the_sweep_would() -> None:
    # A binary COMPLETE at baseline that crashes after the fill, with no
    # newly-red verdict in any completed binary. The comparison names the
    # crash, and the gate now CHARGES it: the sweep counts the vanished
    # tests as failing and the ratchet restores the snapshot one iteration
    # later, so keeping it here "with cost withheld" was a full iteration
    # lost per cycle (run `959654-1e96b403`, 2026-09-06: error.rs kept with
    # five crashed binaries, reverted by the next sweep's ratchet).
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
    assert res is not None and res[0].newly_crashed == ("tests/a.rs",)
    assert any(loc == "phase-c/crash_only_reverted"
               and "tests/a.rs" in m for loc, m in fired)
    assert not any(loc == "phase-c/crash_only_unmeasurable_kept"
                   for loc, _m in fired)


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


def test_regression_gate_keeps_a_stub_panic_crash_only_while_the_hollow_set_shrinks(
        tmp_path: Path) -> None:
    # A previously-completing binary crashes after the fill, and the capture's
    # ONLY panic is `not implemented` in a stub. The sweep's closure
    # exemption (`tdd_ratchet.closure_withheld`) keeps such a reading only
    # while the hollow set strictly SHRANK; the gate applies the same rule.
    # `tmp_path` holds no `.rs` file, so the hollow count after is 0.
    fired: list[tuple[str, str]] = []
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    res = test_regression_gate(
        _const_run_cargo(
            101, _crashed_after("thread 'x' panicked at src/error.rs:64:17:\n"
                                "not implemented\n")),
        "cargo test", tmp_path, {}, _completed_baseline(),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired.append((loc, m)), hollow_before=1)
    assert res is None
    assert any(loc == "phase-c/crash_closure_kept" for loc, _m in fired)
    # Control: the same capture with the hollow set NOT shrinking (run
    # `1160135-7b10cdfa`'s shape: 15 → 15) is a revert, as the sweep would
    # charge it — and so is a caller that did not measure the hollow set.
    for before in (0, None):
        fired2: list[tuple[str, str]] = []
        res2 = test_regression_gate(
            _const_run_cargo(
                101, _crashed_after("thread 'x' panicked at src/error.rs:64:17:\n"
                                    "not implemented\n")),
            "cargo test", tmp_path, {}, _completed_baseline(),
            enabled=True, timeout=120,
            guard=lambda loc, m, _f=fired2: _f.append((loc, m)),
            hollow_before=before)
        assert res2 is not None
        assert any(loc == "phase-c/crash_stub_panic_reverted" for loc, _m in fired2)
        assert not any(loc == "phase-c/crash_blocked_kept" for loc, _m in fired2)


def test_regression_gate_reverts_stub_panic_when_this_fill_introduced_stubs() -> None:
    # Same capture as the keep above: a previously-completing binary dies on
    # a stub panic. The caller just put that stub back (file-first peel).
    # Pre-fix this returned None (keep) and the peel's own panic exonerated
    # it — run 1160135-7b10cdfa. With introduced_stubs the gate reverts.
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
        guard=lambda loc, m: fired.append((loc, m)),
        introduced_stubs=True)
    assert res is not None
    assert res[0].newly_crashed and not res[0].newly_red
    assert any(loc == "phase-c/crash_introduced_stub_reverted" for loc, _ in fired)
    assert not any(loc == "phase-c/crash_blocked_kept" for loc, _ in fired)


def test_regression_gate_reverts_a_crash_without_stub_evidence() -> None:
    # A crash-only regression with NO stub-panic attribution — a segfault,
    # or an assertion panic beside a stub panic — is charged here exactly as
    # the sweep would charge it (the oracle counts the vanished tests as
    # failing; the ratchet restores the snapshot). It used to be kept as
    # "unmeasurable, cost withheld", which made every such keep a breach
    # one iteration later (runs `334902-2774708c` iters 9→10 and
    # `959654-1e96b403` iter 1→2, 2026-09-06).
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    fired: list[tuple[str, str]] = []
    res = test_regression_gate(
        _const_run_cargo(101, _crashed_after("Segmentation fault\n")),
        "cargo test", Path("."), {}, _completed_baseline(),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired.append((loc, m)))
    assert res is not None and res[0].crash_lost_pass == 2
    assert any(loc == "phase-c/crash_only_reverted" for loc, _m in fired)
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
    assert res2 is not None
    assert any(loc == "phase-c/crash_only_reverted" for loc, _m in fired2)
    assert not any(loc == "phase-c/crash_only_unmeasurable_kept"
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


def test_a_passing_accept_carries_the_names_it_saw_pass() -> None:
    """ACCEPT is three states — tests passed / gate off / inconclusive — and
    only the first is a measurement of the fill. The ledger has to be able to
    tell them apart, so the one accept with positive evidence carries it.

    Run `1064759-0f23fb68` (2026-08-15/16) is the cost of the conflation: 50
    fills recorded `accept`/POSITIVE while the per-binary verdict vector stayed
    byte-identical for the whole 7h48m."""
    ok = _const_run_cargo(0, "test a ... ok\ntest b ... ok\n"
                             "test result: ok. 2 passed; 0 failed; 0 ignored")
    v = _gate(ok, {"a"})
    assert v.decision is U2Decision.ACCEPT
    assert v.self_passed == frozenset({"a"}), \
        "only the unit's OWN tests count — `b` is not attributed to it"

    # The three accepts that measured nothing must stay empty, or the ledger
    # learns to call a timeout a success.
    boom = _const_run_cargo(101, "test a ... FAILED")
    assert _gate(boom, {"a"}, enabled=False).self_passed == frozenset()
    assert _gate(boom, set()).self_passed == frozenset()
    assert _gate(_const_run_cargo(-1, "timeout"), {"a"}).self_passed == frozenset()


def test_regression_gate_reverts_a_crash_in_a_stub_free_tree() -> None:
    """The crash-only keep exists for a still-hollow sibling's panic. With no
    stub in the tree there is no such sibling, and the fill is the only change
    between a baseline whose binaries completed and a reading where they did
    not. Run `626784-4b7c38ba` (2026-08-20): one kept crash in a 0-stub tree
    blinded the rest of its batch — every later unit judged with 7 binaries
    excluded, both owners of 19 failing tests accepted `unmeasurable`. With
    `stub_free=True` the gate reverts now; the default keeps the recorded
    behaviour (negative control)."""
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    fired: list[tuple[str, str]] = []
    res = test_regression_gate(
        _const_run_cargo(101, _crashed_after("Segmentation fault\n")),
        "cargo test", Path("."), {}, _completed_baseline(),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired.append((loc, m)), stub_free=True)
    assert res is not None
    cmp, detail = res
    assert cmp.newly_crashed and not cmp.newly_red
    assert any(loc == "phase-c/crash_stub_free_reverted" for loc, _m in fired)
    assert not any(loc == "phase-c/crash_only_unmeasurable_kept"
                   for loc, _m in fired)
    assert "crash" in detail.lower()
    # The same capture without the stub-free statement is now ALSO a revert
    # (the crash-only keep is gone), under its own tag.
    fired2: list[tuple[str, str]] = []
    assert test_regression_gate(
        _const_run_cargo(101, _crashed_after("Segmentation fault\n")),
        "cargo test", Path("."), {}, _completed_baseline(),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired2.append((loc, m))) is not None
    assert any(loc == "phase-c/crash_only_reverted" for loc, _m in fired2)
    # A stub panic in the capture still outranks the stub-free statement (the
    # caller's pass-start scan can lag a sibling's revert-to-stub), and it
    # is judged by the closure rule: without a shrinking hollow set, revert.
    fired3: list[tuple[str, str]] = []
    assert test_regression_gate(
        _const_run_cargo(101, _crashed_after(
            "thread 'x' panicked at src/error.rs:64:17:\nnot implemented\n")),
        "cargo test", Path("."), {}, _completed_baseline(),
        enabled=True, timeout=120,
        guard=lambda loc, m: fired3.append((loc, m)), stub_free=True) is not None
    assert any(loc == "phase-c/crash_stub_panic_reverted" for loc, _m in fired3)
    assert not any(loc == "phase-c/crash_stub_free_reverted" for loc, _m in fired3)


# --- the abort-in-Drop shape of run 2585062-494e630e -------------------------
#
# A correct fill lets `Error::drop` reach the still-stubbed `vtable()`; the
# stub panics while a first panic is unwinding; the runtime prints its own
# `panicked at …/library/core/src/panicking.rs:233:5: panic in a destructor
# during cleanup` and aborts. Both gates read the crashed binaries' OWN
# sections and treat the runtime line as the announcement of the stub panic,
# never as a veto; a `crashed_all_red` binary's assertion elsewhere in the
# same capture is not this crash's evidence.

_RUSTC_PANICKING = ("/rustc/59807616e1fa2540724bfbac14d7976d7e4a3860/library/"
                    "core/src/panicking.rs:233:5")
_DESTRUCTOR = "panic in a destructor during cleanup"


def test_regression_gate_keeps_the_overnight_abort_in_drop_reading(tmp_path: Path) -> None:
    from pipeline_core.application.reconstruct.cargo_runner import (
        test_regression_gate,
    )
    from pipeline_core.domain.test_results import (
        TestStateReading,
        parse_binary_results,
    )
    from tests.test_test_report import _AFTER_245, _BASE_245
    base = TestStateReading(parse_binary_results(_BASE_245))
    fired: list[tuple[str, str]] = []
    # `tmp_path` holds no `.rs`, so the hollow count after is 0 < 1: the
    # closure exemption's shrink condition holds, as on the live `Own::new`
    # fill (the fill turned one stub into a body).
    res = test_regression_gate(
        _const_run_cargo(101, _AFTER_245), "cargo test", tmp_path, {}, base,
        enabled=True, timeout=120,
        guard=lambda loc, m: fired.append((loc, m)), hollow_before=1)
    assert res is None
    assert any(loc == "phase-c/crash_all_red" for loc, _m in fired)
    kept = [m for loc, m in fired if loc == "phase-c/crash_closure_kept"]
    assert kept and "src/error.rs:669" in kept[0]
    assert not any(loc == "phase-c/crash_only_reverted" for loc, _m in fired)
    # NEGATIVE CONTROL — the same reading with the assertion INSIDE the
    # newly-crashed binary is the fill's own and reverts, and the reverted
    # comparison carries no stub evidence for the retro writer.
    own = _AFTER_245.replace(
        "thread 'ctx_a' panicked at src/error.rs:669:67:\nnot implemented\n",
        "thread 'ctx_a' panicked at tests/context_gen_1.rs:9:5:\n"
        "assertion failed: boom\n"
        "thread 'ctx_a' panicked at src/error.rs:669:67:\nnot implemented\n")
    fired2: list[tuple[str, str]] = []
    res2 = test_regression_gate(
        _const_run_cargo(101, own), "cargo test", tmp_path, {}, base,
        enabled=True, timeout=120,
        guard=lambda loc, m: fired2.append((loc, m)), hollow_before=1)
    assert res2 is not None
    assert any(loc == "phase-c/crash_only_reverted" for loc, _m in fired2)
    assert res2[0].crash_stub_sites == frozenset()
    # The stub-only reading with the hollow set NOT shrinking still reverts
    # (the sweep would charge it) — and the comparison it returns carries the
    # sites, so the record earned from it is stamped as the tree's.
    fired3: list[tuple[str, str]] = []
    res3 = test_regression_gate(
        _const_run_cargo(101, _AFTER_245), "cargo test", tmp_path, {}, base,
        enabled=True, timeout=120,
        guard=lambda loc, m: fired3.append((loc, m)), hollow_before=0)
    assert res3 is not None
    assert any(loc == "phase-c/crash_stub_panic_reverted" for loc, _m in fired3)
    assert res3[0].crash_stub_sites == frozenset({("src/error.rs", 669)})


def _vtable_tree(tmp_path: Path) -> tuple[_FakeStructure, Path]:
    """A tree with a still-stubbed `vtable()` in error.rs and the unit under
    test in ptr.rs, wired for the site locator."""
    (tmp_path / "src").mkdir()
    err = tmp_path / "src" / "error.rs"
    err.write_text("fn vtable() {\n    unimplemented!()\n}\n", encoding="utf-8")
    recs = [{"file": "src/error.rs", "fn_name": "vtable", "container": "",
             "sig_byte_start": 0, "body_byte_end": len(err.read_text())}]
    return _FakeStructure(recs), tmp_path


def test_u2_gate_keeps_an_abort_whose_stub_panic_the_runtime_announced(tmp_path: Path) -> None:
    # The scoped run of per-fn `ptr.rs::new`: the aborted binary printed the
    # stub panic in `vtable()` and then the runtime's destructor line. Pre-fix
    # the runtime line vetoed `external_stub_crash_sites` and the gate
    # REJECTED with the undefined-behaviour text (12 `u2_missing_verdict`,
    # 0 `u2_abort_external` overnight).
    structure, src = _vtable_tree(tmp_path)
    out = ("running 2 tests\n"
           "test a ... \n"
           "thread 'a' panicked at src/error.rs:2:5:\nnot implemented\n"
           "\n--- stderr ---\n"
           "     Running tests/t.rs (t/t-1)\n"
           f"thread 'a' panicked at {_RUSTC_PANICKING}:\n{_DESTRUCTOR}\n"
           "thread caused non-unwinding panic. aborting.\n")
    fired: list[tuple[str, str]] = []
    v = _gate(_const_run_cargo(101, out), {"a", "b"},
              baseline=_judged(["a", "b"]),
              unit_fns=frozenset({FnSite("src/ptr.rs", "new", "impl Own<T>")}),
              structure=structure, src_dir=src,
              guard=lambda loc, m: fired.append((loc, m)))
    assert v.decision is U2Decision.ACCEPT_EXTERNAL_ONLY
    assert FnSite("src/error.rs", "vtable", "") in v.blocking_fns
    assert any(loc == "phase-c/u2_abort_external" for loc, _m in fired)


def test_u2_gate_names_the_units_own_first_panic_when_drop_then_aborts(tmp_path: Path) -> None:
    # The residual REJECT: the unit's own assertion came first, the stubbed
    # drop path turned it into an abort. The text blames the first panic and
    # says the abort is the tree's; the verdict carries the abort evidence
    # (the external stub site) for the retro stamp.
    structure, src = _vtable_tree(tmp_path)
    out = ("running 2 tests\n"
           "test a ... \n"
           "thread 'a' panicked at tests/t.rs:9:5:\nassertion failed: boom\n"
           "thread 'a' panicked at src/error.rs:2:5:\nnot implemented\n"
           "\n--- stderr ---\n"
           "     Running tests/t.rs (t/t-1)\n"
           f"thread 'a' panicked at {_RUSTC_PANICKING}:\n{_DESTRUCTOR}\n")
    v = _gate(_const_run_cargo(101, out), {"a", "b"},
              baseline=_judged(["a", "b"]),
              unit_fns=frozenset({FnSite("src/ptr.rs", "new", "impl Own<T>")}),
              structure=structure, src_dir=src)
    assert v.decision is U2Decision.REJECT
    assert v.aborted == frozenset({"a", "b"})
    assert "ABORTED the test process" in v.detail
    assert "tests/t.rs:9: assertion failed: boom" in v.detail
    assert "The abort is the tree's; the first panic is yours" in v.detail
    assert "src/error.rs:2" in v.detail
    assert "undefined behaviour" not in v.detail
    assert v.crash is not None and v.crash.runtime_aborts == 1
    assert v.external_stub_sites == frozenset({("src/error.rs", 2)})
    # A silent abort keeps the undefined-behaviour text and carries no
    # evidence (stamped-empty downstream: the body's own).
    seg = _gate(_const_run_cargo(101, "running 2 tests\ntest a ... \n"
                                      "(signal: 11, SIGSEGV)"),
                {"a", "b"}, baseline=_judged(["a", "b"]))
    assert seg.decision is U2Decision.REJECT
    assert "undefined behaviour" in seg.detail
    assert seg.external_stub_sites == frozenset() and seg.crash is not None
    assert seg.crash.stub_sites == frozenset()


# --- a test binary that HANGS is a reading, not "nothing was measured" ----

_HUNG_AFTER = ("    Running unittests src/lib.rs (t/lib-1)\n"
               "running 2 tests\ntest x ... ok\ntest y ... ok\n"
               "test result: ok. 2 passed; 0 failed; 0 ignored\n"
               "    Running tests/ptr_gen_1.rs (t/ptr_gen_1-1)\n"
               "running 3 tests\n"
               "test test_error_context_display ... FAILED\n"
               "test test_error_debug_chain ... \n"
               "\nTIMEOUT after 120s")


def _two_binary_baseline() -> object:
    from pipeline_core.domain.test_results import (
        TestStateReading,
        parse_binary_results,
    )
    base_out = ("    Running unittests src/lib.rs (t/lib-1)\n"
                "running 2 tests\ntest x ... ok\ntest y ... ok\n"
                "test result: ok. 2 passed; 0 failed; 0 ignored\n"
                "    Running tests/ptr_gen_1.rs (t/ptr_gen_1-1)\n"
                "running 3 tests\ntest test_error_context_display ... FAILED\n"
                "test test_error_debug_chain ... ok\ntest test_z ... ok\n"
                "test result: FAILED. 2 passed; 1 failed; 0 ignored\n"
                "    Running tests/later.rs (t/later-1)\n"
                "running 1 test\ntest later ... ok\n"
                "test result: ok. 1 passed; 0 failed; 0 ignored\n")
    return TestStateReading(parse_binary_results(base_out))


def test_a_timed_out_capture_with_an_open_section_is_a_hung_reading() -> None:
    """Run `3014175-d98ed09e` / `3371694-c8835690` (2026-09-08): the timeout
    cut `cargo test` inside `tests/ptr_gen_1.rs::test_error_debug_chain`;
    every rc -1 path read the capture as "no reading". The capture the
    cargo adapter returns on a timeout is the partial stdout, and its open
    section names the binary that hung and the test in flight."""
    from pipeline_core.application.reconstruct.cargo_runner import measure_test_state
    from pipeline_core.domain.test_results import hung_sections, parse_binary_results
    hung = hung_sections(_HUNG_AFTER)
    assert [(b.label, b.dangling) for b in hung] == [
        ("tests/ptr_gen_1.rs", "test_error_debug_chain")]
    # Only the LAST open section is the hang; an earlier unclosed one is a
    # crash cargo went past. Without `timed_out` nothing is marked hung.
    assert not any(b.hung for b in parse_binary_results(_HUNG_AFTER))
    reading = measure_test_state(_const_run_cargo(-1, _HUNG_AFTER),
                                 "cargo test", Path("."), {})
    assert reading is not None and reading.timed_out
    assert [b.label for b in reading.hung_binaries] == ["tests/ptr_gen_1.rs"]
    # Control (the pre-fix contract, kept): a timeout that cut the BUILD —
    # no section opened — is still inconclusive.
    assert measure_test_state(_const_run_cargo(-1, "   Compiling anyhow v1\n"),
                              "cargo test", Path("."), {}) is None


def test_regression_gate_reverts_a_hang_whatever_the_count() -> None:
    """The hung binary was passing 2 at baseline and the binary after it
    never ran; the comparison names the hang and the gate reverts before
    the crash-only branch (which asks what panicked — a hung binary printed
    nothing). Control: the same baseline against a completed after-reading
    that merely fails the test is a plain newly-red revert, not a hang."""
    from pipeline_core.domain.test_results import compare_readings
    seen: list[tuple[str, str]] = []
    out = _regr(_const_run_cargo(-1, _HUNG_AFTER), _two_binary_baseline(),
                guard=lambda k, m: seen.append((k, m)))
    assert out is not None
    cmp, detail = out
    assert cmp.newly_hung == ("tests/ptr_gen_1.rs",)
    assert cmp.hung_tests == ("tests/ptr_gen_1.rs::test_error_debug_chain",)
    assert cmp.regressed and "hung" in cmp.summary
    assert cmp.not_run == ("tests/later.rs",)
    assert cmp.signature == frozenset({"__binary_hang__:tests/ptr_gen_1.rs"})
    assert "HANG" in detail and "test_error_debug_chain" in detail
    assert [k for k, _ in seen] == ["phase-c/hang_reverted"]
    # A hang with NOTHING passing at baseline in the hung binary is still a
    # regression: it blocks every later binary and every later sweep.
    from pipeline_core.domain.test_results import TestStateReading, parse_binary_results
    all_red = ("    Running unittests src/lib.rs (t/lib-1)\n"
               "running 2 tests\ntest x ... ok\ntest y ... ok\n"
               "test result: ok. 2 passed; 0 failed; 0 ignored\n"
               "    Running tests/ptr_gen_1.rs (t/ptr_gen_1-1)\n"
               "running 1 test\ntest test_error_debug_chain ... FAILED\n"
               "test result: FAILED. 0 passed; 1 failed; 0 ignored\n")
    base_red = TestStateReading(parse_binary_results(all_red))
    after = TestStateReading(parse_binary_results(_HUNG_AFTER, timed_out=True))
    cmp2 = compare_readings(base_red, after)
    assert cmp2.newly_hung and cmp2.regressed and cmp2.crash_lost_pass == 0
    # Control: a completed after-reading with the same test simply FAILED.
    failed_out = _HUNG_AFTER.replace("test test_error_debug_chain ... \n\nTIMEOUT after 120s",
                                     "test test_error_debug_chain ... FAILED\n"
                                     "test result: FAILED. 0 passed; 2 failed; 0 ignored\n")
    out2 = _regr(_const_run_cargo(101, failed_out), _two_binary_baseline())
    assert out2 is not None and not out2[0].newly_hung
    assert "test_error_debug_chain" in out2[0].newly_red


def test_u2_gate_rejects_a_scoped_run_that_hangs_inside_its_own_test() -> None:
    """The scoped run carries the unit's tests and nothing else, so a test
    that never returned is this unit's self failure; the detail says HANG
    so the next attempt changes control flow, not values. Control (the
    pre-fix contract, kept): a timeout that cut the build is still the
    fail-safe accept."""
    from pipeline_core.application.reconstruct.cargo_runner import (
        run_scoped_tests,
        u2_test_gate,
    )
    from pipeline_core.domain.test_report import U2Decision
    seen: list[tuple[str, str]] = []
    v = u2_test_gate(
        _const_run_cargo(-1, _HUNG_AFTER), {"test_error_debug_chain"},
        frozenset(), "cargo test", Path("."), {}, structure=None, enabled=True,
        timeout=120, err_cap=4000, guard=lambda k, m: seen.append((k, m)))
    assert v.decision is U2Decision.REJECT
    assert v.self_failures == frozenset({"test_error_debug_chain"})
    assert "HANG" in v.detail and "120s" in v.detail
    assert [k for k, _ in seen] == ["phase-c/u2_hang"]
    scoped = run_scoped_tests(_const_run_cargo(-1, _HUNG_AFTER), "cargo test",
                              Path("."), {}, timeout=120, err_cap=4000)
    assert not scoped.passed and scoped.failed == {"test_error_debug_chain"}
    assert "HANG" in scoped.detail
    build_cut = u2_test_gate(
        _const_run_cargo(-1, "   Compiling anyhow v1\n"), {"test_error_debug_chain"},
        frozenset(), "cargo test", Path("."), {}, structure=None, enabled=True,
        timeout=120, err_cap=4000, guard=lambda *_: None)
    assert build_cut.decision is U2Decision.ACCEPT


# --- a silent death under test isolation is a destroyed verdict -------------

def _isolated(name: str, block: list[str], *, completed: bool = True) -> str:
    """A scoped capture after test isolation: `name` failed and its block is
    `block` (the isolation rewrite of an aborted test opens with
    ABORT_MARK)."""
    return "\n".join([
        "running 1 test", f"test {name} ... FAILED", "", "failures:", "",
        f"---- {name} stdout ----", *block, "",
        "failures:", f"    {name}", "",
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; "
        "0 filtered out"])


def _iso_reading(capture: str):
    from pipeline_core.domain.test_results import (
        TestStateReading,
        parse_binary_results,
    )
    return TestStateReading(parse_binary_results(capture))


_STUB_RED = ["thread 'rec' panicked at src/a.rs:3:5:", "not implemented"]


def test_u2_gate_charges_a_silent_isolated_abort_of_a_test_judged_red() -> None:
    """Run `2157238-210637e6` (2026-10-11): a file-route reply wrote
    `construct_from_*` as calls to themselves. The attributed tests were red
    at baseline (stub panic) and, after the splice, died of stack overflow
    with no panic. Test isolation rebuilds that death as a FAILED entry, so
    nothing is missing and the abort branch never fired; the overflow read
    as an ordinary self failure and the keep took it as non-regression.

    NEGATIVE CONTROL: the destroyed set as read before (missing names only)
    is empty on the same capture, and the outcome model without the abort
    clause does not refuse."""
    from pipeline_core.domain.test_report import judge_refill
    from pipeline_core.domain.test_results import ABORT_MARK
    baseline = _iso_reading(_isolated("rec", _STUB_RED))
    assert "rec" in baseline.failed_names and not baseline.aborted_names
    after = _isolated("rec", [ABORT_MARK, "",
                              "thread 'rec' has overflowed its stack",
                              "fatal runtime error: stack overflow"])
    assert _iso_reading(after).aborted_silent_names == {"rec"}
    v = _gate(_const_run_cargo(101, after), {"rec"}, baseline=baseline)
    assert v.decision is U2Decision.REJECT
    assert v.aborted == frozenset({"rec"})
    ji = judge_refill({"rec"}, baseline, v)
    assert ji.aborted == frozenset({"rec"}) and not ji.non_regression

    from pipeline_core.domain.test_report import (
        parse_test_output,
        self_aborted_tests,
    )
    report = parse_test_output(after)
    missing = frozenset({"rec"} - (report.passed
                                   | {f.test_name for f in report.failures}))
    assert self_aborted_tests(missing, baseline.judged_names) == frozenset(), (
        "pre-fix: the isolated death is reported, so nothing is missing")
    from pipeline_core.domain.assertion_position import TestOutcomes
    assert not TestOutcomes(lost=ji.new_failures).refuses, (
        "pre-fix: red before and after is non-regression")


def test_judge_refill_refuses_a_silent_death_even_from_an_accept_capture() -> None:
    """The keep reads the capture itself, not only what the gate named: a
    verdict whose capture shows a silent death of an attributed test that
    the baseline ran to its own verdict refuses."""
    from pipeline_core.domain.test_report import U2Verdict, judge_refill
    from pipeline_core.domain.test_results import ABORT_MARK
    baseline = _iso_reading(_isolated("rec", _STUB_RED))
    after = _isolated("rec", [ABORT_MARK, "", "fatal runtime error: stack overflow"])
    v = U2Verdict(U2Decision.REJECT, self_failures=frozenset({"rec"}),
                  capture=after)
    assert not judge_refill({"rec"}, baseline, v).non_regression


def test_an_abort_that_prints_a_stub_panic_is_not_a_native_fault() -> None:
    """A test whose process died after a stub's `unimplemented!()` fired
    while unwinding printed that panic: not silent, not `aborted`, and the
    keep judges it as before (red both sides: non-regression)."""
    from pipeline_core.domain.test_report import U2Verdict, judge_refill
    from pipeline_core.domain.test_results import ABORT_MARK
    baseline = _iso_reading(_isolated("rec", _STUB_RED))
    after = _isolated("rec", [ABORT_MARK,
                              "thread 'rec' panicked at src/a.rs:9:5:",
                              "not implemented",
                              "thread 'rec' panicked at src/a.rs:12:5:",
                              "not implemented"])
    assert not _iso_reading(after).aborted_silent_names
    v = U2Verdict(U2Decision.REJECT, self_failures=frozenset({"rec"}),
                  capture=after)
    ji = judge_refill({"rec"}, baseline, v)
    assert not ji.aborted and ji.non_regression


def test_a_silent_death_is_not_cleared_by_a_stub_panic_in_another_section(
        tmp_path: Path) -> None:
    """One capture holds both shapes isolation can leave side by side: a
    completed silent death of a test the baseline judged, and an incomplete
    section whose only panic is another unit's stub. The silent death stays
    a reject. Negative control: the same stub section with no silent death
    is still the external-stub keep."""
    from pipeline_core.domain.test_results import ABORT_MARK
    structure, src = _vtable_tree(tmp_path)
    stub = ("running 1 test\n"
            "test other ... \n"
            "thread 'other' panicked at src/error.rs:2:5:\n"
            "not implemented\n")
    silent = ("running 1 test\n"
              "test rec ... FAILED\n\n"
              "failures:\n\n"
              "---- rec stdout ----\n"
              f"{ABORT_MARK}\n\n"
              "thread 'rec' has overflowed its stack\n"
              "fatal runtime error: stack overflow\n\n"
              "failures:\n"
              "    rec\n\n"
              "test result: FAILED. 0 passed; 1 failed; 0 ignored; "
              "0 measured; 0 filtered out\n")
    stderr = ("\n--- stderr ---\n"
              "     Running tests/stub.rs (t/stub-1)\n"
              "     Running tests/rec.rs (t/rec-1)\n")
    mixed = stub + silent + stderr
    unit = frozenset({FnSite("src/ptr.rs", "new", "impl Own<T>")})
    baseline = _judged(["rec", "other"])
    v = _gate(_const_run_cargo(101, mixed), {"rec", "other"},
              baseline=baseline, unit_fns=unit,
              structure=structure, src_dir=src)
    assert v.decision is U2Decision.REJECT
    assert "rec" in v.aborted
    assert v.decision is not U2Decision.ACCEPT_EXTERNAL_ONLY
    only_stub = (stub
                 + "\n--- stderr ---\n"
                 + "     Running tests/stub.rs (t/stub-1)\n")
    kept = _gate(_const_run_cargo(101, only_stub), {"other"},
                 baseline=_judged(["other"]), unit_fns=unit,
                 structure=structure, src_dir=src)
    assert kept.decision is U2Decision.ACCEPT_EXTERNAL_ONLY


def test_a_test_already_dead_at_baseline_is_not_charged_again() -> None:
    from pipeline_core.domain.test_report import U2Verdict, judge_refill
    from pipeline_core.domain.test_results import ABORT_MARK
    dead = _isolated("rec", [ABORT_MARK, "", "fatal runtime error: stack overflow"])
    baseline = _iso_reading(dead)
    v = U2Verdict(U2Decision.REJECT, self_failures=frozenset({"rec"}),
                  capture=dead)
    assert judge_refill({"rec"}, baseline, v).non_regression

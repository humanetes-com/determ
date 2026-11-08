"""The keep gate matches a printed libtest name to a held (frontier) name by
leaf, and a cfg twin's untouched arm is a still-stubbed sibling body.

Found on the from-scratch hardcrate run `3669371-9d5a7361` (2026-11-06): the
frontier holds leaves (`counter_bumps`), libtest prints a test inside a module
with its path (`counter::gen_tests_1::counter_bumps`), and `u2_test_gate`
compared the two as strings. Six fill-1 keeps whose own inline-module test
FAILED were logged `reported no verdict in the scoped run (binary crashed)`
and kept unjudged. Each test below carries the pre-fix comparison as its
negative control.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from pipeline_core.application.reconstruct import cargo_runner
from pipeline_core.application.reconstruct.attribution import (
    make_untouched_arm_probe,
)
from pipeline_core.application.reconstruct.cargo_runner import u2_test_gate
from pipeline_core.domain.attribution import WriteGround
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.test_report import (
    HeldTests,
    U2Decision,
    UnitScope,
    judge_refill,
    parse_test_output,
    partition_blame,
    self_aborted_tests,
)
from pipeline_core.domain.test_results import TestStateReading, parse_binary_results

QUALIFIED = "counter::gen_tests_1::counter_bumps"
LEAF = "counter_bumps"
UNIT = FnSite("src/counter.rs", "bump", "")


class _Structure:
    def __init__(self, recs=None):
        self._recs = recs or []

    def enumerate_fns(self, src_dir):
        return self._recs


def _section(binary: str, name: str, *, ok: bool, left: str = "0") -> str:
    head = f"     Running {binary} (target/debug/deps/x-abc)\n\nrunning 1 test\n"
    if ok:
        return head + (f"test {name} ... ok\n\n"
                       "test result: ok. 1 passed; 0 failed; 0 ignored; "
                       "0 measured; 0 filtered out\n\n")
    return head + (
        f"test {name} ... FAILED\n\nfailures:\n\n---- {name} stdout ----\n"
        f"thread '{name}' panicked at src/counter.gen_tests_1.rs:10:5:\n"
        f"assertion `left == right` failed\n  left: {left}\n right: 2\n\n"
        f"failures:\n    {name}\n\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; "
        "0 filtered out\n\n")


def _gate(capture: str, held: set[str], *, baseline=None, rc: int = 101,
          structure=None, src_dir: Path = Path(".")):
    fired: list[tuple[str, str]] = []
    ground = WriteGround(frozenset(held), frozenset(held))
    v = u2_test_gate(
        lambda cmd, d, env, timeout=None: (rc, capture), ground,
        frozenset({UNIT}), "cargo test", src_dir, {},
        structure=structure or _Structure(), enabled=True, timeout=60,
        err_cap=4000, guard=lambda k, m: fired.append((k, m)),
        baseline=baseline)
    return v, fired


def test_a_module_test_that_failed_in_the_body_rejects_it() -> None:
    capture = _section("unittests src/lib.rs", QUALIFIED, ok=False)
    v, fired = _gate(capture, {LEAF})
    assert v.decision is U2Decision.REJECT
    assert v.self_failures == frozenset({LEAF})
    assert not [k for k, _m in fired if k == "phase-c/u2_missing_verdict"]
    # Control: the pre-fix comparison (`f.test_name in fn_tests`) on the same
    # capture drops the failure, which is how the gate read "no failures".
    report = parse_test_output(capture)
    assert [f for f in report.failures if f.test_name in {LEAF}] == []
    assert HeldTests(frozenset({LEAF})).failures_in(report)


def test_a_vanished_module_test_the_baseline_judged_is_charged() -> None:
    baseline = TestStateReading(parse_binary_results(
        _section("unittests src/lib.rs", QUALIFIED, ok=True)))
    assert baseline.judged_names == frozenset({QUALIFIED})
    died = ("     Running unittests src/lib.rs (target/debug/deps/x-abc)\n\n"
            "running 1 test\n"
            "error: test failed, to rerun pass `--lib`\n\nCaused by:\n"
            "  process didn't exit successfully (signal: 6, SIGABRT)\n")
    v, _fired = _gate(died, {LEAF}, baseline=baseline)
    assert v.decision is U2Decision.REJECT
    assert v.aborted == frozenset({LEAF})
    assert self_aborted_tests(frozenset({LEAF}),
                              baseline.judged_names) == frozenset({LEAF})
    # Control: the pre-fix string intersection charged nothing.
    assert frozenset({LEAF}) & baseline.judged_names == frozenset()


def test_one_binary_two_modules_the_other_module_failing_convicts() -> None:
    # One binary, one leaf: one `TestKey`, so the failure is the test's.
    capture = ("     Running unittests src/lib.rs (target/debug/deps/x-abc)\n\n"
               "running 2 tests\n"
               f"test a::gen_tests_1::{LEAF} ... ok\n"
               + _section("", f"b::gen_tests_1::{LEAF}", ok=False)
               .split("running 1 test\n", 1)[1])
    v, _fired = _gate(capture, {LEAF})
    assert v.decision is U2Decision.REJECT


def test_two_binaries_the_other_copy_red_before_and_after_banks() -> None:
    before = (_section("unittests src/lib.rs", QUALIFIED, ok=False)
              + _section("tests/counter.rs", LEAF, ok=False, left="1"))
    after = (_section("unittests src/lib.rs", QUALIFIED, ok=True)
             + _section("tests/counter.rs", LEAF, ok=False, left="1"))
    baseline = TestStateReading(parse_binary_results(before))
    v, _fired = _gate(after, {LEAF}, baseline=baseline)
    # The gate names the leaf red (the other binary's copy).
    assert v.decision is U2Decision.REJECT
    jr = judge_refill({LEAF}, baseline, v)
    assert jr.banks
    # Control: the same keep where the other copy was GREEN before it is a
    # loss, and does not bank.
    before_green = (_section("unittests src/lib.rs", QUALIFIED, ok=False)
                    + _section("tests/counter.rs", LEAF, ok=True))
    lost = judge_refill(
        {LEAF}, TestStateReading(parse_binary_results(before_green)), v)
    assert not lost.banks


def _twin(tmp_path: Path, *, other_arm_stub: bool):
    src = tmp_path / "src"
    src.mkdir()
    a = "#[cfg(x)]\npub fn cap() -> usize {\n    255\n}\n"
    b = ("#[cfg(not(x))]\npub fn cap() -> usize {\n"
         + ("    unimplemented!()\n" if other_arm_stub else "    64\n") + "}\n")
    (src / "lib.rs").write_text(a + b, encoding="utf-8")
    recs = [
        {"file": "src/lib.rs", "fn_name": "cap", "container": "",
         "sig_byte_start": 10, "body_byte_end": len(a) - 1,
         "has_unimplemented_body": False},
        {"file": "src/lib.rs", "fn_name": "cap", "container": "",
         "sig_byte_start": len(a) + 15, "body_byte_end": len(a + b) - 1,
         "has_unimplemented_body": other_arm_stub},
    ]
    return _Structure(recs), src


def test_a_twin_arm_still_stubbed_is_a_sibling_not_the_written_arm(
        tmp_path: Path) -> None:
    structure, _src = _twin(tmp_path, other_arm_stub=True)
    probe = make_untouched_arm_probe(structure, tmp_path)
    assert probe("src/lib.rs", 7) is True       # the stubbed arm
    assert probe("src/lib.rs", 3) is False      # the written arm
    capture = ("test gen_tests_1::default_cap ... FAILED\n\n"
               "---- gen_tests_1::default_cap stdout ----\n"
               "thread 'gen_tests_1::default_cap' panicked at src/lib.rs:7:5:\n"
               "not implemented\n")
    site = FnSite("src/lib.rs", "cap", "")
    report = parse_test_output(capture)

    def locate(_f: str, _l: int) -> FnSite:
        return site

    scoped = partition_blame(report, UnitScope(frozenset({site}), probe), locate)
    assert not scoped.self_failures and scoped.external
    # Control: the bare site set (the pre-fix partition) makes the untouched
    # arm's stub this write's own failure.
    bare = partition_blame(report, frozenset({site}), locate)
    assert bare.self_failures and not bare.external


def _world(world: str, where: str, msg: str) -> str:
    name = "gen_tests_1::checks"
    return (
        f"=== cfg world: {world} ===\n"
        "     Running unittests src/lib.rs (target/debug/deps/x-abc)\n\n"
        "running 1 test\n"
        f"test {name} ... FAILED\n\n"
        "failures:\n\n"
        f"---- {name} stdout ----\n"
        f"thread '{name}' panicked at {where}:\n"
        f"{msg}\n\n"
        "failures:\n"
        f"    {name}\n\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; "
        "0 filtered out\n\n")


def test_a_stub_world_does_not_hide_the_other_worlds_assertion(
        tmp_path: Path) -> None:
    # The home world is joined first. Its section is the primary, and the
    # other world's section of the same name is `others`. A partition that
    # reads only the primary keeps the body when the home world is the
    # untouched arm, and the written world's assertion never convicts.
    structure, _src = _twin(tmp_path, other_arm_stub=True)
    site = FnSite("src/lib.rs", "cap", "")
    leaf = "checks"
    stub = _world("stable", "src/lib.rs:7:5", "not implemented")
    assertion = _world(
        "extra", "src/gen_tests_1.rs:4:5",
        "assertion `left == right` failed\n  left: 1\n right: 2")

    def drive(capture: str):
        return u2_test_gate(
            lambda cmd, d, env, timeout=None: (101, capture),
            WriteGround(frozenset({leaf}), frozenset({leaf})),
            frozenset({site}), "cargo test", tmp_path, {},
            structure=structure, enabled=True, timeout=60, err_cap=4000,
            guard=lambda _k, _m: None)

    for capture in (stub + assertion, assertion + stub):
        v = drive(capture)
        assert v.decision is U2Decision.REJECT
        assert v.self_failures == frozenset({leaf})
        assert "left: 1" in v.detail
    # Control: the stub section alone is the untouched arm. The keep stands.
    alone = drive(stub)
    assert alone.decision is U2Decision.ACCEPT_EXTERNAL_ONLY
    assert not alone.self_failures


def test_a_single_definition_stub_stays_the_units_own(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    text = "pub fn cap() -> usize {\n    unimplemented!()\n}\n"
    (src / "lib.rs").write_text(text, encoding="utf-8")
    structure = _Structure([{
        "file": "src/lib.rs", "fn_name": "cap", "container": "",
        "sig_byte_start": 0, "body_byte_end": len(text) - 1,
        "has_unimplemented_body": True}])
    assert make_untouched_arm_probe(structure, tmp_path)("src/lib.rs", 2) is False


_RAW_SETS = {"fn_tests", "convicting", "judged"}


def _forbidden(node: ast.AST) -> str | None:
    """A comparison between a printed name and a raw name set."""
    if isinstance(node, ast.Compare) and any(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
        left = node.left
        if isinstance(left, ast.Attribute) and left.attr == "test_name":
            return "`<failure>.test_name in …`"
        for c in node.comparators:
            if isinstance(c, ast.Attribute) and c.attr in {"judged", "convicting"}:
                return "`… in ground.judged/convicting`"
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitAnd, ast.Sub)):
        for side in (node.left, node.right):
            if isinstance(side, ast.Attribute) and side.attr in {
                    "passed", "judged", "convicting", "judged_names"}:
                return f"set operation on `.{side.attr}`"
            if isinstance(side, ast.Name) and side.id in _RAW_SETS:
                return f"set operation on `{side.id}`"
    return None


def test_the_keep_gate_compares_printed_names_only_through_held_tests() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(cargo_runner.u2_test_gate)))
    hits = [(n.lineno, why) for n in ast.walk(tree)
            if (why := _forbidden(n)) is not None]
    assert hits == [], hits
    # Control: the pre-fix filter is caught by the same scan.
    old = ast.parse("x = tuple(f for f in r.failures if f.test_name in fn_tests)\n"
                    "y = report.passed & fn_tests\n")
    assert len([n for n in ast.walk(old) if _forbidden(n)]) == 2


# ---------------------------------------------------------------------------
# Run `79280-8bd7cb71` (hardcrate, fill 1, 2026-11-06): the ground held one
# lib test under two spellings, bare and module-qualified. Every judged lib
# binary completed, and all seven `u2_missing_verdict … (binary crashed)`
# guards of the fill named the spelling `held_as` did not pick.
# ---------------------------------------------------------------------------


def _pre_fix_held_of(held: HeldTests, printed) -> frozenset[str]:
    """The match as shipped before the fix: one held spelling per printed
    name (`held_as`'s first in sorted order)."""
    out = (held.held_as(p) for p in printed)
    return frozenset(n for n in out if n is not None)


def test_two_spellings_of_one_test_are_both_reported() -> None:
    capture = _section("unittests src/lib.rs", QUALIFIED, ok=False)
    v, fired = _gate(capture, {LEAF, QUALIFIED})
    assert v.decision is U2Decision.REJECT
    assert not [k for k, _m in fired if k == "phase-c/u2_missing_verdict"]
    held = HeldTests(frozenset({LEAF, QUALIFIED}))
    assert held.missing_from([QUALIFIED]) == frozenset()
    # Control: the pre-fix match picks `counter::…` (':' sorts before '_')
    # and leaves the bare spelling unreported — the live guard's name.
    assert held.names - _pre_fix_held_of(held, [QUALIFIED]) == frozenset({LEAF})


def test_two_spellings_with_a_baseline_are_not_charged_as_an_abort() -> None:
    baseline = TestStateReading(parse_binary_results(
        _section("unittests src/lib.rs", QUALIFIED, ok=False, left="9")))
    after = _section("unittests src/lib.rs", QUALIFIED, ok=False)
    v, fired = _gate(after, {LEAF, QUALIFIED}, baseline=baseline)
    assert v.decision is U2Decision.REJECT
    assert v.aborted == frozenset()
    assert not [k for k, _m in fired if k == "phase-c/u2_missing_verdict"]
    # Control: the pre-fix missing set, met with the baseline's judged names,
    # is the abort charge the live fill logged as a crash-evidence keep.
    held = HeldTests(frozenset({LEAF, QUALIFIED}))
    missing_pre = held.names - _pre_fix_held_of(held, [QUALIFIED])
    assert self_aborted_tests(missing_pre, baseline.judged_names) == {LEAF}


def _one_binary(a_ok: bool, b_ok: bool, *, b_left: str = "0") -> str:
    def _row(name: str, ok: bool) -> str:
        return f"test m::gen_tests_1::{name} ... {'ok' if ok else 'FAILED'}\n"

    def _body(name: str, left: str) -> str:
        return (f"---- m::gen_tests_1::{name} stdout ----\n"
                f"thread 'm::gen_tests_1::{name}' panicked at "
                "src/counter.gen_tests_1.rs:10:5:\n"
                f"assertion `left == right` failed\n  left: {left}\n"
                " right: 2\n\n")
    fails = [n for n, ok in (("a_case", a_ok), ("b_case", b_ok)) if not ok]
    out = ("     Running unittests src/lib.rs (target/debug/deps/x-abc)\n\n"
           "running 2 tests\n" + _row("a_case", a_ok) + _row("b_case", b_ok))
    if fails:
        out += ("\nfailures:\n\n"
                + "".join(_body(n, b_left if n == "b_case" else "0")
                          for n in fails)
                + "failures:\n" + "".join(f"    m::gen_tests_1::{n}\n"
                                          for n in fails))
    passed = 2 - len(fails)
    return out + (f"\ntest result: {'FAILED' if fails else 'ok'}. {passed} "
                  f"passed; {len(fails)} failed; 0 ignored; 0 measured; "
                  "0 filtered out\n\n")


def test_per_fn_plain_reject_with_partial_gain_goes_to_the_next_attempt() -> None:
    from pipeline_core.domain.test_report import per_fn_reject_kept
    held = {"a_case", "b_case"}
    base = TestStateReading(parse_binary_results(_one_binary(False, False)))
    v, _fired = _gate(_one_binary(True, False, b_left="7"), held,
                      baseline=base)
    assert v.decision is U2Decision.REJECT
    assert v.self_failures == frozenset({"b_case"})
    jr = judge_refill(held, base, v)
    assert jr.banks  # a_case turned green and nothing refuses
    # The plan's claim: a body that fails its own test on an assertion is
    # retried in the fill, not kept, when the attempt was not crash-graded
    # and nothing aborted.
    assert not per_fn_reject_kept(crash_graded=False, aborted=False, judged=jr)
    # Control: the rule shipped earlier the same day (keep on `banks`) kept it.
    assert jr.banks is True
    # The two shapes the rule does keep.
    assert per_fn_reject_kept(crash_graded=True, aborted=False, judged=jr)
    assert per_fn_reject_kept(crash_graded=False, aborted=True, judged=jr)
    assert not per_fn_reject_kept(crash_graded=True, aborted=True, judged=None)

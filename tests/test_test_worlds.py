"""A generated test's gate is the worlds it was measured to pass in.

Run `292191-d60a5308` (2026-10-02), anyhow workspace: the rule this replaced
predicted the gate from the test's text and gated 26 of 26 files written
under the default cluster, 24 of them because their text holds the word
`fmt`; with the gate removed 25 of the 26 build and pass in the other
cluster. Invented worlds and bodies only.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline_core.application.reconstruct.cluster_membership import cluster_cfg_attr
from pipeline_core.application.test_worlds import (
    TestWorldsDeps,
    gate_line,
    own_world_gate,
    settle_test_gate,
)
from pipeline_core.domain.cfg_worlds import gate_for_worlds

_BASE = SimpleNamespace(name="base", is_default=True, active_cfg=[])
_ALT = SimpleNamespace(name="alt", is_default=False, active_cfg=["alt"])
_ZED = SimpleNamespace(name="zed", is_default=False, active_cfg=["zed"])

_BODY = "// generated\nuse std::fmt;\n#[test]\nfn sized() { assert_eq!(1, 1); }\n"


def _deps(clusters: list[SimpleNamespace], passing: set[str],
          log: list[str] | None = None, asked: list[str] | None = None,
          boom: str = "") -> TestWorldsDeps:
    """`passing` names every world the test passes in — its own included."""
    def _passes(_p: Path, _m: str, _bin: str, _block: str, c: SimpleNamespace) -> bool:
        if asked is not None:
            asked.append(c.name)
        if c.name == boom:
            raise RuntimeError("cargo missing")
        return c.name in passing

    sink = log if log is not None else []
    return TestWorldsDeps(clusters=lambda: clusters,
                          world_gate=lambda c: cluster_cfg_attr(c, clusters),
                          passes_in=_passes, log=sink.append,
                          world_key=lambda c: getattr(c, "name", None))


def _written(tmp_path: Path, generating: SimpleNamespace,
             deps: TestWorldsDeps) -> Path:
    f = tmp_path / "size_gen_1.rs"
    f.write_text(gate_line(own_world_gate(generating, deps)) + _BODY)
    return f


# --- the rendering ----------------------------------------------------------

_ATOMS = {"base": "not(any(alt, zed))", "alt": "alt", "zed": "zed"}


def test_every_world_is_no_gate_and_a_subset_is_the_any_of_its_atoms() -> None:
    assert gate_for_worlds({"base", "alt", "zed"}, _ATOMS) == ""
    assert gate_for_worlds({"alt"}, _ATOMS) == "alt"
    assert gate_for_worlds({"zed", "alt"}, _ATOMS) == "any(alt, zed)"
    assert gate_for_worlds({"base", "zed"}, _ATOMS) == "any(not(any(alt, zed)), zed)"


def test_a_crate_with_one_world_has_no_gate_and_no_world_is_refused() -> None:
    assert gate_for_worlds({"only"}, {"only": ""}) == ""
    with pytest.raises(ValueError):
        gate_for_worlds(set(), _ATOMS)
    with pytest.raises(ValueError):
        gate_for_worlds({"unknown"}, _ATOMS)


# --- the measurement --------------------------------------------------------

def test_a_test_that_passes_in_every_world_ends_ungated(tmp_path: Path) -> None:
    """The plan's sentence. NEGATIVE CONTROL: the rule this replaced asked
    whether the text names a fn some world compiles out; `fmt` was such a
    name on the recorded tree, this body holds the word, and it gated."""
    clusters = [_BASE, _ALT]
    deps = _deps(clusters, passing={"base", "alt"})
    f = _written(tmp_path, _BASE, deps)
    assert f.read_text().startswith("#![cfg(not(any(alt, feature = \"alt\")))]\n")
    assert settle_test_gate(tmp_path, "m", f, _BODY, _BASE, deps) == ""
    assert f.read_text() == _BODY

    def _old_rule(body: str, names: frozenset[str]) -> bool:
        import re
        return any(re.search(rf"\b{re.escape(n)}\b", body) for n in names)
    assert _old_rule(_BODY, frozenset({"fmt", "source"})) is True


def test_a_test_written_under_a_non_default_world_is_shared_back(tmp_path: Path) -> None:
    clusters = [_BASE, _ALT]
    deps = _deps(clusters, passing={"base", "alt"})
    f = _written(tmp_path, _ALT, deps)
    assert f.read_text().startswith('#![cfg(any(alt, feature = "alt"))]\n')
    assert settle_test_gate(tmp_path, "m", f, _BODY, _ALT, deps) == ""
    assert f.read_text() == _BODY


def test_a_test_that_passes_nowhere_else_keeps_its_world(tmp_path: Path) -> None:
    clusters = [_BASE, _ALT]
    log: list[str] = []
    deps = _deps(clusters, passing={"alt"}, log=log)
    f = _written(tmp_path, _ALT, deps)
    before = f.read_text()
    assert settle_test_gate(tmp_path, "m", f, _BODY, _ALT, deps) == \
        'any(alt, feature = "alt")'
    assert f.read_text() == before
    assert "passes in no other world" in log[-1]


def test_a_proper_subset_of_three_worlds_is_gated_to_exactly_it(tmp_path: Path) -> None:
    clusters = [_BASE, _ALT, _ZED]
    asked: list[str] = []
    deps = _deps(clusters, passing={"alt", "zed"}, asked=asked)
    f = _written(tmp_path, _ALT, deps)
    gate = settle_test_gate(tmp_path, "m", f, _BODY, _ALT, deps)
    assert gate == 'any(any(alt, feature = "alt"), any(zed, feature = "zed"))'
    assert f.read_text() == f"#![cfg({gate})]\n" + _BODY
    assert asked == ["alt", "base", "zed"]   # its own world first, then the others


def test_the_other_worlds_see_the_file_without_its_gate(tmp_path: Path) -> None:
    clusters = [_BASE, _ALT]
    seen: list[str] = []
    f = tmp_path / "size_gen_1.rs"

    def _passes(_p: Path, _m: str, _bin: str, _block: str, c: SimpleNamespace) -> bool:
        seen.append(f.read_text())
        return c.name == "base"

    deps = TestWorldsDeps(clusters=lambda: clusters,
                          world_gate=lambda c: cluster_cfg_attr(c, clusters),
                          passes_in=_passes, log=lambda _m: None,
                          world_key=lambda c: getattr(c, "name", None))
    f.write_text(gate_line(own_world_gate(_BASE, deps)) + _BODY)
    as_written = f.read_text()
    settle_test_gate(tmp_path, "m", f, _BODY, _BASE, deps)
    # its own world runs it as written; the other world runs it ungated
    assert seen == [as_written, _BODY]


def test_a_world_that_gives_no_reading_is_not_shared_and_the_file_stays_valid(
        tmp_path: Path) -> None:
    clusters = [_BASE, _ALT, _ZED]
    log: list[str] = []
    deps = _deps(clusters, passing={"base", "zed"}, log=log, boom="alt")
    f = _written(tmp_path, _BASE, deps)
    gate = settle_test_gate(tmp_path, "m", f, _BODY, _BASE, deps)
    assert gate == ('any(not(any(alt, feature = "alt", zed, feature = "zed")), '
                    'any(zed, feature = "zed"))')
    assert f.read_text() == f"#![cfg({gate})]\n" + _BODY
    assert any("no reading in world 'alt'" in line for line in log)


def test_a_crate_with_one_world_is_still_run_in_it(tmp_path: Path) -> None:
    asked: list[str] = []
    deps = _deps([_BASE], passing={"base"}, asked=asked)
    f = _written(tmp_path, _BASE, deps)
    assert settle_test_gate(tmp_path, "m", f, _BODY, _BASE, deps) == ""
    assert asked == ["base"] and f.read_text() == _BODY
    assert settle_test_gate(tmp_path, "m", f, _BODY, _BASE,
                            _deps([_BASE], passing=set())) is None


def test_a_test_that_fails_in_its_own_world_has_no_world(tmp_path: Path) -> None:
    """The plan's sentence, and the hole it closes: a block that fails under
    the world that generated it and passes under another comes back with no
    gate to write — None — the file exactly as found, and no other world
    asked. NEGATIVE CONTROL: taking the generating world on the caller's word
    (the set started as `{generating}`) rendered this as ungated."""
    clusters = [_BASE, _ALT]
    asked: list[str] = []
    log: list[str] = []
    deps = _deps(clusters, passing={"alt"}, asked=asked, log=log)
    f = _written(tmp_path, _BASE, deps)
    before = f.read_text()
    assert settle_test_gate(tmp_path, "m", f, _BODY, _BASE, deps) is None
    assert f.read_text() == before and asked == ["base"]
    assert "does not pass in the world that generated it" in log[-1]
    from pipeline_core.domain.cfg_worlds import gate_for_worlds as _render
    atoms = {c.name: cluster_cfg_attr(c, clusters) for c in clusters}
    assert _render({"base"} | {"alt"}, atoms) == ""      # the pre-fix outcome


def test_no_reading_in_its_own_world_is_not_a_pass(tmp_path: Path) -> None:
    clusters = [_BASE, _ALT]
    deps = _deps(clusters, passing={"base", "alt"}, boom="base")
    f = _written(tmp_path, _BASE, deps)
    before = f.read_text()
    assert settle_test_gate(tmp_path, "m", f, _BODY, _BASE, deps) is None
    assert f.read_text() == before


def test_a_file_not_in_the_written_shape_is_left_alone(tmp_path: Path) -> None:
    clusters = [_BASE, _ALT]
    asked: list[str] = []
    deps = _deps(clusters, passing={"base", "alt"}, asked=asked)
    f = tmp_path / "size_gen_1.rs"
    f.write_text(_BODY)                      # no own-world gate on line 1
    assert settle_test_gate(tmp_path, "m", f, _BODY, _BASE, deps) == \
        'not(any(alt, feature = "alt"))'
    assert asked == ["base"] and f.read_text() == _BODY


def test_a_world_with_the_same_env_as_the_generating_one_is_not_a_reading(
        tmp_path: Path) -> None:
    """Two clusters whose cargo runs are identical cannot be told apart by
    running: a pass under one is the generating run again. The test stays out
    of that world; a world that does differ is still measured."""
    clusters = [_BASE, _ALT, _ZED]
    asked: list[str] = []
    log: list[str] = []
    base = _deps(clusters, passing={"base", "alt", "zed"}, asked=asked, log=log)
    import dataclasses
    deps = dataclasses.replace(
        base, world_key=lambda c: (None if c is None else
                                  "same" if c.name in ("base", "alt") else c.name))
    f = _written(tmp_path, _BASE, deps)
    gate = settle_test_gate(tmp_path, "m", f, _BODY, _BASE, deps)
    assert asked == ["base", "zed"]
    assert gate == ('any(not(any(alt, feature = "alt", zed, feature = "zed")), '
                    'any(zed, feature = "zed"))')
    assert any("same env and toolchain" in line for line in log)


# --- every write site drops a test that has no world --------------------------

def test_every_settle_call_in_the_augmentation_engine_handles_no_world() -> None:
    """`settle_test_gate` returns None for a test that does not pass where it
    was written. A call site that ignores the result, or tests it and then
    does nothing, keeps that test in the suite — the quick-retry site did the
    first with the block it had only compiled. Every call in `task0c` must be
    the condition of an `if … is None:` whose body either reverts the file
    or returns a failed attempt to the fix loop."""
    import ast

    src = (Path(__file__).resolve().parents[1] / "pipeline_core" / "application"
           / "tasks" / "task0c.py").read_text()
    assert _unhandled_settle_calls(src) == []
    assert len(_settle_calls(ast.parse(src))) >= 2, (
        "the fix-loop admission and the quick retry both settle")


def _settle_calls(tree):  # noqa: ANN001, ANN202
    import ast
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "_settle_test_gate"]


def _unhandled_settle_calls(src: str) -> list[int]:
    import ast

    tree = ast.parse(src)
    calls = _settle_calls(tree)
    handled: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Is)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None and test.left in calls):
            continue
        acts = False
        for inner in node.body:
            for sub in ast.walk(inner):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_revert_gen_tests_pass"):
                    acts = True
                if (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Call)
                        and isinstance(sub.value.func, ast.Name)
                        and sub.value.func.id == "FixOutcome" and sub.value.args
                        and isinstance(sub.value.args[0], ast.Constant)
                        and sub.value.args[0].value == "failed"):
                    acts = True
        if acts:
            handled.add(id(test.left))
    return [c.lineno for c in calls if id(c) not in handled]


def test_the_settle_owner_walk_catches_an_ignored_result_and_a_no_op_branch() -> None:
    """NEGATIVE CONTROLS of the walk above, on invented source."""
    ignored = "def f():\n    _settle_test_gate(a, b)\n"
    no_op = "def f():\n    if _settle_test_gate(a, b) is None:\n        pass\n"
    reverts = ("def f():\n    if _settle_test_gate(a, b) is None:\n"
               "        _revert_gen_tests_pass(x)\n")
    fails = ("def f():\n    if _settle_test_gate(a, b) is None:\n"
             "        return FixOutcome('failed', err_sig=s)\n")
    assert _unhandled_settle_calls(ignored) == [2]
    assert _unhandled_settle_calls(no_op) == [2]
    assert _unhandled_settle_calls(reverts) == []
    assert _unhandled_settle_calls(fails) == []


def test_a_world_that_turns_on_no_build_flag_is_not_measured_and_keeps_its_gate(
        tmp_path: Path) -> None:
    """A cluster with cfg atoms and no cargo flags (a cargo-feature cluster)
    has a gate that is false in the build it runs. Measuring there reports no
    test, which would read as "does not pass at home" and drop every block
    written under it. There is no reading to take: the gate stays as written
    and no world is asked. A world that does set flags is measured as usual."""
    clusters = [_BASE, _ALT]
    asked: list[str] = []
    log: list[str] = []
    base = _deps(clusters, passing={"base", "alt"}, asked=asked, log=log)
    import dataclasses
    # `alt` contributes nothing to cargo: its key is the no-cluster key.
    deps = dataclasses.replace(
        base, world_key=lambda c: "default" if c is None or c.name == "alt" else c.name)
    f = _written(tmp_path, _ALT, deps)
    before = f.read_text()
    assert settle_test_gate(tmp_path, "m", f, _BODY, _ALT, deps) == \
        'any(alt, feature = "alt")'
    assert asked == [] and f.read_text() == before
    assert any("turns on no build flag of its own" in line for line in log)
    # The default cluster contributes no flag either, and IS measured: its
    # gate is true in the build it runs.
    g = _written(tmp_path, _BASE, deps)
    settle_test_gate(tmp_path, "m", g, _BODY, _BASE, deps)
    assert asked[:1] == ["base"]

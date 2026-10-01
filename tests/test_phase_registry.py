"""Pins for the phase registry and its two contracts with the code.

The registry (`domain/phase_registry.py`) is the one list the pipeline announces
phases from and the monitor draws them from. Two ways it can rot, both pinned
here by reading the source that writes the artefact, never a hand-made list:

* a `pipeline_state.md` key written by the pipeline that no phase claims (the
  strip would never show it), found by scanning every `mark_phase` /
  `commit_and_mark` / `phase_valid` call for its literal key;
* a phase id announced that the registry does not know, or a registered id no
  call site ever announces (a cell that would read `pending` forever).

Run `34607-00cca24e` (2026-09-30) is the class: the strip, the bake and the
behaviour book ran for 24 minutes under a state line that still read
`task0c/nightly / starting`, because the strip's phases were in no list.
"""
from __future__ import annotations

import ast
from pathlib import Path

from pipeline_core.application.tasks.task0d import phase_key as t0d_phase_key
from pipeline_core.domain.phase_registry import (
    PHASES,
    TOP_LEVEL,
    children_of,
    known_ids,
    state_key_owner,
)

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(p for p in (ROOT / "pipeline_core").rglob("*.py")
                 if "monitor" not in p.parts and p.name != "phase_registry.py")

STATE_CALLS = {"mark_phase", "commit_and_mark", "phase_valid"}
SCOPE_CALLS = {"phase_scope", "phase_skipped"}
STEP_CALLS = {"phase_step", "step"}


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _template(node: ast.expr) -> str | None:
    """A string literal or f-string as a key template, `{...}` -> `x`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "x"
                       for v in node.values)
    return None


def _calls():
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                yield path, node, _name(node.func)


def test_registry_is_well_formed():
    ids = [p.id for p in PHASES]
    assert len(ids) == len(set(ids))
    for p in PHASES:
        assert p.label and p.title and p.what, p.id
        if p.parent is not None:
            assert p.parent in known_ids(), p.id
            assert ids.index(p.parent) < ids.index(p.id), p.id
    for p in TOP_LEVEL:
        assert p.why or p.id in {"t2b", "t2c", "t3", "t4", "lock"}, (
            f"{p.id}: a phase the detailed view explains needs a `why`")


def test_every_state_key_the_pipeline_writes_is_claimed_by_a_phase():
    seen: set[str] = set()
    for _path, node, fn in _calls():
        if fn in STATE_CALLS and len(node.args) >= 2:
            key = _template(node.args[1])
            if key is not None:
                seen.add(key)
    seen.add(t0d_phase_key("stable"))
    assert {"cfg_scan", "test_cmd", "task0b", "task2-4/x"} <= seen, seen
    unowned = sorted(k for k in seen if state_key_owner(k) is None)
    assert not unowned, f"state keys no phase claims: {unowned}"


def test_every_announced_id_is_registered_and_every_registered_id_is_announced():
    announced: set[str] = set()
    for path, node, fn in _calls():
        if fn in SCOPE_CALLS | STEP_CALLS and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if fn == "step" and "." not in arg.value:
                    continue  # an unrelated `.step(...)`
                announced.add(arg.value)
                assert arg.value in known_ids(), f"{path.name}: unknown phase {arg.value!r}"
            elif fn in SCOPE_CALLS:
                raise AssertionError(f"{path.name}: phase id must be a literal")
    dead = sorted(known_ids() - announced)
    assert not dead, f"registered but never announced (would read pending forever): {dead}"


def test_children_of_top_level_phases_are_the_dotted_ids():
    for p in TOP_LEVEL:
        assert all(c.id.startswith(p.id + ".") for c in children_of(p.id))
    assert "layout" in {p.id for p in TOP_LEVEL}, "t1.5 must not be a child of t1"

"""One answer to "which stub stops this test": a static pin.

Four places used to answer it from their own evidence (the sweep's first
failures, the coverage slice, attribution, the owed rows) and disagreed on
run `857717-812ee44c` (13 vs 47 stubs on iter 1). `domain/held_stubs` is the
view every consumer reads now. This test fails when a function anywhere in
`pipeline_core` takes a set of stubs and is named for deciding that a test is
hollow, held or blocked, which is the shape each of the deleted answers had
(`hollow_by_test(slices, stubs)`, `defer_blocked_consumers(frontier,
stub_keys)`, `_stub_blocks(units, hollow)`). A fifth consumer added beside the
view, instead of through it, fails here without anyone editing this test.

The one allowed function is named with its reason.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "pipeline_core"

#: Parameter names that carry a set of still-stubbed units.
_STUB_PARAMS = {"stubs", "stub_keys", "stub_set", "still_stubbed",
                "hollow_stubs", "stub_locs", "hollow"}
#: Name fragments that say the function decides a test is stopped by one.
_DECIDES = ("hollow", "held", "block", "stub_hold")
#: Allowed, with the reason it is not the same question.
_ALLOWED = {
    # The per-fn fix loop's PREDICTION for one failed attempt: whether a
    # correct body would need a still-stubbed sibling the test's attribution
    # names. The U2 gate has already classified every observed first failure
    # in another stub as external; this is not that observation.
    ("pipeline_core/domain/dispatch.py", "sibling_blocked_u2_failures"),
    # The schedule's own reading of a run against the held map it is
    # handed; it receives the view, it does not derive it.
    ("pipeline_core/domain/fill_schedule.py", "_blocking_stubs"),
    ("pipeline_core/domain/fill_schedule.py", "run_blocks"),
    ("pipeline_core/domain/fill_schedule.py", "_cover_blockers"),
    # Release a stored conviction once the blockers IT recorded are no
    # longer stubs: an inventory lookup on the record's own stamp, not a
    # decision about which stub a test stops in.
    ("pipeline_core/domain/retro_gate.py", "cleared_blockers"),
    ("pipeline_core/adapters/retros.py", "retire_unblocked_convictions"),
    # Orders stub RECORDS by the driver's rank map (`dispatch_ranks_of`);
    # `stubs` there is the pool, not a set deciding anything is held.
    ("pipeline_core/application/reconstruct/scheduling.py",
     "rank_stubs_by_blocker_counts"),
}


def _offenders(tree: ast.AST, rel: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name.lower()
        if not any(d in name for d in _DECIDES):
            continue
        params = {a.arg for a in (*node.args.posonlyargs, *node.args.args,
                                  *node.args.kwonlyargs)}
        if not params & _STUB_PARAMS:
            continue
        if (rel, node.name) in _ALLOWED:
            continue
        out.append(f"{rel}::{node.name}({', '.join(sorted(params & _STUB_PARAMS))})")
    return out


def test_no_function_outside_the_view_decides_a_test_is_held() -> None:
    offenders: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = str(path.relative_to(ROOT.parent))
        if rel == "pipeline_core/domain/held_stubs.py":
            continue
        offenders += _offenders(ast.parse(path.read_text(encoding="utf-8")), rel)
    assert offenders == [], offenders


def test_negative_control_the_deleted_answers_would_fail_here() -> None:
    """The three deleted signatures, as they were, are caught."""
    src = '''
def hollow_by_test(slices, stubs): ...
def defer_blocked_consumers(frontier, stub_keys): ...
def _stub_blocks(units, hollow): ...
'''
    found = _offenders(ast.parse(src), "pipeline_core/domain/x.py")
    assert len(found) == 3, found

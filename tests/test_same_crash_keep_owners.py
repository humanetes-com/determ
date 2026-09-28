"""Every non-regression keep asks whether the change left the crash.

Run `371055-1a407b3c` (2026-09-27): two session turns rewrote members of a
stack-overflow cycle so they still called each other, and the session kept
them on non-regression. A later correct one-member fix then no longer
broke the cycle, and one test stayed aborted. The question is
`crash_keep.SameCrashJudge.refuses`. There are four keep sites that build
"non-regression" themselves (per-fn, file, impl, pair) plus the session's
turn judge, and the first cut of the rule was a unit test of the judge
alone: deleting the call at any site stayed green.

This is the static half. Every log or verdict text in `pipeline_core` that
announces a keep on non-regression is found by its words. The function
holding it has to ask the judge (a `.refuses(...)` call) in the lines
before it. A new keep site fails here until it asks. Keyed by the text, not
by a line number, so moving code does not fail it.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "pipeline_core"

#: Words that announce a keep of a compiling change whose tests are not
#: passing. Lowercased; matched against each literal as the parser joins it.
KEEP_WORDS = ("kept on non-regression",)

#: How far above the keep text the question has to be asked, in lines. The
#: sites ask immediately before they log; the bound only has to stop a call
#: at the top of a 5,000-line function from vouching for a keep at its end.
WINDOW = 30

#: Texts that restate a keep already decided (and asked) above them, by
#: file and the literal's own words. Adding one is the moment to check that
#: the flag it reports went through the question.
RESTATED: dict[tuple[str, str], str] = {
    ("application/reconstruct/impl_fill.py",
     "kept on non-regression — no newly-red self-failure"):
        "verdict reason of the file keep; `_nonregr_keep_file` is asked "
        "where it is set",
}

#: The session's keep is its per-turn best-state decision, which logs no
#: "non-regression" words; its question is asked by the turn loop itself.
SESSION = ("application/reconstruct/slice_session.py", "run_slice_session")


def _text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant)
                       and isinstance(v.value, str))
    return ""


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _refuses_lines(fn: ast.AST) -> list[int]:
    return [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "refuses"]


def _keep_lines(fn: ast.AST, rel: str = "") -> list[int]:
    out: list[int] = []
    for n in ast.walk(fn):
        if not isinstance(n, (ast.JoinedStr, ast.Constant)):
            continue
        text = _text(n)
        if (rel, text) in RESTATED:
            continue
        if any(w in text.lower() for w in KEEP_WORDS):
            out.append(n.lineno)
    return out


def unasked_keeps(source: str, rel: str) -> list[tuple[str, int]]:
    """Keep texts in `source` with no `.refuses(` call in the WINDOW lines
    above them, inside the innermost function that holds both."""
    tree = ast.parse(source)
    missing: list[tuple[str, int]] = []
    seen: set[int] = set()
    # Innermost function first, so a nested closure answers for its own
    # keeps and the outer function is not credited with them.
    fns = sorted(_functions(tree),
                 key=lambda f: (f.end_lineno or f.lineno) - f.lineno)
    for fn in fns:
        asks = _refuses_lines(fn)
        for line in _keep_lines(fn, rel):
            if line in seen:
                continue
            seen.add(line)
            if not any(0 <= line - a <= WINDOW for a in asks):
                missing.append((rel, line))
    return missing


def _all_sources() -> list[tuple[str, str]]:
    return [(p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8"))
            for p in sorted(ROOT.rglob("*.py"))]


def test_every_non_regression_keep_asks_the_same_crash_question() -> None:
    missing = [m for rel, src in _all_sources()
               for m in unasked_keeps(src, rel)]
    assert not missing, (
        "a keep on non-regression that never asks SameCrashJudge.refuses — "
        f"it can keep a body that is still the crash it was written against: "
        f"{missing}")


def test_the_known_keep_sites_are_found() -> None:
    """The words have to find the four sites, or the test above proves
    nothing: a reworded log line would drop a site out of it."""
    found = {rel for rel, src in _all_sources()
             if any(_keep_lines(fn) for fn in _functions(ast.parse(src)))}
    assert "application/reconstruct/fill_one_fn.py" in found
    assert "application/reconstruct/impl_fill.py" in found
    impl_src = (ROOT / "application/reconstruct/impl_fill.py").read_text(
        encoding="utf-8")
    keeps = {ln for fn in _functions(ast.parse(impl_src))
             for ln in _keep_lines(fn)}
    # file (log and verdict reason), impl and pair.
    assert len(keeps) >= 4, keeps


def test_the_session_turn_loop_asks() -> None:
    rel, name = SESSION
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    fn = next(f for f in _functions(tree) if f.name == name)
    assert _refuses_lines(fn), (
        "run_slice_session keeps a compiling turn without asking whether it "
        "left the crash")


def test_negative_control_a_keep_site_without_the_question_is_flagged() -> None:
    """The per-fn site with its question removed is what the pre-fix code
    looked like; the scan has to flag it."""
    rel = "application/reconstruct/fill_one_fn.py"
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert not unasked_keeps(src, rel)
    stripped = src.replace(".refuses(", ".refuses_not_called(")
    assert unasked_keeps(stripped, rel)

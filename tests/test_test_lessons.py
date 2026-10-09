"""Task 4 writes a lesson per red test from what t2a tried for it, and the
next t2a owes the function it names, first, and prints it.

Before: t4 saw the test output, `genesis.md` and a failure log of pass
counts, never what t2a tried for a red test, and wrote `genesis.md`, which no
t2a fill route reads. The loop could carry nothing from one iteration's
writer to the next one's."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.adapters.red_ledger_store import load_red_ledger, save_red_ledger
from pipeline_core.adapters.test_lessons_store import load_lessons, save_lessons
from pipeline_core.application.reconstruct.test_lessons import (
    LessonPassDeps,
    run_test_lessons,
)
from pipeline_core.domain.candidate_tiers import AskLedger, due_reading
from pipeline_core.domain.test_lessons import Lesson, LessonStore, admit_lessons, parse_lessons

_REPLY = """<LESSON fn="src/ensure.rs::render">
The body prints `left: … right: …`; the test asserts the message contains
` vs `: format the operands as `({lhs:?} vs {rhs:?})`.
</LESSON>
<LESSON fn="src/lib.rs::elsewhere">off the slice</LESSON>"""


def test_a_reply_parses_and_only_on_slice_new_lessons_are_admitted() -> None:
    got = parse_lessons(_REPLY, "test_atom")
    assert [le.unit for le in got] == ["src/ensure.rs::render", "src/lib.rs::elsewhere"]
    store = LessonStore()
    admitted, refused = admit_lessons(
        got, allowed={"src/ensure.rs::render"}, store=store)
    assert [le.unit for le in admitted] == ["src/ensure.rs::render"]
    assert refused[0][1] == "names a function off the test's slice"
    store.add(admitted[0])
    again, refused2 = admit_lessons(
        parse_lessons(_REPLY, "test_atom"), allowed={"src/ensure.rs::render"},
        store=store)
    assert not again and "already stored" in refused2[0][1]


def test_a_new_lesson_re_owes_its_function_first_and_once() -> None:
    slices = {"test_atom": frozenset({"src/ensure.rs::render", "src/error.rs::fmt"})}
    reach = {"src/error.rs::fmt": frozenset({"A"}),
             "src/ensure.rs::render": frozenset({"A", "B"})}
    ledger = AskLedger()
    for u in slices["test_atom"]:
        ledger.record(u, "test_atom", due_reading(
            ["test_atom"], slices=slices, reach=reach, evidence={}, held={},
            ledger=AskLedger()).fingerprints["test_atom"])
    spent = due_reading(["test_atom"], slices=slices, reach=reach, evidence={},
                        held={}, ledger=ledger)
    assert spent.exhausted, "every function asked under this evidence"
    store = LessonStore()
    store.add(Lesson("src/ensure.rs::render", "test_atom", "use ` vs `"))
    r = due_reading(["test_atom"], slices=slices, reach=reach, evidence={},
                    held={}, ledger=ledger, lessons=store.digests())
    assert set(r.by_test["test_atom"].owed) == {"src/ensure.rs::render"}
    for u, fp in r.by_test["test_atom"].owed.items():
        ledger.record(u, "test_atom", fp)
    assert due_reading(["test_atom"], slices=slices, reach=reach, evidence={},
                       held={}, ledger=ledger, lessons=store.digests()).exhausted
    assert "use ` vs `" in store.render_for("src/ensure.rs::render", ["test_atom"])


def _deps(tmp_path: Path, replies: list[str], calls: list[str]) -> LessonPassDeps:
    def ask(prompt: str) -> str:
        calls.append(prompt)
        return replies.pop(0) if replies else ""
    return LessonPassDeps(
        ask=ask, load_red_ledger=load_red_ledger, load_lessons=load_lessons,
        save_lessons=save_lessons,
        test_files=lambda pd, cl: {"tests/t.rs": "#[test]\nfn test_atom() { ensure!(1 == 2); }\n"},
        unit_bodies=lambda pd, cl: {"src/ensure.rs::render": "format!(\"left: {lhs:?}\")"},
        log=lambda *_a: None, emit=lambda *_a, **_k: None)


def test_the_t4_pass_asks_per_red_test_skips_withdrawn_and_stores_lessons(
        tmp_path: Path) -> None:
    entries: list[dict[str, Any]] = [
        {"test": "test_atom", "class": "model_limit", "why": "w",
         "tiers": [["src/ensure.rs::render"]], "asked": ["src/ensure.rs::render"],
         "words": "left: false right: true"},
        {"test": "test_ui", "class": "withdrawn", "why": "original fails it"},
    ]
    save_red_ledger(tmp_path, "stable", entries, run_id="r", close_kind="cluster_failed")
    calls: list[str] = []
    n = run_test_lessons(tmp_path, ["stable", "nightly"],
                         _deps(tmp_path, [_REPLY], calls), iteration="1")
    assert n == 1 and len(calls) == 1, "one call: the withdrawn test is skipped"
    assert "fn test_atom()" in calls[0] and "left: false right: true" in calls[0]
    assert "format!(\"left: {lhs:?}\")" in calls[0]
    store = load_lessons(tmp_path)
    assert store.count() == 1
    calls2: list[str] = []
    assert run_test_lessons(tmp_path, ["stable"],
                            _deps(tmp_path, [_REPLY], calls2), iteration="2") == 0, \
        "the same lesson again is not new: no re-entry on it"

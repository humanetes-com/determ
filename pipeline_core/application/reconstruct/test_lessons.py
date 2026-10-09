"""Task 4's per-test pass: a lesson per red test, from what t2a tried for it.

Runs before task 4's genesis update, once per outer iteration. For every red
test of every cluster's red ledger that the original crate does not also
fail, one model call with the test's ledger entry, its source, what it
printed and the current bodies of its functions; the admitted lessons are
stored (`domain/test_lessons`) and the next t2a prints them for the
functions they name and owes those functions again for that test. Returns
how many lessons were admitted: the module loop re-enters t2a only when this
is non-zero (`ModuleReconstructor`, `stall_refill`)."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.refill_evidence import test_item_source
from pipeline_core.domain.test_lessons import LessonStore, admit_lessons, parse_lessons
from pipeline_core.prompts.test_lessons import build_test_lesson_prompt

__all__ = ["LessonPassDeps", "run_test_lessons"]

_MAX_UNITS = 8
_MAX_BODY = 2500


@dataclass(frozen=True, slots=True)
class LessonPassDeps:
    ask: Callable[[str], str]
    load_red_ledger: Callable[[Path, str], Mapping[str, Any]]
    load_lessons: Callable[[Path], LessonStore]
    save_lessons: Callable[[Path, LessonStore], None]
    #: (project_dir, cluster) -> {path: text} of the files tests live in.
    test_files: Callable[[Path, str], Mapping[str, str]]
    #: (project_dir, cluster) -> {"file::fn": body} of the cluster's tree.
    unit_bodies: Callable[[Path, str], Mapping[str, str]]
    log: Callable[[str], None]
    emit: Callable[..., None]


def _allowed(entry: Mapping[str, Any]) -> list[str]:
    units: list[str] = []
    for tier in entry.get("tiers") or []:
        units.extend(tier)
    for key in ("held_by", "owed", "asked"):
        units.extend(entry.get(key) or [])
    units.extend((entry.get("missed") or {}).keys())
    return list(dict.fromkeys(str(u) for u in units if u))


def _source_of(test: str, files: Mapping[str, str]) -> str:
    for text in files.values():
        src = test_item_source(text, test)
        if src:
            return src
    return ""


def run_test_lessons(project_dir: Path, clusters: Iterable[str],
                     deps: LessonPassDeps, *, iteration: str = "") -> int:
    store = deps.load_lessons(project_dir)
    admitted_n = 0
    for cluster in clusters:
        ledger = deps.load_red_ledger(project_dir, cluster)
        entries = [e for e in ledger.get("entries") or []
                   if e.get("class") != "withdrawn"]
        if not entries:
            continue
        files = deps.test_files(project_dir, cluster)
        bodies_all = deps.unit_bodies(project_dir, cluster)
        for e in entries:
            test = str(e.get("test", ""))
            allowed = _allowed(e)
            if not test or not allowed:
                continue
            bodies = {u: bodies_all.get(u, "")[:_MAX_BODY]
                      for u in allowed[:_MAX_UNITS]}
            prompt = build_test_lesson_prompt(
                test=test, entry=e, test_source=_source_of(test, files),
                words=str(e.get("words", "")), bodies=bodies, allowed=allowed)
            try:
                reply = deps.ask(prompt)
            except Exception as exc:  # noqa: BLE001 - one test's call never stops the pass
                deps.log(f"  [t4/lessons] [{cluster}] {test}: call failed: {exc!r}")
                continue
            got, refused = admit_lessons(
                parse_lessons(reply, test, cluster=cluster, iteration=iteration),
                allowed=allowed, store=store)
            for le in got:
                store.add(le)
            admitted_n += len(got)
            deps.log(f"  [t4/lessons] [{cluster}] {test} ({e.get('class')}): "
                     f"{len(got)} lesson(s) admitted"
                     + (f", {len(refused)} refused ("
                        + "; ".join(f"{r.unit}: {why}" for r, why in refused[:3])
                        + ")" if refused else ""))
            deps.emit("task4.test_lessons", data={
                "cluster": cluster, "test": test, "class": e.get("class"),
                "admitted": [le.unit for le in got],
                "refused": [[r.unit, why] for r, why in refused]})
    if admitted_n:
        deps.save_lessons(project_dir, store)
    deps.log(f"  [t4/lessons] {admitted_n} new lesson(s) for the next t2a "
             f"({store.count()} stored).")
    return admitted_n

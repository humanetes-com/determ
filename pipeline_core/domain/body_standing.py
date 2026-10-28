"""Which filled bodies on the tree have evidence that they are right (pure
domain, plus the per-fill binding every prompt reader goes through).

After strip every filled body is a model's answer, and most were kept while
their tests could not run (they stopped in a still-stubbed function). Run
`1057870-b427a3a2` (2026-10-15): on that augmentation 71 bodies were graded
`unmeasured` at their landing, 0 of them were ever graded again, and the
prompt's crate-idioms section showed one of them — a constructor that calls
an accessor on `NonNull::dangling()` — to every later prompt of the file as
"patterns this file already uses, MATCH these". The next constructor copied
it.

A body is PROVEN ON THE TREE when the body on the tree is the one a proven
ledger row graded, or when, in the latest intact sweep, at least one test
attributed to the unit passed with this body on the tree and none of them
died. That second clause is not written back to the ledger: the schedule
reads a landing's later grade as that landing's payoff
(`fill_schedule.unit_evidence_from`), and a test a sibling turned green is
not what the landing bought. It is a reading of the tree, recomputed at every
sweep.

The driver binds the set once per sweep (:func:`bind`); readers that show a
filled body to the model as something to follow call :func:`is_proven_text`
themselves, so none of them can be wired without it. Unbound (None) means no
reading was taken: readers then make no claim either way.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from pipeline_core.domain.body_ledger import canon_body, strip_comments
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_fn_headers import fn_headers, locate_all_fns
from pipeline_core.domain.rust_scan import is_hollow_body

# unit -> the identities of its proven bodies, as the sweep read them; and
# every unit with a body the sweep read, proven or not.
_proven: Mapping[FnLocator, frozenset[str]] | None = None
_known: frozenset[FnLocator] = frozenset()


def _identity(body_text: str) -> str:
    # Comments are not compared: a prompt's whole-file view may elide them.
    return canon_body(strip_comments(body_text))


def bind(proven: Mapping[FnLocator, Iterable[str]] | None,
         known: Iterable[FnLocator] = ()) -> None:
    """Install the sweep's reading: ``proven`` maps a unit to its body texts
    on the tree when the sweep was read; ``known`` is every unit the reading
    covered. None clears the reading."""
    global _proven, _known
    _proven = (None if proven is None else
               {u: frozenset(_identity(t) for t in texts)
                for u, texts in proven.items()})
    _known = frozenset(known) | frozenset(_proven or ())


def is_proven_text(unit: FnLocator, body_text: str) -> bool | None:
    """Whether ``body_text`` is a proven body of ``unit`` on this tree. A body
    rewritten since the sweep is not the one the sweep measured. None when no
    reading is bound, or the reading never saw the unit (a test file, a path
    outside the crate)."""
    if _proven is None or unit not in _known:
        return None
    return _identity(body_text) in _proven.get(unit, frozenset())


#: The line a prompt's file view puts above a filled body that is not proven.
UNVERIFIED_MARK = ("// UNVERIFIED — an earlier fill's answer; no test has "
                   "passed with it on the tree yet. Read it for what it "
                   "calls, not as how this crate does things.")


def mark_unverified(path: str, text: str) -> str:
    """``text`` (a rendering of the crate file ``path``) with
    :data:`UNVERIFIED_MARK` above every filled body the bound reading knows
    and does not find proven. Stubs, unknown units and an unbound reading
    are left as they are."""
    if _proven is None:
        return text
    inserts: list[tuple[int, str]] = []
    for name in dict.fromkeys(h.name for h in fn_headers(text, line_anchored=False)):
        for _sig, sig_start, body_start, body_end in locate_all_fns(text, name):
            body = text[body_start:body_end]
            if is_hollow_body(body):
                continue
            if is_proven_text(FnLocator(path, name), body) is not False:
                continue
            line_start = text.rfind("\n", 0, sig_start) + 1
            indent = text[line_start:sig_start]
            indent = indent[:len(indent) - len(indent.lstrip(" \t"))]
            inserts.append((line_start, f"{indent}{UNVERIFIED_MARK}\n"))
    for at, line in sorted(set(inserts), reverse=True):
        text = text[:at] + line + text[at:]
    return text


def proven_units(
    ledger_proven: Iterable[FnLocator],
    attributed: Mapping[str, Iterable[FnLocator]],
    *,
    passed: Iterable[str],
    died: Iterable[str],
    intact: bool,
) -> frozenset[FnLocator]:
    """The units whose on-tree bodies :func:`bind` records.
    ``ledger_proven`` are the units whose on-tree body a proven ledger row
    graded; ``attributed`` maps a test
    (bare name) to the units it is attributed to; ``passed`` and ``died``
    are the sweep's passing tests and the tests whose process died. A sweep
    that could not be reconciled (``intact`` False) proves nothing new."""
    out = set(ledger_proven)
    if not intact:
        return frozenset(out)
    dead = frozenset(died)
    by_unit: dict[FnLocator, set[str]] = {}
    for test, units in attributed.items():
        for u in units:
            by_unit.setdefault(u, set()).add(test)
    passing = frozenset(passed)
    for unit, tests in by_unit.items():
        if tests & passing and not tests & dead:
            out.add(unit)
    return frozenset(out)


def lost_to_aborts(
    attributed: Mapping[str, Iterable[FnLocator]],
    *,
    passed: Iterable[str],
    died: Iterable[str],
) -> frozenset[FnLocator]:
    """The units a passing attributed test would prove that :func:`proven_units`
    leaves out only because another attributed test's process died. The
    reading entry 365.4 asks for: on run `2355041-eae39441` (2026-10-27) the
    standing set read 48, 4, 48 proven units at iterations 3, 4, 5 with the
    sweep reconciled every time; iteration 4 was the abort-heavy sweep. This
    count says whether the `died` clause is what emptied it."""
    dead = frozenset(died)
    passing = frozenset(passed)
    by_unit: dict[FnLocator, set[str]] = {}
    for test, units in attributed.items():
        for u in units:
            by_unit.setdefault(u, set()).add(test)
    return frozenset(u for u, tests in by_unit.items()
                     if tests & passing and tests & dead)

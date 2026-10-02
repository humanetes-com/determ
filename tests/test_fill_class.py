"""Pins for the per-fill class histogram — the seven integers on the iter line."""
from __future__ import annotations

import pytest

from pipeline_core.domain.fill_class import (
    FillClassCounter,
    FillClassHistogram,
    FillOutcomeClass,
    class_for_kept,
    keep_reading,
    note_fill_class,
)
from pipeline_core.domain.fill_class import (
    tests_could_not_run as could_not_run,
)


def test_rendered_matches_iter_line_token() -> None:
    h = FillClassHistogram(cashed=2, wrong=3, mis=1, unreachable=4,
                           dropped=0, compile=5, declined=1)
    assert h.rendered() == (
        "classes: cashed=2 wrong=3 mis=1 unreachable=4 dropped=0 compile=5 "
        "declined=1")
    assert h.total == 16
    assert h


def test_empty_histogram_is_falsey() -> None:
    assert not FillClassHistogram()
    assert FillClassHistogram().rendered().startswith("classes: cashed=0")


_FLAT = keep_reading()


def test_class_for_kept_is_never_cashed_on_observed_passes_alone() -> None:
    """An empty passing set was the else-of-U2 CASHED assignment (run
    1064759-0f23fb68). A non-empty one with a reading that moved nothing
    was the second: run `3040-51eab48c` (2026-10-02, fill 5) graded four
    keeps `cashed` beside `net +0` for a test an earlier keep turned
    green. Observed passing with nothing moved is MIS_TARGETED."""
    assert class_for_kept(reading=_FLAT, passed=frozenset(),
                          unreachable=could_not_run()) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)
    assert class_for_kept(reading=_FLAT, passed={"t"},
                          unreachable=could_not_run()) is (
        FillOutcomeClass.MIS_TARGETED)
    assert class_for_kept(
        reading=_FLAT, passed={"t"},
        unreachable=could_not_run(crash_keep=True)) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)


def test_the_keep_after_the_one_that_paid_is_not_cashed() -> None:
    """The plan's sentence: no `cashed` beside `net +0`. The first keep's
    comparison turns the unit's attributed test green and cashes; the next
    keep of the same fill sees that test passing, moves nothing, and is
    MIS_TARGETED. Negative control: the pre-change rule (attributed tests
    observed passing) grades the second keep CASHED."""
    attributed = {"bin::t_last_red"}
    first = keep_reading(net_delta=1, newly_green={"t_last_red"},
                         attributed=attributed)
    assert class_for_kept(reading=first, passed={"t_last_red"},
                          unreachable=could_not_run(),
                          attributed=attributed, measured=True) is (
        FillOutcomeClass.CASHED)
    second = keep_reading(net_delta=0, newly_green=(), attributed=attributed)
    got = class_for_kept(reading=second, passed={"t_last_red"},
                         unreachable=could_not_run(),
                         attributed=attributed, measured=True)
    assert got is FillOutcomeClass.MIS_TARGETED

    def _pre_change(passed: set[str]) -> FillOutcomeClass:
        return (FillOutcomeClass.CASHED if passed
                else FillOutcomeClass.WRONG_BODY)

    assert _pre_change({"t_last_red"}) is FillOutcomeClass.CASHED


def test_a_newly_green_test_counts_only_inside_the_units_own_tests() -> None:
    """A sibling's test going green in the same comparison is not this
    unit's payment; the suite's net gain still is (a joint keep with an
    empty attributed set, run 3014175-d98ed09e)."""
    assert not keep_reading(net_delta=0, newly_green={"t_sibling"},
                            attributed={"t_own"})
    assert keep_reading(net_delta=0, newly_green={"bin::t_own"},
                        attributed={"t_own"})
    assert keep_reading(net_delta=3, attributed=())
    assert not keep_reading(net_delta=-1, attributed={"t_own"})


def test_some_attributed_still_red_and_none_moved_is_wrong_body() -> None:
    assert class_for_kept(
        reading=_FLAT, passed={"t_a"}, unreachable=could_not_run(),
        attributed={"t_a", "t_b"}, measured=True) is (
        FillOutcomeClass.WRONG_BODY)


def test_cashed_cannot_be_asserted_without_a_reading() -> None:
    """The class this closes: a site grading CASHED from its own evidence.
    The reading is a type built by one function, and the function has no
    argument for tests observed passing."""
    import inspect

    assert set(inspect.signature(keep_reading).parameters) == {
        "net_delta", "newly_green", "attributed"}
    assert "net_delta" not in inspect.signature(class_for_kept).parameters
    assert "newly_passing" not in inspect.signature(class_for_kept).parameters
    for fabricated in (True, 5, {"t"}, (1, {"t"})):
        with pytest.raises(TypeError, match="keep_reading"):
            class_for_kept(reading=fabricated, passed={"t"},
                           unreachable=could_not_run())


def test_class_for_kept_measured_attributed_still_failing_is_wrong_body() -> None:
    """A keep whose attributed tests ran and stayed red is WRONG_BODY.

    Run 3500465-67c40e78 labelled that unreachable because `not passed`
    collapsed "none passed" with "could not run". Negative control: the
    same attributed set unmeasured, or no attributed tests, stays
    UNREACHABLE. Invented names only.
    """
    assert class_for_kept(
        reading=_FLAT, passed=frozenset(), unreachable=could_not_run(),
        attributed={"t_aim"}, measured=True) is FillOutcomeClass.WRONG_BODY
    assert class_for_kept(
        reading=_FLAT, passed=frozenset(), unreachable=could_not_run(),
        attributed={"t_aim"}, measured=False) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)
    assert class_for_kept(
        reading=_FLAT, passed=frozenset(), unreachable=could_not_run(),
        attributed=frozenset(), measured=True) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)
    # Omitting attributed/measured keeps the pre-change reading.
    assert class_for_kept(reading=_FLAT, passed=frozenset(),
                          unreachable=could_not_run()) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)


def test_class_for_kept_a_reading_that_moved_is_cashed() -> None:
    """A keep whose suite reading moved verdicts is CASHED even with an
    empty attributed set — the session keep of run 3014175-d98ed09e
    iter 3 session #1 (net +5, a target still red) and the per-fn
    object_ref keep of iter 2 (net +55, hollow leftover set). Negative
    control: the same call with the reading withheld is UNREACHABLE."""
    assert class_for_kept(
        reading=keep_reading(net_delta=5), passed=frozenset(),
        unreachable=could_not_run()) is FillOutcomeClass.CASHED
    assert class_for_kept(
        reading=keep_reading(net_delta=0), passed=frozenset(),
        unreachable=could_not_run()) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)


def test_counter_and_note_emit() -> None:
    events: list[tuple[str, dict]] = []
    c = FillClassCounter()
    note_fill_class(
        lambda ev, data=None, **_k: events.append((ev, data or {})),
        c, FillOutcomeClass.MIS_TARGETED,
        cluster="stable", unit="src/error.rs::from", route="impl")
    assert events[0][0] == "phase_c.fill.class"
    assert events[0][1]["class"] == "mis"
    assert c.histogram().mis == 1
    assert c.histogram().combined_with(
        FillClassHistogram(wrong=2)).wrong == 2


# --- unreachable is one helper; a non-regression keep cannot be passed in --
# Run `3400-4d0618ba` (2026-09-21): the file and pair sites folded the
# non-regression keep into `unreachable=`, so all four file keeps were graded
# `unreachable` beside a GUARD line saying their tests ran.


def test_tests_could_not_run_holds_only_the_could_not_run_reasons() -> None:
    import inspect

    assert not could_not_run()
    for reason in ("crash_keep", "external_only", "blocked_sibling",
                   "crate_red"):
        got = could_not_run(**{reason: True})
        assert got and got.reason == reason
    assert set(inspect.signature(could_not_run).parameters) == {
        "crash_keep", "external_only", "blocked_sibling", "crate_red"}


def test_a_keep_whose_tests_ran_red_is_wrong_not_unreachable() -> None:
    attributed = {"t_alpha", "t_beta"}
    assert class_for_kept(reading=_FLAT, passed=frozenset(),
                          unreachable=could_not_run(),
                          attributed=attributed, measured=True) \
        is FillOutcomeClass.WRONG_BODY
    assert class_for_kept(
        reading=_FLAT, passed=frozenset(),
        unreachable=could_not_run(crash_keep=True),
        attributed=attributed, measured=True) \
        is FillOutcomeClass.UNREACHABLE_VERDICT
    # The pre-change file kwargs — a non-regression flag folded into
    # `unreachable=` — cannot be expressed at all any more: the reason is a
    # type, and a site's own boolean is refused rather than believed. This
    # is what makes the class unreachable for a keep site that never calls
    # the helper, which the AST walk below cannot promise for a site that
    # does not exist yet.
    for fabricated in (True, 1, "blocked", object()):
        with pytest.raises(TypeError, match="tests_could_not_run"):
            class_for_kept(reading=_FLAT, passed=frozenset(),
                           unreachable=fabricated,
                           attributed=attributed, measured=True)


def _keep_site_violations(tree: object, path: str) -> list[str]:
    import ast
    assert isinstance(tree, ast.AST)
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else "")
            if name != "class_for_kept":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            v = kw.get("unreachable")
            ok = (isinstance(v, ast.Call)
                  and isinstance(v.func, ast.Name)
                  and v.func.id == "tests_could_not_run")
            if not ok:
                bad.append(f"{path}:{node.lineno} class_for_kept "
                           "unreachable= is not could_not_run(...)")
            r = kw.get("reading")
            if not (isinstance(r, ast.Call) and isinstance(r.func, ast.Name)
                    and r.func.id == "keep_reading"):
                bad.append(f"{path}:{node.lineno} class_for_kept "
                           "reading= is not keep_reading(...)")
        elif (isinstance(node, ast.Attribute)
              and node.attr == "UNREACHABLE_VERDICT"
              and not path.endswith("domain/fill_class.py")):
            bad.append(f"{path}:{node.lineno} UNREACHABLE_VERDICT outside "
                       "class_for_kept")
    return bad


def test_every_keep_site_grades_through_the_helper() -> None:
    """Every consumer, not a fixed count: a new keep site that inlines a
    flag, or emits UNREACHABLE directly, fails here without editing it."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "pipeline_core"
    bad: list[str] = []
    calls = 0
    for py in sorted(root.rglob("*.py")):
        src = py.read_text(encoding="utf-8")
        if "class_for_kept" not in src and "UNREACHABLE_VERDICT" not in src:
            continue
        rel = py.relative_to(root.parent).as_posix()
        tree = ast.parse(src)
        calls += sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and getattr(n.func, "id", getattr(n.func, "attr", ""))
                     == "class_for_kept")
        bad += _keep_site_violations(tree, rel)
    assert calls >= 1
    assert bad == []


def test_the_site_check_catches_an_inlined_flag() -> None:
    """Negative control for the walk: the pre-change file-site shape."""
    import ast
    src = ("_cls(class_for_kept(reading=keep_reading(), passed=p, "
           "unreachable=_crash_keep_file or _nonregr_keep_file))\n"
           "_cls(FillOutcomeClass.UNREACHABLE_VERDICT, b, 'impl')\n")
    bad = _keep_site_violations(ast.parse(src), "pipeline_core/x.py")
    assert len(bad) == 2


def test_the_site_check_catches_a_site_grading_without_a_comparison() -> None:
    """Negative control: the pre-change session-accepted shape, which cashed
    from the slice's test names with no comparison at all."""
    import ast
    src = ("_cls(class_for_kept(passed=frozenset(_sl.tests), "
           "unreachable=tests_could_not_run(), measured=True))\n"
           "_cls(class_for_kept(reading=my_own_reading, passed=p, "
           "unreachable=tests_could_not_run()))\n")
    bad = _keep_site_violations(ast.parse(src), "pipeline_core/x.py")
    assert len(bad) == 2 and all("keep_reading" in b for b in bad)

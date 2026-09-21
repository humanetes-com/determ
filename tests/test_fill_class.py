"""Pins for the per-fill class histogram — the seven integers on the iter line."""
from __future__ import annotations

from pipeline_core.domain.fill_class import (
    FillClassCounter,
    FillClassHistogram,
    FillOutcomeClass,
    class_for_kept,
    note_fill_class,
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


def test_class_for_kept_is_cashed_only_with_observed_passes() -> None:
    """Negative control: empty passing set was the else-of-U2 CASHED
    assignment (run 1064759-0f23fb68)."""
    assert class_for_kept(passed=frozenset(), unreachable=False) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)
    assert class_for_kept(passed={"t"}, unreachable=False) is (
        FillOutcomeClass.CASHED)
    assert class_for_kept(passed={"t"}, unreachable=True) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)


def test_class_for_kept_measured_attributed_still_failing_is_wrong_body() -> None:
    """A keep whose attributed tests ran and stayed red is WRONG_BODY.

    Run 3500465-67c40e78 labelled that unreachable because `not passed`
    collapsed "none passed" with "could not run". Negative control: the
    same attributed set unmeasured, or no attributed tests, stays
    UNREACHABLE. Invented names only.
    """
    assert class_for_kept(
        passed=frozenset(), unreachable=False,
        attributed={"t_aim"}, measured=True) is FillOutcomeClass.WRONG_BODY
    assert class_for_kept(
        passed=frozenset(), unreachable=False,
        attributed={"t_aim"}, measured=False) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)
    assert class_for_kept(
        passed=frozenset(), unreachable=False,
        attributed=frozenset(), measured=True) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)
    # Omitting attributed/measured keeps the pre-change reading.
    assert class_for_kept(passed=frozenset(), unreachable=False) is (
        FillOutcomeClass.UNREACHABLE_VERDICT)


def test_class_for_kept_a_reading_that_moved_is_cashed() -> None:
    """A keep whose suite reading moved verdicts is CASHED even with an
    empty attributed set — the session keep of run 3014175-d98ed09e
    iter 3 session #1 (net +5, a target still red) and the per-fn
    object_ref keep of iter 2 (net +55, hollow leftover set). Negative
    control: the same call with the reading withheld is UNREACHABLE."""
    assert class_for_kept(
        passed=frozenset(), unreachable=False, net_delta=5) is (
        FillOutcomeClass.CASHED)
    assert class_for_kept(
        passed=frozenset(), unreachable=False, net_delta=0) is (
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

    from pipeline_core.domain.fill_class import tests_could_not_run
    assert tests_could_not_run() is False
    for reason in ("crash_keep", "external_only", "blocked_sibling",
                   "crate_red"):
        assert tests_could_not_run(**{reason: True}) is True
    assert set(inspect.signature(tests_could_not_run).parameters) == {
        "crash_keep", "external_only", "blocked_sibling", "crate_red"}


def test_a_keep_whose_tests_ran_red_is_wrong_not_unreachable() -> None:
    from pipeline_core.domain.fill_class import tests_could_not_run
    attributed = {"t_alpha", "t_beta"}
    assert class_for_kept(passed=frozenset(), unreachable=tests_could_not_run(),
                          attributed=attributed, measured=True) \
        is FillOutcomeClass.WRONG_BODY
    assert class_for_kept(
        passed=frozenset(), unreachable=tests_could_not_run(crash_keep=True),
        attributed=attributed, measured=True) \
        is FillOutcomeClass.UNREACHABLE_VERDICT
    # Negative control: the pre-change file kwargs (non-regression folded
    # into unreachable) grade the same measured keep unreachable.
    assert class_for_kept(passed=frozenset(), unreachable=True,
                          attributed=attributed, measured=True) \
        is FillOutcomeClass.UNREACHABLE_VERDICT


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
                           "unreachable= is not tests_could_not_run(...)")
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
    src = ("_cls(class_for_kept(passed=p, unreachable=_crash_keep_file "
           "or _nonregr_keep_file))\n"
           "_cls(FillOutcomeClass.UNREACHABLE_VERDICT, b, 'impl')\n")
    bad = _keep_site_violations(ast.parse(src), "pipeline_core/x.py")
    assert len(bad) == 2

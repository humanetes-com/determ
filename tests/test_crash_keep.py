"""A compiling change that is still the same crash is not a keep.

Run 371055-1a407b3c (2026-09-27), cluster `stable`. The sweep blamed a
three-member stack-overflow cycle: `from_boxed`, `construct_from_boxed`,
`kind.rs::new`. Session #0 kept `kind.rs::new` (turn 5) and
`construct_from_boxed` (turn 8), each still calling the other, as
non-regression. Reverting only those two on a scratch copy of the tree
the fill left cleared every abort once per-fn had fixed `from_boxed`.

The fixtures below are that fill's units and outcomes. The claims are the
review's, not the code's: a rotated culprit is refused whatever else the
change wrote; a change that wrote no culprit is not this question; a
change whose aimed tests stopped crashing is not this question.
"""
from __future__ import annotations

from collections.abc import Collection

from pipeline_core.domain.crash_keep import (
    SameCrashJudge,
    aimed_all_still_native_crash,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.test_isolation import (
    IsolatedStatus,
    IsolatedTest,
    render_isolated_section,
)

_FROM = FnLocator("src/error.rs", "from_boxed")
_CFB = FnLocator("src/error.rs", "construct_from_boxed")
_NEW = FnLocator("src/kind.rs", "new")
_FMT = FnLocator("src/error.rs", "fmt")
_ERR_NEW = FnLocator("src/error.rs", "new")

#: The sweep's blame on that fill.
_BLAMED = frozenset({_FROM, _CFB, _NEW})
#: After turn 4/5 the ring is `construct_from_boxed` <-> `kind.rs::new`.
_RING_AFTER = [_CFB, _NEW]

_OVERFLOW = render_isolated_section([
    IsolatedTest("test_boxed_str", IsolatedStatus.ABORTED,
                 "thread 'test_boxed_str' has overflowed its stack\n"),
    IsolatedTest("test_from_boxed_executed", IsolatedStatus.ABORTED,
                 "thread 'test_from_boxed_executed' has overflowed its stack\n"),
])
_AIMED = ("test_boxed_str", "test_from_boxed_executed")


def _judge(after: Collection[FnLocator] | None) -> SameCrashJudge:
    return SameCrashJudge(
        _BLAMED, None if after is None else (lambda _out: list(after)))


def _pre_fix_rule(written: Collection[FnLocator],
                  after: Collection[FnLocator]) -> bool:
    """The first cut: refuse only when EVERY written unit is a culprit."""
    blamed = frozenset(after)
    return bool(written) and (not blamed or frozenset(written) <= blamed)


def test_the_session_rotations_are_refused() -> None:
    judge = _judge(_RING_AFTER)
    assert judge.refuses(output=_OVERFLOW, aimed=_AIMED, written=[_NEW])
    assert judge.refuses(output=_OVERFLOW, aimed=_AIMED, written=[_CFB])


def test_a_rotation_written_beside_an_innocent_body_is_refused() -> None:
    """The file route writes several bodies at once, the impl and pair
    routes whole blocks. One innocent body in the set must not save the
    rotated culprit. Negative control: the pre-fix rule keeps both shapes."""
    judge = _judge(_RING_AFTER)
    mixed = [_NEW, _FMT]
    file_shape = [_FMT, _ERR_NEW, _CFB]
    assert judge.refuses(output=_OVERFLOW, aimed=_AIMED, written=mixed)
    assert judge.refuses(output=_OVERFLOW, aimed=_AIMED, written=file_shape)
    assert not _pre_fix_rule(mixed, _RING_AFTER)
    assert not _pre_fix_rule(file_shape, _RING_AFTER)


def test_a_change_that_wrote_no_culprit_is_not_this_question() -> None:
    """The file route's keep on that fill: `fmt` and `error.rs::new`, while
    the aimed tests still aborted. Not a culprit, not refused, even when
    nothing could be localized."""
    for after in (_RING_AFTER, [], None):
        assert not _judge(after).refuses(
            output=_OVERFLOW, aimed=_AIMED, written=[_FMT, _ERR_NEW])


def test_a_written_culprit_that_left_the_crash_is_kept() -> None:
    """`from_boxed` rewritten out of the ring while the other two still
    loop: it left, so the change stands."""
    assert not _judge(_RING_AFTER).refuses(
        output=_OVERFLOW, aimed=_AIMED, written=[_FROM])


def test_an_unlocalized_crash_does_not_show_the_culprit_left() -> None:
    """One meaning for "not localized", whether no localizer was wired or
    it found nothing."""
    assert _judge([]).refuses(output=_OVERFLOW, aimed=_AIMED, written=[_NEW])
    assert _judge(None).refuses(output=_OVERFLOW, aimed=_AIMED, written=[_NEW])


def test_aimed_tests_that_stopped_crashing_are_not_this_question() -> None:
    """Per-fn `from_boxed` on that fill: 12 aimed tests passing."""
    passed = render_isolated_section([
        IsolatedTest("test_from_boxed_executed", IsolatedStatus.PASSED, ""),
    ])
    asked: list[str] = []

    def localize(out: str) -> list[FnLocator]:
        asked.append(out)
        return _RING_AFTER

    judge = SameCrashJudge(_BLAMED, localize)
    assert not judge.refuses(output=passed, aimed=["test_from_boxed_executed"],
                             written=[_FROM])
    assert not judge.refuses(output=_OVERFLOW,
                             aimed=[*_AIMED, "test_ok"], written=[_NEW])
    assert asked == [], "the localizer runs only when the question applies"


def test_the_crash_leaves_are_read_from_an_isolated_capture() -> None:
    assert aimed_all_still_native_crash(_AIMED, _OVERFLOW)
    assert not aimed_all_still_native_crash((), _OVERFLOW)


def test_a_localizer_that_fails_is_not_localized() -> None:
    def broken(_out: str) -> list[FnLocator]:
        raise RuntimeError("gdb did not start")

    judge = SameCrashJudge(_BLAMED, broken)
    assert judge.culprits_after(_OVERFLOW) == frozenset()
    assert judge.refuses(output=_OVERFLOW, aimed=_AIMED, written=[_NEW])

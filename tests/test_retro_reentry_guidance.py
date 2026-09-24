"""A fix loop re-enters on a retrospective only with guidance its next prompt
carries, and the lesson it just wrote is that guidance.

Run `271137-e728ae95` (2026-09-23): `src/error.rs::construct_from_boxed` and
`src/error.rs::context` logged `retro round 1 kept the error floor improving —
re-entering the fix loop with the new guidance`, then `[phase-c/retro-retired]
… recorded no tree premise — NOT injected (no_tree_premise)` on the re-entry
prompts, then `same error set` on the same E0277. The lesson was compile-taught
(`error_count` 2) and the failed body named no stub, so `blocked_on` was empty
and the screen's ninth rule retired it at its first read — inside the loop that
had just written it. Over the logs of the fortnight before, about four in five
logged re-entries carried no honoured guidance.

Two sentences are pinned:

- a record a fix loop wrote is admissible inside that loop even with no tree
  premise, and in no other loop (`FIX_LOOP_SCOPE`, `earned_in_loop`);
- a round re-enters only when the screen the next prompt runs admits the fresh
  record (`informed_reentry`), whatever the floor did.

The shape is invented: one `answer` fn in `lib.rs`, a compile error that never
changes, and a scripted retrospective.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline_core.domain import retro_gate
from pipeline_core.domain.retro_gate import (
    FIX_LOOP_SCOPE,
    NO_TREE_PREMISE,
    ConvictionEvidence,
    RetroProvenance,
    earned_in_loop,
    screen_guidance,
    stamp_conviction,
    tree_premise_absent,
)
from tests.test_fill_engine_usecases import (
    RecordingFill,
    _cov_ctx,
    _mk_ws,
    _run_bound,
)

_LESSON = "Name the boxed error type in the turbofish; do not dereference it."
_E0277 = "error[E0277]: the size for values of type `dyn Trait` cannot be known"


def _compile_record(scope: str = "") -> dict[str, Any]:
    """A compile-taught lesson whose failed body named no stub: the live
    shape, no `blocked_on`, no entry stamp, no joint sha."""
    return stamp_conviction(
        {"category": "WRONG_TYPE_CHOICE", "guidance": _LESSON,
         "error_count": 2, "failing_test_count": 0,
         "fully_exhausted": False},
        RetroProvenance.current("c", ""), ConvictionEvidence(frozenset()),
        retro_gate.BlockedOnStubs(frozenset()), loop_scope=scope)


# ---- the premise a fix loop's own record has ----


def test_a_loop_admits_the_compile_lesson_it_wrote() -> None:
    rec = _compile_record("loop-a")
    verdict = screen_guidance(rec, "", evidence=ConvictionEvidence(frozenset()),
                              loop_scope="loop-a")
    assert verdict.text == _LESSON and verdict.retired_by == ""


def test_control_another_loop_and_no_loop_retire_it_as_before() -> None:
    rec = _compile_record("loop-a")
    for scope in ("", "loop-b"):
        verdict = screen_guidance(
            rec, "", evidence=ConvictionEvidence(frozenset()), loop_scope=scope)
        assert verdict == ("", NO_TREE_PREMISE), scope


def test_the_loop_token_is_no_premise_for_exhaustion() -> None:
    """Exhaustion is honoured across dispatches, so it keeps asking for a tree
    premise; the loop token scopes guidance, never a skip."""
    rec = dict(_compile_record("loop-a"), fully_exhausted=True)
    assert tree_premise_absent(rec)


def test_a_restamp_outside_a_loop_drops_the_old_token() -> None:
    rec = _compile_record("loop-a")
    assert rec[FIX_LOOP_SCOPE] == "loop-a"
    again = stamp_conviction(dict(rec), RetroProvenance.current("c", ""),
                             ConvictionEvidence(frozenset()))
    assert FIX_LOOP_SCOPE not in again
    assert not earned_in_loop(again, "loop-a")
    assert not earned_in_loop(rec, "")


# ---- the re-entry reads the screen its next prompt runs ----


class _LessonRetro(RecordingFill):
    """A first-round retrospective that improves the floor and writes a
    compile-taught lesson. Keeps the store and every prompt it was sent."""

    def __init__(self, guidance: str = _LESSON, **scripted: Any) -> None:
        super().__init__(**scripted)
        self.guidance = guidance
        self.store: dict[str, Any] = {}
        self.prompts: list[str] = []

    def ask_per_fn_body(self, *a: Any, **k: Any) -> tuple[str | None, str]:
        out = super().ask_per_fn_body(*a, **k)
        self.prompts.append(self.s["sent"][-1] if self.s.get("sent") else "")
        return out

    def diagnose(self, **k: Any) -> dict[str, Any]:
        self.diagnose_calls += 1
        return {"signal": "", "category": "WRONG_TYPE_CHOICE",
                "guidance": self.guidance}

    def load(self, *a: Any) -> dict[str, Any]:
        return dict(self.store)

    def save(self, *a: Any) -> None:
        super().save(*a)
        self.store = dict(a[-1])


def _run(tmp_path: Path, fake: _LessonRetro) -> Any:
    ws = _mk_ws(tmp_path)
    return _run_bound(fake, ws, _cov_ctx(ws), entered=True)


def test_the_re_entry_prompt_carries_the_lesson_the_loop_just_wrote(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "2")
    fake = _LessonRetro(run=(1, _E0277), err=_E0277, body=("Ok(())", "R"))
    res = _run(tmp_path, fake)
    assert not res.filled
    assert "phase_c.fn.retro_retry" in fake.events
    assert any(_LESSON in p for p in fake.prompts), (
        "the re-entered loop must carry the lesson it re-entered on")
    assert not any("no_tree_premise" in m for m in fake.logs)


def test_control_without_the_loop_premise_the_re_entry_is_withheld(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same run with the loop premise removed: the fresh lesson is
    retired `no_tree_premise`, so the round does not re-enter. Before the
    re-entry read the screen, it re-entered anyway (next control)."""
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "2")
    monkeypatch.setattr(retro_gate, "earned_in_loop", lambda *a: False)
    fake = _LessonRetro(run=(1, _E0277), err=_E0277, body=("Ok(())", "R"))
    _run(tmp_path, fake)
    assert "phase_c.fn.retro_retry" not in fake.events
    assert "phase_c.fn.retro_retry_withheld" in fake.events
    assert fake.diagnose_calls == 1
    assert not any(_LESSON in p for p in fake.prompts)


def test_control_the_pre_fix_loop_re_enters_on_guidance_it_withholds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control, the live outcome: no loop premise and a re-entry
    that does not read the screen. The loop re-enters and no re-entry
    prompt carries the lesson."""
    import pipeline_core.application.reconstruct.fill_one_fn as fof
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "2")
    monkeypatch.setattr(retro_gate, "earned_in_loop", lambda *a: False)
    monkeypatch.setattr(fof, "informed_reentry", lambda v: True)
    fake = _LessonRetro(run=(1, _E0277), err=_E0277, body=("Ok(())", "R"))
    _run(tmp_path, fake)
    assert "phase_c.fn.retro_retry" in fake.events
    assert fake.diagnose_calls == 2
    assert not any(_LESSON in p for p in fake.prompts)
    assert any("no_tree_premise" in m for m in fake.logs)


def test_a_round_whose_lesson_the_screen_retires_does_not_re_enter(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any retiring rule, not only the premise: an empty lesson carries
    nothing, so the improved floor alone buys no re-entry."""
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "2")
    fake = _LessonRetro(guidance="", run=(1, _E0277), err=_E0277,
                        body=("Ok(())", "R"))
    _run(tmp_path, fake)
    assert "phase_c.fn.retro_retry" not in fake.events
    assert "phase_c.fn.retro_retry_withheld" in fake.events
    assert fake.diagnose_calls == 1

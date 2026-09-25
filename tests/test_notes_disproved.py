"""A lesson nothing validated is not standing guidance.

The class, from run `116341-24c76a97` (2026-09-25, cluster `stable`): an
exhausted fix loop on `src/error.rs::context` wrote the note "[E0277] I tried
passing self.into_boxed_dyn_error() into a generic Error bound … use self
directly, not a boxed dyn conversion" at 11:10. The loop never compiled; the
extraction prompt it reused ordered "State the correction as a rule", so the
cure was invented. The same unit then failed E0277 by passing self directly
in fills 5, 6 and 7 with the note twice in its prompt, and no rule could
retire it (no rejected symbol, and not E0599/E0425).

The plan's sentences, each with its control:
- an exhausted loop's note is asked for without a correction
  (control: a recovered loop's note is still asked for one);
- an unvalidated lesson learned on a unit, shown in its prompt, is retired
  when that unit fails again with the lesson's code
  (controls: validated, another unit, not shown, another code — kept);
- the live record, as stored, is retired by the live failure.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.model_notes_store import FileModelNotesStore
from pipeline_core.domain.model_notes import (
    NoteProvenance,
    disproved_lessons,
    note_hash,
)

HEADER = "## Model Notes — Confusions to Avoid"

# The live note and its sidecar entry, verbatim from
# tmp/anyhow/functional_requirements_anyhow.md(.note_provenance.json).
_LIVE_NOTE = (
    "[E0277] I tried passing self.into_boxed_dyn_error() into a generic "
    "Error bound because I assumed boxing would satisfy the bound, but dyn "
    "Error is unsized so the Sized requirement fails; use self directly, "
    "not a boxed dyn conversion.")
_LIVE_PROV = {"unit": "src/error.rs::context", "error_codes": ["E0277"],
              "rejected_symbols": [], "failed_body_hash": "dc9d9357c374009b"}
_UNIT = "src/error.rs::context"


class _Ask:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def ask(self, prompt, system=None, model=None, max_tokens=None,
            think=None, temperature=None):
        self.prompts.append(prompt)
        return "I tried X because Y."


def _store(ask=None, log=None) -> FileModelNotesStore:
    return FileModelNotesStore(
        ask or _Ask(), "m", log or (lambda *a, **k: None),
        max_chars=4000, max_entries=80, failed_body_cap=2000, fix_err_cap=2000)


def _live_fr(tmp_path: Path) -> Path:
    fr = tmp_path / "functional_requirements_x.md"
    fr.write_text(f"# FR\n\n{HEADER}\n\n- {_LIVE_NOTE}\n")
    side = fr.with_name(fr.name + ".note_provenance.json")
    side.write_text(json.dumps({note_hash(_LIVE_NOTE): _LIVE_PROV}))
    return fr


# ---- the writer ----


def test_an_exhausted_loop_is_not_asked_for_a_correction() -> None:
    ask = _Ask()
    _store(ask).extract_self_note("Self::make(self)", "error[E0277]", "")
    assert "Do NOT propose a correction" in ask.prompts[0]
    assert "State the correction as a rule" not in ask.prompts[0]


def test_control_a_recovered_loop_is_still_asked_for_its_correction() -> None:
    ask = _Ask()
    _store(ask).extract_self_note("Self::make(self)", "error[E0277]",
                                  "Self::make(self.inner)")
    assert "State the correction as a rule" in ask.prompts[0]


def test_provenance_round_trips_whether_it_was_validated() -> None:
    p = NoteProvenance(unit=_UNIT, error_codes=("E0277",), rejected_symbols=(),
                       failed_body_hash="h", validated=False)
    assert NoteProvenance.from_dict(p.to_dict()) == p
    assert NoteProvenance.from_dict(_LIVE_PROV).validated is None


# ---- the reader ----


def _prov(**kw) -> NoteProvenance:
    base = {"unit": _UNIT, "error_codes": ("E0277",), "rejected_symbols": (),
            "failed_body_hash": "h", "validated": None}
    base.update(kw)
    return NoteProvenance(**base)


def _disproved(prov: NoteProvenance, *, unit: str = _UNIT,
               codes: frozenset[str] = frozenset({"E0277"}),
               shown: str = f"<MODEL_NOTES>\n- {_LIVE_NOTE}\n</MODEL_NOTES>"
               ) -> list[str]:
    return disproved_lessons([_LIVE_NOTE], {note_hash(_LIVE_NOTE): prov},
                             unit=unit, error_codes=codes, shown=shown)


def test_its_own_unit_failing_its_code_with_it_shown_retires_it() -> None:
    assert _disproved(_prov()) == [_LIVE_NOTE]
    assert _disproved(_prov(validated=False)) == [_LIVE_NOTE]


def test_controls_every_other_shape_keeps_it() -> None:
    assert _disproved(_prov(validated=True)) == []
    assert _disproved(_prov(), unit="src/error.rs::construct") == []
    assert _disproved(_prov(), codes=frozenset({"E0308"})) == []
    assert _disproved(_prov(), shown="<MODEL_NOTES>\n</MODEL_NOTES>") == []


def test_the_live_record_is_retired_on_disk_by_the_live_failure(
        tmp_path: Path) -> None:
    fr = _live_fr(tmp_path)
    logs: list[str] = []
    s = _store(log=logs.append)
    shown = fr.read_text()  # the notes as the fill-5 prompt carried them
    assert s.retire_disproved(fr, _UNIT, frozenset({"E0277"}), shown) == 1
    assert s.load(fr) == []
    side = json.loads(fr.with_name(fr.name + ".note_provenance.json").read_text())
    assert side == {}
    assert any("retired an unvalidated lesson" in m for m in logs)


def test_the_per_fn_unit_asks_for_the_retirement_on_a_compile_failure(
        tmp_path: Path) -> None:
    from dataclasses import replace

    from pipeline_core.domain.models import Stub
    from pipeline_core.domain.reconstruction import FnLocation, SpliceOutcome
    from tests.test_fill_engine_usecases import FakeFill, _ctx, _fill_one, _mk_ws
    ws = _mk_ws(tmp_path)
    fr = _live_fr(tmp_path)
    fake = FakeFill(
        loc_rec=FnLocation(0, 0, 5, "pub fn answer()", "unimplemented!()"),
        splice=SpliceOutcome(True, "ok", 4),
        run=(101, "error[E0277]: the trait bound is not satisfied"),
        u2=None, body=("Self::make(self)", "RESP"))
    _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                        ctx=replace(_ctx(ws), fr_path=fr))
    asked = fake.s["retire_disproved"]
    assert asked and all(a[1] == "lib.rs::answer" and a[2] == {"E0277"}
                         for a in asked)

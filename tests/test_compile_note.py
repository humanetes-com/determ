"""An owed function whose last body did not compile carries the compiler's
words into its next prompt, on every route.

Before, only the pair route read the stored error into a later fill's prompt
(its `LAST_ERROR`); per-fn, file and impl printed a compile error only
within the fill that produced it (run `14141-9a904822`: 59 compile reverts,
36 at a first attempt)."""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.retros import last_compile_errors, retrospectives_path
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.refill_evidence import compile_note, unit_notes_text

_U = FnLocator("src/error.rs", "construct_from_boxed")
_E0425 = "error[E0425]: cannot find value `STD_ERROR_VTABLE` in this scope"


def _store(tmp_path: Path, recs: dict) -> None:  # type: ignore[type-arg]
    p = retrospectives_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(recs), encoding="utf-8")


def test_the_store_names_each_units_last_compile_error(tmp_path: Path) -> None:
    _store(tmp_path, {
        "src/error.rs::construct_from_boxed::impl Error": {
            "last_compile_error": _E0425, "error_count": 1},
        "src/error.rs::msg::impl Error": {
            "last_error_preview": "panicked at …", "error_count": 0},
    })
    errs = last_compile_errors(tmp_path)
    assert errs == {_U: _E0425}, "a unit whose last body compiled has no entry"


def test_the_note_is_printed_for_the_unit_and_only_for_it() -> None:
    fr = FailureFrontier.build({_U: {"t"}}, {_U: {"t"}},
                               unit_notes={_U: compile_note(_U.fn_name, _E0425)})
    text = unit_notes_text((_U,), fr)
    assert "did not compile" in text and "STD_ERROR_VTABLE" in text
    other = FnLocator("src/error.rs", "msg")
    assert unit_notes_text((other,), fr) == ""
    assert unit_notes_text((_U,), FailureFrontier.build({_U: {"t"}}, {})) == ""

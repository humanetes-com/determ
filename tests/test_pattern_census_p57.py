"""P57 — undelivered repeat: a request answered "repeat — not re-sent" whose
only earlier answer sat in a follow-up the session's transcript bound refused.

The recorded shape is run `3323407-984b07f2` (2026-11-08) fill 3: two file
answers logged, the admission line, the refusal, then `src/fmt.rs` asked alone
and answered as a repeat. Negative controls: the same repeat after a batch
that was delivered (no refusal) is not a hit, and a session opening between
the refusal and the repeat resets.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p57", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()

_FMT = '<REQUEST kind="file" arg="src/fmt.rs"/>'
_ERR = '<REQUEST kind="file" arg="src/error.rs"/>'
_S = "session[4 test(s), 16 unit(s)]"


def _pull(tag: str, extra: str = "") -> str:
    return f"  [pull] {tag} → ok (3009 chars, lines 1-97 of 97{extra})"


_REFUSE = (f"  [phase-c/session] {_S}: requested context 43,477 chars would carry "
           "the transcript past its bound (122,899 of 192,000) — not sent, the "
           "session continues")
_ADMIT = "  [phase-c/session] FILE request admitted 1 unit(s) to the write set: src/fmt.rs::debug"
_REPEAT = ", repeat — not re-sent"


def test_the_recorded_shape_is_a_hit() -> None:
    lines = [_pull(_FMT), _pull(_ERR), _ADMIT, _REFUSE, _pull(_FMT, _REPEAT)]
    assert census.p57_undelivered_repeats(lines) == [_FMT]


def test_control_a_repeat_after_a_delivered_batch_is_not_a_hit() -> None:
    lines = [_pull(_FMT), _ADMIT, "  [llm] call done", _pull(_FMT, _REPEAT)]
    assert census.p57_undelivered_repeats(lines) == []
    # Delivered once, refused later, repeated: the earlier copy is above.
    lines = [_pull(_FMT), "x", _pull(_FMT, _REPEAT), _pull(_ERR), _REFUSE,
             _pull(_FMT, _REPEAT)]
    assert census.p57_undelivered_repeats(lines) == []


def test_control_a_session_opening_resets() -> None:
    lines = [_pull(_FMT), _REFUSE,
             f"  [phase-c/session] {_S}: opening — targets t; editable 1 unit(s)",
             _pull(_FMT, _REPEAT)]
    assert census.p57_undelivered_repeats(lines) == []

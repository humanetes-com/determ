"""Blocking bodies a pin pass did not pin.

One candidate pins the body its rebuilt panic lands in. The other stays
blocking and is asked again while it is still unimplemented. A body the
fill has written leaves the set.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.adapters.pin_gaps_store import (
    load_pin_gaps,
    pin_gaps_path,
    save_pin_gaps,
)
from pipeline_core.domain.models import FnSite
from pipeline_core.domain.pin_gaps import gaps_left, gaps_still_unimplemented

_A = FnSite("src/a.rs", "f", "impl A")
_B = FnSite("src/a.rs", "f", "impl B")


def test_gaps_left_keeps_blocking_order_and_drops_what_was_pinned() -> None:
    assert gaps_left((_A, _B), [_B]) == (_A,)
    assert gaps_left((_A, _B), [_A, _B]) == ()
    assert gaps_left((_A, _B), []) == (_A, _B)


def test_a_filled_body_leaves_the_gap() -> None:
    assert gaps_still_unimplemented((_A, _B), [_A]) == (_A,)
    assert gaps_still_unimplemented((_A, _B), []) == ()


def test_the_store_roundtrips_and_an_empty_set_removes_the_file(tmp_path: Path) -> None:
    save_pin_gaps(tmp_path, [_A, _B])
    assert load_pin_gaps(tmp_path) == (_A, _B)
    save_pin_gaps(tmp_path, [])
    assert load_pin_gaps(tmp_path) == ()
    assert not pin_gaps_path(tmp_path).exists()


def test_a_malformed_store_is_no_gap(tmp_path: Path) -> None:
    path = pin_gaps_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    assert load_pin_gaps(tmp_path) == ()

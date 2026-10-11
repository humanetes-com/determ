"""C1/C2 ranking on the recorded post-producer product of run 2220347-7fa9b90f.

The plan sentence: with the service bucket in the rank (`dispatch_rank.Service`),
src/lib.rs::format_err is in the first 12 and src/error.rs::construct_from_adhoc
is not. The recorded input is the ranking after the producer order (format_err
at slot 21, construct_from_adhoc at slot 8), not a hand-made list of
constructors; it is the static order the rank breaks ties on. Negative
control: the batch taken on that ranking alone, which is what the live loop
dispatched.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline_core.domain.dispatch_rank import UnitFacts, rank, service_of, take
from pipeline_core.domain.models import FnLocator

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "dispatch"
    / "2220347_product_service.json"
)


def _loc(s: str) -> FnLocator:
    file, fn = s.rsplit("::", 1)
    return FnLocator(file, fn)


def _load() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _ordered(data: dict) -> list[FnLocator]:
    return [_loc(s) for s in data["after_producers"]]


def _ranked(keys: list[FnLocator], served: set[FnLocator],
            flat: set[FnLocator], dead: set[FnLocator]) -> list[FnLocator]:
    return rank({k: UnitFacts(static_index=i, service=service_of(
        k, served=served, flat=flat, dead=dead)) for i, k in enumerate(keys)})


def _batch(keys: list[FnLocator]) -> tuple[FnLocator, ...]:
    return take(keys, (), 12, cohort=None, traversed=()).keys


def test_recorded_post_producer_ranking_is_the_negative_control() -> None:
    """Without promote_by_service the first 12 is the constructor head the
    live loop already re-bought: construct_from_adhoc in, format_err out."""
    data = _load()
    keys = _ordered(data)
    assert keys.index(_loc("src/lib.rs::format_err")) + 1 == data[
        "format_err_rank_before"]
    assert keys.index(_loc("src/error.rs::construct_from_adhoc")) + 1 == data[
        "construct_from_adhoc_rank_before"]
    names = {f"{k.file}::{k.fn_name}" for k in _batch(keys)}
    assert "src/error.rs::construct_from_adhoc" in names
    assert "src/lib.rs::format_err" not in names


def test_the_service_bucket_puts_format_err_in_the_first_12() -> None:
    """The plan's claim, on the recorded post-producer ranking: format_err
    enters the cap and construct_from_adhoc leaves it."""
    data = _load()
    ranked = _ranked(
        _ordered(data),
        {_loc(s) for s in data["served"]},
        {_loc(s) for s in data["flat"]},
        {_loc(s) for s in data["dead"]},
    )
    names = [f"{k.file}::{k.fn_name}" for k in _batch(ranked)]
    assert "src/lib.rs::format_err" in names
    assert "src/error.rs::construct_from_adhoc" not in names


def test_dead_bodies_do_not_occupy_the_cap() -> None:
    """C2 on the recorded product: never-served dead units occupy the cap
    without the dead bucket; with it they do not. format_err still in.

    The fixture's dead list is computed (every body under the name unexecuted).
    src/ensure.rs::write_str and src/fmt.rs::write_str have a live sibling
    and stay never-served — they must remain in the first 12. src/error.rs::
    backtrace is not in the dead list: it is live at a different line and is
    an earned frontier unit.
    """
    data = _load()
    assert "src/error.rs::backtrace" not in data["dead"]
    assert "src/ensure.rs::write_str" not in data["dead"]
    assert "src/fmt.rs::write_str" not in data["dead"]
    occupy_without = [
        "src/context.rs::write_str",
        "src/ensure.rs::write_str",
        "src/fmt.rs::write_str",
        "src/error.rs::erase",
    ]
    truly_dead = [
        "src/context.rs::write_str",
        "src/error.rs::erase",
    ]
    stay_never_served = [
        "src/ensure.rs::write_str",
        "src/fmt.rs::write_str",
    ]
    served = {_loc(s) for s in data["served"]}
    flat = {_loc(s) for s in data["flat"]}
    without = _ranked(_ordered(data), served, flat, set())
    without_batch = {f"{k.file}::{k.fn_name}" for k in _batch(without)}
    assert set(occupy_without) <= without_batch
    with_dead = _ranked(
        _ordered(data), served, flat, {_loc(s) for s in data["dead"]})
    with_batch = {f"{k.file}::{k.fn_name}" for k in _batch(with_dead)}
    assert not set(truly_dead) & with_batch
    assert set(stay_never_served) <= with_batch
    assert "src/lib.rs::format_err" in with_batch
    assert "src/error.rs::construct_from_adhoc" not in with_batch


def test_empty_service_inputs_are_a_noop() -> None:
    """Walks and a virgin cluster pass empty served/flat/dead; the ranking
    must stay the post-producer order."""
    data = _load()
    ordered = _ordered(data)
    assert _ranked(ordered, set(), set(), set()) == ordered

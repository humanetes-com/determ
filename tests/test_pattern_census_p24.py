"""P24 — crash-cohort lock: the cohort is larger than the batch, every
graded unit is exempt from the pass cursor, the head is re-bought.

Plan sentence: on consecutive sweeps of one run, the crash-evidence batch
line's units are a subset of the previous sweep's while the chain cohort is
larger than the dispatch cap — LOCKED. The recorded run
(1577437-c93d5c26, 2026-09-06) was SIGTERMed inside its first K > cap
batch, so its shape is the lock ARMED: iter 4 `63 unit(s) … 12 of them are
in this batch`, `(12/68 this pass` (attempted_before=0), iter 3
chain-derived blame for 63 fn(s), no later sweep. The batch line names 8 of
the 12; the `phase_c.fill.schedule` event carries all 12.

Negative controls: a K > M sweep whose next batch carries a new name (the
cursor walking — the reading the provenance-scoped exemption must produce)
is not a hit; a K <= M cohort re-dispatched identically (run
1212998-d61fbd2c iters 1-2, 5 of 5) is not a hit; a `0 of them` line is
not a hit.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census_p24", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()

_HEAD8 = ("src/ptr.rs::by_ref, src/error.rs::construct, "
          "src/error.rs::construct_from_adhoc, src/error.rs::construct_from_boxed, "
          "src/error.rs::construct_from_display, src/error.rs::construct_from_std, "
          "src/context.rs::context, src/error.rs::msg")
_HEAD12 = ["src/context.rs::context", "src/context.rs::ext_context",
           "src/error.rs::construct", "src/error.rs::construct_from_adhoc",
           "src/error.rs::construct_from_boxed", "src/error.rs::construct_from_display",
           "src/error.rs::construct_from_std", "src/error.rs::from", "src/error.rs::is",
           "src/error.rs::msg", "src/ptr.rs::by_ref", "src/ptr.rs::from_raw"]


def _batch(it: int, k: int, m: int, names: str) -> str:
    return (f"  [stable] [phase-c/tdd] iter {it}: {k} unit(s) carry crash evidence "
            f"this sweep and rank ahead of the frontier; {m} of them are in this "
            f"batch: {names}")


def _summary(it: int, done: int, of: int = 68, n: int = 12) -> str:
    return (f"  [stable] [phase-c/tdd] iter {it}: cargo test rc=101 (1.2s); pass=104 "
            f"fail=17 oracle=121 (improved=False, stall=0/0); frontier=70 fn(s) "
            f"(merged), 0 skipped as exhausted; unowned=0; dispatching {n} of {of} "
            f"({done}/{of} this pass, mid-pass).")


_CHAIN3 = ("  [stable] [phase-c/crash] iter 3: deterministic crash with no in-crate "
           "frame in any stack — synthesized CHAIN-DERIVED blame (provenance="
           "chain_derived; ranks above test attribution, below an observed frame) "
           "for 63 fn(s) on the crashed tests' construction chain: src/chain.rs::"
           "default, src/chain.rs::len…. Evidence pack armed (prompt + exhaustion "
           "void + frontier); on a fire it also marks the crash signature, which "
           "re-opens the component walk.")
_ITER2_BATCH = _batch(2, 7, 7, "src/error.rs::error, src/ptr.rs::as_ptr, "
                      "src/error.rs::deref, src/error.rs::object_ref, src/error.rs::fmt, "
                      "src/ptr.rs::deref, src/fmt.rs::debug")

RECORDED = [_ITER2_BATCH, _summary(2, 12, 67), _CHAIN3,
            _batch(4, 63, 12, _HEAD8), _summary(4, 12)]


def _run(lines: list[str], run_id: str = "t") -> object:
    run = census.Run(run_id, "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def _events(tmp_path: Path, run_id: str, batches: dict[int, list[str]],
            attempted: dict[int, int]) -> None:
    rows = []
    for it, ab in attempted.items():
        rows.append({"run_id": run_id, "event": "phase_c.tdd.iter",
                     "data": {"iter": it, "attempted_before": ab, "dispatched": 12}})
    for it, names in batches.items():
        rows.append({"run_id": run_id, "event": "phase_c.fill.schedule",
                     "data": {"iter": it, "runs": [
                         {"kind": "file", "targets": names[:8]},
                         {"kind": "per-fn", "targets": names}]}})
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    census.EVENTS_PATH = path
    census._COHORT_EVENTS = None


def test_recorded_shape_is_armed_with_the_full_head_from_the_schedule(tmp_path) -> None:
    _events(tmp_path, "t", {4: _HEAD12}, {2: 0, 4: 0})
    hits = census.detect_p24(_run(RECORDED), [])
    assert len(hits) == 1
    h = hits[0]
    assert h["grade"] == "ARMED"
    assert h["head"] == sorted(_HEAD12)
    assert "ARMED iter 4: crash cohort 63 unit(s) > batch 12" in h["evidence"]
    assert "12 unit(s):" in h["evidence"] and "partial" not in h["evidence"]
    assert "attempted_before=0 at iter(s) 2, 4" in h["evidence"]
    assert "1 chain-derived blame line(s): iter 3 for 63 fn(s)" in h["evidence"]
    assert "repeat is unread" in h["evidence"]
    assert h["refs"][:3] == ["log:4", "log:5", "log:3"]


def test_without_a_schedule_event_the_log_head_is_marked_partial(tmp_path) -> None:
    _events(tmp_path, "t", {}, {})
    hits = census.detect_p24(_run(RECORDED), [])
    assert len(hits) == 1
    assert "8 unit(s) (partial: log names only)" in hits[0]["evidence"]
    assert len(hits[0]["head"]) == 8


def test_the_head_repeated_on_the_next_sweep_is_locked(tmp_path) -> None:
    _events(tmp_path, "t", {4: _HEAD12, 5: _HEAD12}, {2: 0, 4: 0, 5: 0})
    lines = RECORDED + [_batch(5, 63, 12, _HEAD8), _summary(5, 12)]
    hits = census.detect_p24(_run(lines), [])
    assert len(hits) == 1
    assert hits[0]["grade"] == "LOCKED"
    assert "LOCKED iters 4->5" in hits[0]["evidence"]
    assert "2 consecutive sweep(s)" in hits[0]["evidence"]
    assert "attempted_before=0 at iter(s) 2, 4, 5" in hits[0]["evidence"]


def test_a_shrunken_head_that_is_still_a_subset_is_locked(tmp_path) -> None:
    fewer = [n for n in _HEAD12 if n != "src/ptr.rs::from_raw"]
    _events(tmp_path, "t", {4: _HEAD12, 5: fewer}, {4: 0, 5: 0})
    lines = RECORDED + [_batch(5, 40, 11, _HEAD8), _summary(5, 11, n=11)]
    hits = census.detect_p24(_run(lines), [])
    assert len(hits) == 1 and hits[0]["grade"] == "LOCKED"
    assert "crash cohort 63/40 unit(s) > batch 12" in hits[0]["evidence"]


def test_the_cursor_walking_past_the_head_is_not_a_hit(tmp_path) -> None:
    """The plan's fixture claim for the provenance-scoped exemption: the
    batch advances past the constructors on the second iteration."""
    tail = ["src/error.rs::new", "src/error.rs::erase", "src/error.rs::chain",
            "src/error.rs::root_cause", "src/error.rs::source", "src/error.rs::fmt",
            "src/error.rs::deref", "src/error.rs::downcast", "src/kind.rs::new",
            "src/lib.rs::Ok", "src/lib.rs::format_err", "src/ensure.rs::new"]
    _events(tmp_path, "t", {4: _HEAD12, 5: tail}, {2: 0, 4: 0, 5: 12})
    lines = RECORDED + [_batch(5, 63, 12, ", ".join(tail[:8])), _summary(5, 24)]
    assert census.detect_p24(_run(lines), []) == []


def test_a_cohort_under_the_cap_re_dispatched_identically_is_not_a_hit(tmp_path) -> None:
    """Run 1212998-d61fbd2c iters 1-2: 5 of 5 every sweep, the frame-blame
    shape the exemption was written for."""
    _events(tmp_path, "t", {}, {1: 7, 2: 0})
    five = ("src/error.rs::drop, src/error.rs::vtable, src/error.rs::error, "
            "src/context.rs::source, src/error.rs::object_drop")
    lines = [_batch(1, 5, 5, five), _summary(1, 14, 69),
             _batch(2, 5, 5, five), _summary(2, 12, 67)]
    assert census.detect_p24(_run(lines), []) == []


def test_graded_units_absent_from_the_batch_are_not_a_tail(tmp_path) -> None:
    _events(tmp_path, "t", {}, {})
    none = ("  [stable] [phase-c/tdd] iter 1: 3 unit(s) carry crash evidence this "
            "sweep and rank ahead of the frontier; 0 of them are in this batch — "
            "the batch is full of higher-graded evidence.")
    off = ("  [stable] [phase-c/tdd] iter 2: 3 unit(s) carry crash evidence this "
           "sweep and rank ahead of the frontier; 2 of them are in this batch: "
           "src/error.rs::drop, src/ptr.rs::as_ptr")
    lines = [none, _summary(1, 12), off, _summary(2, 12)]
    assert census.detect_p24(_run(lines), []) == []


def test_an_extended_batch_wording_still_parses() -> None:
    line = ("  [stable] [phase-c/tdd] iter 4: 63 unit(s) carry crash evidence this "
            "sweep (3 frame-derived, 60 chain-derived) and rank ahead of the "
            "frontier; 12 of them are in this batch: " + _HEAD8)
    pb = census._parse_crash_batch(line)
    assert pb == {"k": 63, "m": 12, "iter": 4, "names": _HEAD8.split(", ")}


def test_registered() -> None:
    assert "P24" in census.DETECTORS and "P24" in census.CLASS_NAMES
    assert "P24" in census.REACTIONS

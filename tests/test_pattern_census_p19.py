"""P19 — mechanical write left an unparseable tree; P20 — site-cardinality leak
into the re-seed.

Plan sentences: a sweep or refusal line carrying an UNCODED rustc diagnostic
under src/ is a census row naming the last mechanical writer before it; a
coded error is not the class. A re-seed event whose seeded/rejected list
repeats a key is a P20 row. Negative controls: `error[E0308]` on the same
line shape is not P19; a re-seed event with distinct keys is not P20.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _census():
    path = Path(__file__).resolve().parents[1] / "scripts" / "pattern_census.py"
    spec = importlib.util.spec_from_file_location("pattern_census", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _census()

_RESEED = ("  [stable] [phase-c/ledger] iter 0: cluster entry — re-seeded 0 unit(s) "
           "(0 proven, 0 unmeasured); skipped 59 (oracle moved); 7 rejected "
           "(did not build); 11 cargo check(s).")
_SHRUNK = ("  [stable] [phase-c/tdd] iter 1: ORACLE SHRUNK — counting 127 as failing. "
           "121 named test(s) reported no verdict (cluster compile failed — no test "
           "binaries ran); 1 distinct rustc error(s): src/context.rs:35: error: "
           "unexpected closing delimiter: `)`; 6 unnamed test(s) short")
_CODED = ("  [stable] [phase-c/tdd] iter 1: ORACLE SHRUNK — counting 127 as failing. "
          "1 distinct rustc error(s): src/context.rs:35: error[E0308]: mismatched types")


def _run(*lines: str, run_id: str = "t") -> object:
    run = census.Run(run_id, "g", "c", "ts", "start:1")
    run.lines = [(f"log:{i}", ln) for i, ln in enumerate(lines, 1)]
    return run


def test_p19_uncoded_diagnostic_after_a_re_seed_names_the_writer() -> None:
    """Run 2973-66e50ad9: the cluster-entry re-seed, then the parse error."""
    hits = census.detect_p19(_run(_RESEED, "  [stable] [phase-c/tdd] iter 1: running", _SHRUNK), [])
    assert len(hits) == 1
    ev = hits[0]["evidence"]
    assert "src/context.rs:35: error: unexpected closing delimiter" in ev
    assert "re-seeded" in ev, "the writer is named"
    assert hits[0]["refs"] == ["log:1", "log:3"]


def test_p19_without_a_writer_line_still_reports_the_break() -> None:
    hits = census.detect_p19(_run(_SHRUNK), [])
    assert len(hits) == 1
    assert "writer not named" in hits[0]["evidence"]


def test_p19_a_coded_error_is_not_the_class() -> None:
    assert census.detect_p19(_run(_RESEED, _CODED), []) == []
    assert census.RE_UNCODED_SRC.search(_CODED) is None


def test_p19_the_same_break_is_one_row_however_many_sweeps_repeat_it() -> None:
    hits = census.detect_p19(_run(_RESEED, _SHRUNK, _SHRUNK, _SHRUNK), [])
    assert len(hits) == 1


def test_p19_boundary_heal_is_the_reaction() -> None:
    assert any(m == "boundary-heal" for _, m in census.REACTIONS["P19"])
    assert "P19" in census.DETECTORS and "P19" in census.CLASS_NAMES


def test_p20_repeated_keys_in_a_re_seed_event_are_the_leak(tmp_path, monkeypatch) -> None:
    ev_path = tmp_path / "events.jsonl"
    rows = [
        {"run_id": "r1", "kind": "phase_c.ledger.reseed",
         "data": {"site": "cluster entry", "seeded": [],
                  "rejected": ["src/context.rs::ext_context", "src/context.rs::ext_context",
                               "src/ensure.rs::render", "src/error.rs::from",
                               "src/error.rs::from", "src/error.rs::from",
                               "src/error.rs::from"],
                  "skipped": ["src/wrapper.rs::fmt: oracle moved",
                              "src/wrapper.rs::fmt: oracle moved"]}},
        {"run_id": "r2", "kind": "phase_c.ledger.reseed",
         "data": {"site": "cluster entry",
                  "seeded": ["src/error.rs::from", "src/ensure.rs::render"],
                  "rejected": [], "skipped": ["a: oracle moved", "a: oracle moved"]}},
    ]
    ev_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(census, "EVENTS_PATH", ev_path)
    monkeypatch.setattr(census, "_RESEED_EVENTS", None)
    hits = census.detect_p20(_run(run_id="r1"), [])
    assert len(hits) == 1
    assert "rejected repeats src/context.rs::ext_context, src/error.rs::from" in hits[0]["evidence"]
    assert "7 element(s), 3 distinct" in hits[0]["evidence"]
    # control: distinct keys are not the class, and a repeated SKIP is not
    # either (one row per site the gate refused is the skip list's contract)
    assert census.detect_p20(_run(run_id="r2"), []) == []
    assert "P20" in census.DETECTORS and "P20" in census.CLASS_NAMES

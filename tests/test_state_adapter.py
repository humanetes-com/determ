"""Pins pipeline_core/adapters/state.py (pipeline_state.md store).

Pins the regex-free line parser against the legacy pattern's semantics
(`[key]  status  hash=H  detail`) and the read/mark/set_current round-trip.
"""

from pathlib import Path

import pytest

from pipeline_core.adapters.state import (
    _parse_phase_line,
    log_state_summary,
    mark_phase,
    phase_valid,
    read_state,
    set_current,
    state_path,
    write_state,
)


def test_parse_phase_line_semantics() -> None:
    # The canonical line write_state produces.
    assert _parse_phase_line("[task0b]  done          hash=ab12  3 clusters") == \
        ("task0b", "done", "ab12", "3 clusters")
    # Minimal whitespace, no detail.
    assert _parse_phase_line("[t1/mod] in_progress hash=-") == \
        ("t1/mod", "in_progress", "-", "")
    # Leading/trailing whitespace tolerated (legacy parser .strip()ped).
    assert _parse_phase_line("  [k] s hash=h  d1 d2  ") == ("k", "s", "h", "d1 d2")
    # Detail keeps internal spacing.
    assert _parse_phase_line("[k] s hash=h a  b") == ("k", "s", "h", "a  b")
    # Rejections — all matched the legacy pattern's failure cases:
    assert _parse_phase_line("current: t2c/anyhow") is None
    assert _parse_phase_line("# comment") is None
    assert _parse_phase_line("") is None
    assert _parse_phase_line("[] s hash=h") is None        # empty key
    assert _parse_phase_line("[k no close") is None         # no `]`
    assert _parse_phase_line("[k]s hash=h") is None         # no \s+ after `]`
    assert _parse_phase_line("[k] hash=h") is None          # status missing
    assert _parse_phase_line("[k] s hash=") is None         # empty hash value
    assert _parse_phase_line("[k] s nohash x") is None      # hash= prefix missing
    print("  OK  state line parser")


def test_state_round_trip(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    assert read_state(proj) == {"phases": {}, "current": ""}
    assert not phase_valid(proj, "task0b")

    mark_phase(proj, "task0b", "done", detail="3 clusters", source_hash="ab12")
    mark_phase(proj, "task2b/anyhow", "in_progress")
    set_current(proj, "task2c/anyhow")

    st = read_state(proj)
    assert st["phases"]["task0b"] == {"status": "done", "hash": "ab12",
                                      "detail": "3 clusters"}
    assert st["phases"]["task2b/anyhow"]["status"] == "in_progress"
    assert st["current"] == "task2c/anyhow"
    assert phase_valid(proj, "task0b")
    assert not phase_valid(proj, "task2b/anyhow")   # in_progress ≠ done
    # current_hash is a back-compat shim — ignored.
    assert phase_valid(proj, "task0b", current_hash="ZZZZ")

    # mark_phase is read-modify-write: earlier entries survive.
    mark_phase(proj, "task2b/anyhow", "done", source_hash="cd34")
    st2 = read_state(proj)
    assert st2["phases"]["task0b"]["detail"] == "3 clusters"
    assert phase_valid(proj, "task2b/anyhow")
    assert st2["current"] == "task2c/anyhow"        # preserved across marks
    print("  OK  state round trip")


def test_write_state_atomic_no_tmp_left(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    write_state(proj, {"phases": {"k": {"status": "done", "hash": "h",
                                        "detail": ""}}, "current": "x"})
    assert state_path(proj).exists()
    assert not list(proj.glob("*.tmp"))             # os.replace consumed it
    assert read_state(proj)["phases"]["k"]["status"] == "done"
    print("  OK  state atomic write")


def test_mark_phase_lock_failure_falls_back_to_unlocked_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the advisory lock is unavailable (flock raises) the write still lands —
    the atomic os.replace is the second line of defence (state.py:127-138)."""
    proj = tmp_path / "p"
    proj.mkdir()
    monkeypatch.setattr("fcntl.flock", lambda *a, **k: (_ for _ in ()).throw(OSError("no lock")))
    mark_phase(proj, "task0a", "done", detail="d", source_hash="h1")
    assert read_state(proj)["phases"]["task0a"] == {"status": "done", "hash": "h1", "detail": "d"}


def test_log_state_summary_all_branches(tmp_path: Path) -> None:
    """log_state_summary renders valid/STALE/resumed/pending symbols + the last
    position line; empty state reports a fresh run (state.py:164-184)."""
    proj = tmp_path / "p"
    proj.mkdir()
    captured: list[str] = []
    import pipeline_core.log as log_mod
    orig_log, orig_emit = log_mod._log_sink, log_mod._emit_sink
    log_mod.set_sinks(captured.append, orig_emit)
    try:
        # Empty state → fresh-run line.
        log_state_summary(proj, current_hash="X")
        assert any("fresh run" in m for m in captured)
        captured.clear()

        write_state(proj, {"phases": {
            "task0a": {"status": "done", "hash": "X", "detail": "valid one"},
            "task0b": {"status": "done", "hash": "OLD", "detail": "stale one"},
            "task1":  {"status": "in_progress", "hash": "-", "detail": "resuming"},
            "task2":  {"status": "pending", "hash": "-", "detail": "later"},
        }, "current": "task1/mod"})
        log_state_summary(proj, current_hash="X")
        blob = "\n".join(captured)
        assert "valid" in blob and "STALE" in blob          # hash match vs mismatch
        assert "resumed" in blob and "pending" in blob       # in_progress + pending
        assert "✓" in blob and "✗" in blob and "~" in blob   # the symbol set
        assert "last position: task1/mod" in blob
    finally:
        log_mod.set_sinks(orig_log, orig_emit)

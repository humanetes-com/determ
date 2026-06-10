"""Slice 9 pins — pipeline_core/adapters/state.py (pipeline_state.md store).

Pins the regex-free line parser against the legacy pattern's semantics
(`[key]  status  hash=H  detail`) and the read/mark/set_current round-trip.
"""

from pathlib import Path

from pipeline_core.adapters.state import (
    _parse_phase_line,
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

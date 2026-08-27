"""rotate_handover.sh owns the next session number and the pin paste.

The writer used to grep handover.md for `session N`, add one, and paste pins
from handover_pinned.md. That is reading the artefact to recover a number the
first line already carried, and it is how a pin got dropped. These tests pin
the script's stdout and the stub it writes — a hand-made 'session 369' string
would pass by construction and fail on nothing.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "rotate_handover.sh"
STUB_TITLE = "UNWRITTEN — replace this title"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ROTATE_HANDOVER_ROOT"] = str(root)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _handover(session: int, body: str, pins: str = "") -> str:
    return (
        f"# Handover — session {session} (2026-08-27) — **old session {session}**\n\n"
        f"{body}\n"
        f"{pins}"
    )


def _pins() -> str:
    return (
        "<!-- PINNED:BEGIN score-history -->\n"
        "standing score history\n"
        "<!-- PINNED:END score-history -->\n"
        "<!-- PINNED:BEGIN interactive-run -->\n"
        "watcher note\n"
        "<!-- PINNED:END interactive-run -->\n"
    )


def test_writes_the_next_session_stub_with_pins_already_in_the_file(tmp_path: Path) -> None:
    """Computed: session 368 rotates to a stub whose first line is 369, whose
    body is empty sections, and whose pins are the ones the old file carried —
    not a leftover in handover_pinned.md the writer has to paste. Negative
    control: the archived copy must not contain PINNED:BEGIN."""
    (tmp_path / "handover.md").write_text(_handover(368, "## ON RETURN\nbody", _pins()), encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "next session 369" in r.stdout
    assert "pinned 2" in r.stdout
    assert "do not grep for the number" in r.stdout
    stub = (tmp_path / "handover.md").read_text(encoding="utf-8")
    first = stub.splitlines()[0]
    assert first.startswith("# Handover — session 369 (")
    assert first.endswith(f"— **{STUB_TITLE}**")
    assert "## ON RETURN" in stub
    assert "## State at handover" in stub
    assert "## Key facts" in stub
    assert stub.count("PINNED:BEGIN") == 2
    assert "standing score history" in stub
    archived = (tmp_path / "previous_handovers.md").read_text(encoding="utf-8")
    assert "PINNED:BEGIN" not in archived
    assert "**old session 368**" in archived
    assert "session 368" in archived
    pinned = (tmp_path / "handover_pinned.md").read_text(encoding="utf-8")
    assert pinned.count("PINNED:BEGIN") == 2


def test_a_second_run_on_the_stub_is_a_noop(tmp_path: Path) -> None:
    """Rotating the unwritten stub would archive a session that never existed
    and bump the number again. The number the first run printed is still the
    number in the file."""
    (tmp_path / "handover.md").write_text(_handover(368, "body", _pins()), encoding="utf-8")
    assert _run(tmp_path).returncode == 0
    before = (tmp_path / "handover.md").read_text(encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "already rotated" in r.stdout
    assert "next session 369" in r.stdout
    assert (tmp_path / "handover.md").read_text(encoding="utf-8") == before
    # One archive entry, not two.
    assert (tmp_path / "previous_handovers.md").read_text(encoding="utf-8").count("old session 368") == 1


def test_filling_the_stub_then_rotating_advances_again(tmp_path: Path) -> None:
    """The number is monotonic off the first line, not off a grep of the
    archive. A filled 369 rotates to a 370 stub."""
    (tmp_path / "handover.md").write_text(_handover(368, "body", _pins()), encoding="utf-8")
    assert _run(tmp_path).returncode == 0
    stub = (tmp_path / "handover.md").read_text(encoding="utf-8")
    filled = stub.replace(STUB_TITLE, "what 369 established", 1)
    (tmp_path / "handover.md").write_text(filled, encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "next session 370" in r.stdout
    first = (tmp_path / "handover.md").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# Handover — session 370 (")
    archived = (tmp_path / "previous_handovers.md").read_text(encoding="utf-8")
    assert "what 369 established" in archived
    assert "PINNED:BEGIN" not in archived


def test_already_archived_but_unreplaced_file_still_gets_the_stub(tmp_path: Path) -> None:
    """Crash between archive and stub used to leave handover.md on the old
    session. A re-run must not duplicate the archive, and must still write
    session N+1."""
    (tmp_path / "handover.md").write_text(_handover(368, "body", _pins()), encoding="utf-8")
    assert _run(tmp_path).returncode == 0
    # Put the old file back, as if the stub write never happened.
    (tmp_path / "handover.md").write_text(_handover(368, "body", _pins()), encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "already archived" in r.stdout
    assert "next session 369" in r.stdout
    first = (tmp_path / "handover.md").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# Handover — session 369 (")
    assert (tmp_path / "previous_handovers.md").read_text(encoding="utf-8").count("old session 368") == 1


def test_a_pinless_handover_carries_the_mirrored_pins_rather_than_dropping_them(tmp_path: Path) -> None:
    """The paste used to be a second step. A handover that forgot its pins
    still has them in handover_pinned.md; the stub must not write over that
    with nothing."""
    (tmp_path / "handover.md").write_text(_handover(368, "body", ""), encoding="utf-8")
    (tmp_path / "handover_pinned.md").write_text(_pins(), encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "carrying from" in r.stdout
    stub = (tmp_path / "handover.md").read_text(encoding="utf-8")
    assert stub.count("PINNED:BEGIN") == 2


def test_refuses_a_first_line_that_is_not_a_session_header(tmp_path: Path) -> None:
    (tmp_path / "handover.md").write_text("# not a handover\n", encoding="utf-8")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "unexpected first line" in r.stderr


def test_script_is_executable() -> None:
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_mode & stat.S_IXUSR

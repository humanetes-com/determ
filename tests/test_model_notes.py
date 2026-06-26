"""Unit tests for the FR model-notes domain (P6 prerequisite slice) — pins the
behaviour of the regex→structured-scan migration of `slice_model_notes`."""
from __future__ import annotations

from pipeline_core.domain.model_notes import (
    MODEL_NOTES_HEADER as H,
)
from pipeline_core.domain.model_notes import (
    errorimpl_object_note,
    is_errorimpl_container,
    parse_note_bullets,
    preserve_model_notes,
    render_with_notes,
    slice_model_notes,
)


def test_slice_absent_returns_text_and_empty() -> None:
    body, notes = slice_model_notes("# FR\n\nsome body\n")
    assert body == "# FR\n\nsome body\n" and notes == ""


def test_slice_to_eof() -> None:
    text = f"# FR\n\nbody\n\n{H}\n\n- note one\n"
    body, notes = slice_model_notes(text)
    assert body == "# FR\n\nbody\n"
    assert notes.startswith(H) and "- note one" in notes


def test_slice_stops_at_next_top_heading_not_subheading() -> None:
    # A `### ` subheading inside the notes must NOT end the section; the next
    # `## ` top-level heading must (this is exactly what the old \n##\s did).
    text = (f"# FR\n\nbody\n\n{H}\n\n- a\n### sub still notes\n- b\n"
            "## Other Section\n\ntail\n")
    body, notes = slice_model_notes(text)
    assert "### sub still notes" in notes and "- b" in notes
    assert "## Other Section" not in notes
    assert "## Other Section" in body and "tail" in body


def test_parse_bullets_joins_continuations() -> None:
    _, notes = slice_model_notes(f"{H}\n\n- first line\n  continued\n\n- second\n")
    assert parse_note_bullets(notes) == ["first line continued", "second"]


def test_preserve_reattaches_old_notes() -> None:
    old = f"# FR\n\nold body\n\n{H}\n\n- keep me\n"
    new = "# FR\n\nregenerated body\n"
    out = preserve_model_notes(old, new)
    assert "regenerated body" in out and "- keep me" in out
    # idempotent header (no duplicate notes section)
    assert out.count(H) == 1


def test_render_round_trips() -> None:
    out = render_with_notes("# Functional Requirements\n\nbody\n", ["x", "y"])
    assert out.count(H) == 1
    _, notes = slice_model_notes(out)
    assert parse_note_bullets(notes) == ["x", "y"]


# D/R2 — ErrorImpl _object field note tests
def test_errorimpl_container_detection_positive() -> None:
    assert is_errorimpl_container("impl<E> StdError for ErrorImpl<E>")
    assert is_errorimpl_container("impl ErrorImpl")
    assert is_errorimpl_container("impl<E> ErrorImpl<E>")


def test_errorimpl_container_detection_negative() -> None:
    assert not is_errorimpl_container("impl Error for MyError")
    assert not is_errorimpl_container("impl<E> Display for Wrapper<E>")
    assert not is_errorimpl_container("")


def test_errorimpl_note_has_two_or_more_wrong_right_pairs() -> None:
    note = errorimpl_object_note()
    assert note.count("WRONG:") >= 2
    assert note.count("RIGHT:") >= 2


def test_errorimpl_note_names_object_field() -> None:
    note = errorimpl_object_note()
    assert "_object" in note


def test_errorimpl_note_round_trips_through_render() -> None:
    note = errorimpl_object_note()
    out = render_with_notes("# FR\n\nbody\n", [note])
    _, notes_section = slice_model_notes(out)
    bullets = parse_note_bullets(notes_section)
    assert len(bullets) == 1
    assert "_object" in bullets[0]
    assert bullets[0].count("WRONG:") >= 2

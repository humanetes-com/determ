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


# --- relocated note relevance / dedup / promotion rules (model-notes cluster) ---
from collections import Counter

from pipeline_core.domain.model_notes import (
    build_promoted_rules,
    dedup_append,
    is_wellformed_error_code,
    mine_common_tokens,
    note_hash,
    notes_are_semantic_dupes,
    prepare_note,
    rank_notes,
    strip_model_notes_section,
)


def test_is_wellformed_error_code() -> None:
    assert is_wellformed_error_code("E0277")
    assert is_wellformed_error_code("E044")      # 3 digits
    assert not is_wellformed_error_code("E02")   # 2 digits
    assert not is_wellformed_error_code("E02777")  # 5 digits
    assert not is_wellformed_error_code("0277")  # no E
    assert not is_wellformed_error_code("E027a")


def test_note_hash_normalises_whitespace_and_case() -> None:
    assert note_hash("Use  X   not Y") == note_hash("use x not y")
    assert note_hash("a") != note_hash("b")


def test_semantic_dupes_same_error_code() -> None:
    a = "[E0277] add where T: Sized"
    b = "for E0277 return &T instead of T by value"
    assert notes_are_semantic_dupes(a, b)


def test_semantic_dupes_keyword_overlap() -> None:
    a = "TypeId::of needs T: 'static generic lifetime"
    b = "the generic must be 'static for typeid; lifetime bound"
    assert notes_are_semantic_dupes(a, b)
    assert not notes_are_semantic_dupes("about display formatting", "ownership move clone")


def test_prepare_note_too_short_returns_none() -> None:
    assert prepare_note("short") is None
    assert prepare_note("") is None


def test_prepare_note_strips_bullet_and_tags_codes() -> None:
    out = prepare_note("- this is a long enough note to keep around", {"E0277", "bad", "E0599"})
    assert out == "[E0277,E0599] this is a long enough note to keep around"
    # already-tagged note isn't double-tagged
    assert prepare_note("[E0001] already tagged note long enough", {"E0277"}).startswith("[E0001]")


def test_dedup_append_literal_and_semantic_and_lru() -> None:
    existing = ["first note about lifetimes and generics here"]
    hashes = {note_hash(existing[0])}
    # literal dupe rejected
    _, added = dedup_append(existing[0], list(existing), set(hashes), 80)
    assert added is False
    # new accepted
    new, added = dedup_append("a totally different fresh note about ptr casts", list(existing), set(hashes), 80)
    assert added is True and len(new) == 2
    # LRU trims oldest
    notes = [f"distinct note number {i} padding padding padding" for i in range(5)]
    h = {note_hash(n) for n in notes}
    trimmed, added = dedup_append("brand new entry after the cap padding padding", notes, h, 5)
    assert added is True and len(trimmed) == 5 and trimmed[0] == notes[1]


def test_rank_notes_tier_ordering() -> None:
    generic = "generic recency-only note about wrappers and delegation"
    fmt_note = "remember to call fmt on the inner Display value before returning"
    ecode = "[E0277] T may be unsized; add where T: Sized or return &T instead"
    out = rank_notes(
        [generic, ecode, fmt_note],
        file_rel="src/error.rs", fn_name="fmt",
        file_text="struct Wrapper { }",
        current_error="error[E0277]: the trait bound `T: Sized` is not satisfied",
        err_codes={"E0277"}, max_chars=4000)
    lines = [ln[2:] for ln in out.splitlines()]
    assert lines[0].startswith("[E0277]")
    assert lines.index(fmt_note) < lines.index(generic)


def test_rank_notes_caps_on_bullet_boundary() -> None:
    notes = [f"note {i} about lifetimes generics sized bounds padding padding" for i in range(50)]
    out = rank_notes(notes, file_rel="a.rs", fn_name="f", file_text="",
                     current_error="", err_codes=set(), max_chars=200)
    assert 0 < len(out) <= 200
    assert all(ln.startswith("- ") for ln in out.splitlines())


def test_mine_common_tokens() -> None:
    ex = ["expected `MyWidget`, found `Other`", "the `MyWidget` type here too"]
    common = mine_common_tokens(ex)
    assert ("MyWidget", 2) in common
    assert mine_common_tokens([]) == []


def test_build_promoted_rules_threshold_and_past_note() -> None:
    assert build_promoted_rules(Counter({"E0277": 2}), {}, []) == ""
    out = build_promoted_rules(Counter({"E0277": 3}), {}, [])
    assert "E0277" in out and "seen 3" in out and "trait bound" in out
    past = build_promoted_rules(
        Counter({"E0277": 3}), {},
        ["[E0277] build via construct_from_display, do not call new"])
    assert "past note: build via construct_from_display" in past


def test_strip_model_notes_section() -> None:
    fr = "# FR\n\nbody.\n\n## Model Notes — x\n\n- a note\n\n## Other\n\ntail.\n"
    out = strip_model_notes_section(fr)
    assert "Model Notes" not in out
    assert "## Other" in out and "tail." in out
    assert strip_model_notes_section("# FR\nno notes\n") == "# FR\nno notes\n"

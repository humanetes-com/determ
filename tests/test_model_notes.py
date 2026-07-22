"""Unit tests for the FR model-notes domain (P6 prerequisite slice) — pins the
behaviour of the regex→structured-scan migration of `slice_model_notes`."""
from __future__ import annotations

from pipeline_core.domain.model_notes import (
    MODEL_NOTES_HEADER as H,
)
from pipeline_core.domain.model_notes import (
    layout_contradicted_notes,
    parse_note_bullets,
    preserve_model_notes,
    render_with_notes,
    slice_model_notes,
    struct_fields,
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


# --- layout-contradicted note retirement (reader-side) -----------------------
# A field fact persisted in a note can invert across a re-bake or differ per
# cluster; the file text of the attempt being prompted is the only truth.

AUTHORED_LAYOUT = """
pub(crate) struct ErrorImpl<E = ()> {
    pub(crate) vtable: &'static ErrorVTable,
    pub(crate) error: E,
}
// vtable of erased fns
struct ErrorVTable {
    object_drop: unsafe fn(Own<ErrorImpl>),
    object_ref: unsafe fn(Ref<ErrorImpl>) -> Ref<dyn StdError + Send + Sync>,
}
"""

FROZEN_LAYOUT = """
#[repr(C)]
pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    backtrace: Option<Backtrace>,
    _object: E,
}
"""

# The exact note observed poisoning live fills: written when `_object` was the
# field, injected after a re-bake renamed it to `error`.
POISON_NOTE = (
    "[E0609] ErrorImpl<E> stores the wrapped error in field `_object: E`, not "
    "`error` or `inner`. WRONG: `self.error.source()` (no field `error` → "
    "E0609) / RIGHT: `self._object.source()`. WRONG: `self.inner.source()` "
    "(no field `inner` → E0609) / RIGHT: `self._object.source()`."
)


def test_struct_fields_reads_named_fields() -> None:
    assert struct_fields(AUTHORED_LAYOUT, "ErrorImpl") == {"vtable", "error"}
    assert struct_fields(AUTHORED_LAYOUT, "ErrorVTable") == {
        "object_drop", "object_ref"}
    assert struct_fields(FROZEN_LAYOUT, "ErrorImpl") == {
        "vtable", "backtrace", "_object"}


def test_struct_fields_no_evidence_cases() -> None:
    assert struct_fields(AUTHORED_LAYOUT, "Missing") is None
    assert struct_fields("struct Own<T: ?Sized>(NonNull<T>);", "Own") is None
    assert struct_fields("struct Marker;", "Marker") is None
    # exact-name only: `Error` must not match `ErrorImpl`
    assert struct_fields(AUTHORED_LAYOUT, "Error") is None


def test_struct_fields_skips_comments_and_attrs() -> None:
    text = """
struct S {
    // not_a_field: u8,
    /* also_not: u8, */
    #[cfg(feature = "std")]
    real: u8,
}
"""
    assert struct_fields(text, "S") == {"real"}


def test_poison_note_retired_when_field_renamed() -> None:
    # Written against the frozen `_object` layout, judged against the
    # re-authored `error` layout → its recommendation no longer exists.
    assert layout_contradicted_notes([POISON_NOTE], AUTHORED_LAYOUT) == [POISON_NOTE]


def test_poison_note_also_retired_on_frozen_layout_by_wrong_examples() -> None:
    # Bias to retire: its WRONG examples claim `error`/`inner`, absent from the
    # frozen layout too. The struct def in the prompt already carries the fact.
    assert layout_contradicted_notes([POISON_NOTE], FROZEN_LAYOUT) == [POISON_NOTE]


def test_note_consistent_with_current_layout_is_kept() -> None:
    good = ("ErrorImpl stores the payload in field `error`. "
            "RIGHT: `self.error.source()`.")
    assert layout_contradicted_notes([good], AUTHORED_LAYOUT) == []


def test_struct_literal_claim_checked_against_fields() -> None:
    lit = "initialize every field: `ErrorImpl { vtable, error, backtrace }`"
    assert layout_contradicted_notes([lit], AUTHORED_LAYOUT) == [lit]
    ok = "initialize every field: `ErrorImpl { vtable, error }`"
    assert layout_contradicted_notes([ok], AUTHORED_LAYOUT) == []


def test_notes_without_in_file_structs_are_kept() -> None:
    # `self.items` is a claim, but the note names no struct declared in this
    # file — no evidence to judge it against, so it is kept.
    general = ("[E0382] I tried accessing a collection after moving it; "
               "use `self.items` after cloning, not after the move.")
    assert layout_contradicted_notes([general], AUTHORED_LAYOUT) == []
    boxnote = "use `Box::leak` for 'static, not a stack borrow"
    assert layout_contradicted_notes([boxnote], AUTHORED_LAYOUT) == []


def test_method_calls_are_not_field_claims() -> None:
    note = "ErrorImpl: call `self.compute()` before reading `self.vtable`."
    assert layout_contradicted_notes([note], AUTHORED_LAYOUT) == []


def test_empty_inputs_keep_everything() -> None:
    assert layout_contradicted_notes([], AUTHORED_LAYOUT) == []
    assert layout_contradicted_notes([POISON_NOTE], "") == []


# --- relocated note relevance / dedup / promotion rules (model-notes cluster) ---
from collections import Counter

from pipeline_core.domain.model_notes import (
    build_promoted_rules,
    dedup_append,
    is_wellformed_error_code,
    mine_common_tokens,
    note_hash,
    note_signature,
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


# --- branch-completion tests (domain coverage ratchet) ---

def test_is_wellformed_error_code_three_and_four_digits() -> None:
    assert is_wellformed_error_code("E0001")   # 4 digits
    assert is_wellformed_error_code("E001")    # 3 digits
    assert not is_wellformed_error_code("E1")  # 1 digit


def test_note_signature_direct() -> None:
    codes, kw = note_signature("E0277 box ptr unsafe")
    assert "e0277" in codes
    assert {"box", "ptr", "unsafe"} <= kw


def test_semantic_dupes_single_keyword_overlap_is_not_dupe() -> None:
    # share exactly one keyword → inter < 2 → not a dupe
    assert not notes_are_semantic_dupes("use box here for it", "box and the trait stuff")


def test_semantic_dupes_low_jaccard_not_dupe() -> None:
    # >=2 shared but <50% jaccard → not a dupe
    a = "box ptr"                       # {box, ptr}
    b = "box ptr trait impl generic"    # {box, ptr, trait, impl, generic}
    assert not notes_are_semantic_dupes(a, b)  # 2/5 = 0.4 < 0.5


def test_prepare_note_without_error_codes() -> None:
    assert prepare_note("a plain note long enough to keep") == "a plain note long enough to keep"


def test_prepare_note_all_codes_malformed_no_tag() -> None:
    out = prepare_note("a long enough note with bad codes only", {"nope", "E1"})
    assert out == "a long enough note with bad codes only"


def test_dedup_append_rejects_semantic_dupe() -> None:
    existing = ["TypeId::of needs T: 'static generic lifetime"]
    hashes = {note_hash(existing[0])}
    _, added = dedup_append(
        "the generic must be 'static for typeid; lifetime bound",
        list(existing), set(hashes), 80)
    assert added is False


def test_rank_notes_empty_returns_blank() -> None:
    assert rank_notes([], file_rel="a.rs", fn_name="f", file_text="",
                      current_error="", err_codes=set(), max_chars=100) == ""


def test_rank_notes_keyword_tier_outranks_generic() -> None:
    kw_note = "watch the lifetime and the generic sized bound on the box"   # ≥2 kw
    generic = "a recency only note about delegation here"
    out = rank_notes([generic, kw_note], file_rel="a.rs", fn_name="f", file_text="",
                     current_error="E9999 lifetime generic sized box issue",
                     err_codes=set(), max_chars=4000)
    lines = [ln[2:] for ln in out.splitlines()]
    assert lines.index(kw_note) < lines.index(generic)


def test_rank_notes_symbol_tier_outranks_generic() -> None:
    sym_note = "the MyWidget value must be cloned before capture"
    generic = "a recency only note about ownership here"
    out = rank_notes([generic, sym_note], file_rel="c.rs", fn_name="render",
                     file_text="pub struct MyWidget { }", current_error="",
                     err_codes=set(), max_chars=4000)
    lines = [ln[2:] for ln in out.splitlines()]
    assert lines.index(sym_note) < lines.index(generic)


def test_mine_common_tokens_filters_skip_and_short() -> None:
    # `Box`/`Vec` are in the skip set; `Foo` is <4 chars with no generic → dropped;
    # `LongName` survives (≥2 across excerpts).
    ex = ["Box Vec Foo LongName here", "LongName again with Box"]
    common = mine_common_tokens(ex)
    names = {t for t, _ in common}
    assert "LongName" in names
    assert "Box" not in names and "Vec" not in names and "Foo" not in names


def test_mine_common_tokens_one_count_per_excerpt() -> None:
    # `Widget` twice in ONE excerpt counts once → below the n>=2 threshold
    assert mine_common_tokens(["Widget and Widget again"]) == []


def test_build_promoted_rules_with_shared_tokens() -> None:
    out = build_promoted_rules(
        Counter({"E0599": 3}),
        {"E0599": ["no method on MyType here", "MyType still wrong"]},
        [])
    assert "E0599" in out and "often on" in out and "`MyType`" in out


def test_build_promoted_rules_truncates_long_past_note() -> None:
    long_body = "x" * 300
    out = build_promoted_rules(
        Counter({"E0277": 3}), {}, [f"[E0277] {long_body}"])
    assert "..." in out


def test_build_promoted_rules_empty_and_below_threshold() -> None:
    from collections import Counter as _C
    assert build_promoted_rules(_C(), {}, []) == ""           # no counts
    assert build_promoted_rules(_C({"E0277": 1}), {}, []) == ""  # all below threshold


def test_strip_model_notes_section_at_eof() -> None:
    fr = "# FR\n\nbody.\n\n## Model Notes — x\n\n- a note\n"   # notes is last section
    out = strip_model_notes_section(fr)
    assert "Model Notes" not in out and "body." in out


def test_rank_notes_empty_file_rel() -> None:
    out = rank_notes(["just one recency note here about things"], file_rel="",
                     fn_name="", file_text="", current_error="", err_codes=set(),
                     max_chars=4000)
    assert out == "- just one recency note here about things"


def test_build_promoted_rules_unknown_code_default_hint() -> None:
    out = build_promoted_rules(Counter({"E9999": 3}), {}, [])
    assert "E9999" in out and "rustc --explain E9999" in out


def test_build_promoted_rules_skips_untagged_stored_note() -> None:
    # an untagged stored note (parse_error_code_note → None) is ignored
    out = build_promoted_rules(Counter({"E0277": 3}), {},
                               ["a plain untagged note with no code prefix"])
    assert "E0277" in out and "past note" not in out

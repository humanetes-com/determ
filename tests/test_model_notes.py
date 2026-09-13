"""Unit tests for the FR model-notes domain (P6 prerequisite slice) — pins the
behaviour of the regex→structured-scan migration of `slice_model_notes`."""
from __future__ import annotations

from pipeline_core.domain.model_notes import (
    MODEL_NOTES_HEADER as H,
)
from pipeline_core.domain.model_notes import (
    guidance_contradicts_layout,
    layout_contradicted_notes,
    layout_safe_guidance,
    parse_note_bullets,
    preserve_model_notes,
    render_with_notes,
    slice_model_notes,
    src_text_declaring,
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


def test_parse_bullets_keeps_continuations_as_lines() -> None:
    # NEWLINE-joined, not space-joined: a note's continuation is usually a Rust
    # body, and one run-on line is not code the model can read.
    _, notes = slice_model_notes(f"{H}\n\n- first line\n  continued\n\n- second\n")
    assert parse_note_bullets(notes) == ["first line\ncontinued", "second"]


def test_parse_bullets_keeps_column_zero_body_lines() -> None:
    # THE ROUND-TRIP DEFECT. A Rust body opens at column 0 (`#[cold]`,
    # `pub(crate) fn …`). The old reader kept only lines starting with two
    # spaces and dropped the rest silently, so every `[regression]` note lost
    # its body on the next append (a read-modify-write) and became an
    # unretirable accusation. This is the pin that fails on the pre-fix reader.
    # Column 0 is the shape the OLD writer put on disk — it rendered the note
    # verbatim after `- `, so the body kept its own indentation.
    section = (f"{H}\n\n- [regression] In error.rs, after `f` was written. "
               "Body applied:\n"
               "#[cold]\n"
               "pub(crate) fn f<M>(m: M) -> Self {\n"
               "    Self::g(MessageError(m))\n"
               "}\n"
               "Re-implement `f` so t keep passing.\n")
    (note,) = parse_note_bullets(section)
    assert "#[cold]" in note
    assert "pub(crate) fn f<M>(m: M) -> Self {" in note
    assert "Re-implement `f` so t keep passing." in note


def test_multiline_note_round_trips_through_the_fr_file() -> None:
    # Every FR append is load → dedup → save, so one lossy read destroys the
    # body permanently. Ten cycles must be a fixed point.
    note = ("[regression] In error.rs, after `f` was written. Body applied:\n"
            "#[cold]\n"
            "pub(crate) fn f<M>(m: M) -> Self {\n"
            "    Self::g(MessageError(m))\n"
            "}\n"
            "Re-implement `f` so t keep passing.")
    text = "# FR\n\nbody\n"
    for _ in range(10):
        text = render_with_notes(text, [note])
        _, section = slice_model_notes(text)
        assert parse_note_bullets(section) == [note]


def test_parse_bullets_does_not_swallow_a_heading() -> None:
    # `#` + SPACE is markdown structure and closes the note; `#[cold]` is a
    # Rust attribute and is body — the two must not be confused.
    _, notes = slice_model_notes(
        f"{H}\n\n- a\n### sub\n- b\n")
    assert parse_note_bullets(notes) == ["a", "b"]


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
    assert struct_fields("struct Marker;", "Marker") is None
    # exact-name only: `Error` must not match `ErrorImpl`
    assert struct_fields(AUTHORED_LAYOUT, "Error") is None


def test_struct_fields_reads_tuple_positions() -> None:
    # A tuple struct HAS fields, they are numbered — `self.0` is valid against
    # it, a named claim is not; both are unjudgeable if this returns None.
    assert struct_fields("struct Own<T: ?Sized>(NonNull<T>);", "Own") == {"0"}
    assert struct_fields("pub struct Pair(u8, Vec<(u8, u8)>);", "Pair") == {"0", "1"}
    assert struct_fields("struct Trailing(A, B,);", "Trailing") == {"0", "1"}
    assert struct_fields("struct Empty();", "Empty") == frozenset()
    # a top-level comma inside generics / a `->` in a fn-ptr type is not a
    # field separator
    assert struct_fields("struct One(Vec<A, B>);", "One") == {"0"}
    assert struct_fields(
        "struct Fp(unsafe fn(Own<E>) -> Ref<E>, u8);", "Fp") == {"0", "1"}
    # a field whose type STARTS with a bracket is still a field
    assert struct_fields("struct Arr([u8; 4], u8);", "Arr") == {"0", "1"}
    assert struct_fields("struct Tup((u8, u8), bool);", "Tup") == {"0", "1"}


def test_tuple_index_claims_judged_against_named_layout() -> None:
    # The live guidance that convicted two impl blocks: `self.0` against a
    # struct with NAMED fields is a layout lie, invisible to an ident-only sieve.
    g = ("Replace the non-existent field `_object` with `self.0`, as the "
         "generic `E` is stored in the first anonymous tuple field. Use the "
         "exact body `Debug::fmt(&self.0, formatter)`.")
    anchor = frozenset({"ErrorImpl"})  # the impl this guidance was earned on
    assert layout_contradicted_notes([g], AUTHORED_LAYOUT, anchor_types=anchor) == [g]
    # …and the same claim is CORRECT where the layout really is a tuple struct
    # (the store is shared across clusters, so both must be judged per file).
    g2 = "On `ErrorImpl`, use the exact body `Debug::fmt(&self.0, formatter)`."
    assert layout_contradicted_notes([g2], AUTHORED_LAYOUT) == [g2]
    assert layout_contradicted_notes([g2], "pub(crate) struct ErrorImpl<E>(E);") == []


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


def test_anchor_type_judges_prose_that_names_no_struct() -> None:
    # Live guidance that stayed injected (and kept a block convicted) because
    # it names no struct declared in the file: without an anchor there is
    # nothing to judge `self._object` against.
    g = ("The function `crate::nightly::provide` does not exist in scope; "
         "change the body to `self._object.provide(request)`, which is "
         "guaranteed to compile under the frozen `E: StdError` bound.")
    assert layout_contradicted_notes([g], AUTHORED_LAYOUT) == []
    assert layout_contradicted_notes(
        [g], AUTHORED_LAYOUT, anchor_types=frozenset({"ErrorImpl"})) == [g]
    # …and the anchor does not manufacture a contradiction where the field
    # really exists (same string, the cluster whose layout kept `_object`).
    assert layout_contradicted_notes(
        [g], FROZEN_LAYOUT, anchor_types=frozenset({"ErrorImpl"})) == []


def test_anchor_is_a_fallback_not_an_override() -> None:
    # Prose that DOES name a declared struct is judged against that struct;
    # an anchor that is not one of the named types never widens the
    # vocabulary (accepted bias: guidance about a sibling type while
    # claiming a field of the unit is retired — a wrong retirement costs
    # attempts, a wrong retention cost a plateau).
    g = "ErrorImpl is the erased payload; call `self.object_drop` on it."
    assert layout_contradicted_notes(
        [g], AUTHORED_LAYOUT, anchor_types=frozenset({"ErrorVTable"})) == [g]


def test_anchors_complete_the_vocabulary_when_the_prose_names_a_unit_type() -> None:
    # A two-type transaction: the prose names one member's type and claims
    # a field of the other. Pre-fix the named type was the only vocabulary
    # and the lesson was retired as layout_contradicted (run
    # `106897-f7b6fb77` fill 8). Negative control: the implementer alone
    # still has no `inner`.
    layout = """
pub struct Error {
    inner: Own<ErrorImpl>,
}
pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    backtrace: Option<Backtrace>,
    _object: E,
}
"""
    g = ("use `self.inner.as_ptr()` instead of `self.inner.by_ref()`, and "
         "inside ErrorImpl::debug treat this as a raw pointer")
    assert layout_contradicted_notes(
        [g], layout, anchor_types=frozenset({"ErrorImpl"})) == [g]
    assert layout_contradicted_notes(
        [g], layout, anchor_types=frozenset({"Error", "ErrorImpl"})) == []


def test_a_leftover_claim_is_kept_when_a_self_type_is_not_in_the_file() -> None:
    # The pair writes error.rs + fmt.rs; Error is declared in lib.rs. The
    # leftover `self.inner` is a field of the missing type, not a lie about
    # ErrorImpl. Negative control: with only the in-file type as an anchor
    # there is no missing self type, so the claim is still a contradiction.
    impl_only = """
pub(crate) struct ErrorImpl<E = ()> {
    vtable: &'static ErrorVTable,
    backtrace: Option<Backtrace>,
    _object: E,
}
"""
    g = ("use `self.inner.as_ptr()` instead of `self.inner.by_ref()`, and "
         "inside ErrorImpl::debug treat this as a raw pointer")
    assert layout_contradicted_notes(
        [g], impl_only, anchor_types=frozenset({"ErrorImpl"})) == [g]
    assert layout_contradicted_notes(
        [g], impl_only, anchor_types=frozenset({"Error", "ErrorImpl"})) == []


def test_src_text_declaring_concatenates_files_that_hold_the_structs(
        tmp_path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub struct Error { inner: u8 }\n")
    (src / "fmt.rs").write_text("pub fn debug() {}\n")
    (src / "error.rs").write_text(
        "pub(crate) struct ErrorImpl { vtable: u8 }\n")
    text = src_text_declaring(src, {"Error", "ErrorImpl"})
    assert "pub struct Error" in text
    assert "struct ErrorImpl" in text
    assert "pub fn debug" not in text
    assert src_text_declaring(src, set()) == ""
    assert src_text_declaring(tmp_path / "missing", {"Error"}) == ""


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


# ---- retrospective guidance judged against the current layout ----
# Same judgement as the notes channel, applied to the retrospective store:
# retros are keyed by (file, fn, container) with NO cluster, so one guidance
# string is served to clusters whose private layouts differ.

# The exact guidance observed live, convicting `impl ErrorImpl` as exhausted.
POISON_GUIDANCE = (
    "Eliminate all references to the hallucinated ErrorVTable fields "
    "(`object_backtrace`, `object_ref`) and non-existent methods (`erase`, "
    "`error` field). Instead, directly access the wrapped error via the "
    "`_object` field using `(*this.as_ptr())._object`."
)


def test_guidance_naming_absent_field_is_contradicted() -> None:
    assert guidance_contradicts_layout(POISON_GUIDANCE, AUTHORED_LAYOUT)
    assert layout_safe_guidance(POISON_GUIDANCE, AUTHORED_LAYOUT) == ""


def test_guidance_struct_literal_with_absent_field_is_contradicted() -> None:
    # the shape that produced E0560 live: a literal listing a field the
    # current layout does not have.
    g = "Build it complete: `ErrorImpl { vtable, error, backtrace }`."
    assert guidance_contradicts_layout(g, AUTHORED_LAYOUT)
    assert layout_safe_guidance(g, AUTHORED_LAYOUT) == ""


def test_guidance_consistent_with_layout_survives() -> None:
    g = "Read the wrapped error from the `error` field of ErrorImpl."
    assert not guidance_contradicts_layout(g, AUTHORED_LAYOUT)
    assert layout_safe_guidance(g, AUTHORED_LAYOUT) == g


def test_same_guidance_is_correct_against_the_other_cluster_layout() -> None:
    # `_object` IS declared on the frozen layout — the judgement is per
    # (cluster, bake), which is why the shared store cannot be pruned on disk.
    g = "Access the wrapped error via the `_object` field of ErrorImpl."
    assert guidance_contradicts_layout(g, AUTHORED_LAYOUT)
    assert not guidance_contradicts_layout(g, FROZEN_LAYOUT)


def test_guidance_about_types_absent_from_file_survives() -> None:
    # judged only against structs the file actually declares — guidance about
    # a type defined elsewhere is unknown, not contradicted.
    g = "The `inner` field of Other holds the pointer."
    assert not guidance_contradicts_layout(g, AUTHORED_LAYOUT)


def test_empty_guidance_or_file_text_is_never_contradicted() -> None:
    assert not guidance_contradicts_layout("", AUTHORED_LAYOUT)
    assert not guidance_contradicts_layout(POISON_GUIDANCE, "")
    assert layout_safe_guidance("", AUTHORED_LAYOUT) == ""
    assert layout_safe_guidance(POISON_GUIDANCE, "") == POISON_GUIDANCE


def test_struct_fields_anchor_skips_commented_declaration() -> None:
    # An annotated-stub comment quotes a failed attempt verbatim, plain
    # `struct NAME { … }` included — the anchor must be the CODE declaration,
    # not the phantom one in the comment.
    text = (
        "// struct ErrorImpl { _object: E }\n"
        "pub struct ErrorImpl { error: E, vtable: V }\n"
    )
    assert struct_fields(text, "ErrorImpl") == frozenset({"error", "vtable"})
    # Comment-only declaration = no evidence, not a phantom layout.
    assert struct_fields("// struct Ghost { a: u8 }\n", "Ghost") is None


def test_struct_fields_anchor_needs_word_boundary() -> None:
    assert struct_fields(
        "mystruct Foo {}\nstruct Foo { a: u8 }", "Foo") == frozenset({"a"})


def test_tuple_positions_ignore_comment_content() -> None:
    # A `,` inside a comment is not a field separator.
    assert struct_fields(
        "struct P(A /* , */ , B);", "P") == frozenset({"0", "1"})

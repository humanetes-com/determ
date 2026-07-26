"""Unit coverage for splice.py locate-disambiguation + splice-guard paths,
using a fake enumerator (no syn binary). Complements test_splice_byte_offsets.py
(the byte-correctness regression)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from pipeline_core.application.reconstruct.splice import (
    locate_fn_by_record,
    restore_frozen_signature,
    splice_fn_body_at,
    splice_fn_with_where,
)
from pipeline_core.domain.reconstruction import SpliceOutcome


def test_splice_outcome_is_named_tuple_and_unpacks() -> None:
    o = SpliceOutcome(True, "ok", 12)
    ok, reason, delta = o                      # positional unpack still works
    assert (ok, reason, delta) == (True, "ok", 12)
    assert (o.ok, o.reason, o.delta) == (True, "ok", 12)
    assert o == (True, "ok", 12)               # tuple-equal → drop-in


def test_splice_fn_with_where_missing_file_returns_outcome(tmp_path: Path) -> None:
    out = splice_fn_with_where(lambda _p: [], tmp_path / "nope.rs", "", "f", "{ 1 }")
    assert isinstance(out, SpliceOutcome)
    assert out == (False, "file_missing", 0)


def _rec(**kw: object) -> dict[str, object]:
    base = {"file": "src/lib.rs", "fn_name": "f", "container": "",
            "sig_text": "fn f()", "sig_byte_start": 0, "body_byte_start": 5,
            "body_byte_end": 10, "has_unimplemented_body": True}
    base.update(kw)
    return base


def _loc(recs: list[dict[str, object]] | None, name: str = "f",
         container: str = "", sig: str = "fn f()") -> object:
    return locate_fn_by_record(lambda _p: recs, Path("/x/src/lib.rs"),
                               name, container, sig)


def test_locate_none_when_enumeration_fails() -> None:
    assert _loc(None) is None


def test_locate_none_when_no_file_match() -> None:
    assert _loc([_rec(file="src/other.rs")]) is None


def test_locate_none_when_no_name_match() -> None:
    assert _loc([_rec(fn_name="g")]) is None


def test_locate_container_narrows() -> None:
    recs = [_rec(container="impl A", sig_byte_start=1),
            _rec(container="impl B", sig_byte_start=2)]
    out = _loc(recs, container="impl B")
    assert out is not None and out[0] == 2


def test_locate_free_fn_by_sig_when_container_empty() -> None:
    """A FREE function has no impl container (extractor renders it ``""``), so
    container narrowing can't pick it out from same-named impl-method siblings
    (the free `fn not(cond)` vs the two `Bool::not`). Its verbatim signature
    disambiguates — this is what lets file-first land a free-fn producer atomically
    with the impls that call it."""
    recs = [
        _rec(fn_name="not", container="",
             sig_text="pub fn not(cond: impl Bool) -> bool", sig_byte_start=1),
        _rec(fn_name="not", container="impl Bool for bool",
             sig_text="fn not(self) -> bool", sig_byte_start=2),
        _rec(fn_name="not", container="impl Bool for &bool",
             sig_text="fn not(self) -> bool", sig_byte_start=3),
    ]
    out = locate_fn_by_record(lambda _p: recs, Path("/x/src/lib.rs"), "not",
                              "", "pub fn not(cond: impl Bool) -> bool")
    assert out is not None and out[0] == 1  # the free fn, located by signature


def test_locate_container_canonical_match_ignores_syn_spacing() -> None:
    """The syn extractor renders `& bool` (spaced) while a caller holds the
    compact `&bool`. Container narrowing is canonical-identity, not exact
    string — otherwise the ambiguity fallback would pick the FIRST same-name
    stub and splice a body into the wrong impl (the plateau's `not`/`from`
    collision)."""
    recs = [_rec(container="impl Bool for bool", sig_byte_start=1),
            _rec(container="impl Bool for & bool", sig_byte_start=2)]
    out = _loc(recs, container="impl Bool for &bool")
    assert out is not None and out[0] == 2


def test_locate_exact_sig_wins() -> None:
    recs = [_rec(sig_text="fn f() -> i32", sig_byte_start=1),
            _rec(sig_text="fn f()", sig_byte_start=2)]
    out = _loc(recs, sig="fn f()")
    assert out is not None and out[0] == 2


def test_locate_normalised_sig_match() -> None:
    recs = [_rec(sig_text="fn   f( )", sig_byte_start=7)]
    out = _loc(recs, sig="fn f( )")
    assert out is not None and out[0] == 7


def test_locate_ambiguous_picks_stub() -> None:
    recs = [_rec(has_unimplemented_body=False, sig_byte_start=1),
            _rec(has_unimplemented_body=True, sig_byte_start=2)]
    out = _loc(recs)  # two same-name/sig → pick the stub-bodied one
    assert out is not None and out[0] == 2


def test_locate_cfg_tier_disambiguates_variant_pair() -> None:
    """debt #17a: two same-name same-sig fns differing only by #[cfg] are
    distinguished when the caller passes the target's cfg_predicates."""
    extra = 'any (extra_cap , feature = "extra_cap")'
    stable = 'not (any (extra_cap , feature = "extra_cap"))'
    recs = [_rec(cfg_predicates=[extra], has_unimplemented_body=False, sig_byte_start=1),
            _rec(cfg_predicates=[stable], has_unimplemented_body=False, sig_byte_start=2)]
    # Target the stable variant (whitespace-insensitive match).
    out = locate_fn_by_record(lambda _p: recs, Path("/x/src/lib.rs"), "f", "", "fn f()",
                              ("not(any(extra_cap, feature = \"extra_cap\"))",))
    assert out is not None and out[0] == 2
    # Target the extra_cap variant.
    out2 = locate_fn_by_record(lambda _p: recs, Path("/x/src/lib.rs"), "f", "", "fn f()",
                               ('any(extra_cap, feature = "extra_cap")',))
    assert out2 is not None and out2[0] == 1


def test_locate_cfg_tier_absent_keeps_legacy_behavior() -> None:
    """No cfg passed → unchanged (stub-preference) behavior."""
    recs = [_rec(cfg_predicates=["a"], has_unimplemented_body=False, sig_byte_start=1),
            _rec(cfg_predicates=["b"], has_unimplemented_body=True, sig_byte_start=2)]
    out = _loc(recs)  # cfg=None → falls through to stub-preference
    assert out is not None and out[0] == 2


def test_splice_read_failure_returns_false() -> None:
    ok, reason, delta = splice_fn_body_at(Path("/no/such/file.rs"), 0, 1, "x")
    assert ok is False and reason == "read_failed" and delta == 0


def _stub_file(body: str = "    ::core::unimplemented!()\n") -> tuple[Path, int, int]:
    td = Path(tempfile.mkdtemp()) / "lib.rs"
    raw = ("fn f() {\n" + body + "}\n").encode("utf-8")
    td.write_bytes(raw)
    bs = raw.index(b"{") + 1
    be = raw.index(b"}", bs)
    return td, bs, be


def test_splice_out_of_bounds_returns_false() -> None:
    p, bs, _ = _stub_file()
    ok, reason, _ = splice_fn_body_at(p, bs, 10_000, "x")
    assert ok is False and reason == "bad_offsets"


def test_splice_refuses_nonstub_body() -> None:
    p, bs, be = _stub_file(body="    let a = 1;\n")  # real body, not a stub
    ok, reason, _ = splice_fn_body_at(p, bs, be, "a + 1")
    assert ok is False and reason == "target_not_stub"


def test_splice_strips_outer_braces_and_rejects_empty() -> None:
    p, bs, be = _stub_file()
    ok, reason, _ = splice_fn_body_at(p, bs, be, "{ }")  # strips to empty → reject
    assert ok is False and reason == "body_is_stub"


def test_splice_rejects_new_stub_body() -> None:
    p, bs, be = _stub_file()
    ok, reason, _ = splice_fn_body_at(p, bs, be, "unimplemented!()")
    assert ok is False and reason == "body_is_stub"


def test_splice_braced_body_succeeds() -> None:
    p, bs, be = _stub_file()
    ok, reason, delta = splice_fn_body_at(p, bs, be, "{ a + 1 }")
    assert ok is True and reason == "ok" and delta != 0
    assert "a + 1" in p.read_text()


# --- restore_frozen_signature (E0276 ratchet recovery) -----------------------

_CORRUPTED = """impl<C, E> StdError for ContextError<C, E> {
    fn provide<'a>(&'a self, request: &mut Request<'a>)
    where
        C: 'static,
 {
        request.provide_ref(&self.context)
    }
}
"""

_FROZEN_SIG = "fn provide<'a>(&'a self, request: &mut Request<'a>)"


def test_restore_frozen_signature_strips_spliced_where(tmp_path: Path) -> None:
    f = tmp_path / "context.rs"
    f.write_text(_CORRUPTED, encoding="utf-8")
    assert restore_frozen_signature(f, "provide", _FROZEN_SIG)
    text = f.read_text(encoding="utf-8")
    assert "'static" not in text                      # corruption gone
    assert _FROZEN_SIG + " {" in text                 # frozen sig restored
    assert "request.provide_ref(&self.context)" in text   # body intact


def test_restore_frozen_signature_noop_when_already_frozen(tmp_path: Path) -> None:
    f = tmp_path / "ok.rs"
    f.write_text(_FROZEN_SIG + " {\n    1\n}\n", encoding="utf-8")
    before = f.read_text(encoding="utf-8")
    assert not restore_frozen_signature(f, "provide", _FROZEN_SIG)
    assert f.read_text(encoding="utf-8") == before


def test_restore_frozen_signature_guards(tmp_path: Path) -> None:
    f = tmp_path / "g.rs"
    f.write_text("fn other() { 1 }\n", encoding="utf-8")
    # sig must declare the fn being restored
    assert not restore_frozen_signature(f, "other", "fn different()")
    # fn absent from the file
    assert not restore_frozen_signature(f, "missing", "fn missing()")
    # unreadable file
    assert not restore_frozen_signature(tmp_path / "nope.rs", "f", "fn f()")


def test_restore_frozen_signature_preserves_indent(tmp_path: Path) -> None:
    f = tmp_path / "i.rs"
    f.write_text(
        "impl Foo for Bar {\n"
        "    fn go(&self) -> u8 where Self: Sized {\n"
        "        7\n"
        "    }\n"
        "}\n", encoding="utf-8")
    assert restore_frozen_signature(f, "go", "fn go(&self) -> u8")
    assert "    fn go(&self) -> u8 {" in f.read_text(encoding="utf-8")


# --- the whole-item answer: a fill that parses but implements nothing --------

_WHOLE_ITEM = """unsafe fn context_drop_rest<C, E>(e: Own<ErrorImpl>, target: TypeId)
where
    C: 'static,
{
    ptr::write(e.ptr.as_ptr(), target);
}"""


def test_splice_at_unwraps_a_body_that_only_redeclares_the_fn() -> None:
    # Restating the whole item is a PROTOCOL mistake — the code inside is the
    # answer. Refusing it would burn the attempt and leave the fn a stub; the
    # wrapper is peeled and the real work lands.
    p, bs, be = _stub_file()
    ok, reason, _ = splice_fn_body_at(p, bs, be, _WHOLE_ITEM,
                                      fn_name="context_drop_rest")
    assert ok is True and reason == "ok"
    text = p.read_text()
    assert "unimplemented" not in text
    assert "ptr::write(e.ptr.as_ptr(), target);" in text
    assert "fn context_drop_rest" not in text        # no nested item survives


def test_splice_at_refuses_a_wrapper_with_nothing_inside() -> None:
    # The shape matched but there is no work to recover — landing it would
    # write an empty body that reads as FILLED forever.
    p, bs, be = _stub_file()
    ok, reason, _ = splice_fn_body_at(p, bs, be, "fn f() {}", fn_name="f")
    assert ok is False and reason == "body_redeclares_fn"
    assert "unimplemented" in p.read_text()          # stub untouched


def test_splice_at_refuses_a_wrapper_whose_body_is_a_stub() -> None:
    # Unwrapping must not smuggle `unimplemented!()` past the stub refusal.
    p, bs, be = _stub_file()
    ok, reason, _ = splice_fn_body_at(p, bs, be, "fn f() {\n    unimplemented!()\n}",
                                      fn_name="f")
    assert ok is False and reason == "body_is_stub"


def test_splice_at_still_accepts_a_differently_named_nested_helper() -> None:
    p, bs, be = _stub_file()
    ok, reason, _ = splice_fn_body_at(p, bs, be, "fn helper() -> i32 { 1 }\nhelper()",
                                      fn_name="f")
    assert ok is True and reason == "ok"


def test_splice_fn_body_unwraps_the_same_shape(tmp_path: Path) -> None:
    from pipeline_core.application.reconstruct.splice import splice_fn_body
    f = tmp_path / "e.rs"
    f.write_text("fn context_drop_rest() {\n    unimplemented!()\n}\n", encoding="utf-8")
    ok, reason, _ = splice_fn_body(f, "context_drop_rest", _WHOLE_ITEM)
    assert ok is True and reason == "ok"
    assert "ptr::write(e.ptr.as_ptr(), target);" in f.read_text()


def test_restore_frozen_signature_never_duplicates_attributes(tmp_path: Path) -> None:
    # A skeleton sig that carries the item's attributes must not write them
    # again: the originals stay in the file, and a doubled `#[cfg(...)]`
    # silently changes which cluster the fn compiles in.
    f = tmp_path / "a.rs"
    f.write_text('#[cfg(feature = "std")]\n'
                 "fn provide(&self) -> i32\nwhere\n    C: 'static,\n{\n    1\n}\n",
                 encoding="utf-8")
    sig = '#[cfg(feature = "std")]\nfn provide(&self) -> i32'
    assert restore_frozen_signature(f, "provide", sig)
    assert f.read_text(encoding="utf-8").count('#[cfg(feature = "std")]') == 1


def test_restore_frozen_signature_refuses_a_sig_carrying_a_body(tmp_path: Path) -> None:
    f = tmp_path / "b.rs"
    before = "fn provide(&self) -> i32 {\n    1\n}\n"
    f.write_text(before, encoding="utf-8")
    assert not restore_frozen_signature(f, "provide", "fn provide(&self) -> i32 { 9 }")
    assert f.read_text(encoding="utf-8") == before


def test_restore_frozen_signature_rolls_back_an_unparseable_result(tmp_path: Path) -> None:
    # No body offsets exist to undo this write, so an unparseable result would
    # PERSIST and kill the whole cluster's compile. It must be parse-gated.
    f = tmp_path / "c.rs"
    before = "fn provide(&self) -> i32 {\n    1\n}\n"
    f.write_text(before, encoding="utf-8")
    assert not restore_frozen_signature(f, "provide", "fn provide(&self) -> Vec<i32",
                                        validate_parse=lambda _p: False)
    assert f.read_text(encoding="utf-8") == before

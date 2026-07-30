"""Characterization for the relocated disk-preserving fill merge (P6)."""
from __future__ import annotations

from pipeline_core.application.reconstruct.fill_merge import (
    merge_fills_preserving_existing,
)


def test_fills_only_unimplemented_bodies() -> None:
    disk = "fn a() { unimplemented!() }\nfn b() { 1 + 1 }\n"
    model = "fn a() { 42 }\nfn b() { 999 }\n"
    out = merge_fills_preserving_existing(disk, model)
    assert "fn a() { 42 }" in out      # stub filled from model
    assert "fn b() { 1 + 1 }" in out   # already-filled disk body preserved
    assert "999" not in out


def test_empty_model_returns_disk_verbatim() -> None:
    disk = "fn a() { unimplemented!() }\n"
    assert merge_fills_preserving_existing(disk, "") == disk


def test_no_fns_returns_disk_verbatim() -> None:
    disk = "// just a comment\nuse std::fmt;\n"
    assert merge_fills_preserving_existing(disk, "fn x() { 1 }") == disk


def test_count_stub_bodies() -> None:
    from pipeline_core.application.reconstruct.fill_merge import count_stub_bodies
    text = ("fn a() { unimplemented!() }\n"
            "fn b() { 1 + 1 }\n"
            "fn c() {\n    unimplemented!()\n}\n")
    assert count_stub_bodies(text) == 2
    assert count_stub_bodies("// no fns\n") == 0


def test_backfill_stub_bodies_earliest_donor_wins() -> None:
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "fn a() { unimplemented!() }\nfn b() { 5 }\n"
    donor1 = "fn a() { 1 }\n"
    donor2 = "fn a() { 2 }\nfn b() { 999 }\n"
    r = backfill_stub_bodies(target, [donor1, donor2])
    assert r.filled == 1
    assert "fn a() { 1 }" in r.text        # earliest donor won
    assert "fn b() { 5 }" in r.text        # filled body never overwritten
    assert "999" not in r.text and "fn a() { 2 }" not in r.text


def test_backfill_carries_the_imports_the_adopted_bodies_resolve_in() -> None:
    # The live failure: 121 bodies in the right slots, 41 `E0425 cannot find …
    # in this scope`. The donor earned `use crate::wrapper::MessageError;`
    # during its own fill; the target's bake never had it.
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "use crate::ptr::Own;\nfn a() { unimplemented!() }\n"
    donor = ("use crate::ptr::Own;\nuse crate::wrapper::MessageError;\n"
             "fn a() { MessageError(1) }\n")
    r = backfill_stub_bodies(target, [donor])
    assert r.filled == 1
    assert r.uses_added == 1
    assert "use crate::wrapper::MessageError;" in r.text
    assert r.text.count("use crate::ptr::Own;") == 1


def test_backfill_repairs_scope_on_a_resume_that_places_no_new_body() -> None:
    # A killed-and-resumed cluster already holds the donor bodies, so nothing is
    # placed this pass — and the imports those bodies need are still missing.
    # Gating the repair on "placed something now" leaves that tree red forever.
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "fn a() { MessageError(1) }\nfn b() { unimplemented!() }\n"
    donor = ("use crate::wrapper::MessageError;\n"
             "fn a() { MessageError(1) }\nfn b() { unimplemented!() }\n")
    r = backfill_stub_bodies(target, [donor])
    assert r.filled == 0
    assert r.uses_added == 1
    assert r.text.startswith("use crate::wrapper::MessageError;")


def test_backfill_import_carry_is_idempotent() -> None:
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "fn a() { unimplemented!() }\nfn b() { unimplemented!() }\n"
    donor = "use crate::wrapper::MessageError;\nfn a() { MessageError(1) }\n"
    once = backfill_stub_bodies(target, [donor])
    twice = backfill_stub_bodies(once.text, [donor])
    assert twice.uses_added == 0
    assert twice.text.count("use crate::wrapper::MessageError;") == 1


def test_backfill_stub_bodies_no_stubs_short_circuits() -> None:
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "fn a() { 1 }\n"
    r = backfill_stub_bodies(target, ["fn a() { 2 }\n"])
    assert (r.text, r.filled) == (target, 0)


def test_backfill_stub_bodies_donor_stub_adds_nothing() -> None:
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "fn a() { unimplemented!() }\n"
    r = backfill_stub_bodies(target, ["fn a() { unimplemented!() }\n"])
    assert r.filled == 0
    assert "unimplemented!()" in r.text


# --- same-signature slots: the seed defect that opened `nightly` at 0/124 ---

_TWO_IMPLS_DONOR = """\
mod ext {
    pub trait Ctx { fn go(self) -> u32; }
    impl<E> Ctx for E where E: Copy {
        fn go(self) -> u32 { from_generic() }
    }
    impl Ctx for Own {
        fn go(self) -> u32 { self.go_inner() }
    }
}
"""
_TWO_IMPLS_TARGET = """\
mod ext {
    pub trait Ctx { fn go(self) -> u32; }
    impl<E> Ctx for E where E: Copy {
        fn go(self) -> u32 { ::core::unimplemented!() }
    }
    impl Ctx for Own {
        fn go(self) -> u32 { ::core::unimplemented!() }
    }
}
"""


def test_same_signature_in_two_impls_keeps_each_body_in_its_own_slot() -> None:
    """The seed bug: keyed by signature alone, `impl Ctx for Own`'s body won and
    was handed to BOTH impls, so the generic impl called a method its type
    parameter does not have. Slots keep them apart."""
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    r = backfill_stub_bodies(_TWO_IMPLS_TARGET, [_TWO_IMPLS_DONOR])
    assert r.filled == 2
    assert r.ambiguous_skipped == 0
    generic, concrete = r.text.split("impl Ctx for Own")
    assert "from_generic()" in generic and "self.go_inner()" not in generic
    assert "self.go_inner()" in concrete and "from_generic()" not in concrete


def test_unmatched_slot_with_ambiguous_signature_is_refused_not_guessed() -> None:
    """A donor whose owner chain does not match (a model's thin re-emission)
    must not fall back to a signature that names two disk slots — a stub the
    fill loop can still target beats a body in the wrong impl."""
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    thin_donor = "fn go(self) -> u32 { from_generic() }\n"
    r = backfill_stub_bodies(_TWO_IMPLS_TARGET, [thin_donor])
    assert r.filled == 0
    assert r.ambiguous_skipped == 2
    assert r.text.count("::core::unimplemented!()") == 2


_CFG_SIBLINGS_TARGET = """\
#[cfg(feature = "std")]
impl Trait for T {
    fn f(&self) -> u32 { ::core::unimplemented!() }
}
#[cfg(not(feature = "std"))]
impl Trait for T {
    fn f(&self) -> u32 { ::core::unimplemented!() }
}
"""


def test_cfg_siblings_share_a_slot_and_are_refused_not_filled_from_one_body() -> None:
    """The seed defect on the axis clusters are DEFINED by. `owner_path` drops
    attributes, so two impls differing only by `#[cfg]` are ONE slot — and a
    donor cluster, being a different cfg-set, typically has just one of them
    filled. Placing that body in both is the same wrong-answer machine with a
    wider key, so the slot must be tested for uniqueness on the disk side too."""
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    donor = _CFG_SIBLINGS_TARGET.replace(
        "fn f(&self) -> u32 { ::core::unimplemented!() }",
        "fn f(&self) -> u32 { self.std_path() }", 1)
    r = backfill_stub_bodies(_CFG_SIBLINGS_TARGET, [donor])
    assert r.filled == 0
    assert r.ambiguous_skipped == 2
    assert "self.std_path()" not in r.text
    assert r.text.count("::core::unimplemented!()") == 2


def test_ambiguity_is_not_charged_when_the_donor_offered_no_body() -> None:
    """`ambiguous_skipped` names stubs the fill loop now has to pay for. A donor
    that carried its own stub for that signature offered nothing to decline."""
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    r = backfill_stub_bodies(_CFG_SIBLINGS_TARGET, [_CFG_SIBLINGS_TARGET])
    assert (r.filled, r.ambiguous_skipped) == (0, 0)


def test_unambiguous_signature_still_falls_back_across_formatting() -> None:
    """The fallback is preserved where it is safe: one occurrence on each side."""
    from pipeline_core.application.reconstruct.fill_merge import backfill_stub_bodies
    target = "impl Foo {\n    fn only(&self) -> u32 { ::core::unimplemented!() }\n}\n"
    donor = "fn only(&self)  ->  u32 { 7 }\n"
    r = backfill_stub_bodies(target, [donor])
    assert r.filled == 1
    assert "{ 7 }" in r.text


def test_brace_inside_string_literal_does_not_desync_bodies() -> None:
    """Body spans count braces over CODE only — a `}` in a string literal used
    to end the body early and splice over the next fn's header."""
    from pipeline_core.application.reconstruct.fill_merge import count_stub_bodies
    text = ('fn a() { let s = "}"; unimplemented!() }\n'
            "fn b() { 1 }\n")
    assert count_stub_bodies(text) == 1


def test_merge_fn_blocks_refuses_two_stubs_sharing_one_signature() -> None:
    """The bare-signature protocol cannot address two same-signature stubs, so
    it must place neither rather than the same body in both."""
    from pipeline_core.application.reconstruct.fill_merge import (
        merge_fn_blocks_into_disk,
    )
    out = merge_fn_blocks_into_disk(
        _TWO_IMPLS_TARGET, {"fn go(self) -> u32": "from_generic()"})
    assert out == _TWO_IMPLS_TARGET
    # ...but a single stub with that signature is still filled.
    one_stub = _TWO_IMPLS_TARGET.replace(
        "fn go(self) -> u32 { ::core::unimplemented!() }",
        "fn go(self) -> u32 { already_filled() }", 1)
    out2 = merge_fn_blocks_into_disk(
        one_stub, {"fn go(self) -> u32": "from_generic()"})
    assert "from_generic()" in out2 and "already_filled()" in out2

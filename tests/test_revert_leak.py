"""Revert-leak pins: a failed per-fn splice must always be undoable.

Live incident (run 16657, ensure.rs::render): the model wrapped its body in
the target's full `fn` definition; the splice nested a duplicate header, the
FREE-fn revert took the bare-name path, refused `ambiguous_name`, and the
broken body stayed in the tree — every sibling's compile then failed on the
foreign E0308 while the race guard discarded the model's later attempts as
"another worker filled it". These tests pin the repaired behaviours.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.fill_engine_roles import MutatorAdapter
from pipeline_core.application.reconstruct.splice import (
    splice_fn_body,
    splice_fn_body_at,
)
from pipeline_core.domain.rust_fn_headers import locate_all_fns

_NESTED = (
    "use core::fmt::Debug;\n"
    "fn render(msg: &'static str) -> Error {\n"
    "    fn render(msg: &'static str) -> Error {\n"
    "        Error::msg(msg)\n"
    "    }\n"
    "}\n"
)


def _mutator(tmp: Path) -> MutatorAdapter:
    # Record locator = the syn enumerator's role: indexes only the OUTER fn
    # (nested fns are not items it reports). Faked with the outer offsets.
    def _locate_by_record(file_path: Path, name: str, container: str,
                          sig_text: str, cfg=None):
        text = file_path.read_text(encoding="utf-8")
        locs = locate_all_fns(text, name)
        if not locs:
            return None
        _sig, sig_start, body_start, body_end = locs[0]
        return (sig_start, body_start, body_end, _sig)

    return MutatorAdapter(
        locate_fn_by_record_fn=_locate_by_record,
        locate_fn_fn=lambda text, name: None,
        splice_fn_body_at_fn=splice_fn_body_at,
        splice_fn_body_fn=splice_fn_body,
        replace_fn_signature_fn=lambda *a: False,
        extract_signature_block_fn=lambda s: None,
        lock_for_fn=lambda p: None,  # type: ignore[arg-type]
    )


def test_bare_name_revert_refuses_on_nested_duplicate(tmp_path: Path) -> None:
    # The pre-fix failure mode, pinned so the refusal reason stays visible.
    f = tmp_path / "ensure.rs"
    f.write_text(_NESTED, encoding="utf-8")
    out = splice_fn_body(f, "render", "unimplemented!()",
                         allow_nonstub=True, allow_stub_body=True)
    assert not out.ok and out.reason == "ambiguous_name"


def test_free_fn_revert_restores_stub_via_record_locator(tmp_path: Path) -> None:
    # With a sig disambiguator the revert must route through the record
    # locator (outer fn only) and land even when the bare name is ambiguous.
    f = tmp_path / "ensure.rs"
    f.write_text(_NESTED, encoding="utf-8")
    mut = _mutator(tmp_path)
    out = mut.revert_body_scoped(
        f, "render", "", "fn render(msg: &'static str) -> Error",
        "unimplemented!()")
    assert out.ok, out.reason
    text = f.read_text(encoding="utf-8")
    assert len(locate_all_fns(text, "render")) == 1
    assert "unimplemented!()" in text and "Error::msg" not in text


def test_free_fn_revert_without_disambiguator_stays_bare_name(
        tmp_path: Path) -> None:
    # No container AND no sig: the record locator would have to guess among
    # same-name candidates; a revert must never guess, so the ambiguity
    # refusal is the correct outcome.
    f = tmp_path / "ensure.rs"
    f.write_text(_NESTED, encoding="utf-8")
    out = _mutator(tmp_path).revert_body_scoped(
        f, "render", "", "", "unimplemented!()")
    assert not out.ok and out.reason == "ambiguous_name"

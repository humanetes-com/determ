"""The re-seed, end to end on a real tree: what comes back, and what must not.

Three properties, each with the control that makes it a measurement:

* the units whose layout still matches come back, and with the ledger disabled
  they stay stubbed — the 56-body re-stub of run `259637-36db9aad` in miniature;
* a moved struct field set re-seeds NOTHING into that file — the 2026-08-08
  `ErrorImpl` lesson (`pub(crate) backtrace` vanished between `1f4099b` and the
  tree that replaced it, and bodies from one run do not compile against
  another's skeleton) as an assertion;
* a body that does not build is bisected out and left stubbed, and with the
  build check unwired the same re-seed leaves a tree nothing verified — which
  is the `nightly 0/124 for 8.7 h` class, a sweep scoring 0 with no per-unit
  signal to aim at.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.adapters.fn_reseed import (
    current_bodies,
    current_signatures,
    splice_bodies,
)
from pipeline_core.adapters.fn_revert import revert_fns_to_stub
from pipeline_core.application.reconstruct.body_reseed import (
    TreeWriter,
    reseed,
    skeleton_fingerprints,
)
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    body_sha,
    sig_sha_of,
)
from pipeline_core.domain.models import FnLocator

_WRITER = TreeWriter(splice=splice_bodies, revert=revert_fns_to_stub,
                     signatures=current_signatures, bodies=current_bodies)

_ERROR_RS = """\
pub struct ErrorImpl {
    pub backtrace: u32,
}

impl ErrorImpl {
    pub fn depth(&self) -> u32 {
        BODY_DEPTH
    }

    pub fn width(&self) -> u32 {
        BODY_WIDTH
    }
}
"""

_KIND_RS = """\
pub struct Kind;

impl Kind {
    pub fn tag(&self) -> u8 {
        BODY_TAG
    }
}
"""

_STUB = "\n        unimplemented!()\n    "
_DEPTH = "\n        self.backtrace\n    "
_WIDTH = "\n        self.backtrace * 2\n    "
_TAG = "\n        7\n    "


def _tree(root: Path, *, filled: bool = False) -> Path:
    """A two-file cluster: the skeleton (the authored layout, stub bodies) and
    the filled tree the loop scores. Returns the CRATE root — every identity
    here is crate-relative (`src/error.rs`), the way the extractor names a
    file and the way cargo prints one."""
    crate = root / ("filled" if filled else "skeleton")
    src = crate / "src"
    src.mkdir(parents=True)
    (src / "error.rs").write_text(
        _ERROR_RS.replace("BODY_DEPTH", "unimplemented!()")
        .replace("BODY_WIDTH", "unimplemented!()"))
    (src / "kind.rs").write_text(_KIND_RS.replace("BODY_TAG",
                                                  "unimplemented!()"))
    return crate


_ORACLE = "oracle-A"


def _entries(skeleton: Path, *, state: BodyState = BodyState.PROVEN,
             oracle_fp: str = _ORACLE) -> list[LedgerEntry]:
    fps = skeleton_fingerprints(skeleton)
    rows = []
    for file, fn, body in (("src/error.rs", "depth", _DEPTH),
                           ("src/error.rs", "width", _WIDTH),
                           ("src/kind.rs", "tag", _TAG)):
        sigs = current_signatures(FnLocator(file, fn), skeleton)
        rows.append(LedgerEntry(
            key=LedgerKey("anyhow", "stable", file, fn),
            body_sha=body_sha((body,)), bodies=(body,),
            sig_sha=sig_sha_of(sigs), layout_fp=fps[file], state=state,
            oracle_fp=oracle_fp))
    return rows


def _stubs() -> list[FnLocator]:
    return [FnLocator("src/error.rs", "depth"),
            FnLocator("src/error.rs", "width"),
            FnLocator("src/kind.rs", "tag")]


def _reseed(tmp_path: Path, entries: list[LedgerEntry], filled: Path,
            skeleton: Path, check=None, oracle_fp: str = _ORACLE):
    return reseed(site="test", entries=entries, wanted=_stubs(),
                  project="anyhow", cluster="stable", src_dir=filled,
                  skeleton_root=skeleton, writer=_WRITER, oracle_fp=oracle_fp,
                  check_builds=check)


def test_the_matching_units_come_back_and_the_control_leaves_them_stubbed(
        tmp_path: Path) -> None:
    skeleton = _tree(tmp_path)
    filled = _tree(tmp_path, filled=True)

    out = _reseed(tmp_path, _entries(skeleton), filled, skeleton)
    assert {(k.file, k.fn_name) for k in out.seeded} == {
        ("src/error.rs", "depth"), ("src/error.rs", "width"), ("src/kind.rs", "tag")}
    assert "self.backtrace" in (filled / "src" / "error.rs").read_text()
    assert "7" in (filled / "src" / "kind.rs").read_text()
    assert out.proven == 3 and not out.checked

    # THE CONTROL: the same site with an empty ledger. Every unit stays a stub,
    # which is what every one of these seams did before this entry.
    control_filled = _tree(tmp_path / "control", filled=True)
    control = _reseed(tmp_path, [], control_filled, skeleton)
    assert control.seeded == []
    assert (control_filled / "src" / "error.rs").read_text().count(
        "unimplemented!()") == 2


def test_a_moved_struct_field_set_re_seeds_nothing_into_that_file(
        tmp_path: Path) -> None:
    """The 2026-08-08 lesson. `ErrorImpl` loses a field; the bodies that read
    it must not go back, and the file that did NOT move must be restored
    anyway — a per-file gate, not a per-tree one, or one re-author would cost
    every proven body in the cluster."""
    skeleton = _tree(tmp_path)
    filled = _tree(tmp_path, filled=True)
    entries = _entries(skeleton)
    (skeleton / "src" / "error.rs").write_text(
        (skeleton / "src" / "error.rs").read_text().replace(
            "pub backtrace: u32,", "pub trace: u32,"))

    out = _reseed(tmp_path, entries, filled, skeleton)
    assert {(k.file, k.fn_name) for k in out.seeded} == {("src/kind.rs", "tag")}
    assert {k.file for k, _why in out.skipped} == {"src/error.rs"}
    assert all(why == "layout moved" for _k, why in out.skipped)
    assert "2 (layout moved)" in out.render(), (
        "the line counts skips BY REASON — 'layout moved' and 'state landed' "
        "send a reader to different places")
    assert (filled / "src" / "error.rs").read_text().count("unimplemented!()") == 2


def test_a_moved_signature_refuses_even_when_the_file_matches(
        tmp_path: Path) -> None:
    skeleton = _tree(tmp_path)
    filled = _tree(tmp_path, filled=True)
    entries = _entries(skeleton)
    (filled / "src" / "kind.rs").write_text(
        (filled / "src" / "kind.rs").read_text().replace("pub fn tag(&self) -> u8",
                                                 "pub fn tag(&self) -> u16"))
    out = _reseed(tmp_path, entries, filled, skeleton)
    assert ("src/kind.rs", "tag") not in {(k.file, k.fn_name) for k in out.seeded}
    assert ("src/kind.rs", "signature moved") in {(k.file, why)
                                              for k, why in out.skipped}


def test_a_body_that_does_not_build_is_bisected_out_and_left_stubbed(
        tmp_path: Path) -> None:
    """The transaction. One bad body must cost itself and nothing else, and
    the tree the site leaves behind must be one the checker accepted."""
    skeleton = _tree(tmp_path)
    filled = _tree(tmp_path, filled=True)
    calls: list[int] = []

    def _check() -> tuple[bool, str]:
        calls.append(1)
        text = (filled / "src" / "error.rs").read_text()
        ok = "self.backtrace * 2" not in text     # `width` is the bad body
        return ok, "" if ok else "E0308 mismatched types"

    out = _reseed(tmp_path, _entries(skeleton), filled, skeleton, check=_check)
    assert {(k.file, k.fn_name) for k in out.seeded} == {
        ("src/error.rs", "depth"), ("src/kind.rs", "tag")}
    assert [(k.file, k.fn_name) for k in out.rejected] == [
        ("src/error.rs", "width")]
    assert out.checked and out.builds == len(calls)
    ok, _ = _check()
    assert ok, "the site must leave a tree the checker accepts"
    text = (filled / "src" / "error.rs").read_text()
    assert "self.backtrace\n" in text and "self.backtrace * 2" not in text
    assert "rejected (did not build)" in out.render()


def test_negative_control_without_the_check_the_bad_body_stays_on_the_tree(
        tmp_path: Path) -> None:
    """The same re-seed with no checker wired: the tree ends up holding a body
    nothing verified, and the line SAYS so rather than implying otherwise. This
    is the state release bar 1 would be asserting a pass count on."""
    skeleton = _tree(tmp_path)
    filled = _tree(tmp_path, filled=True)
    out = _reseed(tmp_path, _entries(skeleton), filled, skeleton)
    assert "self.backtrace * 2" in (filled / "src" / "error.rs").read_text()
    assert not out.checked and out.rejected == []
    assert "NOT build-checked" in out.render()


def test_an_occurrence_count_mismatch_is_refused_rather_than_half_applied(
        tmp_path: Path) -> None:
    """`src/context.rs::source` is two impls, and the revert seam reverts every
    occurrence — so a stored tuple that does not match the tree's shape means
    the file is not the file the body was proven in, whatever the fingerprint
    said."""
    skeleton = _tree(tmp_path)
    filled = _tree(tmp_path, filled=True)
    entries = _entries(skeleton)
    entries[0] = LedgerEntry(
        key=entries[0].key, body_sha=entries[0].body_sha,
        bodies=(_DEPTH, _DEPTH), sig_sha=entries[0].sig_sha,
        layout_fp=entries[0].layout_fp, state=BodyState.PROVEN,
        oracle_fp=_ORACLE)
    out = _reseed(tmp_path, entries, filled, skeleton)
    assert ("src/error.rs", "depth") not in {(k.file, k.fn_name)
                                         for k in out.seeded}
    assert any("occurrence count" in why for _k, why in out.skipped)


def test_a_proven_row_under_oracle_a_is_refused_on_a_tree_scored_by_oracle_b(
        tmp_path: Path) -> None:
    """The plan's sentence at the transaction: the layout matches, the
    signatures match, the bodies build — and nothing goes back, because the
    tests that proved them are not the tests scoring this tree. The control
    is the same site under oracle A, which re-seeds all three."""
    skeleton = _tree(tmp_path)
    filled = _tree(tmp_path, filled=True)
    out = _reseed(tmp_path, _entries(skeleton, oracle_fp="oracle-A"), filled,
                  skeleton, oracle_fp="oracle-B")
    assert out.seeded == []
    assert {why for _k, why in out.skipped} == {"oracle moved"}
    assert "3 (oracle moved)" in out.render()
    assert (filled / "src" / "error.rs").read_text().count("unimplemented!()") == 2
    # Rows written before the field existed carry "" and are refused once.
    blank = _reseed(tmp_path, _entries(skeleton, oracle_fp=""), filled,
                    skeleton, oracle_fp="oracle-A")
    assert blank.seeded == [] and len(blank.skipped) == 3
    control_filled = _tree(tmp_path / "control", filled=True)
    control = _reseed(tmp_path, _entries(skeleton, oracle_fp="oracle-A"),
                      control_filled, skeleton, oracle_fp="oracle-A")
    assert len(control.seeded) == 3 and control.skipped == []

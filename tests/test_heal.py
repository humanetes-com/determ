"""F2 pins — heal hierarchy (surgical excision → best-tree → git → skeleton)."""
from __future__ import annotations

from pipeline_core.application.reconstruct.heal import HealDeps, heal_file
from pipeline_core.domain.heal import (
    RestorePoint,
    apply_excision,
    count_unimplemented,
    doccomment_intrusions,
)

# The incident shape: fn bodies spliced INTO a `/// ```` doc example.
CORRUPT = (
    "impl Display for E {\n"
    "    /// Example:\n"
    "    /// ```\n"
    "    /// use foo::bar;\n"
    "    fn fmt(&self, f: &mut Formatter) -> Result {\n"
    "        write!(f, \"boom\")\n"
    "    }\n"
    "    /// bar();\n"
    "    /// ```\n"
    "    fn real(&self) -> u32 { 1 }\n"
    "}\n"
)


def test_doccomment_intrusion_detected() -> None:
    idx = doccomment_intrusions(CORRUPT)
    assert idx == [4, 5, 6]   # the 3 spliced code lines
    new, n = apply_excision(CORRUPT)
    assert n == 3
    assert "fn fmt" not in new       # intrusion gone
    assert "fn real" in new          # real fill kept


def test_legit_code_between_doc_blocks_untouched() -> None:
    legit = "/// doc a\nfn a() {}\n/// doc b\nfn b() {}\n"
    assert doccomment_intrusions(legit) == []


def _deps(parse_ok, *, best=None, git=(), skel=None, events=None):
    return HealDeps(
        parse_ok=parse_ok,
        best_tree_bytes=lambda _rel: best,
        git_ancestor_blobs=lambda _rel: list(git),
        skeleton_bytes=lambda _rel: skel,
        log=lambda _m: None,
        emit=(lambda *a, **k: events.append((a, k))) if events is not None
        else (lambda *a, **k: None))


def test_heal_surgical_excision_keeps_fills(tmp_path) -> None:
    f = tmp_path / "src"
    f.mkdir()
    (f / "e.rs").write_text(CORRUPT, encoding="utf-8")
    # parse_ok: the excised text (no `fn fmt` intrusion) parses; corrupt does not.
    parse_ok = lambda p: "fn fmt(&self" not in p.read_text()  # noqa: E731
    out = heal_file(tmp_path, "src/e.rs", "stable", _deps(parse_ok))
    assert out.point is RestorePoint.SURGICAL_EXCISION
    assert out.excised_lines == 3
    assert "fn real" in (f / "e.rs").read_text()


def test_heal_falls_through_to_skeleton_with_loud_event(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "e.rs"
    # no doc-comment intrusion (excision finds nothing) and everything is broken
    # until the skeleton (2 stubs vs the 1-stub current tree → 1 fill lost).
    target.write_text("fn a() -> u32 { 5 }\nfn b() -> u32 { unimplemented!() }\n",
                      encoding="utf-8")
    skel = b"fn a() -> u32 { unimplemented!() }\nfn b() -> u32 { unimplemented!() }\n"
    events: list = []
    out = heal_file(tmp_path, "src/e.rs", "stable",
                    _deps(lambda _p: False, skel=skel, events=events))
    assert out.point is RestorePoint.BAKED_SKELETON
    assert out.fills_lost == 1
    assert target.read_bytes() == skel
    assert events and events[0][0][0] == "phase_c.heal.skeleton_fallback"


def test_heal_prefers_best_tree_over_git_and_skeleton(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "e.rs"
    target.write_text("garbage {", encoding="utf-8")
    best = b"fn a() -> u32 { 1 }\n"
    git = [b"fn a() -> u32 { 2 }\n"]
    skel = b"fn a() -> u32 { unimplemented!() }\n"
    out = heal_file(tmp_path, "src/e.rs", "stable",
                    _deps(lambda _p: True, best=best, git=git, skel=skel))
    assert out.point is RestorePoint.BEST_TREE_COPY
    assert target.read_bytes() == best


def test_count_unimplemented() -> None:
    assert count_unimplemented("unimplemented!() x unimplemented!()") == 2

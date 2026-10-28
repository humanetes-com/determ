"""The vocabulary is what rustdoc's public index says an outside crate can
name, when that index was measured.

Computed 2026-10-07 on hardcrate's original source (the crate of run
`88434-2e3bb4d7`): 14 `pub use` lines re-export items of private modules;
the mod-chain walk gave a 4-name glossary, the index gives 47 (`Counter`,
`Guarded`, `encode_id`, `seal`, …). This test pins the rule on records of the
same shape; the other direction is a `pub` trait of a private inline module
nobody re-exports (anyhow's `ext::StdError`), which the index leaves out.
"""
from __future__ import annotations

from pipeline_core.domain.behaviour_book import glossary_entries
from pipeline_core.domain.behaviour_probe import probe_targets
from pipeline_core.domain.fn_reachability import PublicIndex
from pipeline_core.domain.models import FnRecord, TypeRecord

_SRC = [("src/lib.rs", "mod counter;\npub use counter::Counter;\npub fn add() {}\n"),
        ("src/counter.rs", "pub struct Counter;\nimpl Counter { pub fn bump(&self) {} }\n"),
        ("src/ext.rs", "pub trait StdError { fn ext_context(&self); }\n")]
_TYPES = [TypeRecord(file="src/counter.rs", kind="struct", name="Counter",
                     is_pub=True, is_exactly_pub=True),
          TypeRecord(file="src/ext.rs", kind="trait", name="StdError",
                     is_pub=True, is_exactly_pub=True)]
_FNS = [FnRecord(file="src/lib.rs", fn_name="add", is_pub=True, is_exactly_pub=True),
        FnRecord(file="src/counter.rs", fn_name="bump", container="impl Counter",
                 is_pub=True, is_exactly_pub=True),
        FnRecord(file="src/ext.rs", fn_name="ext_context", container="trait StdError",
                 is_pub=False, is_exactly_pub=False)]
_REACH = frozenset({"src/lib.rs", "src/ext.rs"})   # the mod-chain walk's answer
_RAW = {"src/lib.rs": frozenset({"add"}),
        "src/counter.rs": frozenset({"Counter", "bump"})}
_INDEX = PublicIndex.of(_RAW)


def test_the_index_names_a_re_export_and_drops_an_unexported_trait() -> None:
    walk = {g.name for g in glossary_entries(_FNS, _TYPES, _SRC, reachable_files=_REACH)}
    indexed = {g.name for g in glossary_entries(_FNS, _TYPES, _SRC, reachable_files=_REACH,
                                                public_index=_INDEX)}
    # Negative control: the walk misses the re-export and keeps the trait.
    assert "Counter" not in walk and "StdError::ext_context" in walk
    assert {"Counter", "Counter::bump", "add"} <= indexed
    assert "StdError" not in indexed and "StdError::ext_context" not in indexed


def test_probe_targets_read_the_same_rule() -> None:
    assert probe_targets(_FNS, (), max_targets=10, reachable_files=_REACH,
                         type_files={"Counter": "src/counter.rs"}) == ("add",)
    got = probe_targets(_FNS, (), max_targets=10, reachable_files=_REACH,
                        type_files={"Counter": "src/counter.rs"}, public_index=_INDEX)
    assert len(got) == 2 and "add" in got


def test_records_enumerated_from_src_join_the_crate_rooted_index() -> None:
    """Task 0d enumerates records from `src/` (`counter.rs`); the index is
    measured with crate-root paths (`src/counter.rs`). Joined as a plain
    mapping, nothing matched: run `2355041-eae39441`'s book vocabulary was
    the crate's 3 macros, against 42 entries for the same original crate
    enumerated from its root. Claim: the src-rooted records give the same
    vocabulary and targets as the crate-rooted ones. Control: the raw
    mapping lookup on the src-rooted records finds nothing."""
    from dataclasses import replace
    fns = [replace(r, file=r.file.removeprefix("src/")) for r in _FNS]
    types = [replace(t, file=t.file.removeprefix("src/")) for t in _TYPES]
    src = [(f.removeprefix("src/"), s) for f, s in _SRC]
    rooted = {g.name for g in glossary_entries(_FNS, _TYPES, _SRC, public_index=_INDEX)}
    from_src = {g.name for g in glossary_entries(fns, types, src, public_index=_INDEX)}
    assert from_src == rooted and {"Counter", "Counter::bump", "add"} <= from_src
    got_src = probe_targets(fns, (), max_targets=10, public_index=_INDEX)
    got_root = probe_targets(_FNS, (), max_targets=10, public_index=_INDEX)
    assert len(got_src) == len(got_root) == 2 and "add" in got_src
    # Control: the pre-change join keyed the raw mapping by the record's file.
    assert all(t.name not in _RAW.get(t.file, frozenset()) for t in types)


def test_the_index_roots_every_spelling_of_one_file_alike() -> None:
    idx = PublicIndex.of({"src/counter.rs": ["Counter"], "./lib.rs": ["add"]})
    for spelling in ("src/counter.rs", "counter.rs", "./counter.rs", "/src/counter.rs"):
        assert idx.names_in(spelling) == {"Counter"}
    assert idx.names_in("src/lib.rs") == {"add"} and idx.names() == {"Counter", "add"}
    assert not PublicIndex.of({}) and PublicIndex.of(None).names() == frozenset()

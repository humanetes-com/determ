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
_INDEX = {"src/lib.rs": frozenset({"add"}),
          "src/counter.rs": frozenset({"Counter", "bump"})}


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

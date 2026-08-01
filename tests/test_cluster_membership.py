"""Cluster-membership use-cases — fake-injected collaborators, no monkeypatch."""
from pathlib import Path

from pipeline_core.application.reconstruct import cluster_membership as cm
from pipeline_core.application.reconstruct.cleanup_residuals import (
    filter_test_exercised,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.cluster_membership_report import ClusterMembershipReport
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock


def test_line_declares_mod_token_exact():
    assert cm._line_declares_mod(" mod nightly ;", "nightly")
    assert cm._line_declares_mod("pub(crate) mod ptr;", "ptr")
    # not a whole word
    assert not cm._line_declares_mod("mod nightlyx ;", "nightly")
    # 'mod' must be a word
    assert not cm._line_declares_mod("kommode nightly ;", "nightly")
    # needs the semicolon
    assert not cm._line_declares_mod("mod nightly { }", "nightly")


def test_file_cfg_predicate_regex_fallback_adjacent_mods(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text(
        " # [cfg (gated_feature)] mod nightly ;\n"
        " mod ptr ;\n"
        " mod wrapper ;\n",
        encoding="utf-8",
    )
    f = cm.file_cfg_predicate_regex_fallback
    assert f(tmp_path, "src/nightly.rs") == "gated_feature"
    assert f(tmp_path, "src/ptr.rs") is None
    assert f(tmp_path, "src/wrapper.rs") is None


def test_filter_test_exercised():
    stubs = [("src/a.rs", "used"), ("src/a.rs", "unused")]
    tests = [("tests/t.rs", "fn t() { used(); }")]
    assert filter_test_exercised(stubs, tests) == [("src/a.rs", "used")]
    assert filter_test_exercised([], tests) == []
    assert filter_test_exercised(stubs, []) == []


# --- collect_impl_blocks_for_cluster / collect_impl_refill_targets ---

def _member(name: str, is_stub: bool) -> FnRecord:
    return FnRecord(file="lib.rs", fn_name=name, container="impl Foo",
                    sig_text=f"fn {name}()", has_unimplemented_body=is_stub)


def test_collect_impl_blocks_for_cluster_drops_block_with_no_active_members():
    block = ImplBlock(file="lib.rs", container="impl Foo",
                      members=(_member("kind", False),), stub_members=())
    out = cm.collect_impl_blocks_for_cluster(
        None, object(),
        collect_impl_blocks=lambda _d: [block],
        fn_belongs=lambda *a, **k: False)   # nothing active in this cluster
    assert out == []


def test_collect_impl_blocks_for_cluster_keeps_all_filled_block_with_active_member():
    # The bug this fixes: a block with ZERO stub members (all filled, one wrong)
    # used to be dropped outright — unreachable for behavioral re-fill targeting.
    block = ImplBlock(file="lib.rs", container="impl Foo",
                      members=(_member("kind", False), _member("name", False)),
                      stub_members=())
    out = cm.collect_impl_blocks_for_cluster(
        None, object(),
        collect_impl_blocks=lambda _d: [block],
        fn_belongs=lambda _dir, _f, fn_name, _c, **_k: fn_name == "kind")
    assert len(out) == 1
    assert out[0].stub_members == ()          # no stub was active — narrowed to empty
    assert out[0].members == block.members    # members untouched


def test_collect_impl_blocks_for_cluster_narrows_stub_members_to_active_subset():
    block = ImplBlock(file="lib.rs", container="impl Foo",
                      members=(_member("kind", True), _member("name", True)),
                      stub_members=(_member("kind", True), _member("name", True)))
    out = cm.collect_impl_blocks_for_cluster(
        None, object(),
        collect_impl_blocks=lambda _d: [block],
        fn_belongs=lambda _dir, _f, fn_name, _c, **_k: fn_name == "kind")
    assert len(out) == 1
    assert [m.fn_name for m in out[0].stub_members] == ["kind"]


def test_collect_impl_blocks_for_cluster_drops_oracle_file_block():
    """This route enumerates impls from the TREE, not from the frontier, so the
    frontier's `filter_test_targets` never sees them. On 2026-08-01 it filled
    `tests/drop/mod.rs::DetectDrop` and `tests/test_ensure.rs::DebugforEnum<T>`
    among ten fixture bodies in the shared test oracle; two stopped compiling
    and the cluster went from 93/103 to a red build no fill could repair.

    `fn_belongs` says every member is cluster-active — the point is that the
    block never reaches the cluster question at all."""
    oracle = ImplBlock(
        file="tests/drop/mod.rs", container="impl DetectDrop",
        members=(_member("new", False),), stub_members=())
    prod = ImplBlock(file="src/error.rs", container="impl Error",
                     members=(_member("chain", False),), stub_members=())
    out = cm.collect_impl_blocks_for_cluster(
        None, object(),
        collect_impl_blocks=lambda _d: [oracle, prod],
        fn_belongs=lambda *a, **k: True)
    assert [b.file for b in out] == ["src/error.rs"]


def test_collect_impl_refill_targets_stamps_nonstub_frontier_member():
    block = ImplBlock(file="lib.rs", container="impl Foo",
                      members=(_member("kind", False), _member("name", False)),
                      stub_members=())
    out = cm.collect_impl_refill_targets(
        [block],
        tdd_frontier_keys={FnLocator("lib.rs", "kind")},
        tdd_frontier_tests=FailureFrontier.of_direct({FnLocator("lib.rs", "kind"): {"kind_is_wrong"}}))
    assert [m.fn_name for m in out[0].refill_members] == ["kind"]


def test_collect_impl_refill_targets_excludes_actual_stubs():
    # A member still IN stub_members is a normal stub fill, never a refill target
    # (even if it happens to be named in the frontier).
    block = ImplBlock(file="lib.rs", container="impl Foo",
                      members=(_member("kind", True),),
                      stub_members=(_member("kind", True),))
    out = cm.collect_impl_refill_targets(
        [block],
        tdd_frontier_keys={FnLocator("lib.rs", "kind")},
        tdd_frontier_tests=FailureFrontier.of_direct({FnLocator("lib.rs", "kind"): {"kind_is_wrong"}}))
    assert out[0].refill_members == ()


def test_collect_impl_refill_targets_noop_without_frontier():
    block = ImplBlock(file="lib.rs", container="impl Foo",
                      members=(_member("kind", False),), stub_members=())
    assert cm.collect_impl_refill_targets([block], None, None) == [block]
    assert cm.collect_impl_refill_targets([block], set(), {}) == [block]


class _Cluster:
    name = "stable"
    cfg_true: list[str] = []
    cfg_false = ["error_generic_member_access"]


_VTABLE_NEIGHBOUR = (
    "pub(crate) struct ErrorVTable {\n"
    "    object_drop_rest: unsafe fn(Own<ErrorImpl>, TypeId),\n"
    '    #[cfg(error_generic_member_access)]\n'
    "    object_backtrace: unsafe fn(Ref<ErrorImpl>) -> Option<&Backtrace>,\n"
    "}\n"
    "unsafe fn object_drop<E>(e: Own<ErrorImpl>) { unimplemented!() }\n"
)


def test_fn_cfg_predicates_stops_at_the_item_not_the_blank_line():
    # `object_drop` is ungated; the only `#[cfg]` above it belongs to the
    # struct field of the item before it, and no blank line separates them.
    assert cm.fn_cfg_predicates(_VTABLE_NEIGHBOUR, "object_drop") == []
    # the genuinely-gated neighbour still reports its own gate
    gated = "#[cfg(error_generic_member_access)]\nfn provide() { unimplemented!() }\n"
    assert cm.fn_cfg_predicates(gated, "provide") == ["error_generic_member_access"]


def test_ungated_fn_under_a_gated_neighbour_stays_in_the_cluster(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "error.rs").write_text(_VTABLE_NEIGHBOUR, encoding="utf-8")
    (src / "lib.rs").write_text("mod error;\n", encoding="utf-8")
    assert cm.fn_belongs_to_cluster(src, "error.rs", "object_drop", _Cluster(),
                                    mod_records=lambda _d: [])


def test_scan_for_cluster_prefers_the_extractors_per_fn_cfg_over_the_text_walk(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "error.rs").write_text(_VTABLE_NEIGHBOUR, encoding="utf-8")
    logged: list[str] = []
    out = cm.scan_unimplemented_for_cluster(
        src, _Cluster(),
        # extractor says ungated; a text walk over this file must not override it
        enumerate_fns=lambda _d: [{"file": "error.rs", "fn_name": "object_drop",
                                   "has_unimplemented_body": True,
                                   "cfg_predicates": []}],
        mod_records=lambda _d: [],
        log=logged.append)
    assert out == [FnLocator("error.rs", "object_drop")]
    assert not logged


def test_scan_for_cluster_names_the_skipped_stubs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "error.rs").write_text("fn provide() { unimplemented!() }\n", encoding="utf-8")
    logged: list[str] = []
    out = cm.scan_unimplemented_for_cluster(
        src, _Cluster(),
        enumerate_fns=lambda _d: [{"file": "error.rs", "fn_name": "provide",
                                   "has_unimplemented_body": True,
                                   "cfg_predicates": ["error_generic_member_access"]}],
        mod_records=lambda _d: [],
        log=logged.append)
    assert out == []
    # the denominator and the identity must both be readable without a probe
    assert "1 of 1 stub(s)" in logged[0]
    assert "error.rs::provide" in logged[0]


def test_record_route_reports_the_same_denominator_and_names_as_the_tuple_route():
    """The record form is the route the impl fill actually consumes, and it
    emitted a bare count long after its sibling was corrected — a skip line that
    cannot be told apart from a filter that dropped everything."""
    recs = [{"file": "src/error.rs", "fn_name": "provide", "cfg_predicates":
             ["error_generic_member_access"]},
            {"file": "src/error.rs", "fn_name": "object_drop", "cfg_predicates": []}]
    logged: list[str] = []
    out = cm.scan_unimplemented_records_for_cluster(
        Path("/x"), _Cluster(),
        scan_records=lambda _d: recs,
        fn_belongs=lambda _d, _f, fn, _c, fn_cfg_predicates: not fn_cfg_predicates,
        log=logged.append)
    assert [r["fn_name"] for r in out] == ["object_drop"]
    report = ClusterMembershipReport.parse(logged[0])
    assert report is not None
    assert report.total == 2 and report.kept == 1
    assert report.skipped == ("src/error.rs::provide",)

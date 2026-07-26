"""Cluster-membership use-cases — fake-injected collaborators, no monkeypatch."""
from pipeline_core.application.reconstruct import cluster_membership as cm
from pipeline_core.application.reconstruct.cleanup_residuals import (
    filter_test_exercised,
)
from pipeline_core.domain.attribution import FailureFrontier
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

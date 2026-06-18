"""FixPlan partition rules (P3.C) — pure, no cargo/model/tree."""
from __future__ import annotations

from pipeline_core.domain.fix_plan import plan_per_fn, plan_per_impl
from pipeline_core.domain.models import FnRecord, ImplBlock


def _always_writable(_rel: str) -> bool:
    return True


def test_per_fn_unique_goes_free_duplicate_deferred() -> None:
    frontier = {("src/a.rs", "foo"): {"t"}, ("src/a.rs", "dup"): {"t"}}
    plan = plan_per_fn(
        frontier,
        name_counts={("src/a.rs", "foo"): 1, ("src/a.rs", "dup"): 2},
        container_index={("src/a.rs", "foo"): "impl X"},
        writable=_always_writable)
    assert [(t.file_rel, t.fn_name, t.container) for t in plan.free_fns] == [
        ("src/a.rs", "foo", "impl X")]
    assert plan.deferred == (("src/a.rs", "dup"),)
    assert plan.has_units


def test_per_fn_skips_unwritable() -> None:
    frontier = {("tests/it.rs", "foo"): {"t"}}
    plan = plan_per_fn(frontier, name_counts={("tests/it.rs", "foo"): 1},
                       container_index={}, writable=lambda r: not r.startswith("tests/"))
    assert not plan.has_units and plan.free_fns == ()


def test_per_impl_groups_members_and_frees_loose() -> None:
    block = ImplBlock(file="src/m.rs", container="impl Foo",
                      members=(FnRecord(file="src/m.rs", fn_name="a"),
                               FnRecord(file="src/m.rs", fn_name="b")))
    fn_to_block = {("src/m.rs", "a"): block, ("src/m.rs", "b"): block}
    frontier = {("src/m.rs", "a"): {"t"}, ("src/m.rs", "b"): {"t"},
                ("src/free.rs", "loose"): {"t"}}
    plan = plan_per_impl(frontier, fn_to_block=fn_to_block,
                         container_index={}, writable=_always_writable)
    assert [t.container for t in plan.impl_groups] == ["impl Foo"]
    assert [t.fn_name for t in plan.free_fns] == ["loose"]

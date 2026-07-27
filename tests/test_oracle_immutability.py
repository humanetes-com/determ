"""Pins for the one predicate that says what a reconstruction may never rewrite."""
from __future__ import annotations

from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.oracle_immutability import is_oracle_fn, production_fns


def test_gen_tests_sibling_is_oracle_without_any_test_marker():
    """The relocation puts `#[cfg(test)]` on the parent's `mod` decl, so records
    inside the sibling carry neither the attr nor the cfg. A guard keyed on
    cfg_predicates alone is a no-op on exactly the files holding the oracle."""
    rec = FnRecord(file="src/error.gen_tests_2.rs", fn_name="fmt",
                   sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result")
    assert rec.is_test is False and rec.cfg_predicates == ()
    assert is_oracle_fn(rec)


def test_test_attr_and_test_cfg_are_each_sufficient():
    assert is_oracle_fn(FnRecord(file="src/ptr.rs", fn_name="t", is_test=True))
    assert is_oracle_fn(FnRecord(file="src/ptr.rs", fn_name="t",
                                 cfg_predicates=("test",)))


def test_production_fn_in_an_ordinary_file_is_not_oracle():
    assert not is_oracle_fn(FnRecord(file="src/error.rs", fn_name="chain"))
    assert not is_oracle_fn(FnRecord(file="src/gen_tests_helper.rs",
                                     fn_name="chain"))


def test_production_fns_keeps_order_and_drops_only_oracle():
    recs = [
        FnRecord(file="src/error.rs", fn_name="a"),
        FnRecord(file="src/error.gen_tests_1.rs", fn_name="b"),
        FnRecord(file="src/ptr.rs", fn_name="c", is_test=True),
        FnRecord(file="src/ptr.rs", fn_name="d"),
    ]
    assert [r.fn_name for r in production_fns(recs)] == ["a", "d"]

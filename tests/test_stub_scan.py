"""Unit coverage for the stub scanner's notion of "still unimplemented", and for
the shape it returns: an inventory that carries sites AND units by name.

The extractor's own `has_unimplemented_body` flag is purely syntactic. These
pins cover the shapes that hold no `unimplemented!()` yet implement nothing —
and the cardinality pin: a name at three impl sites is three sites and ONE
unit, and nothing a caller can iterate hands it three locators.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.stub_scan import scan_stub_inventory
from pipeline_core.domain.models import FnLocator


def test_scan_sees_a_fn_whose_body_only_redeclares_itself() -> None:
    """A body that is just a nested copy of its own fn holds no
    `unimplemented!()`, so the fn reads as FILLED while silently returning
    `()`. It must still scan as a stub, or an already-corrupted tree is never
    re-attempted."""
    recs: list[dict[str, Any]] = [
        {"file": "src/error.rs", "fn_name": "context_drop_rest",
         "has_unimplemented_body": False,
         "body_text": "unsafe fn context_drop_rest<C, E>(e: Own<E>)\n"
                      "{\n    ptr::write(p, v);\n}"},
        {"file": "src/error.rs", "fn_name": "real",
         "has_unimplemented_body": False, "body_text": "self.inner.fmt(f)"},
        {"file": "src/lib.rs", "fn_name": "stub",
         "has_unimplemented_body": True, "body_text": "unimplemented!()"},
    ]
    out = scan_stub_inventory(Path("/x"), enumerate_fns=lambda _p: recs)
    assert ("src/error.rs", "context_drop_rest") in out
    assert ("src/lib.rs", "stub") in out
    assert ("src/error.rs", "real") not in out


def test_scan_keeps_a_same_named_nested_helper_that_does_work() -> None:
    """Legal shadowing that actually runs code is a real implementation."""
    recs: list[dict[str, Any]] = [
        {"file": "src/lib.rs", "fn_name": "go", "has_unimplemented_body": False,
         "body_text": "fn go(n: i32) -> i32 { n + 1 }\ngo(self.n)"},
    ]
    out = scan_stub_inventory(Path("/x"), enumerate_fns=lambda _p: recs)
    assert not out and out.site_count == 0


def test_scan_distinguishes_ungated_from_unknown() -> None:
    recs: list[dict[str, Any]] = [
        {"file": "src/error.rs", "fn_name": "object_drop",
         "has_unimplemented_body": True, "cfg_predicates": []},
        {"file": "src/error.rs", "fn_name": "provide",
         "has_unimplemented_body": True,
         "cfg_predicates": ["error_generic_member_access"]},
    ]
    out = scan_stub_inventory(Path("/x"), enumerate_fns=lambda _p: recs)
    assert out.site_pairs() == (
        (FnLocator("src/error.rs", "object_drop"), ()),
        (FnLocator("src/error.rs", "provide"), ("error_generic_member_access",)))


def test_scan_reports_unknown_cfg_when_the_extractor_is_unavailable(tmp_path) -> None:
    """`None` (no authoritative list) must stay distinguishable from `()`
    (positively ungated) — only the former licenses a text-walk fallback."""
    (tmp_path / "lib.rs").write_text("fn stub() { unimplemented!() }\n", encoding="utf-8")
    out = scan_stub_inventory(tmp_path, enumerate_fns=lambda _p: None)
    assert out.site_pairs() == ((FnLocator("lib.rs", "stub"), None),)


def test_scan_never_returns_a_stub_in_an_oracle_file() -> None:
    """A stub is a fill target by definition, and no fill may rewrite a test.
    On 2026-08-01 a layout retrigger reverted `tests/common/mod.rs::bail_error`
    and `tests/test_context.rs::make_chain` to `unimplemented!()`; offering them
    back would have asked the model to re-author the specification."""
    recs: list[dict[str, Any]] = [
        {"file": "tests/common/mod.rs", "fn_name": "bail_error",
         "has_unimplemented_body": True, "body_text": "unimplemented!()"},
        {"file": "tests/test_context.rs", "fn_name": "make_chain",
         "has_unimplemented_body": True, "body_text": "unimplemented!()"},
        {"file": "src/error.gen_tests_1.rs", "fn_name": "fmt",
         "has_unimplemented_body": True, "body_text": "unimplemented!()"},
        {"file": "src/error.rs", "fn_name": "chain",
         "has_unimplemented_body": True, "body_text": "unimplemented!()"},
    ]
    out = scan_stub_inventory(Path("."), enumerate_fns=lambda _d: recs)
    assert out.unit_list() == [FnLocator("src/error.rs", "chain")]


def test_a_name_at_three_sites_is_three_sites_and_one_unit() -> None:
    """The plan's sentence. `fmt` in three impls of one file: the extractor
    reports three records; the scan hands back three sites and ONE unit, and
    a caller that wants a sequence gets the collapsed list — never three
    locators to iterate. The pre-fix scan returned `[fmt, fmt, fmt]`, which
    the cluster-entry re-seed of run `2973-66e50ad9` iterated (2026-09-04)."""
    recs: list[dict[str, Any]] = [
        {"file": "src/w.rs", "fn_name": "fmt", "has_unimplemented_body": True,
         "container": f"impl {trait_} for W"}
        for trait_ in ("Debug", "Display", "Other")
    ]
    out = scan_stub_inventory(Path("/x"), enumerate_fns=lambda _p: recs)
    assert (out.site_count, out.unit_count) == (3, 1)
    assert out.unit_list() == [FnLocator("src/w.rs", "fmt")]
    assert [s.site.container for s in out.by_unit[FnLocator("src/w.rs", "fmt")]] == [
        "impl Debug for W", "impl Display for W", "impl Other for W"]
    assert out.render_count() == "3 stub site(s) in 1 unit(s)"
    # The type has no sequence protocol: there is nothing to iterate by accident.
    assert not hasattr(out, "__iter__") and not hasattr(out, "__len__")

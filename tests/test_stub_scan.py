"""Unit coverage for the stub scanner's notion of "still unimplemented".

The extractor's own `has_unimplemented_body` flag is purely syntactic. These
pins cover the shapes that hold no `unimplemented!()` yet implement nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.stub_scan import (
    scan_unimplemented_functions,
)


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
    out = scan_unimplemented_functions(Path("/x"), enumerate_fns=lambda _p: recs)
    assert ("src/error.rs", "context_drop_rest") in out
    assert ("src/lib.rs", "stub") in out
    assert ("src/error.rs", "real") not in out


def test_scan_keeps_a_same_named_nested_helper_that_does_work() -> None:
    """Legal shadowing that actually runs code is a real implementation."""
    recs: list[dict[str, Any]] = [
        {"file": "src/lib.rs", "fn_name": "go", "has_unimplemented_body": False,
         "body_text": "fn go(n: i32) -> i32 { n + 1 }\ngo(self.n)"},
    ]
    out = scan_unimplemented_functions(Path("/x"), enumerate_fns=lambda _p: recs)
    assert out == []

"""Same-name splice collision pins.

anyhow's collision-heavy code — three `fn not` (the free `not(cond)` + the two
`Bool` impls), six `fn from` across the `From<…>` impls, eight `fn fmt` across
Debug/Display — was the plateau: a bare-name locate returns the FIRST definition,
so a body meant for one impl was spliced into another (`!self` into the self-less
free `not(cond)` → E0424; a `From` body into the wrong target type → E0308).

Two guarantees pinned here:
  1. The bare-name splice/signature helpers REFUSE (`ambiguous_name` / False) when
     the name is not unique — they can never silently corrupt a same-named sibling.
  2. The container-scoped path splices the body into the RIGHT impl and leaves the
     other same-named definitions untouched.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pipeline
from pipeline_core.application.reconstruct.fill_rules import (
    AttemptFailure,
    make_failed_attempt_body,
)
from pipeline_core.application.reconstruct.splice import (
    replace_fn_signature,
    restore_frozen_signature,
    splice_fn_body,
)
from pipeline_core.domain.reconstruction import SpliceOutcome

# anyhow's `__private` negation helper: one free fn + two trait impls, all `not`.
NOT_FIXTURE = """\
pub fn not(cond: impl Bool) -> bool {
    unimplemented!()
}
mod not {
    pub trait Bool {
        fn not(self) -> bool;
    }
    impl Bool for bool {
        fn not(self) -> bool {
            unimplemented!()
        }
    }
    impl Bool for &bool {
        fn not(self) -> bool {
            unimplemented!()
        }
    }
}
"""

# Two same-named private helpers (no container to disambiguate them by name).
DUP_PRIVATE = """\
fn helper() -> i32 {
    unimplemented!()
}
fn helper() -> i64 {
    unimplemented!()
}
"""


def _write(text: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d) / "src"
    p.mkdir()
    f = p / "lib.rs"
    f.write_text(text, encoding="utf-8")
    return f


# --- guard: bare-name paths refuse on collision -----------------------------
def test_splice_fn_body_refuses_ambiguous_name() -> None:
    f = _write(NOT_FIXTURE)
    # Three bodied `not` in the file → a bare-name splice cannot know which.
    out = splice_fn_body(f, "not", "!self")
    assert out == (False, "ambiguous_name", 0)
    # And it changed nothing (every `not` still an unimplemented stub).
    assert f.read_text(encoding="utf-8").count("unimplemented!()") == 3


def test_splice_fn_body_unique_name_still_splices() -> None:
    f = _write("fn only() -> i32 {\n    unimplemented!()\n}\n")
    out = splice_fn_body(f, "only", "42")
    assert out.ok and out.reason == "ok"
    assert "42" in f.read_text(encoding="utf-8")


def test_replace_fn_signature_refuses_ambiguous() -> None:
    f = _write(DUP_PRIVATE)
    assert replace_fn_signature(f, "helper", "fn helper() -> u8") is False
    assert "fn helper() -> u8" not in f.read_text(encoding="utf-8")


def test_restore_frozen_signature_refuses_ambiguous() -> None:
    f = _write(DUP_PRIVATE)
    assert restore_frozen_signature(f, "helper", "fn helper() -> u8") is False


# --- correctness: container-scoped splice hits the right impl ----------------
def _splice_scoped(f: Path, container: str, body: str) -> SpliceOutcome:
    return pipeline._APP.fill_roles.mut.splice_body_scoped(
        f, "not", container, "", body)


def test_scoped_splice_lands_in_named_impl_only() -> None:
    f = _write(NOT_FIXTURE)
    out = _splice_scoped(f, "impl Bool for bool", "!self")
    assert out.ok, out.reason
    text = f.read_text(encoding="utf-8")
    # The `Bool for bool` method got the body; the free `not(cond)` and the
    # `&bool` impl are untouched — no `self` ever reached the self-less free fn.
    assert "!self" in text
    assert text.count("unimplemented!()") == 2
    # The free `pub fn not(cond: impl Bool)` must NOT have received `!self`.
    free_fn = text.split("mod not", 1)[0]
    assert "!self" not in free_fn
    assert "unimplemented!()" in free_fn


def test_scoped_splice_ampbool_variant_independent() -> None:
    f = _write(NOT_FIXTURE)
    assert _splice_scoped(f, "impl Bool for bool", "!self").ok
    assert _splice_scoped(f, "impl Bool for &bool", "!*self").ok
    text = f.read_text(encoding="utf-8")
    # Both impls filled distinctly; only the free fn remains a stub.
    assert "!self" in text and "!*self" in text
    assert text.count("unimplemented!()") == 1


def test_scoped_splice_writes_the_annotated_stub_when_allowed() -> None:
    # The fix-loop exhaustion path re-stubs a fn with the failed attempt kept as
    # comments; that body ENDS in `unimplemented!()` on purpose, so the scoped
    # splice must forward `allow_stub_body`. Without the forward it is refused
    # `body_is_stub` every time and the failure history never reaches the next
    # pass — the annotate was dead in production while its own unit test passed.
    f = _write(NOT_FIXTURE)
    annotated = make_failed_attempt_body("let x = self;", "error[E0424]", 3,
                                         failure=AttemptFailure.COMPILE)
    refused = pipeline._APP.fill_roles.mut.splice_body_scoped(
        f, "not", "impl Bool for bool", "", annotated)
    assert refused == (False, "body_is_stub", 0)
    out = pipeline._APP.fill_roles.mut.splice_body_scoped(
        f, "not", "impl Bool for bool", "", annotated, allow_stub_body=True)
    assert out.ok, out.reason
    assert "let x = self;" in f.read_text(encoding="utf-8")


def test_scoped_splice_falls_back_to_refusal_without_container() -> None:
    # No container → routes to the bare-name splice, which refuses on collision.
    f = _write(NOT_FIXTURE)
    out = pipeline._APP.fill_roles.mut.splice_body_scoped(f, "not", "", "", "!self")
    assert out == (False, "ambiguous_name", 0)

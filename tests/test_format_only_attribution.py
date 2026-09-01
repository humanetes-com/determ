"""A failing test whose only contact with the crate is a formatting macro
(`format!("{:#?}", v)`) names no crate fn, so `behavior_frontier_from_texts`
gave it no owner — it was never dispatched and no sweep could move it
(`test_altdebug`, every sweep of the stable 98-plateau: `fail=7 frontier=6`,
0 of 68 393 fill prompts ever carried it). The fix attributes such a test
through the format trait its placeholder needs, scoped to the crate self-type
its file names. Reachability only — the unit becomes dispatchable; no score.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    _impl_trait_and_self,
    behavior_frontier_from_texts,
)
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.refill_stamp import format_traits_used
from pipeline_core.domain.rust_fn_headers import locate_fn


class _FakeStructure:
    def __init__(self, recs: list[dict[str, Any]]) -> None:
        self._recs = recs

    def enumerate_fns(self, _src_dir: Path) -> list[dict[str, Any]]:
        return self._recs


def _deps(recs: list[dict[str, Any]]) -> tuple[AttributionDeps, list[str]]:
    lines: list[str] = []
    return AttributionDeps(
        structure=_FakeStructure(recs),
        classify=lambda **_k: {},
        locate_fn=locate_fn,
        run_check_diag=lambda *_a, **_k: (0, "", []),
        derive_check_only_cmd=lambda c: c,
        is_gen_tests_file=lambda _f: False,
        log=lines.append,
    ), lines


# The crate's fmt/debug methods, as the extractor enumerates them on the live
# tree. `Debug for Error::fmt` is the DISPATCHER — already correct, it delegates
# the `{:#?}` case to `ErrorImpl::debug` (`impl ErrorImpl`, in src/fmt.rs), which
# is the wrong body (both branches write Display). `Debug for Error::fmt` also
# calls `core::fmt::Display::fmt(...)` in its non-alternate branch — the token
# `Display` must NOT be mistaken for a self-type.
_FMT_RECS = [
    {"file": "src/error.rs", "fn_name": "fmt", "container": "impl Display for Error",
     "body_text": "write!(f, \"{}\", self.0)"},
    {"file": "src/error.rs", "fn_name": "fmt", "container": "impl Debug for Error",
     "body_text": ("if formatter.alternate() {"
                   "  let r = self.inner.by_ref();"
                   "  unsafe { ErrorImpl::debug(r, formatter) }"
                   "} else {"
                   "  core::fmt::Display::fmt(first, formatter)?;"
                   "}")},
    {"file": "src/fmt.rs", "fn_name": "debug", "container": "impl ErrorImpl",
     "body_text": ("let err = Self::error(this);"
                   "if f.alternate() { write!(f, \"{}\", err) }"
                   "else { write!(f, \"{}\", err) }")},
    {"file": "src/wrapper.rs", "fn_name": "fmt",
     "container": "impl Debug for MessageError < M >",
     "body_text": "core::fmt::Debug::fmt(&self.0, f)"},
    {"file": "src/wrapper.rs", "fn_name": "fmt",
     "container": "impl Display for MessageError < M >",
     "body_text": "core::fmt::Display::fmt(&self.0, f)"},
]

# A file shaped like `tests/test_fmt.rs`: imports/builds `Error` (the bare token
# appears via `io::Error` and the expected Debug output), never names
# `MessageError`. `test_altdebug`'s only crate contact is `{:#?}`.
_TEST_FMT = (
    "use anyhow::{bail, Context, Result};\n"
    "use std::io;\n"
    "fn f() -> Result<()> {\n"
    "    bail!(io::Error::new(io::ErrorKind::PermissionDenied, \"oh no!\"));\n"
    "}\n"
    "const EXPECTED: &str = \"Custom { .. }\";\n"
    "#[test]\n"
    "fn test_altdebug() {\n"
    "    assert_eq!(EXPECTED, format!(\"{:#?}\", f().unwrap_err()));\n"
    "}\n"
)

_ALTDEBUG_FAILED = (
    "running 1 test\ntest test_altdebug ... FAILED\n\nfailures:\n\n"
    "---- test_altdebug stdout ----\nassertion `left == right` failed\n"
)


def test_helper_reads_format_traits() -> None:
    assert format_traits_used('format!("{:#?}", x)') == frozenset({"Debug"})
    assert format_traits_used('format!("{:?}", x)') == frozenset({"Debug"})
    assert format_traits_used('format!("{}", x)') == frozenset({"Display"})
    assert format_traits_used("x.to_string()") == frozenset({"Display"})
    assert format_traits_used("plain fn with no formatting") == frozenset()


def test_helper_parses_trait_and_self() -> None:
    assert _impl_trait_and_self("impl Debug for Error") == ("Debug", "Error")
    assert _impl_trait_and_self(
        "impl std :: fmt :: Display for TestErr") == ("Display", "TestErr")
    assert _impl_trait_and_self(
        "impl Debug for MessageError < M >") == ("Debug", "MessageError")
    assert _impl_trait_and_self("impl Error") is None      # inherent impl
    assert _impl_trait_and_self("") is None
    # The domain parsers, not a local split: a where-clause and a `dyn` self
    # type read as they do everywhere else in the pipeline.
    assert _impl_trait_and_self(
        "impl<E> Debug for ErrorImpl<E> where E: Debug") == ("Debug", "ErrorImpl")
    assert _impl_trait_and_self(
        "impl Debug for dyn StdError + Send") == ("Debug", "StdError")


def test_format_only_test_owns_the_implementer_not_the_dispatcher() -> None:
    # The fix's core assertion: test_altdebug owns EXACTLY src/fmt.rs::debug —
    # the wrong body — reached by following the one call from the already-correct
    # `Debug for Error::fmt` dispatcher. It does NOT own error.rs::fmt (correct,
    # a futile accessor pass) nor any wrapper/Display fmt.
    deps, lines = _deps(_FMT_RECS)
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, _ALTDEBUG_FAILED, {"tests/test_fmt.rs": _TEST_FMT}, Path("/x"))
    owners = {f"{k.file}::{k.fn_name}"
              for k, v in frontier.items() if "test_altdebug" in v}
    assert owners == {"src/fmt.rs::debug"}, owners
    assert any(s.fn_name == "debug" and "test_altdebug" in ts
               for s, ts in _sites.items())
    assert not any(s.fn_name == "from" and "test_altdebug" in ts
                   for s, ts in _sites.items())
    # The `Display::fmt` trait-dispatch call in the Error body must not attach
    # any Display impl (trait token is not a self-type).
    assert FnLocator("src/wrapper.rs", "fmt") not in frontier
    assert FnLocator("src/error.rs", "fmt") not in frontier
    assert any("formatting implementer" in ln for ln in lines)


def test_self_contained_fmt_is_its_own_owner() -> None:
    # NEGATIVE CONTROL for the follow-the-call clause: a crate whose matching
    # Debug fmt does NOT delegate to a sibling formatter (it IS the implementer)
    # is attributed to the fmt itself, not dropped.
    self_contained = [
        {"file": "src/mine.rs", "fn_name": "fmt",
         "container": "impl Debug for Error",
         "body_text": "write!(f, \"Error({:?})\", self.code)"},
    ]
    deps, _lines = _deps(self_contained)
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, _ALTDEBUG_FAILED, {"tests/test_fmt.rs": _TEST_FMT}, Path("/x"))
    assert frontier.get(FnLocator("src/mine.rs", "fmt")) == {"test_altdebug"}


def test_self_qualified_dispatch_resolves_to_the_dispatchers_self_type() -> None:
    # `Self::debug(r, f)` inside `impl Debug for Error` names Error's own
    # `debug` — the qualifier is resolved to the dispatcher's self-type before
    # matching, so the implementer is still the owner, not the dispatcher.
    recs = [
        {"file": "src/error.rs", "fn_name": "fmt", "container": "impl Debug for Error",
         "body_text": "if f.alternate() { unsafe { Self::debug(self.inner.by_ref(), f) } } else { Ok(()) }"},
        {"file": "src/fmt.rs", "fn_name": "debug", "container": "impl Error",
         "body_text": "write!(f, \"{}\", self)"},
    ]
    deps, _lines = _deps(recs)
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, _ALTDEBUG_FAILED, {"tests/test_fmt.rs": _TEST_FMT}, Path("/x"))
    owners = {f"{k.file}::{k.fn_name}"
              for k, v in frontier.items() if "test_altdebug" in v}
    assert owners == {"src/fmt.rs::debug"}, owners


def test_negative_control_self_type_absent_yields_no_owner() -> None:
    # NEGATIVE CONTROL = the pre-fix behaviour of the whole class: a formatting
    # test whose crate self-type is not named in its file gets NO owner. Here
    # the crate has ONLY wrapper `fmt` impls (MessageError), which the file
    # never names in CODE, so the fallback adds nothing — exactly as the
    # unpatched attributor left every such test.
    wrapper_only = [r for r in _FMT_RECS if r["file"] == "src/wrapper.rs"]
    deps, _lines = _deps(wrapper_only)
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, _ALTDEBUG_FAILED, {"tests/test_fmt.rs": _TEST_FMT}, Path("/x"))
    assert frontier == {}


def test_fallback_does_not_override_a_named_fn() -> None:
    # Additive/empty-only: a test that DOES name a crate fn attributes to that
    # fn and the format fallback never runs (no fmt entry appears for it).
    recs = _FMT_RECS + [
        {"file": "src/error.rs", "fn_name": "downcast", "container": ""}]
    named = (
        "#[test]\n"
        "fn t_named() {\n"
        "    assert_eq!(3, downcast(format!(\"{:#?}\", x)));\n"
        "}\n"
    )
    deps, lines = _deps(recs)
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, "test t_named ... FAILED\n---- t_named stdout ----\nboom\n",
        {"tests/t.rs": named}, Path("/x"))
    assert frontier.get(FnLocator("src/error.rs", "downcast")) == {"t_named"}
    assert FnLocator("src/error.rs", "fmt") not in frontier
    assert not any("formatting-macro test" in ln for ln in lines)


# ---------------------------------------------------------------------------
# String/char/comment spans do not attribute: a token inside a literal is not
# a call. The live shape (run `114436-9cb17382`): a Debug test owned a fn
# named `backtrace` solely through `set_var("RUST_BACKTRACE", …)` and the
# asserted substring `Stack backtrace`, and that false edge fed the futility
# escalation the producer family of an accessor the test never calls.
# ---------------------------------------------------------------------------

_MASK_RECS = [
    {"file": "src/report.rs", "fn_name": "make", "container": "impl Report",
     "body_text": "Report::default()"},
    {"file": "src/report.rs", "fn_name": "backtrace", "container": "impl Report",
     "body_text": "&self.trace"},
]

_STRING_ONLY_TEST = (
    "#[test]\n"
    "fn t_substring() {\n"
    "    std::env::set_var(\"RUST_BACKTRACE\", \"1\");\n"
    "    let r = make();\n"
    "    assert!(format!(\"{:?}\", r).contains(\"Stack backtrace\"));\n"
    "}\n"
)

_REAL_CALL_TEST = (
    "#[test]\n"
    "fn t_calls() {\n"
    "    let r = make();\n"
    "    assert!(r.backtrace().is_some());\n"
    "}\n"
)

_MASK_FAILED = "test t_substring ... FAILED\ntest t_calls ... FAILED\n"


def test_a_fn_named_only_inside_string_literals_is_not_attributed() -> None:
    from pipeline_core.domain.test_results import identifiers_in
    deps, _lines = _deps(_MASK_RECS)
    frontier, _sites, srcs = behavior_frontier_from_texts(
        deps, _MASK_FAILED, {"tests/t.rs": _STRING_ONLY_TEST}, Path("/x"))
    owners = {f"{k.file}::{k.fn_name}"
              for k, v in frontier.items() if "t_substring" in v}
    assert "src/report.rs::backtrace" not in owners, owners
    assert "src/report.rs::make" in owners, owners
    # Control (the pre-fix rule): the unmasked identifier scan DOES carry the
    # token — only the literal spans held it, so masking is what removed it.
    _trel, tsrc = srcs["t_substring"]
    assert "backtrace" in identifiers_in(tsrc)


def test_a_fn_actually_called_still_attributes() -> None:
    # Negative control for the mask: the same fn name invoked as CODE keeps
    # its edge.
    deps, _lines = _deps(_MASK_RECS)
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, _MASK_FAILED, {"tests/t.rs": _REAL_CALL_TEST}, Path("/x"))
    owners = {f"{k.file}::{k.fn_name}"
              for k, v in frontier.items() if "t_calls" in v}
    assert "src/report.rs::backtrace" in owners, owners


def test_an_owned_format_test_also_gains_the_implementer() -> None:
    """A test whose real calls are a fixture constructor plus
    `format!("{:#?}", …)` owns the constructor — and no constructor rewrite
    changes what Debug emits, so the implementer hop now runs beside the
    identifier owners instead of only for unowned tests."""
    recs = _FMT_RECS + [
        {"file": "src/error.rs", "fn_name": "make", "container": "impl Error",
         "body_text": "Error::default()"}]
    owned_fmt = (
        "use anyhow::Error;\n"
        "#[test]\n"
        "fn t_owned_fmt() {\n"
        "    let e = make();\n"
        "    assert_eq!(\"X\", format!(\"{:#?}\", e));\n"
        "}\n"
    )
    deps, lines = _deps(recs)
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, "test t_owned_fmt ... FAILED\n",
        {"tests/t.rs": owned_fmt}, Path("/x"))
    owners = {f"{k.file}::{k.fn_name}"
              for k, v in frontier.items() if "t_owned_fmt" in v}
    assert "src/error.rs::make" in owners, owners
    assert "src/fmt.rs::debug" in owners, owners
    assert any("renders through a format trait" in ln for ln in lines)


def test_a_non_format_owned_test_does_not_run_the_hop() -> None:
    # Negative control: ownership alone never invokes the hop — only a format
    # trait in the test's own source does.
    recs = _FMT_RECS + [
        {"file": "src/error.rs", "fn_name": "make", "container": "impl Error",
         "body_text": "Error::default()"}]
    plain = (
        "use anyhow::Error;\n"
        "#[test]\n"
        "fn t_plain() {\n"
        "    let e = make();\n"
        "    assert!(e.is_real());\n"
        "}\n"
    )
    deps, lines = _deps(recs)
    frontier, _sites, _srcs = behavior_frontier_from_texts(
        deps, "test t_plain ... FAILED\n",
        {"tests/t.rs": plain}, Path("/x"))
    owners = {f"{k.file}::{k.fn_name}"
              for k, v in frontier.items() if "t_plain" in v}
    assert owners == {"src/error.rs::make"}, owners
    assert not any("format" in ln for ln in lines)

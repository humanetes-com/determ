"""Pins the prompt source-view cluster at its new home
(application/reconstruct/source_view.py, 2026-06-29 relocation)."""
from __future__ import annotations

from pipeline_core.application.reconstruct.source_view import (
    annotate_file_view,
    condense_rust_to_signatures,
)
from pipeline_core.domain.models import FnSite

SRC = (
    "pub fn a() -> i32 {\n"
    "    let x = 1;\n"
    "    x + 1\n"
    "}\n"
    "fn b() -> i32 { unimplemented!() }\n"
)


def test_condense_elides_bodies() -> None:
    out = condense_rust_to_signatures(SRC)
    # the fn signature survives; the multi-line body of `a` is elided
    assert "pub fn a() -> i32" in out
    assert "x + 1" not in out


def test_annotate_marks_target_and_stub() -> None:
    out = annotate_file_view(SRC, FnSite("src/lib.rs", "b", ""), "fn b() -> i32",
                             doc_keep_full_limit=10_000)
    assert ">>> TARGET" in out
    # `b` is the target; `a` is a real (non-stub) body → no annotation on it
    assert "SKELETON STUB" not in out
    assert "fn b() -> i32" in out


def test_annotate_non_target_stub_gets_skeleton_note() -> None:
    src = "fn x() { unimplemented!() }\nfn y() { unimplemented!() }\n"
    out = annotate_file_view(src, FnSite("src/lib.rs", "x", ""), "fn x()",
                             doc_keep_full_limit=10_000)
    assert ">>> TARGET" in out          # x marked target
    assert "SKELETON STUB" in out        # y marked as later-filled stub


def test_annotate_condenses_when_over_limit() -> None:
    # doc_keep_full_limit=0 forces the condense-docs branch; output still valid
    doc = "/// long doc line that would be condensed\npub fn a() -> i32 { 1 }\n"
    out = annotate_file_view(doc, FnSite("src/lib.rs", "a", ""), "pub fn a() -> i32",
                             doc_keep_full_limit=0)
    assert "pub fn a() -> i32" in out


# ── the TARGET is a definition, not a name ────────────────────────────────────

# Two impls of one trait for one generic type, each holding a stub of the same
# method with the same signature: only the enclosing impl tells them apart.
_TWO_IMPLS = (
    "impl<A, B> Show for Wrap<A, B>\n"
    "where\n"
    "    B: Show + 'static,\n"
    "{\n"
    "    fn reveal<'a>(&'a self, out: &mut Sink<'a>) {\n"
    "        ::core::unimplemented!()\n"
    "    }\n"
    "}\n"
    "impl<A> Show for Wrap<A, Plain>\n"
    "where\n"
    "    A: Show,\n"
    "{\n"
    "    fn reveal<'a>(&'a self, out: &mut Sink<'a>) {\n"
    "        ::core::unimplemented!()\n"
    "    }\n"
    "}\n"
)
_REVEAL_SIG = "fn reveal<'a>(&'a self, out: &mut Sink<'a>)"


def _marked_impl(view: str) -> list[str]:
    """The impl header above each TARGET marker, in order."""
    out = []
    for i, line in enumerate(view.splitlines()):
        if ">>> TARGET" in line:
            above = [ln for ln in view.splitlines()[:i] if ln.startswith("impl")]
            out.append(above[-1])
    return out


def test_a_prompt_for_one_definition_marks_only_that_definition() -> None:
    """Run `240506-87538963` (2026-10-19): the task 3 retry's prompt for the
    first of two same-named, same-signature methods marked BOTH as TARGET. The
    reply went to the first; the red test panicked in the second. The container
    comes in the extractor's token-spaced rendering, as the retry passes it."""
    second = FnSite("src/w.rs", "reveal", "impl Show for Wrap < A , Plain >")
    view = annotate_file_view(_TWO_IMPLS, second, _REVEAL_SIG,
                              doc_keep_full_limit=10_000)
    assert _marked_impl(view) == ["impl<A> Show for Wrap<A, Plain>"]
    assert view.count("SKELETON STUB") == 1

    first = FnSite("src/w.rs", "reveal", "impl Show for Wrap < A , B >")
    view = annotate_file_view(_TWO_IMPLS, first, _REVEAL_SIG,
                              doc_keep_full_limit=10_000)
    assert _marked_impl(view) == ["impl<A, B> Show for Wrap<A, B>"]


def test_a_target_the_text_cannot_place_is_still_marked_by_name() -> None:
    """A container no impl in the view carries narrows to nothing, and a tier
    that narrows to nothing does not narrow: the model is never shown a view
    with no TARGET at all."""
    nowhere = FnSite("src/w.rs", "reveal", "impl Show for Elsewhere")
    view = annotate_file_view(_TWO_IMPLS, nowhere, _REVEAL_SIG,
                              doc_keep_full_limit=10_000)
    assert len(_marked_impl(view)) == 2


def test_a_free_fn_is_told_apart_from_a_same_named_method() -> None:
    src = ("fn reveal() -> u8 { unimplemented!() }\n"
           "impl Show for Plain {\n"
           "    fn reveal() -> u8 { unimplemented!() }\n"
           "}\n")
    view = annotate_file_view(src, FnSite("src/w.rs", "reveal", ""),
                              "fn reveal() -> u8", doc_keep_full_limit=10_000)
    lines = view.splitlines()
    assert ">>> TARGET" in lines[0]
    assert "SKELETON STUB" in lines[3]


def test_both_per_fn_builders_mark_the_definition_their_container_names() -> None:
    """The builders are the annotator's only callers; each must hand it the
    container it prints on its `In:` line, or the view and that line disagree."""
    from functools import partial

    from pipeline_core.prompts import runtime
    from pipeline_core.prompts.context import PerFnFixSections, PerFnSections
    from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt, build_per_fn_prompt

    old = runtime._DEPS
    try:
        runtime.bind(runtime.PromptRuntimeDeps(annotate_file_view=partial(
            annotate_file_view, doc_keep_full_limit=10_000)))
        empty = {"macros": "", "notes": "", "container": "impl Show for Wrap < A , Plain >",
                 "types": "", "invariants": "", "siblings": "", "crate_idioms": "",
                 "bound_methods": ""}
        common = {"file_text": _TWO_IMPLS, "fn_name": "reveal", "sig_text": _REVEAL_SIG,
                  "test_blocks": [], "module_name": "m", "cluster_name": "stable",
                  "file_rel": "src/w.rs"}
        first = build_per_fn_prompt(**common, sections=PerFnSections(**empty))
        fix = build_per_fn_fix_prompt(
            **common, prev_body="::core::unimplemented!()", error_excerpt="E",
            sections=PerFnFixSections(**empty, retrospective_guidance=""))
        for prompt in (first, fix):
            assert _marked_impl(prompt) == ["impl<A> Show for Wrap<A, Plain>"]
    finally:
        runtime.bind(old)

"""Per-fn retry prompt keys on LastErrorKind, not cargo-check for a run fault.

The live constructor `build_per_fn_fix_prompt` opened `Previous attempt
broke cargo check` and wrapped a REGRESSED excerpt in COMPILE_ERRORS
while the same process already stored AttemptFailure.TEST. Pair already
used last_error_prefix; this builder did not. Invented names only.
"""
from __future__ import annotations

from pipeline_core.domain.last_error import (
    RED_HEAD_MARK,
    LastErrorKind,
    last_error_prefix,
)
from pipeline_core.prompts.context import PerFnFixSections
from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt


def _sections() -> PerFnFixSections:
    return PerFnFixSections(
        macros="", notes="", container="impl Holder", types="",
        invariants="", siblings="", crate_idioms="", bound_methods="",
        retrospective_guidance="try a different pointer")


def _prompt(*, excerpt: str, kind: LastErrorKind | None) -> str:
    return build_per_fn_fix_prompt(
        file_text="fn widen() { unimplemented!() }", fn_name="widen",
        sig_text="fn widen(&self) -> u8", test_blocks=[],
        module_name="demo", cluster_name="stable", file_rel="src/lib.rs",
        prev_body="self.0[0]", error_excerpt=excerpt,
        sections=_sections(), fr_path=None, kind=kind)


def test_reddened_retry_does_not_say_broke_cargo_check() -> None:
    excerpt = (
        f"This compiled, but it {RED_HEAD_MARK}: ['t_sib']\n"
        "panicked at src/lib.rs:4:1")
    p = _prompt(excerpt=excerpt, kind=LastErrorKind.REDDENED)
    assert "broke cargo check" not in p
    assert "<COMPILE_ERRORS>" not in p
    assert "<LAST_ERROR>" in p
    assert last_error_prefix(excerpt, LastErrorKind.REDDENED).rstrip() in p
    assert "body rustc accepted that then failed at run time" in p


def test_compile_retry_is_today_s_cargo_check_text() -> None:
    """Negative control: a rustc rejection keeps the pre-change opener."""
    p = _prompt(excerpt="error[E0308]: mismatched types",
                kind=LastErrorKind.COMPILE)
    assert "Previous attempt broke cargo check." in p
    assert "<COMPILE_ERRORS>" in p
    assert "<LAST_ERROR>" not in p
    omitted = _prompt(excerpt="error[E0308]: mismatched types", kind=None)
    assert "Previous attempt broke cargo check." in omitted
    assert "<COMPILE_ERRORS>" in omitted

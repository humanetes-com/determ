"""Frozen-bound mismatch detection (domain) + retro fast-path."""
from __future__ import annotations

from pipeline_core.domain.bound_mismatch import (
    detect_frozen_bound_mismatch,
    frozen_bounds,
    path_leaf,
    unsatisfied_trait_bounds,
)

# The live anyhow case (run 2026-06-23): frozen impl bounds E to ext::StdError,
# every in-scope constructor requires core::error::Error.
_CONTAINER = ("impl<T, E> Context<T, E> for Result<T, E> "
              "where E: ext::StdError + Send + Sync + 'static")
_ERR = ("Errors in target file:\n"
        "error[E0277]: the trait bound `E: core::error::Error` is not satisfied\n"
        "  --> src/context.rs:54:31\n")


def test_frozen_bounds_from_where_clause() -> None:
    b = frozen_bounds("fn context(self, ctx: C) -> Result<T, Error>", _CONTAINER)
    assert b["E"] == ("ext::StdError", "Send", "Sync")


def test_frozen_bounds_inline_generics_and_hrtb() -> None:
    b = frozen_bounds(
        "fn f<E: StdError, F>(e: E, f: F) -> i32 where F: for<'a> Fn(&'a str) -> i32",
        "")
    assert b["E"] == ("StdError",)
    assert b["F"] == ("Fn(&'a str) -> i32",)


def test_unsatisfied_trait_bounds_parses_e0277_param_lines_only() -> None:
    pairs = unsatisfied_trait_bounds(_ERR)
    assert pairs == [("E", "core::error::Error")]
    # non-param subjects (concrete types) are ignored
    assert unsatisfied_trait_bounds(
        "error[E0277]: the trait bound `&(A, B): NotBothDebug` is not satisfied"
    ) == []


def test_detect_the_anyhow_case() -> None:
    mm = detect_frozen_bound_mismatch("fn context(self, ctx: C) -> Result<T, Error>",
                                      _CONTAINER, _ERR)
    assert mm is not None
    assert mm.param == "E"
    assert mm.required == "core::error::Error"
    assert "ext::StdError" in mm.frozen


def test_no_mismatch_when_leaf_names_agree() -> None:
    # frozen `E: StdError`, required `std::error::Error as StdError`-style same
    # leaf: `E: fmt::Debug` vs required `core::fmt::Debug` → NOT a mismatch.
    err = "error[E0277]: the trait bound `E: core::fmt::Debug` is not satisfied"
    assert detect_frozen_bound_mismatch(
        "fn f<E>(e: E) where E: fmt::Debug", "", err) is None


def test_no_mismatch_for_unconstrained_param() -> None:
    err = "error[E0277]: the trait bound `E: core::error::Error` is not satisfied"
    assert detect_frozen_bound_mismatch("fn f<E>(e: E)", "", err) is None


def test_no_mismatch_without_e0277_lines() -> None:
    assert detect_frozen_bound_mismatch(
        "fn f<E>(e: E) where E: ext::StdError", "",
        "error[E0308]: mismatched types") is None


def test_path_leaf() -> None:
    assert path_leaf("core::error::Error") == "Error"
    assert path_leaf("Context<T, E>") == "Context"
    assert path_leaf("StdError") == "StdError"


def test_retro_fast_path_parks_without_llm_call() -> None:
    from pipeline_core.application.reconstruct.retrospective import (
        PhaseCRetroDeps,
        diagnose_phase_c,
    )
    calls: list[str] = []

    def ask(prompt: str) -> str:
        calls.append(prompt)
        return "CATEGORY: API_HALLUCINATION\nGUIDANCE: nope"

    deps = PhaseCRetroDeps(ask_retrospective=ask, log=lambda *_: None,
                           failed_body_cap=2000, fix_err_cap=2000)
    out = diagnose_phase_c(
        "context", "fn context(self, ctx: C) -> Result<T, Error>", _CONTAINER,
        "src/context.rs", "stable", "let x = 1;", _ERR, 2, deps)
    assert out["category"] == "FROZEN_BOUND_MISMATCH"
    assert out["signal"] == "frozen_bound_mismatch"
    assert "core::error::Error" in out["guidance"]
    assert calls == []  # structural — no LLM call burned


# --- edge branches (coverage debt, s119 handover: 13 missed lines) -----------


def test_double_colon_paths_never_split_bound_entries() -> None:
    """`::` in paths (both scan directions) must not be taken as the bound
    colon; `dyn ` prefixes are stripped from bound paths."""
    got = frozen_bounds(
        "fn f<T: std::fmt::Debug + dyn my::Trait>(t: T) -> T", "")
    assert got == {"T": ("std::fmt::Debug", "my::Trait")}


def test_where_entry_without_colon_is_skipped() -> None:
    assert frozen_bounds("fn f<T>(t: T) where T", "") == {}


def test_paren_close_inside_generics_list_walk() -> None:
    """`(...)` nesting inside the `<...>` list (fn-pointer bound) keeps depth."""
    got = frozen_bounds("fn f<F: Fn(u8) -> u8, T: Clone>(f: F, t: T)", "")
    assert got == {"F": ("Fn(u8) -> u8",), "T": ("Clone",)}


def test_associated_type_where_entry_is_skipped_not_missplit() -> None:
    """`Self::Item: Clone` in a where clause — the `::` is walked over (not
    taken as the bound colon) and the non-identifier param is then skipped."""
    got = frozen_bounds("fn f<T: Iterator>(t: T) where T::Item: Clone", "")
    assert got == {"T": ("Iterator",)}


def test_split_first_bound_colon_degenerate_colon_runs() -> None:
    """Path-only text (no bound colon) → None; a `:::` run (colon whose
    previous char is a colon after the `::` skip) is also stepped over."""
    from pipeline_core.domain.bound_mismatch import _split_first_bound_colon
    assert _split_first_bound_colon("std::fmt::Debug") is None
    assert _split_first_bound_colon("a:::b") is None


def test_unclosed_generics_list_yields_nothing() -> None:
    assert frozen_bounds("fn f<T: Clone", "") == {}


def test_lifetime_and_const_generic_entries_are_skipped() -> None:
    got = frozen_bounds("fn f<'a, const N: usize, T: Copy>(t: &'a T)", "")
    assert got == {"T": ("Copy",)}


def test_unsatisfied_lines_missing_marker_or_backtick_or_colon() -> None:
    err = ("error[E0277]: something is not satisfied\n"                # no marker
           "error[E0277]: the trait bound `E: core::error::Error\n"    # no close `
           "error[E0277]: the trait bound `NoColonHere` is not satisfied\n")
    assert unsatisfied_trait_bounds(err) == []


def test_detect_skips_params_without_frozen_bounds() -> None:
    """E0277 on `U` (unconstrained) with `T` frozen: not a mismatch."""
    err = "error[E0277]: the trait bound `U: Display` is not satisfied\n"
    assert detect_frozen_bound_mismatch(
        "fn f<T: Clone, U>(t: T, u: U)", "", err) is None

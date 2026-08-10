"""BUILD-mode attribution of macro-expansion errors.

An error raised inside a macro expansion renders its primary span at the macro
DEFINITION, where there is usually no fn record — on 2026-08-10 (run
`2514529-5e77dafb`) two E0605s pointed at `src/backtrace.rs:21` inside
`backtrace_if_absent!`'s nightly variant, `frontier_from_diags` returned an
empty frontier, and BUILD mode aborted `unattributed_compile_errors` while the
two by-value call sites (`src/error.rs::new` line 47, `::from_boxed` line 151)
sat parsed in `Diagnostic.macro_call_sites`. These tests replay that recorded
pair against a tree shaped like the incident's.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.attribution import (
    AttributionDeps,
    frontier_from_diags,
)
from pipeline_core.domain.models import Diagnostic, FnLocator


def _file_text(n_lines: int) -> str:
    return "".join(f"// pad line {i}\n" for i in range(1, n_lines + 1))


def _line_span(text: str, first: int, last: int) -> tuple[int, int]:
    """Byte offsets of lines [first, last] (1-based, inclusive) in `text`."""
    lines = text.splitlines(keepends=True)
    start = sum(len(ln) for ln in lines[: first - 1])
    end = start + sum(len(ln) for ln in lines[first - 1 : last])
    return start, end


def _tree(tmp_path):
    """A tree shaped like the incident's: backtrace.rs has one fn record whose
    body does NOT span the macro-definition line 21 (`_assert_send_sync` at
    37), error.rs has `new` spanning line 47 and `from_boxed` spanning 151."""
    src = tmp_path / "src"
    src.mkdir()
    backtrace = _file_text(45)
    error = _file_text(160)
    (src / "backtrace.rs").write_text(backtrace, encoding="utf-8")
    (src / "error.rs").write_text(error, encoding="utf-8")
    bt_s, bt_e = _line_span(backtrace, 36, 38)
    new_s, new_e = _line_span(error, 45, 50)
    fb_s, fb_e = _line_span(error, 149, 155)
    recs = [
        {"file": "src/backtrace.rs", "fn_name": "_assert_send_sync",
         "sig_byte_start": bt_s, "body_byte_end": bt_e},
        {"file": "src/error.rs", "fn_name": "new",
         "sig_byte_start": new_s, "body_byte_end": new_e},
        {"file": "src/error.rs", "fn_name": "from_boxed",
         "sig_byte_start": fb_s, "body_byte_end": fb_e},
    ]

    class _S:
        def enumerate_fns(self, _sd):
            return recs

        def enumerate_types(self, _sd):
            return []

    deps = AttributionDeps(
        structure=_S(), classify=lambda *a, **k: {},
        locate_fn=lambda *a, **k: None,
        run_check_diag=lambda *a, **k: (0, "", []),
        derive_check_only_cmd=lambda c: c, is_gen_tests_file=lambda n: False,
        log=lambda m: None,
    )
    return deps, tmp_path


def _e0605(call_sites: tuple[tuple[str, int], ...]) -> Diagnostic:
    return Diagnostic(
        code="E0605", file="src/backtrace.rs", line=21,
        message="non-primitive cast: `E` as `&dyn core::error::Error`",
        macro_call_sites=call_sites,
    )


def test_macro_expansion_errors_attribute_to_their_call_sites(tmp_path):
    deps, src_dir = _tree(tmp_path)
    diags = [_e0605((("src/error.rs", 47),)),
             _e0605((("src/error.rs", 151),))]
    frontier = frontier_from_diags(deps, diags, src_dir)
    assert set(frontier) == {FnLocator("src/error.rs", "new"),
                             FnLocator("src/error.rs", "from_boxed")}


def test_negative_control_primary_span_alone_attributes_nothing(tmp_path):
    """The pre-fix behaviour on the recorded input: the primary span is the
    macro definition, the only fn record there does not span it, and the code
    never read `macro_call_sites` — an empty frontier. Stripping the call
    sites reproduces that path exactly (the field is the whole difference)."""
    deps, src_dir = _tree(tmp_path)
    diags = [_e0605(()), _e0605(())]
    assert frontier_from_diags(deps, diags, src_dir) == {}


def test_primary_span_wins_when_it_resolves(tmp_path):
    """Non-macro behaviour is byte-identical: a diagnostic whose primary span
    lands inside a fn body attributes there, and any recorded call sites are
    never consulted."""
    deps, src_dir = _tree(tmp_path)
    d = Diagnostic(code="E0308", file="src/error.rs", line=47,
                   message="mismatched types",
                   macro_call_sites=(("src/error.rs", 151),))
    frontier = frontier_from_diags(deps, [d], src_dir)
    assert set(frontier) == {FnLocator("src/error.rs", "new")}


def test_first_resolving_call_site_wins(tmp_path):
    """A nested expansion records one call site per level; the first that
    resolves to a fn record is the attribution (outermost-first, as rustc
    records the chain)."""
    deps, src_dir = _tree(tmp_path)
    d = _e0605((("src/backtrace.rs", 21), ("src/error.rs", 151)))
    frontier = frontier_from_diags(deps, [d], src_dir)
    assert set(frontier) == {FnLocator("src/error.rs", "from_boxed")}

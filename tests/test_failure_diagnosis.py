"""Characterization for the relocated attribution-cluster diagnoses (P6).
Each use-case is driven through INJECTED fakes (no monkeypatch), pinning the
classifier parse, the regression-note hook, and the cargo-error implicator."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.failure_diagnosis import (
    classify_phase_c_test_failures,
    implicated_fns_from_cargo_errors,
    implicated_fns_from_check,
    implicated_fns_from_diagnostics,
    record_tdd_regression,
)
from pipeline_core.domain.models import Diagnostic


def test_classify_parses_lines_and_drops_unknown() -> None:
    out_lines = (
        "TEST t_bad: BAD_FILL [fn=src/a.rs::foo] — wrong body\n"
        "TEST t_env: ENV_DEPENDENT — missing file\n"
        "TEST t_x: NONSENSE_CATEGORY — dropped\n"
    )
    logged: list[str] = []
    res = classify_phase_c_test_failures(
        "cargo tail", [("src/a.rs", "foo")], ["t_bad", "t_env", "t_x"], "stable",
        ask=lambda *a, **k: out_lines, log=logged.append, model="m", err_cap=1000,
    )
    assert res["t_bad"] == {"category": "BAD_FILL", "fn": "src/a.rs::foo", "reason": "wrong body"}
    assert res["t_env"]["category"] == "ENV_DEPENDENT"
    assert "t_x" not in res  # unknown category dropped


def test_classify_empty_on_no_failures() -> None:
    assert classify_phase_c_test_failures(
        "", [], [], "c", ask=lambda *a, **k: "x", log=lambda s: None,
        model="m", err_cap=10) == {}


def test_classify_swallows_llm_error() -> None:
    logged: list[str] = []

    def boom(*a: object, **k: object) -> str:
        raise RuntimeError("vllm down")

    res = classify_phase_c_test_failures(
        "tail", [], ["t1"], "c", ask=boom, log=logged.append, model="m", err_cap=5)
    assert res == {} and logged and "LLM call failed" in logged[0]


def test_record_tdd_regression_writes_note_for_broken_fn(tmp_path: Path) -> None:
    fr = tmp_path / "fr.md"
    notes: list[tuple[Path, str]] = []
    recs = [{"fn_name": "foo", "file": "src/a.rs", "body_text": "0 // wrong"}]
    msg = record_tdd_regression(
        fr, tmp_path, [("src/a.rs", "foo")], {"t1", "t2"}, 14, 11,
        enumerate_fns=lambda d: recs, append_note=lambda p, n: notes.append((p, n)),
        body_cap=200,
    )
    assert msg == "regressed t1, t2"
    assert notes and "`foo`" in notes[0][1] and "0 // wrong" in notes[0][1]


def test_implicated_fns_backward_scan_fallback(tmp_path: Path) -> None:
    src = tmp_path / "a.rs"
    src.write_text("fn helper() {\n    let x = 1;\n}\n", encoding="utf-8")
    cargo = "error[E0308]: mismatched types\n  --> a.rs:2:13\n"
    # enumerate_fns=None forces the first_fn_decl_name backward walk.
    pairs = implicated_fns_from_cargo_errors(cargo, tmp_path, enumerate_fns=lambda d: None)
    assert pairs == [("a.rs", "helper")]


# The blind spot the seeding heal fell into: an error whose PRIMARY span lands
# in a macro body (which encloses no fn) is attributed to nothing by the
# `-->`-only text path, so the fn that INVOKED the macro is never reverted and
# its layout-incompatible seeded body keeps the whole cluster red. The
# structured path reaches it through the diagnostic's macro call-sites.
def test_macro_hidden_error_attributed_to_invoking_fn(tmp_path: Path) -> None:
    # `mac.rs` holds a top-level macro (no enclosing fn). `caller.rs` invokes it
    # inside `build`. rustc reports the error in the macro body; the invocation
    # call-site is `caller.rs::build`.
    (tmp_path / "mac.rs").write_text(
        "macro_rules! wrap {\n    ($e:expr) => { $e as &dyn X };\n}\n",
        encoding="utf-8")
    (tmp_path / "caller.rs").write_text(
        "fn build(v: E) -> Self {\n    Self::mk(wrap!(v))\n}\n", encoding="utf-8")
    recs = [
        {"fn_name": "build", "file": "caller.rs",
         "sig_byte_start": 0, "body_byte_end": 46},
    ]

    diag = Diagnostic(
        code="E0605", file="mac.rs", line=2, message="non-primitive cast",
        macro_call_sites=(("caller.rs", 2),))

    # Text path sees only the `-->` primary (mac.rs:2), which encloses no fn.
    text = ("error[E0605]: non-primitive cast\n  --> mac.rs:2:20\n")
    assert implicated_fns_from_cargo_errors(
        text, tmp_path, enumerate_fns=lambda d: recs) == []
    # Structured path follows the macro call-site to the invoking fn.
    assert implicated_fns_from_diagnostics(
        [diag], tmp_path, enumerate_fns=lambda d: recs) == [("caller.rs", "build")]


def test_implicated_fns_from_diagnostics_unions_primary_and_macro(tmp_path: Path) -> None:
    # A diagnostic contributes BOTH its own body-span fn and every macro
    # call-site fn; duplicates across diagnostics collapse, order preserved.
    (tmp_path / "a.rs").write_text(
        "fn direct() {\n    bad();\n}\n", encoding="utf-8")
    (tmp_path / "caller.rs").write_text(
        "fn build() {\n    wrap!(v);\n}\n", encoding="utf-8")
    recs = [
        {"fn_name": "direct", "file": "a.rs",
         "sig_byte_start": 0, "body_byte_end": 24},
        {"fn_name": "build", "file": "caller.rs",
         "sig_byte_start": 0, "body_byte_end": 26},
    ]
    diags = [
        Diagnostic(code="E0308", file="a.rs", line=2, message="x"),
        Diagnostic(code="E0605", file="mac.rs", line=9, message="y",
                   macro_call_sites=(("caller.rs", 2),)),
        Diagnostic(code="E0605", file="mac.rs", line=9, message="z",
                   macro_call_sites=(("caller.rs", 2),)),  # dup call-site
    ]
    assert implicated_fns_from_diagnostics(
        diags, tmp_path, enumerate_fns=lambda d: recs) == [
            ("a.rs", "direct"), ("caller.rs", "build")]


def test_implicated_fns_from_check_prefers_structured_over_rendered(
        tmp_path: Path) -> None:
    """Every revert gate shares one policy: structured diagnostics when cargo
    gave any, the rendered scan only when it gave none. The gate that skipped
    the structured half read "no implicated fns" on a macro-hidden error and
    gave up with the cluster red."""
    (tmp_path / "caller.rs").write_text(
        "fn build() {\n    wrap!(v);\n}\n", encoding="utf-8")
    (tmp_path / "plain.rs").write_text(
        "fn other() {\n    bad();\n}\n", encoding="utf-8")
    recs = [
        {"fn_name": "build", "file": "caller.rs",
         "sig_byte_start": 0, "body_byte_end": 26},
        {"fn_name": "other", "file": "plain.rs",
         "sig_byte_start": 0, "body_byte_end": 25},
    ]
    diag = Diagnostic(code="E0605", file="mac.rs", line=9, message="cast",
                      macro_call_sites=(("caller.rs", 2),))
    rendered = "error[E0308]: mismatch\n  --> plain.rs:2:5\n"

    # Diagnostics present → the rendered text is NOT consulted, so the macro
    # call-site wins and plain.rs::other is not dragged in.
    assert implicated_fns_from_check(
        [diag], rendered, tmp_path, enumerate_fns=lambda d: recs) == [
            ("caller.rs", "build")]
    # No diagnostics (cargo-level failure, no JSON) → the text scan is the
    # fallback, not silence.
    assert implicated_fns_from_check(
        [], rendered, tmp_path, enumerate_fns=lambda d: recs) == [
            ("plain.rs", "other")]
    assert implicated_fns_from_check(
        [], "", tmp_path, enumerate_fns=lambda d: recs) == []


def test_error_outside_every_fn_implicates_nobody(tmp_path: Path) -> None:
    """`nightly`, 2026-08-01: a crash retrigger re-authored `struct Error` into
    `src/lib.rs` and left the previous install's `unsafe impl Send for Error {}`
    standing below it. rustc answered E0119 at line 5 here — a top-level item,
    inside no fn.

    The extractor said so: `line_to_fn_record` returned None. The backward line
    scan then overruled it, walked up to the nearest thing shaped like a fn
    declaration and named `source` — a fn 3 lines above the error and behind a
    `#[cfg]` this build does not compile. The compile-blocked frontier narrows
    to the fns the compiler implicates, so that one unfixable fn was
    re-dispatched every iteration of a 26-iteration grace window.

    No fn body can delete a duplicate top-level impl. Attributing one to a fn
    is not a lead, it is a wrong answer that reads exactly like a right one.
    """
    src = tmp_path / "lib.rs"
    src.write_text(
        "trait StdError {\n"                       # 1
        "    fn source(&self) -> Option<&Self> {\n"  # 2
        "        None\n"                             # 3
        "    }\n"                                    # 4
        "}\n"                                        # 5
        "unsafe impl Send for Error {}\n"            # 6
        "unsafe impl Sync for Error {}\n",           # 7
        encoding="utf-8")
    recs = [{"fn_name": "source", "file": "lib.rs",
             "sig_byte_start": 21, "body_byte_end": 60}]
    cargo = ("error[E0119]: conflicting implementations of trait `Send`\n"
             "  --> lib.rs:7:1\n")
    assert implicated_fns_from_cargo_errors(
        cargo, tmp_path, enumerate_fns=lambda d: recs) == []
    # An error INSIDE the fn is still attributed to it.
    inside = ("error[E0308]: mismatched types\n  --> lib.rs:3:9\n")
    assert implicated_fns_from_cargo_errors(
        inside, tmp_path, enumerate_fns=lambda d: recs) == [("lib.rs", "source")]


def test_backward_scan_still_covers_a_file_the_extractor_never_parsed(
    tmp_path: Path,
) -> None:
    """The scan is the fallback for a file with no records at all — a parse
    failure, or an extractor that could not run. Silencing it there would drop
    the attribution that reverts a body whose unbalanced braces escaped to file
    scope, since a file in that state is exactly the one syn cannot read."""
    (tmp_path / "a.rs").write_text("fn helper() {\n    let x = 1;\n}\n",
                                   encoding="utf-8")
    (tmp_path / "b.rs").write_text("fn other() {}\n", encoding="utf-8")
    recs = [{"fn_name": "other", "file": "b.rs",
             "sig_byte_start": 0, "body_byte_end": 13}]
    cargo = "error[E0308]: mismatched types\n  --> a.rs:2:13\n"
    assert implicated_fns_from_cargo_errors(
        cargo, tmp_path, enumerate_fns=lambda d: recs) == [("a.rs", "helper")]


def test_record_tdd_regression_multi_fill_writes_one_event_note(tmp_path: Path) -> None:
    """201 slice D: a per-fn accusation is written only when the causal claim
    holds (one filled body). A 12-body pass used to produce 12 identical
    accusations each admitting it was 1/12 likely to be about its fn — 29 such
    notes sat on disk blaming 29 different fns (measured 2026-08-22). A
    multi-body regression now records ONE note naming the event and its
    filled set, accusing nobody."""
    fr = tmp_path / "fr.md"
    notes: list[tuple[Path, str]] = []
    recs = [{"fn_name": "foo", "file": "src/a.rs", "body_text": "0 // wrong"},
            {"fn_name": "bar", "file": "src/b.rs", "body_text": "1 // wrong"}]
    msg = record_tdd_regression(
        fr, tmp_path, [("src/a.rs", "foo"), ("src/b.rs", "bar")],
        {"t1"}, 14, 11,
        enumerate_fns=lambda d: recs, append_note=lambda p, n: notes.append((p, n)),
        body_cap=200,
    )
    assert msg == "regressed t1"
    assert len(notes) == 1, "one note per regression EVENT, not per fn"
    note = notes[0][1]
    assert "2 bodies" in note and "no single body is accused" in note
    assert "src/a.rs::foo" in note and "src/b.rs::bar" in note
    assert "after `" not in note, "the event note accuses no subject"
    assert "0 // wrong" not in note, "no body — the causal claim is 1/N"


def test_record_tdd_regression_single_fill_still_accuses(tmp_path: Path) -> None:
    """The len==1 case keeps the concrete WRONG-body accusation — that claim
    actually holds, and it is the actionable half the model reacts to."""
    fr = tmp_path / "fr.md"
    notes: list[tuple[Path, str]] = []
    recs = [{"fn_name": "foo", "file": "src/a.rs", "body_text": "0 // wrong"}]
    record_tdd_regression(
        fr, tmp_path, [("src/a.rs", "foo")], {"t1"}, 14, 11,
        enumerate_fns=lambda d: recs, append_note=lambda p, n: notes.append((p, n)),
        body_cap=200,
    )
    assert len(notes) == 1
    assert "after `foo` was written" in notes[0][1]
    assert "0 // wrong" in notes[0][1]
    assert "the culprit is ONE of them" not in notes[0][1]

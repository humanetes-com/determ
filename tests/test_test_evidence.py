"""What a committed test block may carry into the next prompt
(`domain/test_evidence.py`): the code without the model's comments, and the
measured fact that a function it was aimed at still has 0 executions."""
from __future__ import annotations

from pipeline_core.domain.test_evidence import REMOVED_NOTE, still_unexecuted, without_model_comments

# tests/wrapper_gen_3.rs on anyhow, cut down: the comment is the false premise.
_BLOCK = """#![allow(unused)]
use anyhow::*;

// The DisplayError<M> wrapper is used internally for Display-only error
// messages.  `anyhow!("...")` and the format-args path create an Error whose
// inner object is a DisplayError.
#[test]
fn test_display_error_debug_anyhow_literal() {
    let err = anyhow!("debug via anyhow");
    /// a doc comment the model wrote
    let alt = format!("{:#?}", err);
}"""


def test_the_models_comments_leave_the_paste_and_the_paste_says_so() -> None:
    out = without_model_comments(_BLOCK)
    assert out.startswith(REMOVED_NOTE)
    assert "DisplayError" not in out and "doc comment" not in out
    for kept in ("#![allow(unused)]", "use anyhow::*;", "#[test]", 'let alt = format!("{:#?}", err);'):
        assert kept in out


def test_negative_control_code_without_comments_is_unchanged() -> None:
    code = "#[test]\nfn t() { let x = 1; }"
    assert without_model_comments(code) == code


def test_the_zero_line_names_the_files_that_ran_and_only_the_still_unexecuted() -> None:
    note = still_unexecuted({"src/wrapper.rs::fmt [impl Debug for DisplayError<M>]":
                             ["tests/wrapper_gen_3.rs", "tests/wrapper_gen_3.rs", "tests/wrapper_gen_4.rs"],
                             "src/lib.rs::gone": ["tests/lib_gen_1.rs"]},
                            ["src/wrapper.rs::fmt [impl Debug for DisplayError<M>]"])
    assert note.endswith(
        "- `tests/wrapper_gen_3.rs`, `tests/wrapper_gen_4.rs` ran; "
        "`src/wrapper.rs::fmt [impl Debug for DisplayError<M>]` still has 0 executions.\n")
    assert "src/lib.rs::gone" not in note
    assert still_unexecuted({}, ["x"]) == ""

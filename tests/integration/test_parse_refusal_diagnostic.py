"""Integration: a splice the parser refuses comes back with the parser's own
diagnostic.

Real rustfmt (rustc's parser), the real syn extractor as the splice gate, the
real splice. No model, no cargo build.

The class, run `473471-33301154` (2026-09-29): a reply body ending in a cast
to `&dyn A + B + 'static` was refused `parse_broken` and the unit's fix loop
ended at attempt 1 with nothing to tell the model. rustc's parser says exactly
what to do about that text; the pipeline never asked it.

The fix rests on rustfmt printing rustc's parse diagnostics for a source file
on standard input (`error: …`, a ` --> <stdin>:L:C` location, the offending
line, a `help:`). `test_premise_*` pins that; if rustfmt stops doing it the
refusal degrades to the pipeline's own one-line reason and says so here.

Gated on `INTEGRATION=1`; `scripts/run_integration.sh` is the launcher.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.adapters.rust_parse import ParseCheck, rustfmt_parse_diagnostic
from pipeline_core.adapters.structure import CliStructureReader
from pipeline_core.application.reconstruct.splice import splice_fn_body
from pipeline_core.domain.reconstruction import SpliceRefusal
from pipeline_core.domain.splice_feedback import reply_refusal_feedback

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("INTEGRATION") != "1",
                       reason="real rustfmt + extractor; set INTEGRATION=1"),
    pytest.mark.skipif(shutil.which("rustfmt") is None,
                       reason="rustfmt is not installed"),
]

_REPO = Path(__file__).resolve().parents[2]
_EXTRACTOR = (_REPO / "tools" / "skeleton_extractor" / "target" / "release"
              / "skeleton-extractor")

_SRC = """pub trait Shape {}

pub fn widen(p: &u8) -> u8 {
    unimplemented!()
}
"""
# The recorded slip, on invented names: `+` in a cast type needs parentheses.
_AMBIGUOUS = "let _r = p as &dyn Shape + Send + 'static;\n*p"
_FINE = "*p"


def _gate() -> ParseCheck:
    assert _EXTRACTOR.exists(), (
        f"{_EXTRACTOR} missing — build it (bash pre_run.sh does) before the "
        "integration tests; they read the real extractor")
    return ParseCheck(CliStructureReader(CliExtractor(_EXTRACTOR)).parse_ok)


def test_premise_rustfmt_prints_the_parsers_diagnostic_for_stdin() -> None:
    text = _SRC.replace("unimplemented!()", _AMBIGUOUS)
    diag = rustfmt_parse_diagnostic(text)
    assert "ambiguous `+` in a type" in diag and "<stdin>" in diag, (
        "rustfmt no longer prints rustc's parse error for a file on stdin; a "
        "refused reply would be told only the pipeline's own reason.\n" + diag)
    assert "parentheses" in diag
    assert rustfmt_parse_diagnostic(_SRC) == ""


def test_a_body_the_parser_rejects_is_refused_with_that_diagnostic(
        tmp_path: Path) -> None:
    f = tmp_path / "lib.rs"
    f.write_text(_SRC)
    out = splice_fn_body(f, "widen", _AMBIGUOUS, validate_parse=_gate())
    assert not out.ok and out.reason == "parse_broken"
    assert isinstance(out, SpliceRefusal) and "ambiguous `+`" in out.diagnostic
    assert f.read_text() == _SRC                      # nothing was written
    feedback = reply_refusal_feedback(out, unit="widen", file_rel="src/lib.rs")
    assert feedback is not None
    assert "--> src/lib.rs:" in feedback and "try adding parentheses" in feedback
    # NEGATIVE CONTROL: the gate as it was — a bare callable, no diagnosis —
    # refuses the same body with nothing to say about it.
    bare = splice_fn_body(f, "widen", _AMBIGUOUS,
                          validate_parse=_gate().parse_ok)
    assert not bare.ok and getattr(bare, "diagnostic", "") == ""


def test_a_body_that_parses_is_written_as_before(tmp_path: Path) -> None:
    f = tmp_path / "lib.rs"
    f.write_text(_SRC)
    out = splice_fn_body(f, "widen", _FINE, validate_parse=_gate())
    assert out.ok and "*p" in f.read_text()

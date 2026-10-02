"""The splice gate's parse check, and what the parser says when it fails
(adapter).

The gate itself is the syn extractor's `parse_ok` over the written file. A
refusal needs more than a boolean: the reply that caused it has to be told
what is wrong, in the compiler's words. rustfmt runs rustc's own parser and
prints its diagnostics, on standard input, without a crate, a build or a
target directory — on the recorded body of run `473471-33301154` it prints
``error: ambiguous `+` in a type`` with ``help: try adding parentheses``,
which is the sentence the model needed.

:class:`ParseCheck` is the validator every splice site already receives; it
is callable as before and adds `diagnose(text)`.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_MAX_CHARS = 3000


def rustfmt_parse_diagnostic(text: str, *, edition: str = "2021",
                             timeout: int = 30) -> str:
    """rustc's parser diagnostics for `text` as one source file, or "" when it
    parses, rustfmt is not installed, or the run does not finish. Only the
    `error` blocks; formatting output is discarded."""
    try:
        run = subprocess.run(
            ["rustfmt", "--edition", edition, "--emit", "stdout"],
            input=text, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if run.returncode == 0:
        return ""
    err = (run.stderr or "").strip()
    start = err.find("error")
    return err[start:start + _MAX_CHARS] if start >= 0 else ""


@dataclass(frozen=True, slots=True)
class ParseCheck:
    """`path -> parses?` for the splice gate, plus `text -> diagnostic`."""

    parse_ok: Callable[[Path], bool]
    diagnose_text: Callable[[str], str] = rustfmt_parse_diagnostic

    def __call__(self, path: Path) -> bool:
        return self.parse_ok(path)

    def diagnose(self, text: str) -> str:
        return self.diagnose_text(text)

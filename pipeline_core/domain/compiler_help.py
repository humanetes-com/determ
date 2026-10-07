"""What the compiler itself says would fix a compile error (pure domain).

rustc attaches a `help:` to many errors, and the concrete ones carry the
code: an added line (`2 + use alloc::format;`) or an inline replacement
(`help: a macro with a similar name exists: `print``). A retry prompt that
pastes such a diagnostic and then says "do not resubmit the same approach"
tells the model the opposite of what the compiler said.

Run `486447-5207101c` (2026-10-07, cluster `stable`), fill 2, file route
`src/ensure.rs`: the first reply for `render` formatted both operands with
`format!` and failed `cannot find macro format`, the crate being `no_std`;
rustc offered `use crate::__private::format;`, `use std::format;`, `use
alloc::format;`. The retry prompt (`e58cedda…`) quoted all three under
"the bodies below must resolve them; do not resubmit the same approach", and
the reply dropped the operands instead (`let _ = (lhs, rhs); Error::msg(msg)`),
which compiled and was kept.

:func:`compiler_fixes` reads the suggestions; the LAST_ERROR lead-in
(`last_error.last_error_prefix`) uses them. Generic: it reads rustc's own
rendering, names no crate and no symbol.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["CompilerError", "CompilerFix", "CompilerHelp", "compiler_fixes"]

_ERROR_HEAD = re.compile(r"^error(\[E\d{4}\])?:", re.MULTILINE)
_HELP = re.compile(r"^\s*(?:=\s*|\d*\s*\|\s*[\^\-~ ]*[\^\-~]\s+)?help:\s*(.*)$")
_ADDED = re.compile(r"^\s*\d*\s*\+\s?(.*\S)\s*$")
_REPLACED = re.compile(r"^\s*\d*\s*~\s?(.*\S)\s*$")
_INLINE = re.compile(r"^(.*?):\s*`([^`]+)`\s*$")
_AT = re.compile(r"^\s*-->\s*(\S+)")


@dataclass(frozen=True, slots=True)
class CompilerFix:
    """One `help:` that carries code: what it says, and the code it offers
    (several when rustc lists alternatives, any one of which fixes it)."""

    help: str
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompilerError:
    """One error of a diagnostic: its head line, where rustc put it, and the
    code-carrying fixes rustc offered for it (empty when none)."""

    head: str
    at: str
    fixes: tuple[CompilerFix, ...]


@dataclass(frozen=True, slots=True)
class CompilerHelp:
    """The fixes rustc offered across a diagnostic, and whether every error
    in it came with one. `per_error` keeps them with the error they fix, so
    a lead can say which error keeps its body and which does not."""

    errors: int
    fixes: tuple[CompilerFix, ...]
    errors_with_fix: int
    per_error: tuple[CompilerError, ...] = ()

    @property
    def covers_every_error(self) -> bool:
        return self.errors > 0 and self.errors_with_fix == self.errors


def _block_fixes(block: str) -> list[CompilerFix]:
    out: list[CompilerFix] = []
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        m = _HELP.match(lines[i])
        if m is None:
            i += 1
            continue
        message = m.group(1).strip()
        choices: list[str] = []
        inline = _INLINE.match(message)
        if inline is not None:
            choices.append(inline.group(2).strip())
            message = inline.group(1).strip()
        j = i + 1
        while j < len(lines):
            ln = lines[j]
            if _HELP.match(ln) or _ERROR_HEAD.match(ln) or ln.lstrip().startswith(
                    ("note:", "= note:", "warning:")):
                break
            added = _ADDED.match(ln) or _REPLACED.match(ln)
            if added is not None and "|" not in ln.split("+", 1)[0].split("~", 1)[0]:
                code = added.group(1).strip()
                if code and code not in choices:
                    choices.append(code)
            j += 1
        if choices:
            out.append(CompilerFix(message, tuple(choices)))
        i = j
    return out


def compiler_fixes(diagnostic: str) -> CompilerHelp:
    """The code-carrying `help:` suggestions of every error in `diagnostic`."""
    text = diagnostic or ""
    starts = [m.start() for m in _ERROR_HEAD.finditer(text)]
    fixes: list[CompilerFix] = []
    per_error: list[CompilerError] = []
    with_fix = 0
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(text)
        block = text[start:end]
        block_fixes = _block_fixes(block)
        if block_fixes:
            with_fix += 1
            fixes.extend(f for f in block_fixes if f not in fixes)
        lines = block.splitlines()
        at = next((m.group(1) for m in map(_AT.match, lines[1:4]) if m), "")
        per_error.append(CompilerError(lines[0].strip() if lines else "", at,
                                       tuple(block_fixes)))
    return CompilerHelp(errors=len(starts), fixes=tuple(fixes),
                        errors_with_fix=with_fix, per_error=tuple(per_error))

"""Cargo error-text shaping for fix prompts (P2 slice 4) — warning strip,
target-file error focusing, E-code scanning. All regex-free: the inputs are
rustc's stable text format, parsed with plain line/boundary scans (the
structured-diagnostics JSON path is preferred where diags exist — R8.3;
these helpers shape arbitrary captured TEXT where they don't).
"""

from __future__ import annotations

import json

from pipeline_core.domain.models import Diagnostic


def is_error_header(ln: str) -> bool:
    """True for a rustc error-block header line: `error`, `error:`,
    `error[E0308]: ...` — "error" at line start followed by a word
    boundary (regex-free form of `^error\\b`)."""
    if not ln.startswith("error"):
        return False
    return len(ln) == 5 or not (ln[5].isalnum() or ln[5] == "_")


def has_rs_line_loc(ln: str) -> bool:
    """True when the line carries a `<file>.rs:<line>` location (regex-free
    form of `\\.rs:\\d+`)."""
    i = 0
    while True:
        i = ln.find(".rs:", i)
        if i == -1:
            return False
        if i + 4 < len(ln) and ln[i + 4].isdigit():
            return True
        i += 4


def ecodes_in_text(text: str) -> set[str]:
    """All rustc error codes (`E0308`-style: E + 3-4 digits at word
    boundaries) in `text` — regex-free scan."""
    out: set[str] = set()
    i, n = 0, len(text)
    while True:
        i = text.find("E", i)
        if i == -1:
            return out
        if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
            i += 1
            continue
        j = i + 1
        while j < n and text[j].isdigit():
            j += 1
        ndig = j - i - 1
        if 3 <= ndig <= 4 and (j >= n or not (text[j].isalpha() or text[j] == "_")):
            out.add(text[i:j])
        i = j if j > i + 1 else i + 1


def excerpt_for_code(output: str, code: str, *, max_following_lines: int = 6,
                     max_chars: int = 600) -> str | None:
    """The `error[<code>]:` header line plus up to `max_following_lines`
    lines after it, stripped and capped — the per-code context excerpt
    (regex-free form of `error\\[c\\][^\\n]*\\n(?:[^\\n]*\\n){0,6}`)."""
    needle = f"error[{code}]"
    lines = output.splitlines()
    for i, ln in enumerate(lines):
        if needle in ln:
            chunk = "\n".join(lines[i:i + 1 + max_following_lines])
            chunk = chunk.strip()[:max_chars]
            return chunk or None
    return None


def strip_cargo_warnings(output: str) -> str:
    """Drop `warning:` blocks from cargo output. A block runs from the
    `warning:` header until the next blank line (rustc separates each block
    with a blank line). Used as a defensive filter — the model should never
    see warnings, only errors.
    """
    out: list[str] = []
    skip = False
    for ln in output.splitlines():
        if ln.startswith("warning:") or ln.startswith("warning["):
            skip = True
            continue
        if skip:
            if ln.strip() == "":
                # blank line terminates the warning block
                skip = False
            # while skipping, drop every non-blank line of the block
            continue
        out.append(ln)
    return "\n".join(out)


def focus_cargo_errors(output: str, file_rel: str, max_chars: int) -> str:
    """Extract a focused error excerpt from cargo output.

    P3.7 tiered output. Two groups of error blocks:
      - In-target-file: blocks whose `-->` mentions `file_rel`'s basename
      - Cross-file: blocks whose `-->` mentions any other .rs file
    Returned with a header demarcating the two. Budget split 60/40 in favour
    of in-target. Warnings dropped first.

    A bad fill in error.rs can break compilation in lib.rs (downstream
    consumer of error.rs). Without the cross-file slice, the model would
    not see the real symptom — only the absence of expected errors in its
    own file.
    """
    output = strip_cargo_warnings(output)
    base = file_rel.rsplit("/", 1)[-1]
    in_blocks: list[list[str]] = []
    cross_blocks: list[list[str]] = []
    in_err_block = False
    cur_block: list[str] = []
    has_target = False
    has_any_loc = False
    for ln in output.splitlines():
        if is_error_header(ln):
            if in_err_block and cur_block:
                if has_target:
                    in_blocks.append(cur_block)
                elif has_any_loc:
                    cross_blocks.append(cur_block)
            cur_block = [ln]
            in_err_block = True
            has_target = False
            has_any_loc = False
        elif in_err_block:
            cur_block.append(ln)
            if base in ln:
                has_target = True
            if has_rs_line_loc(ln):
                has_any_loc = True
            if ln.strip() == "" and cur_block:
                if has_target:
                    in_blocks.append(cur_block)
                elif has_any_loc:
                    cross_blocks.append(cur_block)
                cur_block = []
                in_err_block = False
                has_target = False
                has_any_loc = False
    if in_err_block and cur_block:
        if has_target:
            in_blocks.append(cur_block)
        elif has_any_loc:
            cross_blocks.append(cur_block)

    if not in_blocks and not cross_blocks:
        return output[:max_chars]

    in_budget = int(max_chars * 0.6)
    cross_budget = max_chars - in_budget
    parts: list[str] = []
    if in_blocks:
        parts.append("# Errors in target file:")
        used = 0
        for b in in_blocks:
            txt = "\n".join(b)
            if used + len(txt) + 1 > in_budget:
                break
            parts.append(txt)
            used += len(txt) + 1
    if cross_blocks:
        parts.append("# Errors elsewhere in the crate (likely downstream of the target):")
        used = 0
        for b in cross_blocks:
            txt = "\n".join(b)
            if used + len(txt) + 1 > cross_budget:
                break
            parts.append(txt)
            used += len(txt) + 1
    return "\n".join(parts)[:max_chars]


def diagnostics_from_json(output: str) -> list[Diagnostic] | None:
    """Walk `output` line-by-line, parse JSON cargo messages, return the
    `compiler-message`s with `level == "error"` as structured records.

    Returns None if the output contains no JSON cargo messages — caller
    falls back to the rendered-text scan. Empty list means "JSON present,
    zero errors" (an actual signal: a JSON cargo check that returned
    cleanly).
    """
    json_seen = False
    out: list[Diagnostic] = []
    for ln in output.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if obj.get("reason") != "compiler-message":
            json_seen = True  # JSON present even if not an error
            continue
        msg = obj.get("message") or {}
        if msg.get("level") != "error":
            json_seen = True
            continue
        json_seen = True
        code_field = msg.get("code") or {}
        code = (code_field.get("code") or "") if isinstance(code_field, dict) else ""
        spans = msg.get("spans") or []
        primary = next((s for s in spans if s.get("is_primary")), spans[0] if spans else {})
        out.append(Diagnostic(
            code=code,
            file=(primary.get("file_name") or "") if isinstance(primary, dict) else "",
            line=int(primary.get("line_start") or 0) if isinstance(primary, dict) else 0,
            message=msg.get("message") or "",
            rendered=msg.get("rendered") or "",
        ))
    return out if json_seen else None


def err_sig_from_diagnostics(diagnostics: list[Diagnostic]) -> frozenset[str]:
    """Build the stable error signature directly from structured
    diagnostics. Sig is `code:message` (or just `message` when no code) —
    same disambiguation power as the text sig but produced from rustc's own
    classification rather than guessed from rendered text."""
    sigs: set[str] = set()
    for d in diagnostics:
        code = d.code or ""
        msg = (d.message or "").strip()
        sigs.add(f"{code}:{msg}" if code else msg)
    return frozenset(sigs)


def err_sig_from_text(output: str) -> frozenset[str]:
    """Regex-free form of the legacy rendered-text error signature
    (`error\\[E\\d+\\]: .+`  |  `^error: .+`). For each line: an
    `error[E<digits>]: <≥1 char>` marker anywhere in the line contributes
    the substring from that marker to end-of-line; a line that STARTS with
    `error: <≥1 char>` contributes the whole line."""
    sigs: set[str] = set()
    for ln in output.splitlines():
        # error[E<digits>]: <≥1 char>  — anywhere, first marker wins (greedy .+ to EOL)
        i = ln.find("error[")
        while i != -1:
            j = i + len("error[")
            if j < len(ln) and ln[j] == "E":
                k = j + 1
                while k < len(ln) and ln[k].isdigit():
                    k += 1
                if k > j + 1 and ln[k:k + 3] == "]: " and len(ln) > k + 3:
                    sigs.add(ln[i:])
                    break
            i = ln.find("error[", i + len("error["))
        # ^error: <≥1 char>
        if ln.startswith("error: ") and len(ln) > len("error: "):
            sigs.add(ln)
    return frozenset(sigs)


def err_sig(output: str) -> frozenset[str]:
    """Stable error signature for `output`. Prefers the structured JSON
    diagnostics (catches non-E-coded failures: linker, build-script, cargo
    wrapper — the recurring "compile failed but errs=0" false positive);
    falls back to the regex-free rendered-text scan when no JSON is present."""
    diags = diagnostics_from_json(output)
    if diags is not None:
        return err_sig_from_diagnostics(diags)
    return err_sig_from_text(output)

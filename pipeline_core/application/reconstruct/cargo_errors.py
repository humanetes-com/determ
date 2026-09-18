"""Cargo error-text shaping for fix prompts — warning strip, target-file error
focusing, E-code scanning. All regex-free: the inputs are rustc's stable text
format, parsed with plain line/boundary scans (the structured-diagnostics JSON
path is preferred where diags exist; these helpers shape arbitrary captured
TEXT where they don't).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pipeline_core.domain.attribution import container_key
from pipeline_core.domain.import_repair import import_fixes_from_message
from pipeline_core.domain.models import Diagnostic, FnSite
from pipeline_core.domain.test_results import strip_ansi


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


_TYPE_ERROR_TRIGGERS = (
    "expected type", "mismatched types", "cannot find type",
    "no method named", "struct", "enum",
)


def error_type_signals(text: str) -> list[tuple[str, int]]:
    r"""Type names blamed in a rendered rustc error, each with its char index (so
    the caller can grab surrounding context). Regex-free, word-aware replacement
    for the old r'(?:expected type|…|struct|enum)\s+`?([A-Z][a-zA-Z0-9]+)`?' scan
    — which falsely matched the bare word `struct` *inside* e.g. `reconstruct`.
    For each trigger at a left word boundary, followed by whitespace, an optional
    backtick, then a CamelCase identifier ([A-Z] + ≥1 ascii-alnum), the name is
    recorded once (first occurrence wins)."""
    found: dict[str, int] = {}
    n = len(text)
    for trig in _TYPE_ERROR_TRIGGERS:
        start = 0
        while True:
            s = text.find(trig, start)
            if s == -1:
                break
            start = s + len(trig)
            if s > 0 and (text[s - 1].isalnum() or text[s - 1] == "_"):
                continue  # trigger is a substring of a larger word
            j = s + len(trig)
            if j >= n or not text[j].isspace():
                continue
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "`":
                j += 1
            if j >= n or not (text[j].isascii() and text[j].isupper()):
                continue
            k = j + 1
            while k < n and text[k].isascii() and text[k].isalnum():
                k += 1
            if k - j >= 2:  # [A-Z][a-zA-Z0-9]+ (≥2 chars)
                found.setdefault(text[j:k], j)
    return list(found.items())


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


def focus_diagnostics(diagnostics: list[Diagnostic], file_rel: str, *,
                      max_target: int = 8, max_cross: int = 4,
                      line_lo: int = 0, line_hi: int = 0,
                      locate: Callable[[str, int], FnSite | None] | None = None,
                      target_container: str = "") -> str:
    """Diagnostics-first error focusing: render rustc's OWN complete
    blocks, capped by diagnostic COUNT — never by characters — so the model
    always sees a diagnostic whole (an E0308 cut before its expected/found
    lines is unactionable; starvation-miner evidence, runs 6390/187469).

    Target-file diagnostics first, then — when a line window is set —
    same-file diagnostics outside that window under a THIS-FILE header,
    then other files under the crate-wide header. Same-file-other-impl
    must not share the crate-wide header: retro treats an excerpt that
    is only ``Errors elsewhere in the crate`` (no target section) as
    ``downstream_only`` and skips the informed retry. Run-shaped e2e
    fixtures with a diagnostic on line 1 of a file whose impl starts on
    line 3 then parked after round 1. ``line_*`` of 0 means no window
    (byte-identical to file-only focusing). A diagnostic with no line
    stays in the target bucket when the file matches.

    When ``locate`` and ``target_container`` are set, a same-file
    diagnostic whose line resolves to a fn is in-span iff that fn's
    container matches the target (canonical). Locate-miss and line 0
    keep the window fallback, so omitting locate is byte-identical to
    the pre-locate behaviour.

    ANSI stripped, duplicates and `aborting due to …` summaries
    dropped. Returns "" when nothing renders — callers fall back to the
    text path (:func:`focus_cargo_errors`)."""
    base = file_rel.rsplit("/", 1)[-1]
    target: list[str] = []
    samefile: list[str] = []
    cross: list[str] = []
    seen: set[str] = set()
    windowed = line_lo > 0 and line_hi > 0
    want_cont = container_key(target_container) if target_container else ""
    for d in diagnostics:
        text = strip_ansi(d.rendered or d.message).strip()
        if not text or "aborting due to" in text:
            continue
        if text in seen:
            continue
        seen.add(text)
        same_file = bool(d.file) and d.file.rsplit("/", 1)[-1] == base
        in_span = (not windowed or d.line <= 0
                   or line_lo <= d.line <= line_hi)
        if (locate is not None and want_cont and same_file and d.line > 0):
            hit = locate(d.file, d.line)
            if hit is not None:
                in_span = container_key(hit.container) == want_cont
        if same_file and in_span:
            target.append(text)
        elif same_file:
            samefile.append(text)
        else:
            cross.append(text)
    parts: list[str] = []
    if target:
        parts.append("# Errors in target file:")
        parts.extend(target[:max_target])
    if samefile:
        parts.append("# Errors elsewhere in this file "
                     "(other impls / items, not this block):")
        parts.extend(samefile[:max_cross])
    if cross:
        parts.append("# Errors elsewhere in the crate "
                     "(likely downstream of the target):")
        parts.extend(cross[:max_cross])
    return "\n".join(parts)


def focus_cargo_errors(output: str, file_rel: str, max_chars: int) -> str:
    """Extract a focused error excerpt from cargo output.

    Tiered output. Two groups of error blocks:
      - In-target-file: blocks whose `-->` mentions `file_rel`'s basename
      - Cross-file: blocks whose `-->` mentions any other .rs file
    Returned with a header demarcating the two. Budget split 60/40 in favour
    of in-target. Warnings dropped first.

    A bad fill in error.rs can break compilation in lib.rs (downstream
    consumer of error.rs). Without the cross-file slice, the model would
    not see the real symptom — only the absence of expected errors in its
    own file.

    ANSI is stripped FIRST: cargo's colored rendering prefixes `error[…]`
    headers with SGR escapes, which silently defeated `is_error_header` — no
    blocks parsed, and the model got a head-truncated raw dump (starvation
    miner, runs 6390/187469).
    """
    output = strip_cargo_warnings(strip_ansi(output))
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
            import_fixes=import_fixes_from_message(msg),
            macro_call_sites=macro_call_sites_from_spans(spans),
            secondary_spans=secondary_spans_from_spans(spans),
        ))
    return out if json_seen else None


def secondary_spans_from_spans(
    spans: list[dict[str, Any]],
) -> tuple[tuple[str, int], ...]:
    """Every labelled NON-primary span, deduped, in rustc's order.

    The other end of a cross-file error. `E0592` points its primary span at one
    definition and prints the other under `:::`; keeping only the primary left
    the second definition invisible to the frontier, so the repair could only
    ever rewrite the body of the one it could see."""
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for span in spans:
        if not isinstance(span, dict) or span.get("is_primary"):
            continue
        key = (str(span.get("file_name") or ""), int(span.get("line_start") or 0))
        if not key[0] or key[1] < 1 or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return tuple(out)


def macro_call_sites_from_spans(
    spans: list[dict[str, Any]],
) -> tuple[tuple[str, int], ...]:
    """Every macro-invocation call-site named by the diagnostic's spans.

    An error inside a macro renders with its primary span at the macro's own
    body; rustc records the INVOCATION site under `span.expansion.span`, one
    level per nested expansion. Walk each span's expansion chain and collect
    those call-site `(file, line)` pairs — the fn that wrote the macro call, not
    the macro definition. Deduped, order-preserving. Empty for a plain error."""
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for span in spans:
        node = span
        while isinstance(node, dict):
            exp = node.get("expansion")
            if not isinstance(exp, dict):
                break
            call = exp.get("span")
            if not isinstance(call, dict):
                break
            f = call.get("file_name") or ""
            ln = int(call.get("line_start") or 0)
            key = (f, ln)
            if f and ln and key not in seen:
                seen.add(key)
                out.append(key)
            node = call
    return tuple(out)


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


def categorize_compile_error(error_output: str) -> str:
    """Classify a compile error into a short category tag for compile_patterns."""
    o = error_output.lower()
    if "cannot find type" in o or "unresolved import" in o or "use of undeclared" in o:
        return "missing_type"
    if "module not found" in o or "no module named" in o or "mod " in o and "not found" in o:
        return "module_declaration"
    if "trait bound" in o or "does not implement" in o or "the trait" in o:
        return "trait_bound"
    if "mismatched types" in o or "expected" in o and "found" in o:
        return "type_mismatch"
    if "cannot borrow" in o or "borrow" in o and "conflict" in o:
        return "borrow_checker"
    if "lifetime" in o:
        return "lifetime"
    if "unused import" in o or "dead_code" in o:
        return "lint_error"
    if "proc-macro" in o or "proc_macro" in o:
        return "proc_macro"
    return "compile_error"

"""Test-result parsing + acceptance rules (pure domain).

Relocated verbatim from pipeline.py (slice 5b foundation). These are pure functions over
cargo/pytest *output text* and pass/fail counts — no I/O, no pipeline deps — so tasks
(task3/task4 and the FR-024 loops) import them instead of carrying them in a deps record.

Note: the regex here parses tool *output* (cargo/nextest/pytest), for which there is no
structured parser to use — distinct from source parsing, which goes through the syn
extractor. No new patterns are authored; the bodies are moved as-is.
"""

from __future__ import annotations

import re
from enum import StrEnum


class ClusterStatus(StrEnum):
    """The strict per-cluster TASK 3 verdict (FR-024). A `StrEnum` so existing
    ``status == "pass"`` comparisons keep working while the vocabulary is now
    explicit and exhaustive."""

    PASS = "pass"
    FAIL = "fail"              # tests ran and ≥1 failed — a repairable defect
    INCOMPLETE = "incomplete"  # fewer tests reported than statically expected


def cluster_failure_is_actionable(status: str, rc: int, fail_count: int) -> bool:
    """Can the code-fix loop actually repair this non-pass cluster?

    True only when the output carries a concrete defect to act on:
      - FAIL (``fail_count > 0``): failing-test diagnostics — fixable.
      - INCOMPLETE with ``rc != 0``: a test binary failed to compile/link or
        panicked before reporting — compiler diagnostics are present — fixable.

    A *clean-exit shortfall* (INCOMPLETE, ``rc == 0``, ``fail_count == 0``:
    cargo ran fine but fewer ``#[test]``s reported than statically expected) has
    NO repair signal. Routing it to the fix loop only churns (the loop sees no
    compile error and no failing test → its floor metric is 0). It is a
    count/oracle mismatch to SURFACE, not a code defect to repair."""
    if status == ClusterStatus.FAIL:
        return True
    if status == ClusterStatus.INCOMPLETE:
        return rc != 0
    return False


# Format marker between the genesis snapshot and the real test output in
# tests_output_N.txt. Owned here because extract_test_output_from_file parses it.
OUTPUT_SEPARATOR = "=== test output ==="


def parse_test_counts(test_output: str) -> tuple[int, int, int]:
    """
    Parse the number of passed, failed, and ignored tests from cargo
    test / pytest output. Returns (passed, failed, ignored) summed
    across every `test result:` line. Returns (0, 0, 0) when not
    parseable.

    Used by FR-024 improvement-driven loops (the metric for "is the
    pipeline making progress on tests?") and by older single-shot
    callers that just need passed+failed (ignored is appended and the
    older callers unpack only the first two — Python tolerates this
    via tuple-unpack with `_` or explicit indexing).
    """
    passed = failed = ignored = 0
    if not test_output:
        return (0, 0, 0)
    # Per-binary line: `test result: ok. 5 passed; 0 failed; 1 ignored; ...`
    for m in re.finditer(
        r"test result:\s*\w+\.\s*(\d+)\s+passed;\s*(\d+)\s+failed"
        r"(?:;\s*(\d+)\s+ignored)?",
        test_output,
    ):
        try:
            passed += int(m.group(1))
            failed += int(m.group(2))
            ignored += int(m.group(3) or "0")
        except ValueError:
            continue
    return (passed, failed, ignored)


# R6.5: this is a module-public parser (sibling to count_passing_tests /
# validate_task3_output_strict) — the public name is the canonical one; the old
# `_`-prefixed name is kept as a back-compat alias for the pipeline.py test seam
# (test_pipeline_smoke asserts `pipeline._parse_test_counts is tr._parse_test_counts`).
_parse_test_counts = parse_test_counts


def count_passing_tests(test_output: str) -> int:
    """
    Extract the number of passing tests from test output.
    Handles cargo-nextest, cargo test, pytest, and jest output formats.
    Returns 0 if not parseable.
    """
    patterns = [
        r'(\d+)\s+passed',          # cargo nextest: "42 passed"
        r'test result:.*?(\d+)\s+passed',  # cargo test
        r'(\d+)\s+tests?\s+passed',
        r'passed\s+(\d+)',
        r'(\d+)\s+ok',
    ]
    for pat in patterns:
        m = re.search(pat, test_output, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


def validate_task3_output_strict(
    pass_count: int, fail_count: int, ignored_count: int,
    expected_count: int, cluster_name: str,
) -> tuple[str, str]:
    """FR-024 strict acceptance for TASK 3 per cluster. Returns (status, reason)
    where status is 'pass' | 'fail' | 'incomplete'.

    Pass criteria (ALL must hold):
      - fail_count == 0
      - pass_count + ignored_count >= expected_count
        (every expected test reported a verdict, with `ignored` counting
        toward "reported")
      - expected_count > 0 OR pass_count > 0
        (refuse silent successes — at least one test must exist & have run)

    'incomplete' is returned when the corpus is non-empty but the run
    reported fewer tests than expected — usually a test binary failed
    to compile, or a binary panicked before reporting all tests. The
    caller treats 'incomplete' as 'fail' for control flow but logs the
    distinct reason for diagnostic value.
    """
    reported = pass_count + fail_count + ignored_count
    if fail_count > 0:
        return ClusterStatus.FAIL, (
            f"cluster '{cluster_name}': {fail_count} failed, "
            f"{pass_count} passed, {ignored_count} ignored "
            f"(expected {expected_count})"
        )
    if expected_count <= 0 and pass_count == 0:
        return ClusterStatus.INCOMPLETE, (
            f"cluster '{cluster_name}': no tests detected statically and "
            "none ran — corpus appears empty for this cluster"
        )
    if reported < expected_count:
        return ClusterStatus.INCOMPLETE, (
            f"cluster '{cluster_name}': {reported} tests reported "
            f"({pass_count} pass + {fail_count} fail + {ignored_count} ignored), "
            f"but {expected_count} expected — some binary failed to compile "
            "or crashed before reporting all tests"
        )
    return ClusterStatus.PASS, (
        f"cluster '{cluster_name}': {pass_count} passed, "
        f"{ignored_count} ignored (expected {expected_count})"
    )


def validate_task3_output(pass_count: int, fail_count: int,
                          module_has_tests: bool) -> tuple[str, str]:
    """
    Anti-corruption check on test results — LEGACY non-strict variant kept
    for the few call sites that have no cluster context. The current TASK 3
    path uses `validate_task3_output_strict` above; verdicts that already
    have task3's strict boolean must go through `resolve_task3_verdict`.

    Returns (status, reason) where status is one of:
      'pass'      — all tests passed, lock the module
      'fail'      — tests ran but failed, continue to TASK 4
      'no_tests'  — 0 tests ran despite test files existing (structural problem)
      'no_files'  — module has no test files (expected — lock on build success)
    """
    if pass_count > 0 and fail_count == 0:
        return "pass", f"{pass_count} tests passed"
    if not module_has_tests and pass_count == 0 and fail_count == 0:
        return "no_files", "module has no test files — lock on build success"
    if pass_count == 0 and fail_count == 0:
        return "no_tests", (
            "0 tests ran despite test files existing — "
            "test binary may be empty, harness may not compile, or test filter excludes everything"
        )
    return "fail", f"{pass_count} passed, {fail_count} failed"


def insert_no_run(test_command: str) -> str:
    """Insert ``--no-run`` after the first ``cargo test`` so a zero-tests-ran
    result can be reclassified (build error vs harness issue). Regex-free
    (replaces the legacy ``\\bcargo\\s+test\\b`` sub): token-scan for an adjacent
    ``cargo`` ``test`` pair and splice ``--no-run`` after it. Returns the command
    unchanged when no such pair exists."""
    toks = test_command.split()
    for i in range(len(toks) - 1):
        if toks[i] == "cargo" and toks[i + 1] == "test":
            toks.insert(i + 2, "--no-run")
            return " ".join(toks)
    return test_command


def resolve_task3_verdict(passed: bool, pass_count: int, fail_count: int,
                          module_has_tests: bool) -> tuple[str, str]:
    """task3's strict per-cluster boolean is the verdict AUTHORITY; the
    lenient count-based classifier only subtypes failures for TASK 4 routing
    (fail / no_tests / no_files) and may NEVER upgrade them to 'pass'.

    Re-deriving the verdict from parsed counts let an INCOMPLETE run
    (24 passed, 0 failed, 26 expected — a cluster's binary never compiled)
    reclassify as 'pass' and exit 0 (2026-06-12 microcrate false SUCCESS).
    """
    if passed:
        return "pass", f"{pass_count} tests passed"
    status, reason = validate_task3_output(pass_count, fail_count, module_has_tests)
    if status == "pass":
        return "fail", (
            f"strict per-cluster verdict failed despite {pass_count} passed, "
            f"{fail_count} failed — a cluster is incomplete or missing tests"
        )
    return status, reason


def extract_test_output_from_file(file_content: str) -> str:
    """
    tests_output_N.txt contains a genesis snapshot followed by OUTPUT_SEPARATOR
    and then the actual test output. Strip the genesis header so that only the
    test output is returned -- avoids truncation cutting into the genesis block.
    """
    marker = OUTPUT_SEPARATOR + "\n"
    idx = file_content.find(marker)
    if idx == -1:
        return file_content
    return file_content[idx + len(marker):]


def _resolve_test_file(module_path: list[str], known_files: set[str]) -> str:
    """Resolve a unit-test module path (e.g. ['context', 'gen_tests_1']) to a
    source file in `known_files` (forward-slash relative paths). Tries, deepest
    first: the slash-path module file, then the dotted #[path] sibling
    (`src/context.gen_tests_1.rs`), then shallower prefixes (inline `mod tests`
    lands on the parent file). No module path → src/lib.rs."""
    if not module_path:
        return "src/lib.rs" if "src/lib.rs" in known_files else ""
    for depth in range(len(module_path), 0, -1):
        prefix = module_path[:depth]
        slash = "src/" + "/".join(prefix) + ".rs"
        if slash in known_files:
            return slash
        if depth >= 2:
            dotted = "src/" + "/".join(prefix[:-1]) + "." + prefix[-1] + ".rs"
            if dotted in known_files:
                return dotted
    return "src/lib.rs" if "src/lib.rs" in known_files else ""


def per_file_test_stats(
    test_output: str,
    known_files: set[str],
    test_name_index: dict[str, str] | None = None,
) -> dict[str, dict[str, int]]:
    """Attribute cargo-test result lines to source/test files. Returns
    {relative_file: {"passed": n, "failed": n, "ignored": n}}.

    Attribution is per test NAME (interleaving-proof: cargo binary outputs can
    interleave on stdout, so section↔binary-header pairing by order is NOT
    reliable — observed live). Order tried:
      1. `test_name_index` — bare name → tests/*.rs file (caller builds it from
         file contents); "" in the index marks an ambiguous name (same fn name
         in several test files) → "(unmapped)".
      2. doc-tests — name starts with a known file path ("src/lib.rs - … (line N)").
      3. module path — `context::gen_tests_1::test_x` via _resolve_test_file.
      4. bare name with no index hit → "(unmapped)" (never guess lib.rs).
    """
    index = test_name_index or {}
    stats: dict[str, dict[str, int]] = {}
    for raw in test_output.splitlines():
        line = raw.strip()
        if not (line.startswith("test ") and " ... " in line):
            continue
        if line.startswith("test result:"):
            continue
        name, _, status = line[len("test "):].partition(" ... ")
        name = name.strip()
        status = status.split(",")[0].split(" ")[0]  # "ignored, reason" → "ignored"
        if status not in ("ok", "FAILED", "ignored"):
            continue
        if name in index:
            file = index[name]
        elif " - " in name and name.split(" - ", 1)[0] in known_files:
            file = name.split(" - ", 1)[0]          # doc-test
        elif "::" in name:
            file = _resolve_test_file(name.split("::")[:-1], known_files)
        else:
            file = ""
        rec = stats.setdefault(file or "(unmapped)",
                               {"passed": 0, "failed": 0, "ignored": 0})
        rec[{"ok": "passed", "FAILED": "failed", "ignored": "ignored"}[status]] += 1
    return stats


def failing_test_names(test_output: str) -> list[str]:
    """Names of FAILED tests from cargo-test result lines, in output order,
    deduplicated. Names keep their full form (module path or doc-test label)."""
    names: list[str] = []
    seen: set[str] = set()
    for raw in test_output.splitlines():
        line = raw.strip()
        if not (line.startswith("test ") and " ... " in line):
            continue
        if line.startswith("test result:"):
            continue
        name, _, status = line[len("test "):].partition(" ... ")
        name = name.strip()
        if status.split(",")[0].split(" ")[0] == "FAILED" and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def parse_failed_tests(output: str) -> set[str]:
    """Names of FAILED tests, capturing BOTH cargo-test forms (regex-free):

        test foo::bar ... FAILED      → 'foo::bar'
        ---- foo::bar stdout ----     → 'foo::bar'

    The set superset of `failing_test_names`: the `---- … stdout ----` section
    headers catch a failure even when its summary `test … FAILED` line is
    absent. Parity with the old `^test\\s+(\\S+)\\s+\\.\\.\\.\\s+FAILED` /
    `^----\\s+(\\S+)\\s+stdout\\s+----` regexes: the line must START with the
    keyword token (no leading whitespace), and the name is a single
    whitespace-free token (doc-test labels with spaces don't match — same as
    the regexes).
    """
    failed: set[str] = set()
    for ln in output.splitlines():
        toks = ln.split()
        if (ln[:4] == "test" and len(toks) >= 4
                and toks[0] == "test" and toks[2] == "..." and toks[3] == "FAILED"):
            failed.add(toks[1])
        if (ln[:4] == "----" and len(toks) >= 4
                and toks[0] == "----" and toks[2] == "stdout" and toks[3] == "----"):
            failed.add(toks[1])
    return failed


def parse_rustc_compile_errors(build_output: str) -> list[tuple[str, str, int]]:
    """`(message, file, line)` for each rustc `error[...]: <msg>` line immediately
    followed by a `--> <file>:<line>[:col]` location line (regex-free, R7; mirrors
    `^error(?:\\[E\\d+\\])?:\\s+([^\\n]+)\\n\\s*-->\\s*([^:]+):(\\d+)(?::\\d+)?`,
    MULTILINE). The `error` keyword must start the line — same ANSI sensitivity as
    the old regex; the structured cargo-JSON frontier is the primary path and this
    is only the text fallback."""
    out: list[tuple[str, str, int]] = []
    lines = build_output.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("error"):
            continue
        rest = line[len("error"):]
        if rest.startswith("[E"):                 # optional [E<digits>]
            j = 2
            while j < len(rest) and rest[j].isdigit():
                j += 1
            if j == 2 or j >= len(rest) or rest[j] != "]":
                continue
            rest = rest[j + 1:]
        if not rest.startswith(":") or len(rest) < 2 or not rest[1].isspace():
            continue                               # `:` then `\s+`
        msg = rest[1:].lstrip()                    # `\s+` consumed, `[^\n]+` = rest of line
        if not msg or i + 1 >= len(lines):
            continue
        arrow = lines[i + 1].lstrip()              # `\n\s*-->`
        if not arrow.startswith("-->"):
            continue
        loc = arrow[3:].lstrip()                   # `-->\s*`
        colon = loc.find(":")                      # `([^:]+):`
        if colon <= 0:
            continue
        digits = 0
        while digits < len(loc) - colon - 1 and loc[colon + 1 + digits].isdigit():
            digits += 1
        if digits == 0:                            # `(\d+)`
            continue
        out.append((msg, loc[:colon], int(loc[colon + 1:colon + 1 + digits])))
    return out


def rustc_error_signatures(cargo_output: str) -> frozenset[str]:
    """Stable rustc error signatures (`error[E<code>]: <message>`, no file/line),
    regex-free (R7). Equivalent to `re.findall(r'error\\[E\\d+\\]: .+', output)`:
    per line, the first `error[E<digits>]: ` with ≥1 trailing char, captured to
    end of line (the greedy `.+` consumes the remainder → one per line)."""
    marker = "error[E"
    sigs: set[str] = set()
    for line in cargo_output.splitlines():
        pos = 0
        while (k := line.find(marker, pos)) >= 0:
            rest = line[k + len(marker):]
            d = 0
            while d < len(rest) and rest[d].isdigit():
                d += 1
            if d and rest[d:d + 3] == "]: " and len(rest) > d + 3:
                sigs.add(line[k:])      # error[E<code>]: <rest of line>
                break                   # `.+` consumed the rest of this line
            pos = k + len(marker)       # malformed code → try the next occurrence
    return frozenset(sigs)


_IDENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def identifiers_in(text: str) -> set[str]:
    """Every identifier-shaped token in `text` (manual scan, no regex).
    Used to attribute a failing test to the source fns it exercises by
    intersecting with the known fn-name set."""
    out: set[str] = set()
    start = -1
    for i, ch in enumerate(text):
        if ch in _IDENT_CHARS:
            if start < 0:
                start = i
        elif start >= 0:
            tok = text[start:i]
            if not tok[0].isdigit():
                out.add(tok)
            start = -1
    if start >= 0:
        tok = text[start:]
        if not tok[0].isdigit():
            out.add(tok)
    return out


def rustc_error_codes(text: str, letter: str) -> list[str]:
    """Every ``<letter><3-4 digits>`` rustc error code as a whole word, in order —
    byte-equivalent to ``re.findall(rf'\\b{letter}\\d{{3,4}}\\b', text)`` (`letter` is
    ``E`` or ``e``). A code is a complete word-token: letter + 3 or 4 ASCII digits."""
    out: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] in _IDENT_CHARS:
            j = i + 1
            while j < n and text[j] in _IDENT_CHARS:
                j += 1
            tok = text[i:j]
            if tok[0] == letter and 4 <= len(tok) <= 5 and all(c in "0123456789" for c in tok[1:]):
                out.append(tok)
            i = j
        else:
            i += 1
    return out


def is_compiler_output_line(stripped: str) -> bool:
    """True iff `stripped` begins like a rustc diagnostic — ``error[E123]:`` /
    ``error:`` / ``warning[W1]:`` / ``warning:`` — byte-equivalent to
    ``re.match(r'^error(\\[E\\d+\\])?:', s) or re.match(r'^warning(\\[W\\d+\\])?:', s)``."""
    n = len(stripped)
    for kw, code in (("error", "E"), ("warning", "W")):
        if not stripped.startswith(kw):
            continue
        p = len(kw)
        if p < n and stripped[p] == "[" and p + 1 < n and stripped[p + 1] == code:
            d = p + 2  # \d+
            while d < n and stripped[d].isdigit():
                d += 1
            if d > p + 2 and d < n and stripped[d] == "]":  # ≥1 digit, then ]
                p = d + 1
        if p < n and stripped[p] == ":":
            return True
    return False


def _phase_c_rest(ln: str, p: int, n: int) -> tuple[str, str | None, str] | None:
    """Match ``\\s*:\\s*[A-Z_]+\\s*(\\[fn=…\\])?\\s*(—|-)\\s*.+?\\s*$`` from `p`; returns
    ``(cat, fn_raw, reason_stripped)`` or None. Tries the optional ``[fn=…]`` group
    present-then-absent (greedy ``?``)."""
    while p < n and ln[p].isspace():  # \s*
        p += 1
    if p >= n or ln[p] != ":":
        return None
    p += 1
    while p < n and ln[p].isspace():  # \s*
        p += 1
    cs = p
    while p < n and (("A" <= ln[p] <= "Z") or ln[p] == "_"):  # [A-Z_]+
        p += 1
    if p == cs:
        return None
    cat = ln[cs:p]
    for use_fn in (True, False):
        q = p
        fn: str | None = None
        while q < n and ln[q].isspace():  # \s*
            q += 1
        if use_fn:
            if not ln.startswith("[fn=", q):
                continue
            c = ln.find("]", q + 4)
            if c == -1 or c <= q + 4:  # [^\]]+ needs ≥1
                continue
            fn = ln[q + 4:c]
            q = c + 1
        while q < n and ln[q].isspace():  # \s*
            q += 1
        if q >= n or ln[q] not in "—-":  # (?:—|-)
            continue
        q += 1
        rest = ln[q:]  # \s*(.+?)\s*$ → matches iff non-empty; consumer reason = .strip()
        if not rest:
            continue
        return (cat, fn, rest.strip())
    return None


def parse_phase_c_line(line: str) -> tuple[str, str, str | None, str] | None:
    r"""Parse a phase-C classifier line ``TEST <name>: <CAT> [fn=<f>] — <reason>`` →
    ``(test, cat, fn_or_none, reason_stripped)``, else None. Equivalent to
    ``re.match(r'^\s*TEST\s+(?P<test>\S+?)\s*:\s*(?P<cat>[A-Z_]+)\s*(?:\[fn=(?P<fn>[^\]]+)\])?'``
    ``r'\s*(?:—|-)\s*(?P<reason>.+?)\s*$', line)`` (`reason` pre-stripped as the consumer
    does; the non-greedy ``\S+?`` test grows until `cat` can match)."""
    n = len(line)
    p = 0
    while p < n and line[p].isspace():  # ^\s*
        p += 1
    if not line.startswith("TEST", p):
        return None
    p += 4
    if p >= n or not line[p].isspace():  # \s+
        return None
    while p < n and line[p].isspace():
        p += 1
    start = p
    run = start
    while run < n and not line[run].isspace():  # the \S+ run
        run += 1
    for test_end in range(start + 1, run + 1):  # \S+? non-greedy: shortest that lets the rest match
        rest = _phase_c_rest(line, test_end, n)
        if rest is not None:
            return (line[start:test_end], rest[0], rest[1], rest[2])
    return None


def camelcase_type_tokens(text: str) -> list[str]:
    r"""CamelCase type tokens (optionally with a single ``<…>`` generic) mined from
    cargo error text, in order — byte-equivalent to the group(1) of
    ``re.finditer(r'\b([A-Z][A-Za-z0-9_]*(?:<[^<>\n]{1,60}>)?)\b', text)``. The
    generic is included ONLY when the closing ``>`` is followed by a word char (the
    trailing ``\b``); otherwise the bare name is captured."""
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        c = text[i]
        if "A" <= c <= "Z" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            e = i + 1  # [A-Z][A-Za-z0-9_]*
            while e < n and (text[e].isalnum() or text[e] == "_"):
                e += 1
            end = e
            if e < n and text[e] == "<":  # optional <[^<>\n]{1,60}> then \b
                g = e + 1
                cnt = 0
                while g < n and text[g] not in "<>\n" and cnt < 60:
                    g += 1
                    cnt += 1
                if g < n and text[g] == ">" and cnt >= 1 and g + 1 < n and (
                        text[g + 1].isalnum() or text[g + 1] == "_"):
                    end = g + 1
            out.append(text[i:end])
            i = end
        else:
            i += 1
    return out


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR colour escapes — byte-equivalent to
    ``re.sub(r'\\x1b\\[[0-9;]*m', '', text)``."""
    if "\x1b" not in text:  # fast path: nothing to strip → no rebuild
        return text
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        if text[i] == "\x1b" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and (text[j].isdigit() or text[j] == ";"):  # [0-9;]*
                j += 1
            if j < n and text[j] == "m":
                i = j + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def rustc_diagnostic_locations(output: str) -> list[tuple[str, int]]:
    """``(file, line)`` of every ``--> path.rs:LINE:COL`` cargo diagnostic location, in
    order — byte-equivalent to the (group(1), int(group(2))) of
    ``re.finditer(r'-->\\s+([^:\\s]+\\.rs):(\\d+):\\d+', output)``."""
    out: list[tuple[str, int]] = []
    n = len(output)
    i = 0
    while (k := output.find("-->", i)) >= 0:
        i = k + 3
        p = k + 3
        if p >= n or not output[p].isspace():  # \s+
            continue
        while p < n and output[p].isspace():
            p += 1
        s = p  # ([^:\s]+\.rs)
        while p < n and output[p] != ":" and not output[p].isspace():
            p += 1
        file = output[s:p]
        if not file.endswith(".rs") or p == s or p >= n or output[p] != ":":
            continue
        p += 1
        d1 = p  # (\d+)
        while p < n and output[p].isdigit():
            p += 1
        if p == d1 or p >= n or output[p] != ":":
            continue
        line = int(output[d1:p])
        p += 1
        d2 = p  # \d+ (column)
        while p < n and output[p].isdigit():
            p += 1
        if p == d2:
            continue
        out.append((file, line))
        i = p
    return out


def build_test_name_index(file_texts: dict[str, str]) -> dict[str, str]:
    """{relative_path: file_text} for tests/*.rs → {fn_name: path} for bare
    cargo test names. Collects every `fn name(` (over-collection of helpers is
    harmless — only actual test names are looked up). A name defined in more
    than one file maps to "" (ambiguous → caller buckets it "(unmapped)")."""
    index: dict[str, str] = {}
    for path, text in file_texts.items():
        for raw in text.splitlines():
            line = raw.strip()
            for prefix in ("pub ", "async ", "unsafe ", "const ", "extern \"C\" "):
                if line.startswith(prefix):
                    line = line[len(prefix):]
            if not line.startswith("fn "):
                continue
            name = line[3:].split("(")[0].strip()
            if name.isidentifier():
                index[name] = "" if index.get(name, path) != path else path
    return index


def parse_error_code_note(line: str) -> tuple[str, str] | None:
    r"""Parse a ``[E0277,E0599] note text`` model-note line → ``(codes, body)``, else
    None — equivalent to ``re.match(r'^\[((?:E\d{3,4})(?:,E\d{3,4})*)\]\s*(.+)$', line)``
    projected onto ``(group(1), group(2).strip())`` (the consumer always strips the body;
    codes is the raw comma list)."""
    n = len(line)
    if not line.startswith("["):
        return None

    def _code_end(p: int) -> int | None:  # E\d{3,4} at p → index past it, else None
        if p < n and line[p] == "E":
            d = p + 1
            while d < n and line[d].isdigit():
                d += 1
            if 3 <= d - (p + 1) <= 4:
                return d
        return None

    e = _code_end(1)
    if e is None:
        return None
    while e < n and line[e] == ",":  # (,E\d{3,4})*
        nxt = _code_end(e + 1)
        if nxt is None:
            break
        e = nxt
    if e >= n or line[e] != "]":
        return None
    codes = line[1:e]
    rest = line[e + 1:]  # \s*(.+)$ matches iff rest non-empty; .+ reclaims leading ws
    return (codes, rest.strip()) if rest else None

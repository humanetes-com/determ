"""Test-result parsing + acceptance rules (pure domain).

Relocated verbatim from pipeline.py. These are pure functions over
cargo/pytest *output text* and pass/fail counts — no I/O, no pipeline deps — so tasks
(task3/task4 and the loops) import them instead of carrying them in a deps record.

Note: the regex here parses tool *output* (cargo/nextest/pytest), for which there is no
structured parser to use — distinct from source parsing, which goes through the syn
extractor. No new patterns are authored; the bodies are moved as-is.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from pipeline_core.domain.last_error import (
    CRASH_HEAD_MARK,
    EXECUTED_DROP_MARK,
    HANG_HEAD_MARK,
    PASS_DROP_MARK,
    RED_HEAD_MARK,
    LastErrorKind,
    instruction_for,
)
from pipeline_core.domain.oracle_ledger import bare_test_name
from pipeline_core.domain.reference_floor import doc_test_line

if TYPE_CHECKING:
    from pipeline_core.domain.assertion_position import KeyedVerdicts, TestKey


class ClusterStatus(StrEnum):
    """The strict per-cluster TASK 3 verdict. A `StrEnum` so existing
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


def rank_repair_candidates(
    candidates: list[tuple[str, frozenset[str], bool]],
    unrepairable: frozenset[str] = frozenset(),
) -> list[str]:
    """Order actionable failing clusters by how good a repair signal each is.

    `candidates` is (cluster name, its failing test names, compile-broken?).
    The retry adopts ONE of these as its primary: its failures aim the frontier
    and its output is what the repair reads. That choice used to fall out of
    iteration order, which is alphabetical — so on 2026-08-05 `nightly` was
    picked over `stable` because `"nightly" < "stable"`, and it is the cluster
    that carries `ui`, a trybuild compile-fail test no body rewrite can
    satisfy. `stable` failed only `test_boxed_str`.

    A COMPILE-BROKEN cluster (INCOMPLETE with rc != 0: its lib or a test binary
    did not build, so it reported no verdicts) ranks first. Its failing-test
    name set is necessarily empty, which under the name-based key sorted it
    into the same bucket as "nothing repairable" — on 2026-08-10 `nightly`
    (2 compile errors, a minutes-scale BUILD-mode fix) ranked behind `stable`
    (17 failing tests) and waited 4h15m behind a TEST-mode grind moving ~1
    test per 2h pass, while its ~138 expected verdicts were worth zero to the
    shared accept gate. Restoring a broken cluster's compilation restores its
    verdicts — un-blinding that gate for every later repair — and its cost is
    bounded by diagnostic count, not by a test grind; the reverify recursion
    then re-ranks with both clusters producing verdicts.

    Ordering, in words: compile-broken first, then a cluster with at least one
    REPAIRABLE failure beats one with none, then fewer failures beats more (a
    smaller frontier, and the cluster closest to green), then the name so the
    choice is reproducible.
    """
    def key(item: tuple[str, frozenset[str], bool]) -> tuple[int, int, int, str]:
        name, failures, compile_broken = item
        repairable = {f for f in failures if f.split("::")[-1] not in unrepairable}
        return (0 if compile_broken else 1,
                0 if repairable else 1, len(failures), name)

    return [name for name, _, _ in sorted(candidates, key=key)]


def split_reachable_failures(
    failed: Iterable[str], unrepairable: Iterable[str] = (),
    excused: Iterable[str] = (),
) -> tuple[frozenset[str], frozenset[str]]:
    """`(reachable, out_of_reach)` over a set of failing test names.

    A failure is out of reach when a body rewrite cannot address it — either
    because the test's assertion is not about a body at all (`unrepairable`,
    e.g. a trybuild harness asserting on rustc's own stderr) or because the
    pristine crate fails it here too (`excused`).

    The distinction decides whether another iteration can accomplish anything.
    On `anyhow` 2026-08-06 the answer was no from the first iteration on:
    `ui` was the only failure, `per_fn_retry` had already excluded it from the
    frontier with "not body-repairable", and the loop still ran the fix loop
    three times and TASK 4 once per module iteration — seven model calls and
    13 min 06 s per iteration, every one recording
    `metric_prev 10001, metric_new 10001, improved False` — with
    `MODULE_PLATEAU_ABORT=10` between it and a guaranteed exit 1.

    Names are compared on their last `::` segment, matching
    :func:`rank_repair_candidates`: cargo reports a test as `module::path::name`
    in one binary and bare in another, and the taxonomy is keyed on the bare
    name.
    """
    stop = {str(x).split("::")[-1] for x in unrepairable}
    stop |= {str(x).split("::")[-1] for x in excused}
    reachable = {f for f in failed if str(f).split("::")[-1] not in stop}
    return frozenset(reachable), frozenset(set(failed) - reachable)


@dataclass(frozen=True)
class TestOutcome:
    """The result of one test-command run — a named value object replacing the
    ad-hoc ``done["data"] = {...}`` dicts that drifted into three inconsistent
    shapes at the call sites. The constructor names the *intent* (timed out /
    command missing / ran with counts); ``as_event_data`` renders the exact
    telemetry dict each intent historically produced, so emitted events stay
    byte-identical while the concept is now explicit and drift-proof.

    ``reason`` is set only for the non-running outcomes (``"timeout"`` /
    ``"command_not_found"``); a run that produced pass/fail counts leaves it
    ``None``. The two are mutually exclusive by construction."""

    __test__ = False  # domain VO, not a pytest class (silences collection warning)

    passed: bool
    pass_count: int = 0
    fail_count: int = 0
    reason: str | None = None

    @classmethod
    def timeout(cls) -> TestOutcome:
        """Tests exceeded the timeout — no counts available."""
        return cls(passed=False, reason="timeout")

    @classmethod
    def command_not_found(cls) -> TestOutcome:
        """The test command's runtime was missing from PATH — no counts."""
        return cls(passed=False, reason="command_not_found")

    @classmethod
    def from_counts(cls, passed: bool, pass_count: int, fail_count: int) -> TestOutcome:
        """Tests ran to completion; carry the parsed pass/fail counts."""
        return cls(passed=passed, pass_count=pass_count, fail_count=fail_count)

    def as_event_data(self) -> dict[str, object]:
        """Render the telemetry `data` payload. Reproduces the historical
        per-intent key set (and order) exactly: a reason-bearing outcome omits
        the counts; a completed run omits the reason."""
        if self.reason is not None:
            return {"passed": self.passed, "reason": self.reason}
        return {"passed": self.passed,
                "pass_count": self.pass_count, "fail_count": self.fail_count}


class TestRunOutcome(NamedTuple):
    """Result of a scoped test run : whether it passed, the failing
    count, the failing-output tail for the next prompt, and the failing-test
    names. On timeout ``fail_count`` is ``-1`` (INCONCLUSIVE — caller falls back
    to compile-based acceptance). A ``NamedTuple`` so it stays a drop-in for the
    ``(passed, fail_count, detail, failed)`` tuple callers already unpack."""

    passed: bool
    fail_count: int
    detail: str
    failed: set[str]


# Format marker between the genesis snapshot and the real test output in
# tests_output_N.txt. Owned here because extract_test_output_from_file parses it.
OUTPUT_SEPARATOR = "=== test output ==="


def parse_test_counts(test_output: str) -> tuple[int, int, int]:
    """
    Parse the number of passed, failed, and ignored tests from cargo
    test / pytest output. Returns (passed, failed, ignored) summed
    across every `test result:` line. Returns (0, 0, 0) when not
    parseable.

    Used by improvement-driven loops (the metric for "is the
    pipeline making progress on tests?") and by older single-shot
    callers that just need passed+failed (ignored is appended and the
    older callers unpack only the first two — Python tolerates this
    via tuple-unpack with `_` or explicit indexing).
    """
    passed = failed = ignored = 0
    if not test_output:
        return (0, 0, 0)
    # Per-binary line: `test result: ok. 5 passed; 0 failed; 1 ignored; ...`
    for line in test_output.splitlines():
        if "test result:" not in line:
            continue
        t = _tallies_on_result_line(line)
        passed += t.get("passed", 0)
        failed += t.get("failed", 0)
        ignored += t.get("ignored", 0)
    return (passed, failed, ignored)


@dataclass(frozen=True, slots=True)
class TestReportGap:
    """Tests a `cargo test` run ANNOUNCED but never reported a verdict for.

    `parse_test_counts` sums the `test result:` lines, and a binary that
    segfaults prints none — so its tests leave the numerator and the denominator
    together and the total looks internally consistent. Measured on `anyhow`
    2026-08-05: the post-merge loop logged `pass=139 fail=1` while
    `tests/test_downcast.rs` cored after its first test, and 6 of its 7
    produced no verdict at all.

    Nothing else in the output has to be trusted to see this: cargo prints
    `running N tests` for the crashed binary before it dies, so the announcement
    is right there next to the silence. `expected` counts from a static
    inventory are not needed and cannot be wrong here.
    """

    announced: int
    reported: int
    silent_binaries: tuple[str, ...]

    @property
    def missing(self) -> int:
        return max(0, self.announced - self.reported)

    def __bool__(self) -> bool:
        """True when a binary went silent — the shape callers branch on."""
        return bool(self.silent_binaries)


def find_unreported_tests(test_output: str) -> TestReportGap:
    """Compare what `cargo test` announced against what it reported.

    Each test binary prints ``Running <label> (<path>)``, then ``running N
    tests``, then — only if it survives — ``test result: …``. A binary with an
    announcement and no result line took its whole `N` out of the counts.
    """
    announced = reported = 0
    current = ""
    pending: dict[str, int] = {}
    silent: list[str] = []
    for line in (test_output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("Running ", "Doc-tests ")):
            # A previous binary that never reported goes down as silent.
            if current and current in pending:
                silent.append(current)
            current = stripped.split(" (", 1)[0]
            continue
        # `running 1 test` is SINGULAR — a binary with exactly one test says
        # "test", not "tests", and matching only the plural silently undercounts
        # the announcement. On the merged `anyhow` tree that was 2 of 149.
        if stripped.startswith("running ") and stripped.endswith(("test", "tests")):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1].isdigit():
                n = int(parts[1])
                announced += n
                if n:
                    pending[current] = n
            continue
        if "test result:" in stripped:
            t = _tallies_on_result_line(stripped)
            reported += (t.get("passed", 0) + t.get("failed", 0)
                         + t.get("ignored", 0))
            pending.pop(current, None)
    if current and current in pending:
        silent.append(current)
    return TestReportGap(announced=announced, reported=reported,
                         silent_binaries=tuple(silent))


# This is a module-public parser (sibling to count_passing_tests /
# validate_task3_output_strict) — the public name is the canonical one; the old
# `_`-prefixed name is kept as a back-compat alias for the pipeline.py test seam
# (test_pipeline_smoke asserts `pipeline._parse_test_counts is tr._parse_test_counts`).
_parse_test_counts = parse_test_counts


def _tallies_on_result_line(line: str) -> dict[str, int]:
    """Every `<N> <label>` tally on ONE `test result:` line, via pure string ops
    (no regex — Hard Rule). `test result: FAILED. 6 passed; 2 failed; 1 ignored;
    0 measured; 0 filtered out` → {passed: 6, failed: 2, ignored: 1, ...}.
    Reading the labels present, rather than matching a fixed sequence, means a
    trailer that omits or reorders a tally still yields the ones it does carry."""
    out: dict[str, int] = {}
    tail = line.split("test result:", 1)[1] if "test result:" in line else ""
    tail = tail.split(".", 1)[1] if "." in tail else tail
    for tok in tail.split(";"):
        parts = tok.split()
        if len(parts) >= 2 and parts[0].lstrip("-").isdigit():
            out[parts[1]] = int(parts[0])
    return out


def _counts_on_result_line(line: str) -> tuple[int, int]:
    """(passed, failed) parsed from ONE `test result:` line."""
    t = _tallies_on_result_line(line)
    return t.get("passed", 0), t.get("failed", 0)


def summarize_test_binaries(test_output: str) -> list[tuple[str, int, int]]:
    """Per-binary breakdown `(label, passed, failed)` in output order — one entry
    per cargo test target (unit-tests, each `tests/*.rs` integration binary, and
    Doc-tests). Lets triage see WHICH test set changed between TDD iters — a
    drop in the number of binaries/tests that RUN, not just pass→fail, was
    previously invisible because only the summed pass/fail was logged.

    Pure string parsing, resilient to the combined stdout+stderr layout: the
    `Running <desc> (<path>)` / `Doc-tests <crate>` labels are STDERR lines,
    so in captured output they may all sit AFTER every stdout `test result:`
    line (which made every label render `?` in live logs). Cargo executes
    targets sequentially, so the i-th label names the i-th stdout section
    opened by a libtest `running N tests` header; sections are paired with
    labels by index. A section with no `test result:` line (its binary
    crashed mid-run) still consumes its label."""
    labels: list[str] = []
    for raw in test_output.splitlines():
        line = raw.strip()
        if line.startswith("Running "):
            # `Running unittests src/lib.rs (target/…)` → drop the (path) tail.
            desc = line[len("Running "):].split(" (", 1)[0].strip()
            labels.append(desc or "?")
        elif line.startswith("Doc-tests "):
            labels.append("Doc-tests " + line[len("Doc-tests "):].strip())
    out: list[tuple[str, int, int]] = []
    section = -1
    for raw in test_output.splitlines():
        line = raw.strip()
        if line.startswith("running ") and line.endswith(("tests", "test")):
            section += 1
        elif line.startswith("test result:"):
            p, f = _counts_on_result_line(line)
            label = labels[section] if 0 <= section < len(labels) else "?"
            out.append((label, p, f))
    return out


def _count_token(tok: str) -> int | None:
    """`tok` as a count, or None when it is not one — the token equivalent of
    `\\d+` bounded by whitespace on both sides."""
    return int(tok) if tok.isdigit() else None


def _word_is(tok: str, word: str) -> bool:
    """Whether `tok` opens with `word`, case-insensitively — the trailing
    `;`/`.` cargo and pytest hang off a label is not part of it."""
    return tok.lower().startswith(word)


# The count shapes this fallback recognises, in PRIORITY order: the first shape
# found anywhere in the output decides, and a later shape is consulted only when
# no earlier one appears at all. Each is a window over whitespace-separated
# tokens; `None` is the position holding the number.
#   "42 passed" · "42 tests passed" · "passed 42" · "42 ok"
_COUNT_SHAPES: tuple[tuple[str | None, ...], ...] = (
    (None, "passed"),
    (None, "test", "passed"),
    ("passed", None),
    (None, "ok"),
)


def count_passing_tests(test_output: str) -> int:
    """
    Extract the number of passing tests from test output.
    Handles cargo-nextest, cargo test, pytest, and jest output formats.
    Returns 0 if not parseable.

    A LAST-RESORT parser: `parse_test_counts` reads libtest's own
    `test result:` trailer and is what every count that matters comes from.
    This exists for the frameworks that print no such trailer, so it stays
    deliberately loose — it answers from the first count-shaped phrase in the
    output rather than from a structure it can verify.
    """
    tokens = test_output.split()
    for shape in _COUNT_SHAPES:
        for i in range(len(tokens) - len(shape) + 1):
            found: int | None = None
            for offset, part in enumerate(shape):
                tok = tokens[i + offset]
                if part is None:
                    found = _count_token(tok)
                    if found is None:
                        break
                # `test`/`tests` — the plural is optional in the shape it came
                # from, and a prefix test accepts both without a second entry.
                elif not _word_is(tok, part):
                    break
            else:
                if found is not None:
                    return found
    return 0


def validate_task3_output_strict(
    pass_count: int, fail_count: int, ignored_count: int,
    expected_count: int, cluster_name: str,
    test_output: str = "",
) -> tuple[str, str]:
    """strict acceptance for TASK 3 per cluster. Returns (status, reason)
    where status is 'pass' | 'fail' | 'incomplete'.

    Pass criteria (ALL must hold):
      - fail_count == 0
      - no binary announced tests and then reported none (see below)
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

    `test_output` arms the check that does not depend on the static
    inventory. `expected_count` is counted against the cluster's own cfg
    sets, and on a MERGED tree it undershoots badly — the 2026-08-05 run
    measured `expected=104` for nightly while 129 tests actually ran, and
    `expected=124` for stable against 149. With that much slack a crashed
    binary could take 40 tests down and `reported < expected` would still
    be false, so the guard that exists to catch exactly that could never
    fire. `find_unreported_tests` compares what cargo ANNOUNCED (`running
    N tests`, printed before the binary dies) against what it reported, in
    the same output, four lines apart — no inventory, no oracle, and it
    names the silent binary.
    """
    reported = pass_count + fail_count + ignored_count
    gap = find_unreported_tests(test_output) if test_output else None
    # Failing tests keep FAIL: they are the most directly actionable signal and
    # the retry's TEST mode is aimed by their names. A binary that went silent
    # in the same run is appended to the reason rather than replacing the
    # status, so the fact is never lost and the control flow is unchanged.
    silence = ""
    if gap is not None and gap.missing > 0:
        silence = (f"; {gap.missing} test(s) announced and never reported "
                   f"(silent: {', '.join(gap.silent_binaries)})")
    if fail_count > 0:
        return ClusterStatus.FAIL, (
            f"cluster '{cluster_name}': {fail_count} failed, "
            f"{pass_count} passed, {ignored_count} ignored "
            f"(expected {expected_count}){silence}"
        )
    if gap is not None and gap.missing > 0:
        return ClusterStatus.INCOMPLETE, (
            f"cluster '{cluster_name}': {gap.announced} tests announced but "
            f"only {gap.reported} reported — {gap.missing} produced no verdict; "
            f"silent binaries: {', '.join(gap.silent_binaries)}"
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


# libtest qualifies a `#[should_panic]` test's result line with ` - should
# panic` BEFORE the ` ... ` separator (`test a::b::test_x - should panic ...
# ok`), while the oracle, attribution and every generated-test index know the
# test as `test_x`. Kept in the name, the qualifier makes the verdict
# unmatchable: `bare_test_name` yields `test_x - should panic`, the expected
# name `test_x` reports no verdict, and the sweep records a vanished test in
# a binary that completed — read as a deterministic crash. Measured on
# `anyhow`/`stable`: `error::gen_tests_4::test_no_backtrace_fallback`
# (generated 2026-08-12) passed as `- should panic ... ok` under plain
# `cargo test --lib` on 2026-08-20 while every sweep since had logged `ORACLE
# SHRUNK — counting 1 as failing ... (binary crashed)`, run the gdb/ASAN
# localizer to a NOBODY verdict, deliberated a layout retrigger, refused the
# durable-tree replacement (`sweep 83 with 1 test(s) unmeasured does not beat
# the stored 83`) and escalated 25 producers for a test that was passing.
_SHOULD_PANIC_QUALIFIER = " - should panic"


def strip_libtest_qualifier(name: str) -> str:
    """The test name as the oracle knows it: libtest's ` - should panic`
    qualifier removed. Only that exact suffix — doc-test names legitimately
    contain ` - ` (`src/lib.rs - Error (line 12)`) and are left alone."""
    if name.endswith(_SHOULD_PANIC_QUALIFIER):
        return name[: -len(_SHOULD_PANIC_QUALIFIER)].rstrip()
    return name


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
        name = strip_libtest_qualifier(name.strip())
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
        name = strip_libtest_qualifier(name.strip())
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


def split_test_stdout_sections(output: str) -> list[tuple[str, str]]:
    """Split cargo test output into `(test_name, body)` pairs at each
    `---- name stdout ----` header (regex-free; same header recognition as
    :func:`parse_failed_tests`). `body` runs until the next such header, one
    of cargo's own trailing structural lines (the `failures:` name-list, a
    `test result:` summary, or the next binary's `running N tests` preamble —
    otherwise the LAST matched section absorbs every subsequent binary's
    output when `cargo test` runs the whole suite in one invocation), or end
    of output — stripped. Order preserved; a name repeats in the list if
    cargo printed its stdout section more than once."""
    sections: list[tuple[str, list[str]]] = []
    cur_name: str | None = None
    cur_body: list[str] = []
    for ln in output.splitlines():
        toks = ln.split()
        if (ln[:4] == "----" and len(toks) >= 4
                and toks[0] == "----" and toks[2] == "stdout" and toks[3] == "----"):
            if cur_name is not None:
                sections.append((cur_name, cur_body))
            cur_name = toks[1]
            cur_body = []
            continue
        if cur_name is not None:
            stripped = ln.strip()
            if (stripped == "failures:" or stripped.startswith("test result:")
                    or stripped.startswith("running ")):
                sections.append((cur_name, cur_body))
                cur_name = None
                cur_body = []
                continue
            cur_body.append(ln)
    if cur_name is not None:
        sections.append((cur_name, cur_body))
    return [(name, "\n".join(body).strip()) for name, body in sections]


def build_phase_output(output: str) -> str:
    """The part of a `cargo test` capture that precedes the first test binary
    announcing tests — i.e. the BUILD phase.

    Once any binary prints `running N tests`, the crate compiled: cargo builds a
    target before it runs it. Everything from there on is something a TEST said,
    and a test is free to say anything, including a compiler diagnostic.

    That distinction decides a whole run's routing and getting it wrong cost
    one. `tests/compiletest.rs` is a trybuild harness; when the `ui` test fails
    it prints the expected and the actual `.stderr` fixtures, and those fixtures
    ARE rustc diagnostics. Measured on the live `anyhow` tree 2026-08-06:
    `error[E0599` appears twice in that output, both at lines 3035 and 3071 of
    3122, with the only `running 1 test` line at 1513 — so every hit is past the
    build phase, and no build signal appears before it at all. `classify_failure`
    matched `error[e` anywhere and returned `build`, and TASK 4 was handed the
    prompt that opens *"The implementation failed to COMPILE this iteration —
    the tests could not even run. This is a STRUCTURAL failure: the generated
    code was missing required files, had wrong module declarations, or had
    incorrect workspace layout"* while cargo had just reported 128 of 129 tests
    passing. The model wrote genesis rules about workspace layout, and said so
    itself the next iteration: "My previous genesis.md changes had NO EFFECT
    because the structural rules (rules 10–15) already covered module layout."

    It generalises past trybuild to any test that asserts on a compiler
    diagnostic — a `compile_fail` doc test, a test comparing an error string
    containing `error[E0308]`.

    The cut is also where cargo's OWN verdict, `error: could not compile …`,
    is read from — nowhere later. Measured on this box (cargo 1.95.0 and
    1.97.0-nightly, 2026-09-07): `cargo test --no-fail-fast --lib --test ok
    --test bad` with an unparseable `tests/bad.rs` prints nine lines, zero
    `Running`, zero `running N tests`, then `error: could not compile …
    (test "bad")` and `warning: build failed, waiting for other jobs to
    finish...` — a compile failure in ANY target runs NO binary, so a genuine
    red build's capture is its build phase in full and this function returns
    it whole. What CAN follow the first `running` line is a test that runs a
    compiler itself: `tests/compiletest.rs::ui` (trybuild) builds this crate
    again as a path dependency of a scratch project, without the cluster's
    `RUSTFLAGS` `--cfg`, and relays that nested cargo's `error: could not
    compile `anyhow` (lib)` — target name and all — into the sweep stream
    between `running 1 test` and `test ui ... FAILED`. Run `2131920-86818224`
    (cluster `nightly`, 2026-09-07) had 32 of 32 binaries complete on that
    stream and a whole-capture substring read it as red: the frontier was
    narrowed from 33 fns to the one the NESTED build implicated, two passes
    dispatched 0 of 0, and the cluster closed EXHAUSTED at 42/152.
    :func:`build_broke` is the one place the driver wording is read, and it
    reads it here.
    """
    lines = output.splitlines()
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("running ") and stripped.endswith(("test", "tests")):
            return "\n".join(lines[:i])
        if stripped.startswith("test result:"):
            return "\n".join(lines[:i])
    return output


_COULD_NOT_COMPILE = "could not compile"


def build_broke(cargo_output: str) -> bool:
    """Did the crate under test fail to BUILD in this `cargo test` capture?

    True iff cargo's own verdict, `error: could not compile …`, appears in
    :func:`build_phase_output` — the stream before any binary announced its
    tests. The same words after that point were printed BY a test (a
    trybuild/ui harness, a `cargo`-invoking integration test, a proc-macro
    harness) and describe a build that is not this one; see
    :func:`build_phase_output` for the two measured cargo facts this rests on.

    The TDD sweep, its reconciliation event, the immediate crash re-sweep,
    the scoped u2 run and the TASK 4 failure classifier all read this one
    function, so they answer "did the build fail" the same way."""
    return _COULD_NOT_COMPILE in build_phase_output(cargo_output or "")


@dataclass(frozen=True, slots=True)
class SweepBuild:
    """The build half of one sweep, read once and shared by every consumer.

    ``red`` is :func:`build_broke` — the build-phase marker, never label
    arithmetic. On this toolchain a red build prints no `Running` line at
    all (``missing`` == ``expected``), and a synthetic sweep with no
    `Running` line (the walk harness's stub) is not red either, so ``missing``
    cannot DEFINE red. It is carried for the iteration event: on a genuine
    red it names the targets that never ran, on a complete sweep it is
    empty — the reading run `2131920-86818224`'s iter-2 event already held
    (32 of 32 complete) while the substring verdict said otherwise.
    ``failed_targets`` is cargo's own naming of what did not build (`lib`,
    `test "x"`), from the build phase only. ``build_output`` is the slice
    every diagnostic reader takes."""

    expected: frozenset[str]
    built: frozenset[str]
    build_output: str

    @property
    def missing(self) -> frozenset[str]:
        return self.expected - self.built

    @property
    def red(self) -> bool:
        return _COULD_NOT_COMPILE in self.build_output

    @property
    def failed_targets(self) -> tuple[str, ...]:
        return compile_failed_targets(self.build_output)


def sweep_build(cargo_output: str, expected_labels: Iterable[str]) -> SweepBuild:
    """:class:`SweepBuild` for one capture. ``expected_labels`` is the label
    vocabulary cargo prints on `Running <label> (<exe>)`, rendered by the
    same domain code that renders the command (`test_cmd.expected_target_labels`);
    ``built`` is every label :func:`parse_binary_results` paired with a
    section."""
    out = cargo_output or ""
    return SweepBuild(
        expected=frozenset(expected_labels),
        built=frozenset(b.label for b in parse_binary_results(out)),
        build_output=build_phase_output(out))


def failure_bodies_for(output: str, names: set[str]) -> dict[str, str]:
    """The stdout-section body for each of `names` present in `output` (first
    occurrence wins). A name with no captured output (empty body) is omitted
    — a bare header tells the caller nothing a blind tail-slice wouldn't."""
    bodies: dict[str, str] = {}
    for name, body in split_test_stdout_sections(output):
        if name in names and body and name not in bodies:
            bodies[name] = body
    return bodies


@dataclass(frozen=True, slots=True)
class RustcDiagnostic:
    """One located rustc error. `code` is the bare `E<digits>` (empty for an
    uncoded `error:`) — kept as its own field because every consumer that has to
    decide what CLASS of failure this is reads the code, and rendering it away
    into the message made the digest unusable as evidence."""
    code: str
    message: str
    file: str
    line: int

    def rendered(self) -> str:
        head = f"error[{self.code}]: " if self.code else "error: "
        return f"{self.file}:{self.line}: {head}{self.message}"


def parse_rustc_compile_errors(build_output: str) -> list[RustcDiagnostic]:
    """One `RustcDiagnostic` for each rustc `error[...]: <msg>` line immediately
    followed by a `--> <file>:<line>[:col]` location line (regex-free; mirrors
    `^error(?:\\[E\\d+\\])?:\\s+([^\\n]+)\\n\\s*-->\\s*([^:]+):(\\d+)(?::\\d+)?`,
    MULTILINE). The `error` keyword must start the line — same ANSI sensitivity as
    the old regex; the structured cargo-JSON frontier is the primary path and this
    is only the text fallback."""
    out: list[RustcDiagnostic] = []
    lines = build_output.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("error"):
            continue
        rest = line[len("error"):]
        code = ""
        if rest.startswith("[E"):                 # optional [E<digits>]
            j = 2
            while j < len(rest) and rest[j].isdigit():
                j += 1
            if j == 2 or j >= len(rest) or rest[j] != "]":
                continue
            code = rest[1:j]
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
        out.append(RustcDiagnostic(
            code, msg, loc[:colon], int(loc[colon + 1:colon + 1 + digits])))
    return out


def compile_gate_reason(cluster_name: str, cargo_output: str) -> str | None:
    """Refined 'incomplete' reason when a nonzero-rc run reported 0 tests
    because the lib failed to compile: name the real gate — error
    count + first diagnostic — instead of the opaque "some binary failed to
    compile or crashed". Returns None when no rustc error is parseable
    (genuine crash/timeout — keep the generic wording)."""
    errors = parse_rustc_compile_errors(cargo_output)
    if not errors:
        return None
    return (f"cluster '{cluster_name}': lib failed to compile — "
            f"{len(errors)} error(s), first: {errors[0].rendered()}")


def compile_error_digest(
    cargo_output: str, *, limit: int = 6,
) -> tuple[int, list[str]]:
    """`(distinct_error_count, first <=limit rendered diagnostics)` for a build
    that failed to compile.

    A cluster-wide compile break emits the SAME diagnostic once per test binary
    (one `anyhow` lib error reappears under test_fmt, test_repr, test_macros, …),
    so the raw list is mostly duplicates. Dedup on `(message, file, line)` and
    keep first-seen order: what a reader needs is the distinct set of things
    actually wrong, not its multiplicity across binaries.

    Rendered as `file:line: error[CODE]: message` — the code is carried, not
    dropped: it is what tells a reader (and the layout-retrigger classifier)
    which CLASS of failure this is. Empty list when nothing parses (a link error
    or an ICE prints no `-->` location) — callers must keep their generic
    wording rather than claim zero errors.
    """
    seen: set[RustcDiagnostic] = set()
    rendered: list[str] = []
    for diag in parse_rustc_compile_errors(cargo_output):
        if diag in seen:
            continue
        seen.add(diag)
        if len(rendered) < limit:
            rendered.append(diag.rendered())
    return len(seen), rendered


def uncoded_src_diagnostics(cargo_output: str) -> tuple[str, ...]:
    """Files under `src/` named by a diagnostic rustc printed WITHOUT an error
    code, first-seen order, deduped.

    rustc codes every type, borrow and resolution error (`error[E0308]`) and
    codes NONE of its parse errors (`error: unexpected closing delimiter`,
    `error: expected one of …`, `error: this file contains an unclosed
    delimiter`). An uncoded diagnostic in the crate's own source is
    therefore the sweep-side signature of a file no fill route can rewrite:
    the extractor enumerates nothing from it, so the frontier cannot name a
    unit in it and every later sweep prints the same line (run
    `2973-66e50ad9`, 2026-09-04: `src/context.rs:35: error: unexpected
    closing delimiter` three times, then a cluster abort). A lint denied to
    an error is uncoded too, which is why the caller confirms each file
    against the structure reader before treating it as a mechanical break.
    Test targets are excluded on purpose: the oracle is never healed from a
    sweep line."""
    out: list[str] = []
    for diag in parse_rustc_compile_errors(cargo_output):
        f = diag.file.replace("\\", "/")
        if diag.code or not (f.startswith("src/") or "/src/" in f):
            continue
        if f not in out:
            out.append(f)
    return tuple(out)


def compile_failed_targets(cargo_output: str) -> tuple[str, ...]:
    """Cargo target descriptors from every `could not compile \\`crate\\` (<target>)`
    line, first-seen order, deduped — e.g. `("lib", 'test "test_autotrait"')`.

    Cargo names the failing TARGET on each compile failure, and that name is the
    only evidence separating "a body inside the lib is wrong" from "the lib built
    and what links against it did not". Empty when no such line parses (link
    error, ICE, unreadable output): callers must read empty as UNKNOWN, never as
    "nothing failed"."""
    marker = _COULD_NOT_COMPILE + " "
    out: list[str] = []
    for line in strip_ansi(cargo_output).split("\n"):
        stripped = line.lstrip()
        if not stripped.startswith("error"):
            continue
        k = stripped.find(marker)
        if k < 0:
            continue
        open_p = stripped.find("(", k)
        close_p = stripped.find(")", open_p) if open_p >= 0 else -1
        if open_p < 0 or close_p < 0:
            continue
        target = stripped[open_p + 1:close_p].strip()
        if target and target not in out:
            out.append(target)
    return tuple(out)


def lib_compile_clean(cargo_output: str) -> bool:
    """True iff the build failed and NO failing target is the library itself.

    The discriminator between a wrong BODY and a wrong SHAPE: fn bodies live
    inside the lib, so once the lib compiles, nothing a fill wrote can explain a
    test binary that did not build. What remains is the API surface those
    binaries link against — auto-traits, trait bounds, signatures.

    Conservative by construction: no parseable failing target returns False, so
    a caller gated on this never acts on absence of evidence."""
    targets = compile_failed_targets(cargo_output)
    if not targets:
        return False
    return not any(t.split()[0] == "lib" for t in targets if t.split())


def rustc_error_signatures(cargo_output: str) -> frozenset[str]:
    """Stable rustc error signatures (`error[E<code>]: <message>`, no file/line),
    regex-free. Equivalent to `re.findall(r'error\\[E\\d+\\]: .+', output)`:
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


# ---------------------------------------------------------------------------
# Per-binary test readings (crash-regime scoping for the per-fill gate)
# ---------------------------------------------------------------------------
# A cluster test run is N independent binaries. When one crashes mid-run
# (SIGSEGV/abort) its tests silently vanish from the output, and WHICH of them
# printed a verdict first varies run-to-run — so whole-run pass counts and
# failed-name sets become draws from a distribution, not measurements. Any
# before/after comparison must therefore be scoped to binaries that COMPLETED
# (printed their `test result:` trailer) in BOTH readings; a crashed binary
# testifies for nobody. The one exact cross-regime fact: a binary that was
# complete at baseline and crashed after a single change was crashed BY that
# change.


@dataclass(frozen=True)
class BinaryResult:
    """One test target's section of a cargo test run.

    ``completed`` is True iff the section printed its `test result:` trailer —
    a section that opened (`running N tests`) but never closed means the
    process died mid-run. ``dangling`` is the test that had printed its
    `test NAME ... ` prefix but no verdict when the process died (under
    `--test-threads=1` this is precisely the crashing test). ``exe_path`` is
    the executable from the `Running <desc> (<path>)` stderr line, kept so a
    crash localizer can re-run the exact binary.

    ``pass_count``/``fail_count`` come from the trailer (authoritative even
    when per-test lines are absent, e.g. `-q` runs); the name SETS drive
    newly-red attribution only. For a crashed section they fall back to the
    verdicts observed before death."""

    label: str
    completed: bool
    passed: frozenset[str]
    failed: frozenset[str]
    pass_count: int = 0
    fail_count: int = 0
    dangling: str | None = None
    exe_path: str | None = None
    # The section was still OPEN when the caller's timeout cut the run: the
    # process did not die, a test never returned (`dangling` names it). The
    # shape a hung binary leaves is the shape a crashed one leaves minus the
    # panic, and until this flag existed every rc -1 path read it as "nothing
    # was measured": run `3014175-d98ed09e` (2026-09-08) kept three `fmt`
    # bodies on that reading, and the next sweep never returned.
    hung: bool = False
    # Names libtest reported as `ignored` (cfg-gated off for THIS cluster, e.g.
    # `ignored, requires nightly`). Kept, not dropped: an ignored test reports
    # no pass/fail verdict, so an oracle that only knows passed∪failed counts
    # it as VANISHED and charges it as a failure — fabricating a crash signal
    # for a test that was never meant to run here.
    ignored: frozenset[str] = frozenset()
    # Tests whose OWN process died (a signal, a double panic) when the
    # binary was re-run one test per process (`test_isolation`). Each is
    # also in ``failed``: an abort is a failing verdict of that test and of
    # no other. Read off the failure block's `ABORT_MARK` line, so every
    # reader of a capture sees the same typed outcome.
    aborted: frozenset[str] = frozenset()
    # The subset of ``aborted`` whose own failure block printed no panic at
    # all: the process died on a signal with nothing the runtime announced —
    # a native fault (a segfault, a sanitizer abort, undefined behaviour).
    # These are what the crash localizer re-runs under gdb / ASAN; an abort
    # that printed a panic already names its site in the block.
    aborted_silent: frozenset[str] = frozenset()

    @property
    def executed(self) -> int:
        return self.pass_count + self.fail_count


@dataclass(frozen=True)
class TestStateReading:
    """A full cluster test-run reading, per binary. Replaces the bare
    ``(pass_count, failed_names)`` tuple as the regression-gate currency so
    the gate can scope its comparison to completed binaries."""

    __test__ = False  # domain VO, not a pytest class (silences collection warning)

    binaries: tuple[BinaryResult, ...]
    #: (binary, test) -> the line of the failing test's OWN body it stopped
    #: at, for the failures that have an assertion position at all
    #: (`assertion_position.assertion_positions`: a panic in `src/`, in a
    #: helper or in a closure has none). Read off the same capture as
    #: `binaries` (`test_report.reading_of_capture`). It lives on the reading
    #: so that every keep decision comparing an attempt to this baseline has
    #: the positions without a second parse: until 2026-09-29 the impl, pair
    #: and per-fn keeps judged "non-regression" on names alone (run
    #: `24951-a72c4642`: a pair keep moved `test_altdebug` from failing at
    #: `tests/test_fmt.rs:91` to `:90` and was kept).
    positions: Mapping[TestKey, int] = field(default_factory=dict)

    @property
    def pass_count(self) -> int:
        return sum(b.pass_count for b in self.binaries)

    @property
    def fail_count(self) -> int:
        return sum(b.fail_count for b in self.binaries)

    @property
    def failed_names(self) -> frozenset[str]:
        out: set[str] = set()
        for b in self.binaries:
            out |= b.failed
        return frozenset(out)

    @property
    def passed_names(self) -> frozenset[str]:
        """Names observed passing in COMPLETED binaries only. A crashed
        binary's partial `ok` verdicts are a nondeterministic prefix (which
        tests print before the signal varies run-to-run), so they can neither
        exonerate nor convict — a test only counts as "was passing" when its
        binary finished."""
        out: set[str] = set()
        for b in self.binaries:
            if b.completed:
                out |= b.passed
        return frozenset(out)

    @property
    def judged_names(self) -> frozenset[str]:
        """Names that RECEIVED a verdict in a COMPLETED binary — passed or
        failed. This is the comparison base for "the change under test
        destroyed a verdict": a test judged here and unjudged in a later
        reading lost its verdict to whatever changed in between. Partial
        verdicts from a crashed binary are excluded for the same reason
        `passed_names` excludes them — which tests print before the signal
        varies run to run."""
        out: set[str] = set()
        for b in self.binaries:
            if b.completed:
                out |= b.passed | b.failed
        return frozenset(out)

    @property
    def ignored_names(self) -> frozenset[str]:
        """Names libtest reported `ignored` (cfg-gated off for this cluster).
        Callers subtract these from any "not passing ⇒ failing" reading — an
        ignored test is neither."""
        out: set[str] = set()
        for b in self.binaries:
            out |= b.ignored
        return frozenset(out)

    @property
    def executed(self) -> int:
        return sum(b.executed for b in self.binaries)

    @property
    def aborted_names(self) -> frozenset[str]:
        """Tests whose own isolated process died (see `BinaryResult.aborted`)."""
        out: set[str] = set()
        for b in self.binaries:
            out |= b.aborted
        return frozenset(out)

    @property
    def aborted_silent_names(self) -> frozenset[str]:
        """Aborted tests whose own block printed no panic — native faults."""
        out: set[str] = set()
        for b in self.binaries:
            out |= b.aborted_silent
        return frozenset(out)

    @property
    def crashed(self) -> tuple[BinaryResult, ...]:
        return tuple(b for b in self.binaries if not b.completed)

    @property
    def hung_binaries(self) -> tuple[BinaryResult, ...]:
        """The binaries the timeout cut while their section was open."""
        return tuple(b for b in self.binaries if b.hung)

    @property
    def timed_out(self) -> bool:
        """The run was cut by the caller's timeout inside a test section."""
        return any(b.hung for b in self.binaries)

    def by_label(self) -> dict[str, BinaryResult]:
        return {b.label: b for b in self.binaries}

    def keyed(self) -> KeyedVerdicts:
        """This reading per (binary, test): what every keep compares
        (`assertion_position.outcomes_of`). The `*_names` properties union
        the binaries, so a name two binaries hold can sit in `passed_names`
        and `failed_names` at once; no comparison reads them for that."""
        from pipeline_core.domain.assertion_position import (
            KeyedVerdicts,
            TestKey,
        )

        def _k(label: str, names: Iterable[str]) -> set[TestKey]:
            return {TestKey(label, bare_test_name(n)) for n in names}

        passed: set[TestKey] = set()
        failed: set[TestKey] = set()
        ignored: set[TestKey] = set()
        aborted: set[TestKey] = set()
        silent: set[TestKey] = set()
        for b in self.binaries:
            if b.completed:
                passed |= _k(b.label, b.passed)
            failed |= _k(b.label, b.failed)
            ignored |= _k(b.label, b.ignored)
            aborted |= _k(b.label, b.aborted)
            silent |= _k(b.label, b.aborted_silent)
        return KeyedVerdicts(
            passed=frozenset(passed), failed=frozenset(failed),
            ignored=frozenset(ignored), aborted=frozenset(aborted),
            aborted_silent=frozenset(silent), positions=dict(self.positions))


# Verdict tokens libtest itself prints. Anything ELSE sitting where a verdict
# belongs is not a verdict: libtest writes `test NAME ... ` WITHOUT a newline
# and only then the outcome, so when the process dies mid-test whatever is
# written next (an ASAN report, a signal message) lands on that same line.
# Treating an unknown token as "no verdict" is what lets the localizer name
# the in-flight test from a real crash capture.
_LIBTEST_VERDICTS = ("ok", "FAILED", "ignored", "bench")

# The first line of an aborted test's failure block in a section rebuilt by
# `test_isolation`: the test's own process died, so libtest printed no
# verdict for it and this line records that it is a failure by abort. Not a
# libtest string, deliberately — nothing libtest prints can be mistaken for
# it, and every parser of a capture reads the same outcome off it.
ABORT_MARK = "determ-isolation: test process aborted"


def failure_block_name(line: str) -> str | None:
    """``---- NAME stdout ----`` (libtest's per-failure block header) → NAME."""
    head, tail = "---- ", " stdout ----"
    if not (line.startswith(head) and line.endswith(tail)):
        return None
    name = line[len(head):-len(tail)]
    return name if name and " " not in name else None


#: The line a capture puts between a process's stdout and its stderr. The
#: layout is stdout, this line, stderr — written by :func:`join_capture` and
#: read by :func:`split_capture`, and nowhere else.
STDERR_MARK = "--- stderr ---"


def join_capture(stdout: str, stderr: str) -> str:
    """The one capture layout: stdout, then :data:`STDERR_MARK`, then
    stderr (no mark when stderr is empty)."""
    return stdout + ("\n" + STDERR_MARK + "\n" + stderr if stderr else "")


def split_capture(output: str) -> tuple[str, str]:
    """(stdout, stderr) of a :func:`join_capture` capture, or (output, "")
    when the capture carries no mark (hand-made fixtures, other runners).

    The split is at the LAST mark. Every writer that embeds a process's
    output inside a stdout section now writes it without the mark
    (`test_isolation.read_single_test_run`), but captures recorded before
    that carry one mark per isolated test inside stdout, and stderr is
    cargo's and the runtime's, which never print the mark. Two readers
    that each switched at the FIRST mark read those embedded marks as
    stderr: `binary_output_sections` marked every isolated binary as never
    completed and dropped every section after it (measured 2026-09-27 on a
    capture rebuilt by the real isolation renderer), and the sanitizer
    sweep, which joined its streams with no mark at all, let trybuild's
    stderr progress line become a crashed binary's in-flight test (run
    `12616-8345ae1d`)."""
    lines = output.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == STDERR_MARK:
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    return output, ""


def _verdict_of_test_line(line: str) -> tuple[str, str] | None:
    """``test NAME ... ok|FAILED|ignored|bench`` → (name, verdict); a line
    whose verdict slot is empty or holds foreign text (the in-flight test when
    a binary dies) → (name, ""). None otherwise. Pure string ops. Handles
    doc-test names containing spaces by splitting on the LAST ``" ... "``.

    A NAME holding a control character is not libtest's line: libtest never
    colours a test name, and trybuild prints its own progress in the same
    shape with the name wrapped in ANSI escapes (``test \\x1b[0m\\x1b[1m
    tests/ui/x.rs\\x1b[0m ... \\x1b[0m\\x1b[32mok``) on stderr, which a
    combined capture places after every binary's stdout. Read as a verdict-less
    test line, it became the in-flight test of the last stdout section when
    that binary aborted before flushing its own line: the sanitized sweep of
    run `12616-8345ae1d` (2026-09-27, `logs/asan/asan-20260927T064608729091.txt`)
    named `tests/wrapper_gen_6.rs`'s crashed test `tests/ui/temporary-value.rs`,
    and that phantom name reached the crash lines, the crash event and every
    CRASH_EVIDENCE block, while the binary's real in-flight test was named
    nowhere. An unknown in-flight name is honest; a foreign one is not."""
    if not line.startswith("test "):
        return None
    if " ... " in line:
        name, _, tail = line[len("test "):].rpartition(" ... ")
        if not name or _has_control(name):
            return None
        # libtest may qualify a verdict with a reason — `ignored, requires
        # nightly`. The token carries the comma, so an unstripped compare
        # fails the verdict table and the line falls through to "no verdict",
        # marking a cfg-gated test as the in-flight one that crashed.
        head = tail.split()[0].rstrip(",") if tail.split() else ""
        verdict = head if head in _LIBTEST_VERDICTS else ""
        return (strip_libtest_qualifier(name.strip()), verdict)
    if line.endswith(" ..."):
        name = strip_libtest_qualifier(line[len("test "):-len(" ...")].strip())
        return (name, "") if name and not _has_control(name) else None
    return None


def _has_control(text: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def _target_label(line: str) -> tuple[str, str | None] | None:
    """The (label, exe) a cargo stderr line names — ``Running <desc>
    (<path>)`` or ``Doc-tests <crate>`` — or None for any other line. The
    ONE rule both :func:`parse_binary_results` and
    :func:`binary_output_sections` pair sections by, so a crash's evidence
    is filed under the same label the regression comparison names."""
    if line.startswith("Running "):
        desc, sep, tail = line[len("Running "):].partition(" (")
        exe = tail[:-1].strip() if sep and tail.endswith(")") else None
        # Fall back to the executable before `?`: an unnamed target is
        # still identifiable by the binary cargo ran.
        return (desc.strip() or exe or "?", exe)
    if line.startswith("Doc-tests "):
        return ("Doc-tests " + line[len("Doc-tests "):].strip(), None)
    return None


def _is_section_header(line: str) -> bool:
    """libtest's ``running N tests`` / ``running 1 test`` preamble."""
    return line.startswith("running ") and line.endswith(("tests", "test"))


# Public names for the two line rules the capture's section pairing rests on,
# so `test_isolation` splices by exactly the rules the parsers read by.
target_label = _target_label
is_section_header = _is_section_header


def _opens_implicit_section(line: str) -> bool:
    """Whether a line with no open section opens one anyway — the
    headerless-capture tolerance :func:`parse_binary_results` documents
    (a trailer, or a `test NAME ... <verdict>` line WITH a verdict)."""
    if line.startswith("test result:"):
        return True
    _open = _verdict_of_test_line(line)
    return _open is not None and bool(_open[1])


def _unnamed_label(index: int) -> str:
    return f"unnamed section #{index + 1}"


@dataclass(frozen=True)
class BinaryOutputSection:
    """One test target's RAW lines in a combined `cargo test` capture: what
    it printed on stdout after its ``running N tests`` header, plus what
    cargo and the runtime printed on stderr under its ``Running`` line.
    ``label`` is the same label :func:`parse_binary_results` gives the
    target, so a comparison's ``newly_crashed`` names index straight into
    these; ``completed`` is the same trailer rule.

    :class:`BinaryResult` deliberately carries no panic text — the READING
    is verdicts. Crash attribution needs the text, and needs it PER BINARY:
    an all-or-nothing rule over the whole capture let an assertion panic in
    one binary veto the stub-only evidence of another (run
    `2585062-494e630e`, 2026-09-07/08: 19 of 21 aborted binaries held only
    stub panics; the three assertion panics sat in two others and vetoed
    every keep). This is that slicer, a sibling of the parser rather than a
    field on it."""

    label: str
    text: str
    completed: bool


def binary_output_sections(test_output: str) -> tuple[BinaryOutputSection, ...]:
    """Slice a combined capture into one :class:`BinaryOutputSection` per
    ``running N tests`` section, paired index-wise with the ``Running`` /
    ``Doc-tests`` labels exactly as :func:`parse_binary_results` pairs them
    (cargo executes targets sequentially, so the i-th label names the i-th
    section; a section past the last label is ``unnamed section #i``).

    Two layouts are read. The pipeline's combined layout is stdout, then
    :data:`STDERR_MARK`, then stderr, split by :func:`split_capture`: stdout
    lines belong to the section whose header they follow, stderr lines to
    the label they follow. Measured on rustc 1.95.0 (run `2585062-494e630e`): a binary
    aborted by a panic in `Drop` prints its crate panics — ``not
    implemented`` at the stub's own line — on STDOUT under its header, and
    the runtime's ``panicked at /rustc/…/library/core/src/panicking.rs:233:5:
    panic in a destructor during cleanup`` plus ``thread caused
    non-unwinding panic. aborting.`` on STDERR under its ``Running`` line.
    Both halves are this binary's evidence, so the section text is their
    concatenation. An interleaved capture (no separator — hand-made
    fixtures, other runners) attributes each line to the most recent marker
    of either kind, which is the same pairing because cargo prints a
    target's ``Running`` line before the target prints its header."""
    labels: list[str] = []
    for raw in test_output.splitlines():
        _lab = _target_label(raw.strip())
        if _lab is not None:
            labels.append(_lab[0])
    stdout_by: dict[int, list[str]] = {}
    stderr_by: dict[int, list[str]] = {}
    completed: set[int] = set()
    sec_idx = -1
    lab_idx = -1
    sec_open = False
    out_text, err_text = split_capture(test_output)
    for raw in out_text.splitlines():
        line = raw.strip()
        if _target_label(line) is not None:
            lab_idx += 1
            continue
        if _is_section_header(line):
            sec_idx += 1
            sec_open = True
            continue
        if not sec_open and _opens_implicit_section(line):
            sec_idx += 1
            sec_open = True
        if sec_open:
            stdout_by.setdefault(sec_idx, []).append(raw)
            if line.startswith("test result:"):
                completed.add(sec_idx)
                sec_open = False
            continue
        # Between a target's `Running` line and its header (interleaved
        # layout), or after its trailer: cargo's / the runtime's own lines
        # for the most recently announced target.
        idx = max(lab_idx, sec_idx)
        if idx >= 0:
            stderr_by.setdefault(idx, []).append(raw)
    lab_idx = -1
    for raw in err_text.splitlines():
        if _target_label(raw.strip()) is not None:
            lab_idx += 1
            continue
        if lab_idx >= 0:
            stderr_by.setdefault(lab_idx, []).append(raw)
    out: list[BinaryOutputSection] = []
    for i in range(sec_idx + 1):
        out.append(BinaryOutputSection(
            label=labels[i] if i < len(labels) else _unnamed_label(i),
            text="\n".join(stdout_by.get(i, []) + stderr_by.get(i, [])),
            completed=i in completed))
    return tuple(out)


def parse_binary_results(
    test_output: str, *, timed_out: bool = False,
) -> tuple[BinaryResult, ...]:
    """Split a combined cargo-test stdout+stderr capture into per-binary
    :class:`BinaryResult`s. Labels come from the stderr ``Running <desc>
    (<path>)`` / ``Doc-tests <crate>`` lines; sections open on the stdout
    ``running N tests`` header and close on their ``test result:`` trailer.
    Cargo executes targets sequentially, so the i-th label names the i-th
    section even when stderr and stdout ordering interleaves (same pairing
    rule as :func:`summarize_test_binaries`). A section with no trailer is a
    crashed binary: ``completed=False`` and any dangling ``test NAME ... ``
    prefix line (verdict never printed) names the in-flight test.

    ``timed_out`` says the capture is the partial output of a run the
    CALLER cut (the cargo adapter returns it with rc -1). Cargo runs
    targets one after another, so at most one section can be open at the
    cut — the last one — and that binary did not die, it HUNG: it is marked
    ``hung`` (its ``dangling`` test is the one that never returned). Earlier
    unclosed sections in the same capture are crashes as before (cargo
    went on to the next target after them)."""
    labels: list[tuple[str, str | None]] = []
    for raw in test_output.splitlines():
        _lab = _target_label(raw.strip())
        if _lab is not None:
            labels.append(_lab)

    class _Section:
        __slots__ = ("passed", "failed", "ignored", "aborted", "block",
                     "panicked", "completed", "dangling", "pass_count",
                     "fail_count")

        def __init__(self) -> None:
            self.passed: set[str] = set()
            self.failed: set[str] = set()
            self.ignored: set[str] = set()
            self.aborted: set[str] = set()
            self.block: str | None = None
            # Blocks that printed a `panicked at` line.
            self.panicked: set[str] = set()
            self.completed = False
            self.dangling: str | None = None
            self.pass_count = 0
            self.fail_count = 0

    sections: list[_Section] = []
    current: _Section | None = None
    # Sections, verdicts and in-flight tests are stdout's; a stderr line is
    # never a verdict (labels above are read from the whole capture).
    for raw in split_capture(test_output)[0].splitlines():
        line = raw.strip()
        if _is_section_header(line):
            current = _Section()
            sections.append(current)
            continue
        if current is None or current.completed:
            # Tolerate headerless captures (truncated logs, minimal
            # fixtures): a verdict/trailer line with no open section opens
            # an implicit one instead of being dropped.
            #
            # A verdict-LESS `test NAME ...` line does NOT: it is the weakest
            # evidence there is, and cargo re-prints exactly that shape inside
            # a failed binary's captured stdout — trybuild's own progress lines
            # (`test tests/ui/x.rs ... ok`, ANSI-coloured so the verdict slot
            # reads as foreign text) come after that binary's trailer and were
            # opening a phantom, never-completed section. It counted as a
            # SECOND crashed binary in every `anyhow` sweep, and had cargo
            # printed the dump between two targets it would have shifted every
            # later label by one. `dangling` describes an in-flight test in a
            # section already open; it must not conjure the section.
            if _opens_implicit_section(line):
                current = _Section()
                sections.append(current)
            else:
                continue
        if line.startswith("test result:"):
            current.completed = True
            current.pass_count, current.fail_count = _counts_on_result_line(line)
            continue
        _blk = failure_block_name(line)
        if _blk is not None:
            current.block = _blk
            continue
        if line.startswith(ABORT_MARK) and current.block is not None:
            current.aborted.add(current.block)
            continue
        if current.block is not None and "panicked at " in line:
            current.panicked.add(current.block)
        parsed = _verdict_of_test_line(line)
        if parsed is None:
            continue
        name, verdict = parsed
        if verdict == "ok":
            current.passed.add(name)
            if current.dangling == name:
                current.dangling = None
        elif verdict == "FAILED":
            current.failed.add(name)
            if current.dangling == name:
                current.dangling = None
        elif verdict == "ignored":
            current.ignored.add(name)
            if current.dangling == name:
                current.dangling = None
        elif verdict == "":
            # `test NAME ...` printed, no libtest verdict followed: in-flight
            # when the process died (the crash output itself may occupy the
            # rest of the line). A later verdict line for the same name
            # (parallel-harness interleaving) clears it above.
            current.dangling = name

    out: list[BinaryResult] = []
    last_open = max((i for i, sec in enumerate(sections) if not sec.completed),
                    default=None)
    for i, sec in enumerate(sections):
        # A section with no matching `Running <desc>` label still has an
        # IDENTITY — its position in cargo's sequential execution order. The
        # old `"?"` threw that away, and the docstring above already records a
        # live episode where EVERY label rendered `?` because the labels are
        # stderr lines interleaved with stdout. Two costs, both observed on
        # `anyhow`: the u2 regression verdict logs an exclusion nobody can
        # audit (`binaries ['?', 'tests/test_downcast.rs'] were already crashed
        # at baseline`, every few minutes for a whole run), and the crash
        # localizer skips the binary outright because it also has no exe path.
        # Naming the section costs nothing and makes both auditable.
        label, exe = (labels[i] if i < len(labels)
                      else (_unnamed_label(i), None))
        if not sec.completed:
            # crashed section: only the observed verdicts exist
            sec.pass_count, sec.fail_count = len(sec.passed), len(sec.failed)
        out.append(BinaryResult(
            label=label,
            completed=sec.completed,
            passed=frozenset(sec.passed),
            failed=frozenset(sec.failed),
            pass_count=sec.pass_count,
            fail_count=sec.fail_count,
            dangling=None if sec.completed else sec.dangling,
            exe_path=exe,
            ignored=frozenset(sec.ignored),
            aborted=frozenset(sec.aborted & sec.failed),
            aborted_silent=frozenset((sec.aborted & sec.failed)
                                     - sec.panicked),
            hung=bool(timed_out and i == last_open),
        ))
    return tuple(out)


def hung_sections(test_output: str) -> tuple[BinaryResult, ...]:
    """The binaries a timed-out capture shows as hung (see
    :func:`parse_binary_results`); empty when no section was open — the
    timeout cut a build, not a test."""
    return tuple(b for b in parse_binary_results(test_output, timed_out=True)
                 if b.hung)


def native_crashes(
    binaries: Sequence[BinaryResult],
) -> tuple[tuple[BinaryResult, str | None], ...]:
    """The native faults a reading still shows, each as (binary, the one test
    to re-run or None for the whole binary). This is the one definition of
    "the crash is still there": the localizer blames units from it, and every
    later reader that asks whether that blame still holds reads the same set.

    Two shapes carry a native fault. A binary whose section never closed (it
    could not be re-run one test per process). And, the normal case since the
    capture isolates aborted binaries, a test whose OWN process died without
    printing a panic (`aborted_silent`): an isolated binary always completes,
    so a reader that asks only "did a binary not complete" sees no crash on
    any isolated capture. Run `118933-877c43bd` (2026-09-27) fill 1: the
    stale-refill probe asked exactly that and dropped a crash-blamed unit as
    "crash gone" while 11 binaries were still aborting on the same stack
    overflow. An abort that printed a panic already names its site in its
    failure block and is not a native fault."""
    out: list[tuple[BinaryResult, str | None]] = []
    for b in binaries:
        if not b.completed:
            out.append((b, None))
        for name in sorted(b.aborted_silent):
            out.append((replace(b, dangling=name), name))
    return tuple(out)


def unmeasured_failures(
    binaries: Sequence[BinaryResult], failed: Iterable[str],
) -> frozenset[str]:
    """Bare names of the failing tests whose own verdict was never read: the
    failure reported with no binary verdict behind it (its binary died before
    it printed one), or a test the reading still shows as a native crash
    (:func:`native_crashes`) — its process died before any assertion spoke.

    The schedule ranks a run aimed only at these, holding no crash-blamed
    unit, as a flat re-buy. Before `native_crashes` this was "no binary
    verdict" alone, which an isolated capture never produces: from the day
    isolation shipped (2026-09-19) to run `118933-877c43bd` (2026-09-27) the
    set was non-empty in 1 of ~160 fills, and the fill of that run put two
    formatter pairs and six file/impl runs aimed only at 13 stack-overflow
    aborts into the budget beside the runs holding the recursion."""
    verdicted = {bare_test_name(n) for b in binaries for n in b.failed}
    crashed = {bare_test_name(name) for _b, name in native_crashes(binaries)
               if name}
    return frozenset(
        bare for bare in (bare_test_name(n) for n in failed)
        if bare not in verdicted or bare in crashed)


def still_open_names(output: str) -> frozenset[str]:
    """A name is open if it reported FAILED or it never reported a verdict.

    ``parse_failed_tests`` is "reported fail" — U2, fail-counts, and the
    retry's TEST mode are correctly aimed by those names. A SIGABRT, an
    ASAN abort, a segfault emit none of those lines, so a probe that asks
    "are this unit's attributed tests in the failed set" treats a crash
    as success. Run `1911637-d0fab212` iters 3–5: `test_large_alignment`
    SIGABRTed with no FAILED line; the probe dropped `downcast_ref` and
    `object_downcast` as already passing.

    Implementation is a wrap, not a new parser: every name
    ``parse_failed_tests`` already reports (including a
    ``---- name stdout ----`` header that a summary ``FAILED`` line can
    miss) union every incomplete binary's dangling in-flight name.
    Ignored tests are not open. ``reconcile_sweep`` stays the SCORE's
    vanished set; this function has the cargo capture and nothing else.
    """
    open_names = set(parse_failed_tests(output))
    for binary in parse_binary_results(output):
        if not binary.completed and binary.dangling:
            open_names.add(binary.dangling)
    return frozenset(open_names)


@dataclass(frozen=True)
class RegressionComparison:
    """Outcome of a binary-scoped baseline-vs-after comparison.

    ``measured`` — labels complete in BOTH readings (the only verdicts that
    count). ``newly_crashed`` — complete at baseline WITH at least one passing
    test, crashed after: a real regression (something that worked was lost).
    ``crashed_all_red`` — complete at baseline with ZERO passing tests,
    crashed after: NOT a regression — every test in it was already failing,
    only the failure MODE moved (caught panic → process abort; typical when a
    newly-filled constructor lets tests reach a still-stubbed Drop). Judging
    these as regressions convicted every partial fill of a coupled family for
    sibling stubs it cannot control. ``excluded_crashed`` — crashed in both
    readings: pre-existing, excluded, never blamed. ``crash_lost_pass`` — how
    many tests those ``newly_crashed`` binaries were passing at baseline, i.e.
    what the crash actually COST, so the verdict can weigh it against what the
    fill bought elsewhere instead of vetoing on the crash alone."""

    newly_red: frozenset[str]
    newly_crashed: tuple[str, ...]
    excluded_crashed: tuple[str, ...]
    not_run: tuple[str, ...]
    measured: tuple[str, ...]
    base_pass: int
    cur_pass: int
    base_executed: int
    cur_executed: int
    crashed_all_red: tuple[str, ...] = ()
    crash_lost_pass: int = 0
    # Binaries complete at baseline that the after-reading's timeout cut
    # while their section was open — a test that never returned — and the
    # in-flight test of each (``label::test``). A hang is charged whatever
    # the binary was passing: it blocks every binary after it (they never
    # ran) and every later sweep costs the whole timeout, so it is oracle
    # corruption of the ``executed_shrank`` kind, never traded away.
    newly_hung: tuple[str, ...] = ()
    hung_tests: tuple[str, ...] = ()
    # What each newly-red test PRINTED (bare name → the failing assertion
    # with its `left` / `right` values), attached by the gate from the same
    # capture the comparison was read from. Empty on a comparison built
    # without a capture; `compare_readings` never fills it. See
    # `test_report.regressed_test_outputs` for the instance.
    newly_red_output: Mapping[str, str] = field(default_factory=dict)
    # The `(file, line)` stub-panic sites of the ``newly_crashed`` binaries
    # when — and only when — every crate panic those binaries printed was a
    # stub panic (`test_report.stub_only_crash` over exactly those
    # sections). Attached by the regression gate on its crash-only exits so
    # the retro writer downstream can stamp the record it earns from this
    # verdict as one the tree's holes decided (`crash_stub_sites` on the
    # record), rather than reading the verdict's prose for it. Empty on a
    # comparison the gate did not judge crash-only, and on a crash with no
    # stub evidence (a segfault): `compare_readings` never fills it.
    crash_stub_sites: frozenset[tuple[str, int]] = frozenset()
    # Tests failing at baseline and passing after, over the measured
    # binaries only — the per-test half of ``net_pass_delta``. A delta says
    # the change paid; this says WHOM it paid. Run `11358-e5de094e`
    # (2026-09-15) kept a pair write at net +2 that turned two of its three
    # aimed tests green; the route ledger recorded only the +2, and the
    # third test's unpaid-buy count restarted on a row that never moved it.
    newly_green: frozenset[str] = frozenset()

    @property
    def net_pass_delta(self) -> int:
        """Tests gained minus tests lost, across the WHOLE measured suite —
        crashed-after binaries counted as losing everything they passed."""
        return (self.cur_pass - self.base_pass) - self.crash_lost_pass

    @property
    def regressed(self) -> bool:
        """A fill is a regression iff it does not pay for what it costs.

        The objective is the oracle pass count, so a change that raises it is
        an improvement even when some individual test moved red or a binary
        started aborting — refusing a +40/-1 fill is not conservatism, it
        loses the run. Below a strict net gain the old absolute rule stands:
        no test may be lost for nothing. Vanished tests inside a binary that
        still completed are never traded away at any price — that is oracle
        corruption, not a behavioural cost."""
        if self.cur_executed < self.base_executed:
            return True
        if self.newly_hung:
            return True
        if self.net_pass_delta > 0:
            return False
        return bool(self.net_pass_delta < 0
                    or self.newly_red or self.newly_crashed)

    @property
    def summary(self) -> str:
        """One-line log label naming WHICH regression fired. Callers used to
        print '(pass-count drop)' whenever `newly_red` was empty, which
        mislabels a binary that crashed — the two are different failures with
        different fixes, and the log is where that distinction is read."""
        if self.newly_hung:
            plural = "ies" if len(self.newly_hung) > 1 else "y"
            return (f"binar{plural} hung: {list(self.newly_hung)} (timed out "
                    f"inside {list(self.hung_tests)}; net "
                    f"{self.net_pass_delta:+d} test(s))")
        if self.newly_crashed:
            plural = "ies" if len(self.newly_crashed) > 1 else "y"
            return (f"binar{plural} crashed: {list(self.newly_crashed)} "
                    f"(net {self.net_pass_delta:+d} test(s))")
        if self.newly_red:
            return f"newly red: {sorted(self.newly_red)}"
        if self.cur_pass < self.base_pass:
            return f"pass count {self.base_pass}→{self.cur_pass}"
        if self.cur_executed < self.base_executed:
            return f"executed count {self.base_executed}→{self.cur_executed}"
        return ""

    @property
    def signature(self) -> frozenset[str]:
        """Stall-signature key. Distinct regression CLASSES must not collapse
        onto one marker: two consecutive attempts, one crashing a binary and
        one dropping the pass count, are different failures and must not read
        as a repeat."""
        if self.newly_red:
            return self.newly_red
        if self.newly_hung:
            return frozenset(f"__binary_hang__:{b}" for b in self.newly_hung)
        if self.newly_crashed:
            return frozenset(f"__binary_crash__:{b}" for b in self.newly_crashed)
        if self.cur_pass < self.base_pass:
            return frozenset({"__pass_count_drop__"})
        if self.cur_executed < self.base_executed:
            return frozenset({"__executed_count_drop__"})
        return frozenset()


def stable_test_keys(b: BinaryResult) -> dict[str, str]:
    """Each test name of ``b`` → the identity it keeps across two trees.

    A doc test's cargo name ends in the line its ``` block opens on, so a
    fill that lengthens a body ABOVE it renames it: on the 2026-09-19
    `construct` replay (run `3213361-9d582ae7`, iter-2 checkpoint) the
    unchanged failing example `src/error.rs - error::Error::downcast_ref`
    read as a NEW failure at line 414 and convicted the fill. The identity
    is the name without its line plus the example's ordinal among the
    binary's doc tests of that name in source order — the rule
    `reference_floor.pairs_of` already compares trees by, applied to every
    test the binary reported so the ordinal does not depend on verdicts.
    Every other name is its own identity."""
    return stable_name_keys(b.passed | b.failed | b.ignored)


def stable_name_keys(names: Iterable[str]) -> dict[str, str]:
    """:func:`stable_test_keys` over any set of names of ONE binary (or of
    a capture whose doc tests all come from one doc-test target)."""
    out: dict[str, str] = {}
    ordinal: dict[str, int] = {}
    for n in sorted(names, key=lambda x: (doc_test_line(x) or 0, x)):
        if doc_test_line(n) is None:
            out[n] = n
            continue
        head = n.strip().rpartition(" (line ")[0].rstrip()
        k = ordinal.get(head, 0)
        ordinal[head] = k + 1
        out[n] = f"{head}#{k}"
    return out


def compare_readings(base: TestStateReading,
                     cur: TestStateReading) -> RegressionComparison:
    """Binary-scoped regression comparison. Only binaries complete in BOTH
    readings contribute verdicts/counts; a binary that crashed at baseline is
    excluded no matter what it printed after (its readings are noise); a
    binary that was complete at baseline and crashed after COSTS whatever it
    was passing — an all-red binary that starts crashing lost nothing that
    worked (only the failure mode moved, panic → abort), so it is reported
    separately and costs zero. Whether a cost convicts is
    :attr:`RegressionComparison.regressed`'s call, not this function's."""
    base_by, cur_by = base.by_label(), cur.by_label()
    measured: list[str] = []
    newly_crashed: list[str] = []
    crashed_all_red: list[str] = []
    excluded: list[str] = []
    not_run: list[str] = []
    newly_hung: list[str] = []
    hung_tests: list[str] = []
    newly_red: set[str] = set()
    newly_green: set[str] = set()
    base_pass = cur_pass = base_exec = cur_exec = crash_lost = 0
    for label, b in base_by.items():
        c = cur_by.get(label)
        if not b.completed:
            excluded.append(label)
            continue
        if c is None:
            # Absent section: the binary never STARTED in the after-reading
            # (cargo fail-fast stopped before it, or the run was cut short).
            # Inconclusive — exclude; only a section that opened and died is
            # attributable as a crash.
            not_run.append(label)
            continue
        if not c.completed:
            if c.hung:
                # The timeout cut the run inside this binary: charged as a
                # crash for the count (its passes are lost) AND as a hang
                # for the verdict, whatever it was passing.
                newly_hung.append(label)
                hung_tests.append(f"{label}::{c.dangling or '?'}")
            if b.pass_count == 0:
                crashed_all_red.append(label)
            else:
                newly_crashed.append(label)
                crash_lost += b.pass_count
            continue
        measured.append(label)
        b_key, c_key = stable_test_keys(b), stable_test_keys(c)
        b_failed = {b_key[n] for n in b.failed}
        newly_red |= {n for n in c.failed if c_key[n] not in b_failed}
        newly_green |= {n for n in c.passed if c_key[n] in b_failed}
        base_pass += b.pass_count
        cur_pass += c.pass_count
        base_exec += b.executed
        cur_exec += c.executed
    return RegressionComparison(
        newly_red=frozenset(newly_red),
        newly_crashed=tuple(sorted(newly_crashed)),
        excluded_crashed=tuple(sorted(excluded)),
        not_run=tuple(sorted(not_run)),
        measured=tuple(sorted(measured)),
        base_pass=base_pass, cur_pass=cur_pass,
        base_executed=base_exec, cur_executed=cur_exec,
        crashed_all_red=tuple(sorted(crashed_all_red)),
        crash_lost_pass=crash_lost,
        newly_hung=tuple(sorted(newly_hung)),
        hung_tests=tuple(sorted(hung_tests)),
        newly_green=frozenset(newly_green))


def regression_detail_kind(cmp: RegressionComparison) -> LastErrorKind:
    """What KIND of failure :func:`render_regression_detail` is about to
    describe — the same branch order, decided here so the fact can travel
    with the excerpt instead of being grepped back out of its prose.

    Every branch of this function reaches a prompt through
    `domain.last_error.LastErrorKind`; the reader consults the kind and
    falls back to the text only when a record carries no kind. The two
    functions are one contract and are tested together: a comparison that
    renders a given head must classify as that head's kind, so a wording
    change cannot silently re-answer the question.

    A regression is by construction a body rustc ACCEPTED — the gate only
    runs the tests after a clean compile — so no branch here returns
    ``COMPILE``. That kind belongs to the diagnostics path.
    """
    if cmp.newly_hung:
        return LastErrorKind.HUNG
    if cmp.newly_crashed:
        return LastErrorKind.CRASHED
    if cmp.newly_red:
        return LastErrorKind.REDDENED
    return LastErrorKind.LOST_TESTS


def render_regression_detail(cmp: RegressionComparison) -> str:
    """Prompt-ready explanation of a regression verdict. The opening sentence
    keeps the exact historical prefixes (the starvation classifier keys on
    them); the scope sentences state WHAT was measured and what was excluded
    as pre-existing, so the model is never blamed for a crash it didn't
    cause."""
    if cmp.newly_hung:
        head = (f"This made test binar{'ies' if len(cmp.newly_hung) > 1 else 'y'} "
                f"{list(cmp.newly_hung)} {HANG_HEAD_MARK} "
                f"inside {list(cmp.hung_tests)}, which never returned, and "
                "every test after it never ran. A hang is an infinite loop or "
                "unbounded recursion in a body this change reaches — a "
                "formatter or accessor that calls itself (directly, or through "
                "a wrapper that forwards back to it) is the usual shape. "
                + instruction_for(LastErrorKind.HUNG))
    elif cmp.newly_crashed:
        head = (f"This made test binar{'ies' if len(cmp.newly_crashed) > 1 else 'y'} "
                f"{list(cmp.newly_crashed)} {CRASH_HEAD_MARK} "
                "(e.g. segfault from bad unsafe code) — they completed fine "
                f"before this change, where they passed {cmp.crash_lost_pass} "
                "test(s). " + instruction_for(LastErrorKind.CRASHED))
    elif cmp.newly_red:
        head = (f"This compiled, but it {RED_HEAD_MARK}: "
                f"{sorted(cmp.newly_red)} (cluster pass count {cmp.base_pass}→"
                f"{cmp.cur_pass}).\n"
                + instruction_for(LastErrorKind.REDDENED))
    elif cmp.cur_executed < cmp.base_executed:
        head = (f"This {EXECUTED_DROP_MARK} "
                f"{cmp.base_executed}→{cmp.cur_executed} within the completed "
                "test binaries. " + instruction_for(LastErrorKind.LOST_TESTS))
    else:
        head = (f"This {PASS_DROP_MARK} {cmp.base_pass}→"
                f"{cmp.cur_pass} (tests that used to run/pass no longer do). "
                "Revert the change that removed them.")
    if cmp.excluded_crashed:
        head += ("\n(Note: test binar"
                 f"{'ies' if len(cmp.excluded_crashed) > 1 else 'y'} "
                 f"{list(cmp.excluded_crashed)} crashed BEFORE this change "
                 "too — pre-existing, excluded from this verdict, and NOT "
                 "your fault. Do not contort this fn to fix them.)")
    if cmp.crashed_all_red:
        head += ("\n(Note: test binar"
                 f"{'ies' if len(cmp.crashed_all_red) > 1 else 'y'} "
                 f"{list(cmp.crashed_all_red)} now abort, but every test in "
                 "them was ALREADY failing — not counted as regression. The "
                 "abort usually means a still-stubbed sibling (often Drop "
                 "machinery) is now reachable; those bodies are separate "
                 "work, not a fault of this change.)")
    head += render_newly_red_outputs(cmp.newly_red, cmp.newly_red_output)
    return head


# Retro-store record key: what the tests a reverted attempt reddened PRINTED
# (bare name → assertion with `left` / `right`), stored beside `regressed` so
# the next prompt for the same transaction can show the values and not only
# the names.
REGRESSED_OUTPUT = "regressed_output"


# Retro-store record key: WHICH attempt reddened the `regressed` tests, and
# what has happened to the transaction since. The next prompt's label is a
# claim about the tree ("the previous attempt was REVERTED"), and the stamp
# outlives the attempt it describes: run `11358-e5de094e` (2026-09-15) sent
# that sentence over fill 1's reverted pair write in fills 4 and 5, after
# fill 3 cashed and fill 4 was kept.
REGRESSED_ATTEMPT = "regressed_attempt"


class RegressedAttempt(StrEnum):
    #: The attempt that reddened them was reverted; nothing kept since.
    REVERTED = "reverted"
    #: The attempt that reddened them was KEPT on a net gain (the losses
    #: were forgiven), so those tests are red on the tree now.
    KEPT = "kept"
    #: An attempt reddened them and was reverted; a later attempt of the
    #: same units was kept, and its body is the one on the tree.
    REVERTED_THEN_KEPT = "reverted_then_kept"


def regressed_attempt_of(record: Mapping[str, object], *,
                         keep_clears_failed_body: bool = False,
                         ) -> RegressedAttempt:
    """The stamp on `record`, or what a record written before it can say.

    A record with no stamp was written by a revert (every writer of
    `regressed` before the stamp was a revert or a forgiven keep that did not
    exist yet), so it reads REVERTED — except on a record whose route clears
    `last_failed_body` on every keep and sets it on every revert
    (`keep_clears_failed_body`, the pair route): there, reddened tests beside
    an empty failed body mean a keep happened after the revert.
    """
    raw = record.get(REGRESSED_ATTEMPT)
    if isinstance(raw, str):
        try:
            return RegressedAttempt(raw)
        except ValueError:
            pass
    if (keep_clears_failed_body and record.get("regressed")
            and not str(record.get("last_failed_body") or "").strip()):
        return RegressedAttempt.REVERTED_THEN_KEPT
    return RegressedAttempt.REVERTED


#: Marks the part of a reddened test's printed output that quotes the crate
#: line it panicked at, read from the attempt before it was reverted
#: (`domain/attempt_site.py`). Renderers keep that part whole: the first
#: lines of the printed output are capped, the quote is the evidence.
ATTEMPT_QUOTE_MARK = "in the text of the attempt that printed this"


def said_lines(said: str, *, max_lines: int = 8) -> list[str]:
    """The non-blank lines of one test's printed output as a prompt shows
    them: the printed part capped at `max_lines`, then every attempt quote
    (`ATTEMPT_QUOTE_MARK`) whole. Without the split the cap cut the quote a
    few lines after the panic message it explains."""
    lines = [ln for ln in said.splitlines() if ln.strip()]
    cut = next((i for i, ln in enumerate(lines) if ATTEMPT_QUOTE_MARK in ln),
               len(lines))
    return lines[:cut][:max_lines] + lines[cut:]


def render_newly_red_outputs(
    newly_red: Iterable[str], outputs: Mapping[str, str], *,
    indent: str = "  ", max_lines: int = 8,
) -> str:
    """The per-test `said:` blocks for a regression verdict, or "" when no
    reddened test has a captured section. Appended AFTER the historical head
    so `regressed_tests_from_excerpt` (which reads the name list off the
    `REGRESSED previously-passing test(s): [...]` line) still parses."""
    names = sorted(n for n in newly_red if n)
    blocks: list[str] = []
    for name in names:
        said = outputs.get(name) or outputs.get(name.rsplit("::", 1)[-1])
        if not said:
            continue
        lines = said_lines(said, max_lines=max_lines)
        blocks.append(f"{indent}{name} said:\n"
                      + "\n".join(f"{indent}  {ln}" for ln in lines))
    if not blocks:
        return ""
    return ("\nWhat the reddened test(s) printed — the assertion that failed, "
            "with the value the body NOW produces (`left`) against what the "
            "test expects (`right`); the next body must keep producing the "
            "expected value:\n" + "\n".join(blocks))

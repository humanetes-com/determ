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

# Format marker between the genesis snapshot and the real test output in
# tests_output_N.txt. Owned here because extract_test_output_from_file parses it.
OUTPUT_SEPARATOR = "=== test output ==="


def _parse_test_counts(test_output: str) -> tuple[int, int, int]:
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
        return "fail", (
            f"cluster '{cluster_name}': {fail_count} failed, "
            f"{pass_count} passed, {ignored_count} ignored "
            f"(expected {expected_count})"
        )
    if expected_count <= 0 and pass_count == 0:
        return "incomplete", (
            f"cluster '{cluster_name}': no tests detected statically and "
            "none ran — corpus appears empty for this cluster"
        )
    if reported < expected_count:
        return "incomplete", (
            f"cluster '{cluster_name}': {reported} tests reported "
            f"({pass_count} pass + {fail_count} fail + {ignored_count} ignored), "
            f"but {expected_count} expected — some binary failed to compile "
            "or crashed before reporting all tests"
        )
    return "pass", (
        f"cluster '{cluster_name}': {pass_count} passed, "
        f"{ignored_count} ignored (expected {expected_count})"
    )


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

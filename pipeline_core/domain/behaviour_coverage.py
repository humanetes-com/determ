"""Line-coverage → exercised units, for the behaviour suite's `exercises`
field — pure domain.

`BehaviourSpec.exercises` names the units a behaviour reaches; it is the
localizer a MISMATCH escalates through, and until 2026-08-26 no writer
existed for it (`layout_pins.py` said "the caller fills it", no caller did;
30/30 specs on the anyhow suite carried it empty). The writer runs each spec
under `-C instrument-coverage`, turns the profile into per-file hit lines
with grcov, and maps those lines onto the extractor's fn records here.

The line→unit contract is `attribution.line_to_fn_record`'s: a line belongs
to the first record whose `[sig_byte_start, body_byte_end)` overlaps the
line's byte span, records already filtered to the file. That helper recomputes
a prefix sum per call, which over hundreds of hit lines × files × specs is the
wrong shape for a whole-suite pass, so this module bisects a per-file line
offset table instead — same answer, one pass. Offsets are UTF-8 BYTE offsets,
the unit the extractor (syn) reports.

Rooting: grcov names files relative to `--source-dir` (the crate root), so a
hit file is `src/error.rs`. The extractor's `file` is relative to the dir it
was pointed at — `src/error.rs` when enumerated from the crate root, bare
`error.rs` when enumerated from `src/` (the bake does the latter). A join
between the two shapes is empty and looks like "no coverage", which is a
rooting bug, not a reading (a 2026-08-25 walk did exactly this and reported
`builders: []`). :func:`normalize_record_files` puts records on the crate-root
shape before the join and says so.
"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pipeline_core.domain.behaviour import BehaviourSpec

SRC_PREFIX = "src/"


def coveralls_hit_lines(data: Mapping[str, Any]) -> dict[str, list[int]]:
    """`file name → sorted 1-based lines with a hit count > 0` from grcov's
    coveralls JSON (`source_files[].coverage` is an array indexed by line-1,
    `null` for non-coverable lines)."""
    out: dict[str, list[int]] = {}
    for sf in data.get("source_files", []) or []:
        name = str(sf.get("name", ""))
        if not name:
            continue
        cov = sf.get("coverage", []) or []
        lines = [i + 1 for i, c in enumerate(cov) if c is not None and c > 0]
        if lines:
            out[name] = lines
    return out


def normalize_record_files(
    fn_records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Records with `file` on the crate-root shape (`src/x.rs`). When NO record
    carries the `src/` prefix the set was enumerated from `src/` and every
    file is prefixed; a mixed set is left alone (it is already rooted)."""
    recs = [dict(r) for r in fn_records]
    if not recs or any(str(r.get("file", "")).startswith(SRC_PREFIX) for r in recs):
        return recs
    for r in recs:
        r["file"] = SRC_PREFIX + str(r.get("file", ""))
    return recs


def _line_byte_starts(text: str) -> list[int]:
    """Byte offset of each line start (index 0 = line 1), plus a sentinel at
    the end of the text."""
    starts = [0]
    pos = 0
    for ln in text.splitlines(keepends=True):
        pos += len(ln.encode("utf-8"))
        starts.append(pos)
    return starts


def units_for_hit_lines(
    hits: Mapping[str, Sequence[int]],
    texts: Mapping[str, str],
    fn_records: Iterable[Mapping[str, Any]],
) -> frozenset[str]:
    """`{"src/x.rs::fn_name", …}` for every fn whose body span overlaps a hit
    line. `hits`/`texts` are keyed by the crate-root file path; records are
    normalised to that shape first. A file with hits but no text or no
    records contributes nothing (never a guess)."""
    recs = normalize_record_files(fn_records)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for r in recs:
        if r.get("is_test"):
            continue
        by_file.setdefault(str(r.get("file", "")), []).append(r)
    out: set[str] = set()
    for rel, lines in hits.items():
        file_recs = by_file.get(rel)
        text = texts.get(rel)
        if not file_recs or text is None:
            continue
        starts = _line_byte_starts(text)
        n_lines = len(starts) - 1
        # (start_line, end_line) per record, 1-based inclusive: the lines whose
        # byte span overlaps [sig_byte_start, body_byte_end).
        spans: list[tuple[int, int, str]] = []
        for r in file_recs:
            s = int(r.get("sig_byte_start", 0))
            e = int(r.get("body_byte_end", 0))
            if e <= s:
                continue
            first = bisect_right(starts, s)          # line containing byte s
            last = bisect_right(starts, e - 1)       # line containing byte e-1
            spans.append((max(1, first), min(n_lines, last), str(r.get("fn_name", ""))))
        for ln in lines:
            for first, last, name in spans:
                if first <= ln <= last:
                    out.add(f"{rel}::{name}")
                    break
    return frozenset(out)


# --- which specs the exercise writer can run, and how ---------------------

def is_test_assertion_spec(spec: BehaviourSpec) -> bool:
    return spec.behaviour_id.startswith("testassert::")


def is_doctest_spec(spec: BehaviourSpec) -> bool:
    return spec.behaviour_id.startswith(("doctest::", "static::"))


def test_target_of(spec: BehaviourSpec) -> tuple[str, str] | None:
    """`(test file stem, test fn name)` from a test-assertion spec's
    `source_ref` (`tests/test_fmt.rs::test_altdebug` → `("test_fmt",
    "test_altdebug")`), else None."""
    if not is_test_assertion_spec(spec) or "::" not in spec.source_ref:
        return None
    path, _, name = spec.source_ref.rpartition("::")
    stem = path.rsplit("/", 1)[-1]
    if stem.endswith(".rs"):
        stem = stem[:-3]
    if not stem or not name:
        return None
    return stem, name


def test_command(spec: BehaviourSpec) -> list[str] | None:
    """The cargo command that runs exactly this test-assertion spec's test —
    one integration-test binary, one test by exact name — or None when the
    spec is not a test assertion."""
    target = test_target_of(spec)
    if target is None:
        return None
    stem, name = target
    return ["cargo", "test", "--quiet", "--test", stem, "--", "--exact", name]

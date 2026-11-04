#!/usr/bin/env python3
"""Which reference doc tests the re-hosted doc examples stand for, computed on
a workspace's real records, with the two negative controls.

WHY. The reference floor reported 26 of anyhow's doc tests "absent here" on
run `391506-3403a6a7` (2026-10-22) while all 26 had a re-hosted copy passing
in the oracle (`tests/doc_*_gen_N.rs`). The fix records each example's origin
at emission and joins it to the line rustdoc names the doc test by
(`pipeline_core/domain/doc_stand_in.py`). The join's claim, measured
2026-11-02 on the pristine anyhow tree: 24 of 26 sit at record line plus
fence offset, and the 2 written in a `macro_rules!` body join at one shift.
This script recomputes that claim on any workspace, before or after a
re-emission, so the claim is checked on real inputs and not described.

USAGE.
    .venv/bin/python scripts/doc_stand_in_fixture.py [tmp/anyhow] [--ref master]

It exports the pristine ref with `git archive` into a temporary directory
(never a checkout in the workspace), runs the release extractor's
`--enumerate-doc-items` and `cargo test --doc` there, and reads the
workspace's `tests/doc_*_gen_N.rs`. No GPU, no model; one cargo doc-test
build of the original (about a minute on anyhow). When the workspace holds
the emitter's origins store (`.doc_example_origins.json`) it is used as is;
otherwise origins are rebuilt from the pristine records with the emitter's
own parser and renderer, and an example counts as kept when a file in
`tests/` carries exactly its rendered part (same hash the emitter records).

HOW TO INTERPRET THE OUTPUT.
    rustdoc doc tests: N           doc tests `cargo test --doc` ran on the original
    origins: O (from store|rebuilt), kept K, macro-body M
    joined at offset: A            doc tests matched at record line + fence offset
    joined at a shift: S           macro-body examples placed by their unique shift
    stand-ins: J of N              doc tests with a kept, unchanged re-hosted file
    gaps: ...                      each doc test with no stand-in, and why
    judge, with covers: X advisory          the floor's judge on these covers, each
                                            re-hosted file in the oracle taken as passing
    control, no covers: N advisory          the same judge with no covers (the pre-fix
                                            reference: every doc test absent)
    control, no shift rule: G advisory      the same judge on the join without the
                                            macro-body rule

Decision rule: the fix is on-theory for this crate when the judge's advisory
X equals the gaps listed and stand-ins J equals N less those gaps, every gap names a reason that is true of the
crate (a `no_run`/`compile_fail` fence, an example dropped at emission), and
both controls report more advisory doc tests than the fix does. A gap reading
"no re-hosted example matches its line" for a doc that is not in a macro body
means rustdoc's line rule moved; the integration premise test in
`tests/integration/test_doc_test_count_cfg_copies.py` should then be red too.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.cargo import CargoCli  # noqa: E402
from pipeline_core.application.reconstruct.cargo_runner import run_cargo  # noqa: E402
from pipeline_core.application.reference_run import (  # noqa: E402
    _doc_lines as _reference_doc_lines,
)
from pipeline_core.domain.doc_example_test import (  # noqa: E402
    DocExample,
    doc_example_names,
    doc_example_stem,
    doc_fences,
    render_doc_example_test,
    rendered_sha,
)
from pipeline_core.domain.doc_stand_in import (  # noqa: E402
    ORIGINS_STORE,
    DocCover,
    DocOrigin,
    doc_covers,
    origins_from_json,
)
from pipeline_core.domain.models import DocItemRecord  # noqa: E402
from pipeline_core.domain.reference_floor import (  # noqa: E402
    ClusterReference,
    judge_against_reference,
)
from pipeline_core.domain.same_failure import FailedReading  # noqa: E402
from pipeline_core.domain.test_results import parse_binary_results  # noqa: E402

_EXTRACTOR = ROOT / "tools/skeleton_extractor/target/release/skeleton-extractor"


def _doc_lines(tree: Path) -> dict[tuple[str, str], tuple[str, int]]:
    """The reference measurement's own reading of rustdoc's names: the real
    runner, parser and `reference_run._doc_lines`."""
    env = dict(os.environ, CARGO_TARGET_DIR=str(tree.parent / "target"))
    _rc, out = run_cargo(CargoCli(), "cargo test --doc", tree, env, timeout=900)
    return _reference_doc_lines(parse_binary_results(out))


def _rebuilt_origins(tree: Path, present: dict[str, str]) -> list[DocOrigin]:
    data = json.loads(subprocess.run(
        [str(_EXTRACTOR), "--enumerate-doc-items", "--input", str(tree)],
        capture_output=True, text=True, check=True).stdout)
    records = [DocItemRecord.from_dict(d) for d in data]
    by_sha = {sha: name for name, sha in present.items()}
    counters: dict[str, int] = {}
    out: list[DocOrigin] = []
    for rec in sorted(records, key=lambda r: (r.file, r.line)):
        if not rec.file.startswith("src/"):
            continue
        for index, fence in enumerate(doc_fences(rec.doc)):
            where = DocOrigin(rec.file, rec.line, index, fence.offset, rec.in_macro_body)
            if not fence.runs:
                out.append(replace(where, reason=f"not re-hostable: rustdoc `{fence.skip_tag}`"))
                continue
            stem = doc_example_stem(rec.file)
            n = counters.get(stem, 0) + 1
            file_name, fn_name = doc_example_names(stem, n)
            body = render_doc_example_test(
                DocExample(fence.code, fence.should_panic, fence.offset),
                fn_name=fn_name, source=f"{rec.file} (item at line {rec.line})")
            sha = rendered_sha(body)
            if by_sha.get(sha) == file_name:
                counters[stem] = n
                out.append(replace(where, kept_as=file_name, test_fn=fn_name, sha=sha))
            else:
                out.append(replace(where, reason="no oracle file carries its rendering"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", nargs="?", default="tmp/anyhow")
    ap.add_argument("--ref", default="master")
    a = ap.parse_args()
    ws = Path(a.workspace).resolve()
    present = {p.name: rendered_sha(p.read_text(encoding="utf-8"))
               for p in sorted((ws / "tests").glob("doc_*_gen_*.rs"))}
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "pristine"
        tree.mkdir()
        archive = subprocess.run(["git", "archive", a.ref], cwd=ws,
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
        lines = _doc_lines(tree)
        store = ws / ORIGINS_STORE
        stored = (origins_from_json(json.loads(store.read_text(encoding="utf-8")))
                  if store.is_file() else None)
        origins = list(stored) if stored is not None else _rebuilt_origins(tree, present)
    source = "from store" if stored is not None else "rebuilt"
    covers = doc_covers(lines, origins, present)
    stood = {p for p, c in covers.items() if c.stand_in is not None}
    known = {(o.file, o.line) for o in origins if o.line is not None}
    at_offset = sum(1 for p in stood if lines[p] in known)
    print(f"rustdoc doc tests: {len(lines)}")
    print(f"origins: {len(origins)} ({source}), kept {sum(1 for o in origins if o.kept_as)}, "
          f"macro-body {sum(1 for o in origins if o.in_macro_body)}")
    print(f"joined at offset: {at_offset}")
    print(f"joined at a shift: {len(stood) - at_offset}")
    print(f"stand-ins: {len(stood)} of {len(lines)}")
    for p, c in sorted(covers.items()):
        if c.stand_in is None:
            print(f"  gap: {p[1]} at {lines[p][0]}:{lines[p][1]} — {c.gap}")
    # The controls go through the floor's own judge, with every re-hosted
    # file of the oracle taken as passing (the reading this script cannot
    # take; the sweep does).
    ours = frozenset(o.stand_in for o in origins
                     if o.stand_in is not None and o.kept_as in present)

    def advisory(cov: dict[tuple[str, str], DocCover]) -> int:
        ref = ClusterReference(cluster="fixture", passed=frozenset(lines),
                               announced=len(lines), reported=len(lines),
                               doc_covers=cov)
        verdict = judge_against_reference(ref, ours, FailedReading(frozenset(), {}))
        return len(verdict.advisory_unmet)

    print(f"judge, with covers: {advisory(covers)} advisory")
    print(f"control, no covers: {advisory({})} advisory")
    no_shift = doc_covers(lines, [o for o in origins if not o.in_macro_body], present)
    print(f"control, no shift rule: {advisory(no_shift)} advisory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

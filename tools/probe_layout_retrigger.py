#!/usr/bin/env python3
"""Replay the layout-retrigger decision against LIVE state, offline.

Answers, without spending a run: given a cluster's recorded layout components
and a block of rustc error text, WHICH component would `retrigger_layout` pick
and under which cause — plus whether more than one matches (the first in store
order wins, so a second match is a targeting ambiguity, not a tie).

  tools/probe_layout_retrigger.py <project_dir> <cluster> [--errors-file F]
                                  [--compile]   # cluster BUILD-failure channel

With no --errors-file the text is read from stdin. Exit 0 always; the report is
the product.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.layout_outcome_store import (  # noqa: E402
    load_layout_records,
    load_retrigger_attempts,
)
from pipeline_core.application.author_layout import LayoutOutcome  # noqa: E402
from pipeline_core.domain.layout_retrigger import (  # noqa: E402
    CompileEvidence,
    classify_compile_retrigger,
    classify_layout_retrigger,
    should_retrigger,
)
from pipeline_core.domain.starvation import classify_starvation  # noqa: E402
from pipeline_core.domain.test_results import (  # noqa: E402
    compile_error_digest,
    compile_failed_targets,
    lib_compile_clean,
)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 0
    project_dir, cluster = Path(argv[1]), argv[2]
    errors = (Path(argv[argv.index("--errors-file") + 1]).read_text()
              if "--errors-file" in argv else sys.stdin.read())

    records = load_layout_records(project_dir, cluster)
    attempts = load_retrigger_attempts(project_dir, cluster)
    starv, _ev = classify_starvation("", "", errors)
    n_err, digest = compile_error_digest(errors)
    lib_clean = lib_compile_clean(errors)
    channel = "compile" if "--compile" in argv else "fill-attempt"
    print(f"error text: {len(errors)} chars | starvation_cause={starv.name}")
    print(f"failed cargo targets: {compile_failed_targets(errors) or '(none parsed)'}"
          f" | lib_clean={lib_clean} | {n_err} distinct rustc error(s)")
    print(f"channel: {channel} | components recorded: {len(records)}"
          f" | attempts spent: {attempts or '{}'}")
    if channel == "compile":
        for ln in digest:
            print(f"    digest| {ln}")

    matches = []
    for rec in records:
        if rec.outcome is LayoutOutcome.GATED:
            print(f"  SKIP (GATED)  {sorted(rec.names)[:4]}")
            continue
        if channel == "compile":
            cause = classify_compile_retrigger(
                CompileEvidence(digest="\n".join(digest), lib_clean=lib_clean),
                rec.names)
        else:
            cause = classify_layout_retrigger(
                starvation_cause=starv, fills_exhausted=True,
                error_text=errors, private_type_names=rec.names)
        if should_retrigger(cause):
            matches.append((rec, cause))
            print(f"  MATCH {cause.name:18} {sorted(rec.names)[:6]}"
                  f" (n={len(rec.names)}, {rec.outcome.name},"
                  f" defs={len(rec.defs)}, attempts={attempts.get('|'.join(sorted(rec.names)), 0)})")

    print()
    if not matches:
        print("VERDICT: no component matches — the stall-exit retrigger fires "
              "but selects nothing (fired=False).")
    else:
        rec, cause = matches[0]
        print(f"VERDICT: would re-author {sorted(rec.names)[:6]}... "
              f"cause={cause.name}")
        if len(matches) > 1:
            print(f"AMBIGUITY: {len(matches)} components match the same text; "
                  "store order decides. Evidence does not target one component.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

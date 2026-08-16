#!/usr/bin/env python3
"""
WHY THIS EXISTS
---------------
The destruction-path evidence channel (`domain/crash_site.py`) answers one
question at a sweep: when a crash reads as a DOUBLE DROP, which of this crate's
own fns is the evidence aimed at? Getting that answer wrong is expensive and
silent. Run `1064759-0f23fb68` (2026-08-15/16) burned 17.6 GPU-hours over 7h48m
with the chain-derived evidence pack aimed at 50 fns — the construction chain of
two tests the oracle happened to name — while the crash was a payload dropped
twice and the wrong body, `src/error.rs::object_drop_front`, was never
dispatched once.

The selector is gated hermetically by
`tests/test_destruction_subjects_integration.py`, which runs the REAL extractor
over a fixture crate. What that test cannot do is tell you what the channel
would aim at on the tree ON DISK RIGHT NOW, because the crate under
reconstruction changes every run — that is the pipeline's job, and a gate
asserting on its contents would fail exactly when the loop works. So the live
reading lives here, on demand, and never in `check.sh` or `pre_run.sh`.

USAGE
-----
    python scripts/probe_destruction_subjects.py [--project tmp/anyhow]
                                                 [--cluster stable]
                                                 [--asan logs/asan]

    --project   workspace root holding `.skeleton-filled/<cluster>` (default
                tmp/anyhow)
    --cluster   cluster whose filled tree to read (default stable)
    --asan      directory of archived ASAN reports; the newest report that
                reads as a double drop is shown. Pass --asan '' to skip.

No GPU, no model calls, no cargo build: it shells the already-built extractor
(`tools/skeleton_extractor/target/release/skeleton-extractor`, from
`bash setup.sh`) and reads text files. Safe to run while a pipeline is live —
it only reads — though the tree it reports is then a moving target.

HOW TO INTERPRET THE OUTPUT
---------------------------
  fns=N                 how many fns the extractor found under the cluster
                        root. The walk includes `tests/`, deliberately: the
                        selector must be seen to EXCLUDE those.
  subjects=[...]        what a double-drop crash would aim the evidence at.
                        Each is printed with its body state:
                          STUB   — still `unimplemented!()`; an ordinary fill
                                   will reach it anyway, and the channel only
                                   changes the prompt it arrives with.
                          FILLED — a body that compiles and is not being
                                   re-visited by the frontier; this is the case
                                   the channel exists for, because nothing else
                                   in the loop will dispatch it.
  subjects=[] means one of two different things, and the line says which:
                        "no family" — the crate names no `drop`-token fn AND no
                        body holds an ownership primitive, so this crash class
                        has no answerable set here (do not read it as "the
                        selector is broken").
                        "over cap (K>12)" — the set was too wide to be a
                        subject, so it contributes NOTHING by design, on
                        `vanished_subject_fns`' precedent. If you see this on a
                        real stall, the cap is the thing to argue with, not the
                        rule.
  A subject list that contains a path outside `src/`, or a `gen_tests` sibling,
  is a BUG — no fill may rewrite those. The integration test pins it; this line
  is the live cross-check.

  reading=...           the newest archived ASAN report that reads as a double
                        drop: the freed type and the two source positions. Read
                        it beside the subjects: the report says WHAT is dropped
                        twice, the subjects say WHICH fns can be asked to stop
                        doing it. If the reading is absent and the run is still
                        crashing, the fault is a different class and this
                        channel is not the one to reach for.

The decision this feeds: on a frame-blind crash, does the loop have something
to aim at? Non-empty subjects with at least one FILLED entry means the next
frame-blind sweep will dispatch a body no other mechanism would have touched.
Empty subjects means it will fall back to the construction chain, which is the
50-fn spray this channel was built to replace.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.domain.crash_site import (  # noqa: E402
    destruction_path_subjects,
    read_destruction_fault,
)

_BIN = REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"
_STUB = re.compile(r"unimplemented!\s*\(")
_DROP_TOKEN = re.compile(r"(^|_)drop($|_)")


def _newest_double_drop(asan_dir: Path) -> tuple[str, object] | None:
    """The newest archived report that reads as a double drop, with its name."""
    if not asan_dir.is_dir():
        return None
    for p in sorted(asan_dir.glob("*.txt"), key=lambda f: f.stat().st_mtime,
                    reverse=True):
        fault = read_destruction_fault(p.read_text(errors="replace"))
        if fault is not None:
            return p.name, fault
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default="tmp/anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--asan", default="logs/asan")
    args = ap.parse_args()

    root = (REPO / args.project / ".skeleton-filled" / args.cluster).resolve()
    if not root.is_dir():
        print(f"no filled tree at {root} — nothing to probe")
        return 2
    if not _BIN.exists():
        print(f"extractor not built at {_BIN} (bash setup.sh)")
        return 2

    recs = CliExtractor(_BIN).enumerate_fns(root)
    if recs is None:
        print("the extractor returned nothing (parse failure in the tree?)")
        return 2
    dicts = [r.to_dict() for r in recs]
    subs = destruction_path_subjects(dicts)
    bodies = {(str(r.get("file")), str(r.get("fn_name"))): str(r.get("body_text") or "")
              for r in dicts}

    print(f"tree={root}")
    print(f"fns={len(dicts)}")
    if subs:
        print(f"subjects={len(subs)}")
        for loc in sorted(subs, key=lambda k: (str(k.file), k.fn_name)):
            body = bodies.get((str(loc.file), loc.fn_name), "")
            state = "STUB  " if _STUB.search(body) else "FILLED"
            flag = "" if str(loc.file).startswith("src/") else "   <-- BUG: not authorable"
            print(f"  {state}  {loc.file}::{loc.fn_name}{flag}")
    else:
        # Say WHICH empty this is — they call for opposite responses.
        wide = sum(1 for r in dicts if _DROP_TOKEN.search(str(r.get("fn_name") or "")))
        print("subjects=[] — "
              + (f"over cap ({wide} drop-named fn(s) > 12)" if wide > 12
                 else "no family (no drop-named fn, no ownership primitive)"))

    if args.asan:
        found = _newest_double_drop((REPO / args.asan).resolve())
        if found is None:
            print("reading=none (no archived report reads as a double drop)")
        else:
            name, fault = found
            print(f"reading={name}: {fault.freed_type} freed at "
                  f"{fault.free_site}, touched at {fault.access_site}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

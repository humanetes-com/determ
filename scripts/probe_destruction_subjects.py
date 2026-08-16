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

The same question has two more answers now, on the route where the localizer
DOES blame somebody, and this probe reports all three. Run `1408670-3c074ece`
(2026-08-16) blamed `src/error.rs::downcast_ref` on all five sweeps — correct as
a frame — while the fault was `object_downcast` returning
`Some(Ref::from_raw(NonNull::dangling()))`, one call away and in no stack: 6h02m,
101 fills, 20 dispatches of the impl block holding the blamed fn, zero verdict
changes, and `object_downcast` never dispatched once.

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
                                                 [--blamed src/error.rs::downcast_ref]
                                                 [--sweep logs/pipeline_anyhow.log]

    --project   workspace root holding `.skeleton-filled/<cluster>` (default
                tmp/anyhow)
    --cluster   cluster whose filled tree to read (default stable)
    --asan      directory of archived ASAN reports; the newest report that
                reads as a double drop is shown. Pass --asan '' to skip.
    --blamed    a frame-derived blame as `FILE::FN`, copied from the run log's
                `[phase-c/crash] … blamed FnLocator(file=…, fn_name=…)` line.
                Prints the one-hop callee subjects that blame would aim at.
    --sweep     a log or sweep-output file to read a PLACEHOLDER POINTER fault
                from (the rustc alignment check prints it; no sanitizer needed).

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

  sentinel=[...]        the fns that HAND OUT a fabricated pointer — a body
                        that builds `dangling()`/`null()`/`null_mut()` anywhere
                        other than as an argument to an in-place exchange
                        (`mem::replace(&mut slot, …dangling())` is the correct
                        moved-out placeholder and is excluded). These are what a
                        PLACEHOLDER POINTER crash aims at, in addition to the
                        blamed fn's callees. Same STUB/FILLED reading, same
                        cap, same "a path outside `src/` is a BUG" rule.
  callees=[...]         with `--blamed FILE::FN`, the in-crate fns that blame's
                        own body names — the producer set a frame blame cannot
                        reach on its own. Read it as a question: is the fn you
                        suspect IN it? If not, the aim will not reach it and the
                        gap is the finding.
  placeholder=...       with `--sweep FILE`, the alignment-check reading: the
                        address dereferenced, the alignment it violates, and
                        where. An address BELOW its own alignment was written as
                        a constant and is not a real allocation — that is the
                        whole discriminator, and its absence means this class is
                        not what the sweep died of.

The decision this feeds: on a crash of either kind, does the loop have something
to aim at? Non-empty subjects with at least one FILLED entry means the next
sweep will dispatch a body no other mechanism would have touched. Empty subjects
means it falls back to the construction chain (frame-blind) or to the blamed
fn's own unit (frame-derived), which are the 50-fn spray and the 20-dispatch
plateau these channels were built to replace.
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
    DestructionFault,
    callee_subjects,
    destruction_path_subjects,
    placeholder_pointer_subjects,
    read_destruction_fault,
    read_placeholder_pointer_fault,
)
from pipeline_core.domain.models import FnLocator  # noqa: E402

_BIN = REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"
_STUB = re.compile(r"unimplemented!\s*\(")
_DROP_TOKEN = re.compile(r"(^|_)drop($|_)")


def _newest_double_drop(asan_dir: Path) -> tuple[str, DestructionFault] | None:
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
    ap.add_argument("--blamed", default="",
                    help="frame-derived blame as FILE::FN")
    ap.add_argument("--sweep", default="",
                    help="log/sweep file to read a placeholder-pointer fault from")
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

    def _show(label: str, locs) -> None:
        print(f"{label}={len(locs)}")
        for loc in sorted(locs, key=lambda k: (str(k.file), k.fn_name)):
            body = bodies.get((str(loc.file), loc.fn_name), "")
            state = "STUB  " if _STUB.search(body) else "FILLED"
            flag = ("" if str(loc.file).startswith("src/")
                    else "   <-- BUG: not authorable")
            print(f"  {state}  {loc.file}::{loc.fn_name}{flag}")

    print(f"tree={root}")
    print(f"fns={len(dicts)}")
    if subs:
        _show("subjects", subs)
    else:
        # Say WHICH empty this is — they call for opposite responses.
        wide = sum(1 for r in dicts if _DROP_TOKEN.search(str(r.get("fn_name") or "")))
        print("subjects=[] — "
              + (f"over cap ({wide} drop-named fn(s) > 12)" if wide > 12
                 else "no family (no drop-named fn, no ownership primitive)"))

    sentinels = placeholder_pointer_subjects(dicts)
    if sentinels:
        _show("sentinel", sentinels)
    else:
        print("sentinel=[] — no body hands out a fabricated pointer (or the "
              "set was over cap)")

    if args.blamed:
        file_rel, _, fn_name = args.blamed.partition("::")
        callees = callee_subjects(FnLocator(file_rel, fn_name), dicts)
        if callees:
            _show("callees", callees)
        else:
            print(f"callees=[] — `{args.blamed}` names no in-crate fn the "
                  "records know, or the set was over cap. If the fn you "
                  "suspect is not listed, the aim will not reach it.")

    if args.sweep:
        _sw = (REPO / args.sweep).resolve()
        _ph = (read_placeholder_pointer_fault(_sw.read_text(errors="replace"))
               if _sw.is_file() else None)
        print(f"placeholder={'none' if _ph is None else _ph}")

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

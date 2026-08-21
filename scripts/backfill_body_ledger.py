#!/usr/bin/env python3
"""Index the bodies already on disk into the proven-body ledger.

WHY THIS EXISTS
---------------
The pipeline has never recorded which body was proven correct — the only
cross-run store, `.phase_c_logs/retrospectives.json`, holds 39 records over 19
fields and every one is a negative claim. The bodies themselves were never
lost: `tmp/<project>.pre_restart/` holds archived reconstructions (2026-08-14
and 2026-08-16 carry both `.skeleton` and `.skeleton-filled`, 12 skeleton files
at 144 stubs against 46 filled files at 26 and 101 stubs), `.best_tree/<cluster>`
carries a filled tree WITH the layout it answers to in `_layout/`, and the live
tree is right there. What was missing was a per-unit index over them, so that a
destruction could be undone by addressing a body instead of re-deriving it
through the model. This script builds that index for what already exists; the
loop's own write path handles everything from here on.

PAIRING IS THE WHOLE RULE. t1.5 re-authors the layout on every run, so bodies
from one run do not compile against another's skeleton — between `1f4099b` and
the tree that replaced it, `ErrorImpl` lost `pub(crate) backtrace`. A source
that does not carry its own layout therefore cannot be indexed at all, and
`tmp/anyhow.pre_restart/20260812T133117Z` (a bare `.best_tree` in the old
single-slot format, no `_layout`, no `.skeleton`) is skipped for exactly that
reason rather than guessed at.

USAGE
-----
    python scripts/backfill_body_ledger.py [--project anyhow]
        [--workspace tmp/anyhow] [--apply] [--json OUT.json]

Dry run by default: it prints what it would index and writes nothing. `--apply`
appends to `logs/body_ledger/<project>/index.jsonl`. No GPU, no cargo, no model
call; it reads trees and writes one JSONL file OUTSIDE the workspace (a
`git reset --hard` in the workspace must not be able to reach it).

Safe to run while the pipeline is live: the ledger is append-only and every row
this script writes enters as `landed`, which no read path will re-seed.

HOW TO INTERPRET THE OUTPUT
---------------------------
One line per source, then a total:

  units          how many filled units it indexed (a unit is `(file, fn)` with
                 ALL of its occurrences — `src/context.rs::source` is two impls
                 and the re-seed restores the pair or neither).
  files          how many source files those units came from.
  layout fps     how many DISTINCT layout fingerprints the source's skeleton
                 produced across those files. One per file is the healthy
                 reading (each file has its own digest); the number to compare
                 across sources is whether the SAME file has the same digest in
                 two sources — printed as `shared with live: N/M files`, and
                 that N is how many of the source's files can ever be re-seeded
                 into today's tree without the per-item fallback.
  skipped        units whose bodies were all `unimplemented!()` — nothing to
                 index, not a failure.

Everything enters as `landed`, never `proven` and never `unmeasured`:
promotion requires a verdict vector measured on THIS code, and a body from a
run whose sweep nobody recorded has not been proven, it has only been written.
The consequence to expect, stated so it is not read as a defect: a backfilled
row is not re-seedable until a sweep of the run that adopts it has classified
it. The backfill's value arrives one sweep later than the live write path's.

Backfilled rows also carry NO layout dependency list — the archives predate the
registry that names the crate's authored items — so the gate's per-item
fallback has nothing to check and a moved file fingerprint refuses them
outright. That is the conservative direction: a refusal costs a re-derivation,
a bad splice costs a tree that does not build with no per-unit signal to aim at.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.adapters.body_ledger_store import (
    append_entries,
    default_root,
)
from pipeline_core.domain.body_ledger import (
    BodyState,
    LedgerEntry,
    LedgerKey,
    authored_layout_fp,
    body_sha,
    sig_sha_of,
)
from pipeline_core.domain.rust_fn_headers import locate_all_fns
from pipeline_core.domain.rust_scan import declared_fn_names

_STUB_MARKERS = ("unimplemented!()", "todo!()")


@dataclass(frozen=True, slots=True)
class Source:
    """One (layout, bodies) PAIR. Never one without the other."""

    label: str
    cluster: str
    skeleton_root: Path      # crate root holding src/ — the authored layout
    filled_root: Path        # crate root holding src/ — the bodies


def _is_stub(body: str) -> bool:
    return any(m in body for m in _STUB_MARKERS)


def _rs_files(root: Path) -> list[tuple[str, Path]]:
    src = root / "src"
    try:
        return [(str(p.relative_to(root)), p) for p in sorted(src.rglob("*.rs"))]
    except OSError:
        return []


def discover(workspace: Path, project: str) -> Iterator[Source]:
    """Every source on disk that carries BOTH halves, best-provenance first."""
    # 1. The archived reconstructions, each with its own skeleton. Two shapes
    #    on disk: `<stamp>/skeleton/<cluster>/src` (a whole-workspace archive)
    #    and `<stamp>/skeleton/src` (a single-cluster restore, whose cluster is
    #    in the stamp name — `20260814T120718Z_restore_stable`). A stamp whose
    #    cluster cannot be read is skipped rather than assumed: a body indexed
    #    under the wrong cluster would be offered to a tree built under a
    #    different cfg world.
    archives = workspace.parent / f"{workspace.name}.pre_restart"
    for stamp in sorted(archives.glob("*")) if archives.is_dir() else []:
        skel, filled = stamp / "skeleton", stamp / "skeleton-filled"
        if not (skel.is_dir() and filled.is_dir()):
            continue                      # a bare `.best_tree`: unpairable
        if (filled / "src").is_dir():
            _, _, cluster = stamp.name.partition("_restore_")
            if not cluster:
                continue
            yield Source(f"archive {stamp.name}", cluster, skel, filled)
            continue
        for cluster_dir in sorted(filled.glob("*")):
            paired = skel / cluster_dir.name
            if not (paired / "src").is_dir() or not (cluster_dir / "src").is_dir():
                continue
            yield Source(f"archive {stamp.name}", cluster_dir.name, paired,
                         cluster_dir)
    # 2. The durable best tree, which stores its own layout beside it.
    best = workspace / ".best_tree"
    for cluster_dir in sorted(best.glob("*")) if best.is_dir() else []:
        layout = cluster_dir / "_layout"
        if (layout / "src").is_dir() and (cluster_dir / "src").is_dir():
            yield Source(f"best_tree {cluster_dir.name}", cluster_dir.name,
                         layout, cluster_dir)
    # 3. The live tree.
    filled = workspace / ".skeleton-filled"
    for cluster_dir in sorted(filled.glob("*")) if filled.is_dir() else []:
        paired = workspace / ".skeleton" / cluster_dir.name
        if (paired / "src").is_dir() and (cluster_dir / "src").is_dir():
            yield Source(f"live {cluster_dir.name}", cluster_dir.name, paired,
                         cluster_dir)


def index_source(src: Source, project: str) -> tuple[list[LedgerEntry], int]:
    """Every filled unit in one source, as `landed` rows, plus the stub count."""
    fps = {rel: authored_layout_fp(p.read_text(encoding="utf-8",
                                               errors="replace"))
           for rel, p in _rs_files(src.skeleton_root)}
    rows: list[LedgerEntry] = []
    stubbed = 0
    for rel, path in _rs_files(src.filled_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for fn_name in sorted(declared_fn_names(text)):
            locs = locate_all_fns(text, fn_name)
            if not locs:
                continue
            bodies = tuple(text[b:e] for _s, _ss, b, e in locs)
            sigs = tuple(sig for sig, _ss, _b, _e in locs)
            if all(_is_stub(b) for b in bodies):
                stubbed += 1
                continue
            fp = fps.get(rel, "")
            if not fp:
                # The body has no layout to answer to in THIS source. Skipping
                # rather than stamping an empty fingerprint: an entry whose
                # gate carries no information re-seeds unconditionally, which
                # is the failure mode this whole store is built to avoid.
                continue
            rows.append(LedgerEntry(
                key=LedgerKey(project=project, cluster=src.cluster, file=rel,
                              fn_name=fn_name),
                body_sha=body_sha(bodies), bodies=bodies,
                sig_sha=sig_sha_of(sigs), layout_fp=fp,
                state=BodyState.LANDED, source=src.label))
    return rows, stubbed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--workspace", default=None,
                    help="default: tmp/<project>")
    ap.add_argument("--apply", action="store_true",
                    help="write rows (default: dry run)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    root = default_root()
    workspace = Path(args.workspace) if args.workspace \
        else root / "tmp" / args.project
    if not workspace.is_dir():
        print(f"no workspace at {workspace}")
        return 1

    # The live layout first, so every other source can be reported against it:
    # the number that matters is not how many fingerprints a source has, it is
    # how many of its files still fingerprint the way today's tree does.
    # Keyed by (cluster, file), never by file alone: `src/error.rs` exists in
    # both clusters with different cfg worlds and different content, and
    # collapsing them would report a nightly file as matching a stable one.
    live_fps: dict[tuple[str, str], str] = {}
    for cluster_dir in sorted((workspace / ".skeleton").glob("*")):
        if (cluster_dir / "src").is_dir():
            live_fps.update({
                (cluster_dir.name, rel): authored_layout_fp(
                    p.read_text(encoding="utf-8", errors="replace"))
                for rel, p in _rs_files(cluster_dir)})
    report: list[dict[str, object]] = []
    total: list[LedgerEntry] = []
    print(f"backfill — project={args.project} workspace={workspace} "
          f"ledger={root / 'logs' / 'body_ledger' / args.project}"
          + ("" if args.apply else "   (DRY RUN)"))
    for source in discover(workspace, args.project):
        rows, stubbed = index_source(source, args.project)
        fps = {r.key.file: r.layout_fp for r in rows}
        shared = sum(1 for f, fp in fps.items()
                     if live_fps.get((source.cluster, f)) == fp)
        print(f"  {source.label:34} cluster={source.cluster:8} "
              f"units={len(rows):4} files={len({r.key.file for r in rows}):3} "
              f"layout fps={len(set(fps.values())):3} "
              f"stub-only units={stubbed:4} "
              f"shared with live: {shared}/{len(fps)} files")
        report.append({"source": source.label, "cluster": source.cluster,
                       "units": len(rows), "stub_only": stubbed,
                       "shared_with_live": shared, "files": len(fps)})
        total.extend(rows)

    print(f"  TOTAL {len(total)} row(s) over "
          f"{len({(r.key.cluster, r.key.file, r.key.fn_name) for r in total})} "
          "distinct unit(s), all state=landed")
    if args.apply:
        written = append_entries(root, args.project, total)
        print(f"  wrote {written} row(s)")
    else:
        print("  dry run — pass --apply to write")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2),
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

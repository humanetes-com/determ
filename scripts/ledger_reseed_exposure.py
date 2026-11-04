#!/usr/bin/env python3
"""How much of a tree would a cluster-entry re-seed put back from the body ledger?

WHY THIS EXISTS
---------------
The body ledger lives OUTSIDE the workspace (`logs/body_ledger/<project>/`,
`adapters/body_ledger_store.py:default_root`) precisely so that no workspace
destruction — `--clean`, `redo.sh`, the augmented `git reset --hard` — can reach
it. The other half of that design is `_reseed_site` (`phase_c_tdd.py`, cluster
entry): it asks the ledger about EVERY current stub, and on a freshly built tree
every unit is a stub. `reseed_allowed` (`domain/body_ledger.py`) admits a
PROVEN or UNMEASURED row when the unit's file fingerprint matches the skeleton
or every `layout_dep` is still defined verbatim.

Until 2026-08-26 it checked LAYOUT identity and never ORACLE identity (which
tests judged the body). So a "cold" run that re-authors the layout was not cold
for any file t1.5 leaves byte-identical: those bodies came back from the
previous tree, judged under the previous gen_tests. Measured on the anyhow
ledger before the planned cold run: 568 stable rows → 85 latest units, 62 PROVEN
+ 23 UNMEASURED; against the live `.skeleton/stable` the gate would TAKE 73 (12
`signature moved`), all 10 files' fingerprints MATCHED, and `rebuy_hits` fired
for `src/context.rs::context`, `::ext_context`, `src/kind.rs::new` on any fresh
run id whatever the layout. The plan that priced that run as "every one of the
~200 bodies is re-derived" had not looked.

The fix is `LedgerEntry.oracle_fp` — the fingerprint of the oracle that graded
the row (`adapters/oracle_fingerprint.py`: the tree's gen_tests siblings, the
crate's `tests/`, the baked behaviour suite) — which `reseed_allowed` and
`rebuy_hits` now require to match. Every row written before the field carries
"" and is refused once. This script computes the exposure with the loop's own
functions (`latest_by_unit`, `plan_reseed`, `reseed_allowed`, `rebuy_hits`)
under the real oracle, and `--ignore-oracle` reproduces the layout-only
exposure — the negative control, and the number the old gate would have acted
on. It runs no cargo, calls no model, writes nothing.

USAGE
-----
    .venv/bin/python scripts/ledger_reseed_exposure.py \
        [--project anyhow] [--cluster stable] \
        [--skeleton-root tmp/anyhow/.skeleton/stable] \
        [--src-dir tmp/anyhow/.skeleton-filled/stable] \
        [--project-dir tmp/anyhow] \
        [--ledger-root .] [--run-id cold-run-probe] \
        [--ignore-oracle] [--verbose]

  --skeleton-root  the AUTHORED layout crate the gate compares against (the
                   crate root, not its src/ — ledger files are `src/x.rs`).
                   Default: the live `.skeleton/<cluster>`. Point it at a
                   re-authored skeleton to project a cold run's re-seed BEFORE
                   task2a starts (the pipeline commits after the bake, so the
                   new skeleton is on disk before the first dispatch).
  --src-dir        the cluster crate root: where current SIGNATURES are read
                   from AND whose gen_tests siblings + tests/ define the live
                   oracle. Point it at the tree whose tests will score the run.
                   When absent the signature condition is skipped (TAKE is an
                   UPPER bound) and the oracle is computed from --project-dir's
                   tests/ alone.
  --project-dir    the workspace (`tests/` fallback and
                   `.skeleton/_behaviours_<cluster>.json`). Default tmp/<project>.
  --ledger-root    repo root holding logs/body_ledger. Point it at an empty
                   directory for the negative control (expect TAKE 0).
  --run-id         a fresh run id for the `rebuy_hits` probe (any string not in
                   the ledger stands for "the next process").
  --ignore-oracle  stamp every row with the live oracle fingerprint before the
                   walk, so the oracle condition is vacuous and the gate reduces
                   to the pre-2026-08-26 layout-only rule. The same functions
                   run; only the premise changes. This is the negative control.

HOW TO INTERPRET THE OUTPUT
---------------------------
  rows / latest units      raw index size, and the per-unit stand after
                           `latest_by_unit` (last row wins; PROVEN outranks a
                           later non-PROVEN of a DIFFERENT body; a later row
                           about the SAME body always wins, so a `flat` row
                           supersedes the `proven` one it repeats).
  latest by oracle         how many latest rows carry each oracle fingerprint.
                           "" is a row written before the field existed — the
                           gate refuses it once, until the next process
                           re-lands or re-judges the body under a recorded one.
  oracle <fp> over N       the live oracle: how many files the fingerprint
                           digested, by kind. `0 gen_tests` on a tree that has
                           siblings is a ROOTING BUG, not a no-op — the script
                           says so and exits 2.
  reseedable latest        PROVEN + UNMEASURED units — the pool the gate reads.
                           LANDED/REFUTED/FLAT never re-seed.
  gate walk: TAKE n        units `plan_reseed` would splice onto a tree built
                           from --skeleton-root and scored by the live oracle.
                           THIS is the number a cold-run reading must subtract
                           from "re-derived".
  skipped: <reason> k      why the rest were refused (`oracle moved`, `layout
                           moved`, `signature moved`, `state …`, `gate …`).
                           `oracle moved` is the intended refusal on a tree
                           whose gen_tests were regenerated, and on every
                           pre-field row; `layout moved` against a re-authored
                           skeleton likewise.
  fingerprint match        per file, whether the skeleton's current LAYOUT
                           fingerprint equals the one on the unit's latest row —
                           the fast path of the layout condition.
  RE-BUY probe             what `rebuy_hits` would print on the next process's
                           first dispatch of these units: graded prior rows with
                           an empty verdict delta under the SAME oracle. With
                           --ignore-oracle it has no oracle check, so a hit
                           there on a regenerated tree cites a body measured by
                           tests that no longer exist.

  Decision rule: before a launch whose reading is "did the loop reconstruct
  from the new spec", TAKE under the real oracle must be 0, or the reading must
  carry "reseeded TAKE / re-derived rest" explicitly. TAKE under
  --ignore-oracle minus TAKE under the real oracle is what the oracle clause
  bought; if the two are equal on a tree whose gen_tests moved, the adapter is
  not reading the tree the run scores (check the `oracle … over N` line).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline_core.adapters.body_ledger_store import load_entries  # noqa: E402
from pipeline_core.adapters.fn_reseed import current_signatures  # noqa: E402
from pipeline_core.adapters.oracle_fingerprint import (  # noqa: E402
    cluster_oracle_files,
)
from pipeline_core.application.reconstruct.body_reseed import (  # noqa: E402
    definition_checker,
    skeleton_fingerprints,
)
from pipeline_core.domain.body_ledger import (  # noqa: E402
    latest_by_unit,
    oracle_fingerprint,
    plan_reseed,
    rebuy_hits,
    sig_sha_of,
)
from pipeline_core.domain.models import FnLocator  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--skeleton-root", type=Path, default=None)
    ap.add_argument("--src-dir", type=Path, default=None)
    ap.add_argument("--project-dir", type=Path, default=None)
    ap.add_argument("--ledger-root", type=Path, default=REPO)
    ap.add_argument("--run-id", default="cold-run-probe")
    ap.add_argument("--ignore-oracle", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    project_dir = a.project_dir or (REPO / "tmp" / a.project)
    skeleton_root = a.skeleton_root or (project_dir / ".skeleton" / a.cluster)
    src_dir = a.src_dir or (project_dir / ".skeleton-filled" / a.cluster)

    # The live oracle, from the same adapter the driver's dep calls.
    files = cluster_oracle_files(src_dir, project_dir, a.cluster)
    live_fp = oracle_fingerprint(files)
    kinds = Counter(
        "gen_tests" if rel.startswith("src/") else
        "tests/" if rel.startswith("tests/") else "behaviours"
        for rel, _t in files)
    print(f"oracle {live_fp} over {len(files)} file(s): "
          + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items()))
          + f"  (src_dir {src_dir}, project_dir {project_dir})")
    has_siblings = (src_dir / "src").is_dir() and any(
        ".gen_tests_" in p.name for p in (src_dir / "src").glob("*.rs"))
    if has_siblings and not kinds.get("gen_tests"):
        print("  the tree HAS gen_tests siblings and the oracle digested none — "
              "rooting bug, not a no-op")
        return 2

    entries = load_entries(a.ledger_root, a.project, cluster=a.cluster)
    if a.ignore_oracle:
        # The negative control: every row is stamped with the live oracle, so
        # the oracle clause cannot refuse and the walk is the layout-only gate
        # the loop ran before the field existed. Same functions, changed premise.
        entries = [replace(e, oracle_fp=live_fp) for e in entries]
        print("  --ignore-oracle: every row stamped with the live oracle "
              "(layout-only gate, the pre-fix rule)")
    stand = latest_by_unit(entries)
    print(f"ledger {a.ledger_root / 'logs/body_ledger' / a.project}: "
          f"{len(entries)} rows (cluster {a.cluster}) → {len(stand)} latest units")
    print("  all rows by state:   " + ", ".join(
        f"{s} {n}" for s, n in sorted(Counter(str(e.state) for e in entries).items())))
    print("  latest by state:     " + ", ".join(
        f"{s} {n}" for s, n in sorted(Counter(str(e.state) for e in stand.values()).items())))
    print("  latest by oracle:    " + ", ".join(
        f"{o or '(none)'} {n}"
        for o, n in sorted(Counter(e.oracle_fp for e in stand.values()).items())))
    pool = {k: e for k, e in stand.items() if e.reseedable}
    print(f"  reseedable latest:   {len(pool)} "
          f"(proven {sum(1 for e in pool.values() if str(e.state) == 'proven')}, "
          f"unmeasured {sum(1 for e in pool.values() if str(e.state) == 'unmeasured')})")
    by_file: dict[str, Counter[str]] = defaultdict(Counter)
    for k, e in pool.items():
        by_file[k.file][str(e.state)] += 1
    for f in sorted(by_file):
        print(f"    {f}: " + ", ".join(f"{s} {n}" for s, n in sorted(by_file[f].items())))

    if not (skeleton_root / "src").is_dir():
        print(f"\nno skeleton at {skeleton_root} — gate walk skipped (pass --skeleton-root)")
        return 0
    file_fp = skeleton_fingerprints(skeleton_root)
    if not file_fp:
        print(f"\nskeleton_fingerprints({skeleton_root}) is EMPTY — rooting bug, not a no-op")
        return 2
    sig: dict = {}
    sig_note = "signature condition SKIPPED (upper bound)"
    if (src_dir / "src").is_dir():
        for k in pool:
            sigs = current_signatures(FnLocator(k.file, k.fn_name), src_dir)
            if sigs:
                sig[k] = sig_sha_of(sigs)
        sig_note = f"signatures read from {src_dir} for {len(sig)}/{len(pool)} units"
    plan = plan_reseed(entries, wanted=list(pool.keys()), file_fp=file_fp,
                       sig_sha=sig, oracle_fp=live_fp,
                       definition_present=definition_checker(skeleton_root))
    take = plan.take
    print(f"\ngate walk against {skeleton_root} ({sig_note}; oracle {live_fp}"
          f"{', IGNORED' if a.ignore_oracle else ''}):")
    print(f"  TAKE {len(take)} unit(s) would re-seed: "
          f"proven {sum(1 for e in take if str(e.state) == 'proven')}, "
          f"unmeasured {sum(1 for e in take if str(e.state) == 'unmeasured')}")
    take_by_file = Counter(e.key.file for e in take)
    for f in sorted(take_by_file):
        print(f"    {f}: {take_by_file[f]}")
    reasons = Counter(why for _, why in plan.skipped)
    for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  skipped: {why} {n}")
    print("  fingerprint match (skeleton layout now == unit's latest row):")
    for f in sorted(by_file):
        fps = {e.layout_fp for k, e in pool.items() if k.file == f}
        cur = file_fp.get(f, "<absent>")
        print(f"    {f}: {'MATCH' if cur in fps else 'moved'} (skeleton {cur}, rows {sorted(fps)})")
    if a.verbose:
        for e in take:
            print(f"    take {e.key.file}::{e.key.fn_name} {e.state} run={e.run_id or '-'} "
                  f"src={e.source} oracle={e.oracle_fp or '(none)'}")

    hits = rebuy_hits([FnLocator(k.file, k.fn_name) for k in stand], stand,
                      project=a.project, cluster=a.cluster, run_id=a.run_id,
                      oracle_fp=live_fp)
    print(f"\nRE-BUY probe (run_id={a.run_id!r}, oracle {live_fp}"
          f"{', IGNORED' if a.ignore_oracle else ''}): {len(hits)} unit(s) "
          "would print RE-BUY on first dispatch")
    for loc, e in hits:
        print(f"    {loc.file}::{loc.fn_name} {e.state} prior_run={e.run_id} "
              f"layout_fp={e.layout_fp} oracle={e.oracle_fp or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

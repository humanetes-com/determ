#!/usr/bin/env bash
# The remaining-stub count, counted the way the pipeline counts it.
#
# `grep -c 'unimplemented!()'` over a filled tree is wrong by a constant: this
# crate's rustdoc examples contain a literal `unimplemented!()` inside `///` and
# `//!` blocks, twelve lines per tree. That is the whole gap between the "20
# stubs left" / "21 stubs left" figures carried in past handover state tables
# and the real single-digit numbers. The pipeline's own scan was never affected
# — it reads the syn extractor's fn records and only sees real bodies — so this
# is a defect in the HUMAN check, and the fix is to give the human check the
# same source of truth.
#
# Reports per cluster tree:
#   real     — fn records whose BODY is a stub, cfg-scoped to that cluster
#   gated    — stubs that exist but are compiled out of this cluster; they can
#              never fill here, and counting them freezes any percentage below
#              100 % by design
#   grep     — the naive count, for contrast
set -u
cd "$(dirname "$0")/.." || exit 2
.venv/bin/python - <<'PY'
import json
import pathlib
import sys

sys.path.insert(0, ".")
from pipeline_core.adapters.extractor import CliExtractor
from pipeline_core.domain.cfg import CfgSet

BIN = pathlib.Path("tools/skeleton_extractor/target/release/skeleton-extractor")
ROOT = pathlib.Path("tmp/anyhow")

clusters = {}
manifest = ROOT / ".skeleton" / "_clusters.json"
if manifest.exists():
    for c in json.loads(manifest.read_text(encoding="utf-8")).get("clusters", []):
        clusters[c["name"]] = (tuple(c.get("cfg_true") or ()),
                               tuple(c.get("cfg_false") or ()))

rc = 0
for name, (cfg_true, cfg_false) in clusters.items() or {"stable": ((), ())}.items():
    src = ROOT / ".skeleton-filled" / name / "src"
    if not src.exists():
        print(f"  {name}: MISSING {src}")
        rc = 1
        continue
    recs = CliExtractor(BIN).enumerate_fns(src)
    if recs is None:
        print(f"  {name}: extractor unavailable ({BIN})")
        rc = 2
        continue
    cfgs = CfgSet.of(cfg_true, cfg_false)
    real = gated = 0
    names = []
    for r in recs:
        if not r.has_unimplemented_body:
            continue
        # A stub whose own cfg predicates are FALSE under this cluster is
        # compiled out here: it is not work this cluster can ever do.
        preds = tuple(r.cfg_predicates or ())
        if any(cfgs.evaluate(p) is False for p in preds):
            gated += 1
            continue
        real += 1
        names.append(f"{r.file}::{r.fn_name}")
    grep = sum(
        1 for p in src.rglob("*.rs")
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
        if "unimplemented!()" in ln)
    print(f"  {name}: {real} real stub(s), {gated} cfg-gated to other cluster(s) "
          f"[grep says {grep} — the difference is rustdoc examples]")
    for n in sorted(names):
        print(f"      {n}")
sys.exit(rc)
PY

#!/usr/bin/env python3
"""Read a workspace's last coverage measurement against the per-file floor.

WHY THIS EXISTS
  task0c's line stop used to compare one crate-wide percent with
  COVERAGE_AUGMENTATION_TARGET, so a small file could sit far under it inside a
  module that passed (anyhow run 44032-0bf107ac: 97.5% total, src/ptr.rs
  89.8%, src/backtrace.rs 0%). The per-file floor (COVERAGE_FILE_FLOOR) reads
  each file over the lines a test can reach. This script prints, before any
  run spends GPU on it, what that floor would decide on a workspace already on
  disk: which files it would hold, which it would name below the floor as
  accounted, and what the reachable denominator removed. It uses the
  pipeline's own definitions (`task0c.split_uncovered`, `judge_files`), so its
  verdicts are the ones the stop would reach.

USAGE
  .venv/bin/python scripts/measure_file_floor.py [WORKSPACE] [--floor N]
                                                 [--module NAME] [--ref REF]

  WORKSPACE  the crate workspace (default tmp/anyhow).
  --floor    the floor in percent (default: COVERAGE_FILE_FLOOR, else
             COVERAGE_AUGMENTATION_TARGET, else 90).
  --module   the module name (default: the workspace directory's name).
  --ref      the git ref whose sources the lcov was measured on (default:
             tag task0c-augmented-<module> when it exists, else the working
             tree). After the strip the working tree holds stubs, so the
             default matters: line numbers and function spans must come from
             the tree the coverage run saw.

  --pragma   the ignore pragma (default `// GRCOV_EXCL_LINE`); its lines are
             dropped as the coverage provider's parser drops them.

  Inputs read: <WORKSPACE>/target/coverage/llvmcov_<module>.lcov (the LAST
  measurement on disk, whichever cluster took it) and every
  <WORKSPACE>/.cluster_augmented/<cluster>__<module>.fns.json (the proved and
  the budget-spent functions). Each cluster's record is judged against that
  one lcov; when the last measurement was another cluster's, its rows are a
  reading of that tree under this cluster's verdicts, and the header says so.
  Function spans come from the skeleton extractor run over the ref's sources
  in a scratch copy. The cfg-compiled-out filter needs the cluster plan and is
  not applied here; a held line that only another cluster compiles shows up as
  held, never the reverse.

HOW TO INTERPRET
  One row per module source file:
    src/ptr.rs  raw 89.8% (79/88)  reachable 100.0% (79/79)  met
    src/wrapper.rs  raw 87.5% (21/24)  reachable 87.5% (21/24)  accounted: attempt budget spent on ...
  `met` and `vacuous` (no reachable line) never hold. `accounted` is below the
  floor with every open line in a function whose attempt budget is spent:
  named, never re-attempted. `held` is what the stop would keep the module
  open for; its open lines follow, and they are the lines a generation prompt
  would be aimed at next.

  Exit 0 normally. Exit 1 when a held file's open lines all lie inside a
  proved function: the exclusion is broken, and the floor would stall on code
  no test can reach. Exit 2 when an input is missing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.application.tasks.task0c import split_uncovered  # noqa: E402
from pipeline_core.domain.attribution import canon_site, record_site  # noqa: E402
from pipeline_core.domain.coverage_annotation import (  # noqa: E402
    filter_cfg_excluded_lines,
    filter_cfg_incompatible_lines,
)
from pipeline_core.domain.file_coverage_floor import (  # noqa: E402
    FILE_HELD,
    judge_files,
    lines_in,
)
from pipeline_core.domain.fn_execution import FnCoverageRecord, FnTarget  # noqa: E402
from pipeline_core.domain.generation import strip_all_gen_tests_decls  # noqa: E402
from pipeline_core.domain.models import FnSite  # noqa: E402
from pipeline_core.domain.rust_scan import is_structural_brace_line  # noqa: E402
from pipeline_core.domain.workspace_layout import CLUSTER_AUGMENTED_DIR  # noqa: E402

EXTRACTOR = ROOT / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"


def _default_floor() -> float:
    for var in ("COVERAGE_FILE_FLOOR", "COVERAGE_AUGMENTATION_TARGET"):
        raw = (os.getenv(var) or "").strip()
        if raw:
            return float(raw)
    return 90.0


def _git(ws: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True)


def _source(ws: Path, ref: str, rel: str) -> str:
    if not ref:
        return (ws / rel).read_text(encoding="utf-8")
    r = _git(ws, "show", f"{ref}:{rel}")
    return r.stdout if r.returncode == 0 else ""


def _read_lcov(path: Path, ws: Path) -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    cur: dict[int, int] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("SF:"):
            sf = Path(line[3:])
            rel = str(sf.relative_to(ws)) if sf.is_absolute() and sf.is_relative_to(ws) else line[3:]
            cur = out.setdefault(rel, {})
        elif line.startswith("DA:") and cur is not None:
            n, c = line[3:].split(",")[:2]
            cur[int(n)] = max(cur.get(int(n), 0), int(c))
    return out


def _spans(ws: Path, ref: str, rels: list[str]) -> dict[FnSite, tuple[int, int]]:
    """Every fn record's (first, last) line, keyed by canonical site, over the
    ref's sources in a scratch copy."""
    with tempfile.TemporaryDirectory(prefix="file_floor_") as tmp:
        root = Path(tmp)
        texts: dict[str, str] = {}
        for rel in rels:
            text = _source(ws, ref, rel)
            texts[rel] = text
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        recs = CliExtractor(EXTRACTOR).enumerate_fns(root) or []
    out: dict[FnSite, tuple[int, int]] = {}
    for rec in recs:
        text = texts.get(rec.file, "")
        first = text.count("\n", 0, rec.sig_byte_start) + 1
        last = text.count("\n", 0, rec.body_byte_end) + 1
        site = record_site(rec)
        lo, hi = out.get(site, (first, last))
        out[site] = (min(lo, first), max(hi, last))
    return out


def _targets(sites: list[FnSite], spans: dict[FnSite, tuple[int, int]],
             missing: list[str]) -> list[FnTarget]:
    out = []
    for s in sites:
        span = spans.get(canon_site(s))
        if span is None:
            missing.append(f"{s.file}::{s.fn_name} [{s.container}]")
            continue
        out.append(FnTarget(s, span[0], span[1], False))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace", nargs="?", default=str(ROOT / "tmp" / "anyhow"))
    ap.add_argument("--floor", type=float, default=_default_floor())
    ap.add_argument("--module", default="")
    ap.add_argument("--ref", default=None)
    ap.add_argument("--pragma", default="// GRCOV_EXCL_LINE",
                    help="the ignore pragma the coverage parser drops lines for "
                         "(llvm-cov/grcov default; tarpaulin: '// # tarpaulin coverage ignore')")
    a = ap.parse_args()
    ws = Path(a.workspace).resolve()
    module = a.module or ws.name
    lcov = ws / "target" / "coverage" / f"llvmcov_{module}.lcov"
    if not lcov.exists():
        print(f"no lcov at {lcov}", file=sys.stderr)
        return 2
    ref = a.ref
    if ref is None:
        tag = f"task0c-augmented-{module}"
        ref = tag if _git(ws, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode == 0 else ""
    records = sorted((ws / CLUSTER_AUGMENTED_DIR).glob(f"*__{module}.fns.json"))
    if not records:
        print(f"no function records under {ws / CLUSTER_AUGMENTED_DIR}", file=sys.stderr)
        return 2
    lines = {rel: hits for rel, hits in _read_lcov(lcov, ws).items()
             if rel.startswith("src/") and Path(rel).name != "build.rs"}
    # The provider's parser drops every line carrying the ignore pragma before
    # anything reads the measurement (`LlvmCovProvider._parse_lcov`); so does this.
    for rel, hits in lines.items():
        src = _source(ws, ref, rel).splitlines()
        for n in [n for n in hits if n <= len(src) and a.pragma in src[n - 1]]:
            del hits[n]
    spans = _spans(ws, ref, sorted(lines))
    print(f"lcov {lcov} (mtime {lcov.stat().st_mtime:.0f}), sources at "
          f"{ref or 'the working tree'}, floor {a.floor:g}%")
    broken = False
    for path in records:
        cluster = path.name[: -len(f"__{module}.fns.json")]
        rec = FnCoverageRecord.from_json(path.read_text(encoding="utf-8"))
        if rec is None:
            print(f"\n[{cluster}] record unreadable or under another criterion: {path}")
            continue
        missing: list[str] = []
        proved = _targets([p.site for p in rec.by_construction], spans, missing)
        spent = _targets([u.site for u in rec.unreached], spans, missing)
        not_actionable: dict[str, frozenset[int]] = {}
        files = []
        for rel, hits in sorted(lines.items()):
            unc = frozenset(n for n, c in hits.items() if c == 0)
            view = strip_all_gen_tests_decls(_source(ws, ref, rel)).rstrip() + "\n"
            split = split_uncovered(view, unc, hits,
                                    lines_in(t for t in proved if t.site.file == rel),
                                    cluster=None, is_structural_line=is_structural_brace_line,
                                    cfg_incompatible=filter_cfg_incompatible_lines,
                                    cfg_excluded=filter_cfg_excluded_lines,
                                    spent_lines=lines_in(t for t in spent if t.site.file == rel))
            not_actionable[rel] = split.outside_denominator(unc)
            files.append((rel, sum(1 for c in hits.values() if c > 0), len(hits), unc))
        floor = judge_files(a.floor, files, not_actionable=not_actionable,
                            spent=spent, stalled=())
        print(f"\n[{cluster}] {len(proved)} proved, {len(spent)} budget-spent function(s)"
              + (f"; no span for {', '.join(missing)}" if missing else ""))
        for f in floor.files:
            if f.raw_coverable == f.raw_covered and f.verdict != FILE_HELD:
                continue
            print(f"  {f.rel}  raw {f.raw_pct:.1f}% ({f.raw_covered}/{f.raw_coverable})  "
                  f"reachable {f.pct:.1f}% ({f.covered}/{f.coverable})  {f.verdict}"
                  + (f": {f.reason}" if f.reason else "")
                  + (f"  open {list(f.open_lines)}" if f.verdict == FILE_HELD else ""))
            proved_lines = lines_in(t for t in proved if t.site.file == f.rel)
            if f.verdict == FILE_HELD and f.open_lines and set(f.open_lines) <= proved_lines:
                broken = True
                print("    BROKEN EXCLUSION: every open line is in a proved function")
        print(f"  => {floor.summary()}")
        print("  " + json.dumps({"held": [f.rel for f in floor.held]}))
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())

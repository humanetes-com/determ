#!/usr/bin/env python3
"""How much of the per-cluster work is the same crate asked again — the census the world partition is judged by.

WHY THIS EXISTS
---------------
The pipeline takes a cluster (a build world: cfg atoms, toolchain, env) as
its unit of WORK in seven phases (t0c, t0d, the strip bake and its book,
t1.5, t2a, t2b, t3). From the behaviour side a cluster is only a world in
which a subset of the crate compiles. On run `72627-751534fe` (2026-09-30,
anyhow, `stable` + `nightly`) the strip bake built the behaviour book twice,
10,090 s and 10,639 s, one call at a time, over 227 examples identical in
every field but `cfg_cluster`; the two glossaries differed by one name. This
script measures, for a workspace, how much of each element class is CORE
(compiled in every world, asked once is enough) and how much is RESIDUE
(a proper subset of worlds), through the same partition the pipeline uses
(`pipeline_core/domain/cfg_worlds.py`), and, for past runs, what each phase
spent per cluster. Each stage of the world-partition plan states its fixture
as a line of this output; "validated" means that line on the live tree next
to the per-cluster negative control printed beside it.

USAGE
-----
    .venv/bin/python scripts/cluster_partition_census.py [--project anyhow]
        [--runs 3] [--run RUN_ID ...] [--no-extractor]

  --project       workspace under tmp/ (default anyhow). Reads
                  .skeleton/_clusters.json, .skeleton/_behaviours_<c>.json,
                  .skeleton/_private_surface_<c>.json, .skeleton/<c>/src and
                  tests/*.rs. No model, no GPU, no cargo.
  --no-extractor  skip the item class (it runs the release extractor binary
                  over one skeleton; tools/skeleton_extractor must be built).
  --runs N        the N most recent run ids in logs/events.jsonl (default 3).
  --run ID        explicit run id(s) instead of --runs.
  --budget-chunk  examples per book prompt (default: BEHAVIOUR_BOOK_EXAMPLES_PER_PROMPT or 24).

HOW TO INTERPRET THE OUTPUT
---------------------------
  worlds                      the clusters, as the bake froze them.
  skeletons identical         `yes` when every .skeleton/<c>/src is
                              byte-identical to the first; else the
                              differing files. The bake writes identical
                              skeletons; t1.5 then authors the layout INTO
                              .skeleton/<c>/src per cluster, so a `no` after
                              t1.5 is Stage 4's per-cluster authoring, read
                              off the tree.
  <class> core / residue:<w> / nowhere
                              per element class, how many elements every
                              world compiles, how many only world <w> (among
                              others, but not all) compiles, and how many
                              none does. Classes:
                                items     fn records of the skeleton, worlds
                                          from the extractor's cfg_predicates
                                          (cfg_worlds.worlds_of).
                                tests     the crate's own `#[test]` fns under
                                          tests/, worlds from the file's
                                          `#![cfg]` and the fn's `#[cfg]`.
                                gen-tests the same over the pipeline's
                                          generated files (`*_gen_N.rs`).
                                          Their gate is written by the
                                          pipeline (t0c and t0d's emit gate
                                          each file to the cluster that
                                          generated it), so residue here is
                                          per-cluster GENERATION, not code
                                          only one world compiles: Stage 2's
                                          duplication, sized.
                                specs     behaviour examples, by content
                                          (world_book.union_worlds); an id
                                          whose content differs between
                                          worlds counts once per variant.
                                glossary  public names, by membership in each
                                          world's stored glossary.
                                features  book features, by membership.
                                private   replaceable private-surface items,
                                          by membership.
  book drafts                 the book's draft calls (one per chunk of
                              examples of an asked feature; each is then
                              followed by its ablation and one check per
                              draft, which depend on the replies):
                              per world if each world builds its own book
                              (the NEGATIVE CONTROL, summed), and once for
                              the union. The union line is Stage 1's
                              fixture: it must equal the largest single
                              world, and the per-world sum is what the old
                              path paid.
  run <id>                    per past run: for every (phase, cluster), the
                              phase's wall minutes (phase.exit duration) and
                              the LLM calls and LLM minutes attributed to it
                              (a response on MainThread goes to the
                              innermost phase open at that moment; a worker
                              thread named `...-<cluster>-w*` supplies the
                              cluster). A phase whose cluster is `-` ran
                              once for the module; `(no phase)` is a call
                              outside every announced phase — for runs
                              before phase.enter/exit were emitted
                              (2026-09-30) that is nearly every call, and
                              only the worker-thread cluster is known. The
                              per-cluster rows of one phase side by side are
                              the duplication this census exists to size.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_core.adapters.structure import load_baked_clusters  # noqa: E402
from pipeline_core.domain.behaviour import BehaviourSuite  # noqa: E402
from pipeline_core.domain.behaviour_book import group_features, rule_checkable  # noqa: E402
from pipeline_core.domain.cfg import cfg_attr_predicate  # noqa: E402
from pipeline_core.domain.cfg_worlds import Partition, membership, partition, worlds_of  # noqa: E402
from pipeline_core.domain.world_book import union_glossary, union_worlds  # noqa: E402

BIN = ROOT / "tools/skeleton_extractor/target/release/skeleton-extractor"


def _print_partition(label: str, p: Partition[object]) -> None:
    sizes = p.sizes()
    rest = ", ".join(f"{k} {v}" for k, v in sizes.items() if k != "core")
    print(f"  {label:<9} core {sizes['core']}, {rest}")


def _skeletons(project: Path, worlds: list[str]) -> None:
    roots = [project / ".skeleton" / w / "src" for w in worlds]
    if not all(r.is_dir() for r in roots):
        print("  skeletons identical   n/a (a skeleton is missing)")
        return
    base = {p.relative_to(roots[0]): p.read_bytes() for p in roots[0].rglob("*.rs")}
    diff: set[str] = set()
    for r in roots[1:]:
        other = {p.relative_to(r): p.read_bytes() for p in r.rglob("*.rs")}
        diff |= {str(k) for k in base.keys() ^ other.keys()}
        diff |= {str(k) for k in base.keys() & other.keys() if base[k] != other[k]}
    print(f"  skeletons identical   {'yes' if not diff else 'no: ' + ', '.join(sorted(diff))}")


_TEST_FN = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)")


def _test_fns(tests_dir: Path) -> list[tuple[str, tuple[str, ...]]]:
    """(`file::fn`, cfg predicates) for every `#[test]` fn under tests/."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for f in sorted(tests_dir.glob("*.rs")):
        text = f.read_text(encoding="utf-8", errors="replace")
        file_preds = [m.group(1) for m in re.finditer(r"^\s*#!\[cfg\((.*)\)\]\s*$", text, re.M)]
        attrs: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#["):
                attrs.append(s)
                continue
            m = _TEST_FN.match(line)
            if m and any(a.replace(" ", "").startswith("#[test") for a in attrs):
                preds = [p for p in (cfg_attr_predicate(a) for a in attrs) if p]
                out.append((f"{f.name}::{m.group(1)}", tuple(file_preds + preds)))
            if s and not s.startswith("//"):
                attrs = []
    return out


def _book_chunks(suite: BehaviourSuite, size: int, crate: str, max_features: int) -> int:
    """Draft calls the book asks: one per chunk of each asked feature (the
    `max_features` features with the most checkable examples)."""
    features = group_features(suite.specs, suite.glossary, crate=crate)
    by_id = {s.behaviour_id: s for s in suite.specs}
    counts = sorted((sum(1 for i in f.example_ids if rule_checkable(by_id[i])) for f in features),
                    reverse=True)
    return sum(-(-k // size) for k in counts[:max_features] if k)


def census(project: Path, *, use_extractor: bool, chunk: int, max_features: int) -> None:
    clusters = load_baked_clusters(project)
    worlds = sorted(c.name for c in clusters)
    print(f"workspace {project}")
    print(f"  worlds                {', '.join(worlds) or '(none baked)'}")
    if not worlds:
        return
    _skeletons(project, worlds)
    if use_extractor and BIN.exists():
        from pipeline_core.adapters.extractor import CliExtractor
        recs = CliExtractor(BIN).enumerate_fns(project / ".skeleton" / worlds[0] / "src") or []
        items = [(f"{r.file}::{r.container}::{r.fn_name}", r.cfg_predicates) for r in recs if not r.is_test]
        _print_partition("items", partition(items, lambda e: worlds_of(e[1], clusters), worlds))
    tests = _test_fns(project / "tests")
    gen = re.compile(r"_gen_\d+\.rs::")
    _print_partition("tests", partition([t for t in tests if not gen.search(t[0])],
                                        lambda e: worlds_of(e[1], clusters), worlds))
    _print_partition("gen-tests", partition([t for t in tests if gen.search(t[0])],
                                            lambda e: worlds_of(e[1], clusters), worlds))
    suites = {}
    for w in worlds:
        p = project / ".skeleton" / f"_behaviours_{w}.json"
        if p.exists():
            suites[w] = BehaviourSuite.from_dict(json.loads(p.read_text(encoding="utf-8")))
    if suites:
        union = union_worlds(suites)
        _print_partition("specs", union.examples)
        glossary = union_glossary({w: s.glossary for w, s in suites.items()})
        g_part = partition([g.name for g in glossary],
                           membership({w: [g.name for g in s.glossary] for w, s in suites.items()}),
                           list(suites))
        _print_partition("glossary", g_part)
        marked = [f"{g.name} [{'+'.join(g.worlds)}]" for g in glossary if g.worlds]
        if marked:
            print(f"            residue names: {', '.join(marked)}")
        f_part = partition(sorted({f.item for s in suites.values() for f in s.features}),
                           membership({w: [f.item for f in s.features] for w, s in suites.items()}),
                           list(suites))
        _print_partition("features", f_part)
        per_world = {w: _book_chunks(s, chunk, project.name, max_features) for w, s in suites.items()}
        u = BehaviourSuite(cfg_cluster="union", specs=union.suite.specs, glossary=glossary)
        print(f"  book drafts           per world {per_world} = {sum(per_world.values())} "
              f"(negative control: one book per cluster); union "
              f"{_book_chunks(u, chunk, project.name, max_features)}")
    surfaces = {}
    for w in worlds:
        p = project / ".skeleton" / f"_private_surface_{w}.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            surfaces[w] = [str(i.get("name", "")) for i in d.get("replaceable", [])]
    if surfaces:
        names = sorted({n for v in surfaces.values() for n in v})
        _print_partition("private", partition(names, membership(surfaces), list(surfaces)))


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


_WORKER = re.compile(r"-(?P<cluster>[A-Za-z0-9_]+)-w\d*$")


def _events(path: Path, runs: set[str] | None) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = defaultdict(list)
    order: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"phase.' not in line and '"llm.response"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            rid = str(e.get("run_id", ""))
            if runs is not None and rid not in runs:
                continue
            if rid not in out:
                order.append(rid)
            out[rid].append(e)
    return {r: out[r] for r in order}


def run_rows(events: Iterable[dict[str, object]]) -> dict[tuple[str, str], list[float]]:
    """(phase, cluster) → [wall min, llm calls, llm min]."""
    rows: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    open_: list[tuple[str, str]] = []
    for e in events:
        ev = e.get("event")
        phase, cl = str(e.get("phase") or ""), str(e.get("cluster") or "-")
        if ev == "phase.enter" and e.get("thread") == "MainThread":
            open_.append((phase, cl))
        elif ev == "phase.exit":
            rows[(phase, cl)][0] += float(e.get("duration_ms") or 0) / 60000
            if e.get("thread") == "MainThread":
                for i in range(len(open_) - 1, -1, -1):
                    if open_[i] == (phase, cl):
                        del open_[i]
                        break
        elif ev == "llm.response":
            ph, where = open_[-1] if open_ else ("(no phase)", "-")
            m = _WORKER.search(str(e.get("thread") or ""))
            if m:
                where = m.group("cluster")
            r = rows[(ph, where)]
            r[1] += 1
            r[2] += float(e.get("duration_ms") or 0) / 60000
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="anyhow")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--run", action="append", default=None)
    ap.add_argument("--no-extractor", action="store_true")
    ap.add_argument("--budget-chunk", type=int,
                    default=int(os.getenv("BEHAVIOUR_BOOK_EXAMPLES_PER_PROMPT", "24")))
    a = ap.parse_args()
    census(ROOT / "tmp" / a.project, use_extractor=not a.no_extractor, chunk=max(1, a.budget_chunk),
           max_features=int(os.getenv("BEHAVIOUR_BOOK_MAX_FEATURES", "24")))
    ev_path = ROOT / "logs" / "events.jsonl"
    if not ev_path.exists():
        return 0
    runs = _events(ev_path, set(a.run) if a.run else None)
    ids = [r for r in runs if r]
    chosen = ids if a.run else (ids[-a.runs:] if a.runs > 0 else [])
    for rid in chosen:
        rows = run_rows(runs[rid])
        print(f"run {rid}")
        for (ph, cl), (wall, calls, llm) in sorted(rows.items()):
            if wall < 0.05 and not calls:
                continue
            print(f"  {ph:<22} {cl:<10} wall {wall:8.1f} min  llm {int(calls):4d} call(s) {llm:8.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

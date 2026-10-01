#!/usr/bin/env python3
"""Take the composition and rule captures t0d now takes, from the ORIGINAL
crate, and run them against an old reconstructed tree known to be wrong:
does a capture disagree where that tree's own generated tests were green?

WHY THIS EXISTS
  The loop optimises the oracle's verdict; whether the reconstruction
  behaves like the original is another quantity. The one recorded comparison
  (`divergences.md`, 2026-08-25) is the workspace checkpoint `14acd2b0`:
  stable 98 pass / 7 fail, every pipeline-authored test around three wrong
  public behaviours green (payload TypeId of an ad hoc message, `{:#?}`, the
  `ensure!` operand dump), and three silent divergences a public-API program
  could observe (the TypeId seen through `&*error as &dyn Error`, a
  backtrace accessor returning a captured backtrace, nightly `provide` left
  stubbed). The pipeline now holds captures out for an agreement reading
  (`domain/held_out.py`), probes compositions of two public items
  (`domain/use_cases.py`) and probes each kept rule (`domain/rule_probe.py`).
  Those run in t0d, which a restart from the augmented tag never re-enters,
  so their first live reading costs a from-scratch run. This script computes
  on that recorded tree what the captures would have said, before one is
  spent.

USAGE
  .venv/bin/python scripts/rule_probe_replay.py [--workspace tmp/anyhow]
      [--original-ref task0c-pre-anyhow] [--commit 14acd2b0]
      [--cluster stable] [--edges 8] [--rules 6] [--programs 3]
      [--dry] [--no-lib-tests] [--out DIR]

  --original-ref  the git ref of the workspace that holds the ORIGINAL crate
                  (the pre-augment tag). Extracted with `git archive` into a
                  scratch directory; the workspace tree is never checked out
                  or written.
  --commit        the reconstructed checkpoint to measure. Its
                  `.skeleton-filled/<cluster>` is extracted the same way.
  --edges         compositions asked (0: none). --rules: kept rules of the
                  CURRENT store's book asked (0: none), --programs each.
  --dry           no model call and no cargo: prints the edge inventory and
                  the asks only.
  --no-lib-tests  skip the negative control (`cargo test --lib` on the
                  reconstructed scratch tree: its own generated tests).
  Preconditions and cost: vLLM on :8000 serving MODEL (one call per 8
  compositions, one per rule). The skeleton-extractor release binary.
  Cargo: the two scratch crates build once each (dev-dependencies
  included, about a minute), then two runs per capture on the original,
  two more per extra rendering, and one on the reconstruction. Nothing in
  the pipeline's state is read for a decision or written; output goes
  under logs/rule_probe_replay/.

HOW TO INTERPRET THE OUTPUT
  `edges: F total, A asserted by an example` is the composition inventory
  of the original's public API against the current store's examples; the
  `ask` lines are the compositions sent to the model.
  `captured: N (K composition, R rule)` are programs the original ran
  twice with one output. `misses` are programs it did not run.
  Per kind, `agree / differ / not run` is each capture on the
  reconstructed tree: `differ` printed something else, `not run` did not
  build or panicked (a stub, or a body that aborts).
  Each `DIFFER` / `NOT RUN` row prints the program's expression, what the
  original printed and what the reconstruction printed. Read them beside
  `divergences.md` sections 3 and 6 (that list enters no test and no
  prompt).
  `negative control: lib tests P passed, F failed` is the reconstructed
  tree's own generated tests.
  CLAIMS, each PASS / FAIL / UNREAD:
    sharper-than-generated  at least one capture differs or does not run
                            on the tree. UNREAD when the lib tests could
                            not be run (nothing to be sharper than).
    reached-unpinned        printed as the rows to read: whether a row
                            names a behaviour no original test pins is a
                            person's reading; the script only says how
                            many rows there are.
  All captures agreeing on a tree known wrong is the off-theory reading:
  the captures are no sharper than the generated tests, or the edge model
  does not reach the divergent behaviours.
  Raw rows: <out>/<timestamp>.json.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pipeline_core.adapters.behaviour_probe import probe_env, run_probe  # noqa: E402
from pipeline_core.adapters.behaviour_store import load_behaviour_suite  # noqa: E402
from pipeline_core.adapters.extractor import CliExtractor  # noqa: E402
from pipeline_core.adapters.structure import load_baked_clusters  # noqa: E402
from pipeline_core.application.behaviour_agreement import judge_capture  # noqa: E402
from pipeline_core.domain.behaviour import BehaviourSpec, BehaviourVerdict, then_value_text  # noqa: E402
from pipeline_core.domain.behaviour_book import glossary_entries  # noqa: E402
from pipeline_core.domain.behaviour_probe import (  # noqa: E402
    CapturedProbe,
    captured_spec,
    other_observes,
    parse_behaviour_specs,
    program_for,
    render_probe_program,
    value_bearing_reason,
)
from pipeline_core.domain.cfg import cluster_cfg_set  # noqa: E402
from pipeline_core.domain.module_reachability import externally_reachable_files  # noqa: E402
from pipeline_core.domain.rule_probe import rule_probe_asks  # noqa: E402
from pipeline_core.domain.use_cases import edges_to_ask  # noqa: E402
from pipeline_core.prompts.behaviour_rules import build_rule_probe_prompt, glossary_lines  # noqa: E402
from pipeline_core.prompts.behaviour_synthesis import build_behaviour_synthesis_prompt  # noqa: E402

EXTRACTOR = REPO / "tools" / "skeleton_extractor" / "target" / "release" / "skeleton-extractor"


def extract(workspace: Path, ref: str, path: str, dest: Path) -> Path:
    """`git archive <ref> <path> | tar -x` into `dest`; the extracted
    `<dest>/<path>`. Never a checkout: the workspace tree is permanently
    dirty and holds fills newer than any commit."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", ref, path], cwd=workspace, capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    return dest / path


def original_crate(workspace: Path, ref: str, dest: Path) -> Path:
    """The original crate at `ref`: manifest, build script, src, tests."""
    for part in ("Cargo.toml", "build.rs", "src", "tests", "rust-toolchain.toml"):
        with contextlib.suppress(subprocess.CalledProcessError):   # a crate without that part
            extract(workspace, ref, part, dest)
    return dest


def reconstructed_crate(workspace: Path, commit: str, cluster: str, dest: Path) -> Path:
    root = extract(workspace, commit, f".skeleton-filled/{cluster}", dest)
    for name in ("tests", "target", "target-asan"):   # a link into the workspace, build output
        p = root / name
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
    return root


def capture(crate: Path, cluster: Any, module: str, probe: CapturedProbe, n: int,
            tag: str) -> BehaviourSpec | None:
    """`probe` on the original, twice, same output — or None."""
    program = render_probe_program(probe.given, probe.when, probe.observe, crate=module)
    a = run_probe(crate, cluster, program, tag)
    if not (a.ok and a.stdout.strip()):
        return None
    if run_probe(crate, cluster, program, f"{tag}_r").stdout != a.stdout:
        return None
    return captured_spec(cfg_cluster=cluster.name, probe=probe, n=n, stdout=a.stdout)


def lib_tests(crate: Path, cluster: Any) -> tuple[int, int] | None:
    """(passed, failed) of the tree's own in-crate tests, None when the
    run printed no result line."""
    cmd = ["cargo", *([f"+{cluster.toolchain}"] if getattr(cluster, "toolchain", "") else []),
           "test", "--lib", "--no-fail-fast"]
    out = subprocess.run(cmd, cwd=crate, env=probe_env(cluster), capture_output=True, text=True,
                         timeout=1800)
    passed = failed = 0
    seen = False
    for line in (out.stdout + out.stderr).splitlines():
        if line.startswith("test result:"):
            seen = True
            words = line.replace(";", " ").split()
            passed += int(words[words.index("passed") - 1])
            failed += int(words[words.index("failed") - 1])
    return (passed, failed) if seen else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", type=Path, default=REPO / "tmp" / "anyhow")
    ap.add_argument("--original-ref", default="task0c-pre-anyhow")
    ap.add_argument("--commit", default="14acd2b0")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--edges", type=int, default=8)
    ap.add_argument("--rules", type=int, default=6)
    ap.add_argument("--programs", type=int, default=3)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-lib-tests", action="store_true")
    ap.add_argument("--out", type=Path, default=REPO / "logs" / "rule_probe_replay")
    args = ap.parse_args()
    module = args.workspace.name
    cluster = next((c for c in load_baked_clusters(args.workspace) if c.name == args.cluster), None)
    if cluster is None:
        print(f"no baked cluster {args.cluster!r} in {args.workspace}")
        return 2
    scratch = Path(tempfile.mkdtemp(prefix="rule_probe_replay_"))
    try:
        original = original_crate(args.workspace, args.original_ref, scratch / "original")
        recon = reconstructed_crate(args.workspace, args.commit, args.cluster, scratch / "recon")
        src = original / "src"
        ex = CliExtractor(EXTRACTOR)
        fns, types, mods = ex.enumerate_fns(src) or [], ex.enumerate_types(src) or [], ex.enumerate_mods(src) or []
        sources = [(p.relative_to(src).as_posix(), p.read_text(encoding="utf-8"))
                   for p in sorted(src.rglob("*.rs"))]
        files = frozenset(r.file for r in fns) | frozenset(t.file for t in types)
        glossary = glossary_entries(fns, types, sources,
                                    reachable_files=externally_reachable_files(mods, files),
                                    cfg_set=cluster_cfg_set(cluster))
        by_name = {g.name: g for g in glossary}
        store = load_behaviour_suite(args.workspace, args.cluster)
        edges, asserted, total = edges_to_ask(glossary, store.specs, max_edges=args.edges)
        print(f"original: {args.original_ref}; reconstruction: {args.commit} "
              f".skeleton-filled/{args.cluster}; glossary {len(glossary)} name(s), "
              f"{sum(1 for g in glossary if g.signature)} with a declaration")
        print(f"edges: {total} total, {asserted} asserted by an example of the current store")
        for e in edges:
            print(f"  ask {e.producer} -> {e.consumer} (via {e.via})")
        rule_asks = rule_probe_asks(store.features, max_rules=args.rules)
        for a in rule_asks:
            print(f"  ask rule {a.rule_id}: {a.sentence[:100]}")
        if args.dry:
            return 0
        import model_interact
        by_id = {s.behaviour_id: s for s in store.specs}
        surface = glossary_lines(glossary)
        replies: list[tuple[str, str, bool]] = []
        if edges:
            replies.append(("composition", model_interact.ask(build_behaviour_synthesis_prompt(
                module_name=module, cfg_cluster=args.cluster, target_units=(),
                public_surface=surface, symbol_inventory="",
                target_edges=tuple((e.producer, by_name[e.producer].signature,
                                    e.consumer, by_name[e.consumer].signature) for e in edges))), True))
        for a in rule_asks:
            replies.append(("rule", model_interact.ask(build_rule_probe_prompt(
                crate=module, cfg_cluster=args.cluster, sentence=a.sentence, glossary=glossary,
                shown=[by_id[i] for i in a.shown if i in by_id],
                near=[by_id[i] for i in a.near if i in by_id], programs=args.programs)), False))
        captures: list[tuple[str, BehaviourSpec]] = []
        misses = 0
        n = 0
        for kind, reply, every_rendering in replies:
            for probe in parse_behaviour_specs(reply):
                n += 1
                if value_bearing_reason(probe.when) is not None:
                    misses += 1
                    continue
                spec = capture(original, cluster, module, probe, n, f"o{n}")
                if spec is None:
                    misses += 1
                    continue
                captures.append((kind, spec))
                for k, other in enumerate(other_observes(probe) if every_rendering else ()):
                    again = CapturedProbe(probe.name, probe.given, probe.when, other)
                    more = capture(original, cluster, module, again, n * 10 + k, f"o{n}_{other}")
                    if more is not None:
                        captures.append((kind, more))
        kinds = Counter(k for k, _ in captures)
        print(f"captured: {len(captures)} ({kinds['composition']} composition, {kinds['rule']} rule); "
              f"misses {misses}")
        rows: list[dict[str, Any]] = []
        tally: dict[str, Counter[str]] = {}
        for i, (kind, spec) in enumerate(captures):
            assert spec.then is not None
            verdict, detail = judge_capture(spec, run_probe(
                recon, cluster, program_for(spec, crate=module), f"r{i}"))
            label = {BehaviourVerdict.HOLD: "agree", BehaviourVerdict.MISMATCH: "differ",
                     BehaviourVerdict.NOT_RUN: "not run"}[verdict]
            tally.setdefault(f"{kind} / {spec.then.kind.name}", Counter())[label] += 1
            rows.append({"kind": kind, "observe": spec.then.kind.name, "setup": spec.setup,
                         "when": spec.when, "outcome": label,
                         "original": then_value_text(spec.then), "reconstruction": detail})
        for key in sorted(tally):
            t = tally[key]
            print(f"  {key:<34} agree {t['agree']:>2}  differ {t['differ']:>2}  not run {t['not run']:>2}")
        off = [r for r in rows if r["outcome"] != "agree"]
        for r in off:
            print(f"  {r['outcome'].upper()} [{r['kind']} / {r['observe']}] {r['when']}\n"
                  f"      original      : {r['original']!r}\n"
                  f"      reconstruction: {r['reconstruction'][:300]!r}")
        control = None if args.no_lib_tests else lib_tests(recon, cluster)
        print("negative control: " + ("lib tests not run" if control is None else
                                      f"lib tests {control[0]} passed, {control[1]} failed"))
        sharper = "UNREAD" if control is None else ("PASS" if off else "FAIL")
        print(f"CLAIM sharper-than-generated: {sharper}")
        print(f"CLAIM reached-unpinned: {len(off)} row(s) to read beside divergences.md sections 3 and 6")
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"{time.strftime('%Y%m%dT%H%M%S')}.json"
        path.write_text(json.dumps({"commit": args.commit, "original": args.original_ref,
                                    "cluster": args.cluster, "edges": [list(map(str, (e.producer, e.consumer)))
                                                                         for e in edges],
                                    "lib_tests": control, "rows": rows}, indent=2), encoding="utf-8")
        print(f"raw: {path}")
        return 0 if sharper != "FAIL" else 1
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

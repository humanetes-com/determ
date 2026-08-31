#!/usr/bin/env python3
"""Which filled body does a FAILING test actually execute — and which bodies does
nothing execute at all?

WHY THIS EXISTS
---------------
The TDD loop picks what to fill from a static attribution chain (panic sites,
rustc diagnostics, an identifier reverse-map, an LLM classifier, then futility
escalation). On run `2220347-7fa9b90f`, 2026-08-30, cluster `stable`, that chain
named the wrong body for 7 of the 11 still-failing tests, and the loop had no
signal that would have said so:

  - `test_downcast` / `test_downcast_ref` / `test_downcast_mut` / `test_trailer`
    were attributed to `src/error.rs::downcast`, `::context` and `::is`. All four
    need `src/lib.rs::format_err`, which renders every `anyhow!("literal")` into a
    `String` so no `&str` payload ever exists. `format_err` had never been a
    dispatch target: the cluster's service record lists only `src/lib.rs::source`.
  - `test_altdebug` was attributed to `src/fmt.rs::debug`, whose alternate branch
    is correct. It needs the `Debug for ContextError` body in `src/context.rs`,
    which prints `ContextError { context, error }` where upstream prints
    `Error { context, source }`.
  - `test_downcast_ref` / `test_downcast_mut` were reported `unowned` on every
    iteration — after attribution, fallback and escalation they owned no unit.

Coverage answers the same question directly and at a granularity the loop's unit
key cannot express. `src/context.rs::fmt` names three distinct bodies (Debug for
ContextError, Display for ContextError, Debug for Quoted); `src/error.rs::fmt`
names four; `src/wrapper.rs::fmt` names six. A profile distinguishes them by
source line, so "which body does this failing test run" has an exact answer.

The second reading is the one nothing else in the pipeline computes. On the same
tree, 18 filled bodies are executed by NO test in the suite, and five of them sit
inside the 30-unit escalation product the dispatcher ranks ahead of every
attributed accessor — including `src/error.rs::construct_from_adhoc`, which the
constructor-first promotion puts near the head of every escalated batch. Nothing
calls it: `Error::msg` builds its own vtable inline instead. A fill dispatched at
a body no test executes cannot change a verdict, by construction, however good
the body is. The same holds one level down: the entire non-alternate branch of
`src/fmt.rs::debug` (lines 19-32 on that tree) is dead, because `Debug for Error`
in `src/error.rs` reimplements it instead of delegating — so the two
`fmt_gen_3.rs` backtrace tests sit behind unreachable code and no rewrite of
`fmt.rs::debug` alone could ever have moved them.

Measured cost on that tree: instrumented rebuild after one edited source file 1s,
whole-suite run plus profdata merge plus export 1s, a single test's profile
sub-second. A plain uninstrumented cold build and run of the same suite is 6s.
This is cheap enough to run every sweep; it is not cheap enough to ignore the
separate target directory (see USAGE).

USAGE
-----
    .venv/bin/python scripts/coverage_owner_probe.py [--project tmp/anyhow]
                                                     [--cluster stable]
                                                     [--work DIR] [--keep]
                                                     [--tests a,b,c]
                                                     [--json OUT.json]
                                                     [--max-dead-regions N]

  --project   workspace holding `.skeleton-filled/<cluster>` and `tests/`
              (default `tmp/anyhow`).
  --cluster   cluster whose filled tree to measure (default `stable`).
  --work      staging directory. DEFAULTS TO A FRESH TEMP DIR, and that default
              matters: the probe copies the tree and builds with its own
              CARGO_TARGET_DIR because `RUSTFLAGS=-C instrument-coverage`
              invalidates every artefact in a target dir it touches. Pointing
              this at the live workspace would force the running loop to rebuild
              its whole suite on the next sweep.
  --keep      leave the staging directory in place (default: removed on exit).
  --tests     comma-separated test names to treat as the failing set, instead of
              taking them from the probe's own suite run. Use when the tree has
              moved under you and you want the reading for a recorded set.
  --json      also write the whole reading as JSON.
  --max-dead-regions
              cap on how many dead-region bodies are printed (default 12).

PRECONDITIONS. No GPU and no vLLM: this is cargo and llvm only, and it never
writes to the workspace. It needs `llvm-profdata` and `llvm-cov` on PATH whose
major version matches the one rustc reports under `rustc -vV` (override with
`LLVM_PROFDATA` / `LLVM_COV`). The tree must compile: a red tree yields no
binaries and the probe says so and stops. A test binary that CRASHES writes no
profile, so a crashing suite under-reports; the crash localizer remains the
signal there, and this probe complements it rather than replacing it.

HOW TO INTERPRET THE OUTPUT
---------------------------
Section OWNER CANDIDATES, one block per failing test. Each block lists the crate
bodies that test actually executed, split into two groups.

  exclusive   the body is executed by this failing test and by NO passing test.
              Nothing in the suite is currently protecting it, so a rewrite can
              be judged on this test alone and cannot regress anything. These are
              the units to dispatch first; a failing test with a non-empty
              exclusive list is a unit-scope fix.
  shared      the body is executed by this failing test AND by at least one
              passing test. A rewrite here is a set problem: it must be judged
              with the regression gate armed, and the correct change may require
              a sibling body in the same call to stay green. A failing test whose
              candidate list is entirely `shared` is the case that no single-unit
              fill can close — on the 2026-08-30 tree, delegating `Debug for
              Error` alone turned 10 passing tests red, and only became correct
              once `src/fmt.rs::debug` changed in the same edit.

Section DEAD BODIES: filled bodies executed by no test in the suite. These are
NOT fill targets. A dead body means some caller is wrong — the defect is
upstream, in whoever should be calling it and is not. Dispatching the dead body
spends a model call that cannot move a verdict. The `product`/`served` columns
say whether the loop is currently ranking it (in the escalation product) and
whether it has ever been a batch target, so `product=YES dead=YES` is a re-buy
the record can convict before the GPU is spent.

Section DEAD REGIONS: bodies that ARE executed, but with lines no test reaches.
Read a body with a large dead fraction as a body whose OTHER branch is
unreachable — again a caller defect, not a body defect. This is the reading that
separates "the fill was wrong" from "the fill was fine and nothing calls it".

Section FLOOR CROSS-TAB, printed when `.skeleton/_tdd_floor_<cluster>.json`
exists: for the escalation product, how many of its units are dead, how many have
never been served, and where the exclusive owners of the failing tests sit. The
decision rule the whole report feeds: dispatch exclusive owners that are alive
and never served, before anything that is dead or already graded flat.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TEST_LINE = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|ignored)\s*$")
FN_DECL = re.compile(r"\bfn\s+([A-Za-z_]\w*)")
IMPL_DECL = re.compile(r"^(unsafe\s+)?impl\b")


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None,
        capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True,
                          capture_output=capture, check=False)


def stage_tree(project: Path, cluster: str, work: Path) -> Path:
    """Copy the filled cluster tree + the shared tests dir into `work`."""
    filled = project / ".skeleton-filled" / cluster
    if not (filled / "src").is_dir():
        sys.exit(f"no filled tree at {filled / 'src'}")
    dst = work / "tree"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(filled / "src", dst / "src", dirs_exist_ok=True)
    for name in ("Cargo.toml", "Cargo.lock"):
        if (filled / name).exists():
            shutil.copy2(filled / name, dst / name)
    tests = project / "tests"
    if tests.exists():
        # symlinks_to_real: the cluster's `tests` is a symlink into the workspace
        shutil.copytree(tests, dst / "tests", dirs_exist_ok=True, symlinks=False)
    if (project / "rust-toolchain.toml").exists():
        shutil.copy2(project / "rust-toolchain.toml", dst / "rust-toolchain.toml")
    return dst


def cov_env(tree: Path, profile: str) -> dict[str, str]:
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(tree / "target-cov")
    env["RUSTFLAGS"] = "-C instrument-coverage"
    env["LLVM_PROFILE_FILE"] = profile
    return env


def test_binaries(tree: Path) -> list[Path]:
    deps = tree / "target-cov" / "debug" / "deps"
    out = []
    for p in sorted(deps.iterdir()) if deps.is_dir() else []:
        if p.suffix in (".d", ".rlib", ".rmeta", ".so") or not p.is_file():
            continue
        if os.access(p, os.X_OK):
            out.append(p)
    return out


def merge_and_export(prof_files: list[Path], objects: list[Path], out_json: Path,
                     profdata_bin: str, cov_bin: str) -> dict | None:
    if not prof_files:
        return None
    merged = out_json.with_suffix(".profdata")
    r = run([profdata_bin, "merge", "-sparse", *map(str, prof_files),
             "-o", str(merged)], cwd=out_json.parent)
    if r.returncode != 0 or not merged.exists():
        return None
    cmd = [cov_bin, "export", f"--instr-profile={merged}"]
    for o in objects:
        cmd += ["-object", str(o)]
    cmd += ["--format=text", "--skip-expansions"]
    r = run(cmd, cwd=out_json.parent)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    out_json.write_text(r.stdout)
    parsed: dict = json.loads(r.stdout)
    return parsed


def body_counts(export: dict, src_root: Path) -> dict[tuple[str, int], int]:
    """Max execution count per body, keyed (repo-relative file, first line).

    Keyed on the source line and not on the mangled symbol on purpose: the same
    generic body is a distinct symbol in every test binary's object, so a
    symbol-keyed fold reports a body as dead whenever any one binary linked it
    without running it.
    """
    marker = f"{src_root.name}{os.sep}src{os.sep}"
    best: dict[tuple[str, int], int] = collections.defaultdict(int)
    for exp in export.get("data", []):
        for fn in exp.get("functions", []):
            files = fn.get("filenames") or []
            if not files or marker not in files[0]:
                continue
            rel = "src/" + files[0].split(marker, 1)[1]
            regions = fn.get("regions") or []
            if not regions:
                continue
            key = (rel, min(r[0] for r in regions))
            best[key] = max(best[key], fn.get("count", 0))
    return dict(best)


def region_counts(export: dict, src_root: Path) -> dict[tuple[str, int], int]:
    """Max execution count per source LINE, for the dead-branch reading."""
    marker = f"{src_root.name}{os.sep}src{os.sep}"
    best: dict[tuple[str, int], int] = collections.defaultdict(int)
    for exp in export.get("data", []):
        for fn in exp.get("functions", []):
            files = fn.get("filenames") or []
            if not files or marker not in files[0]:
                continue
            rel = "src/" + files[0].split(marker, 1)[1]
            for r in fn.get("regions", []):
                for ln in range(r[0], r[2] + 1):
                    best[(rel, ln)] = max(best[(rel, ln)], r[4])
    return dict(best)


class Namer:
    """Resolve (file, line) to `file::fn` plus the ENCLOSING impl header.

    The impl is found by brace-matched spans, not by walking back to the nearest
    `impl` line: `src/error.rs` has 14 free `object_*` / `context_*` fns sitting
    below `impl Drop for Error`, and a backwards scan labels every one of them
    with that block.
    """

    def __init__(self, tree: Path) -> None:
        self.tree = tree
        self.cache: dict[str, list[str]] = {}
        self.spans: dict[str, list[tuple[int, int, str]]] = {}

    def lines(self, rel: str) -> list[str]:
        if rel not in self.cache:
            p = self.tree / rel
            self.cache[rel] = p.read_text().splitlines() if p.exists() else []
        return self.cache[rel]

    def impl_spans(self, rel: str) -> list[tuple[int, int, str]]:
        """(start_line, end_line, header) for every impl block, 1-indexed."""
        if rel in self.spans:
            return self.spans[rel]
        out: list[tuple[int, int, str]] = []
        src = self.lines(rel)
        i = 0
        while i < len(src):
            if IMPL_DECL.match(src[i].strip()):
                header = re.sub(r"\s*\{\s*$", "", src[i].strip())
                depth, started, j = 0, False, i
                while j < len(src):
                    depth += src[j].count("{") - src[j].count("}")
                    if "{" in src[j]:
                        started = True
                    if started and depth <= 0:
                        break
                    j += 1
                out.append((i + 1, j + 1, header))
                i = j + 1
                continue
            i += 1
        self.spans[rel] = out
        return out

    def name(self, rel: str, line: int) -> tuple[str, str]:
        src = self.lines(rel)
        if not src:
            return f"{rel}::?", ""
        for i in range(min(line, len(src)) - 1, max(-1, line - 40), -1):
            m = FN_DECL.search(src[i])
            if not m:
                continue
            fn_line = i + 1
            impl = ""
            for start, end, header in self.impl_spans(rel):
                if start < fn_line <= end:
                    impl = header
            return f"{rel}::{m.group(1)}", impl
        return f"{rel}::?", ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", default="tmp/anyhow")
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--work", default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--tests", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--max-dead-regions", type=int, default=12)
    args = ap.parse_args()

    profdata_bin = os.environ.get("LLVM_PROFDATA", "llvm-profdata")
    cov_bin = os.environ.get("LLVM_COV", "llvm-cov")
    for tool in (profdata_bin, cov_bin):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} not on PATH (set LLVM_PROFDATA / LLVM_COV)")

    project = Path(args.project).resolve()
    work = Path(args.work).resolve() if args.work else \
        Path(tempfile.mkdtemp(prefix="coverage_owner_probe_"))
    work.mkdir(parents=True, exist_ok=True)
    prof = work / "prof"
    prof.mkdir(exist_ok=True)

    try:
        t0 = time.time()
        tree = stage_tree(project, args.cluster, work)
        env = cov_env(tree, str(prof / "build-%p.profraw"))
        r = run(["cargo", "test", "--no-run", "--offline"], cwd=tree, env=env)
        if r.returncode != 0:
            print(r.stdout[-4000:] or r.stderr[-4000:])
            sys.exit("the staged tree does not build under instrumentation; "
                     "nothing to measure")
        bins = test_binaries(tree)
        t_build = time.time() - t0

        # Pass A — the whole suite. Gives the failing set and the dead bodies.
        t0 = time.time()
        run_a = prof / "a"
        run_a.mkdir(exist_ok=True)
        failed: list[str] = []
        passed: list[str] = []
        for b in bins:
            e = dict(env)
            e["LLVM_PROFILE_FILE"] = str(run_a / "%p-%m.profraw")
            out = run([str(b), "--test-threads=1"], cwd=tree, env=e)
            for ln in (out.stdout or "").splitlines():
                m = TEST_LINE.match(ln.strip())
                if not m:
                    continue
                if m.group(2) == "FAILED":
                    failed.append(m.group(1))
                elif m.group(2) == "ok":
                    passed.append(m.group(1))
        if args.tests:
            failed = [t.strip() for t in args.tests.split(",") if t.strip()]
        export_a = merge_and_export(sorted(run_a.glob("*.profraw")), bins,
                                    prof / "all.json", profdata_bin, cov_bin)
        if export_a is None:
            sys.exit("no coverage export from the whole-suite pass")
        any_cov = body_counts(export_a, tree)
        any_lines = region_counts(export_a, tree)

        # Pass B — the whole suite with every failing test skipped. What remains
        # is exactly the coverage the PASSING tests provide, which is what says
        # whether a candidate can be rewritten alone.
        run_b = prof / "b"
        run_b.mkdir(exist_ok=True)
        skip: list[str] = []
        for t in failed:
            skip += ["--skip", t.rsplit("::", 1)[-1]]
        for b in bins:
            e = dict(env)
            e["LLVM_PROFILE_FILE"] = str(run_b / "%p-%m.profraw")
            run([str(b), "--test-threads=1", *skip], cwd=tree, env=e)
        export_b = merge_and_export(sorted(run_b.glob("*.profraw")), bins,
                                    prof / "passing.json", profdata_bin, cov_bin)
        passing_cov = {k for k, c in body_counts(export_b, tree).items()
                       if c > 0} if export_b else set()

        # Pass C — one profile per failing test, on its own binary.
        owner: dict[str, set[tuple[str, int]]] = {}
        for t in failed:
            stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", t).strip("_") or "t"
            hit: Path | None = None
            for b in bins:
                e = dict(env)
                e["LLVM_PROFILE_FILE"] = str(prof / f"one-{stem}.profraw")
                (prof / f"one-{stem}.profraw").unlink(missing_ok=True)
                out = run([str(b), "--exact", t, "--test-threads=1"],
                          cwd=tree, env=e)
                if "running 1 test" in (out.stdout or ""):
                    hit = b
                    break
            if hit is None:
                owner[t] = set()
                continue
            ex = merge_and_export([prof / f"one-{stem}.profraw"], [hit],
                                  prof / f"one-{stem}.json", profdata_bin, cov_bin)
            owner[t] = {k for k, c in body_counts(ex, tree).items() if c > 0} \
                if ex else set()
        t_measure = time.time() - t0

        namer = Namer(tree)
        dead = sorted(k for k, c in any_cov.items() if c == 0)
        live_names = {
            namer.name(rel, start)[0]
            for (rel, start), c in any_cov.items() if c > 0
        }

        print(f"tree      {project / '.skeleton-filled' / args.cluster / 'src'}")
        print(f"staged at {tree}")
        print(f"binaries  {len(bins)}   tests {len(passed)} passing, "
              f"{len(failed)} failing")
        print(f"cost      instrumented build {t_build:.0f}s, "
              f"measurement {t_measure:.0f}s")
        print(f"bodies    {len(any_cov)} seen, "
              f"{sum(1 for c in any_cov.values() if c > 0)} executed, "
              f"{len(dead)} executed by nothing")

        print("\n===== OWNER CANDIDATES (bodies each failing test executes)")
        for t in failed:
            cands = sorted(owner.get(t) or ())
            excl = [k for k in cands if k not in passing_cov]
            shared = [k for k in cands if k in passing_cov]
            print(f"\n  {t}   {len(excl)} exclusive, {len(shared)} shared")
            for k in excl:
                n, impl = namer.name(*k)
                print(f"      exclusive  {n}:{k[1]}"
                      + (f"   [{impl}]" if impl else ""))
            if not excl:
                print("      exclusive  (none — every body this test runs is "
                      "also run by a passing test; no single-unit fill can "
                      "close it without the gate)")
            for k in shared[:12]:
                n, impl = namer.name(*k)
                print(f"      shared     {n}:{k[1]}"
                      + (f"   [{impl}]" if impl else ""))
            if len(shared) > 12:
                print(f"      shared     ... {len(shared) - 12} more")

        floor_p = project / ".skeleton" / f"_tdd_floor_{args.cluster}.json"
        product: set[str] = set()
        served: set[str] = set()
        if floor_p.exists():
            floor = json.loads(floor_p.read_text())
            served = set(floor.get("served") or [])
            for p in (floor.get("escalation_products") or {}).values():
                product |= set(p.get("earned") or [])
                product |= set(p.get("additions") or {})

        print("\n===== DEAD BODIES (executed by no test — the defect is the caller)")
        print(f"  {'unit':<48} {'product':<8} {'served':<7} impl")
        dead_names = []
        for k in dead:
            n, impl = namer.name(*k)
            dead_names.append(n)
            also = " ALSO LIVE" if n in live_names else ""
            print(f"  {n:<48} {'YES' if n in product else '-':<8} "
                  f"{'YES' if n in served else '-':<7} {impl[:44]}{also}")
        dead_locator_names = sorted({n for n in dead_names if n not in live_names})

        # What a WHOLE-FILE prompt carries versus what the failing test runs.
        # The file-first route hands the model every body in one file, on the
        # theory that the sibling bodies are the context the target needs. This
        # section prices that theory per test: `noise` is bodies the prompt
        # carries that the test never executes, `missing` is bodies the test
        # DOES execute that a single-file prompt cannot show.
        bodies_per_file: dict[str, int] = collections.Counter(
            rel for rel, _ in any_cov)
        print("\n===== CONTEXT SLICE vs WHOLE-FILE PROMPT")
        print("  (dispatch file = the file holding most of the test's slice)")
        for t in failed:
            sl = owner.get(t) or set()
            if not sl:
                continue
            per_file = collections.Counter(rel for rel, _ in sl)
            big = per_file.most_common(1)[0][0]
            noise = bodies_per_file[big] - per_file[big]
            missing = len(sl) - per_file[big]
            excl_out = sorted(
                namer.name(*k)[0] for k in sl
                if k not in passing_cov and k[0] != big)
            print(f"\n  {t}")
            print(f"      runs {len(sl)} bodies across "
                  f"{len(per_file)} file(s): "
                  + ", ".join(f"{f} {n}" for f, n in per_file.most_common()))
            print(f"      whole-file prompt for {big}: carries "
                  f"{bodies_per_file[big]} bodies, {noise} of them never "
                  f"executed by this test; omits {missing} bodies the test "
                  "does execute")
            if excl_out:
                print("      and omits an EXCLUSIVE owner living outside that "
                      "file: " + ", ".join(excl_out))

        print("\n===== DEAD REGIONS (executed bodies with unreachable lines)")
        rows = []
        for (rel, line), c in any_cov.items():
            if c == 0:
                continue
            n, _ = namer.name(rel, line)
            src = namer.lines(rel)
            end = line
            for i in range(line, len(src)):
                if src[i].rstrip() == "    }" or src[i].rstrip() == "}":
                    end = i + 1
                    break
            span = [(rel, ln) for ln in range(line, end + 1)
                    if (rel, ln) in any_lines]
            if not span:
                continue
            d = sum(1 for key in span if any_lines[key] == 0)
            if d:
                rows.append((d / len(span), d, len(span), n, line))
        rows.sort(reverse=True)
        for frac, d, tot, n, line in rows[:args.max_dead_regions]:
            print(f"  {frac:5.0%} dead ({d}/{tot} lines)  {n}:{line}")

        if product:
            dead_in_product = sorted(set(dead_locator_names) & product)
            never = sorted(product - served)
            print("\n===== FLOOR CROSS-TAB")
            print(f"  escalation product: {len(product)} unit(s); "
                  f"{len(dead_in_product)} executed by nothing; "
                  f"{len(never)} never a batch target")
            for n in dead_in_product:
                print(f"    dead + ranked ahead of attributed accessors: {n}")
            excl_owners = set()
            for t in failed:
                for k in owner.get(t) or ():
                    if k not in passing_cov:
                        excl_owners.add(namer.name(*k)[0])
            print(f"  exclusive owners of failing tests: {len(excl_owners)}; "
                  f"{len(excl_owners - served)} never a batch target")
            for n in sorted(excl_owners - served):
                print(f"    never dispatched, owns a failing test alone: {n}")

        if args.json_out:
            Path(args.json_out).write_text(json.dumps({
                "project": str(project), "cluster": args.cluster,
                "passing": sorted(set(passed)), "failing": failed,
                "dead_bodies": dead_names,
                "dead_locators": dead_locator_names,
                "owners": {t: sorted(f"{namer.name(*k)[0]}:{k[1]}"
                                     for k in (owner.get(t) or ()))
                           for t in failed},
                "exclusive": {t: sorted(f"{namer.name(*k)[0]}:{k[1]}"
                                        for k in (owner.get(t) or ())
                                        if k not in passing_cov)
                              for t in failed},
            }, indent=1))
            print(f"\nwrote {args.json_out}")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"\nstaging kept at {work}")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Plan one fill's sessions on a recorded tree with the session builder as the
packer's verdict, and the same plan under the pre-fix estimate as the
negative control. No GPU, no cargo, no model.

WHY. Run `3203804-8c6a0066` (2026-11-03, cluster `stable`): in fills 3-5 the
session packer admitted 19 sessions on an estimate of the PLANNED units
(`SliceCosts`, charged 121,121-130,369 against 132,000), and 13 of them sent
no turn 0, because the session opens on its write set (installable and
coupled bodies) and carries sections the estimate does not price. In fill 5
three of the four sessions the cap of 4 opened were those, and three sessions
of 4, 3 and 2 units were deferred `kind_cap`. The fix makes the packer decide
on the length of the turn 0 the session builder builds (`Turn0Fit`, from
`impl_fill._prepare_turn0`), over a ladder that also narrows the quoted test
files, TYPES_IN_SCOPE, and then one optional section at a time. This script computes that plan on the tree the fill
planned on, and the old plan on the same inputs, both measured by the builder.

USAGE.
    python scripts/session_open_fixture.py --workspace tmp/anyhow \
        --commit 845f732 --run 3203804-8c6a0066 --plan-index -1 \
        [--cluster stable] [--events logs/events.jsonl] \
        [--scratch /tmp/determ_session_open] [--keep]

`--commit` is the workspace checkpoint the fill planned on (the task2a
checkpoint commit `stable:<N> passing (iter K)` is the tree fill K planned
on). It is extracted with `git archive` into `--scratch`, never checked out.
`--plan-index` picks the run's `phase_c.session.plan` event (0-based, -1 the
last); its `slices` are the packer's input. Nothing under `--workspace` is
written: the builder reads the copy, and `emit`/`log` are captured.
Preconditions: the pipeline imports (`.venv`), the extractor release binary
built (`bash pre_run.sh` builds it). No vLLM, no cargo build. The assertion
probe is not run (the packer reserves its bound, as it does live).

HOW TO INTERPRET THE OUTPUT.
  inputs        — tests in the recorded slices, the capacity, the ladder.
  NEW plan      — one line per session the packer emits: tests, units,
                  `built` (the builder's turn 0 at its step), the step, and
                  the estimate beside it. Every `built` must be <= capacity;
                  `over_chars_single` names how many tests no step opens.
  OLD plan      — the pre-fix packer (estimate as the verdict) on the same
                  slices; per session `built@files@N` is what the builder
                  makes of it at the OLD ladder's narrowest step (narrowest
                  file threshold, test files whole). A line marked
                  UNOPENABLE is a session the old plan admitted that could
                  not send at that step. The builder here already prints
                  TYPES_IN_SCOPE once per type, which the old session did
                  not, so this count is a LOWER bound on the old code's
                  unopenable sessions; the authoritative negative control is
                  the run's own `phase_c.session.turn0` events (`sent` 0).
  fidelity      — the fixture's inputs are the tree, the slices and the
                  stores in the checkpoint. It does not pass the behaviour
                  suite, the crash evidence or the frontier evidence the
                  engine passes, and it reserves the probe at its bound, so a
                  length here differs from the live one by a few thousand
                  chars (2026-11-03, fill 5's opened session: 131,916 here,
                  127,810 live). A NEW session within that margin of the
                  capacity is the case the open-time rebuild exists for.
  per test      — that test alone, on its planned units: the built length
                  at the widest step, at the last step before any optional
                  section is dropped, and at the narrowest step, then the
                  first step that fits or UNOPENABLE. This is the length the
                  entry asks for against the capacity. A test that is
                  UNOPENABLE is over on the kept core (editable files, target
                  tests, the remaining types, the panic quote).
  unopenable    — each test no step opens, with the five largest tagged
                  sections of its narrowest text.
  recorded      — the run's own plan event: sessions and their charge.
  plan time     — seconds and builds the new packer spent (it builds every
                  candidate packing); budget it against a fill.
Decision rule: launch the fix when NEW has zero sessions over capacity and
OLD reproduces the recorded unopenable sessions on this input. A NEW
`over_chars_single` > 0 is the open gap the entry names: those tests get no
session until a ladder step fits them.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_LABEL = re.compile(r"^(?P<file>.+?)::(?P<fn>[^ ]+)(?: \((?P<cont>.*)\))?$")


def _site(label: str):  # noqa: ANN202
    from pipeline_core.domain.models import FnSite
    m = _LABEL.match(label)
    if m is None:
        raise ValueError(f"unparsable site label {label!r}")
    return FnSite(m["file"], m["fn"], m["cont"] or "")


def _extract(workspace: Path, commit: str, scratch: Path) -> Path:
    if scratch.exists():
        shutil.rmtree(scratch)
    dst = scratch / workspace.name
    dst.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(workspace), "archive", commit],
        check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dst)], input=archive, check=True)
    return dst


def _plan_event(events: Path, run: str, index: int) -> dict:
    plans = []
    with events.open(encoding="utf-8") as fh:
        for line in fh:
            if run not in line or '"phase_c.session.plan"' not in line:
                continue
            e = json.loads(line)
            if e.get("run_id") == run and e.get("event") == "phase_c.session.plan":
                plans.append(e)
    if not plans:
        raise SystemExit(f"no phase_c.session.plan event for run {run}")
    return plans[index]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--plan-index", type=int, default=-1)
    ap.add_argument("--cluster", default="stable")
    ap.add_argument("--events", default=str(ROOT / "logs" / "events.jsonl"))
    ap.add_argument("--scratch", default="/tmp/determ_session_open")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--probe", action="store_true",
                    help="also run the assertion probe (cargo, CPU) on the "
                         "copy and print each test's raw observed size")
    ap.add_argument("--dump", nargs="*", default=[],
                    help="write the last built turn 0 of each NEW session "
                         "holding one of these tests beside --scratch")
    args = ap.parse_args()
    workspace = Path(args.workspace).resolve()
    scratch = Path(args.scratch).resolve()
    if scratch == workspace or workspace in scratch.parents:
        print("refusing: --scratch must not be inside the workspace")
        return 2
    event = _plan_event(Path(args.events), args.run, args.plan_index)
    recorded = event["data"]
    slices = {t: frozenset(_site(lab) for lab in labs)
              for t, labs in recorded["slices"].items()}
    dst = _extract(workspace, args.commit, scratch)

    import os
    os.environ["ASSERT_PROBE_STAGE_ROOT"] = str(scratch / "probe_root")
    import pipeline
    from pipeline_core.adapters.structure import load_baked_clusters
    from pipeline_core.application.reconstruct import impl_fill
    from pipeline_core.domain.slice_session import (
        SESSION_OBSERVED_MAX_CHARS,
        Rendering,
        Turn0Fit,
        plan_slices,
        slice_plan_gaps,
    )

    captured: dict = {}
    real_payload = impl_fill.slice_plan_payload

    def _payload(sl, plans, gaps, costs=None, fit=None):  # noqa: ANN001, ANN202
        captured.update(slices=sl, plans=plans, gaps=gaps, costs=costs, fit=fit)
        return real_payload(sl, plans, gaps, costs, fit)

    impl_fill.slice_plan_payload = _payload  # type: ignore[assignment]
    real_build = impl_fill.build_slice_session_prompt
    built_texts: dict[tuple[str, ...], str] = {}

    def _build(**kw):  # noqa: ANN003, ANN202
        text = real_build(**kw)
        sl = kw.get("slice_")
        if sl is not None and sl.tests:
            built_texts[tuple(sl.tests)] = text
        return text

    impl_fill.build_slice_session_prompt = _build  # type: ignore[assignment]
    real_ws = impl_fill.session_write_set
    grown: dict[tuple[str, ...], tuple[int, int, int]] = {}

    def _ws(sl, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        out = real_ws(sl, *a, **k)
        grown[tuple(sl.tests)] = (len(sl.units), len(out.installable),
                                  len(out.coupled))
        return out

    impl_fill.session_write_set = _ws  # type: ignore[assignment]
    events: list[tuple[str, dict]] = []
    log: list[str] = []

    def _no_model(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("the fixture calls no model")

    deps = replace(pipeline._APP.impl_fill_deps,
                   emit=lambda ev, data=None, **_k: events.append((ev, data or {})),
                   log=log.append, ask_turns=_no_model)
    clusters = {c.name: c for c in load_baked_clusters(dst)}
    root = dst / ".skeleton-filled" / args.cluster
    tests_dir = dst / "tests"
    impl_fill.prepare_impl_fill(
        deps, project_dir=dst, target_src_dir=root,
        tests_dir_files=sorted(tests_dir.glob("*.rs")),
        module_name=workspace.name, cluster_name=args.cluster,
        cluster=clusters.get(args.cluster), check_cmd="cargo check --offline",
        cluster_env=pipeline._cargo_env(),
        fr_path=next(dst.glob("functional_requirements_*.md"), None),
        tdd_frontier_keys=set(), session_slices=slices)
    fit = captured.get("fit")
    costs = captured.get("costs")
    if fit is None or costs is None:
        print("the session route planned no slice with a capacity; log tail:")
        print("\n".join(log[-20:]))
        return 1
    plan_ev = next((d for e, d in events if e == "phase_c.session.plan"), {})
    # The old ladder's narrowest step: the narrowest file threshold, every
    # test file whole, the types section at its budget. That is where the
    # pre-fix session gave up.
    narrow = Rendering(min(costs.thresholds)) if costs.thresholds else Rendering()
    print(f"inputs        : {len(slices)} test(s), capacity {fit.capacity:,}, "
          f"ladder {[r.label() for r in fit.ladder]}")
    new_plans = captured["plans"]
    gaps = captured["gaps"]
    print(f"NEW plan      : {len(new_plans)} session(s), "
          f"over_chars_single={gaps.over_chars_single}, over_cap={gaps.over_cap}")
    over_new = 0
    for i, p in enumerate(new_plans):
        n = fit.length_at(p.tests, p.units, p.rendering)
        over_new += n > fit.capacity
        est = costs.chars(p.tests, p.units, file_chars=p.file_chars or None)
        g = grown.get(tuple(p.tests), (len(p.units), 0, 0))
        print(f"  #{i} {list(p.tests)} units={len(p.units)} "
              f"(+{g[1]} installable, +{g[2]} coupled) built={n:,} "
              f"at {p.rendering.label()} (estimate {est:,})")
    old_fit = Turn0Fit(
        length=lambda sl: costs.chars(sl.tests, sl.units,
                                      file_chars=sl.file_chars or None),
        ladder=tuple(Rendering(t) for t in costs.thresholds),
        capacity=costs.max_chars)
    old_plans = plan_slices(slices, held_tests=(), max_units=recorded["max_units"],
                            fit=old_fit)
    unopenable = 0
    print(f"OLD plan      : {len(old_plans)} session(s) "
          f"(over_chars_single={slice_plan_gaps(slices, max_units=recorded['max_units'], fit=old_fit).over_chars_single})")
    for i, p in enumerate(old_plans):
        n = fit.length_at(p.tests, p.units, narrow)
        bad = n > fit.capacity
        unopenable += bad
        print(f"  #{i} {list(p.tests)} units={len(p.units)} "
              f"estimate={costs.chars(p.tests, p.units, file_chars=p.file_chars or None):,} "
              f"built@{narrow.label()}={n:,}{'  UNOPENABLE' if bad else ''}")
    rec = recorded.get("sessions") or []
    print(f"recorded      : {len(rec)} session(s) planned by the run, charges "
          + ", ".join(f"{s.get('chars'):,}" for s in rec if s.get("chars")))
    print(f"plan time     : {plan_ev.get('plan_seconds')} s, "
          f"{plan_ev.get('plan_builds')} preparation(s)")
    print(f"verdict       : NEW over capacity {over_new}; OLD unopenable "
          f"{unopenable} of {len(old_plans)}")
    pre_context = next(
        (r for r in reversed(fit.ladder) if r.context_drops == 0),
        fit.widest())
    narrowest = fit.ladder[-1]
    print("per test      : widest / before context drops / narrowest, "
          f"capacity {fit.capacity:,}")
    for t in sorted(slices):
        units = slices[t]
        wide = fit.length_at((t,), units, fit.widest())
        mid = fit.length_at((t,), units, pre_context)
        narrow_n = fit.length_at((t,), units, narrowest)
        step = fit.rendering((t,), units)
        where = step.label() if step is not None else "UNOPENABLE"
        print(f"  {t} widest={wide:,} before_context={mid:,} "
              f"narrowest={narrow_n:,} {where}")
    refused = [d for e, d in events if e == "phase_c.session.unopenable_test"]
    if refused:
        print(f"unopenable    : {len(refused)}")
        for d in refused:
            secs = ", ".join(
                f"{s['name']} {s['chars']:,}" for s in (d.get("sections") or []))
            print(f"  {d.get('test')} built={d.get('built'):,} {secs}")
    if args.probe:
        from pipeline_core.application.reconstruct.assert_probe_runner import (
            run_assert_probes,
        )
        by_file: dict[Path, list[str]] = {}
        for t in sorted(slices):
            leaf = t.rsplit("::", 1)[-1]
            for f in sorted(tests_dir.glob("*.rs")):
                if f"fn {leaf}(" in f.read_text(encoding="utf-8", errors="replace"):
                    by_file.setdefault(f, []).append(leaf)
                    break
        res = run_assert_probes(
            project_dir=dst, cluster_root=root, cluster_name=args.cluster,
            cluster_env=pipeline._cargo_env(), tests_by_file=by_file,
            run_cargo=pipeline._run_cargo, log=lambda m: print(m))
        sizes = sorted(((len(v), t) for t, v in res.rendered.items()), reverse=True)
        print(f"probe         : {len(sizes)} test(s) observed; raw observed "
              f"chars per test (bound {SESSION_OBSERVED_MAX_CHARS:,}): "
              + ", ".join(f"{t} {n:,}" for n, t in sizes))
    if args.dump:
        for p in new_plans:
            text = built_texts.get(tuple(p.tests))
            if text is not None and set(p.tests) & set(args.dump):
                out = scratch.parent / (scratch.name + "_" + "+".join(p.tests)[:80] + ".txt")
                out.write_text(text, encoding="utf-8")
                print(f"dumped        : {out} ({len(text):,} chars, last build)")
    if not args.keep:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

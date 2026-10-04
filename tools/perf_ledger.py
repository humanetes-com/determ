"""Append-only performance ledger: one JSON line per measured run.

Tracks pipeline performance across CODE MODIFICATIONS, not only across models
— the generalisation of the model A/B exercise whose conclusions live in
`logs/model_ab/REPORT.md`. Every row carries the three things a comparison
needs and the A/B night proved are easy to lose: the code identity (tree
stamp first — runs launch from uncommitted trees, so a commit hash alone
cannot distinguish the edit that introduced a defect from the edit that fixed
it; HEAD is recorded as a human landmark only), the configuration (the env
knobs that change route/model/sampling behaviour), and the fill evolution
(per-cluster pass trajectory across sweeps, wall, calls, contract adherence).

The file is `logs/perf_ledger.jsonl` — one JSON object per line, so it loads
in one line of pandas (`pd.read_json(..., lines=True)`) or jq, which is the
"mathematically easy" property a comma-separated hybrid would lose the moment
a JSON field contains a comma.

Usage:
    .venv/bin/python tools/perf_ledger.py append <run_id> --crate hardcrate \
        --note "what this run measured"        # right AFTER the run, while
                                               # .env still matches it
    .venv/bin/python tools/perf_ledger.py list
    .venv/bin/python tools/perf_ledger.py compare <run_id_a> <run_id_b>
    .venv/bin/python tools/perf_ledger.py phases [--run <run_id>]
                                              # per-phase cost vs the previous
                                              # code identities (the health tab)
    .venv/bin/python tools/perf_ledger.py phases --history t0c
                                              # every recorded run of a phase

The code identity and the configuration of a run the pipeline recorded itself
(`logs/run_stats.db`, written from launch) are read from that record, so an
`append` made after `.env` was edited still states the run's own knobs; only a
run with no record falls back to reading `.env` now, and says so.

Metrics are extracted from `logs/events.jsonl` via the proven
`logs/model_ab/model_report.py` collector; rows seeded from earlier runs
whose events have rotated away carry `"source": "recorded"` and cite the
document the numbers came from.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "perf_ledger.jsonl"
EVENTS = ROOT / "logs" / "events.jsonl"
RUN_STATS = ROOT / "logs" / "run_stats.db"


def _load_rows() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _env_block() -> dict[str, Any]:
    """The knobs that change run behaviour, as the pipeline resolves them
    (`domain.phase_cost.RunConfig`, the one resolver). Read NOW — the fallback
    for a run the pipeline did not record itself."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from pipeline_core.domain.phase_cost import RunConfig
    return RunConfig.from_env(os.environ).as_dict()


def _recorded_run(run_id: str) -> Any:
    """The pipeline's own record of `run_id` (code + config captured at
    launch), or None."""
    from pipeline_core.adapters.run_stats_store import SQLiteRunStatsStore
    if not RUN_STATS.exists():
        return None
    store = SQLiteRunStatsStore(RUN_STATS, read_only=True)
    try:
        return store.run(run_id)
    finally:
        store.close()


def _code_block() -> dict[str, Any]:
    stamp = None
    stamp_file = ROOT / ".pre_run_stamp"
    if stamp_file.exists():
        stamp = stamp_file.read_text(encoding="utf-8").split()[0][:12]
    commit = None
    with suppress(Exception):
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, timeout=10,
            capture_output=True, text=True, check=True).stdout.strip()
    return {"tree_stamp": stamp, "commit": commit}


def _extract_metrics(run_id: str) -> dict[str, Any] | None:
    sys.path.insert(0, str(ROOT / "logs" / "model_ab"))
    from model_report import collect, compliance  # noqa: PLC0415
    c = collect(run_id)
    if not c["events"]:
        return None
    wall = None
    if c["first_ts"] and c["last_ts"]:
        wall = round((c["last_ts"] - c["first_ts"]).total_seconds() / 60, 1)
    comp = compliance(c["pairs"]).get("counts", {})
    traj: dict[str, dict[str, Any]] = {}
    for it in c["iters"]:
        cl = it["cluster"] or "?"
        entry = traj.setdefault(cl, {"passed": [], "total": 0})
        entry["passed"].append(it["passed"])
        entry["total"] = max(entry["total"], it["passed"] + it["failed"])
    return {
        "wall_min": wall,
        "llm_calls": int(c["events"].get("llm.request", 0)),
        "contract": {"obeyed": comp.get("obeyed", 0),
                     "demanded": comp.get("marker demanded", 0)},
        "trajectory": traj,
        "started": c["first_ts"].isoformat() if c["first_ts"] else None,
    }


def _final(traj: dict[str, Any]) -> str:
    parts = []
    for cl in sorted(traj):
        e = traj[cl]
        last = e["passed"][-1] if e.get("passed") else "?"
        parts.append(f"{cl}:{last}/{e.get('total', '?')}")
    return " ".join(parts) or "-"


def _fmt_row(r: dict[str, Any]) -> str:
    env = r.get("env", {})
    m = r.get("metrics", {})
    model = (env.get("MODEL") or "?").rsplit("/", 1)[-1][:22]
    contract = m.get("contract") or {}
    cpct = (f"{contract['obeyed'] * 100 // contract['demanded']:3d}%"
            if contract.get("demanded") else "  - ")
    wall = m.get("wall_min")
    return (f"{(r.get('date') or '?')[:10]}  {r['run_id']:>18s}  "
            f"{r.get('crate', '?'):9s} {model:22s} "
            f"{env.get('VLLM_REASONING_STRENGTH', '?'):5s} "
            f"{str(env.get('PHASE_C_ROUTE_ORDER', '?')):12s} "
            f"{(f'{wall:6.1f}m' if wall is not None else '     ?'):>7s} "
            f"calls={m.get('llm_calls', '?'):>4} contract={cpct}  "
            f"{_final(m.get('trajectory', {}))}"
            + (f"  [{r['note']}]" if r.get("note") else ""))


def cmd_append(args: argparse.Namespace) -> int:
    metrics = _extract_metrics(args.run_id)
    if metrics is None:
        print(f"run {args.run_id}: no events in logs/events.jsonl — for a "
              "rotated/older run, add a `source: recorded` row by hand with "
              "the numbers and where they were recorded.")
        return 2
    recorded = _recorded_run(args.run_id)
    if recorded is not None and recorded.config:
        code = {"tree_stamp": recorded.tree_stamp, "commit": recorded.commit,
                "code_key": recorded.record.code_key}
        env = recorded.config
    else:
        print(f"run {args.run_id}: not in {RUN_STATS.name} — code and config are "
              "read from the tree and .env as they are NOW")
        code, env = _code_block(), _env_block()
    row = {
        "run_id": args.run_id,
        "date": (metrics.pop("started") or "")[:10] or None,
        "crate": args.crate,
        "code": code,
        "env": env,
        "metrics": metrics,
        "note": args.note,
        "source": "events",
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    print("appended: " + _fmt_row(row))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    rows = _load_rows()
    if not rows:
        print(f"{LEDGER} is empty.")
        return 0
    for r in rows:
        print(_fmt_row(r))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    rows = {r["run_id"]: r for r in _load_rows()}
    a, b = rows.get(args.a), rows.get(args.b)
    if a is None or b is None:
        missing = args.a if a is None else args.b
        print(f"run {missing} not in {LEDGER}")
        return 2
    print("A: " + _fmt_row(a))
    print("B: " + _fmt_row(b))
    ma, mb = a.get("metrics", {}), b.get("metrics", {})
    wa, wb = ma.get("wall_min"), mb.get("wall_min")
    if wa and wb:
        print(f"   wall  {wa:.1f}m -> {wb:.1f}m  ({(wb - wa) / wa * 100:+.0f}%)")
    ca, cb = ma.get("llm_calls"), mb.get("llm_calls")
    if ca and cb:
        print(f"   calls {ca} -> {cb}  ({(cb - ca) / ca * 100:+.0f}%)")
    ea, eb = a.get("env", {}), b.get("env", {})
    diffs = {k for k in set(ea) | set(eb) if ea.get(k) != eb.get(k)}
    if diffs:
        print("   env deltas: " + "; ".join(
            f"{k}: {ea.get(k)} -> {eb.get(k)}" for k in sorted(diffs)))
    sa, sb = a.get("code", {}).get("tree_stamp"), b.get("code", {}).get("tree_stamp")
    print(f"   code: {sa} -> {sb}"
          + ("  (same tree)" if sa and sa == sb else ""))
    return 0


def cmd_phases(args: argparse.Namespace) -> int:
    if args.history:
        return _phase_history(args.history)
    from pipeline_core.monitor.phase_cost_view import phase_cost_report
    report = phase_cost_report(EVENTS, running=False, run_id=args.run)
    for f in report.findings:
        print(f"{f.severity.symbol} [{f.kind}] {f.subject}")
        print(f"   {f.evidence}")
        print(f"   → {f.action}")
    if report.findings:
        print()
    print("\n".join(report.table))
    return 0


def _phase_history(phase: str) -> int:
    """Every recorded run of `phase` (live and backfilled), oldest first."""
    from pipeline_core.adapters.run_stats_store import SQLiteRunStatsStore
    if not RUN_STATS.exists():
        print(f"{RUN_STATS} does not exist yet.")
        return 0
    store = SQLiteRunStatsStore(RUN_STATS, read_only=True)
    try:
        runs = {r.record.run_id: r for r in store.runs()}
        rows = [s for s in store.samples() if s.key.phase == phase]
    finally:
        store.close()
    if not rows:
        print(f"no recorded sample of phase {phase!r}")
        return 0
    rows.sort(key=lambda s: runs[s.run_id].record.started if s.run_id in runs else "")
    for s in rows:
        r = runs.get(s.run_id)
        ident = (r.record.code_key or f"HEAD {r.commit}") if r else "?"
        where = "/".join(p for p in (s.key.module, s.key.cluster) if p)
        print(f"{(r.record.started if r else '?')[:16]}  {s.run_id:>18s}  "
              f"{ident:<17s} {where:<18s} {s.outcome:<8s} "
              f"wall {s.wall_ms / 1000:7.1f}s  calls {s.work.llm_calls:>4}  "
              f"tokens {s.work.tokens:>8}  cargo {s.work.cargo:>4}"
              + (f"  [{r.source}]" if r and r.source != "live" else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("append", help="extract a finished run and append a row")
    p_add.add_argument("run_id")
    p_add.add_argument("--crate", required=True,
                       help="anyhow | microcrate | hardcrate | ...")
    p_add.add_argument("--note", default="",
                       help="what changed vs the previous comparable row")
    p_add.set_defaults(fn=cmd_append)
    p_list = sub.add_parser("list", help="print every row, one line each")
    p_list.set_defaults(fn=cmd_list)
    p_cmp = sub.add_parser("compare", help="two rows side by side with deltas")
    p_cmp.add_argument("a")
    p_cmp.add_argument("b")
    p_cmp.set_defaults(fn=cmd_compare)
    p_ph = sub.add_parser("phases", help="per-phase cost vs the previous code "
                                         "identities (what the health tab shows)")
    p_ph.add_argument("--run", default=None,
                      help="run id (default: the latest recorded live run)")
    p_ph.add_argument("--history", metavar="PHASE", default=None,
                      help="list every recorded run of one phase id instead")
    p_ph.set_defaults(fn=cmd_phases)
    args = ap.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())

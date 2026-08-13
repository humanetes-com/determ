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
    """The knobs that change run behaviour, as the pipeline resolves them.
    Read at APPEND time — append right after the run, before editing .env."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from pipeline_core.domain.model_defaults import resolve_route_order
    model = os.getenv("MODEL", "")
    route_env = os.getenv("PHASE_C_ROUTE_ORDER")
    return {
        "MODEL": model,
        "PHASE_C_ROUTE_ORDER": str(resolve_route_order(route_env, model)),
        "route_order_source": "env" if (route_env or "").strip() else "model-default",
        "PHASE_C_DRIVER": os.getenv("PHASE_C_DRIVER", "tdd"),
        "VLLM_REASONING_STRENGTH": os.getenv("VLLM_REASONING_STRENGTH", "high"),
        "VLLM_MAX_MODEL_LEN": int(os.getenv("VLLM_MAX_MODEL_LEN", "0")) or None,
        "VLLM_MAX_THINK_CHARS": int(os.getenv("VLLM_MAX_THINK_CHARS", "60000")),
        "PIPELINE_PARALLELISM": int(os.getenv("PIPELINE_PARALLELISM", "0")) or None,
    }


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
    row = {
        "run_id": args.run_id,
        "date": (metrics.pop("started") or "")[:10] or None,
        "crate": args.crate,
        "code": _code_block(),
        "env": _env_block(),
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
    args = ap.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Per-fill join — what each dispatched fill was TOLD, what it ANSWERED, and what it BOUGHT.

WHY THIS EXISTS
---------------
The repo conventions judge a flat sweep fill by fill: each landed fill is
mis-targeted, a wrong body, a right body with an unreachable verdict, or an
answer the pipeline dropped. Half of that is already a pipeline mechanism —
`pipeline_core/domain/fill_class.py` emits `phase_c.fill.class`
(cluster/unit/route/class) per terminal fill and the histogram rides the
`phase_c.tdd.iter` line, so the CLASS never has to be re-derived by hand.

The other half is not joined anywhere. Naming the lever for a wrong-body fill
requires the PROMPT behind it — which builder call site sent it
(`llm.request.caller`), what the prompt and the reply actually said
(`logs/prompts/<sha>.txt`, written by `_store_prompt_content` in pipeline.py
for the request AND the reply), and what the model reasoned
(`logs/thinking/<response sha>.txt`, written by `_persist_thinking` in
`adapters/llm_transports.py`). Those three stores are keyed by sha and hold
71,506 prompt and 27,273 thinking files against 327,785 event lines from 357
runs (2026-09-03), so the join is redone by hand, per session, with
`tools/event_grep.py` plus greps. This script is that join, once.

It does not classify anything itself: the class column is the pipeline's own
`phase_c.fill.class` value. A segment with no class event is reported as
class `-` rather than guessed at.

HOW THE SEGMENTATION WORKS (and what it assumes)
------------------------------------------------
Fill routes run on named worker threads (`per-fn-stable-w3`,
`file-first-stable-w1`, `impl-fill-stable-w2`, `MainThread` for the pair and
session routes), and within one thread the record is strictly sequential:

    phase_c.fn.fill.start → llm.request/llm.response* → phase_c.fn.fill.done
    → phase_c.verdict* → (retro retry → more calls)* → phase_c.fill.class
    → the NEXT phase_c.fn.fill.start

So a SEGMENT is everything on one thread from a fill-start up to the next
fill-start on that same thread. That deliberately keeps the retrospective
call, the self-note extraction and every retry inside the fill that caused
them — the GPU a unit actually cost, not just its first call. The assumption
is thread sequentiality; it fails only if a route ever runs two fills
concurrently on one thread, which no route does today. Events with a null
thread (older runs) are treated as one pseudo-thread and segment the same way.

USAGE
-----
    .venv/bin/python scripts/fill_join.py [--run ID] [--events PATH]
        [--cluster stable] [--route per-fn|file|impl|pair|session]
        [--class wrong|mis|unreachable|dropped|cashed|compile]
        [--unit-contains error.rs] [--min-calls 1] [--summary-only]
        [--open UNIT_SUBSTRING] [--head N] [--json PATH]

  --run          run id; defaults to the LAST run id in the events file.
  --open         print the full call detail for matching fills: every prompt
                 sha with its builder call site, the reply, and whether a
                 reasoning trace was persisted. With --head N, also print the
                 first N chars of each prompt and the whole reply.
  --json         write the table as JSON (one object per fill) instead of
                 relying on the terminal rendering.

Reads only. No GPU, no model, no writes to the workspace. Safe on a live run
(the events file is append-only); a fill still in flight shows as `open`.

HOW TO INTERPRET THE OUTPUT
---------------------------
One row per dispatched fill:

  class      the pipeline's own `phase_c.fill.class`: cashed (attributed
             tests observed passing), wrong (compiling answer that did not
             cash), mis (the record already said the unit cannot pay),
             unreachable (body kept, attributed tests cannot run), dropped
             (a route/parser/splicer lost the reply), compile (stopped on a
             red crate). `-` means no class event — an in-flight fill, or a
             route that terminated without one.
  route      file / impl / per-fn / pair / session — WHICH PROMPT BUILDER
             family sent it. The lever for a fill must be an argument of the
             builder that route calls; that is the column to read first.
  calls      model calls charged to this fill, retro and note extraction
             included. `dup` counts calls that re-sent a prompt sha this
             same fill had already sent — a non-zero dup with an empty reply
             is the resample loop paying twice for the same question.
  gpu_min    summed `llm.response.duration_ms` for those calls: model time,
             not wall time.
  reply      total reply chars over the fill's calls. Near-zero reply chars
             with calls > 0 is an empty-generation fill, whatever its class.
  verdicts   the `phase_c.verdict` dispositions this fill produced
             (accept/keep_deferred/revert/reject/conviction/cap_refusal).
  unit       the unit as the class event names it.

The decision rule the columns feed, in the order the conventions ask it:

  1. Sum gpu_min by class. GPU sitting in `mis` + `unreachable` is spent on
     units whose verdict could not move no matter what body was written —
     a dispatch/attribution question, and no prompt change converts it.
  2. For `wrong` rows, --open the fill: the builder call site is the owner,
     and the answer to "what was missing" has to be an argument that builder
     takes. A reply that already had the evidence and still wrapped points
     at the instruction slot, not at another evidence channel.
  3. `dropped` rows are pipeline defects by definition — the reply exists in
     `logs/prompts/<response sha>.txt`; read it before theorising.
  4. A fill with calls > 0, class `compile`, and dup > 0 is the resample
     path re-asking an identical prompt; the reply store says whether the
     answers differed.

MEASURED, so the rendering is checkable
---------------------------------------
`.venv/bin/python scripts/fill_join.py --run 330242-0be2c3e1 --route per-fn`
on 2026-09-03, verbatim:

    39 fill(s), 85 model call(s), 409.2 gpu_min
      class        fills  calls  dup  gpu_min  share  routes
      unreachable     33     47    0    246.3    60%  per-fn
      compile          2     26    6     89.6    22%  per-fn
      wrong            1      9    0     47.2    12%  per-fn
      cashed           3      3    0     26.1     6%  per-fn

60% of that route's model time sat in fills whose attributed tests could not
run, and 22% in two fills that never reached a verdict — one of them,
`src/error.rs::construct_from_display`, 11 calls and 43.1 gpu_min with 2 of
those calls re-sending a prompt sha it had already sent, to empty replies.
Three cashed fills cost 3 calls between them. None of those four numbers is
readable from the iteration summary line, which reports the sweep's score.

"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "logs" / "events.jsonl"
PROMPTS = ROOT / "logs" / "prompts"
THINKING = ROOT / "logs" / "thinking"

# A fill segment opens on one of these and runs until the next opener on the
# same thread. `session.start` is the transcript route's opener; it has no
# `.fill.` in its name, which is why the set is explicit rather than a prefix
# match on ".fill.start".
OPENERS = {
    "phase_c.fn.fill.start": "per-fn",
    "phase_c.impl.fill.start": "impl",
    "phase_c.file.fill.start": "file",
    "phase_c.pair.fill.start": "pair",
    "phase_c.session.start": "session",
}
TERMINALS = {
    "phase_c.fn.fill.done", "phase_c.fn.fill.fail", "phase_c.fn.fill.skip",
    "phase_c.fn.fill.salvaged", "phase_c.impl.fill.done", "phase_c.impl.fill.fail",
    "phase_c.impl.fill.salvaged", "phase_c.impl.fill.already_complete",
    "phase_c.file.fill.done", "phase_c.file.fill.revert", "phase_c.session.end",
}


def _unit_of(event: str, data: dict[str, Any]) -> str:
    """The unit name an opener implies, in the same shape `phase_c.fill.class`
    uses (`file::fn`, `file::container`, or the bare file)."""
    if event == "phase_c.fn.fill.start":
        return f"{data.get('file')}::{data.get('fn')}"
    if event == "phase_c.impl.fill.start":
        return f"{data.get('file')}::{data.get('container')}"
    if event == "phase_c.file.fill.start":
        return str(data.get("file"))
    if event == "phase_c.pair.fill.start":
        return f"{data.get('dispatcher')} -> {data.get('implementer')}"
    units = data.get("units") or []
    return f"session[{len(units)} unit(s)]"


@dataclass
class Call:
    """One model call charged to a fill."""

    prompt_sha: str = ""
    caller: str = ""
    prompt_chars: int = 0
    response_sha: str = ""
    response_chars: int = 0
    duration_ms: int = 0
    ts: str = ""
    error: str = ""

    def thinking_path(self) -> Path:
        return THINKING / f"{self.response_sha}.txt"

    def prompt_path(self) -> Path:
        return PROMPTS / f"{self.prompt_sha}.txt"

    def response_path(self) -> Path:
        return PROMPTS / f"{self.response_sha}.txt"


@dataclass
class Fill:
    """One dispatched fill: its opener, its model calls, its verdicts, its class."""

    run_id: str = ""
    cluster: str = ""
    thread: str = ""
    route: str = ""
    unit: str = ""
    iter_no: int | None = None
    pass_n: int | None = None
    ts_start: str = ""
    ts_end: str = ""
    terminal: str = ""
    terminal_reason: str = ""
    fill_class: str = "-"
    calls: list[Call] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    retro_retries: int = 0
    notes_added: int = 0

    @property
    def n_calls(self) -> int:
        return len(self.calls)

    @property
    def dup_calls(self) -> int:
        """Calls that re-sent a prompt sha this fill had already sent."""
        seen: set[str] = set()
        dups = 0
        for c in self.calls:
            if c.prompt_sha in seen:
                dups += 1
            seen.add(c.prompt_sha)
        return dups

    @property
    def gpu_minutes(self) -> float:
        return sum(c.duration_ms for c in self.calls) / 60000.0

    @property
    def reply_chars(self) -> int:
        return sum(c.response_chars for c in self.calls)

    @property
    def prompt_chars(self) -> int:
        return sum(c.prompt_chars for c in self.calls)

    @property
    def wall_minutes(self) -> float:
        if not (self.ts_start and self.ts_end):
            return 0.0
        try:
            t0 = datetime.fromisoformat(self.ts_start)
            t1 = datetime.fromisoformat(self.ts_end)
        except ValueError:
            return 0.0
        return (t1 - t0).total_seconds() / 60.0

    def dispositions(self) -> str:
        c = Counter(str(v.get("disposition")) for v in self.verdicts)
        return ",".join(f"{k}x{n}" if n > 1 else k for k, n in c.most_common()) or "-"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "cluster": self.cluster, "thread": self.thread,
            "route": self.route, "unit": self.unit, "iter": self.iter_no,
            "pass_n": self.pass_n, "class": self.fill_class,
            "ts_start": self.ts_start, "ts_end": self.ts_end,
            "terminal": self.terminal, "terminal_reason": self.terminal_reason,
            "calls": self.n_calls, "dup_calls": self.dup_calls,
            "gpu_min": round(self.gpu_minutes, 1),
            "wall_min": round(self.wall_minutes, 1),
            "prompt_chars": self.prompt_chars, "reply_chars": self.reply_chars,
            "retro_retries": self.retro_retries, "notes_added": self.notes_added,
            "verdicts": self.dispositions(),
            "callers": sorted({c.caller for c in self.calls if c.caller}),
            "prompt_shas": [c.prompt_sha for c in self.calls],
            "response_shas": [c.response_sha for c in self.calls],
        }


def iter_events(path: Path, run_id: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id and d.get("run_id") != run_id:
                continue
            rows.append(d)
    return rows


def latest_run_id(path: Path) -> str | None:
    last = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rid = json.loads(line).get("run_id")
            except json.JSONDecodeError:
                continue
            if rid:
                last = str(rid)
    return last


def build_fill_table(events_path: Path | str = EVENTS, run_id: str | None = None,
                     cluster: str | None = None) -> list[Fill]:
    """The per-fill table for one run: segment each thread on its openers and
    attach every model call, verdict and class event that lands inside.

    This is the function the notebook imports; the CLI below only renders it.
    """
    path = Path(events_path)
    rid = run_id or latest_run_id(path)
    rows = iter_events(path, rid)

    open_fill: dict[str, Fill] = {}       # thread -> the fill currently open on it
    pending: dict[str, Call] = {}         # prompt_sha -> request awaiting its reply
    last_iter: dict[str, int] = {}        # cluster -> last observed tdd iter number
    out: list[Fill] = []

    for d in rows:
        event = str(d.get("event") or "")
        data = d.get("data") or {}
        thread = str(d.get("thread"))
        ts = str(d.get("ts") or "")

        if event == "phase_c.tdd.iter":
            cl, it = data.get("cluster"), data.get("iter")
            if isinstance(cl, str) and isinstance(it, int):
                last_iter[cl] = it
            continue

        if event in OPENERS:
            prev = open_fill.pop(thread, None)
            if prev is not None:
                out.append(prev)
            cl = str(data.get("cluster") or "")
            f = Fill(
                run_id=str(d.get("run_id") or ""), cluster=cl, thread=thread,
                route=OPENERS[event], unit=_unit_of(event, data),
                iter_no=last_iter.get(cl), pass_n=data.get("pass_n"),
                ts_start=ts, ts_end=ts,
            )
            open_fill[thread] = f
            continue

        f = open_fill.get(thread)
        if f is None:
            continue
        f.ts_end = ts

        if event == "llm.request":
            prompt = data.get("prompt") or {}
            call = Call(
                prompt_sha=str(data.get("prompt_sha") or prompt.get("sha256") or ""),
                caller=str(data.get("caller") or ""),
                prompt_chars=int(prompt.get("char_count") or 0),
                ts=ts,
            )
            f.calls.append(call)
            if call.prompt_sha:
                pending[call.prompt_sha] = call
        elif event in ("llm.response", "llm.error"):
            sha = str(data.get("prompt_sha") or "")
            call = pending.pop(sha, None) or (f.calls[-1] if f.calls else None)
            if call is not None:
                call.duration_ms = int(d.get("duration_ms") or 0)
                if event == "llm.response":
                    resp = data.get("response") or {}
                    call.response_sha = str(resp.get("sha256") or "")
                    call.response_chars = int(resp.get("char_count") or 0)
                else:
                    call.error = f"{data.get('error_type')}: {str(data.get('error_msg'))[:120]}"
        elif event == "phase_c.verdict":
            f.verdicts.append(dict(data))
        elif event == "phase_c.fill.class":
            f.fill_class = str(data.get("class") or "-")
            unit = data.get("unit")
            if unit:
                f.unit = str(unit)
        elif event in ("phase_c.fn.retro_retry", "phase_c.impl.retro_retry"):
            f.retro_retries += 1
        elif event == "phase_c.note.added":
            f.notes_added += 1
        elif event in TERMINALS:
            f.terminal = event.rsplit(".", 1)[-1]
            reason = data.get("reason") or data.get("outcome")
            if reason:
                f.terminal_reason = str(reason)[:80]

    out.extend(open_fill.values())
    out.sort(key=lambda x: x.ts_start)
    if cluster:
        out = [f for f in out if f.cluster == cluster]
    return out


def render_summary(fills: list[Fill]) -> None:
    by_class: dict[str, list[Fill]] = {}
    for f in fills:
        by_class.setdefault(f.fill_class, []).append(f)
    total_gpu = sum(f.gpu_minutes for f in fills)
    print(f"{len(fills)} fill(s), {sum(f.n_calls for f in fills)} model call(s), "
          f"{total_gpu:.1f} gpu_min")
    print(f"  {'class':<12}{'fills':>6}{'calls':>7}{'dup':>5}{'gpu_min':>9}"
          f"{'share':>7}  routes")
    order = sorted(by_class.items(), key=lambda kv: -sum(f.gpu_minutes for f in kv[1]))
    for cls, group in order:
        gpu = sum(f.gpu_minutes for f in group)
        share = (gpu / total_gpu * 100) if total_gpu else 0.0
        routes = ",".join(sorted({f.route for f in group}))
        print(f"  {cls:<12}{len(group):>6}{sum(f.n_calls for f in group):>7}"
              f"{sum(f.dup_calls for f in group):>5}{gpu:>9.1f}{share:>6.0f}%  {routes}")


def render_rows(fills: list[Fill]) -> None:
    print(f"{'start':<9}{'iter':>5} {'route':<8}{'class':<12}{'calls':>6}{'dup':>4}"
          f"{'gpu_min':>8}{'reply':>7}  {'verdicts':<22}unit")
    for f in fills:
        print(f"{f.ts_start[11:19]:<9}{str(f.iter_no or '-'):>5} {f.route:<8}"
              f"{f.fill_class:<12}{f.n_calls:>6}{f.dup_calls:>4}{f.gpu_minutes:>8.1f}"
              f"{f.reply_chars:>7}  {f.dispositions()[:20]:<22}{f.unit}")


def render_open(fills: list[Fill], head: int) -> None:
    for f in fills:
        print("=" * 100)
        print(f"{f.unit}   route={f.route} class={f.fill_class} cluster={f.cluster} "
              f"iter={f.iter_no} thread={f.thread}")
        print(f"  {f.ts_start} -> {f.ts_end}  wall {f.wall_minutes:.1f}m  "
              f"gpu {f.gpu_minutes:.1f}m  terminal={f.terminal or 'open'} "
              f"{f.terminal_reason}")
        for v in f.verdicts:
            print(f"  verdict: {v.get('disposition')}/{v.get('outcome')} "
                  f"{str(v.get('unit'))} — {str(v.get('reason'))[:110]}")
        for i, c in enumerate(f.calls, 1):
            think = c.thinking_path()
            tsize = think.stat().st_size if think.exists() else 0
            print(f"  call {i}: {c.prompt_sha[:12]} -> {c.response_sha[:12] or '(none)'} "
                  f"prompt={c.prompt_chars} reply={c.response_chars} "
                  f"think={tsize} {int(c.duration_ms / 1000)}s")
            print(f"    builder: {c.caller or '(unrecorded)'}")
            if c.error:
                print(f"    ERROR: {c.error}")
            if head:
                if c.prompt_path().exists():
                    text = c.prompt_path().read_text(encoding="utf-8", errors="replace")
                    print("    --- prompt head ---")
                    print("    " + text[:head].replace("\n", "\n    "))
                if c.response_path().exists():
                    text = c.response_path().read_text(encoding="utf-8", errors="replace")
                    print("    --- reply ---")
                    print("    " + text.replace("\n", "\n    "))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--events", default=str(EVENTS))
    ap.add_argument("--run", default=None, help="run id; default: last in the file")
    ap.add_argument("--cluster", default=None)
    ap.add_argument("--route", default=None)
    ap.add_argument("--class", dest="cls", default=None)
    ap.add_argument("--unit-contains", default=None)
    ap.add_argument("--min-calls", type=int, default=0)
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--open", default=None, metavar="UNIT_SUBSTRING")
    ap.add_argument("--head", type=int, default=0, help="chars of each prompt to print with --open")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    path = Path(args.events)
    if not path.exists():
        print(f"events file not found: {path}", file=sys.stderr)
        return 2
    run_id = args.run or latest_run_id(path)
    fills = build_fill_table(path, run_id, args.cluster)
    if args.route:
        fills = [f for f in fills if f.route == args.route]
    if args.cls:
        fills = [f for f in fills if f.fill_class == args.cls]
    if args.unit_contains:
        fills = [f for f in fills if args.unit_contains in f.unit]
    if args.min_calls:
        fills = [f for f in fills if f.n_calls >= args.min_calls]

    print(f"run {run_id}  events {path}  {len(fills)} fill(s) after filters")
    render_summary(fills)
    if args.open:
        render_open([f for f in fills if args.open in f.unit], args.head)
    elif not args.summary_only:
        print()
        render_rows(fills)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([f.as_dict() for f in fills], indent=1), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

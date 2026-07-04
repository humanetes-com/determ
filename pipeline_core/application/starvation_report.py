"""Starvation miner core (july_plan P1) — correlate fill failures with the
LLM exchanges that produced them, classify each, rank the causes.

Input is the run's event stream (`events.jsonl` dicts) plus a blob reader over
the content-addressed prompt store. Correlation is deterministic: a fill-fail
event carries (tid, ts, duration_ms); its exchanges are the same-tid
llm.request/llm.response pairs inside that window whose PROMPT mentions the
unit's fn (fn fails) or any container token (impl fails). The LAST matching
exchange is the attempt that died — that is what gets classified.

Pure over injected callables (`read_blob`); the CLI in
`tools/starvation_miner.py` binds the real files.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pipeline_core.domain.starvation import (
    FillPostMortem,
    StarvationCause,
    classify_starvation,
    has_word,
)

_FAIL_EVENTS = ("phase_c.fn.fill.fail", "phase_c.impl.fill.fail")
_WINDOW_SLACK_S = 5.0


@dataclass(frozen=True, slots=True)
class Exchange:
    """One paired llm.request/llm.response on a thread."""

    ts: float
    tid: int
    prompt_sha: str
    response_sha: str | None


def _ts(e: dict[str, Any]) -> float:
    return datetime.fromisoformat(e["ts"]).timestamp()


def _sha(e: dict[str, Any], key: str) -> str | None:
    blob = (e.get("data") or {}).get(key) or {}
    return blob.get("sha256")


def collect_exchanges(events: Iterable[dict[str, Any]], run_id: str,
                      ) -> tuple[list[Exchange], list[dict[str, Any]]]:
    """One pass over the stream: (paired exchanges, fill-fail events) for
    `run_id`. A response pairs with the latest unanswered request on its tid."""
    pending: dict[int, tuple[float, str]] = {}
    exchanges: list[Exchange] = []
    fails: list[dict[str, Any]] = []
    for e in events:
        if e.get("run_id") != run_id:
            continue
        kind = e.get("event")
        tid = int(e.get("tid") or 0)
        if kind == "llm.request":
            sha = _sha(e, "prompt")
            if sha:
                if tid in pending:  # request never answered — keep it visible
                    ts0, sha0 = pending.pop(tid)
                    exchanges.append(Exchange(ts0, tid, sha0, None))
                pending[tid] = (_ts(e), sha)
        elif kind == "llm.response":
            if tid in pending:
                ts0, sha0 = pending.pop(tid)
                exchanges.append(Exchange(ts0, tid, sha0, _sha(e, "response")))
        elif kind in _FAIL_EVENTS:
            fails.append(e)
    for tid, (ts0, sha0) in pending.items():
        exchanges.append(Exchange(ts0, tid, sha0, None))
    exchanges.sort(key=lambda x: x.ts)
    return exchanges, fails


def _unit_tokens(fail: dict[str, Any]) -> tuple[str, list[str]]:
    """(unit label, tokens any of which must appear in the exchange's prompt)."""
    d = fail.get("data") or {}
    file_rel = d.get("file", "")
    if fail.get("event") == "phase_c.fn.fill.fail":
        fn = d.get("fn", "")
        return f"{file_rel}::{fn}", [fn]
    container = d.get("container", "")
    words = [w for w in container.replace("<", " ").replace(">", " ")
             .replace(",", " ").replace("&", " ").split()
             if len(w) > 1 and w not in ("impl", "for", "where", "trait", "mod")
             and not w.startswith("'") and w.replace("_", "a").isalnum()]
    return f"{file_rel}::{container.splitlines()[0][:60]}", words or [container]


def post_mortem(fail: dict[str, Any], exchanges: list[Exchange],
                read_blob: Callable[[str], str | None]) -> FillPostMortem:
    """Classify one fill failure via its LAST matching exchange."""
    d = fail.get("data") or {}
    unit, tokens = _unit_tokens(fail)
    end = _ts(fail)
    start = end - (fail.get("duration_ms") or 0) / 1000.0 - _WINDOW_SLACK_S
    tid = int(fail.get("tid") or 0)
    prompt_text = ""
    response_text = ""
    for ex in exchanges:
        if ex.tid != tid or not (start <= ex.ts <= end):
            continue
        blob = read_blob(ex.prompt_sha) or ""
        if blob and any(has_word(blob, t) for t in tokens):
            prompt_text = blob
            response_text = (read_blob(ex.response_sha) or ""
                             if ex.response_sha else "")
    error_text = d.get("last_error_preview") or ""
    cause, evidence = (
        classify_starvation(prompt_text, response_text, error_text,
                            container=d.get("container", ""))
        if prompt_text else (StarvationCause.UNKNOWN, "no exchange correlated"))
    return FillPostMortem(
        unit=unit, cluster=d.get("cluster", ""),
        attempts=int(d.get("fix_attempts") or 0),
        cause=cause, evidence=evidence)


def mine_run(events: Iterable[dict[str, Any]], run_id: str,
             read_blob: Callable[[str], str | None]) -> list[FillPostMortem]:
    exchanges, fails = collect_exchanges(events, run_id)
    return [post_mortem(f, exchanges, read_blob) for f in fails]


def render_report(pms: list[FillPostMortem], run_id: str) -> str:
    """Ranked cause table + per-unit rows (compact, log-friendly)."""
    lines = [f"starvation report — run {run_id} — {len(pms)} failed fill(s)"]
    counts = Counter(pm.cause for pm in pms)
    for cause, n in counts.most_common():
        lines.append(f"  {cause:<16} {n}")
    lines.append("")
    for pm in sorted(pms, key=lambda p: (p.cause, p.unit)):
        lines.append(f"  [{pm.cause}] {pm.cluster}:{pm.unit} "
                     f"(attempts={pm.attempts}) — {pm.evidence[:100]}")
    return "\n".join(lines)

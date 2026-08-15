"""Live engine gauges for the monitor's Pipeline console — vLLM's own metrics.

WHY THIS EXISTS. Every number the Pipeline panel carried moved on the pipeline's
timescale, not the operator's: fills tick every few minutes, the sweep score
every few HOURS. On run 669347-cf33eae3 (2026-08-15) the panel showed the same
`69/69 frontier fn(s)` for the whole of a 46-minute iteration with four workers
generating the entire time — a healthy run and a hung one render identically,
which is the opposite of what a console is for. The engine has second-by-second
truth about itself and publishes it; this module reads it.

WHAT IT READS. vLLM's Prometheus endpoint, on the same host:port the health
check already uses (:8000, `VLLM_METRICS_URL` overrides). Measured cost on this
box, 2026-08-15, against the Muse-Glimmer-30B server: 66 KB in 0.13 s. Sampled
at most once a second and cached, so a 2 s render loop scrapes once per frame.

GAUGES VS COUNTERS, and why the distinction is visible in the output.
`num_requests_running`, `num_requests_waiting` and `kv_cache_usage_perc` are
gauges — read straight. `generation_tokens_total`, `prompt_tokens_total`,
`prompt_tokens_cached_total`, `num_preemptions_total` and
`request_success_total` are monotone counters, so what the panel shows is their
RATE, from the delta against the previous sample. A server restart resets them:
a negative delta yields no reading at all rather than a fabricated rate, on the
same rule the rest of this panel follows — never substitute a plausible number
for a missing one.
"""

from __future__ import annotations

import os
import urllib.request
from collections.abc import Callable
from typing import Any

from pipeline_core.monitor.render import bold, dim, green, progress_bar, red, yellow
from pipeline_core.monitor.run_data import read_backend

__all__ = ["engine_rows", "engine_sample", "parse_metrics"]

METRICS_URL: str = os.getenv("VLLM_METRICS_URL", "http://localhost:8000/metrics")

# The series the console reads. Everything else in the 630-line scrape is
# histogram buckets we have no room to render honestly.
_GAUGES = {
    "running": ("vllm:num_requests_running",),
    "waiting": ("vllm:num_requests_waiting",),
    # vLLM renamed this between server versions and this box has run both:
    # 0.20.x served `gpu_cache_usage_perc`, the current image serves
    # `kv_cache_usage_perc`. Accept either rather than showing a blank gauge
    # after an image bump.
    "kv": ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
}
_COUNTERS = {
    "gen_tokens": ("vllm:generation_tokens_total",),
    "prompt_tokens": ("vllm:prompt_tokens_total",),
    "cached_tokens": ("vllm:prompt_tokens_cached_total",),
    "preemptions": ("vllm:num_preemptions_total",),
}
# request_success_total is split by finished_reason; `length` is the one that
# matters here — it counts generations the server truncated at max_tokens, which
# in this pipeline is a body that arrives syntactically incomplete.
_SUCCESS = "vllm:request_success_total"

_MIN_SAMPLE_INTERVAL = 1.0

# (ts, counters) of the previous scrape — the only state, and the reason rates
# exist at all.
_prev: tuple[float, dict[str, float]] | None = None
_cache: tuple[float, dict[str, Any]] = (0.0, {})


def _series_name(line: str) -> str:
    """Metric name of a Prometheus sample line, without its `{labels}`."""
    cut = line.find("{")
    if cut < 0:
        cut = line.find(" ")
    return line[:cut] if cut > 0 else line


def _label(line: str, key: str) -> str:
    """Value of one `key="value"` label on a sample line, or "" (regex-free)."""
    needle = f'{key}="'
    i = line.find(needle)
    if i < 0:
        return ""
    j = i + len(needle)
    k = line.find('"', j)
    return line[j:k] if k > j else ""


def parse_metrics(text: str) -> dict[str, float]:
    """Prometheus exposition text → the handful of numbers the console shows.

    Values are SUMMED across label sets: a multi-engine server publishes one
    sample per engine, and the console reports the box, not the shard. Keys are
    the internal names of ``_GAUGES``/``_COUNTERS`` plus ``finish_stop`` and
    ``finish_length`` from the finished_reason split. Absent series are simply
    absent from the result — never zero, which would read as "measured, idle".
    """
    wanted: dict[str, str] = {}
    for metric, series in {**_GAUGES, **_COUNTERS}.items():
        for series_name in series:
            wanted[series_name] = metric
    out: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = _series_name(line)
        key: str | None = wanted.get(name)
        if key is None and name != _SUCCESS:
            continue
        value_str = line.rpartition(" ")[2]
        try:
            value = float(value_str)
        except ValueError:
            continue
        if name == _SUCCESS:
            reason = _label(line, "finished_reason")
            if reason not in ("stop", "length"):
                continue
            key = f"finish_{reason}"
        if key is None:
            continue
        # First name wins per key: the two kv_cache spellings are alternatives,
        # not addends, and a server that somehow published both would otherwise
        # read as 120% cache use.
        if key == "kv" and key in out:
            continue
        out[key] = out.get(key, 0.0) + value
    return out


def _fetch() -> str:
    with urllib.request.urlopen(METRICS_URL, timeout=0.5) as resp:  # noqa: S310 — fixed localhost URL
        raw: bytes = resp.read()
    return raw.decode("utf-8", errors="replace")


def engine_sample(now: float, fetch: Callable[[], str] | None = None) -> dict[str, Any]:
    """One scrape, with per-second rates derived against the previous scrape.

    Returns {} when the backend is not vLLM or the endpoint does not answer —
    the vLLM health row above already says which of the two it is, and an
    engine row full of zeros would claim an idle server rather than no reading.

    Rates appear only on the SECOND call and only for counters that moved
    forward: `gen_tps`, `prompt_tps`, `capped_rate` (generations truncated at
    max_tokens per minute) and `preempt_rate`. `hit_pct` is cumulative by
    construction (cached prompt tokens over all prompt tokens since the server
    started) and is labeled as such where it is rendered.
    """
    global _prev, _cache
    if now - _cache[0] < _MIN_SAMPLE_INTERVAL:
        return _cache[1]
    try:
        text = (fetch or _fetch)()
    except Exception:
        _cache = (now, {})
        return {}
    m = parse_metrics(text)
    if not m:
        _cache = (now, {})
        return {}

    out: dict[str, Any] = {
        "running": m.get("running"),
        "waiting": m.get("waiting"),
        "kv_pct": (m["kv"] * 100.0) if "kv" in m else None,
        "finish_stop": m.get("finish_stop"),
        "finish_length": m.get("finish_length"),
    }
    prompt_total = m.get("prompt_tokens")
    cached_total = m.get("cached_tokens")
    if prompt_total and cached_total is not None:
        out["hit_pct"] = cached_total / prompt_total * 100.0

    counters = {k: m[k] for k in (*_COUNTERS, "finish_length") if k in m}
    prev = _prev
    if prev is not None:
        dt = now - prev[0]
        if dt >= _MIN_SAMPLE_INTERVAL:
            for key, rate_key, per in (("gen_tokens", "gen_tps", 1.0),
                                       ("prompt_tokens", "prompt_tps", 1.0),
                                       ("finish_length", "capped_rate", 60.0),
                                       ("preemptions", "preempt_rate", 60.0)):
                before, after = prev[1].get(key), counters.get(key)
                if before is None or after is None or after < before:
                    continue          # counter reset (server restart) — no reading
                out[rate_key] = (after - before) / dt * per
    if prev is None or now - prev[0] >= _MIN_SAMPLE_INTERVAL:
        _prev = (now, counters)
    _cache = (now, out)
    return out


def _tok(n: float) -> str:
    """Token counts the width of a console column: 812, 1.4k, 18k."""
    if n < 1000:
        return f"{n:.0f}"
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1000:.0f}k"


def engine_rows(now: float, fetch: Callable[[], str] | None = None) -> list[str]:
    """The engine console rows, or [] when there is no reading to show."""
    if read_backend() != "vllm":
        return []
    s = engine_sample(now, fetch)
    if not s:
        return []
    rows: list[str] = []

    kv = s.get("kv_pct")
    # KV pressure is the one gauge with a hard ceiling: at 100% the scheduler
    # starts preempting, which shows up in the row below as a nonzero rate.
    kv_seg = (f"[{progress_bar(kv, 10)}] KV {kv:3.0f}%" if kv is not None
              else dim("KV ?"))
    running, waiting = s.get("running"), s.get("waiting")
    busy = (f"{bold(f'{running:.0f}')} busy" if running is not None else "? busy")
    queued = ((red(f"{waiting:.0f} queued") if waiting else dim("0 queued"))
              if waiting is not None else dim("? queued"))
    gen = s.get("gen_tps")
    pro = s.get("prompt_tps")
    flow = [green(f"gen {_tok(gen)} tok/s") if gen is not None
            else dim("gen — tok/s (first sample)")]
    if pro is not None:
        flow.append(dim(f"prompt {_tok(pro)} tok/s"))
    rows.append(f"           engine   {kv_seg}  {busy} · {queued}   {'  '.join(flow)}")

    tail: list[str] = []
    hit = s.get("hit_pct")
    if hit is not None:
        tail.append(f"prefix-cache {hit:.0f}%")
    stop, length = s.get("finish_stop"), s.get("finish_length")
    if stop is not None and length is not None and (stop + length) > 0:
        pct = length / (stop + length) * 100
        # A generation that stopped because it hit max_tokens is a body cut off
        # mid-token, and the fill that follows it is repairing an artefact of the
        # budget rather than of the model. Worth a colour when it is common.
        col = yellow if pct >= 20 else dim
        tail.append(col(f"length-capped {length:.0f}/{stop + length:.0f} ({pct:.0f}%)"))
    capped_rate = s.get("capped_rate")
    if capped_rate:
        tail.append(yellow(f"+{capped_rate:.1f}/min now"))
    preempt = s.get("preempt_rate")
    if preempt:
        tail.append(red(f"preempting {preempt:.1f}/min"))
    if tail:
        rows.append(f"           {dim('since server start')}  {'  '.join(tail)}")
    return rows

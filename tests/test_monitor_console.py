"""The Pipeline console's live gauges: the score bar, the vLLM engine row, the
per-worker stream rows, and the Generation box's choice of file.

All four exist because of one measured complaint about run 669347-cf33eae3
(2026-08-15): the Pipeline panel's only motion was a sweep row pinned at
`69/69` while four workers generated for 46 minutes. Each test below pins the
reading that was missing AND the reading that must never be invented.
"""
from __future__ import annotations

import os
import time
from typing import Any

from pipeline_core.domain.reasoning_cap import CallMark, CutRate, CutStopKnobs
from pipeline_core.monitor import engine_metrics as _em
from pipeline_core.monitor import generation_panel as _gp
from pipeline_core.monitor import render as _render
from pipeline_core.monitor import worker_streams as _ws

# ── score bar ────────────────────────────────────────────────────────────────


def test_score_row_draws_against_the_counted_oracle() -> None:
    """The one quantity the run exists to move had no gauge. It is drawn
    against `oracle_size` (`pass + fail`, what the sweep counted), with the
    gap to `oracle_static` named beside it rather than folded in: on
    669347-cf33eae3 a crash took ten test binaries down and 4 of 107 declared
    tests are outside the count.

    This reading predates the binary-qualified pair vectors, so the gap cannot
    be split by cause — and the row says so by reporting it whole instead of
    guessing a cause. The split itself is pinned on recorded events in
    tests/test_monitor_iteration_steps.py."""
    state = {"tdd_pass": 9, "tdd_fail": 94, "tdd_oracle": 103,
             "tdd_oracle_static": 107, "tdd_pass_hist": [39, 9]}
    row = _render.score_row(state)
    assert row is not None
    text = _render.plain(row)
    assert "9/103 passing (9%)" in text
    assert "↓30" in text                    # signed against the previous sweep
    assert "94 failing" in text
    assert "9/107" not in text              # never the declared oracle
    # The shortfall is its own row, below the score.
    gap = _render.plain(_render.oracle_row(state) or "")
    assert "107 declared" in gap and "4 outside the count" in gap
    assert "unreachable" not in text + gap  # named the crash's OWN casualties,
                                            # which are counted, as failing


def test_score_row_absent_without_a_reading() -> None:
    """No oracle → no row. A bar over a guessed total is the defect this panel
    keeps being fixed for."""
    assert _render.score_row({"tdd_pass": 9, "tdd_oracle": None}) is None
    assert _render.score_row({"tdd_pass": None, "tdd_oracle": 103}) is None


# ── engine (vLLM /metrics) ───────────────────────────────────────────────────

_METRICS = """\
# HELP vllm:num_requests_running Number of requests currently running.
vllm:num_requests_running{engine="0",model_name="m"} 4.0
vllm:num_requests_waiting{engine="0",model_name="m"} 2.0
vllm:kv_cache_usage_perc{engine="0",model_name="m"} 0.6033
vllm:generation_tokens_total{engine="0",model_name="m"} 1000.0
vllm:prompt_tokens_total{engine="0",model_name="m"} 4000.0
vllm:prompt_tokens_cached_total{engine="0",model_name="m"} 1000.0
vllm:num_preemptions_total{engine="0",model_name="m"} 0.0
vllm:request_success_total{engine="0",finished_reason="stop",model_name="m"} 544.0
vllm:request_success_total{engine="0",finished_reason="length",model_name="m"} 157.0
vllm:request_success_total{engine="0",finished_reason="abort",model_name="m"} 3.0
"""


def test_parse_metrics_reads_the_series_the_console_shows() -> None:
    m = _em.parse_metrics(_METRICS)
    assert m["running"] == 4.0 and m["waiting"] == 2.0
    assert m["kv"] == 0.6033
    assert m["gen_tokens"] == 1000.0
    # finished_reason split: only stop/length; abort is neither and is dropped
    # rather than summed into one of them.
    assert m["finish_stop"] == 544.0 and m["finish_length"] == 157.0
    assert sum(v for k, v in m.items() if k.startswith("finish_")) == 701.0


def test_parse_metrics_sums_engines_and_accepts_either_kv_spelling() -> None:
    two_engines = (
        'vllm:num_requests_running{engine="0"} 3.0\n'
        'vllm:num_requests_running{engine="1"} 2.0\n'
        # The pre-rename spelling this box served until the image was bumped.
        'vllm:gpu_cache_usage_perc{engine="0"} 0.5\n'
    )
    m = _em.parse_metrics(two_engines)
    assert m["running"] == 5.0        # summed across shards
    assert m["kv"] == 0.5             # alternative name accepted
    # Both spellings present → the first wins; they are alternatives, and
    # summing them would report 120% cache use.
    m2 = _em.parse_metrics(two_engines + 'vllm:kv_cache_usage_perc{engine="0"} 0.5\n')
    assert m2["kv"] == 0.5


def _reset_engine_state() -> None:
    _em._prev = None
    _em._cache = (0.0, {})


def test_engine_sample_reports_rates_only_from_a_real_delta() -> None:
    """Counters are shown as rates, and a rate needs two samples. The first
    sample carries none — the alternative is dividing a lifetime counter by an
    uptime nobody measured."""
    _reset_engine_state()
    first = _em.engine_sample(1000.0, fetch=lambda: _METRICS)
    assert first["running"] == 4.0 and first["kv_pct"] == 60.33
    assert "gen_tps" not in first
    assert first["hit_pct"] == 25.0          # 1000 cached of 4000 prompt tokens

    advanced = _METRICS.replace(
        'vllm:generation_tokens_total{engine="0",model_name="m"} 1000.0',
        'vllm:generation_tokens_total{engine="0",model_name="m"} 3000.0')
    second = _em.engine_sample(1010.0, fetch=lambda: advanced)
    assert second["gen_tps"] == 200.0        # 2000 tokens in 10 s


def test_engine_sample_survives_a_server_restart_without_inventing_a_rate() -> None:
    """A restarted server's counters go backwards. That is not a negative
    generation rate; it is no reading."""
    _reset_engine_state()
    _em.engine_sample(1000.0, fetch=lambda: _METRICS)
    reset = _METRICS.replace(
        'vllm:generation_tokens_total{engine="0",model_name="m"} 1000.0',
        'vllm:generation_tokens_total{engine="0",model_name="m"} 12.0')
    after = _em.engine_sample(1010.0, fetch=lambda: reset)
    assert "gen_tps" not in after
    assert after["running"] == 4.0           # gauges still read


def test_engine_rows_are_empty_when_the_endpoint_says_nothing() -> None:
    """No answer → no row. Zeros here would read as an idle server, which is a
    different diagnosis from an unreachable one (the vLLM health row above
    already distinguishes them)."""
    _reset_engine_state()

    def _boom() -> str:
        raise OSError("connection refused")

    assert _em.engine_rows(1000.0, fetch=_boom) == []
    _reset_engine_state()
    assert _em.engine_rows(1000.0, fetch=lambda: "") == []


def test_engine_rows_render_the_live_gauges(monkeypatch: Any) -> None:
    _reset_engine_state()
    monkeypatch.setattr(_em, "read_backend", lambda: "vllm")
    _em.engine_rows(1000.0, fetch=lambda: _METRICS)
    advanced = _METRICS.replace(
        'vllm:generation_tokens_total{engine="0",model_name="m"} 1000.0',
        'vllm:generation_tokens_total{engine="0",model_name="m"} 3000.0')
    text = _render.plain("\n".join(_em.engine_rows(1010.0, fetch=lambda: advanced)))
    assert "KV  60%" in text
    assert "4 busy" in text and "2 queued" in text
    assert "gen 200 tok/s" in text
    # Truncated generations are the pipeline's own pathology, not trivia: a
    # body cut off at max_tokens is repaired as if the model wrote it wrong.
    assert "length-capped 157/701 (22%)" in text


# ── per-worker stream rows ───────────────────────────────────────────────────


def _write(path: str, text: str, mtime: float) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.utime(path, (mtime, mtime))


def test_worker_snapshots_read_the_per_thread_files_not_the_base(tmp_path: Any) -> None:
    """The transports write one stream log per thread and the panels read only
    the base file — the whole defect in one line. Under the parallel engine the
    base file is the idle main thread's."""
    _ws._prev.clear()
    now = 1_000_000.0
    base = str(tmp_path / "stream.log")
    _write(base, "[CALL_START: 999000.0]\nold main-thread call\n", now - 2800)
    _write(str(tmp_path / "stream.impl-fill-stable-w1.log"),
           f"[CALL_START: {now - 120:.3f}]\n[CALL: m]\n<think>x</think>[CONTENT]body",
           now - 3)
    _write(str(tmp_path / "stream.impl-fill-stable-w2.log"),
           f"[CALL_START: {now - 60:.3f}]\n[CALL: m]\n<think>still thinking",
           now - 1)
    snaps = _ws.worker_snapshots(base_log=base, now=now)
    assert [s["thread"] for s in snaps] == ["impl-fill-stable-w2",
                                            "impl-fill-stable-w1"]  # freshest first
    assert snaps[0]["phase"] == "THINKING" and snaps[1]["phase"] == "CODE"
    assert round(snaps[1]["elapsed_s"]) == 120
    assert snaps[0]["rate_cps"] is None      # first sighting: no rate yet
    # The base file is never a worker row — it is the main thread, and counting
    # it as a worker is how a 46-minute-old call read as live generation.
    assert all("stream" not in str(s["thread"]) for s in snaps)


def test_worker_snapshots_rate_and_truncation(tmp_path: Any) -> None:
    """Size delta is the live rate. The transport truncates the file at each
    call (`open(path, "w")`), so a SHRINK is a new call, never a negative rate."""
    _ws._prev.clear()
    now = 2_000_000.0
    base = str(tmp_path / "stream.log")
    path = str(tmp_path / "stream.w0.log")
    _write(path, "x" * 1000, now)
    _ws.worker_snapshots(base_log=base, now=now)
    _write(path, "x" * 3000, now + 2)
    snap = _ws.worker_snapshots(base_log=base, now=now + 2)[0]
    assert snap["rate_cps"] == 1000.0        # 2000 chars in 2 s
    _write(path, "y" * 50, now + 4)          # next call truncated it
    assert _ws.worker_snapshots(base_log=base, now=now + 4)[0]["rate_cps"] is None


def test_worker_snapshots_drop_yesterdays_threads(tmp_path: Any) -> None:
    """A thread's file outlives the thread. Only files written inside the
    active window are live workers."""
    _ws._prev.clear()
    now = 3_000_000.0
    base = str(tmp_path / "stream.log")
    _write(str(tmp_path / "stream.w0.log"), "old", now - 4000)
    assert _ws.worker_snapshots(base_log=base, now=now) == []


def test_worker_rows_bar_only_against_a_real_budget(tmp_path: Any) -> None:
    snaps = [{"thread": "impl-fill-stable-w1", "chars": 33_000, "age_s": 2.0,
              "elapsed_s": 190.0, "phase": "CODE", "rate_cps": 140.0}]
    units = {"impl-fill-stable-w1": {"label": "impl Display for Error\nwhere T: Debug",
                                     "kind": "impl", "refill": True}}
    text = _render.plain("\n".join(_ws.worker_rows(
        snaps, units_by_thread=units, cpt=3.3, request_max=16384, width=140)))
    assert "impl Display for Error" in text and "where" not in text   # first line only
    assert "↻" in text and "CODE" in text
    assert "≈10,000 tok" in text         # 33,000 chars at the measured cpt, an estimate
    assert " 61%" in text                # of the request's own max_tokens
    assert "42 tok/s" in text
    assert "3m10s" in text
    # The bar divides by THIS thread's budget, not by the last one any thread
    # was granted: four workers hold four requests at once.
    per_thread = _render.plain("\n".join(_ws.worker_rows(
        snaps, units_by_thread=units, cpt=3.3, request_max=16384,
        budget_by_thread={"impl-fill-stable-w1": 8192}, width=140)))
    assert "100%" in per_thread and "≈at cap" in per_thread
    assert " 61%" not in per_thread
    # No budget reading → no bar, and the count still shows.
    plain_nobudget = _render.plain("\n".join(_ws.worker_rows(
        snaps, units_by_thread=units, cpt=3.3, request_max=None, width=140)))
    assert "≈10,000 tok" in plain_nobudget
    assert "█" not in plain_nobudget and "░" not in plain_nobudget


def test_worker_rows_flag_silence_not_duration() -> None:
    """A 20-minute call writing a token a second is healthy; a 40-second
    silence is the signal. The row says which one it is looking at."""
    quiet = [{"thread": "w1", "chars": 100, "age_s": 95.0, "elapsed_s": 1200.0,
              "phase": "CODE", "rate_cps": 0.0}]
    text = _render.plain("\n".join(_ws.worker_rows(quiet, width=140)))
    assert "quiet 1m35s" in text
    busy = [{**quiet[0], "age_s": 2.0}]
    assert "quiet" not in _render.plain("\n".join(_ws.worker_rows(busy, width=140)))


def test_worker_rows_label_columns_line_up() -> None:
    """Padding a colored string pads its escape bytes, so the columns after a
    short label used to shift. The visible width is what must be constant."""
    snaps = [{"thread": "w1", "chars": 100, "age_s": 1.0, "elapsed_s": 10.0,
              "phase": "CODE", "rate_cps": 10.0},
             {"thread": "w2", "chars": 100, "age_s": 1.0, "elapsed_s": 10.0,
              "phase": "CODE", "rate_cps": 10.0}]
    units = {"w1": {"label": "f", "kind": "fn"},
             "w2": {"label": "a_very_long_container_name_that_runs_on", "kind": "impl"}}
    rows = [_render.plain(r) for r in
            _ws.worker_rows(snaps, units_by_thread=units, width=140)]
    assert rows[0].index("CODE") == rows[1].index("CODE")


# ── Generation box: which file it follows ────────────────────────────────────


def test_generation_follows_the_freshest_stream(tmp_path: Any) -> None:
    """Run 669347-cf33eae3, 16:10 local: the box read `last-tok 46m04s ago` off
    a base log last written at 15:23 while four per-thread logs were seconds
    old. It follows the freshest of the two kinds now, and names the thread —
    so "no new tokens" means the engine is quiet, not that this panel is
    watching an idle thread."""
    _ws._prev.clear()
    now = time.time()
    base = str(tmp_path / "stream.log")
    _write(base, "[CALL_START: 1.0]\n[CALL: m]\nstale main-thread text", now - 2764)
    worker = str(tmp_path / "stream.impl-fill-stable-w0.log")
    _write(worker, f"[CALL_START: {now - 30:.3f}]\n[CALL: m]\n[CONTENT]fresh body text",
           now - 1)
    rows = _gp.generation_rows(stream_log=base, width=120, now=now,
                               runtime={"cpt": 3.3, "max_tokens": 16384, "retries": 0},
                               refresh=2.0, think_cap_chars=40_000)
    head = _render.plain(rows[0])
    assert "[impl-fill-stable-w0]" in head
    assert "last-tok 1s ago" in head
    assert "46m" not in head
    assert any("fresh body text" in _render.plain(r) for r in rows)
    # Nothing fresh anywhere → the base file, exactly as before this existed.
    _ws._prev.clear()
    os.utime(worker, (now - 5000, now - 5000))
    head = _render.plain(_gp.generation_rows(
        stream_log=base, width=120, now=now, runtime={"cpt": 3.3, "max_tokens": None,
                                                      "retries": 0},
        refresh=2.0, think_cap_chars=40_000)[0])
    assert "[main]" in head and "stale" not in head


def test_generation_missing_and_empty_logs(tmp_path: Any) -> None:
    _ws._prev.clear()
    missing = str(tmp_path / "nope" / "stream.log")
    rows = _gp.generation_rows(stream_log=missing, width=120, now=time.time(),
                               runtime={}, refresh=2.0, think_cap_chars=40_000)
    assert _render.plain(rows[0]) == "no stream log yet"
    empty = str(tmp_path / "stream.log")
    _write(empty, "   \n", time.time())
    rows = _gp.generation_rows(stream_log=empty, width=120, now=time.time(),
                               runtime={}, refresh=2.0, think_cap_chars=40_000)
    assert _render.plain(rows[0]) == "stream log empty"


def test_generation_shows_the_cut_rate_gauge_beside_the_live_call(tmp_path: Any) -> None:
    """The gauge moved out of the Pipeline console's coverage/score family
    and into the Generation box, beside the call it narrates (phase=, cpt=,
    elapsed) — the box a viewer actually watches to see "a call is in
    flight". Row 0 stays the header (existing tests pin it); the gauge is
    row 1 when the caller supplies a reading, and absent entirely when it
    does not (the default — every other caller of this box is unaffected)."""
    _ws._prev.clear()
    stream = str(tmp_path / "stream.log")
    _write(stream, "[CALL_START: 1.0]\n[CALL: m]\n[CONTENT]some body text", time.time())
    knobs = CutStopKnobs(share_pct=4.0, window=100)
    rate = CutRate(knobs.window, knobs.share_pct)
    rate = rate.add(CallMark(route="", cut=True, empty=True, prompt_tokens=0))

    with_gauge = _gp.generation_rows(
        stream_log=stream, width=120, now=time.time(),
        runtime={"cpt": 4.0, "max_tokens": None, "retries": 0}, refresh=2.0,
        think_cap_chars=40_000, cut_rate=rate, cut_knobs=knobs)
    assert "llm cut" in _render.plain(with_gauge[1])

    without_gauge = _gp.generation_rows(
        stream_log=stream, width=120, now=time.time(),
        runtime={"cpt": 4.0, "max_tokens": None, "retries": 0}, refresh=2.0,
        think_cap_chars=40_000)
    assert not any("llm cut" in _render.plain(r) for r in without_gauge)


def test_generation_shows_elapsed_even_with_no_known_budget(tmp_path: Any) -> None:
    """A CODE-phase call before vLLM has reported an adaptive budget used to
    render NO progress line at all — "a call is in flight" was visible only
    as the header's `last-tok Xs ago`, which measures token silence, not how
    long the call has been running. It must always show a ticking elapsed
    counter now, with no invented percentage (there is no real cap to bar
    it against)."""
    _ws._prev.clear()
    now = time.time()
    stream = str(tmp_path / "stream.log")
    _write(stream, f"[CALL_START: {now - 12:.3f}]\n[CALL: m]\n[CONTENT]partial",
           now - 1)
    rows = _gp.generation_rows(
        stream_log=stream, width=120, now=now,
        runtime={"cpt": 4.0, "max_tokens": None, "retries": 0}, refresh=2.0,
        think_cap_chars=40_000)
    plain_rows = [_render.plain(r) for r in rows]
    # `elapsed` floors `now - started`; started was set 12s before `now`, so
    # scheduling jitter between building the fixture and the call may floor
    # to 11 or 12 — either is the ticking counter this test exists to prove
    # exists at all, not a specific value.
    assert any("elapsed 11s" in r or "elapsed 12s" in r for r in plain_rows)
    assert not any("[" in r and "]" in r and "%" in r for r in plain_rows[1:])

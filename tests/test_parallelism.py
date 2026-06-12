"""Pins for the single global PIPELINE_PARALLELISM knob and the per-thread
stream machinery it relies on. Simulates parallelism=4 and validates that
every stream the run produces stays coherent under concurrency:

  - the per-thread LLM token stream files (model_interact._sock_*) — one
    file per thread, no interleaving, no mid-call truncation;
  - the events.jsonl stream (pipeline._emit) — every line parses, every
    line carries the emitting thread's name;
  - generate_in_parallel — genuinely concurrent (proven with a barrier)
    and index-aligned.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def test_global_knob_and_legacy_ignored(monkeypatch) -> None:
    """PIPELINE_PARALLELISM is the only knob; the dead per-step knobs are
    ignored (with a one-shot guard warning, not an error)."""
    import pipeline

    monkeypatch.delenv("PIPELINE_PARALLELISM", raising=False)
    assert pipeline._pipeline_parallelism() == 1   # default: sequential

    monkeypatch.setenv("PIPELINE_PARALLELISM", "4")
    assert pipeline._pipeline_parallelism() == 4

    # Legacy knobs present and contradictory → still the global value.
    monkeypatch.setattr(pipeline, "_legacy_parallelism_warned", False)
    monkeypatch.setenv("PHASE_C_PARALLELISM", "9")
    monkeypatch.setenv("TASK0C_SPLIT_N", "7")
    assert pipeline._pipeline_parallelism() == 4

    monkeypatch.setenv("PIPELINE_PARALLELISM", "0")
    assert pipeline._pipeline_parallelism() == 1   # floor at 1
    print("  OK  PIPELINE_PARALLELISM global knob")


def test_per_thread_stream_files_4_workers(tmp_path: Path, monkeypatch) -> None:
    """4 concurrent calls → 4 stream files, each containing ONLY its own
    call's header + tokens, in order. This is the invariant C10 audits from
    events: one live call per thread name, so per-thread files never collide."""
    import model_interact as mi

    stream_log = tmp_path / "stream.log"
    monkeypatch.setattr(mi, "_STREAM_LOG", str(stream_log))

    barrier = threading.Barrier(4, timeout=10)
    tokens_per_call = 50
    errors: list[str] = []

    def _call(worker: str) -> None:
        try:
            mi._sock_open(label=f"call-{worker}")
            barrier.wait()           # all 4 calls provably live at once
            for i in range(tokens_per_call):
                mi._sock_write(f"<{worker}:{i}>")
            mi._sock_close()
        except Exception as e:       # surface failures to the main thread
            errors.append(f"{worker}: {e}")

    workers = [threading.Thread(target=_call, args=(f"w{i}",), name=f"per-fn-c{i}-w0")
               for i in range(4)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=30)
    assert not errors, errors

    names = {f"w{i}": f"per-fn-c{i}-w0" for i in range(4)}
    for worker, tname in names.items():
        f = tmp_path / f"stream.{tname}.log"
        assert f.exists(), f"missing per-thread stream file {f}"
        content = f.read_text()
        assert content.startswith("[CALL_START"), content[:50]
        assert f"[CALL: call-{worker}]" in content
        # Complete own token sequence, in order.
        pos = -1
        for i in range(tokens_per_call):
            nxt = content.find(f"<{worker}:{i}>")
            assert nxt > pos, f"{worker} token {i} missing/out of order"
            pos = nxt
        # Zero foreign tokens — the whole point of per-thread files.
        for other in names:
            if other != worker:
                assert f"<{other}:" not in content, \
                    f"{worker}'s stream contains {other}'s tokens"
    print("  OK  per-thread stream files (4 concurrent calls, no interleave)")


def test_generate_in_parallel_truly_concurrent(monkeypatch) -> None:
    """parallelism=4 fan-out: the barrier deadlocks (and breaks) unless all 4
    asks run at once; results stay index-aligned to the queries."""
    import pipeline

    barrier = threading.Barrier(4, timeout=10)

    def _fake_ask(query, model=None, think=None, **kw):
        barrier.wait()               # raises BrokenBarrierError if not concurrent
        return f"resp:{query}"

    monkeypatch.setattr(pipeline, "ask", _fake_ask)
    out = pipeline.generate_in_parallel(["A", "B", "C", "D"])
    assert out == ["resp:A", "resp:B", "resp:C", "resp:D"], out
    print("  OK  generate_in_parallel is genuinely 4-way concurrent")


def test_events_stream_under_4_threads(tmp_path: Path, monkeypatch) -> None:
    """events.jsonl under 4 concurrent emitters: every line parses (no torn
    writes), every line names its emitting thread, all 4 threads appear —
    the evidence C10 reads."""
    import pipeline

    ev = tmp_path / "events.jsonl"
    monkeypatch.setattr(pipeline, "_EVENTS_LOG", str(ev))

    barrier = threading.Barrier(4, timeout=10)
    n_each = 100

    def _emitter(worker: str) -> None:
        barrier.wait()
        for i in range(n_each):
            pipeline._emit("llm.request", data={"worker": worker, "i": i})

    threads = [threading.Thread(target=_emitter, args=(f"w{i}",), name=f"emit-w{i}")
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    lines = [ln for ln in ev.read_text().splitlines() if ln.strip()]
    assert len(lines) == 4 * n_each, f"expected {4 * n_each} events, got {len(lines)}"
    seen_threads = set()
    for ln in lines:
        rec = json.loads(ln)         # raises on a torn line → test fails
        assert rec["event"] == "llm.request"
        seen_threads.add(rec["thread"])
    assert seen_threads == {f"emit-w{i}" for i in range(4)}, seen_threads
    print("  OK  events.jsonl coherent under 4 concurrent emitters")


def test_splice_lock_per_tree(tmp_path: Path) -> None:
    """Parallel clusters get DISTINCT splice+cargo locks (their trees are
    isolated); the same tree always maps to the same lock (workers within a
    cluster still serialise)."""
    import pipeline

    a = tmp_path / ".skeleton-filled" / "stable"
    b = tmp_path / ".skeleton-filled" / "nightly"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert pipeline._splice_lock_for(a) is pipeline._splice_lock_for(a)
    assert pipeline._splice_lock_for(a) is not pipeline._splice_lock_for(b)
    print("  OK  splice locks are per source tree")

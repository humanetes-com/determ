"""Bounded benchmark: measure achievable concurrency + prefix-cache effect
against the local idle vLLM. Read-only (inference only). Self-hosted only.

Compares, at concurrency P=4:
  - DISTINCT prefixes (no sharing): KV cost = P x full_prompt
  - SHARED  prefix  (same-file):    KV cost = prefix + P x suffix
Reports wall time, aggregate output tok/s, and peak KV usage + capacity-waits
sampled from /metrics during each run.
"""
import os, time, threading, urllib.request
from openai import OpenAI

BASE = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
MODEL = os.getenv("VLLM_MODEL_NAME", "Qwen/Qwen3.6-27B-FP8")
client = OpenAI(base_url=BASE, api_key="vllm", timeout=900)

# A prefix stands in for stable task0c context (rules preamble + file source +
# coverage_strategy). Size via PREFIX_TOK (approx tokens; one line ≈ 20 tok).
# Default ~6k tok; set PREFIX_TOK=80000 for realistic task0c sizing.
PREFIX_TOK = int(os.getenv("PREFIX_TOK", "6000"))
OUT_TOK = int(os.getenv("OUT_TOK", "200"))
P = int(os.getenv("P", "4"))
_NLINES = max(1, PREFIX_TOK // 20)

def _prefix(seed: str) -> str:
    # seed varies the CONTENT from token 0 so distinct-seed prefixes do NOT
    # share KV (vLLM prefix-cache keys on the longest matching token prefix).
    return (f"// [{seed}] stable context: the quick brown fox jumps over the "
            f"lazy dog; fn foo_{seed}() -> usize {{ 42 }}\n") * _NLINES

SHARED_PREFIX = _prefix("shared")

def metrics():
    try:
        raw = urllib.request.urlopen("http://localhost:8000/metrics", timeout=5).read().decode()
    except Exception:
        return (None, None)
    kv = cap = None
    for ln in raw.splitlines():
        if ln.startswith("vllm:kv_cache_usage_perc{"):
            kv = float(ln.rsplit(" ", 1)[1])
        if 'num_requests_waiting_by_reason{' in ln and 'reason="capacity"' in ln:
            cap = float(ln.rsplit(" ", 1)[1])
    return (kv, cap)

def one(prompt):
    t0 = time.monotonic()
    r = client.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": prompt}],
        max_tokens=OUT_TOK, temperature=0.7, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    dt = time.monotonic() - t0
    ct = r.usage.completion_tokens if r.usage else OUT_TOK
    pt = r.usage.prompt_tokens if r.usage else 0
    return dt, ct, pt

def run(label, prompts):
    peak = {"kv": 0.0, "cap": 0.0}
    stop = threading.Event()
    def sampler():
        while not stop.is_set():
            kv, cap = metrics()
            if kv is not None: peak["kv"] = max(peak["kv"], kv)
            if cap is not None: peak["cap"] = max(peak["cap"], cap)
            time.sleep(0.2)
    s = threading.Thread(target=sampler); s.start()
    res = [None] * len(prompts)
    def w(i): res[i] = one(prompts[i])
    t0 = time.monotonic()
    ts = [threading.Thread(target=w, args=(i,)) for i in range(len(prompts))]
    for t in ts: t.start()
    for t in ts: t.join()
    wall = time.monotonic() - t0
    stop.set(); s.join()
    tot_out = sum(r[1] for r in res)
    pt0 = res[0][2]
    print(f"{label:28} wall={wall:6.2f}s  out_tok={tot_out:5}  "
          f"agg_out_tok/s={tot_out/wall:7.1f}  prompt_tok≈{pt0}  "
          f"peakKV={peak['kv']*100:5.1f}%  peakCapWait={peak['cap']:.0f}")
    return wall

# warm the shared-prefix cache so SHARED reflects a steady-state hit
one(SHARED_PREFIX + "\nWrite one short Rust unit test.")
print(f"--- benchmark (P={P}, prefix≈{PREFIX_TOK} tok, out={OUT_TOK} tok each) ---")
run("serial x1 (baseline)", [SHARED_PREFIX + "\nvariant 0: write a Rust test."])
# DISTINCT: each request gets its own content-distinct prefix → no KV sharing.
run(f"concurrent x{P} DISTINCT", [_prefix(f"req{i}") + f"\nvariant {i}: write a Rust test." for i in range(P)])
# SHARED: all requests share one prefix → KV = prefix + P×suffix.
run(f"concurrent x{P} SHARED-prefix", [SHARED_PREFIX + f"\nvariant {i}: write a Rust test." for i in range(P)])

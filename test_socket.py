#!/usr/bin/env python3
"""
One-shot socket monitor test.
Starts a listener on OLLAMA_MONITOR_SOCK, makes a single ask() call,
then prints everything received on the socket + the debug log.
"""
import os, sys, socket, threading, time

SOCK_PATH = "/tmp/ollama_monitor.sock"
DEBUG_LOG = "/tmp/debug.log"

os.environ["OLLAMA_MONITOR_SOCK"] = SOCK_PATH
os.environ["LLM_DEBUG_LOG"]       = DEBUG_LOG
os.environ.setdefault("OLLAMA_NUM_CTX", "163840")

# Truncate debug log
open(DEBUG_LOG, "w").close()

# ── Socket listener ────────────────────────────────────────────────────────────
received = []
listener_ready = threading.Event()

def _listen():
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    srv.listen(1)
    listener_ready.set()
    srv.settimeout(120)
    try:
        conn, _ = srv.accept()
        print("[socket] client connected", flush=True)
        while True:
            data = conn.recv(4096)
            if not data:
                break
            text = data.decode(errors="replace")
            received.append(text)
            sys.stdout.write(text)
            sys.stdout.flush()
        conn.close()
    except socket.timeout:
        print("\n[socket] timed out waiting for connection", flush=True)
    finally:
        srv.close()

t = threading.Thread(target=_listen, daemon=True)
t.start()
listener_ready.wait()
print(f"[socket] listening on {SOCK_PATH}", flush=True)

# ── Single test call ───────────────────────────────────────────────────────────
sys.path.insert(0, "/home/user/determ")
import importlib
import model_interact
importlib.reload(model_interact)  # pick up env vars set above

MODEL = "qwen3.6:27b-q8"
print(f"\n[test] calling ask() with model={MODEL} ...\n", flush=True)

result = model_interact.ask(
    "Reply with only the number 42. Nothing else.",
    model=MODEL,
)

# Wait briefly for socket to flush
t.join(timeout=2)

# ── Report ─────────────────────────────────────────────────────────────────────
print(f"\n\n{'='*60}")
print(f"ask() returned ({len(result)} chars): {result!r}")

print(f"\n── Debug log ({DEBUG_LOG}) ──")
try:
    print(open(DEBUG_LOG).read() or "(empty)")
except FileNotFoundError:
    print("(not created)")

print(f"\n── Socket total received: {sum(len(r) for r in received)} chars ──")

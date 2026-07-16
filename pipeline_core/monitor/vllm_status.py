"""vLLM backend health panel: TCP/HTTP health check + docker log tail.

Extracted out of monitor.py (the god-file LOC ratchet only allows it to
shrink — see .loc_ratchet.json / tools/loc_ratchet.py) so new monitor
features have budget without this self-contained panel eating it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request

from pipeline_core.monitor.render import dim, green, yellow
from pipeline_core.monitor.run_data import read_backend

__all__ = ["vllm_status_rows"]

_cache: tuple[float, list[str]] = (0.0, [])


def _tcp_open() -> bool:
    """Fast pre-check — fails instantly if nothing listens on :8000."""
    try:
        s = socket.create_connection(("127.0.0.1", 8000), timeout=0.3)
        s.close()
        return True
    except OSError:
        return False


def vllm_status_rows(width: int) -> list[str]:
    """Status rows for the vLLM backend; empty if backend != vllm.

    Cached: health check runs at most once per 3 s; docker log fetch at most
    once per 8 s. Never blocks the render loop for more than ~0.3 s.
    """
    global _cache
    if read_backend() != "vllm":
        return []
    now = time.time()
    cache_age = now - _cache[0]

    if cache_age < 3.0:  # cached result fresh enough (healthy or loading)
        return _cache[1]

    healthy = False
    if _tcp_open():
        try:
            urllib.request.urlopen("http://localhost:8000/health", timeout=0.5)
            healthy = True
        except Exception:
            pass

    if healthy:
        result = [green("vLLM  ✓  healthy")]
        _cache = (now, result)
        return result

    rows: list[str] = [yellow("vLLM  ◑  loading (not yet ready)")]

    if cache_age >= 8.0 or not _cache[1]:   # docker logs are slow, re-fetch rarely
        try:
            cid = ""
            cid_file = "/tmp/vllm_container.id"
            if os.path.exists(cid_file):
                with open(cid_file) as f:
                    cid = f.read().strip()
            if not cid:
                cid = subprocess.check_output(
                    ["docker", "ps", "--filter", "ancestor=vllm/vllm-openai:latest",
                     "--format", "{{.ID}}", "--latest"],
                    stderr=subprocess.DEVNULL, timeout=2, text=True
                ).strip()
            if cid:
                logs = subprocess.check_output(
                    ["docker", "logs", "--tail", "6", cid],
                    stderr=subprocess.STDOUT, timeout=2, text=True
                ).splitlines()
                for line in logs:
                    rows.append(dim((line[:width-1] + "…") if len(line) > width else line))
        except Exception:
            pass

    _cache = (now, rows)
    return rows

"""vLLM server bootstrap adapter (P6).

The `BACKEND=vllm` startup concern lifted out of the pipeline.py god-file: probe
the local server, launch `docker_vllm.sh` if it is down, then poll the inference
endpoint until it accepts requests (health passes before CUDA-graph compilation
finishes, so an inference probe is required). Pure I/O — the composition root
injects the `log` sink.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path


def ensure_vllm_ready(root: Path, log: Callable[[str], None]) -> None:
    vllm_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
    port = vllm_url.split(":")[-1].split("/")[0]
    health_url = f"http://localhost:{port}/health"
    infer_url = f"http://localhost:{port}/v1/chat/completions"
    model = os.getenv("VLLM_MODEL_NAME", "")

    def _health_ok() -> bool:
        try:
            urllib.request.urlopen(health_url, timeout=5)
            return True
        except Exception:
            return False

    if not _health_ok():
        log(f"  vLLM not responding at {health_url} — launching docker_vllm.sh ...")
        script = root / "docker_vllm.sh"
        result = subprocess.run(["bash", str(script)], cwd=str(root))
        if result.returncode != 0:
            log(f"  [warn] docker_vllm.sh exited {result.returncode} — proceeding anyway")
    else:
        log(f"  vLLM health OK at {health_url} — checking inference readiness ...")

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()
    for i in range(1, 241):
        try:
            req = urllib.request.Request(
                infer_url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
            elapsed = i * 5
            log(f"  vLLM inference ready ({elapsed // 60}m {elapsed % 60}s).")
            return
        except Exception:
            pass
        time.sleep(5)
        if i % 12 == 0:
            log(f"  ... vLLM inference not ready yet ({i * 5 // 60}m elapsed)")
    log("  [warn] vLLM inference not ready after 20 min — proceeding anyway")

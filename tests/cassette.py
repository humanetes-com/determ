"""L2 cassette replay.

Build a `{prompt_sha → response_text}` map from a recorded run (`logs/events.jsonl`
+ the content-addressed `logs/prompts/` store) and replay it through the single
model seam `pipeline._ask_raw`. Replay is deterministic and instant: the same
prompt hashes to the same canned response, so a real pipeline run reproduces the
recorded fills with no LLM. A prompt-changing edit produces a **cassette MISS at
exactly the altered prompt** — a precise blast-radius signal, not a 5h re-run.

Keying: `_store_prompt_content` (pipeline.py:404) hashes the prompt AFTER the
context-guard trims it, and `_ask_raw` receives that same trimmed prompt — so
`sha256(prompt_arg) == the recorded prompt sha`. Pairing prefers the explicit
`prompt_sha` on the response event; for older recordings that lack it,
it falls back to emission-order pairing (exact at PIPELINE_PARALLELISM=1).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pipeline


class CassetteMiss(KeyError):
    """Raised in strict replay when a prompt has no recorded response — i.e. the
    code under test changed this prompt since the recording."""


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _read_body(prompts_dir: Path, sha: str) -> str | None:
    if not sha:
        return None
    p = prompts_dir / f"{sha}.txt"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def build_cassette(
    events_path: str | Path,
    prompts_dir: str | Path,
    *,
    product: str | None = None,
    pipeline_sha: str | None = None,
) -> dict[str, str]:
    """Map `{prompt_sha → response_text}` from a recorded run. Filters to one
    `product`/`pipeline_sha` when given (recommended — events.jsonl spans runs)."""
    events_path = Path(events_path)
    prompts_dir = Path(prompts_dir)
    cassette: dict[str, str] = {}
    pending_prompt_sha: str | None = None
    with events_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if product is not None and rec.get("product") != product:
                continue
            if pipeline_sha is not None and rec.get("pipeline_sha") != pipeline_sha:
                continue
            ev = rec.get("event")
            data = rec.get("data") or {}
            if ev == "llm.request":
                pending_prompt_sha = (data.get("prompt") or {}).get("sha256") or None
            elif ev == "llm.response":
                psha = data.get("prompt_sha") or pending_prompt_sha
                rsha = (data.get("response") or {}).get("sha256")
                pending_prompt_sha = None
                if not psha or not rsha:
                    continue
                body = _read_body(prompts_dir, rsha)
                if body is not None:
                    cassette[psha] = body
    return cassette


def install_cassette(
    monkeypatch,
    cassette: dict[str, str],
    *,
    strict: bool = True,
    misses: list[str] | None = None,
):
    """Patch `pipeline._ask_raw` to replay `cassette`. On a miss: append the sha to
    `misses` (if given) and either raise `CassetteMiss` (strict) or return ""."""
    def _replay(prompt, system=None, **_kw):
        sha = _sha(prompt)
        if sha in cassette:
            return cassette[sha]
        if misses is not None:
            misses.append(sha)
        if strict:
            raise CassetteMiss(sha)
        return ""

    monkeypatch.setattr(pipeline, "_ask_raw", _replay)

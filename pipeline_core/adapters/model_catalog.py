"""Network adapters for the model report, relocated out of pipeline.py.

The two live reads behind `--best-models-list`: the local Ollama daemon's
installed-models list, and the HuggingFace Hub top-models query. Pure ranking /
rendering lives in `domain.model_report`; this layer only does I/O and maps the
provider payloads onto the domain `ModelRow` VO.
"""

from __future__ import annotations

from typing import Any

from pipeline_core.domain import model_report as _report
from pipeline_core.domain.model_report import (
    ModelRow,
    extract_params_b,
    fit_label,
    vram_4bit_estimate,
)


def ollama_installed_bases(timeout: float = 2.0) -> set[str]:
    """Base names of models pulled into the local Ollama daemon (empty if absent)."""
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags",
                                    timeout=timeout) as resp:
            data = json.loads(resp.read())
        return {m["name"].split(":")[0] for m in data.get("models", [])}
    except Exception:
        return set()


def hf_top_models(vram_gb: float, limit: int = 1000) -> list[ModelRow]:
    """Top text-generation models on HuggingFace Hub as fit-annotated rows.

    Raises ``ImportError`` if ``huggingface_hub`` is not installed (the caller
    renders the install hint)."""
    from huggingface_hub import list_models

    raw: list[Any] = list(list_models(
        pipeline_tag="text-generation",
        sort="likes",
        limit=limit,
        expand=["safetensors", "downloads", "likes"],
    ))
    rows: list[ModelRow] = []
    for m in raw:
        st = getattr(m, "safetensors", None)
        total = getattr(st, "total", None) if st else None
        pc = getattr(st, "parameter_count", None) if st else None
        params = extract_params_b(m.id, total, pc if isinstance(pc, dict) else None)
        if params is None or params < 1:
            continue
        vram_4bit = vram_4bit_estimate(params)
        rows.append(ModelRow(
            id=m.id, params=params, vram_4bit=vram_4bit,
            fit=fit_label(vram_4bit, vram_gb),
            downloads=m.downloads or 0, likes=m.likes or 0,
        ))
    return rows


def print_best_models(vram_gb: float = 32.0) -> None:
    """Print best coding models (Ollama + HuggingFace) annotated for a VRAM
    budget — the `--best-models-list` command (catalog reads + domain
    ranking/rendering + stdout)."""
    ollama = _report.annotate_installed(
        _report.build_ollama_rows(vram_gb), ollama_installed_bases())
    print(_report.format_model_table(
        ollama, f"── Ollama  (ollama pull <name>)  |  VRAM budget: {vram_gb:.0f} GB"))
    print("  Benchmark scores: https://livecodebench.github.io/leaderboard.html")

    try:
        import huggingface_hub  # noqa: F401  (capability probe; the read is below)
    except ImportError:
        print("\nERROR: huggingface_hub not installed — skipping HF section.  Run: pip install huggingface_hub")
        print(_report.LEGEND)
        return

    print("\nFetching HuggingFace Hub top models …")
    hf = hf_top_models(vram_gb)
    deduped = _report.dedup_hf_rows(_report.rank_hf_rows(hf))
    print(_report.format_model_table(
        deduped[:30], f"── HuggingFace  (HF_MODEL=<id>)  |  VRAM budget: {vram_gb:.0f} GB"))
    print("  Benchmark scores: https://huggingface.co/spaces/bigcode/bigcodebench-leaderboard")
    print(_report.LEGEND)

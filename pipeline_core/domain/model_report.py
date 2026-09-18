"""Pure domain for the `--best-models-list` VRAM-fit report.

A self-contained, engine-unrelated concern lifted out of the pipeline.py god-file:
the model catalog, the VRAM-fit arithmetic, the ranking/dedup rules, and the table
rendering — all pure (stdlib only, no I/O). The network reads (Ollama `/api/tags`,
HuggingFace Hub) live in `adapters.model_catalog`; the composition root wires them.

Regex here parses MODEL IDS (identifier/filename class — the allowed regex carve-out,
never Rust source).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# Top coding models available via Ollama (ollama pull <name>), ranked by quality.
# Params are approximate; VRAM is estimated at Q4_K_M quantization.
OLLAMA_MODELS: list[tuple[str, int, str]] = [
    ("qwen3:72b",            72, "Best coding+reasoning 2026"),
    ("qwen2.5-coder:72b",    72, "Code-specialized, excellent"),
    ("deepseek-r1:70b",      70, "Strong reasoning for code"),
    ("llama3.3:70b",         70, "Solid general + code"),
    ("glm4.7:latest",        20, "89% LiveCodeBench, top score"),
    ("qwen3:35b-a3b",        35, "73% SWE-bench, MoE efficient"),
    ("qwen3:32b",            32, "Best dense 32B available"),
    ("qwen2.5-coder:32b",    32, "Proven, strong baseline"),
    ("deepseek-coder-v2:16b", 16, "Fast, good quality"),
    ("gemma4:27b",           27, "Google, strong code"),
    ("qwen3:14b",            14, "81.1 MMLU, punches above weight"),
    ("llama4:scout-17b",     17, "Meta, long context"),
    ("deepseek-r1:32b",      32, "Reasoning distill, good code"),
    ("phi4:14b",             14, "Microsoft, efficient"),
    ("qwen3:8b",              8, "Best small model for code"),
    ("mistral-small3.1:24b", 24, "Fast, strong instruction following"),
    ("qwen2.5-coder:7b",      7, "Lightweight fallback"),
]

# Sort order: models that fit fully first, then partial-offload, then too-big.
FIT_ORDER: dict[str, int] = {"✓ full": 0, "~ RAM": 1, "✗": 2}


@dataclass(frozen=True, slots=True)
class ModelRow:
    """One row of the model report. ``notes`` (Ollama, curated) and
    ``downloads``/``likes`` (HuggingFace, live) are mutually-exclusive shapes."""

    id: str
    params: float
    vram_4bit: float
    fit: str
    notes: str | None = None
    downloads: int | None = None
    likes: int | None = None


def vram_4bit_estimate(params_b: float) -> float:
    """Estimated VRAM (GB) for a `params_b`-billion-param model at 4-bit quant."""
    return round(params_b * 0.55 + 2.5, 1)


def fit_label(vram_4bit: float, vram_gb: float) -> str:
    if vram_4bit <= vram_gb:
        return "✓ full"
    if vram_4bit <= vram_gb * 1.35:
        return "~ RAM"
    return "✗"


def extract_params_b(model_id: str, safetensors_total: float | None = None,
                     safetensors_param_count: dict[str, int] | None = None
                     ) -> float | None:
    """Parameter count in billions: prefer safetensors metadata, else parse the id."""
    if isinstance(safetensors_total, (int, float)) and safetensors_total > 1e8:
        return safetensors_total / 1e9
    if isinstance(safetensors_param_count, dict):
        s = sum(safetensors_param_count.values())
        if s > 1e8:
            return s / 1e9
    m = re.search(r'[/_\-](\d+(?:\.\d+)?)[Bb](?:[/_\-]|$)', model_id)
    if m:
        return float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)[Bb]$', model_id)
    if m:
        return float(m.group(1))
    return None


def build_ollama_rows(vram_gb: float) -> list[ModelRow]:
    """The curated Ollama catalog as fit-ranked rows (no install annotation yet)."""
    rows = [
        ModelRow(id=name, params=float(params), vram_4bit=vram_4bit_estimate(params),
                 fit=fit_label(vram_4bit_estimate(params), vram_gb), notes=notes)
        for name, params, notes in OLLAMA_MODELS
    ]

    def _key(r: ModelRow) -> tuple[int, int]:
        short_id = r.id.rsplit(":", 1)[0] if r.id.count(":") > 1 else r.id
        entry = (short_id, int(r.params), r.notes or "")
        orig_idx = OLLAMA_MODELS.index(entry) if entry in OLLAMA_MODELS else 99
        return (FIT_ORDER.get(r.fit, 99), orig_idx)

    return sorted(rows, key=_key)


def annotate_installed(rows: list[ModelRow], installed_bases: set[str]) -> list[ModelRow]:
    """Prefix '✔ installed' to the notes of rows whose base model is locally pulled."""
    out: list[ModelRow] = []
    for r in rows:
        base = r.id.split(":")[0]
        if base in installed_bases:
            out.append(replace(r, notes="✔ installed  " + (r.notes or "")))
        else:
            out.append(r)
    return out


def rank_hf_rows(rows: list[ModelRow]) -> list[ModelRow]:
    """Rank HF rows by fit then popularity (likes desc)."""
    return sorted(rows, key=lambda r: (FIT_ORDER.get(r.fit, 99), -(r.likes or 0)))


def dedup_hf_rows(rows: list[ModelRow]) -> list[ModelRow]:
    """Collapse quantization/format variants of the same base model to the first seen."""
    seen: set[str] = set()
    out: list[ModelRow] = []
    for r in rows:
        base = re.sub(
            r'[-_](AWQ|GPTQ|GGUF|bnb|4bit|8bit|fp16|bf16|Int4|Int8|MLX|2507).*$',
            '', r.id, flags=re.IGNORECASE)
        if base not in seen:
            seen.add(base)
            out.append(r)
    return out


def format_model_table(rows: list[ModelRow], title: str) -> str:
    """Render the report table (byte-identical to the legacy printer)."""
    lines = [f"\n{title}\n"]
    hdr = f"{'#':>3}  {'Model':<45} {'Params':>7}  {'4-bit':>6}  {'Fits':>7}"
    extra = "  Notes" if rows[0].notes is not None else f"  {'DL/mo':>9}  {'Likes':>6}"
    lines.append(hdr + extra)
    lines.append("─" * (len(hdr) + len(extra)))
    for i, r in enumerate(rows, 1):
        label = r.id
        if len(label) > 45:
            label = "…" + label[-44:]
        line = f"{i:>3}  {label:<45} {r.params:>6.1f}B  {r.vram_4bit:>5.1f}G  {r.fit:>7}"
        if r.notes is not None:
            line += f"  {r.notes}"
        else:
            line += f"  {r.downloads or 0:>9,}  {r.likes or 0:>6,}"
        lines.append(line)
    return "\n".join(lines)


LEGEND = (
    "\nLegend:  ✓ full = fits entirely in GPU VRAM\n"
    "         ~ RAM  = fits with partial CPU-RAM offload (~20–35% slower)\n"
    "         ✗      = exceeds VRAM + 35% RAM headroom"
)

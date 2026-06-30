"""Pins the pure `--best-models-list` VRAM-fit report domain (model_report.py)."""
from __future__ import annotations

from pipeline_core.domain.model_report import (
    FIT_ORDER,
    OLLAMA_MODELS,
    ModelRow,
    annotate_installed,
    build_ollama_rows,
    dedup_hf_rows,
    extract_params_b,
    fit_label,
    format_model_table,
    rank_hf_rows,
    vram_4bit_estimate,
)


def test_vram_estimate() -> None:
    assert vram_4bit_estimate(72) == round(72 * 0.55 + 2.5, 1)


def test_fit_label_three_bands() -> None:
    assert fit_label(10.0, 32.0) == "✓ full"        # fits
    assert fit_label(40.0, 32.0) == "~ RAM"         # within 1.35×
    assert fit_label(80.0, 32.0) == "✗"             # exceeds


def test_extract_params_b_safetensors_total() -> None:
    assert extract_params_b("x", safetensors_total=7e9) == 7.0


def test_extract_params_b_safetensors_count_dict() -> None:
    assert extract_params_b("x", safetensors_param_count={"a": 3_000_000_000,
                                                          "b": 2_000_000_000}) == 5.0


def test_extract_params_b_from_id_mid_and_suffix() -> None:
    assert extract_params_b("qwen2.5-coder:7b") == 7.0       # suffix form
    assert extract_params_b("org/model-13B-instruct") == 13.0  # mid form
    assert extract_params_b("no-size-here") is None
    # tiny safetensors total falls through to id parse
    assert extract_params_b("m-8b", safetensors_total=100.0) == 8.0


def test_build_ollama_rows_ranked_and_complete() -> None:
    rows = build_ollama_rows(32.0)
    assert len(rows) == len(OLLAMA_MODELS)
    fits = [FIT_ORDER.get(r.fit, 99) for r in rows]
    assert fits == sorted(fits)             # fit-ordered
    assert all(r.notes is not None for r in rows)


def test_annotate_installed() -> None:
    rows = [ModelRow(id="qwen3:32b", params=32, vram_4bit=20.1, fit="✓ full", notes="x"),
            ModelRow(id="phi4:14b", params=14, vram_4bit=10.2, fit="✓ full", notes="y")]
    out = annotate_installed(rows, {"qwen3"})
    assert out[0].notes.startswith("✔ installed")
    assert out[1].notes == "y"              # not installed → unchanged


def test_rank_and_dedup_hf_rows() -> None:
    a = ModelRow(id="org/M", params=7, vram_4bit=6.4, fit="✓ full", likes=5)
    b = ModelRow(id="org/M-AWQ", params=7, vram_4bit=6.4, fit="✓ full", likes=99)
    c = ModelRow(id="org/N", params=7, vram_4bit=6.4, fit="~ RAM", likes=999)
    ranked = rank_hf_rows([c, a, b])
    assert ranked[0].fit == "✓ full"        # full fit ranks before ~ RAM
    assert ranked[0].likes == 99            # within fit, higher likes first
    deduped = dedup_hf_rows([a, b])
    assert [r.id for r in deduped] == ["org/M"]  # AWQ variant collapsed


def test_format_model_table_ollama_and_hf_shapes() -> None:
    ollama = [ModelRow(id="qwen3:32b", params=32, vram_4bit=20.1, fit="✓ full", notes="great")]
    out = format_model_table(ollama, "Ollama")
    assert "Ollama" in out and "Notes" in out and "great" in out
    long_id = "x" * 60
    hf = [ModelRow(id=long_id, params=7, vram_4bit=6.4, fit="✓ full", downloads=1234, likes=56)]
    out2 = format_model_table(hf, "HF")
    assert "DL/mo" in out2 and "1,234" in out2 and "…" in out2  # truncated long id

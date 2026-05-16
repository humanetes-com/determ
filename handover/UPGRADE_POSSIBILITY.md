# Hardware Plan

## Current Machine (to sell)

- **Machine**: HP Victus by HP 15L Gaming Desktop TG02-1xxx (small form factor desktop)
- **GPU**: NVIDIA GeForce RTX 4060 — 8 GB VRAM
- **Plan**: sell to fund the new build

## Target Build

- **Motherboard**: MSI MPG Z390 Gaming Pro Carbon (already owned)
  - 3× PCIe 3.0 x16 slots, dual GPU runs at x8/x8
  - LGA 1151, supports Intel 8th/9th gen
- **GPU**: RTX 3090 24 GB — single card to start
  - 350W TDP
  - 24 GB VRAM — fits qwen3:32b fully (~20 GB at Q4_K_M)
- **PSU**: 1200W 80+ Gold (minimum for single 3090 with headroom; required if adding second later)

## Model

**`qwen3:32b`** — recommended starting model
- ~20 GB at Q4_K_M quantization, fits entirely in 24 GB VRAM
- No RAM offload needed — full GPU speed
- Ranked #1 for coding and requirements writing in the 32B class (April 2026)
- Pull via Ollama: `ollama pull qwen3:32b`
- Or via HuggingFace: `HF_MODEL=Qwen/Qwen3-32B` (not yet on HF as of April 2026 — use Ollama)

## Phase 2 (optional future upgrade)

Add a second RTX 3090 24 GB:
- Total VRAM: 48 GB — fits qwen3:72b fully at Q4_K_M (~42 GB)
- PSU: 1200W is the minimum; 1500W recommended for dual 3090 comfort
- Board supports x8/x8 — both cards get equal PCIe bandwidth
- Use HuggingFace backend with explicit `max_memory`:
  ```python
  max_memory = {0: "22GiB", 1: "22GiB", "cpu": "16GiB"}
  ```

## Budget Estimate (Germany, used market, April 2026)

| Item | Approx. Used Price |
|---|---|
| RTX 3090 24 GB | €450–€600 |
| PSU 1200W 80+ Gold | €80–€150 new |
| **Total** | **€530–€750** |

HP Victus 15L with RTX 4060 sells for roughly €400–€500 used — covers most of the cost.

## Why Not the 8 GB Second GPU

A mismatched 24 GB + 8 GB dual GPU setup causes Ollama to distribute layers
proportionally, overloading the 8 GB card and creating a bottleneck. The 8 GB card
adds complexity without meaningful benefit. Single 24 GB is cleaner and faster for
models in the 32B class.

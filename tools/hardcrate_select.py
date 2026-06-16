#!/usr/bin/env python3
"""Hardcrate complexity selector — CODE_ARCHITECTURE.md R10.7.

The hardcrate is a difficulty LADDER so the same fixture scales from weak to strong
local models. Each trap carries a complexity LEVEL; `--max-level N` strips every
trap above N from a crate copy, leaving a clean crate the pipeline reconstructs with
no idea complexity exists (decoupled from the cfg/cluster machinery — a complexity
level is NOT a cfg/cluster, avoiding the debt #15 entanglement).

Convention going forward: a new trap module = one `src/<name>.rs` (impl + round-trip
unit tests) + one `tests/<name>.rs` (the holistic pin) + a registry row with its
level. `level 0` = baseline only (the microcrate-equivalent happy path); the current
traps are `level 1`; harder traps get 2…10.

Strip = delete the trap's src + test files and remove its `mod`/`pub use` lines (and
their attached `#[cfg]` attrs) from lib.rs. Baseline (`add`/`checked_div`/`Counter`/
`cap_limit`) and `tests/baseline.rs` are always kept.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

# trap name → its level + the files and lib.rs anchor lines that define it.
# Anchors are matched by exact stripped text; contiguous `#[...]` attr lines directly
# above an anchor are removed with it (so a cfg-gated `mod cap;` strips cleanly).
TRAPS: dict[str, dict] = {
    "round":   {"level": 1, "src": "src/round.rs", "test": "tests/round.rs",
                "lib": ["mod round;", "pub use round::divide_round;"]},
    "merge":   {"level": 1, "src": "src/merge.rs", "test": "tests/merge.rs",
                "lib": ["mod merge;", "pub use merge::merge_first_wins;"]},
    "textsum": {"level": 1, "src": "src/textsum.rs", "test": "tests/textsum.rs",
                "lib": ["mod textsum;", "pub use textsum::{sum_csv, SumError};"]},
    "wire":    {"level": 1, "src": "src/wire.rs", "test": "tests/wire.rs",
                "lib": ["mod wire;", "pub use wire::{decode_id, encode_id};"]},
    "cap":     {"level": 1, "src": "src/cap.rs", "test": "tests/cap.rs",
                "lib": ["mod cap;", "pub use cap::wrapping_total;"]},
    # level 2 — harder than the level-1 codec (T5): a cross-fn protocol with three
    # interacting rules whose accept/reject pins pull deframe in tension, meant to
    # need ≥3 outer iterations (C3/C4).
    "framing": {"level": 2, "src": "src/framing.rs", "test": "tests/framing.rs",
                "lib": ["mod framing;", "pub use framing::{deframe, frame};"]},
}


def max_level() -> int:
    return max((t["level"] for t in TRAPS.values()), default=0)


def _strip_lib_anchors(lib_path: Path, anchors: list[str]) -> None:
    """Remove each anchor line and the contiguous `#[...]` attribute lines directly
    above it (an anchor's cfg gate). No-op for anchors already gone."""
    lines = lib_path.read_text(encoding="utf-8").splitlines()
    drop: set[int] = set()
    for anchor in anchors:
        for i, ln in enumerate(lines):
            if ln.strip() == anchor:
                drop.add(i)
                j = i - 1
                while j >= 0 and lines[j].strip().startswith("#["):
                    drop.add(j)
                    j -= 1
                break
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    lib_path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def select(crate_dir: str | Path, max_keep: int) -> list[str]:
    """Strip every trap whose level > `max_keep` from `crate_dir` (in place).
    Returns the sorted names of the traps removed."""
    root = Path(crate_dir)
    lib = root / "src" / "lib.rs"
    removed: list[str] = []
    for name, t in TRAPS.items():
        if t["level"] <= max_keep:
            continue
        (root / t["src"]).unlink(missing_ok=True)
        (root / t["test"]).unlink(missing_ok=True)
        if lib.exists():
            _strip_lib_anchors(lib, t["lib"])
        removed.append(name)
    return sorted(removed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--crate", type=Path, required=True, help="crate dir to edit in place")
    ap.add_argument("--max-level", type=int, default=max_level())
    ap.add_argument("--from", dest="src", type=Path, default=None,
                    help="optional: copy this crate to --crate first, then strip")
    args = ap.parse_args(argv)
    if args.src is not None:
        if args.crate.exists():
            shutil.rmtree(args.crate)
        shutil.copytree(args.src, args.crate,
                        ignore=shutil.ignore_patterns("target", ".git"))
    removed = select(args.crate, args.max_level)
    print(f"complexity {args.max_level}/{max_level()}: "
          f"{'stripped ' + ', '.join(removed) if removed else 'all traps kept'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

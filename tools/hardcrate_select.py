#!/usr/bin/env python3
"""Hardcrate complexity selector.

The hardcrate is a difficulty LADDER so the same fixture scales from weak to strong
local models. Each trap carries a complexity LEVEL; `--max-level N` strips every
trap above N from a crate copy, leaving a clean crate the pipeline reconstructs with
no idea complexity exists (decoupled from the cfg/cluster machinery — a complexity
level is NOT a cfg/cluster, avoiding the false-incomplete / cfg-leak bug a
cfg-modeled complexity toggle once caused: an undecidable custom `#[cfg(...)]`
predicate got miscounted as a test, then the fix loop made the gated code run
in the default build to close the gap).

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
    # level 2 — harder than the level-1 codec: a cross-fn protocol with three
    # interacting rules whose accept/reject pins pull deframe in tension, meant to
    # need ≥3 outer iterations (C3/C4).
    "framing": {"level": 2, "src": "src/framing.rs", "test": "tests/framing.rs",
                "lib": ["mod framing;", "pub use framing::{deframe, frame};"]},
    # level 3 — the AUGMENTATION FAN-OUT reproducer: a WIDE surface (many small
    # branchy pub fns) pinned on one path each, so reconstruction leaves a large
    # uncovered set spanning many functions → TASK 0c augmentation accumulates many
    # `sprawl.gen_tests_<N>.rs` siblings. Exercises the TASK 1 sibling-coalescing
    # (Part A) and the one-sibling-per-split-and-merge-attempt fix (Part B).
    "sprawl":  {"level": 3, "src": "src/sprawl.rs", "test": "tests/sprawl.rs",
                "lib": ["mod sprawl;",
                        "pub use sprawl::{abs_diff, clamp_u8, classify, count_set_bits, "
                        "digit_sum, is_ascii_upper, max3, min3, nth_byte, parity, "
                        "reverse_nibbles, saturating_sub_u8, sign, to_lower, Category};"]},
    # level 4 — FROZEN-BOUND MISMATCH: an impl with a
    # frozen `where T: Describe` bound whose body must call the bound-provided
    # `T::describe`; the Display/Debug attractors are un-carried bounds → E0277. Pins
    # that the BOUND_METHODS prompt block lets the model fill via the bound method
    # without thrash / a GENUINELY_HARD skip.
    "label":   {"level": 4, "src": "src/label.rs", "test": "tests/label.rs",
                "lib": ["mod label;", "pub use label::{Describe, Reason, Tagged};"]},
    # level 5 — UNSAFE POINTER ARITHMETIC: raw-ptr read/write/offset fns that are
    # `unsafe fn` with no trait bounds. This trap end-to-end-guards the per-fn
    # repair loop's low-temperature route: the pipeline must route through
    # _ask_per_fn_body with low temperature and classify ≤2-attempt failures as
    # GENUINELY_HARD (no more thrash on vtable-shaped fns). Tests pin
    # memory-round-trip correctness for each fn.
    "unsafe_ptr": {"level": 5, "src": "src/unsafe_ptr.rs", "test": "tests/unsafe_ptr.rs",
                   "lib": ["mod unsafe_ptr;",
                           "pub use unsafe_ptr::{copy_via_raw, ptr_distance, read_at, write_at};"]},
    # level 6 — CO-DESIGNED PRIVATE SCHEME (the anyhow ContextError reproducer): a
    # tuple struct (construction is `Guarded(a, t)`, not `::new` → E0599 on the naive
    # fill) plus `seal`/`verify` that must AGREE on a private tag scheme the tests pin
    # only RELATIONALLY (round-trip + rejection + distinctness), never absolutely. A
    # single-fn fill is under-constrained; a seal/verify disagreement compiles clean
    # but fails the cross round-trip → forces the TDD re-fill loop across iterations
    # (C3/C4) and a wrong re-fill of one side reddens the round-trip the other passed
    # (C6 ratchet). Directly mirrors the live anyhow ceiling (compile-clean-but-wrong).
    "guarded": {"level": 6, "src": "src/guarded.rs", "test": "tests/guarded.rs",
                "lib": ["mod guarded;", "pub use guarded::{seal, verify, Guarded};"]},
    # level 7 — REGRESSION-CYCLE (the anyhow ceiling's remaining half): the SAME
    # co-designed-private-scheme property as `guarded` (level 6), but the two sides
    # (`stamp` / `unstamp`) live in SEPARATE files with independently-named private
    # helpers (`mix_a` / `mix_b`), so a greedy PER-FILE fix loop cannot co-design them
    # in one LLM call the way it can for `guarded`'s single file. Only the cross-file
    # round-trip test catches a mismatch, forcing ≥2 outer TDD iterations with no
    # single-pass fixed point — exercises the C3/C4 plateau + C6 ratchet-sanity audits
    # end-to-end (they currently SKIP: no existing trap forces >=3 module-iterations).
    "link": {"level": 7, "src": ["src/link_a.rs", "src/link_b.rs"],
             "test": "tests/link_pair.rs",
             "lib": ["mod link_a;", "mod link_b;",
                     "pub use link_a::stamp;", "pub use link_b::unstamp;"]},
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


def _as_list(v: str | list[str]) -> list[str]:
    return v if isinstance(v, list) else [v]


def select(crate_dir: str | Path, max_keep: int) -> list[str]:
    """Strip every trap whose level > `max_keep` from `crate_dir` (in place).
    Returns the sorted names of the traps removed. A trap's `src`/`test` may be
    a single path or a list (a multi-file trap like `link`)."""
    root = Path(crate_dir)
    lib = root / "src" / "lib.rs"
    removed: list[str] = []
    for name, t in TRAPS.items():
        if t["level"] <= max_keep:
            continue
        for rel in _as_list(t["src"]):
            (root / rel).unlink(missing_ok=True)
        for rel in _as_list(t["test"]):
            (root / rel).unlink(missing_ok=True)
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

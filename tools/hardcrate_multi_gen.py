#!/usr/bin/env python3
"""Generates `tests/fixtures/hardcrate_multi/` — a SCALE fixture, sibling to
`tests/fixtures/hardcrate/` (the difficulty ladder). Different axis: hardcrate's
traps are hard to solve with few fns; this fixture is easy per-fn but LARGE and
a real two-crate Cargo WORKSPACE (`hcm_core` + `hcm_ext`, the latter path-
depending on the former) — the two things `hardcrate` and even `anyhow` (single
module) never exercise:

  - enough fns/files for PIPELINE_PARALLELISM>1 to have genuine worker
    contention (both real bugs found 2026-07-07 — the impl-fill file-affinity
    race and the graceful-stop thread bug — were invisible at hardcrate's
    ~70-fn scale and only surfaced on anyhow's ~4000-line run)
  - a real dependency graph: `hcm_ext` consumes `hcm_core`'s public API
    (a trait + two structs), so the pipeline must reconstruct `hcm_core`
    first and expose its PUBLIC surface (never its stripped internals) when
    prompting for `hcm_ext` — untested by both existing fixtures today
  - a genuine nightly-toolchain cluster (`hcm_core`'s `build.rs` mirrors
    anyhow's real `error_generic_member_access` probe exactly, gating a
    `core::error::Request`-based trap module), vs. hardcrate's plain
    `--cfg extra_cap` feature-flag stand-in

Every generated fn is intentionally EASY (single unambiguous implementation,
correctness computed here in Python and asserted in the emitted Rust test) —
volume/wiring is the point, not per-fn difficulty. Idempotent: re-running
overwrites the fixture from scratch.

Usage:
    .venv/bin/python tools/hardcrate_multi_gen.py [--core-files N] [--ext-files N]
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "hardcrate_multi"

# fns per generated file, per family — kept small so each file is a realistic
# per-file-dispatch unit (mirrors hardcrate's one-trap-per-file granularity)
# rather than one giant bag of fns.
FNS_PER_FILE = 6


def _wrap_sum_variant(n: int) -> tuple[str, str]:
    lists = [
        [1, 2, 3],
        [200, 100 + (n % 50)],
        [255, 1, n % 10],
        list(range(n % 7, n % 7 + 5)),
    ]
    cases = []
    for xs in lists:
        expected = sum(xs) % 256
        cases.append((xs, expected))
    body = f"""/// Sum bytes with wrapping (modular) `u8` arithmetic. Variant {n}.
pub fn wrap_sum_{n}(xs: &[u8]) -> u8 {{
    xs.iter().fold(0u8, |acc, &x| acc.wrapping_add(x))
}}
"""
    test = f"""    #[test]
    fn wrap_sum_{n}_wraps_on_overflow() {{
"""
    for xs, expected in cases:
        test += f"        assert_eq!(wrap_sum_{n}(&{xs!r}), {expected});\n"
    test += "    }\n"
    return body, test


def _clamp_add_variant(n: int) -> tuple[str, str]:
    lo, hi = -50 - n, 50 + n
    cases = [(10, 5), (1000, 2000), (-1000, -2000), (0, 0), (n, -n)]
    body = f"""/// Add then clamp to [lo, hi]. Variant {n}.
pub fn clamp_add_{n}(a: i32, b: i32, lo: i32, hi: i32) -> i32 {{
    (a + b).clamp(lo, hi)
}}
"""
    test = f"    #[test]\n    fn clamp_add_{n}_clamps(){{\n"
    for a, b in cases:
        expected = max(lo, min(hi, a + b))
        test += f"        assert_eq!(clamp_add_{n}({a}, {b}, {lo}, {hi}), {expected});\n"
    test += "    }\n"
    return body, test


def _hex_codec_variant(n: int) -> tuple[str, str]:
    ids = [0, 1, 16, 42, 255, 4096, 65535, (n * 137) % 65536]
    body = f"""/// Encode an id to 4-char zero-padded lowercase hex. Variant {n}.
pub fn encode_id_{n}(id: u16) -> String {{
    format!("{{id:04x}}")
}}

/// Decode a wire string back to its id; `None` if not valid hex. Variant {n}.
pub fn decode_id_{n}(s: &str) -> Option<u16> {{
    u16::from_str_radix(s, 16).ok()
}}
"""
    ids_lit = "[" + ", ".join(f"{ids[0]}u16" if i == 0 else str(v)
                              for i, v in enumerate(ids)) + "]"
    test = f"""    #[test]
    fn hex_codec_{n}_roundtrips() {{
        for id in {ids_lit} {{
            assert_eq!(decode_id_{n}(&encode_id_{n}(id)), Some(id));
        }}
        assert_eq!(decode_id_{n}("zz"), None);
    }}
"""
    return body, test


def _bit_ops_variant(n: int) -> tuple[str, str]:
    xs = [0, 1, 255, 128, (n * 31) % 256, (n * 53 + 7) % 256]
    body = f"""/// Count set bits. Variant {n}.
pub fn count_set_bits_{n}(x: u8) -> u32 {{
    x.count_ones()
}}

/// True if `x` has an odd number of set bits. Variant {n}.
pub fn is_odd_parity_{n}(x: u8) -> bool {{
    x.count_ones() % 2 == 1
}}
"""
    test = f"    #[test]\n    fn bit_ops_{n}_matches_popcount() {{\n"
    for x in xs:
        ones = bin(x).count("1")
        test += (f"        assert_eq!(count_set_bits_{n}({x}), {ones});\n"
                  f"        assert_eq!(is_odd_parity_{n}({x}), {str(ones % 2 == 1).lower()});\n")
    test += "    }\n"
    return body, test


CORE_FAMILIES = [
    ("wrap_sum", _wrap_sum_variant),
    ("clamp_add", _clamp_add_variant),
    ("hex_codec", _hex_codec_variant),
    ("bit_ops", _bit_ops_variant),
]


def _scoring_module() -> str:
    return '''//! Hand-written anchor: the PUBLIC API `hcm_ext` depends on across the crate
//! boundary. Reconstructing `hcm_ext` correctly requires the pipeline to see
//! THIS module's public signatures (via the workspace dependency graph) without
//! ever seeing `hcm_core`'s private internals.

/// Something that assigns a score to an integer.
pub trait Scorer {
    fn score(&self, x: i32) -> i32;
}

/// Multiplies by a fixed weight.
pub struct Weighted(pub i32);

impl Scorer for Weighted {
    fn score(&self, x: i32) -> i32 {
        x * self.0
    }
}

/// Adds a fixed bias.
pub struct Biased(pub i32);

impl Scorer for Biased {
    fn score(&self, x: i32) -> i32 {
        x + self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn weighted_scores() {
        assert_eq!(Weighted(3).score(4), 12);
        assert_eq!(Weighted(-2).score(5), -10);
    }

    #[test]
    fn biased_scores() {
        assert_eq!(Biased(3).score(4), 7);
        assert_eq!(Biased(-2).score(5), 3);
    }
}
'''


def _nightly_provide_module() -> str:
    return '''//! Real nightly-toolchain trap (not a `--cfg` stand-in): gated on
//! `hcm_nightly`, which `build.rs` only sets when it can actually compile the
//! `error_generic_member_access` API — mirrors anyhow's own `src/nightly.rs`
//! probe technique exactly (same feature, same `Request`/`provide_ref` shape),
//! so the pipeline's nightly-cluster/toolchain-selection path gets a SECOND,
//! independent crate to prove itself on, not just anyhow.

use core::error::{self, Error, Request};
use core::fmt;

#[derive(Debug)]
pub struct TaggedError {
    pub tag: u32,
}

impl fmt::Display for TaggedError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "tagged error: {}", self.tag)
    }
}

impl Error for TaggedError {
    fn provide<'a>(&'a self, request: &mut Request<'a>) {
        request.provide_ref::<u32>(&self.tag);
    }
}

/// Pull the `u32` tag back out of any `dyn Error` that provided one.
pub fn extract_tag(err: &dyn Error) -> Option<u32> {
    error::request_ref::<u32>(err).copied()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_provided_tag() {
        let e = TaggedError { tag: 42 };
        assert_eq!(extract_tag(&e), Some(42));
    }

    #[derive(Debug)]
    struct Other;

    impl fmt::Display for Other {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            write!(f, "other")
        }
    }

    impl Error for Other {}

    #[test]
    fn none_for_error_that_never_provided() {
        assert_eq!(extract_tag(&Other), None);
    }
}
'''


def _emit_family_file(path: Path, family_name: str, fns: list[tuple[str, str]]) -> None:
    bodies = "\n".join(b for b, _t in fns)
    tests = "\n".join(t for _b, t in fns)
    path.write_text(
        f"{bodies}\n#[cfg(test)]\nmod tests {{\n    use super::*;\n\n{tests}}}\n",
        encoding="utf-8",
    )


def _ext_consumer_variant(n: int) -> tuple[str, str]:
    items = [1, 2, 3, (n % 5) + 1, -4]
    w = (n % 7) - 3 or 2
    b = (n % 5) - 2
    expected_w = sum(x * w for x in items)
    expected_b = sum(x + b for x in items)
    body = f"""/// Total score of `items` under a `Weighted` scorer. Variant {n}.
pub fn total_weighted_{n}(items: &[i32], w: &Weighted) -> i32 {{
    items.iter().map(|&x| w.score(x)).sum()
}}

/// Total score of `items` under a `Biased` scorer. Variant {n}.
pub fn total_biased_{n}(items: &[i32], b: &Biased) -> i32 {{
    items.iter().map(|&x| b.score(x)).sum()
}}
"""
    test = f"""    #[test]
    fn consumer_{n}_totals_match() {{
        let items: Vec<i32> = vec!{items!r};
        assert_eq!(total_weighted_{n}(&items, &Weighted({w})), {expected_w});
        assert_eq!(total_biased_{n}(&items, &Biased({b})), {expected_b});
    }}
"""
    return body, test


def generate(core_files: int, ext_files: int) -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    core_src = ROOT / "core" / "src"
    core_tests = ROOT / "core" / "tests"
    ext_src = ROOT / "ext" / "src"
    ext_tests = ROOT / "ext" / "tests"
    for d in (core_src, core_tests, ext_src, ext_tests):
        d.mkdir(parents=True)

    (ROOT / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["core", "ext"]\nresolver = "2"\n', encoding="utf-8")

    (ROOT / "core" / "Cargo.toml").write_text(
        '[package]\nname = "hcm_core"\nversion = "0.1.0"\nedition = "2021"\n'
        'build = "build.rs"\n\n[lib]\npath = "src/lib.rs"\n',
        encoding="utf-8",
    )
    (ROOT / "core" / "build.rs").write_text(_BUILD_RS, encoding="utf-8")

    core_mods: list[str] = []
    family_counts = {name: 0 for name, _ in CORE_FAMILIES}
    first_variant_n: dict[str, int] = {}
    n = 0
    for file_i in range(core_files):
        family_name, fn_ = CORE_FAMILIES[file_i % len(CORE_FAMILIES)]
        first_variant_n.setdefault(family_name, n)
        variants = []
        for _ in range(FNS_PER_FILE):
            variants.append(fn_(n))
            n += 1
        family_counts[family_name] += 1
        mod_name = f"{family_name}_{family_counts[family_name]}"
        _emit_family_file(core_src / f"{mod_name}.rs", family_name, variants)
        core_mods.append(mod_name)

    (core_src / "scoring.rs").write_text(_scoring_module(), encoding="utf-8")
    (core_src / "nightly_provide.rs").write_text(_nightly_provide_module(), encoding="utf-8")

    lib_lines = [
        "//! `hcm_core` — the SCALE + multi-crate fixture's dependency root.",
        "//! See `tools/hardcrate_multi_gen.py` for how this file is generated.",
        "#![cfg_attr(hcm_nightly, feature(error_generic_member_access))]",
        "",
    ]
    for m in core_mods:
        lib_lines.append(f"mod {m};")
    lib_lines.append("mod scoring;")
    lib_lines.append("#[cfg(hcm_nightly)]")
    lib_lines.append("mod nightly_provide;")
    lib_lines.append("")
    for m in core_mods:
        lib_lines.append(f"pub use {m}::*;")
    lib_lines.append("pub use scoring::*;")
    lib_lines.append("#[cfg(hcm_nightly)]")
    lib_lines.append("pub use nightly_provide::*;")
    (core_src / "lib.rs").write_text("\n".join(lib_lines) + "\n", encoding="utf-8")

    wn = first_variant_n["wrap_sum"]
    cn = first_variant_n["clamp_add"]
    hn = first_variant_n["hex_codec"]
    bn = first_variant_n["bit_ops"]
    (core_tests / "smoke.rs").write_text(
        "//! Integration smoke pin — every family's public API, end to end.\n"
        "use hcm_core::*;\n\n"
        "#[test]\nfn core_public_api_smoke() {\n"
        f"    assert_eq!(wrap_sum_{wn}(&[200, 100]), 44);\n"
        f"    assert_eq!(clamp_add_{cn}(1000, 2000, -50, 50), 50);\n"
        f"    assert_eq!(decode_id_{hn}(&encode_id_{hn}(42)), Some(42));\n"
        f"    assert_eq!(count_set_bits_{bn}(255), 8);\n"
        "    assert_eq!(Weighted(2).score(5), 10);\n"
        "}\n",
        encoding="utf-8",
    )

    # --- ext crate: depends on core, consumes its public Scorer API ---------
    (ROOT / "ext" / "Cargo.toml").write_text(
        '[package]\nname = "hcm_ext"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[lib]\npath = "src/lib.rs"\n\n[dependencies]\nhcm_core = { path = "../core" }\n',
        encoding="utf-8",
    )
    ext_mods: list[str] = []
    n = 0
    for file_i in range(ext_files):
        variants = []
        for _ in range(FNS_PER_FILE):
            variants.append(_ext_consumer_variant(n))
            n += 1
        mod_name = f"consumers_{file_i + 1}"
        body = "use hcm_core::{Biased, Scorer, Weighted};\n\n" + "\n".join(
            b for b, _t in variants)
        tests = "\n".join(t for _b, t in variants)
        (ext_src / f"{mod_name}.rs").write_text(
            f"{body}\n#[cfg(test)]\nmod tests {{\n    use super::*;\n\n{tests}}}\n",
            encoding="utf-8",
        )
        ext_mods.append(mod_name)

    ext_lib_lines = [
        "//! `hcm_ext` — depends on `hcm_core` across a REAL crate boundary.",
        "//! See `tools/hardcrate_multi_gen.py` for how this file is generated.",
        "",
    ]
    for m in ext_mods:
        ext_lib_lines.append(f"mod {m};")
    ext_lib_lines.append("")
    for m in ext_mods:
        ext_lib_lines.append(f"pub use {m}::*;")
    (ext_src / "lib.rs").write_text("\n".join(ext_lib_lines) + "\n", encoding="utf-8")

    (ext_tests / "smoke.rs").write_text(
        "//! Integration smoke pin for the cross-crate consumer API.\n"
        "use hcm_core::{Biased, Weighted};\n"
        "use hcm_ext::*;\n\n"
        "#[test]\nfn ext_public_api_smoke() {\n"
        "    let items = vec![1, 2, 3];\n"
        "    assert_eq!(total_weighted_0(&items, &Weighted(2)), 12);\n"
        "    assert_eq!(total_biased_0(&items, &Biased(1)), 9);\n"
        "}\n",
        encoding="utf-8",
    )

    core_fn_count = core_files * FNS_PER_FILE * 2 + 4  # families emit ~2 fns/variant, scoring=4
    ext_fn_count = ext_files * FNS_PER_FILE * 2
    print(f"Generated {ROOT}")
    print(f"  hcm_core: {core_files + 2} file(s), ~{core_fn_count} fn(s)"
          " (incl. scoring.rs + nightly_provide.rs)")
    print(f"  hcm_ext:  {ext_files} file(s), ~{ext_fn_count} fn(s)")


_BUILD_RS = r'''use std::env;
use std::path::Path;
use std::process::{Command, Stdio};

// Mirrors anyhow's real build.rs probe technique exactly (see
// `git show master:build.rs` / `src/nightly.rs` in tmp/anyhow): actually try to
// COMPILE a tiny snippet using the unstable `error_generic_member_access` API
// under the current toolchain, rather than guessing from a version string.
// Success -> emit a custom cfg; `src/lib.rs` gates `feature(...)` on that same
// cfg, and `mod nightly_provide;` is gated on it too.
fn main() {
    println!("cargo:rustc-check-cfg=cfg(hcm_nightly)");
    println!("cargo:rerun-if-changed=build.rs");
    if probe_supports_provide_api() {
        println!("cargo:rustc-cfg=hcm_nightly");
    }
}

const PROBE_SRC: &str = r#"
#![feature(error_generic_member_access)]
use core::error::{Error, Request};
use core::fmt;
#[derive(Debug)]
struct X;
impl fmt::Display for X {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { write!(f, "X") }
}
impl Error for X {
    fn provide<'a>(&'a self, request: &mut Request<'a>) {
        request.provide_ref::<u32>(&1u32);
    }
}
"#;

fn probe_supports_provide_api() -> bool {
    let rustc = env::var_os("RUSTC").unwrap_or_else(|| "rustc".into());
    let out_dir = match env::var_os("OUT_DIR") {
        Some(d) => d,
        None => return false,
    };
    let probe_dir = Path::new(&out_dir).join("hcm_probe");
    if std::fs::create_dir_all(&probe_dir).is_err() {
        return false;
    }
    let probe_src = probe_dir.join("probe.rs");
    if std::fs::write(&probe_src, PROBE_SRC).is_err() {
        return false;
    }

    let status = Command::new(rustc)
        .arg("--edition=2021")
        .arg("--crate-type=lib")
        .arg("--cap-lints=allow")
        .arg("--emit=metadata")
        .arg("--out-dir")
        .arg(&probe_dir)
        .arg(&probe_src)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();

    let _ = std::fs::remove_dir_all(&probe_dir);
    matches!(status, Ok(s) if s.success())
}
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--core-files", type=int, default=12,
                     help="generated (non-anchor) file count in hcm_core (default 12)")
    ap.add_argument("--ext-files", type=int, default=6,
                     help="generated file count in hcm_ext (default 6)")
    args = ap.parse_args()
    generate(args.core_files, args.ext_files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

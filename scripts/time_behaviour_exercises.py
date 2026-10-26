#!/usr/bin/env python3
"""Time one behaviour-exercise spec the way task 0d runs it.

WHY. On run 42057-641e589c (2026-10-25) the exercise step was about 250
minutes and made no model call. Per spec the runner starts `cargo test
--exact` and then `grcov --binary-path target/debug`, which reads every
binary in that directory. Which of those two is the minutes decides which
half of the runner changes. The test binary's own time is the part neither
change removes.

USAGE. python scripts/time_behaviour_exercises.py ORIGIN [SUITE_JSON]
ORIGIN is the original crate. The script copies it with `cp -rL` into a
scratch directory and removes any `target` in the copy, so a previous build
is not the measurement and ORIGIN is not written. SUITE_JSON is a behaviour
suite; the first 10 test-assertion specs with a `tests/<file>::<fn>` source
are the rows. Without it, the first 10 `#[test] fn` names under `tests/` are
used. Needs `grcov` on PATH and a toolchain that can build ORIGIN. No model
and no pipeline. Scratch is `/tmp/behaviour-exercise-time`.

HOW TO INTERPRET. Each row is one spec, in seconds: `cargo` is `cargo test
--exact` under the instrumented env after one `cargo test --no-run`;
`binary` is that spec's test binary run with `--exact`; `grcov_dir` is grcov
with `--binary-path` the debug directory; `grcov_bin` is grcov with
`--binary-path` that one binary. The summary prints the sum and the median
of each column.
Change the per-spec cargo spawn when the sum of `cargo - binary` is the wall
time. Change the grcov path when the sum of `grcov_dir - grcov_bin` is the
wall time. When `binary` is the sum, neither change takes the hours.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

SCRATCH = Path("/tmp/behaviour-exercise-time")
N = 10


def _copy(origin: Path) -> Path:
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    subprocess.run(["cp", "-rL", str(origin), str(SCRATCH)], check=True)
    target = SCRATCH / "target"
    if target.exists():
        shutil.rmtree(target)
    return SCRATCH


def _specs_from_suite(path: Path) -> list[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for spec in data.get("specs", []):
        if not str(spec.get("behaviour_id", "")).startswith("testassert::"):
            continue
        ref = str(spec.get("source_ref", ""))
        if "::" not in ref:
            continue
        file, _, name = ref.rpartition("::")
        stem = file.rsplit("/", 1)[-1]
        if stem.endswith(".rs"):
            stem = stem[:-3]
        if stem and name:
            out.append((stem, name))
        if len(out) == N:
            break
    return out


def _specs_from_tree(root: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted((root / "tests").glob("*.rs")):
        text = path.read_text(encoding="utf-8", errors="replace")
        stem = path.stem
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("fn ") and "#[test]" in text:
                pass
        # The attribute sits on the line above the fn. A line-pair walk.
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.strip() != "#[test]":
                continue
            for nxt in lines[i + 1:i + 6]:
                s = nxt.strip()
                if s.startswith("fn "):
                    name = s[3:].split("(", 1)[0].strip()
                    if name:
                        out.append((stem, name))
                    break
            if len(out) == N:
                return out
    return out


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[float, int, str]:
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True)
    return time.monotonic() - t0, proc.returncode, proc.stdout + proc.stderr


def _binaries(stdout: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-artifact":
            continue
        target = msg.get("target") or {}
        if "test" not in (target.get("kind") or []):
            continue
        exe = msg.get("executable")
        name = target.get("name")
        if exe and name:
            found[str(name)] = Path(exe)
    return found


def _grcov(profraw: Path, binary_path: Path, out: Path, root: Path) -> list[str]:
    return [
        "grcov", str(profraw),
        "--binary-path", str(binary_path),
        "--source-dir", str(root),
        "--output-type", "coveralls",
        "--token", "unused",
        "--branch",
        "--ignore-not-existing",
        "--output-path", str(out),
        "--ignore", "/*",
    ]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    origin = Path(sys.argv[1]).resolve()
    suite = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    root = _copy(origin)
    specs = _specs_from_suite(suite) if suite else []
    if len(specs) < N:
        specs = _specs_from_tree(root)
    specs = specs[:N]
    if not specs:
        print("no specs")
        return 1
    env = dict(os.environ)
    env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -C instrument-coverage").strip()
    env["CARGO_INCREMENTAL"] = "0"
    env["CARGO_TARGET_DIR"] = str(root / "target")
    build_s, rc, out = _run(
        ["cargo", "test", "--no-run", "--message-format=json"], root, env, 900)
    print(f"instrumented --no-run: {build_s:.1f}s rc={rc}")
    if rc != 0:
        print(out[-1500:])
        return 1
    binaries = _binaries(out)
    rows: list[tuple[str, float, float, float, float]] = []
    for i, (stem, name) in enumerate(specs):
        binary = binaries.get(stem)
        if binary is None:
            print(f"skip {stem}::{name}: no test binary")
            continue
        prof_cargo = root / "target" / f"prof-cargo-{i}"
        prof_bin = root / "target" / f"prof-bin-{i}"
        for d in (prof_cargo, prof_bin):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
        cargo_env = dict(env)
        cargo_env["LLVM_PROFILE_FILE"] = str(prof_cargo / "%p-%m.profraw")
        cargo_s, cargo_rc, _ = _run(
            ["cargo", "test", "--quiet", "--test", stem, "--", "--exact", name],
            root, cargo_env, 600)
        bin_env = dict(env)
        bin_env["LLVM_PROFILE_FILE"] = str(prof_bin / "%p-%m.profraw")
        bin_s, bin_rc, _ = _run([str(binary), "--exact", name], root, bin_env, 600)
        grcov_dir_s = grcov_bin_s = 0.0
        if any(prof_bin.glob("*.profraw")):
            grcov_dir_s, dir_rc, dir_err = _run(
                _grcov(prof_bin, root / "target" / "debug", prof_bin / "dir.json", root),
                root, env, 300)
            grcov_bin_s, bin_grc, bin_err = _run(
                _grcov(prof_bin, binary, prof_bin / "bin.json", root),
                root, env, 300)
            if dir_rc != 0 or bin_grc != 0:
                print(f"  grcov rc dir={dir_rc} bin={bin_grc} {bin_err[-200:]}")
        label = f"{stem}::{name}"
        print(f"{label}  cargo={cargo_s:.2f} rc={cargo_rc}  binary={bin_s:.2f} rc={bin_rc}"
              f"  grcov_dir={grcov_dir_s:.2f}  grcov_bin={grcov_bin_s:.2f}")
        rows.append((label, cargo_s, bin_s, grcov_dir_s, grcov_bin_s))
    if not rows:
        print("no row completed")
        return 1

    def col(n: int) -> list[float]:
        return [r[n] for r in rows]

    def show(title: str, xs: list[float]) -> None:
        print(f"{title}: sum={sum(xs):.1f}s median={statistics.median(xs):.2f}s n={len(xs)}")

    show("cargo", col(1))
    show("binary", col(2))
    show("grcov_dir", col(3))
    show("grcov_bin", col(4))
    spawn = [c - b for _l, c, b, _d, _g in rows]
    export = [d - g for _l, _c, _b, d, g in rows]
    show("cargo_minus_binary", spawn)
    show("grcov_dir_minus_bin", export)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

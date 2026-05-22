#!/usr/bin/env python3
"""Remove GRCOV_EXCL_START/STOP marker lines from all Rust source files in tmp/."""
import sys
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "tmp/anyhow"
src_dir = Path(project) / "src"

if not src_dir.exists():
    print(f"Not found: {src_dir}")
    sys.exit(1)

for rs in sorted(src_dir.rglob("*.rs")):
    original = rs.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    filtered = [l for l in lines if "GRCOV_EXCL_START" not in l and "GRCOV_EXCL_STOP" not in l]
    if len(filtered) != len(lines):
        rs.write_text("".join(filtered), encoding="utf-8")
        print(f"  {rs.relative_to(Path(project))}: removed {len(lines) - len(filtered)} EXCL line(s)")

print("Done.")

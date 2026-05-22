#!/usr/bin/env python3
"""Remove all gen_tests pipeline artifacts from a project's src/ tree.

Cleans up:
- GRCOV_EXCL_START/STOP marker lines (legacy, from pre-sibling-file design)
- foo.gen_tests_N.rs sibling files (deleted)
- `#[cfg(test)] #[path = "...gen_tests_N.rs"] mod gen_tests_N;` declarations
"""
import re
import sys
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "tmp/anyhow"
src_dir = Path(project) / "src"

if not src_dir.exists():
    print(f"Not found: {src_dir}")
    sys.exit(1)

DECL_RE = re.compile(
    r'\n*#\[cfg\(test\)\][\s\n]*'
    r'(?:#\[[^\]]*\][\s\n]*)*'
    r'#\[path\s*=\s*"[^"]+\.gen_tests_\d+\.rs"\][\s\n]*'
    r'mod\s+gen_tests_\d+\s*;',
)

sibling_count = 0
for rs in sorted(src_dir.rglob("*.gen_tests_*.rs")):
    if re.search(r'\.gen_tests_\d+\.rs$', rs.name):
        rs.unlink()
        sibling_count += 1
        print(f"  deleted sibling: {rs.relative_to(Path(project))}")

excl_count = decl_count = 0
for rs in sorted(src_dir.rglob("*.rs")):
    original = rs.read_text(encoding="utf-8")
    text = original

    # Legacy EXCL markers.
    lines = text.splitlines(keepends=True)
    filtered = [l for l in lines if "GRCOV_EXCL_START" not in l and "GRCOV_EXCL_STOP" not in l]
    if len(filtered) != len(lines):
        excl_count += len(lines) - len(filtered)
        text = "".join(filtered)

    # Path-mod declarations.
    new_text, n_decls = DECL_RE.subn("", text)
    if n_decls > 0:
        decl_count += n_decls
        text = new_text

    if text != original:
        rs.write_text(text, encoding="utf-8")
        print(f"  cleaned: {rs.relative_to(Path(project))}")

print(f"Done. siblings deleted={sibling_count}, EXCL lines removed={excl_count}, decls removed={decl_count}")

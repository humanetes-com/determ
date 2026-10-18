#!/usr/bin/env python3
"""Write the distinctive names a reconstructed crate declares, for the crate-symbol gate.

WHY
    Hard Rule 11: the pipeline may not carry the crate under reconstruction's
    own answers. On 2026-10-17 an AST scan of the pipeline's executed string
    literals (not docstrings, not comments) found 16 literals in 6 files
    naming a symbol of `anyhow` — a catalog driver whose recipes named the
    wrapper type, the vtable builders and the exact `construct(...)` call,
    one environment flip from a prompt, and one live literal
    (`anyhow_kind` in the test locator's generic-name set). The catalog was
    deleted the same day. `tools/check_crate_symbols.py` is the gate that keeps
    a new one out; it needs the list of names to ban, and this script writes
    it from the crate's ORIGINAL source (a ref taken before strip), once per
    crate, into `tools/crate_symbols/<crate>.txt`.

USAGE
    python scripts/crate_symbol_list.py --workspace tmp/anyhow \
        --ref task0c-pre-anyhow --out tools/crate_symbols/anyhow.txt
    No GPU, no cargo, no model. Reads `git show <ref>:<path>` for every
    `src/**/*.rs` at that ref (generated test siblings excluded) and writes the
    list. Run it again only when the crate's original source changes; the
    gate reads the file, never the workspace, so `check.sh` works on a
    checkout with no `tmp/`.

HOW TO INTERPRET THE OUTPUT
    The file holds one identifier per line, sorted, after a comment header
    naming the workspace and ref. An identifier is listed when the crate
    DECLARES it (`fn`, `struct`, `enum`, `trait`, `type`, `union`,
    `macro_rules!`) on a line that is not a comment (doc examples declare
    names the crate does not have), it is distinctive (contains `_` inside
    the name or a lower-to-upper case step: `object_drop`, `ErrorImpl`;
    single words like `new`, `msg`, `context` are vocabulary the pipeline's
    own text uses), and it is not a name the crate shares with the standard
    library (`STD_SHARED` below: a std name in a prompt is the language, not
    the crate). The printed summary is `declared=N distinctive=M listed=K`;
    K is the size of the ban list the gate enforces.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

# Names the crate declares that std (or the language's own test harness)
# also declares. Banning them would ban the language: `as_ref` in a prompt
# is AsRef, not the crate. Extend this when a new crate's list catches a
# std name; the gate's allowlist is for the opposite case (a crate name a
# pipeline string must carry, with its reason).
STD_SHARED = frozenset({
    "as_mut", "as_ptr", "as_ref", "as_str", "by_mut", "by_ref", "deref_mut",
    "downcast_mut", "downcast_ref", "from_raw", "from_str", "into_raw",
    "must_use", "next_back", "request_ref", "request_value", "size_hint",
    "write_str", "write_fmt", "to_string", "StdError", "type_id",
})

_DECL = re.compile(
    r"\b(?:fn|struct|enum|trait|type|union|macro_rules!)\s+([A-Za-z_][A-Za-z0-9_]*)")


def _distinctive(name: str) -> bool:
    core = name.strip("_")
    return "_" in core or re.search(r"[a-z][A-Z]", core) is not None


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True, text=True, check=True).stdout


def declared_names(workspace: Path, ref: str) -> set[str]:
    names: set[str] = set()
    for rel in _git(workspace, "ls-tree", "-r", "--name-only", ref, "--", "src").split():
        if not rel.endswith(".rs") or ".gen_tests_" in rel:
            continue
        for line in _git(workspace, "show", f"{ref}:{rel}").splitlines():
            if line.lstrip().startswith("//"):
                continue
            names.update(m.group(1) for m in _DECL.finditer(line))
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    names = declared_names(a.workspace, a.ref)
    distinctive = {n for n in names if _distinctive(n)}
    listed = sorted(distinctive - STD_SHARED)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(
        f"# Distinctive names declared by {a.workspace.name} at {a.ref}.\n"
        "# Written by scripts/crate_symbol_list.py; read by "
        "tools/check_crate_symbols.py.\n"
        + "".join(f"{n}\n" for n in listed),
        encoding="utf-8")
    print(f"declared={len(names)} distinctive={len(distinctive)} "
          f"listed={len(listed)} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

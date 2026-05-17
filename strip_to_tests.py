#!/usr/bin/env python3
"""
strip_to_tests.py -- Strip a project down to test code only.

Removes implementation source code while preserving ALL non-source files.
Only files whose extension is in SOURCE_EXTENSIONS are ever touched.
Everything else (Cargo.toml, package.json, build.gradle, *.proto, *.toml,
*.json, *.yaml, etc.) is left completely untouched.

For source files the script:
  - Keeps pure test files entirely (files in tests/ directories, test_*.py, etc.)
  - Strips mixed files down to their test blocks only
  - Deletes implementation-only source files (no test code found)

When done, the project will no longer compile or pass tests, but all test
code is preserved as signal for the pipeline to reconstruct the implementation.

Usage:
    python strip_to_tests.py <project_dir> [--dry-run]

Supported languages (auto-detected):
    - Rust  (Cargo.toml present)
    - Python (pyproject.toml or *.py files present)
    - JavaScript/TypeScript (package.json present)
"""

import argparse
import os
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

def detect_language(project_dir: Path) -> str:
    if (project_dir / "Cargo.toml").exists():
        return "rust"
    if (project_dir / "package.json").exists():
        return "javascript"
    if any(project_dir.glob("*.py")) or (project_dir / "pyproject.toml").exists():
        return "python"
    return "unknown"


# ---------------------------------------------------------------------------
# Source code extensions: ONLY files with these extensions are ever touched.
# All other files (Cargo.toml, package.json, *.proto, *.toml, *.yaml, etc.)
# are left completely untouched regardless of language.
# ---------------------------------------------------------------------------

SOURCE_EXTENSIONS = {
    # Systems / compiled
    ".rs", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".go", ".swift", ".kt", ".kts", ".java", ".cs", ".vb", ".fs", ".fsx",
    ".d", ".nim", ".cr", ".zig", ".v", ".odin",
    # Scripting
    ".py", ".rb", ".pl", ".pm", ".php", ".php3", ".php4", ".php5", ".phtml",
    ".lua", ".sh", ".bash", ".zsh", ".fish", ".tcl",
    # JVM / functional
    ".scala", ".sc", ".clj", ".cljs", ".cljc", ".groovy", ".gvy", ".gradle",
    ".ex", ".exs", ".hs", ".lhs", ".ml", ".mli", ".mll", ".mly",
    ".erl", ".hrl", ".elm", ".purs",
    # Web / JS
    ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx",
    ".coffee", ".vue", ".svelte",
    # Database / query
    ".sql",
    # Other
    ".r", ".R", ".m", ".mm", ".f", ".f90", ".f95", ".f03", ".f08",
    ".ada", ".adb", ".ads", ".pas", ".pp",
}

# Directories to never descend into
SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "target",           # Rust build output
    "node_modules",     # Node.js deps
    "vendor",           # Go / PHP / Ruby vendored deps
    ".venv", "venv", "env",
    "__pycache__", ".mypy_cache",
    "dist", "build", ".build",
    ".tox", ".eggs",
    "farf",             # Solana test ledger artifacts
}


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------

# Directory names that are pure test directories in Rust
RUST_TEST_DIRS = {"tests"}

# Test attributes that mark a function or block as test code
RUST_TEST_ATTRS = re.compile(
    r"#\s*\[\s*(?:cfg\s*\(\s*test\s*\)|test|tokio::test|async_std::test|"
    r"serial_test::serial|rstest|parameterized|test_case)\s*[^\]]*\]"
)


def _delimiter_depth_change(line: str) -> tuple:
    """
    Count net depth change for braces {}, parens (), and brackets [] on a line.
    Ignores delimiters inside string literals and line comments.
    Returns (brace_delta, paren_delta, bracket_delta).
    """
    brace = paren = bracket = 0
    in_str = False
    str_char = None
    i = 0
    while i < len(line):
        c = line[i]
        if not in_str and c == '/' and i + 1 < len(line) and line[i + 1] == '/':
            break
        if c in ('"', '\'') and (i == 0 or line[i - 1] != '\\'):
            if not in_str:
                in_str = True
                str_char = c
            elif c == str_char:
                in_str = False
        elif not in_str:
            if c == '{':
                brace += 1
            elif c == '}':
                brace -= 1
            elif c == '(':
                paren += 1
            elif c == ')':
                paren -= 1
            elif c == '[':
                bracket += 1
            elif c == ']':
                bracket -= 1
        i += 1
    return brace, paren, bracket


def _brace_depth_change(line: str) -> int:
    """Net brace-only depth change for a line (convenience wrapper)."""
    return int(_delimiter_depth_change(line)[0])


def extract_rust_tests(content: str) -> str:
    """
    Extract all test-related code from a Rust source file.

    Keeps:
    - Top-level use / extern crate statements (type context for tests)
    - Any item preceded by #[cfg(test)] -- extracted with full brace-matched block
    - Any item preceded by #[test] (or similar test attributes)
    - Any mod, struct, impl, etc. inside a #[cfg(test)] block (captured by brace matching)

    Returns the test-only content as a string, or empty string if no tests found.
    """
    lines = content.split('\n')
    n = len(lines)

    # Extract test items using attribute detection + brace matching.
    output = []
    i = 0

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Detect test attribute line
        if RUST_TEST_ATTRS.search(stripped):
            # Collect the full attribute stack (may span multiple #[...] lines)
            attr_block = []
            j = i
            while j < n and lines[j].strip().startswith('#['):
                attr_block.append(lines[j])
                j += 1

            # j now points to the item declaration (mod, fn, impl, struct, ...)
            if j >= n:
                i = j
                continue

            item_line = lines[j]
            attr_block.append(item_line)
            j += 1

            # Track all delimiters ({}, (), []) to collect the entire item body.
            bd, pd, brd = _delimiter_depth_change(item_line)

            if bd > 0 or pd > 0 or brd > 0:
                # Multi-line item: collect until all delimiters are closed
                while j < n and (bd > 0 or pd > 0 or brd > 0):
                    attr_block.append(lines[j])
                    db2, dp2, dbr2 = _delimiter_depth_change(lines[j])
                    bd += db2
                    pd += dp2
                    brd += dbr2
                    j += 1
                # If we hit EOF before closing all delimiters, emit synthetic closers
                # so the output is at least syntactically valid.
                if bd > 0:
                    attr_block.append('}' * bd)
                if pd > 0:
                    attr_block.append(')' * pd)
                if brd > 0:
                    attr_block.append(']' * brd)
            # else: single-line item (e.g., a type alias) -- already collected

            output.append('')
            output.extend(attr_block)
            i = j

        else:
            i += 1

    if not output:
        return ""

    return '\n'.join(output).strip()


def is_rust_pure_test_file(path: Path, project_dir: Path) -> bool:
    """True if the file lives inside a tests/ directory."""
    rel = path.relative_to(project_dir)
    return any(part in RUST_TEST_DIRS for part in rel.parts)


def _rust_crate_dirs(project_dir: Path) -> list:
    """
    Return all directories inside project_dir that contain a Cargo.toml,
    excluding SKIP_DIRS subtrees.
    """
    crates = []
    for cargo in project_dir.rglob("Cargo.toml"):
        rel_parts = cargo.relative_to(project_dir).parts
        if any(p in SKIP_DIRS for p in rel_parts):
            continue
        crates.append(cargo.parent)
    return crates


def _rust_needs_lib(cargo_path: Path) -> bool:
    """
    Return True if this Cargo.toml expects a lib target (default when no [[bin]]
    and no explicit [lib] path overriding the default).
    """
    try:
        text = cargo_path.read_text(errors='ignore')
    except Exception:
        return False
    # If there's a [lib] section it's definitely a lib target.
    if re.search(r'^\[lib\]', text, re.MULTILINE):
        return True
    # If there are ONLY [[bin]] entries and no [lib], it's binary-only.
    has_bin = bool(re.search(r'^\[\[bin\]\]', text, re.MULTILINE))
    has_lib = bool(re.search(r'^\[lib\]', text, re.MULTILINE))
    if has_bin and not has_lib:
        return False
    # Default for Rust: if neither [lib] nor [[bin]], cargo expects src/lib.rs or src/main.rs.
    # Treat as lib unless we find a src/main.rs.
    return True


def _rust_bin_paths(cargo_path: Path) -> list:
    """
    Return list of bin paths that must exist for this crate to compile.

    Handles three cases:
    1. Explicit [[bin]] with path = "..." → use those paths.
    2. [[bin]] with no path → fall back to src/main.rs.
    3. default-run = "name" with no [[bin]] → Cargo auto-discovers from
       src/bin/<name>.rs (preferred) or src/main.rs when package name matches.
    """
    try:
        text = cargo_path.read_text(errors='ignore')
    except Exception:
        return []

    # Case 1 & 2: explicit [[bin]] sections.
    if re.search(r'^\[\[bin\]\]', text, re.MULTILINE):
        paths = re.findall(r'path\s*=\s*"([^"]+\.rs)"', text)
        return paths if paths else ["src/main.rs"]

    # Case 3: default-run without [[bin]] — Cargo auto-discovers bins.
    m = re.search(r'^default-run\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if m:
        bin_name = m.group(1)
        crate_dir = cargo_path.parent
        # Check if the auto-discovered bin file exists; if not, return its expected path.
        src_bin = crate_dir / "src" / "bin" / f"{bin_name}.rs"
        src_main = crate_dir / "src" / "main.rs"
        if not src_bin.exists() and not src_main.exists():
            return [f"src/bin/{bin_name}.rs"]

    return []


def _rust_bench_paths(cargo_path: Path) -> list:
    """
    Return list of bench paths declared in [[bench]] sections of Cargo.toml.
    Falls back to benches/<name>.rs when no explicit path is given.
    """
    try:
        text = cargo_path.read_text(errors='ignore')
    except Exception:
        return []
    if not re.search(r'^\[\[bench\]\]', text, re.MULTILINE):
        return []
    # Find all [[bench]] blocks and extract path or name.
    paths = []
    for block in re.split(r'\[\[bench\]\]', text)[1:]:
        path_m = re.search(r'path\s*=\s*"([^"]+\.rs)"', block)
        if path_m:
            paths.append(path_m.group(1))
        else:
            name_m = re.search(r'^name\s*=\s*"([^"]+)"', block, re.MULTILINE)
            if name_m:
                paths.append(f"benches/{name_m.group(1)}.rs")
    return paths


def _ensure_rust_crate_stubs(project_dir: Path, dry_run: bool) -> int:
    """
    After stripping, ensure every Rust crate still has a valid entry-point file.
    - For lib crates missing src/lib.rs: create a stub with pub mod declarations
      for every remaining .rs file in src/ (so the compiler can see them).
    - For bin crates missing their main file: create a stub fn main() {}.
    - For bench targets missing their bench file: create a stub fn main() {}.
    Returns number of stubs created.
    """
    created = 0
    for crate_dir in _rust_crate_dirs(project_dir):
        cargo = crate_dir / "Cargo.toml"
        src_dir = crate_dir / "src"

        # --- lib target ---
        if _rust_needs_lib(cargo):
            lib_rs = src_dir / "lib.rs"
            if not lib_rs.exists():
                # Gather remaining .rs siblings to declare as modules.
                if src_dir.exists():
                    siblings = sorted(
                        p.stem for p in src_dir.iterdir()
                        if p.is_file() and p.suffix == ".rs" and p.name != "lib.rs"
                    )
                    subdirs = sorted(
                        p.name for p in src_dir.iterdir()
                        if p.is_dir() and (p / "mod.rs").exists()
                    )
                else:
                    siblings = []
                    subdirs = []
                lines = [f"pub mod {m};" for m in siblings + subdirs]
                stub = "\n".join(lines) + ("\n" if lines else "")
                rel = lib_rs.relative_to(project_dir)
                print(f"  STUB    {rel}  (lib entry point, {len(lines)} mod declarations)")
                if not dry_run:
                    src_dir.mkdir(parents=True, exist_ok=True)
                    lib_rs.write_text(stub)
                created += 1

        # --- bin targets ---
        for bin_rel in _rust_bin_paths(cargo):
            bin_path = crate_dir / bin_rel
            if not bin_path.exists():
                rel = bin_path.relative_to(project_dir)
                print(f"  STUB    {rel}  (bin entry point)")
                if not dry_run:
                    bin_path.parent.mkdir(parents=True, exist_ok=True)
                    bin_path.write_text("fn main() {}\n")
                created += 1

        # --- bench targets ---
        for bench_rel in _rust_bench_paths(cargo):
            bench_path = crate_dir / bench_rel
            if not bench_path.exists():
                rel = bench_path.relative_to(project_dir)
                print(f"  STUB    {rel}  (bench entry point)")
                if not dry_run:
                    bench_path.parent.mkdir(parents=True, exist_ok=True)
                    bench_path.write_text("fn main() {}\n")
                created += 1

    return created


def process_rust(project_dir: Path, dry_run: bool):
    """Strip a Rust project: only touch .rs files, leave everything else alone."""
    deleted = 0
    stripped = 0
    kept = 0

    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(project_dir)
        parts = rel.parts

        # Skip directories we never descend into
        if any(p in SKIP_DIRS for p in parts):
            continue

        # Only process source files -- everything else is untouched
        if path.suffix not in SOURCE_EXTENSIONS:
            kept += 1
            continue

        # Pure test files (inside tests/ directory) -- keep entirely
        if is_rust_pure_test_file(path, project_dir):
            kept += 1
            continue

        # .rs source file: extract test blocks, delete if none
        try:
            content = path.read_text(errors='ignore')
        except Exception:
            continue

        if '#[test]' not in content and '#[cfg(test)]' not in content:
            print(f"  DELETE  {rel}")
            if not dry_run:
                path.unlink()
            deleted += 1
        else:
            test_content = extract_rust_tests(content)
            if not test_content.strip():
                print(f"  DELETE  {rel}  (extraction yielded nothing)")
                if not dry_run:
                    path.unlink()
                deleted += 1
            else:
                print(f"  STRIP   {rel}")
                if not dry_run:
                    path.write_text(test_content)
                stripped += 1

    # Remove directories that are now empty (bottom-up)
    if not dry_run:
        for dirpath in sorted(project_dir.rglob("*"), reverse=True):
            if dirpath.is_dir():
                try:
                    dirpath.rmdir()  # only succeeds if empty
                except OSError:
                    pass

    # Ensure every crate still has a valid entry-point file after stripping.
    stubs = _ensure_rust_crate_stubs(project_dir, dry_run)

    print(f"\nRust: {deleted} deleted, {stripped} stripped to test blocks, "
          f"{kept} kept unchanged, {stubs} entry-point stubs created.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Strip a project to test code only.")
    parser.add_argument("project_dir", help="Path to the project directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without making changes")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.exists():
        print(f"ERROR: {project_dir} does not exist.")
        sys.exit(1)

    lang = detect_language(project_dir)
    print(f"Detected language: {lang}")
    print(f"Project directory: {project_dir}")
    if args.dry_run:
        print("DRY RUN -- no files will be modified.\n")

    if lang == "rust":
        process_rust(project_dir, args.dry_run)
    else:
        print(f"ERROR: Language '{lang}' is not yet supported.")
        print("Supported: rust")
        sys.exit(1)


if __name__ == "__main__":
    main()

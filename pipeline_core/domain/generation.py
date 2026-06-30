"""Value objects for the TASK-2 source generators (P4).

`Task2Config` carries the prompt constants/budgets the generators read (resolved
from `SETTINGS`/pipeline.py globals at the composition root). `GenerationRequest`
is the parameter object for `Task2Generator.generate` — the 15 call arguments
bundled into one named, immutable record so adding a context input is a
one-field change instead of a 5-signature edit.

Pure domain: stdlib only, no I/O.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.rust_scan import gen_mod_body_start


@dataclass(frozen=True, slots=True)
class Task2Config:
    """Prompt scaffolding constants + char budgets for the generators."""
    file_format_instruction: str
    task2_model: str | None
    thinking_directive: str
    verbosity_guard: str
    genesis_cap: int
    strategy_cap: int
    single_call_char_limit: int
    model_label: str = ""


@dataclass(frozen=True, slots=True)
class Task1Config:
    """Prompt scaffolding constants + char budgets for the TASK-1 synthesizers
    (resolved from `SETTINGS`/pipeline.py globals at the composition root)."""
    model: str
    task1_model: str | None
    thinking_directive: str
    file_format_instruction: str
    single_call_char_limit: int
    synthesis_group_size: int
    readme_cap: int
    strategy_cap: int
    chunk_cache_dir: str
    parallelism: int = 1


@dataclass(frozen=True, slots=True)
class SkeletonConfig:
    """Runtime knobs for the 4-phase skeleton generator (resolved from
    `SETTINGS`/env at the composition root). `phase_c_per_fn`/`phase_c_tdd`
    are the two mode flags; the remaining ints are loop budgets."""
    task2_model: str | None
    parallelism: int
    phase_c_per_fn: bool
    phase_c_tdd: bool
    phase_c_max_passes: int = 3
    validate_iters: int = 3
    post_merge_max_stalls: int = 3
    post_merge_hard_cap: int = 10_000
    post_merge_test_timeout: int = 180
    recently_filled_max: int = 50


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """One TASK-2 generation request — the inputs every generator's `generate`
    consumes. `existing_test_files` is the cluster's read-only test spec (a list
    or set of relative paths); the remaining strings are optional prompt
    context, empty when not applicable."""
    project_dir: Path
    genesis_path: Path
    functional_req_path: Path
    manifest_path: Path
    existing_test_files: Any  # list[str] | set[str] — caller's collection, passed through
    project_profile: str = ""
    workspace_structure: str = ""
    build_error: str = ""
    locked_dep_context: str = ""
    current_module: str = ""
    module_dir_rel: str = ""
    whole_project_context: str = ""
    coverage_strategy: str = ""
    api_surface: str = ""
    prev_source_context: str = ""


def strip_test_prefix(stem: str) -> str:
    """Drop a leading `test_`/`tests_`/`it_` prefix from a test file stem
    (regex-free, R7). Equivalent to `re.sub(r'^(test_|tests_|it_)', '', stem)` —
    `test_` cannot match a `tests_` stem (5th char differs), so first-match
    order is preserved."""
    for p in ("test_", "tests_", "it_"):
        if stem.startswith(p):
            return stem[len(p):]
    return stem


def tests_output_number(name: str) -> int | None:
    """The N of a ``tests_output_<N>.txt`` filename, else None — byte-equivalent to
    ``re.search(r'tests_output_(\\d+)\\.txt', name)`` projected onto ``int(group(1))``."""
    pre = "tests_output_"
    i = name.find(pre)
    while i >= 0:
        d = i + len(pre)
        e = d
        while e < len(name) and name[e].isdigit():
            e += 1
        if e > d and name[e:e + 4] == ".txt":
            return int(name[d:e])
        i = name.find(pre, i + 1)
    return None


def is_gen_tests_sibling_name(name: str) -> bool:
    """True iff `name` ends with a `.gen_tests_<digits>.rs` suffix — byte-equivalent
    to ``bool(re.search(r'\\.gen_tests_\\d+\\.rs$', name))`` (looser than
    `parse_gen_tests_filename`: no stem requirement, no `.broken`)."""
    if not name.endswith(".rs"):
        return False
    core = name[:-3]
    idx = core.rfind(".gen_tests_")
    if idx == -1:
        return False
    digits = core[idx + len(".gen_tests_"):]
    return bool(digits) and digits.isdigit()


def gen_tests_sibling_number(name: str, stem: str) -> int | None:
    """The N of `<stem>.gen_tests_<N>.rs` for this exact `stem`, else None —
    byte-equivalent to ``re.match(rf'^{re.escape(stem)}\\.gen_tests_(\\d+)\\.rs$',
    name)`` projected onto ``int(group(1))``."""
    prefix = stem + ".gen_tests_"
    if not name.startswith(prefix) or not name.endswith(".rs"):
        return None
    digits = name[len(prefix):-3]
    return int(digits) if digits and digits.isdigit() else None


def has_gen_tests_mod_decl(src_text: str, pass_n: int) -> bool:
    """True iff `src_text` declares ``mod gen_tests_<pass_n>;`` — byte-equivalent to
    ``bool(re.search(rf'\\bmod\\s+gen_tests_{pass_n}\\s*;', src_text))`` (the trailing
    ``\\s*;`` enforces an exact N, so `gen_tests_3` never matches `gen_tests_30;`)."""
    target = "gen_tests_" + str(pass_n)
    n = len(src_text)
    i = 0
    while (k := src_text.find("mod", i)) >= 0:
        i = k + 3
        if k > 0 and (src_text[k - 1].isalnum() or src_text[k - 1] == "_"):
            continue  # \bmod
        p = k + 3
        if p >= n or not src_text[p].isspace():  # \s+
            continue
        while p < n and src_text[p].isspace():
            p += 1
        if not src_text.startswith(target, p):
            continue
        q = p + len(target)
        while q < n and src_text[q].isspace():  # \s*
            q += 1
        if q < n and src_text[q] == ";":
            return True
    return False


def strip_mod_test_prefix(stem: str) -> str:
    """Drop a leading `mod_`/`test_`/`tests_` prefix — byte-equivalent to
    ``re.sub(r'^(mod|tests?)_', '', stem)`` (``tests?`` is greedy, so `tests_` is
    tried before `test_`)."""
    for p in ("mod_", "tests_", "test_"):
        if stem.startswith(p):
            return stem[len(p):]
    return stem


def parse_gen_tests_filename(name: str) -> tuple[str, int] | None:
    """Parse a `<stem>.gen_tests_<N>.rs` sibling filename → (stem, N), or None.
    Accepts an optional trailing `.broken` quarantine suffix (regex-free, R7).
    Equivalent to `^(.+?)\\.gen_tests_(\\d+)\\.rs(?:\\.broken)?$` — `partition`
    takes the first `.gen_tests_` to mirror the non-greedy stem capture."""
    rest = name[: -len(".broken")] if name.endswith(".broken") else name
    if not rest.endswith(".rs"):
        return None
    stem, sep, n_str = rest[:-3].partition(".gen_tests_")
    if not sep or not stem or not n_str.isdigit():
        return None
    return stem, int(n_str)


def coalesce_gen_tests_siblings(
    files: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Merge `<stem>.gen_tests_<N>.rs` augmentation siblings of the same module
    into ONE logical test unit for requirements synthesis (TASK 1).

    task0c split-and-merge writes one physical sibling per uncovered-line subset
    per attempt per cluster, so a single module's tests fragment across dozens of
    tiny files. Requirements are per-module, so feeding each fragment to its own
    LLM call is pure fan-out. This collapses every group sharing a parent dir +
    stem into one `(rel, content)` entry, emitted at the position of the group's
    first member; fragments are ordered by N and prefixed with a `// gen_tests_<N>`
    provenance marker. Non-sibling entries (real test files, base sources) pass
    through unchanged and in order. Pure: no I/O, no regex, no disk mutation —
    the physical siblings are untouched (attribution still resolves them).
    """
    groups: dict[tuple[str, str], list[tuple[int, str]]] = {}
    order: list[tuple[str, str]] = []      # group key in first-appearance order
    out: list[tuple[str, str]] = []        # passthrough entries, by key/sentinel
    out_keys: list[tuple[str, str] | None] = []

    for rel, content in files:
        parsed = parse_gen_tests_filename(Path(rel).name)
        if parsed is None:
            out.append((rel, content))
            out_keys.append(None)
            continue
        stem, n = parsed
        key = (str(Path(rel).parent), stem)
        if key not in groups:
            groups[key] = []
            order.append(key)
            out.append(("", ""))          # placeholder; filled after grouping
            out_keys.append(key)
        groups[key].append((n, content))

    merged: dict[tuple[str, str], tuple[str, str]] = {}
    for key in order:
        parent, stem = key
        fragments = sorted(groups[key], key=lambda item: item[0])
        body = "\n\n".join(f"// gen_tests_{n}\n{c}" for n, c in fragments)
        rel = f"{parent}/{stem}.rs (augmented tests)" if parent not in ("", ".") \
            else f"{stem}.rs (augmented tests)"
        merged[key] = (rel, body)

    result: list[tuple[str, str]] = []
    for i, out_key in enumerate(out_keys):
        result.append(merged[out_key] if out_key is not None else out[i])
    return result


def merge_gen_tests_blocks(blocks: list[str]) -> str | None:
    """Combine the subset gen_tests blocks of ONE split-and-merge attempt into a
    single block, so the attempt writes ONE `gen_tests_<N>` sibling instead of
    one-per-subset (the fan-out that made TASK 1 grind ~140 files).

    Each subset's body is unwrapped from its `mod gen_X { … }` shell, then the
    bodies are concatenated flat under one fresh `mod gen_tests_merged { … }`
    shell (the downstream `_finalize_and_commit` re-injects allow/prelude and
    strips this shell exactly as for a single LLM block). Exact-duplicate `use`
    lines collapse to one (the per-subset `use super::*;` + identical prelude
    imports).

    Returns None when any declared top-level item name occurs in more than one
    subset — flattening into one module would then be an E0428/E0252 clash, so
    the caller keeps the blocks as separate siblings (rare; correctness over
    fewer files). Deterministic; never emits a block with a NEW name clash.
    """
    from .rust_scan import item_names, mod_body

    if not blocks:
        return None
    bodies = [(mod_body(b) or b.strip() + "\n") for b in blocks]

    seen_names: set[str] = set()
    for body in bodies:
        names = set(item_names(body))
        if names & seen_names:
            return None                  # cross-subset collision → don't flatten
        seen_names |= names

    out_lines: list[str] = []
    seen_use: set[str] = set()
    for body in bodies:
        for ln in body.splitlines():
            stripped = ln.strip()
            if stripped.startswith("use ") and stripped.endswith(";"):
                if stripped in seen_use:
                    continue
                seen_use.add(stripped)
            out_lines.append(ln)
    merged = "\n".join(out_lines).strip()
    return f"mod gen_tests_merged {{\n{merged}\n}}\n"


# --- gen_tests path-mod declaration toolkit (R7: structured, replaces the four
# `decl_re` regexes that did string surgery on the pipeline's OWN fixed format) ---
#
# The decl builder emits, for each augmentation sibling:
#
#     [#[cfg(<gate>)]]      ← optional cluster gate (nightly etc.)
#     #[cfg(test)]
#     [#[allow(...)] ...]   ← zero or more further attributes
#     #[path = "<stem>.gen_tests_<N>.rs"]
#     [pub ]mod gen_tests_<N>;
#
# Everything below is a pure structural line-scan anchored on the `mod` line.


@dataclass(frozen=True, slots=True)
class GenTestsDecl:
    """One `[pub ]mod gen_tests_<N>;` path-mod declaration block. `start`/`end` are
    the inclusive line-index span of the whole block — its contiguous leading
    `#[...]` attribute run through the `mod` line."""
    pass_n: int
    sibling_name: str | None   # value of `#[path = "..."]`, or None when absent
    gate: str                  # leading non-test `#[cfg(<gate>)]` predicate, or ""
    start: int
    end: int


def _mod_gen_tests_pass(stripped_line: str) -> int | None:
    """Pass N from a `[pub ]mod gen_tests_<N>;` line (already `.strip()`ed), else None."""
    body = stripped_line.removeprefix("pub ")
    if not (body.startswith("mod gen_tests_") and body.endswith(";")):
        return None
    n_str = body[len("mod gen_tests_"):-1].strip()
    return int(n_str) if n_str.isdigit() else None


def _attr_path_value(stripped_attr: str) -> str | None:
    """The quoted value of a `#[path = "..."]` attribute line, or None."""
    open_q = stripped_attr.find('"')
    if open_q < 0:
        return None
    close_q = stripped_attr.find('"', open_q + 1)
    return stripped_attr[open_q + 1:close_q] if close_q > open_q else None


def iter_gen_tests_decls(text: str) -> Iterator[GenTestsDecl]:
    """Yield every gen_tests path-mod decl block in `text`, scanning each
    `mod gen_tests_<N>;` anchor and walking back over its contiguous `#[...]`
    attribute run. Pure structural scan of the pipeline's own fixed format (R7)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        pass_n = _mod_gen_tests_pass(line.strip())
        if pass_n is None:
            continue
        sibling_name: str | None = None
        gate = ""
        start = i
        j = i - 1
        while j >= 0 and lines[j].lstrip().startswith("#["):
            attr = lines[j].strip()
            if attr.startswith("#[path"):
                sibling_name = _attr_path_value(attr)
            elif attr.startswith("#[cfg(") and attr != "#[cfg(test)]":
                inner = attr[len("#[cfg("):].rsplit(")]", 1)[0]
                if inner != "test":
                    gate = inner
            start = j
            j -= 1
        yield GenTestsDecl(pass_n, sibling_name, gate, start, i)


def _is_gen_tests_sibling(name: str | None) -> bool:
    return name is not None and parse_gen_tests_filename(name) is not None


def _strip_decls(text: str, keep: Callable[[GenTestsDecl], bool]) -> str:
    """Drop every decl block for which `keep` is False; preserve the rest verbatim."""
    spans = [(d.start, d.end) for d in iter_gen_tests_decls(text) if not keep(d)]
    if not spans:
        return text
    lines = text.splitlines()
    for start, end in sorted(spans, reverse=True):  # bottom-up: indices stay valid
        del lines[start:end + 1]
    out = "\n".join(lines)
    return out + "\n" if text.endswith("\n") else out


def strip_gen_tests_decl(parent_text: str, sibling_name: str, pass_n: int) -> str:
    """Remove the one decl block whose `#[path]` is `sibling_name` and pass is
    `pass_n`; leave all others untouched. (#9: replaces the reattach-quarantine regex.)"""
    return _strip_decls(
        parent_text,
        keep=lambda d: not (d.pass_n == pass_n and d.sibling_name == sibling_name))


def strip_gen_tests_decl_n(parent_text: str, pass_n: int) -> str:
    """Remove the decl block for pass `pass_n` (any sibling)."""
    return _strip_decls(parent_text, keep=lambda d: d.pass_n != pass_n)


def strip_all_gen_tests_decls(text: str) -> str:
    """Remove every gen_tests path-mod decl block (path is a `*.gen_tests_<N>.rs`)."""
    return _strip_decls(text, keep=lambda d: not _is_gen_tests_sibling(d.sibling_name))


def strip_trailing_legacy_gen_block(text: str) -> str:
    """Remove a trailing legacy inline ``#[cfg(test)] mod gen_*`` block (the
    pre-sibling-file design) — byte-equivalent to ``re.sub(r'\\n+(?:// '
    r'GRCOV_EXCL_START\\n)?#\\[cfg\\(test\\)\\](?:\\n#\\[[^\\]]*\\])*\\n(?:pub )?mod '
    r'gen_\\w+\\s*\\{.*', '', text, flags=re.DOTALL)``: from the first qualifying
    ``\\n+`` run through end-of-text. Returns `text` unchanged when absent."""
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "\n":
            i += 1
            continue
        run_start = i
        while i < n and text[i] == "\n":               # `\n+` (greedy; can't backtrack here)
            i += 1
        p = i
        if text.startswith("// GRCOV_EXCL_START\n", p):  # optional marker line
            p += len("// GRCOV_EXCL_START\n")
        if not text.startswith("#[cfg(test)]", p):
            continue
        p += len("#[cfg(test)]")
        while text.startswith("\n#[", p):               # `(?:\n#\[[^\]]*\])*`
            cb = text.find("]", p + 3)
            if cb == -1:
                break
            p = cb + 1
        if p >= n or text[p] != "\n":                   # `\n`
            continue
        p += 1
        if text.startswith("pub ", p):                  # `(?:pub )?`
            p += 4
        if not text.startswith("mod gen_", p):
            continue
        p += len("mod gen_")
        ws = p
        while p < n and (text[p].isalnum() or text[p] == "_"):  # `\w+`
            p += 1
        if p == ws:
            continue
        while p < n and text[p].isspace():              # `\s*`
            p += 1
        if p < n and text[p] == "{":                    # `\{` then `.*` (DOTALL → EOF)
            return text[:run_start]
    return text


def gen_tests_decl_blocks(text: str) -> list[GenTestsDecl]:
    """Every gen_tests-sibling decl block, in source order — for cluster gating
    (each block's `gate` predicate + `sibling_name`)."""
    return [d for d in iter_gen_tests_decls(text) if _is_gen_tests_sibling(d.sibling_name)]


def testfile_paths(text: str) -> list[str]:
    """Every `path` in `<TESTFILE path="...">` markers within `text`, in order
    (regex-free, R7). Equivalent to `re.findall(r'<TESTFILE path="([^"]+)"', text)`
    — empty paths are skipped (the `+` quantifier required ≥1 char)."""
    out: list[str] = []
    marker = '<TESTFILE path="'
    i = 0
    while True:
        j = text.find(marker, i)
        if j < 0:
            break
        start = j + len(marker)
        k = text.find('"', start)
        if k < 0:
            break
        if k > start:  # non-empty path only (matches the regex's `+`)
            out.append(text[start:k])
        i = k + 1
    return out


def topo_sort_files(file_paths: list[str], known_content: dict[str, str]) -> list[str]:
    """FR-010: topological order of `file_paths` by `mod` declarations parsed
    from `known_content` — a file that declares `mod foo;` must be generated
    before `foo.rs`. Pure: uses stdlib `graphlib` + the domain mod-decl scan."""
    from graphlib import TopologicalSorter

    from .rust_scan import mod_decls
    path_set = set(file_paths)
    stems = {Path(p).stem: p for p in file_paths}
    deps: dict[str, set[str]] = {p: set() for p in file_paths}
    for path, content in known_content.items():
        for mod_name in mod_decls(content):
            sub = stems.get(mod_name)
            if sub and sub != path and sub in path_set:
                deps[sub].add(path)  # sub must come after path (path declares it)
    ts: TopologicalSorter[str] = TopologicalSorter(deps)
    return list(ts.static_order())


def prelude_import_names(prelude_block: str) -> str:
    """Comma-separated short names imported by the `use …;` lines of a gen_tests
    prelude block (regex-free, R7). Expands `use a::b::{X, Y as Z}` groups, takes
    the last path segment (or the rename target) of each, dedups preserving order.

    Lets task0c prompts reflect whatever the project's prelude actually provides
    instead of hardcoding a fixed set."""
    def _short(seg: str) -> str:
        seg = seg.strip()
        if " as " in seg:
            seg = seg.split(" as ", 1)[1].strip()
        return seg

    names: list[str] = []
    for raw_line in prelude_block.splitlines():
        line = raw_line.strip().rstrip(";")
        if not line.startswith("use "):
            continue
        path = line[len("use "):].strip()
        # Grouped import `prefix::{A, B}` → expand each member to its short name.
        if path.endswith("}") and "::{" in path:
            inner = path[path.rindex("::{") + 3:-1]
            names.extend(s for member in inner.split(",") if (s := _short(member)))
        else:
            last = _short(path.split("::")[-1])
            if last:
                names.append(last)

    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ", ".join(ordered)


def extract_gen_tests_body(block: str) -> str:
    """Strip the outer `mod gen_X { ... }` wrapper and return the dedented body.

    If the block shape doesn't match (bare test functions), returns block unchanged.
    """
    start = gen_mod_body_start(block)
    if start is None:
        return block.strip() + "\n"
    depth = 1
    i = start
    while i < len(block):
        c = block[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                body = block[start:i]
                lines = body.splitlines()
                non_empty = [ln for ln in lines if ln.strip()]
                if non_empty:
                    indent = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
                    if indent > 0:
                        body = "\n".join(
                            (ln[indent:] if len(ln) >= indent and ln[:indent].isspace() else ln)
                            for ln in lines
                        )
                return body.strip() + "\n"
        i += 1
    return block.strip() + "\n"


def build_gen_tests_decl(
    sibling_name: str, n: int | str, allow_line: str, cluster_cfg_attr: str = ""
) -> str:
    """Build the `#[cfg(test)] ... mod gen_tests_N;` declaration block.

    cluster_cfg_attr: optional cfg predicate gating this sibling to a specific cluster.
    """
    parts: list[str] = []
    if cluster_cfg_attr:
        parts.append(f"#[cfg({cluster_cfg_attr})]")
    parts.append("#[cfg(test)]")
    if allow_line:
        parts.append(allow_line)
    parts.append(f'#[path = "{sibling_name}"]')
    return "\n" + "\n".join(parts) + f"\nmod gen_tests_{n};\n"


def gen_tests_origin_cfgs(file_text: str) -> dict[int, str]:
    """Map each `mod gen_tests_<n>;` decl to its cluster cfg predicate.

    Returns {pass_n: cfg_predicate}; '' for the stable/default cluster.
    Deterministic structural scan of the pipeline's own fixed decl format.
    """
    lines = file_text.splitlines()
    out: dict[int, str] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if not (s.startswith("mod gen_tests_") and s.endswith(";")):
            continue
        try:
            n = int(s[len("mod gen_tests_"):-1])
        except ValueError:
            continue
        cfg = ""
        j = i - 1
        while j >= 0:
            a = lines[j].strip()
            if a.startswith("#["):
                if a.startswith("#[cfg(") and a.endswith(")]") and a != "#[cfg(test)]":
                    inner = a[len("#[cfg("):].rsplit(")]", 1)[0]
                    if inner != "test":
                        cfg = inner
                j -= 1
                continue
            if a == "":
                j -= 1
                continue
            break
        out[n] = cfg
    return out


def extract_test_mod(text: str) -> str:
    """Brace-counter extraction of a gen_tests mod block.

    Finds the first #[cfg(test)] in text, scans forward past any attribute
    lines (#[allow(...)], etc.) to the opening brace of mod gen_*, then walks
    character-by-character counting brace depth until the block closes.
    Returns everything from #[cfg(test)] to the closing }, or "" if not found.
    No regex — works regardless of what the model puts between cfg and mod.
    """
    cfg_pos = text.find('#[cfg(test)]')
    if cfg_pos == -1:
        return ""
    mod_pos = text.find('mod gen_', cfg_pos)
    if mod_pos == -1:
        mod_pos = text.find('mod gen_tests', cfg_pos)
    if mod_pos == -1:
        return ""
    brace_start = text.find('{', mod_pos)
    if brace_start == -1:
        return ""
    depth = 0
    for i, ch in enumerate(text[brace_start:], brace_start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[cfg_pos:i + 1].strip()
    return ""

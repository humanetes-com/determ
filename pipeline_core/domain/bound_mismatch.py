"""Frozen-bound mismatch detection.

A per-fn fix loop whose errors are E0277 "the trait bound `P: Trait` is not
satisfied" on a generic param `P` that the FROZEN signature/impl header already
constrains to a *different* trait cannot succeed: the body cannot add or change
bounds, and every in-scope call path requires the trait the frozen bound does
not grant. The root cause is a cross-module inconsistency (the module defining
the frozen bound's trait), not this fn — the loop must park the fn instead of
thrashing 5-6 attempts per cluster.

Pure text scanners (no regex, angle-bracket-aware token walks) over the frozen
signature text and the rustc error excerpt already carried by the retrospective.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FrozenBoundMismatch:
    """Evidence that no body can compile under the frozen signature: `param` is
    frozen to `frozen` bounds, but the compiler demands `required` for it."""
    param: str
    required: str
    frozen: tuple[str, ...]


def _split_top(s: str, sep: str) -> list[str]:
    """Split `s` on `sep` (a single char) at angle/paren/bracket depth 0.
    `->` return arrows do not close an angle bracket."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    prev = ""
    for ch in s:
        if ch in "<([":
            depth += 1
        elif ch in ")]" or (ch == ">" and prev != "-"):
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        prev = ch
    parts.append("".join(cur))
    return parts


def _find_top_level_word(s: str, word: str) -> int:
    """Offset of whitespace-delimited `word` at bracket depth 0, or -1."""
    depth = 0
    i = 0
    prev = ""
    n = len(s)
    while i < n:
        ch = s[i]
        if ch in "<([":
            depth += 1
        elif ch in ")]" or (ch == ">" and prev != "-"):
            depth -= 1
        if (depth == 0 and s.startswith(word, i)
                and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_"))
                and (i + len(word) >= n
                     or not (s[i + len(word)].isalnum() or s[i + len(word)] == "_"))):
            return i
        prev = ch
        i += 1
    return -1


def _split_first_bound_colon(entry: str) -> tuple[str, str] | None:
    """Split a `P: Bound + Bound` entry at the first single `:` (never inside
    a `::` path separator). Returns (left, right) or None."""
    i = 0
    n = len(entry)
    while i < n:
        if entry[i] == ":":
            if i + 1 < n and entry[i + 1] == ":":
                i += 2
                continue
            if i > 0 and entry[i - 1] == ":":
                i += 1
                continue
            return entry[:i], entry[i + 1:]
        i += 1
    return None


def _bound_paths(bounds_text: str) -> tuple[str, ...]:
    """The trait paths in a `+`-joined bound list; lifetimes and `?Sized`
    relaxations dropped, leading `dyn ` / HRTB `for<...>` prefixes stripped."""
    out: list[str] = []
    for b in _split_top(bounds_text, "+"):
        b = b.strip()
        if not b or b.startswith("'") or b.startswith("?"):
            continue
        if b.startswith("for<"):
            close = b.find(">")
            b = b[close + 1:].strip() if close != -1 else b
        if b.startswith("dyn "):
            b = b[4:].strip()
        if b:
            out.append(b)
    return tuple(out)


def _collect_where_bounds(text: str, acc: dict[str, list[str]]) -> None:
    w = _find_top_level_word(text, "where")
    if w == -1:
        return
    clause = text[w + len("where"):]
    for entry in _split_top(clause, ","):
        parts = _split_first_bound_colon(entry.strip())
        if parts is None:
            continue
        param, bounds = parts[0].strip(), parts[1]
        if param.isidentifier() and not param.startswith("'"):
            acc.setdefault(param, []).extend(_bound_paths(bounds))


def _collect_generic_list_bounds(text: str, acc: dict[str, list[str]]) -> None:
    """Inline bounds from the first `<...>` generics list (`impl<E: Trait>` /
    `fn f<E: Trait>`)."""
    start = text.find("<")
    if start == -1:
        return
    depth = 0
    prev = ""
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if ch in "<([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == ">" and prev != "-":
            depth -= 1
            if depth == 0:
                end = i
                break
        prev = ch
    if end == -1:
        return
    for entry in _split_top(text[start + 1:end], ","):
        entry = entry.strip()
        if not entry or entry.startswith("'") or entry.startswith("const "):
            continue
        parts = _split_first_bound_colon(entry)
        if parts is None:
            continue
        param, bounds = parts[0].strip(), parts[1]
        if param.isidentifier():
            acc.setdefault(param, []).extend(_bound_paths(bounds))


def frozen_bounds(sig_text: str, container: str) -> dict[str, tuple[str, ...]]:
    """Trait bounds the FROZEN fn signature + impl/trait header grant each
    generic type param — the bounds no body can change."""
    acc: dict[str, list[str]] = {}
    for text in (container or "", sig_text or ""):
        if not text:
            continue
        _collect_generic_list_bounds(text, acc)
        _collect_where_bounds(text, acc)
    return {p: tuple(bs) for p, bs in acc.items() if bs}


def unsatisfied_trait_bounds(error_excerpt: str) -> list[tuple[str, str]]:
    """`(param, required_trait_path)` pairs from rustc "the trait bound
    `P: Trait` is not satisfied" lines where `P` is a bare type param."""
    out: list[tuple[str, str]] = []
    marker = "the trait bound `"
    for line in (error_excerpt or "").splitlines():
        if "is not satisfied" not in line:
            continue
        p = line.find(marker)
        if p == -1:
            continue
        rest = line[p + len(marker):]
        tick = rest.find("`")
        if tick == -1:
            continue
        parts = _split_first_bound_colon(rest[:tick])
        if parts is None:
            continue
        param, required = parts[0].strip(), parts[1].strip()
        if param.isidentifier() and required and (param, required) not in out:
            out.append((param, required))
    return out


def path_leaf(path: str) -> str:
    """Last `::` segment of a trait path, generic args stripped."""
    cut = path.find("<")
    if cut != -1:
        path = path[:cut]
    return path.strip().rsplit("::", 1)[-1].strip()


def detect_frozen_bound_mismatch(
    sig_text: str, container: str, error_excerpt: str,
) -> FrozenBoundMismatch | None:
    """The structural tell: an E0277 on a generic param whose frozen
    bounds name a DIFFERENT trait (by leaf ident) than the one the compiler
    requires. Same-leaf requirements (path spelling differences) are not
    mismatches; unconstrained params keep the normal fix-loop behavior."""
    bounds = frozen_bounds(sig_text, container)
    if not bounds:
        return None
    for param, required in unsatisfied_trait_bounds(error_excerpt):
        frozen = bounds.get(param)
        if not frozen:
            continue
        req_leaf = path_leaf(required)
        if req_leaf and all(path_leaf(b) != req_leaf for b in frozen):
            return FrozenBoundMismatch(param=param, required=required, frozen=frozen)
    return None

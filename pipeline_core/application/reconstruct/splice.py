"""Byte-exact fn-body splice + syn-record locator — the regex-free core of
the per-fn fill engine's source mutation (P2 slice 3, relocated verbatim).

The regex-based siblings (`_locate_fn`/`_splice_fn_body` name-match fallback,
`_splice_fn_with_where`) deliberately STAY in pipeline.py: they are the
extractor-absent fallback / model-protocol parsers (R7.6, R8.5 classes).
`locate_fn_by_record` takes the fn enumerator as a plain injected callable so
this layer never imports the extractor adapter (DIP without ceremony).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body


def locate_fn_by_record(enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
                        file_path: Path, name: str, container: str,
                        sig_text: str) -> tuple[int, int, int, str] | None:
    """Re-enumerate `file_path` and return (sig_byte_start, body_byte_start,
    body_byte_end, fresh_sig_text) for the fn matching (name, container,
    sig_text). Used by the per-fn loop to splice by EXACT byte offset
    instead of regex-locating by name (which collides when a file has
    multiple fns sharing a name — e.g. Display::fmt vs Debug::fmt).

    Re-enumeration is required because byte offsets shift after any prior
    splice in the same file. ~30 ms via the syn binary; negligible vs the
    LLM call cost.

    Disambiguation order (each tier narrows the candidate set):
      1. fn name match
      2. container match (e.g. "impl Display for X")
      3. signature similarity (exact, else normalised whitespace)
    """
    src_dir = file_path.parent
    # The extractor walks a directory tree; we want one file. Easiest path:
    # enumerate the parent directory and filter to records from this file.
    # For large crates that's wasteful; for our scale (12 files) it's fine.
    recs = enumerate_fns(src_dir.parent if src_dir.name == "src" else src_dir)
    if recs is None:
        return None
    file_rel_candidates = {
        str(file_path.name),
        str(file_path).split("/src/", 1)[-1] if "/src/" in str(file_path) else str(file_path.name),
        f"src/{file_path.name}",
    }
    file_recs = [r for r in recs if r.get("file") in file_rel_candidates
                  or r.get("file", "").endswith("/" + file_path.name)
                  or r.get("file", "") == file_path.name]
    if not file_recs:
        return None
    cands = [r for r in file_recs if r.get("fn_name") == name]
    if not cands:
        return None
    if container:
        narrowed = [r for r in cands if r.get("container") == container]
        if narrowed:
            cands = narrowed
    if sig_text:
        exact = [r for r in cands if r.get("sig_text") == sig_text]
        if exact:
            cands = exact
        else:
            norm = " ".join(sig_text.split())
            close = [r for r in cands if " ".join(r.get("sig_text", "").split()) == norm]
            if close:
                cands = close
    if len(cands) != 1:
        # Ambiguous still — pick the first stub-bodied one (more likely to
        # be the one we want to fill).
        stubs = [r for r in cands if r.get("has_unimplemented_body")]
        cands = [stubs[0]] if stubs else [cands[0]]
    r = cands[0]
    return (
        int(r["sig_byte_start"]),
        int(r["body_byte_start"]),
        int(r["body_byte_end"]),
        r.get("sig_text", ""),
    )


def splice_fn_body_at(file_path: Path, body_byte_start: int, body_byte_end: int,
                        new_body: str, allow_nonstub: bool = False) -> tuple[bool, int]:
    """Splice `new_body` between bytes [body_byte_start, body_byte_end) in
    `file_path`. Replaces an explicit byte range — used when the caller has
    canonical offsets from the syn enumerator. By default refuses to
    overwrite a body that isn't currently a stub.

    The body content between [start, end) is whatever currently sits between
    the fn's `{` and `}`. If that text doesn't look like an unimplemented
    stub, the splice is rejected so we never clobber a real body — UNLESS
    `allow_nonstub=True` (the fix-loop per_fn strategy, which re-fills a fn
    whose previous body was filled but wrong). When re-filling a non-stub,
    the caller MUST re-enumerate offsets before each splice, since every
    write shifts the byte ranges of later fns in the same file.

    Returns (success, bytes_delta).
    """
    # The offsets are BYTE offsets from the syn enumerator. They MUST be applied
    # in byte space: a `str` from read_text() is indexed by CHARACTERS, so any
    # multibyte UTF-8 char before the span (e.g. an em-dash `—` = 3 bytes/1 char
    # in a doc comment) shifts char-index past byte-offset and the slice eats the
    # closing `}` / next item. Work on raw bytes; decode only the small spans.
    try:
        raw = file_path.read_bytes()
    except Exception:
        return False, 0
    if body_byte_end > len(raw) or body_byte_start > body_byte_end:
        return False, 0
    cur_body = raw[body_byte_start:body_byte_end].decode("utf-8", errors="replace")
    if not allow_nonstub and not is_unimplemented_body(cur_body):
        return False, 0
    nb = new_body.strip()
    if nb.startswith("{") and nb.endswith("}"):
        nb = nb[1:-1].strip()
    if not nb or is_unimplemented_body(nb):
        return False, 0
    # Re-indent the body to match the fn's existing indentation. Find the
    # column of the fn declaration by walking back to the previous newline.
    line_start = raw.rfind(b"\n", 0, body_byte_start) + 1
    base_indent = ""
    for ch in raw[line_start:body_byte_start].decode("utf-8", errors="replace"):
        if ch in (" ", "\t"):
            base_indent += ch
        else:
            break
    inner_indent = base_indent + "    "
    nb_indented = "\n" + "\n".join(
        (inner_indent + ln) if ln.strip() else "" for ln in nb.splitlines()
    ) + "\n" + base_indent
    new_raw = raw[:body_byte_start] + nb_indented.encode("utf-8") + raw[body_byte_end:]
    file_path.write_bytes(new_raw)
    return True, len(new_raw) - len(raw)

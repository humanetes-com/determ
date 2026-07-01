"""Byte-exact fn-body splice + syn-record locator — the regex-free core of
the per-fn fill engine's source mutation (P2 slice 3; the name-match splice
siblings folded in 2026-06-29 / source-splice cluster).

Two locate strategies: `locate_fn_by_record` uses the syn enumerator (canonical
byte offsets) and takes it as a plain injected callable, so this layer never
imports the extractor adapter (DIP without ceremony); the name-match siblings
(`replace_fn_signature`/`splice_fn_body`/`splice_fn_with_where`) use the pure
`domain.rust_fn_headers.locate_fn` char-space scan (regex-free since P7) as the
extractor-absent fallback. pipeline.py keeps thin late-binding shims over these
so the e2e harness's `_enumerate_fns_via_extractor` monkeypatch is resolved at
CALL time.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.reconstruction import SpliceOutcome
from pipeline_core.domain.rust_fn_headers import is_pub_fn, locate_fn
from pipeline_core.domain.rust_idents import declares_fn


def _norm_cfg_set(preds: Any) -> frozenset[str]:
    """Whitespace-INSENSITIVE set of an fn's cfg predicates for cfg-variant
    matching: the extractor renders syn spacing (`not (any (extra_cap , ...))`)
    while a caller may pass the compact form (`not(any(extra_cap,...))`). cfg
    tokens carry no significant internal whitespace, so strip it all."""
    return frozenset("".join(str(p).split()) for p in (preds or ()))


def locate_fn_by_record(enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
                        file_path: Path, name: str, container: str,
                        sig_text: str,
                        cfg_predicates: tuple[str, ...] | None = None,
                        ) -> tuple[int, int, int, str] | None:
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
      4. cfg predicates (debt #17a) — two same-name same-sig free fns that
         differ ONLY by `#[cfg]` (the `cap_limit` toggle pair) are otherwise
         indistinguishable; when the caller knows the target's cfg, prefer the
         record whose cfg matches so the right cfg-variant is spliced.
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
    if cfg_predicates is not None and len(cands) > 1:
        want = _norm_cfg_set(cfg_predicates)
        cfg_match = [r for r in cands if _norm_cfg_set(r.get("cfg_predicates")) == want]
        if cfg_match:
            cands = cfg_match
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


def replace_fn_signature(file_path: Path, fn_name: str, new_sig: str) -> bool:
    """P3.1: replace the verbatim signature line(s) of ``fn_name`` with
    ``new_sig``, leaving the body intact. Only for PRIVATE fns (changing a
    ``pub`` sig would break the test surface) and only when ``new_sig`` still
    declares ``fn_name`` (the model can't rename). Returns True iff applied."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False
    if is_pub_fn(text, fn_name):
        return False
    if not declares_fn(new_sig, fn_name):
        return False
    loc = locate_fn(text, fn_name)
    if loc is None:
        return False
    _old_sig, sig_start, body_start, _body_end = loc
    line_start = text.rfind("\n", 0, sig_start) + 1
    cleaned = new_sig.strip().rstrip("{").rstrip()
    new_text = text[:line_start] + cleaned + " {" + text[body_start:]
    file_path.write_text(new_text, encoding="utf-8")
    return True


def splice_fn_body(file_path: Path, fn_name: str, new_body: str,
                   allow_nonstub: bool = False) -> tuple[bool, int]:
    """Replace the body of ``fn_name`` in ``file_path`` with ``new_body`` via a
    name-match (char-space) locate. By default only splices an
    ``unimplemented!()`` stub; ``allow_nonstub=True`` re-fills a wrong body. The
    file is re-read every call so byte offsets stay fresh. Returns
    ``(success, bytes_delta)``."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False, 0
    loc = locate_fn(text, fn_name)
    if loc is None:
        return False, 0
    _sig, _sig_start, body_start, body_end = loc
    cur_body = text[body_start:body_end]
    if not allow_nonstub and not is_unimplemented_body(cur_body):
        return False, 0
    nb = new_body.strip()
    if nb.startswith("{") and nb.endswith("}"):
        nb = nb[1:-1].strip()
    if not nb or is_unimplemented_body(nb):
        return False, 0
    line_start = text.rfind("\n", 0, _sig_start) + 1
    sig_line = text[line_start:_sig_start]
    base_indent = ""
    for ch in sig_line:
        if ch in (" ", "\t"):
            base_indent += ch
        else:
            break
    inner_indent = base_indent + "    "
    nb_indented = "\n" + "\n".join(
        (inner_indent + ln) if ln.strip() else "" for ln in nb.splitlines()
    ) + "\n" + base_indent
    new_text = text[:body_start] + nb_indented + text[body_end:]
    file_path.write_text(new_text, encoding="utf-8")
    return True, len(new_text) - len(text)


def splice_fn_with_where(
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
    file_path: Path,
    container: str,
    fn_name: str,
    body: str,
    where_clause: str | None = None,
    allow_nonstub: bool = False,
) -> SpliceOutcome:
    """Splice ``body`` into ``fn_name`` whose containing impl matches
    ``container`` (located via the syn enumerator). When ``where_clause`` is
    given, insert it into the signature just before the body's opening brace
    (byte space — offsets come from the enumerator). Returns a
    :class:`SpliceOutcome` ``(ok, reason, delta)``; ``reason`` is ``"ok"`` or a
    failure tag."""
    if not file_path.exists():
        return SpliceOutcome(False, "file_missing", 0)
    loc = locate_fn_by_record(enumerate_fns, file_path, fn_name, container, "")
    if loc is None:
        loose_container = container.split("{", 1)[0].strip()
        loc = locate_fn_by_record(enumerate_fns, file_path, fn_name, loose_container, "")
    if loc is None:
        return SpliceOutcome(False, "fn_not_found", 0)
    sig_byte_start, body_byte_start, body_byte_end, _sig_text = loc

    if where_clause:
        raw = file_path.read_bytes()
        brace_pos = raw.rfind(b"{", sig_byte_start, body_byte_start + 1)
        if brace_pos != -1:
            sig_slice = raw[sig_byte_start:brace_pos].decode("utf-8", errors="replace")
            if "where" not in sig_slice:
                insert = f"\n    {where_clause}\n".encode()
                raw = raw[:brace_pos] + insert + raw[brace_pos:]
                shift = len(insert)
                body_byte_start += shift
                body_byte_end += shift
                file_path.write_bytes(raw)

    ok, delta = splice_fn_body_at(file_path, body_byte_start, body_byte_end, body,
                                  allow_nonstub=allow_nonstub)
    if not ok:
        return SpliceOutcome(False, "splice_rejected", 0)
    return SpliceOutcome(True, "ok", delta)

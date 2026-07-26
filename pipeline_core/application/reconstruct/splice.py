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

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.application.reconstruct.splice_txn import (
    ParseValidator,
    parse_guarded_write,
)
from pipeline_core.domain.llm_output import xml_unescaped_variant
from pipeline_core.domain.reconstruction import SpliceOutcome, SpliceReason
from pipeline_core.domain.rust_fn_headers import is_pub_fn, locate_all_fns
from pipeline_core.domain.rust_idents import canon_container, declares_fn
from pipeline_core.domain.rust_scan import (
    has_code_brace,
    redeclared_inner_body,
    strip_outer_attributes,
    top_level_item_names,
)

# A wrapper can survive one unwrap (the model restated the item twice). Bounded
# so a pathological reply cannot spin here.
_MAX_UNWRAP_ROUNDS = 3


def _unwrap_redeclaration(body: str, fn_name: str) -> str | None:
    """`body` with any self-redeclaring wrapper(s) peeled off, or `None` when
    what is left is nothing worth splicing.

    A reply that restates the WHOLE item instead of just the body is a protocol
    mistake, not a content mistake — the code inside is the answer. Peeling the
    wrapper turns a refused attempt into a landed fill; refusing it outright
    would burn the attempt and leave the fn a stub."""
    for _ in range(_MAX_UNWRAP_ROUNDS):
        inner = redeclared_inner_body(body, fn_name)
        if inner is None:
            return body
        body = inner
    return None if redeclared_inner_body(body, fn_name) is not None else body


def _commit_with_xml_fallback(
    commit: Callable[[str, SpliceReason], SpliceOutcome], body: str,
) -> SpliceOutcome:
    """Commit `body`, retrying once with XML entities resolved if the verbatim
    write leaves the file unparseable.

    A model that emits XML-safe attribute values emits them in the body of the
    same reply, and `match &amp;mut self.state` is not Rust — syn rejects the
    file and a CORRECT body is reverted and lost. The retry is gated on the
    parse failure rather than applied up front so that a body legitimately
    containing `"&amp;"` (a string literal in HTML-handling code) is kept
    verbatim: it parses, so the fallback never runs.
    """
    out = commit(body, "ok")
    if out.ok or out.reason != "parse_broken":
        return out
    variant = xml_unescaped_variant(body)
    if variant is None:
        return out
    retry = commit(variant, "ok_xml_unescaped")
    return retry if retry.ok else out

# Process-global path-keyed splice+cargo locks. One lock per resolved source
# tree — regression-gate baselines are captured fresh INSIDE this lock, so it
# must be a genuine per-tree singleton across every engine in the process.
_splice_locks: dict[str, threading.Lock] = {}
_splice_locks_guard = threading.Lock()


def splice_lock_for(tree: Path) -> threading.Lock:
    """The splice+cargo lock owned by `tree` (one per resolved source tree)."""
    key = str(Path(tree).resolve())
    with _splice_locks_guard:
        return _splice_locks.setdefault(key, threading.Lock())


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
        # Canonical-identity match, NOT exact-string. The syn extractor renders
        # type syntax with its own spacing (`impl Bool for & bool`, `impl
        # Context < T , E > for ...`) which need not match a caller's compact
        # string. An exact compare silently fails to narrow and the ambiguity
        # fallback then picks the FIRST same-name stub — landing a body in the
        # wrong impl (the `not`/`from`/`fmt` collision). `canon_container`
        # (`<Trait>for<SelfTy>`, whitespace/impl-generics/where-clause dropped)
        # is the same identity the per-impl router and retro store key on.
        want_c = canon_container(container)
        narrowed = [r for r in cands
                    if canon_container(str(r.get("container", ""))) == want_c]
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
                        new_body: str, allow_nonstub: bool = False,
                        allow_stub_body: bool = False,
                        validate_parse: ParseValidator | None = None,
                        fn_name: str = "") -> SpliceOutcome:
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

    Returns a :class:`SpliceOutcome`; every refusal carries its distinct
    :data:`SpliceReason` (debt #39).
    """
    # The offsets are BYTE offsets from the syn enumerator. They MUST be applied
    # in byte space: a `str` from read_text() is indexed by CHARACTERS, so any
    # multibyte UTF-8 char before the span (e.g. an em-dash `—` = 3 bytes/1 char
    # in a doc comment) shifts char-index past byte-offset and the slice eats the
    # closing `}` / next item. Work on raw bytes; decode only the small spans.
    try:
        raw = file_path.read_bytes()
    except Exception:
        return SpliceOutcome(False, "read_failed", 0)
    if body_byte_end > len(raw) or body_byte_start > body_byte_end:
        return SpliceOutcome(False, "bad_offsets", 0)
    cur_body = raw[body_byte_start:body_byte_end].decode("utf-8", errors="replace")
    if not allow_nonstub and not is_unimplemented_body(cur_body):
        return SpliceOutcome(False, "target_not_stub", 0)
    nb = new_body.strip()
    if nb.startswith("{") and nb.endswith("}"):
        nb = nb[1:-1].strip()
    # The stub refusal exists to stop the MODEL answering `unimplemented!()`.
    # A REVERT writes the pipeline's own pre-image back and may legitimately
    # be a stub — that is what undoing a fill means.
    if not nb or (not allow_stub_body and is_unimplemented_body(nb)):
        return SpliceOutcome(False, "body_is_stub", 0)
    _unwrapped = _unwrap_redeclaration(nb, fn_name)
    if _unwrapped is None or not _unwrapped.strip():
        return SpliceOutcome(False, "body_redeclares_fn", 0)
    nb = _unwrapped
    if not allow_stub_body and is_unimplemented_body(nb):
        return SpliceOutcome(False, "body_is_stub", 0)
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
    items_before = top_level_item_names(raw.decode("utf-8", errors="replace"))

    def _commit(candidate: str, reason: SpliceReason) -> SpliceOutcome:
        indented = "\n" + "\n".join(
            (inner_indent + ln) if ln.strip() else ""
            for ln in candidate.splitlines()
        ) + "\n" + base_indent
        new_raw = (raw[:body_byte_start] + indented.encode("utf-8")
                   + raw[body_byte_end:])
        # A body write lives strictly between one fn's braces. If the file's
        # top-level items moved, the candidate closed the fn early and declared
        # its own — it parses, then collides with the real definition (E0255)
        # and poisons every sibling fill in the file until someone reverts it.
        if top_level_item_names(
                new_raw.decode("utf-8", errors="replace")) != items_before:
            return SpliceOutcome(False, "item_set_changed", 0)
        if not parse_guarded_write(file_path, new_raw, raw, validate_parse):
            return SpliceOutcome(False, "parse_broken", 0)
        return SpliceOutcome(True, reason, len(new_raw) - len(raw))

    return _commit_with_xml_fallback(_commit, nb)


def replace_fn_signature(file_path: Path, fn_name: str, new_sig: str,
                         validate_parse: ParseValidator | None = None) -> bool:
    """P3.1: replace the verbatim signature line(s) of ``fn_name`` with
    ``new_sig``, leaving the body intact. Only for PRIVATE fns (changing a
    ``pub`` sig would break the test surface) and only when ``new_sig`` still
    declares ``fn_name`` (the model can't rename). Returns True iff applied.

    `new_sig` is MODEL text and the write is parse-gated like a body splice:
    `declares_fn` confirms it declares the right fn but accepts an XML-escaped
    rendering (`fn f(x: &amp;str)` reads as a declaration of `f`), which would
    otherwise be written verbatim, leave the file unparseable, and — unlike
    every other model write here — persist, because a signature edit has no
    body offsets to roll back to.

    The write starts at the signature LINE, so the attribute block above it
    survives: a candidate that re-emits those attributes is reduced to its bare
    signature first, and one that carries a body (any `{`) is refused outright
    rather than written in front of the body already there. The signature's
    original indentation is preserved."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False
    if is_pub_fn(text, fn_name):
        return False
    # The attributes above the fn (`#[cfg]`, `#[cold]`, `#[must_use]`) stay in
    # the file and are not the model's to restate; keeping its copy would leave
    # BOTH. A `{` means the candidate carries a body — splicing that in front of
    # the existing one duplicates the item (E0255 on the second definition).
    sig_only = strip_outer_attributes(new_sig)
    if not declares_fn(sig_only, fn_name):
        return False
    if has_code_brace(sig_only.strip().rstrip("{").rstrip()):
        return False
    locs = locate_all_fns(text, fn_name)
    if len(locs) != 1:
        # 0 → absent; >1 → same-name collision this bare-name path cannot
        # safely disambiguate. Rewriting the first def's signature would
        # corrupt an unrelated same-named fn — refuse.
        return False
    _old_sig, sig_start, body_start, _body_end = locs[0]
    line_start = text.rfind("\n", 0, sig_start) + 1
    indent = text[line_start:sig_start]
    def _commit(candidate: str, _reason: SpliceReason) -> SpliceOutcome:
        cleaned = strip_outer_attributes(candidate).strip().rstrip("{").rstrip()
        if not cleaned or has_code_brace(cleaned):
            return SpliceOutcome(False, "sig_not_bare", 0)
        new_text = text[:line_start] + indent + cleaned + " {" + text[body_start:]
        if not parse_guarded_write(file_path, new_text.encode("utf-8"),
                                   text.encode("utf-8"), validate_parse):
            return SpliceOutcome(False, "parse_broken", 0)
        return SpliceOutcome(True, "ok", len(new_text) - len(text))

    return _commit_with_xml_fallback(_commit, new_sig).ok


def restore_frozen_signature(file_path: Path, fn_name: str,
                             skeleton_sig: str,
                             validate_parse: ParseValidator | None = None) -> bool:
    """Overwrite ``fn_name``'s CURRENT signature with the baked-skeleton one,
    leaving the body intact. The recovery path for frozen-signature corruption:
    a model-proposed where-clause spliced into a trait-impl method ratchets
    (splice_fn_with_where only inserts, and fix prompts re-read the corrupted
    sig as \"verbatim, do not change\"). The skeleton is the signature
    authority, so unlike :func:`replace_fn_signature` this is allowed on any
    fn — restoring the frozen sig cannot break the API surface. Returns True
    iff applied (False also when the sig already matches).

    The write starts at the signature LINE, so the attribute block above it
    survives: a skeleton sig that re-emits those attributes is reduced to its
    bare signature first, or the file ends up with BOTH — and a duplicated
    `#[cfg(...)]` silently changes which cluster the fn compiles in. A sig
    carrying a body (any `{`) is refused rather than written in front of the
    body already there. Like every other write here the result is parse-gated:
    this one has no body offsets to roll back to, so an unparseable result
    would PERSIST and kill the whole cluster's compile."""
    if not declares_fn(skeleton_sig, fn_name):
        return False
    sig_only = strip_outer_attributes(skeleton_sig)
    if not declares_fn(sig_only, fn_name):
        return False
    if has_code_brace(sig_only.strip().rstrip("{").rstrip()):
        return False
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return False
    locs = locate_all_fns(text, fn_name)
    if len(locs) != 1:
        # 0 → absent; >1 → the frozen sig belongs to ONE of several same-named
        # fns and a bare-name locate cannot tell which. Restoring the first's
        # signature could overwrite an unrelated same-named fn — refuse. The
        # container-aware caller path handles collision-heavy files.
        return False
    cur_sig, sig_start, body_start, _body_end = locs[0]
    cleaned = sig_only.strip().rstrip("{").rstrip()
    if " ".join(cur_sig.split()) == " ".join(cleaned.split()):
        return False
    line_start = text.rfind("\n", 0, sig_start) + 1
    indent = text[line_start:sig_start]
    new_text = text[:line_start] + indent + cleaned + " {" + text[body_start:]
    return parse_guarded_write(file_path, new_text.encode("utf-8"),
                               text.encode("utf-8"), validate_parse)


def splice_fn_body(file_path: Path, fn_name: str, new_body: str,
                   allow_nonstub: bool = False, allow_stub_body: bool = False,
                   validate_parse: ParseValidator | None = None) -> SpliceOutcome:
    """Replace the body of ``fn_name`` in ``file_path`` with ``new_body`` via a
    name-match (char-space) locate. By default only splices an
    ``unimplemented!()`` stub; ``allow_nonstub=True`` re-fills a wrong body. The
    file is re-read every call so byte offsets stay fresh. Returns a
    :class:`SpliceOutcome` with the distinct :data:`SpliceReason` (debt #39)."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return SpliceOutcome(False, "read_failed", 0)
    all_locs = locate_all_fns(text, fn_name)
    if not all_locs:
        return SpliceOutcome(False, "fn_not_found", 0)
    if len(all_locs) > 1:
        # The name is not unique in this file. A bare-name splice cannot know
        # which definition the caller means, and picking the first would land
        # a body meant for one impl into another (the `from`/`not`/`fmt`
        # collision class). Refuse — the caller must supply a container and
        # re-route through the syn-record locator.
        return SpliceOutcome(False, "ambiguous_name", 0)
    loc = all_locs[0]
    _sig, _sig_start, body_start, body_end = loc
    cur_body = text[body_start:body_end]
    if not allow_nonstub and not is_unimplemented_body(cur_body):
        return SpliceOutcome(False, "target_not_stub", 0)
    nb = new_body.strip()
    if nb.startswith("{") and nb.endswith("}"):
        nb = nb[1:-1].strip()
    if not nb or (not allow_stub_body and is_unimplemented_body(nb)):
        return SpliceOutcome(False, "body_is_stub", 0)
    _unwrapped = _unwrap_redeclaration(nb, fn_name)
    if _unwrapped is None or not _unwrapped.strip():
        return SpliceOutcome(False, "body_redeclares_fn", 0)
    nb = _unwrapped
    if not allow_stub_body and is_unimplemented_body(nb):
        return SpliceOutcome(False, "body_is_stub", 0)
    line_start = text.rfind("\n", 0, _sig_start) + 1
    sig_line = text[line_start:_sig_start]
    base_indent = ""
    for ch in sig_line:
        if ch in (" ", "\t"):
            base_indent += ch
        else:
            break
    inner_indent = base_indent + "    "
    items_before = top_level_item_names(text)

    def _commit(candidate: str, reason: SpliceReason) -> SpliceOutcome:
        indented = "\n" + "\n".join(
            (inner_indent + ln) if ln.strip() else ""
            for ln in candidate.splitlines()
        ) + "\n" + base_indent
        new_text = text[:body_start] + indented + text[body_end:]
        # Same invariant as the byte-offset splice: a body write lives strictly
        # between one fn's braces, so a changed top-level item list means the
        # candidate closed the fn early and declared items at file scope.
        if top_level_item_names(new_text) != items_before:
            return SpliceOutcome(False, "item_set_changed", 0)
        if not parse_guarded_write(file_path, new_text.encode("utf-8"),
                                   text.encode("utf-8"), validate_parse):
            return SpliceOutcome(False, "parse_broken", 0)
        return SpliceOutcome(True, reason, len(new_text) - len(text))

    return _commit_with_xml_fallback(_commit, nb)


def splice_fn_with_where(
    enumerate_fns: Callable[[Path], list[dict[str, Any]] | None],
    file_path: Path,
    container: str,
    fn_name: str,
    body: str,
    where_clause: str | None = None,
    allow_nonstub: bool = False,
    validate_parse: ParseValidator | None = None,
    sig_text: str = "",
) -> SpliceOutcome:
    """Splice ``body`` into ``fn_name`` whose containing impl matches
    ``container`` (located via the syn enumerator). ``sig_text`` is an OPTIONAL
    extra disambiguator: a FREE function has no impl container (the extractor
    renders it ``""``), so container narrowing can't pick it out from same-named
    impl-method siblings (`fn not(cond)` vs the two `Bool::not`); its verbatim
    signature can. Pass the stub record's ``sig_text`` to target a free fn.
    When ``where_clause`` is given, insert it into the signature just before the
    body's opening brace (byte space — offsets come from the enumerator).
    Returns a :class:`SpliceOutcome` ``(ok, reason, delta)``; ``reason`` is the
    distinct :data:`SpliceReason` cause. The where-clause insert + body splice
    are ONE transaction: a non-ok outcome restores the original bytes so a
    where-clause is never left dangling on an unspliced stub."""
    if not file_path.exists():
        return SpliceOutcome(False, "file_missing", 0)
    loc = locate_fn_by_record(enumerate_fns, file_path, fn_name, container, sig_text)
    if loc is None:
        loose_container = container.split("{", 1)[0].strip()
        loc = locate_fn_by_record(enumerate_fns, file_path, fn_name, loose_container, sig_text)
    if loc is None:
        return SpliceOutcome(False, "fn_not_found", 0)
    sig_byte_start, body_byte_start, body_byte_end, _sig_text = loc

    pre_image: bytes | None = None
    if where_clause:
        raw = file_path.read_bytes()
        brace_pos = raw.rfind(b"{", sig_byte_start, body_byte_start + 1)
        if brace_pos != -1:
            sig_slice = raw[sig_byte_start:brace_pos].decode("utf-8", errors="replace")
            if "where" not in sig_slice:
                insert = f"\n    {where_clause}\n".encode()
                new_raw = raw[:brace_pos] + insert + raw[brace_pos:]
                shift = len(insert)
                body_byte_start += shift
                body_byte_end += shift
                pre_image = raw
                file_path.write_bytes(new_raw)

    out = splice_fn_body_at(file_path, body_byte_start, body_byte_end, body,
                            allow_nonstub=allow_nonstub,
                            validate_parse=validate_parse, fn_name=fn_name)
    if not out.ok and pre_image is not None:
        file_path.write_bytes(pre_image)   # undo the dangling where-clause insert
    return out

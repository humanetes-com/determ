"""Put stored bodies back into a live source tree — the ledger's write half.

The exact mirror of `fn_revert.revert_fns_to_stub`, and deliberately so: that
function reverts EVERY occurrence of each `(file, fn_name)` because `fmt` lives
in each Debug/Display impl and `source` lives in two impls of `context.rs`, and
a re-seed that restored only the first occurrence would leave a tree half in
one state and half in another. So the unit here is the same tuple: a stored
entry whose occurrence count does not match the tree's is REFUSED rather than
partially applied — a shape mismatch means the file is not the file the body
was proven in, whatever the fingerprint said.

`body_text` is the extractor's own contract — "verbatim contents between `{`
and `}`, no braces" (`tools/skeleton_extractor/src/model.rs`) — which is the
same span `locate_all_fns` reports, so the substitution is direct.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_fn_headers import locate_all_fns

SpliceUnit = tuple[FnLocator, Sequence[str]]


def splice_bodies(units: Sequence[SpliceUnit], src_dir: Path,
                  ) -> tuple[list[FnLocator], list[tuple[FnLocator, str]]]:
    """Write each unit's stored bodies over its occurrences.

    Returns `(spliced, refused)`, where a refusal carries its reason. Rewrites
    last-to-first within a file so earlier byte offsets stay valid across
    several splices in one file — the same ordering rule the revert uses, for
    the same reason.
    """
    by_file: dict[str, list[SpliceUnit]] = {}
    for key, bodies in units:
        by_file.setdefault(key.file, []).append((key, bodies))

    spliced: list[FnLocator] = []
    refused: list[tuple[FnLocator, str]] = []
    for rel_file, entries in by_file.items():
        fp = Path(src_dir) / rel_file
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            refused.extend((k, "file unreadable") for k, _ in entries)
            continue
        # Collect every (span, replacement) first, then apply them right-to-left
        # in ONE pass: two units in the same file must not each re-locate on a
        # text the other has already rewritten.
        edits: list[tuple[int, int, str]] = []
        for key, bodies in entries:
            locs = locate_all_fns(text, key.fn_name)
            if not locs:
                refused.append((key, "fn not found"))
                continue
            if len(locs) != len(bodies):
                refused.append(
                    (key, f"occurrence count {len(locs)} != stored "
                          f"{len(bodies)}"))
                continue
            for (_sig, _sig_start, body_start, body_end), body in zip(
                    locs, bodies, strict=True):
                edits.append((body_start, body_end, body))
            spliced.append(key)
        for start, end, body in sorted(edits, reverse=True):
            text = text[:start] + body + text[end:]
        if edits:
            try:
                fp.write_text(text, encoding="utf-8")
            except OSError:
                for key, _ in entries:
                    if key in spliced:
                        spliced.remove(key)
                    refused.append((key, "file unwritable"))
    return spliced, refused


def current_signatures(key: FnLocator, src_dir: Path) -> tuple[str, ...]:
    """The unit's signature texts as they stand, in occurrence order.

    Empty when the fn is absent — which the gate reads as "no signature to
    compare", and a re-seed of a unit that is not in the tree is refused by the
    splice above anyway."""
    try:
        text = (Path(src_dir) / key.file).read_text(encoding="utf-8")
    except OSError:
        return ()
    return tuple(sig for sig, _s, _b, _e in locate_all_fns(text, key.fn_name))


def current_bodies(key: FnLocator, src_dir: Path) -> tuple[str, ...]:
    """The unit's body texts as they stand, in occurrence order."""
    try:
        text = (Path(src_dir) / key.file).read_text(encoding="utf-8")
    except OSError:
        return ()
    return tuple(text[b:e] for _sig, _s, b, e in locate_all_fns(text,
                                                                key.fn_name))

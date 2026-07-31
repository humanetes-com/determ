"""Disk-preserving fill merge (P6 relocation).

`merge_fills_preserving_existing` splices the model's fn BODIES into the on-disk
file without ever losing the disk's structure (imports/types/macros/attrs/prior
fills). Disk is the structural base; only `unimplemented!()` bodies are replaced.

Matching is by `FnSlot` — the enclosing item chain PLUS the signature — never by
the signature alone. A signature is not an identity in Rust: `fmt` occurs once
per `Debug`/`Display` impl, `source` once per `StdError` impl, `clone` once per
pointer type. Keyed on the signature, a dict silently keeps the LAST body and
hands it to every slot that shares the signature; that is how a cross-cluster
seed put `impl StdError for Error`'s body into `impl<E> StdError for E` and
opened a cluster on 39 rustc errors and an oracle of 0/124 (see
`domain/rust_fn_slots.py` for the full account).

Where a slot cannot be matched — a model emitting a thin file with its own
formatting for the impl header — the merge falls back to the signature, but ONLY
for signatures that occur exactly once on each side. An ambiguous signature is
refused rather than guessed: the cost of refusing is a stub the ordinary fill
loop then targets with full context, and the cost of guessing is a body in the
wrong impl, which at best fails to compile and at worst compiles and is wrong.

A slot is not automatically unique either. `owner_path` deliberately drops
attributes, so two impls differing only by `#[cfg]` share one slot — and since a
cluster IS a cfg-set, that is the one axis a cross-cluster seed's two trees are
guaranteed to differ on. Ambiguity is therefore tested on BOTH sides and for
BOTH keys; a key that resolves on one side of the merge is no safer for it.

Pure text→text; fn boundaries and owner chains come from `fn_bodies`, which
counts braces over code only, so a `}` in a string literal or doc comment can
neither close a body early nor desync the merge.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.cfg import cfg_inline_predicates
from pipeline_core.domain.rust_fn_slots import FnSlot, fn_bodies
from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.rust_scan import contains_word, item_start
from pipeline_core.domain.rust_use_items import (
    divergent_item_names,
    insert_use_items,
    missing_use_items,
)


def fn_body_spans(text: str) -> list[tuple[int, int, int]]:
    """``(header_start, body_start, body_end)`` for each fn with a balanced body.
    ``body_start`` is the offset just after the opening ``{``; ``body_end`` is
    the offset of the matching ``}`` (exclusive end of body). Malformed
    (unbalanced) fns are skipped."""
    return [(b.header_start, b.body_start, b.body_end) for b in fn_bodies(text)]


def count_stub_bodies(text: str) -> int:
    """Number of fns in ``text`` whose body is still ``unimplemented!()``."""
    return sum(1 for b in fn_bodies(text)
               if is_unimplemented_body(text[b.body_start:b.body_end]))


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """What a cross-cluster backfill did to one file.

    `ambiguous_skipped` is reported rather than swallowed: it is the count of
    stubs a donor HAD a body for and this merge declined to place, and a run
    that starts filling with a non-zero count here is doing avoidable model work
    that the seed could have done for free.

    `uses_added` counts the file-level imports carried over with the bodies. A
    body placed in the right slot still fails to compile if what it NAMES is not
    in the target's scope, and no per-fn fill can repair a file-level import.

    `layout_skipped` is counted APART from `ambiguous_skipped` because the two
    are different findings that happen to have the same remedy. Ambiguity means
    the merge could not tell which slot a body belongs in; layout divergence
    means it knew exactly, and the body still cannot compile here. Reporting 61
    layout refusals under "signature ambiguous across sibling impl blocks" would
    have sent the next reader hunting a merge bug instead of a shape mismatch."""

    text: str
    filled: int
    ambiguous_skipped: int
    uses_added: int = 0
    layout_skipped: int = 0
    cfg_skipped: int = 0


def unbuilt_fn_starts(text: str, cfg_set: Any) -> frozenset[int]:
    """`header_start` of every fn in `text` whose own `#[cfg(...)]` attributes
    are decidably FALSE under `cfg_set` — the fns this text's OWN build never
    compiles, and so the ones no compiler has ever checked.

    Per OCCURRENCE, not per name. `error.rs::provide` appears three times under
    three different gates, and a by-name answer would convict all three on the
    first one it found. `item_start` bounds the window to this fn's own
    attributes: a blank line does not delimit an item, so a window walked back
    to one would inherit the `#[cfg]` of whatever declaration sits above.

    Undecidable predicates are not convicted — the same conservative direction
    `fn_belongs_to_cluster` takes; only a definite False is evidence."""
    if cfg_set is None:
        return frozenset()
    mask = code_mask(text)
    out: set[int] = set()
    for b in fn_bodies(text):
        window = text[item_start(text, mask, b.header_start):b.header_start + 1]
        preds = [p.strip() for p in cfg_inline_predicates(window)]
        if any(cfg_set.evaluate(p) is False for p in preds):
            out.add(b.header_start)
    return frozenset(out)


def backfill_stub_bodies(
    target_text: str, donor_texts: Sequence[str],
    donor_cfg_sets: Sequence[Any] | None = None,
) -> BackfillResult:
    """Cross-cluster stub backfill: adopt filled fn bodies from sibling-cluster
    copies of the same file into ``target_text``'s remaining ``unimplemented!()``
    stubs. Donors apply in order — the earliest donor with a real body for a
    given SLOT wins. Only stub bodies are ever replaced, so a body this cluster
    filled itself is never overwritten.

    Donor and target here are two bakes of the SAME file, so their owner chains
    are byte-identical and slot matching is near-total (measured on `anyhow`:
    122 of 124 donor bodies matched by slot); the signature fallback exists for
    the two that do not and cannot fire on an ambiguous one.

    `donor_cfg_sets` (positionally paired with `donor_texts`, `None` per donor
    to opt out) withholds bodies the DONOR's own cfg-set never compiled. A
    cluster can hold a filled body inside a `#[cfg]` it does not build — the
    compile gate that accepts every other fill simply never sees it — and
    adopting one hands this cluster code with the authority of a verified body
    and none of the verification. Observed on `anyhow`: `stable` filled
    `wrapper.rs::provide` under `#[cfg(error_generic_member_access)]`, which
    `stable` never compiles, and seeded it to `nightly`, which does."""
    before = count_stub_bodies(target_text)
    if before == 0:
        return BackfillResult(target_text, 0, 0)
    cfg_sets = list(donor_cfg_sets or ())
    out = target_text
    skipped = 0
    layout = 0
    cfg_refused = 0
    for i, donor in enumerate(donor_texts):
        donor_cfg = cfg_sets[i] if i < len(cfg_sets) else None
        out, _placed, _skip, _layout, _cfg = _merge(out, donor, donor_cfg)
        skipped += _skip
        layout += _layout
        cfg_refused += _cfg
    # A body cannot bring its own import. The donor's bodies were authored
    # against the donor's import block, which a fill may have GROWN
    # (`use crate::wrapper::MessageError;`), so adopting bodies without it hands
    # the cluster a tree of correct code that does not resolve.
    #
    # Deliberately NOT gated on bodies placed in THIS call. The scope debt
    # belongs to the tree, not to the pass: a resumed cluster already holds the
    # donor bodies from an earlier run, places nothing new, and would keep its
    # missing imports forever — which is exactly the state a killed-and-resumed
    # `nightly` was in. Reconciling every time makes the seed idempotent, and
    # `missing_use_items` only ever ADDS a name the target neither imports nor
    # binds, so running it twice is a no-op.
    imported = 0
    for donor in donor_texts:
        _adds = missing_use_items(out, donor)
        if _adds:
            out = insert_use_items(out, _adds)
            imported += len(_adds)
    return BackfillResult(out, before - count_stub_bodies(out), skipped,
                          imported, layout, cfg_refused)


def merge_fills_preserving_existing(disk_text: str, model_text: str) -> str:
    """Merge model-emitted file with the on-disk file.

    Strategy: disk_text is the structural base (skeleton + any prior fills
    + all imports/types/macros/attributes). We walk it function-by-function
    and ONLY replace `unimplemented!()` bodies with the corresponding body
    from model_text. Everything else from disk is preserved verbatim —
    imports, type definitions, module declarations, attributes, comments.

    This is the safe direction: the model can emit a thin file that drops
    the surrounding context, but we never lose what's already on disk.
    The model contributes function bodies only.

    Matching is by `FnSlot`, with an unambiguous-signature fallback. If the
    model didn't return a given function (or returned it with `unimplemented!()`
    still, or returned it under a signature that is ambiguous on either side),
    the disk body is kept — the fill loop picks up any residuals.
    """
    return _merge(disk_text, model_text)[0]


def _merge(
    disk_text: str, model_text: str, model_cfg: Any = None,
) -> tuple[str, int, int, int, int]:
    """`(merged_text, placed, ambiguous_refusals, layout_refusals,
    unbuilt_refusals)` — the core. `model_cfg` is the cfg-set the SOURCE text
    was built under, when there is one (a cross-cluster donor); `None` for the
    model-output merge, whose thin file was never built by anything."""
    disk_fns = fn_bodies(disk_text)
    if not disk_fns:
        # Disk has no functions to fill — either an empty or
        # non-Rust-source-ish file. Don't risk overwriting structure;
        # return disk verbatim.
        return disk_text, 0, 0, 0, 0

    # The model's bodies, addressed by slot. A body the model regurgitated as
    # `unimplemented!()` adds nothing over what is already on disk.
    #
    # A slot can still be ambiguous where two sibling items are distinguished
    # only by something the owner chain drops (two impls differing solely by
    # `#[cfg]`). Two distinct bodies under one slot are therefore refused for
    # the same reason two under one signature are — the whole point is that an
    # unresolvable key must not resolve to a guess. The SAME test has to run
    # against the disk side below: ambiguity is a property of the key, and a key
    # that resolves on one side of the merge is no safer for it.
    slot_bodies: dict[FnSlot, set[str]] = {}
    model_sig_counts: Counter[str] = Counter()
    # Signatures the model returned a REAL body for — the refusal counter's test.
    # `model_sig_counts` cannot serve: it counts stubs too, and a refusal to place
    # a body nobody offered is not a stub anyone has to pay to fill.
    model_offered: set[str] = set()
    # Withheld at the SOURCE, before any key is built from them: a body its own
    # build never compiled must not become a slot's answer, a signature
    # fallback, or a refusal anyone is asked to pay for.
    unbuilt = unbuilt_fn_starts(model_text, model_cfg)
    unbuilt_offered = 0
    for b in fn_bodies(model_text):
        model_sig_counts[b.signature] += 1
        body = model_text[b.body_start:b.body_end]
        if is_unimplemented_body(body):
            continue
        if b.header_start in unbuilt:
            unbuilt_offered += 1
            continue
        model_offered.add(b.signature)
        slot_bodies.setdefault(b.slot, set()).add(body)
    by_slot: dict[FnSlot, str] = {
        slot: next(iter(bodies)) for slot, bodies in slot_bodies.items()
        if len(bodies) == 1}

    # A signature is a safe fallback key only when it names exactly one fn on
    # EACH side. One occurrence in the model but two on disk would fill both
    # disk slots from one body; two in the model but one on disk cannot say
    # which of the two the disk slot wants.
    disk_sig_counts = Counter(b.signature for b in disk_fns)
    disk_slot_counts = Counter(b.slot for b in disk_fns)
    by_sig: dict[str, str] = {
        b.signature: model_text[b.body_start:b.body_end]
        for b in fn_bodies(model_text)
        if model_sig_counts[b.signature] == 1
        and b.header_start not in unbuilt
        and not is_unimplemented_body(model_text[b.body_start:b.body_end])
    }

    if not by_slot and not by_sig:
        return disk_text, 0, 0, 0, unbuilt_offered  # model gave us nothing usable

    # Types the two files SHAPE differently. A body is written against a layout,
    # and the layout is authored per cluster while bodies are shared across
    # them — so a body naming one of these cannot compile here however perfectly
    # it is placed, and no per-fn fill can repair it because the fix is a
    # declaration. Refusing costs this cluster a native re-fill; adopting costs
    # a tree that never builds (`nightly`: 31 `E0560`/`E0609` from `ErrorImpl`
    # alone). Empty for the model-output merge, where the thin file declares no
    # items and there is nothing to disagree about.
    divergent = divergent_item_names(disk_text, model_text)

    # Walk disk fns IN REVERSE so character offsets stay valid as we splice.
    out = disk_text
    placed = 0
    refused = 0
    layout_refused = 0
    for b in reversed(disk_fns):
        if not is_unimplemented_body(disk_text[b.body_start:b.body_end]):
            continue  # already filled — never overwrite
        if disk_slot_counts[b.slot] != 1:
            # The slot names SEVERAL disk fns — sibling items separated only by
            # what the owner chain drops, `#[cfg]` above all. Filling them from
            # one body is the original defect with a wider key, and it is the
            # likeliest collision left in a CROSS-CLUSTER seed: a cluster is a
            # cfg-set, so cfg is the one axis on which the two trees are known
            # to differ. Refuse every fn in the group.
            if b.signature in model_offered:
                refused += 1
            continue
        new_body = by_slot.get(b.slot)
        if new_body is None:
            if disk_sig_counts[b.signature] != 1:
                # Ambiguous on disk and unmatched by slot: any body we pick here
                # is a coin flip between sibling impls. Refuse and count it.
                if b.signature in model_offered:
                    refused += 1
                continue
            new_body = by_sig.get(b.signature)
        if new_body is None:
            continue  # model didn't return this function
        if divergent and any(contains_word(new_body, n) for n in divergent):
            # Right slot, wrong world. Leave the stub for this cluster's own
            # fill loop, which will write it against the layout this tree
            # actually declares.
            layout_refused += 1
            continue
        out = out[:b.body_start] + new_body + out[b.body_end:]
        placed += 1
    return out, placed, refused, layout_refused, unbuilt_offered


def merge_fn_blocks_into_disk(disk_text: str, fn_blocks: dict[str, str]) -> str:
    """Merge ``{signature → body}`` into ``disk_text`` by replacing the body of
    each disk fn whose signature key matches AND whose body is currently
    ``unimplemented!()``. All non-function content is preserved verbatim — the
    per-function partner of :func:`merge_fills_preserving_existing` (same safety
    guarantees, simpler input).

    The input protocol is a bare signature (`<FN sig="…">`), so this cannot key
    on slots. It can still refuse the case the key genuinely cannot address: two
    or more STUBS sharing one signature, where placing the single returned body
    in both is guaranteed to be wrong for at least one of them. Where only one
    of the same-signature fns is still a stub the key is unambiguous in effect,
    and that is the common per-fn case."""
    if not fn_blocks:
        return disk_text
    spans = fn_bodies(disk_text)
    # Strip the trailing '{' so the sig matches the `<FN sig="...">` key.
    raw_sig = {b.body_start: disk_text[b.header_start:b.body_start]
                                      .rstrip("{ \t\n").strip()
               for b in spans}
    stub_sig_counts = Counter(
        b.signature for b in spans
        if is_unimplemented_body(disk_text[b.body_start:b.body_end]))

    out = disk_text
    for b in reversed(spans):
        if not is_unimplemented_body(out[b.body_start:b.body_end]):
            continue  # already filled — never overwrite
        if stub_sig_counts[b.signature] > 1:
            continue  # one body, several indistinguishable homes — refuse
        sig = raw_sig[b.body_start]
        new_body = fn_blocks.get(sig)
        if new_body is None:
            # Relaxed match: normalize whitespace and compare.
            normalized_sig = " ".join(sig.split())
            for k, v in fn_blocks.items():
                if " ".join(k.split()) == normalized_sig:
                    new_body = v
                    break
        if new_body is None:
            continue
        out = out[:b.body_start] + new_body + out[b.body_end:]
    return out

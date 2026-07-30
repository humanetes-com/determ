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

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.rust_fn_slots import FnSlot, fn_bodies


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
    that the seed could have done for free."""

    text: str
    filled: int
    ambiguous_skipped: int


def backfill_stub_bodies(
    target_text: str, donor_texts: Sequence[str],
) -> BackfillResult:
    """Cross-cluster stub backfill: adopt filled fn bodies from sibling-cluster
    copies of the same file into ``target_text``'s remaining ``unimplemented!()``
    stubs. Donors apply in order — the earliest donor with a real body for a
    given SLOT wins. Only stub bodies are ever replaced, so a body this cluster
    filled itself is never overwritten.

    Donor and target here are two bakes of the SAME file, so their owner chains
    are byte-identical and slot matching is near-total (measured on `anyhow`:
    122 of 124 donor bodies matched by slot); the signature fallback exists for
    the two that do not and cannot fire on an ambiguous one."""
    before = count_stub_bodies(target_text)
    if before == 0:
        return BackfillResult(target_text, 0, 0)
    out = target_text
    skipped = 0
    for donor in donor_texts:
        out, _n, _skip = _merge(out, donor)
        skipped += _skip
    return BackfillResult(out, before - count_stub_bodies(out), skipped)


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


def _merge(disk_text: str, model_text: str) -> tuple[str, int, int]:
    """`(merged_text, bodies_placed, ambiguous_refusals)` — the shared core."""
    disk_fns = fn_bodies(disk_text)
    if not disk_fns:
        # Disk has no functions to fill — either an empty or
        # non-Rust-source-ish file. Don't risk overwriting structure;
        # return disk verbatim.
        return disk_text, 0, 0

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
    for b in fn_bodies(model_text):
        model_sig_counts[b.signature] += 1
        body = model_text[b.body_start:b.body_end]
        if is_unimplemented_body(body):
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
        and not is_unimplemented_body(model_text[b.body_start:b.body_end])
    }

    if not by_slot and not by_sig:
        return disk_text, 0, 0  # model gave us nothing usable

    # Walk disk fns IN REVERSE so character offsets stay valid as we splice.
    out = disk_text
    placed = 0
    refused = 0
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
        out = out[:b.body_start] + new_body + out[b.body_end:]
        placed += 1
    return out, placed, refused


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

"""Impl-block collection use-case.

Groups every fn record the extractor reports into its impl/trait/mod container,
keyed by the FULL impl header text (source slice between the `impl` keyword and
the opening `{`) so two impls with identical `impl Foo<T>` heads but different
`where T: …` clauses stay separate blocks.

The only injected collaborator is `enumerate_fns` (the `StructureReader` seam);
the byte-range scan (`parse_impl_block_ranges`) and the trait-name extraction
(`extract_trait_from_container`) are pure and imported directly.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...domain.impl_block_ranges import ImplRange, parse_impl_block_ranges
from ...domain.models import FnLocator, FnRecord, ImplBlock
from ...prompts.sections import extract_trait_from_container

EnumerateFns = Callable[[Path], "list[dict[str, Any]] | None"]


def fn_container_index(
    src_dir: Path, *, enumerate_fns: EnumerateFns,
) -> dict[FnLocator, str]:
    """(file_rel_to_src, fn_name) → container ("impl … for …" / "trait …" / "").

    The syn enumerator is the one source of truth for which impl owns a fn —
    every retry site needs this to resolve the self-type whose definition must
    be injected. First record wins for duplicate names, matching the previous
    inline builders."""
    idx: dict[FnLocator, str] = {}
    for rec in (enumerate_fns(src_dir) or []):
        idx.setdefault(FnLocator(rec.get("file", ""), rec.get("fn_name", "")),
                       rec.get("container", ""))
    return idx


def fn_occurrence_index(
    src_dir: Path, *, enumerate_fns: EnumerateFns,
) -> dict[FnLocator, tuple[tuple[str, str], ...]]:
    """(file_rel, fn_name) → every occurrence's ``(container, sig_text)``, in
    record order.

    :func:`fn_container_index` keeps the FIRST record per name, which is right
    for a caller that has already resolved which definition it means. A repair
    frontier has not: its key is (file, bare name), so on this crate
    `kind.rs::new` stands for `Adhoc::new`, `Trait::new` AND `Boxed::new`, and a
    loop that can only ever address the first re-authored `Adhoc::new` three
    times across two runs while `Boxed::new` — on the path of the failing test —
    was unreachable. This index is what lets a caller enumerate the definitions
    a key stands for and target each one by container.
    """
    idx: dict[FnLocator, list[tuple[str, str]]] = {}
    for rec in (enumerate_fns(src_dir) or []):
        key = FnLocator(rec.get("file", ""), rec.get("fn_name", ""))
        idx.setdefault(key, []).append(
            (rec.get("container", ""), rec.get("sig_text", "")))
    return {k: tuple(v) for k, v in idx.items()}


def collect_impl_blocks(src_dir: Path, *, enumerate_fns: EnumerateFns) -> list[ImplBlock] | None:
    """Return one :class:`ImplBlock` per (file, header_text) pair, or None when
    the extractor binary isn't available."""
    recs = enumerate_fns(src_dir)
    if recs is None:
        return None

    # Step 1 — per file: parse the source to get impl block byte ranges, then
    # assign each fn record to the block whose body range contains its
    # sig_byte_start. Caches parsed ranges so we don't re-parse per fn record.
    file_to_source: dict[str, str] = {}
    file_to_impl_ranges: dict[str, list[ImplRange]] = {}

    def _source_for(rel: str) -> str:
        if rel not in file_to_source:
            p = src_dir / rel
            try:
                file_to_source[rel] = p.read_text(encoding="utf-8")
            except Exception:
                file_to_source[rel] = ""
        return file_to_source[rel]

    def _ranges_for(rel: str) -> list[ImplRange]:
        if rel not in file_to_impl_ranges:
            file_to_impl_ranges[rel] = parse_impl_block_ranges(_source_for(rel))
        return file_to_impl_ranges[rel]

    # Step 2 — group fns by (file, header_text). header_text is the actual
    # source slice for the impl block — generics, where-clauses, `for Trait`.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in recs:
        rel = r.get("file", "")
        sig_start = int(r.get("sig_byte_start", 0))
        header_text = ""
        for rng in _ranges_for(rel):
            if rng.body_open < sig_start < rng.body_close:
                src = _source_for(rel)
                header_text = src[rng.header_start:rng.body_open].rstrip()
                break
        if not header_text:
            # Fallback to the extractor's container string when source parsing
            # missed (fn in a trait, free fn, or the scanner failed).
            header_text = r.get("container", "") or ""
        key = (rel, header_text)
        groups.setdefault(key, []).append(r)

    # Step 3 — render block records. Two trait impls in the same file are
    # "related" iff they share the same trait name (regex-free via
    # extract_trait_from_container; None for inherent impls / free fns).
    blocks: list[ImplBlock] = []
    file_to_trait_containers: dict[str, dict[str, list[str]]] = {}
    for (file_rel, container), _members in groups.items():
        if not container:
            continue
        trait_name = extract_trait_from_container(container)
        if not trait_name:
            continue
        file_to_trait_containers.setdefault(file_rel, {}).setdefault(
            trait_name, []).append(container)
    for (file_rel, container), members in groups.items():
        members_sorted = sorted(members, key=lambda r: int(r.get("sig_byte_start", 0)))
        stub_members = [m for m in members_sorted if m.get("has_unimplemented_body")]
        related: list[str] = []
        if container:
            trait_name = extract_trait_from_container(container)
            if trait_name:
                same_trait = file_to_trait_containers.get(file_rel, {}).get(trait_name, [])
                related = [c for c in same_trait if c != container]
        blocks.append(ImplBlock(
            file=file_rel,
            container=container,
            members=tuple(FnRecord.from_dict(r) for r in members_sorted),
            stub_members=tuple(FnRecord.from_dict(r) for r in stub_members),
            related_containers=tuple(related),
        ))
    return blocks

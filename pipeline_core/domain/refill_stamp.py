"""Who a compiling body may be re-filled for — container-qualified, not (file, name).

The TDD frontier key is ``FnLocator(file, fn_name)``. Four ``from`` impls in
``src/error.rs`` therefore inherit one another's tests, and a BEHAVIOR
conviction that only shares the token ``Error`` stamps every ``From`` coercion
as DIRECT. Run ``463713-b7c666c8`` (2026-08-28/29) then spent the night
rewriting ``error.into_boxed_dyn_error()`` because ``test_boxed_str`` panicked
at ``construct_from_boxed`` and Debug asserts lived on the same key.

File-first's U2 gate already reads ``convicting_for``. The refill *stamp* used
``tests_for``. This module is the stamp's filter: a test convicts a member only
when the record supports THIS container, and a refill-only block whose filtered
set is empty is not a GPU target.

Also: after file-first served a file this call, refill-only blocks of that file
are a same-iteration re-buy — drop them; the next TDD sweep is the first moment
they can be judged.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from pipeline_core.domain.attribution import FailureFrontier, container_key
from pipeline_core.domain.impl_block_ranges import parse_impl_block_ranges
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite, ImplBlock
from pipeline_core.domain.rust_idents import impl_trait_name
from pipeline_core.domain.test_report import parse_panic_location
from pipeline_core.domain.test_results import identifiers_in


def format_traits_used(test_src: str) -> frozenset[str]:
    """Which std formatting traits a test's OWN source invokes, from its
    format placeholders and stringify calls: `{:?}`/`{:#?}` need `Debug`,
    `{}`/`{:#}`/`.to_string()` need `Display`.

    The single home of the rule. The attribution module imports it for the
    format-implementer hop and the futility-escalation gate; the stamp
    filter reads it directly.
    """
    traits: set[str] = set()
    if "{:?}" in test_src or "{:#?}" in test_src:
        traits.add("Debug")
    if ("{}" in test_src or "{:#}" in test_src
            or ".to_string()" in test_src):
        traits.add("Display")
    return frozenset(traits)


def homonym_containers(
    blocks: Iterable[ImplBlock],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """``(file, fn_name) → containers in that file that declare the name``."""
    acc: dict[tuple[str, str], list[str]] = {}
    for b in blocks:
        names = {m.fn_name for m in (*b.members, *b.stub_members, *b.refill_members)}
        for name in names:
            acc.setdefault((b.file, name), []).append(b.container)
    return {k: tuple(vs) for k, vs in acc.items()}


def assertion_owned_by_container(
    container: str,
    homonym_containers_: Sequence[str],
    test_text: str,
) -> bool:
    """Among same-named members in one file, does THIS container uniquely
    best-match an assertion test?

    Identifier overlap alone is not enough: ``Error`` sits in both
    ``impl Debug for Error`` and ``impl From<Error> for Box<dyn …>``, which
    is how Debug ``msg\\n\\n<disabled>`` asserts convicted every ``from``.
    A format-trait the test actually uses (``{:?}`` → Debug) outranks a
    shared self-type token. A container whose trait is not in that set
    does not own the assertion even when every same-named sibling also
    scores 0 — otherwise four ``from`` impls keep a Debug assert because
    homonyms are keyed on ``(file, fn_name)`` and ``fmt`` is a different
    name. When the text carries no format-trait token (libtest evidence),
    a tie of zeros keeps rather than un-stamping everyone.
    """
    others = [
        c for c in homonym_containers_
        if container_key(c) != container_key(container)]
    idents = identifiers_in(test_text)
    traits = format_traits_used(test_text)
    this_trait = impl_trait_name(container) or ""
    if traits and this_trait and this_trait not in traits:
        return False
    if not others:
        return True

    def score(c: str) -> int:
        trait = impl_trait_name(c) or ""
        n = len(identifiers_in(c) & idents)
        if trait and trait in traits:
            n += 10
        if trait and trait in idents:
            n += 5
        return n

    mine = score(container)
    best_other = max(score(c) for c in others)
    if mine == 0 and best_other == 0:
        return True
    return mine > best_other


def _stub_panic_in_src(text: str) -> bool:
    return "unimplemented" in text or "not implemented" in text or "todo!" in text


def stamp_reads_dispatch(*, covers: bool, file_primary: bool) -> bool:
    """Whether the refill stamp reads ``tests_for`` rather than ``convicting_for``.

    ``impl_route_covers`` keys on stub+refill count, not on the block's total
    members. A 40-member ``impl Error`` with two work items in the batch looks
    coverable, so the stamp used ``convicting_for`` and dropped every
    dispatch-only constructor; then file-primary took the file anyway and the
    impl route dropped the block. Run ``125003-269e2338`` (2026-08-31): 9 of
    12 batch units ``no route attempted``, including ``construct_from_boxed``
    sitting in the same block as the two members that did enter the write list.

    Under file-primary the file pass writes every block with work, so the
    impl-route homonym filter is the wrong planner. The filter stays for the
    default route order on a block that route will actually attempt.
    """
    return file_primary or not covers


def convicting_tests_for_member(
    frontier: FailureFrontier,
    file: str,
    fn_name: str,
    container: str,
    homonyms: Sequence[str],
) -> frozenset[str]:
    """Tests that may revert THIS member, not a same-named sibling.

    Starts from the site-precise map when this container was written
    (live RUNTIME/COMPILE/BEHAVIOR). Falls back to ``convicting_for`` plus
    :func:`assertion_owned_by_container` for of_direct fixtures that lifted
    locators to the empty-container site — scoring stays until those
    fixtures write real containers. Never ``tests_for``.
    """
    exact = frontier.convicting_for_site(FnSite(file, fn_name, container))
    if exact:
        return frozenset(exact)
    raw = frontier.convicting_for(FnLocator(file, fn_name))
    if not raw:
        return frozenset()
    others = [
        c for c in homonyms if container_key(c) != container_key(container)]
    if not others:
        return frozenset(raw)
    kept: set[str] = set()
    for t in raw:
        evidence = frontier.evidence_for(t)
        source = frontier.source_for(t)
        file_hit, _line, _col, _msg = parse_panic_location(evidence)
        if file_hit and not str(file_hit).startswith("tests/") and _stub_panic_in_src(evidence):
            # Panic in production src of another fn — not this coercion.
            continue
        if assertion_owned_by_container(container, homonyms, source or evidence or t):
            kept.add(t)
    return frozenset(kept)


def blocks_after_file_first(
    viable: Sequence[ImplBlock],
    served_files: set[str],
) -> tuple[list[ImplBlock], list[ImplBlock]]:
    """After file-first served a file this call, keep only stub-bearing
    blocks of that file. Refill-only waits for the next sweep.

    Returns ``(kept, dropped)``.
    """
    kept: list[ImplBlock] = []
    dropped: list[ImplBlock] = []
    for b in viable:
        if b.file in served_files and not b.stub_members:
            dropped.append(b)
        else:
            kept.append(b)
    return kept, dropped


def byte_to_line(text: str, byte: int) -> int:
    """1-indexed line of ``byte`` in ``text``."""
    if byte <= 0:
        return 1
    return text.count("\n", 0, min(byte, len(text))) + 1


def block_line_window(
    file_text: str,
    records: Sequence[FnRecord],
) -> tuple[int, int]:
    """Inclusive 1-indexed line range covering the members' signatures/bodies.

    ``(0, 0)`` when no offsets are recorded (fixtures, extractor absent) —
    callers treat that as "no window, keep file-scoped focusing".
    """
    starts = [m.sig_byte_start for m in records if m.sig_byte_start]
    ends = [m.body_byte_end for m in records if m.body_byte_end]
    if not starts or not file_text:
        return 0, 0
    lo = byte_to_line(file_text, min(starts))
    hi = byte_to_line(file_text, max(ends) if ends else min(starts))
    return lo, hi


def container_line_window(file_text: str, container: str) -> tuple[int, int]:
    """Inclusive 1-indexed line range of this impl in ``file_text``
    (header through close). Recomputed from the current tree so a splice
    that grew a body cannot put the model's own error in the cross bucket.

    ``(0, 0)`` when ``container`` is empty (free fn) or unmatched —
    callers treat that as file-scoped focusing.
    """
    if not container or not file_text:
        return 0, 0
    want = container_key(container)
    for r in parse_impl_block_ranges(file_text):
        header = file_text[r.header_start:r.body_open]
        if container_key(header) != want:
            continue
        return (byte_to_line(file_text, r.header_start),
                byte_to_line(file_text, r.body_close))
    return 0, 0

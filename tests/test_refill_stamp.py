"""Container-qualified refill stamp — plan 2026-08-29 T1b/T2 plus the
homonym filter the independent walk named as the missing closer.

The frontier key is ``(file, fn_name)``. Four ``from`` impls therefore
share tests. These tests assert the PLAN sentences on the channels the
pipeline actually supplies: Debug ``{:?}`` in the test's OWN source
uniquely owns ``impl Debug for Error``; libtest failure text does not.
After file-first served a file, refill-only blocks of that file drop.
"""
from __future__ import annotations

from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
from pipeline_core.domain.refill_stamp import (
    assertion_owned_by_container,
    block_line_window,
    blocks_after_file_first,
    container_line_window,
    convicting_tests_for_member,
    stamp_reads_dispatch,
)

DEBUG_C = "impl Debug for Error"
DISPLAY_C = "impl Display for Error"
FROM_BOX = (
    "impl From<Error> for Box<dyn StdError + Send + 'static>"
)
FROM_E = "impl < E > From < E > for Error"
# The channel ``format_traits_used``'s docstring names: the test's OWN source.
DEBUG_SRC = (
    '#[test] fn message_error_debug_with_string() { '
    'assert_eq!(format!("{:?}", err), "msg"); }'
)
CONVERT_SRC = (
    '#[test] fn test_convert() { '
    'let _: Box<dyn StdError + Send> = err.into(); }'
)
# Live libtest evidence for that test (run `463713-b7c666c8`): panic in
# `tests/wrapper_gen_*.rs`, left/right, no `{:?}`. Production
# `failure_evidence_by_test` yields this shape, never DEBUG_SRC.
LIVE_EVIDENCE = (
    "thread 'message_error_debug_with_string' panicked at "
    "tests/wrapper_gen_4.rs:41:5:\n"
    "assertion `left == right` failed\n"
    '  left: "msg\\n\\n<disabled>"\n'
    ' right: "msg"'
)
BOXED_PANIC = (
    "thread 'test_boxed_str' panicked at src/error.rs:287:9:\n"
    "not implemented"
)


def test_debug_format_in_test_source_uniquely_owns_debug_impl_not_from() -> None:
    """``{:?}`` in the test's own source uniquely owns ``impl Debug for Error``,
    not a From coercion."""
    homonyms = (DEBUG_C, FROM_BOX)
    assert assertion_owned_by_container(DEBUG_C, homonyms, DEBUG_SRC)
    assert not assertion_owned_by_container(FROM_BOX, homonyms, DEBUG_SRC)


def test_debug_source_does_not_stamp_from_even_among_from_only_homonyms() -> None:
    """Homonyms are same ``(file, fn_name)``. The four ``from`` impls do not
    include ``impl Debug for Error``. A ``{:?}`` source must still not convict
    a From coercion — trait mismatch, not a tie of zeros."""
    homonyms = (FROM_BOX, FROM_E)
    assert not assertion_owned_by_container(FROM_BOX, homonyms, DEBUG_SRC)
    assert not assertion_owned_by_container(FROM_E, homonyms, DEBUG_SRC)


def test_libtest_evidence_tie_does_not_un_stamp_debug() -> None:
    """Negative control: live evidence has no format-trait token. Scoring it
    uniquely assigns nobody; a tie of zeros must not drop the test from every
    homonym — that is how ``impl Debug for Error`` lost its refill."""
    homonyms = (DEBUG_C, DISPLAY_C)
    assert assertion_owned_by_container(DEBUG_C, homonyms, LIVE_EVIDENCE)
    assert assertion_owned_by_container(DISPLAY_C, homonyms, LIVE_EVIDENCE)


def test_convert_text_uniquely_owns_from_box_not_from_e() -> None:
    """Among the From homonyms, Box/Send in the test owns the Box coercion."""
    homonyms = (FROM_BOX, FROM_E)
    assert assertion_owned_by_container(FROM_BOX, homonyms, CONVERT_SRC)
    assert not assertion_owned_by_container(FROM_E, homonyms, CONVERT_SRC)


def test_convicting_drops_src_stub_panic_and_debug_assert_from_from_block() -> None:
    """The live From block's attributed mix: stub-panic at construct_from_boxed
    (evidence) plus a Debug assert whose SOURCE uniquely owns Debug, not From."""
    key = FnLocator("src/error.rs", "from")
    fr = FailureFrontier.build(
        {key: {"test_boxed_str", "message_error_debug_with_string"}},
        {key: {"test_boxed_str", "message_error_debug_with_string"}},
        evidence={
            "test_boxed_str": BOXED_PANIC,
            "message_error_debug_with_string": LIVE_EVIDENCE,
        },
        sources={"message_error_debug_with_string": DEBUG_SRC},
    )
    kept = convicting_tests_for_member(
        fr, "src/error.rs", "from", FROM_BOX, (FROM_BOX, FROM_E, DEBUG_C))
    assert kept == frozenset()


def test_convicting_keeps_debug_assert_on_debug_impl_when_source_rides() -> None:
    """Plan non-goal: a Debug-for-Error block whose tests assertion-fail in
    wrapper_gen still refills — once the source channel is on the frontier."""
    fmt_key = FnLocator("src/error.rs", "fmt")
    fr = FailureFrontier.build(
        {fmt_key: {"message_error_debug_with_string"}},
        {fmt_key: {"message_error_debug_with_string"}},
        evidence={"message_error_debug_with_string": LIVE_EVIDENCE},
        sources={"message_error_debug_with_string": DEBUG_SRC},
    )
    kept = convicting_tests_for_member(
        fr, "src/error.rs", "fmt", DEBUG_C, (DEBUG_C, DISPLAY_C))
    assert kept == frozenset({"message_error_debug_with_string"})
    dropped = convicting_tests_for_member(
        fr, "src/error.rs", "fmt", DISPLAY_C, (DEBUG_C, DISPLAY_C))
    assert dropped == frozenset()


def test_of_dispatch_frontier_stamps_nobody() -> None:
    """Negative control: dispatch projection is not conviction."""
    key = FnLocator("src/error.rs", "from")
    fr = FailureFrontier.of_dispatch({key: {"message_error_debug_with_string"}})
    kept = convicting_tests_for_member(
        fr, "src/error.rs", "from", FROM_BOX, (FROM_BOX,))
    assert kept == frozenset()


def test_blocks_after_file_first_drops_refill_only_of_served_files() -> None:
    """After file-first served error.rs, keep stub-bearing impl Error; drop
    refill-only From. Unserved files stay (the filter is served-file, not
    'every refill-only block in the crate')."""
    stub = FnRecord(file="src/error.rs", fn_name="construct_from_boxed",
                    container="impl Error", has_unimplemented_body=True)
    filled = FnRecord(file="src/error.rs", fn_name="from",
                      container=FROM_BOX, has_unimplemented_body=False)
    error_impl = ImplBlock(
        file="src/error.rs", container="impl Error",
        members=(stub,), stub_members=(stub,))
    from_impl = ImplBlock(
        file="src/error.rs", container=FROM_BOX,
        members=(filled,), stub_members=(), refill_members=(filled,))
    chain_filled = FnRecord(file="src/chain.rs", fn_name="len",
                            container="impl<'a> Chain<'a>",
                            has_unimplemented_body=False)
    chain = ImplBlock(
        file="src/chain.rs", container="impl<'a> Chain<'a>",
        members=(chain_filled,), stub_members=(),
        refill_members=(chain_filled,))
    kept, dropped = blocks_after_file_first(
        [error_impl, from_impl, chain],
        {"src/error.rs", "src/chain.rs"})
    assert kept == [error_impl]
    assert from_impl in dropped and chain in dropped


def test_block_line_window_from_byte_offsets() -> None:
    text = "a\n" + ("x" * 10) + "\n" + "tail\n"
    rec = FnRecord(file="src/error.rs", fn_name="from",
                   sig_byte_start=2, body_byte_end=14)
    assert block_line_window(text, (rec,)) == (2, 3)


def test_block_line_window_absent_offsets_is_unset() -> None:
    rec = FnRecord(file="src/error.rs", fn_name="from")
    assert block_line_window("fn from() {}", (rec,)) == (0, 0)


def test_container_line_window_covers_header_through_close() -> None:
    """LAST_ERROR window is the live impl (header through close), recomputed
    from the current file so a splice cannot stale it."""
    text = (
        "fn other() {}\n"
        "impl Debug for Error {\n"
        "    fn fmt(&self, f: &mut Formatter) { ok }\n"
        "}\n"
        "impl From<Error> for Box<dyn StdError> {\n"
        "    fn from(e: Error) { e.into() }\n"
        "}\n"
    )
    assert container_line_window(text, "impl Debug for Error") == (2, 4)
    assert container_line_window(
        text, "impl From<Error> for Box<dyn StdError>") == (5, 7)
    assert container_line_window(text, "") == (0, 0)


def test_stamp_reads_exact_site_without_scoring() -> None:
    """Live S1 path: Debug site has the assert, From site does not — no
    sources channel required. of_dispatch still stamps nobody."""
    from pipeline_core.domain.models import FnSite
    debug_site = FnSite("src/error.rs", "fmt", DEBUG_C)
    fmt_loc = FnLocator("src/error.rs", "fmt")
    from_loc = FnLocator("src/error.rs", "from")
    fr = FailureFrontier.build(
        {fmt_loc: {"message_error_debug_with_string"},
         from_loc: {"message_error_debug_with_string"}},
        {debug_site: {"message_error_debug_with_string"}},
    )
    kept = convicting_tests_for_member(
        fr, "src/error.rs", "fmt", DEBUG_C, (DEBUG_C, DISPLAY_C))
    assert kept == frozenset({"message_error_debug_with_string"})
    dropped = convicting_tests_for_member(
        fr, "src/error.rs", "from", FROM_BOX, (FROM_BOX, FROM_E))
    assert dropped == frozenset()
    assert convicting_tests_for_member(
        FailureFrontier.of_dispatch({fmt_loc: {"message_error_debug_with_string"}}),
        "src/error.rs", "fmt", DEBUG_C, (DEBUG_C,),
    ) == frozenset()


def test_stamp_reads_dispatch_when_file_primary_even_if_the_block_looks_coverable() -> None:
    """File-primary writes the block; a coverable work-item count must not
    keep convicting_for. Negative control: default order on a covered block
    still uses the homonym filter."""
    assert stamp_reads_dispatch(covers=True, file_primary=True)
    assert not stamp_reads_dispatch(covers=True, file_primary=False)
    assert stamp_reads_dispatch(covers=False, file_primary=False)
    assert stamp_reads_dispatch(covers=False, file_primary=True)

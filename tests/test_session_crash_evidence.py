"""A session's turn 0 carries the crash evidence of the units it can edit.

Run `118933-877c43bd` (2026-09-27, cluster `stable`): 13 tests aborted on one
stack overflow through a three-member call cycle. The localizer rendered the
cycle (members, "each body reaches the next and the last reaches the first")
and the file-first and per-fn prompts of the same fill carried it. Both slice
sessions — the first and largest runs of the fill — did not: their turn 0
quoted each target's own words ("has overflowed its stack") and nothing
else, because `build_slice_session_prompt` had no evidence slot. Six turns,
five compile failures, every reply rewriting one cycle member alone.
"""
from __future__ import annotations

from pipeline_core.domain.crash_site import crash_evidence_for_units
from pipeline_core.domain.models import FnLocator, FnRecord, FnSite
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.prompts.slice_session import build_slice_session_prompt

CYCLE = "These fns call each other without a base case (`ping`, `pong`)."
OTHER = "An unrelated fault in `render`."

# Keyed as the localizer keys evidence: by locator, with the `src/` path.
EVIDENCE = {
    FnLocator("src/ring.rs", "ping"): CYCLE,
    FnLocator("src/ring.rs", "pong"): CYCLE,
    FnLocator("src/view.rs", "render"): OTHER,
}

PING = FnRecord(file="src/ring.rs", fn_name="ping", container="impl Ring",
                sig_text="fn ping(self) -> Out", body_text="self.pong()")


def _site(r: FnRecord) -> FnSite:
    return FnSite(r.file, r.fn_name, r.container)


def test_a_units_crash_block_is_selected_once_and_foreign_blocks_are_not() -> None:
    # Two locators share one cycle block; a site carries a container the
    # locator does not. The join is (file, fn) and the block appears once.
    got = crash_evidence_for_units(
        EVIDENCE, [_site(PING), FnSite("src/ring.rs", "pong", "impl Ring")])
    assert got == CYCLE


def test_the_join_survives_the_src_prefix_disagreement() -> None:
    # Extractor records and block records disagree on the `src/` prefix; an
    # empty join here would be a rooting bug read as "no crash".
    assert crash_evidence_for_units(
        EVIDENCE, [FnSite("ring.rs", "ping", "impl Ring")]) == CYCLE


def test_no_units_means_every_block_for_the_size_reservation() -> None:
    assert crash_evidence_for_units(EVIDENCE, None) == f"{CYCLE}\n\n{OTHER}"


def test_a_slice_off_the_crash_selects_nothing() -> None:
    assert crash_evidence_for_units(
        EVIDENCE, [FnSite("src/view.rs", "layout", "")]) == ""


def _turn0(crash_evidence: str) -> str:
    sl = SessionSlice(tests=("t_ring",), units=(_site(PING),))
    return build_slice_session_prompt(
        module_name="m", cluster_name="c", slice_=sl,
        records={_site(PING): PING}, all_records=[PING],
        file_texts={"src/ring.rs": f"{PING.sig_text} {{ {PING.body_text} }}"},
        target_evidence={"t_ring": "thread 't_ring' has overflowed its stack"},
        test_blocks=[], types_section="", crash_evidence=crash_evidence)


def test_turn0_renders_the_block_before_any_code() -> None:
    prompt = _turn0(crash_evidence_for_units(EVIDENCE, [_site(PING)]))
    assert "<CRASH_EVIDENCE>" in prompt and CYCLE in prompt
    assert prompt.index(CYCLE) < prompt.index("THE SLICE")


def test_turn0_without_evidence_has_no_block() -> None:
    # Negative control: the pre-fix turn 0 — the target's own words only.
    prompt = _turn0("")
    assert "<CRASH_EVIDENCE>" not in prompt and CYCLE not in prompt
    assert "has overflowed its stack" in prompt

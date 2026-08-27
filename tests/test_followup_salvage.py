"""A CUT forced followup keeps what it closed, and nothing more.

WHY these exist. The forced followup writes the reply for 1 595 of the 1 596
followups on record (`logs/events.jsonl`, `llm.stream_meta` rows with
`followup=True`), so the two failure modes are symmetric and both expensive:
splicing a half-written body as a finished one, and discarding a reply whose
first 21 of 25 blocks are usable. The rule under test is that a cut reply is
truncated to its last closed `</FN>` / `</FILE>` — which is what the main
stream has always effectively done, since its `llm/length_truncated` guard
logs and splices — and that a reply which closed nothing yields "".

The recorded shape these pin against: run `82157-476ddc16`'s constructor call
`49385be5…` returned 8 192 CHARACTERS carrying 21 `<FN ` and 21 `</FN>`,
ending on a closer, against a followup budget of 8 192 TOKENS. It was not cut;
`test_uncut_reply_is_returned_verbatim` is that case, and it is the control for
every salvage assertion below.
"""

from __future__ import annotations

from typing import Any

from pipeline_core.domain.llm_output import extract_fn_bodies, salvage_complete_blocks
from pipeline_core.domain.reasoning_cap import (
    summarize_followup_cut,
    summarize_followup_salvage,
)
from pipeline_core.llm.controller import _FollowupOutcome


def _fn(name: str, body: str) -> str:
    return f'<FN name="{name}" block="impl Error">{body}</FN>\n'


def _reply(n: int) -> str:
    return "".join(_fn(f"f{i}", f"todo_{i}()") for i in range(n))


# --- the domain rule -------------------------------------------------------

def test_cut_after_a_closed_block_keeps_every_closed_block() -> None:
    """21 closed blocks plus a fragment → the 21 survive."""
    text = _reply(21) + '<FN name="f21" block="impl Error">Self::constr'
    kept = salvage_complete_blocks(text)
    assert kept.endswith("</FN>")
    assert kept.count("<FN ") == 21
    # The parser agrees: 21 usable bodies, and the fragment is gone.
    assert [r["fn_name"] for r in extract_fn_bodies(kept)] == [
        f"f{i}" for i in range(21)]
    assert "Self::constr" not in kept


def test_a_reply_that_closed_nothing_salvages_to_empty() -> None:
    """A mid-body cut of a SINGLE element has no complete answer in it —
    this is the case the discard rule exists for, and salvage must not
    reintroduce it."""
    assert salvage_complete_blocks(
        '<FN name="construct_from_adhoc" block="impl Error">Self::construct_fr') == ""
    assert salvage_complete_blocks("") == ""
    assert salvage_complete_blocks("no tags at all, just prose") == ""


def test_file_replies_salvage_at_the_last_closed_file() -> None:
    text = ('<FILE path="tests/a_gen_1.rs">#[test] fn t() {}</FILE>\n'
            '<FILE path="tests/a_gen_2.rs">#[test] fn u() {')
    kept = salvage_complete_blocks(text)
    assert kept.endswith("</FILE>")
    assert kept.count("<FILE ") == 1


def test_the_later_closer_wins_when_a_reply_carries_both_shapes() -> None:
    text = ('<FILE path="tests/a_gen_1.rs">x</FILE>\n' + _reply(2)
            + '<FN name="f2" block="impl Error">unfin')
    kept = salvage_complete_blocks(text)
    assert kept.endswith("</FN>") and kept.count("<FN ") == 2


def test_a_malformed_last_closer_is_not_trusted() -> None:
    """The element a truncation damaged is the one whose closer is likely
    malformed, so salvage cuts at a WELL-FORMED closer even though the FN
    reader tolerates `</FN` without its `>`."""
    text = _reply(2) + '<FN name="f2" block="impl Error">body</FN'
    kept = salvage_complete_blocks(text)
    assert kept.count("<FN ") == 2


# --- the outcome the controller returns ------------------------------------

def test_uncut_reply_is_returned_verbatim() -> None:
    """The recorded 82157 shape: finished of its own accord, so nothing is
    trimmed and nothing is reported as salvaged. This is the control."""
    raw = _reply(21)
    fu = _FollowupOutcome(result=raw, finish_reason="stop",
                          raw_chars=len(raw), salvaged_chars=0)
    assert fu.complete and fu.spliceable and not fu.cut
    assert fu.result == raw


def test_a_cut_outcome_is_spliceable_but_never_complete() -> None:
    kept = _reply(21)
    fu = _FollowupOutcome(result=kept, finish_reason="length",
                          raw_chars=len(kept) + 30, salvaged_chars=len(kept))
    assert fu.cut and fu.spliceable and not fu.complete


def test_a_cut_that_closed_nothing_is_not_spliceable() -> None:
    fu = _FollowupOutcome(result="", finish_reason="length",
                          raw_chars=400, salvaged_chars=0)
    assert fu.cut and not fu.spliceable and not fu.complete


def test_a_failed_followup_is_not_spliceable() -> None:
    fu = _FollowupOutcome(finish_reason="error")
    assert not fu.spliceable and not fu.complete


def test_think_cap_stop_counts_as_cut() -> None:
    fu = _FollowupOutcome(result=_reply(1), finish_reason="stop",
                          stop="think_cap")
    assert fu.cut and not fu.complete and fu.spliceable


# --- the audit fold --------------------------------------------------------

def _row(**kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"kind": "followup", "finish_reason": "length"}
    row.update(kw)
    return row


def test_salvage_fold_splits_cut_into_salvaged_and_lost() -> None:
    rows = [
        _row(salvaged_chars=6000),          # cut, last block lost only
        _row(salvaged_chars=0),             # cut, whole reply lost
        _row(finish_reason="stop", salvaged_chars=0),   # not cut
        {"kind": "stream", "finish_reason": "length"},  # main stream, ignored
    ]
    cut, salvaged = summarize_followup_salvage(rows)
    assert (cut, salvaged) == (2, 1)
    # The pre-existing count is unchanged — this fold only adds a column.
    assert summarize_followup_cut(rows) == 2


def test_rows_written_before_the_field_count_as_not_salvaged() -> None:
    """Negative control for the historical shape: 1 596 recorded followups
    carry no `salvaged_chars`, and reading them must not invent one."""
    rows = [_row(), _row()]
    assert summarize_followup_salvage(rows) == (2, 0)


# --- through the real controller -------------------------------------------

def test_a_cut_followup_splices_its_closed_prefix_and_records_the_loss() -> None:
    """End to end on the real `LLMController`: main stream burns its budget on
    reasoning, the followup writes 21 closed blocks and is cut mid-22nd. The
    call returns the 21; `llm.followup_meta` records the raw length beside
    what survived, so the audit can tell this from a total loss."""
    from pipeline_core.llm import Delta, Hooks, LLMController
    from tests.test_llm_controller import SENTINEL, FakeTransport, _spec

    raw = _reply(21) + '<FN name="f21" block="impl Error">Self::constr'
    t = FakeTransport(
        [[Delta(reasoning="r" * 71_584)], [Delta(content=raw)]],
        spec=_spec(content_budget=8192, thinking_budget=8192),
        finish_reasons=["length", "length"])
    events: dict[str, list[dict[str, Any]]] = {}
    hooks = Hooks()
    hooks.emit = lambda name, data: events.setdefault(name, []).append(dict(data))
    out = LLMController(hooks, default_sentinel=SENTINEL).run(
        t, system="s", prompt="p", model="m", max_tokens=SENTINEL)

    assert out.endswith("</FN>") and out.count("<FN ") == 21
    assert "Self::constr" not in out
    assert len(extract_fn_bodies(out)) == 21
    row = events["llm.followup_meta"][0]
    assert row["finish_reason"] == "length"
    assert row["content_chars"] == len(raw)          # what the stream produced
    assert row["salvaged_chars"] == len(out)         # what survived the cut
    assert 0 < row["salvaged_chars"] < row["content_chars"]


def test_a_cut_followup_that_closed_nothing_leaves_the_main_reply_alone() -> None:
    """The protective half: nothing usable, so the truncation is NOT spliced
    and `salvaged_chars` is 0 against a nonempty `content_chars`."""
    from pipeline_core.llm import Delta, Hooks, LLMController
    from tests.test_llm_controller import SENTINEL, FakeTransport, _spec

    t = FakeTransport(
        [[Delta(reasoning="r" * 900)],
         [Delta(content='<FN name="construct_from_adhoc" block="impl Error">Self::con')]],
        spec=_spec(content_budget=8192, thinking_budget=8192),
        finish_reasons=["length", "length"])
    events: dict[str, list[dict[str, Any]]] = {}
    hooks = Hooks()
    hooks.emit = lambda name, data: events.setdefault(name, []).append(dict(data))
    out = LLMController(hooks, default_sentinel=SENTINEL).run(
        t, system="s", prompt="p", model="m", max_tokens=SENTINEL)

    assert "Self::con" not in out
    row = events["llm.followup_meta"][0]
    assert row["content_chars"] > 0 and row["salvaged_chars"] == 0


def test_an_uncut_followup_is_spliced_whole_and_reports_no_salvage() -> None:
    """The recorded 82157 case, as the control: the followup finished on its
    own, so the reply is returned verbatim and `salvaged_chars` is 0 — a
    reader must not confuse "not cut" with "cut and lost everything"."""
    from pipeline_core.llm import Delta, Hooks, LLMController
    from tests.test_llm_controller import SENTINEL, FakeTransport, _spec

    raw = _reply(21)
    t = FakeTransport(
        [[Delta(reasoning="r" * 71_584)], [Delta(content=raw)]],
        spec=_spec(content_budget=8192, thinking_budget=8192),
        finish_reasons=["length", "stop"])
    events: dict[str, list[dict[str, Any]]] = {}
    hooks = Hooks()
    hooks.emit = lambda name, data: events.setdefault(name, []).append(dict(data))
    out = LLMController(hooks, default_sentinel=SENTINEL).run(
        t, system="s", prompt="p", model="m", max_tokens=SENTINEL)

    # `transport.clean` normalises trailing whitespace; nothing structural
    # is trimmed, which is what "spliced whole" means here.
    assert out == raw.strip()
    assert out.count("<FN ") == raw.count("<FN ") == 21
    row = events["llm.followup_meta"][0]
    assert row["finish_reason"] == "stop" and row["salvaged_chars"] == 0


def test_a_think_capped_followup_counts_as_cut_in_the_fold() -> None:
    """The controller salvages on `length` OR `stop == "think_cap"`; a fold
    that keys on `length` alone undercounts every think-capped followup.
    Invisible while `PHASE_C_FOLLOWUP_THINK` is off (nothing arms the mid-
    stream cap), wrong the moment it is switched on — which is exactly when
    the reading matters. `stop == "closed"` is the StopDetector finishing a
    structure and is NOT a cut."""
    rows = [
        {"kind": "followup", "finish_reason": "stop", "stop": "think_cap",
         "salvaged_chars": 3000},
        {"kind": "followup", "finish_reason": "stop", "stop": "think_cap",
         "salvaged_chars": 0},
        {"kind": "followup", "finish_reason": "stop", "stop": "closed",
         "salvaged_chars": 0},
    ]
    # `summarize_followup_cut` keeps its narrower contract — a client-side
    # think-cap break is not a server budget, and sizing `request_max` is what
    # that count answers. The salvage fold covers both, because the controller
    # salvages both.
    assert summarize_followup_cut(rows) == 0
    assert summarize_followup_salvage(rows) == (2, 1)

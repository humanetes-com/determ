"""Fold of `llm.stream_meta` over one fill window.

Run `2932-cf516410` (2026-08-26): 5 of 9 fill replies ended because thinking
ran out (`finish_reason=length`; live `stop` is always empty and followup
coincides with length).
Nothing folded those events, so the constructor call that hit the cap at
67 376 reasoning chars was only visible by grepping the event log after
the fact. The count belongs beside the landed-change audit.
"""
from __future__ import annotations

from pipeline_core.domain.reasoning_cap import (
    collect_stream_meta,
    is_reasoning_capped,
    record_followup_meta,
    record_stream_meta,
    summarize_capped,
    summarize_followup_cut,
)

# 5 of 9 is the fold of the first nine of 2932's 21 iter-1 stream_meta
# rows. Live shape: finish_reason is `length` or `stop`, `stop` is always
# empty, followup coincides with length. This fixture is that shape
# (5 length + 4 clean stop), not a reconstruction of the nine payloads —
# those were not on disk when the test was written.
_2932_LIVE_SHAPE = [
    {"finish_reason": "length", "stop": "", "followup": True},
    {"finish_reason": "length", "stop": "", "followup": True},
    {"finish_reason": "length", "stop": "", "followup": True},
    {"finish_reason": "length", "stop": "", "followup": True},
    {"finish_reason": "length", "stop": "", "followup": True},
    {"finish_reason": "stop", "stop": "", "followup": False},
    {"finish_reason": "stop", "stop": "", "followup": False},
    {"finish_reason": "stop", "stop": "", "followup": False},
    {"finish_reason": "stop", "stop": "", "followup": False},
]


def test_five_of_nine_on_the_2932_shape() -> None:
    assert summarize_capped(_2932_LIVE_SHAPE) == (5, 9)
    assert is_reasoning_capped(_2932_LIVE_SHAPE[0])
    assert not is_reasoning_capped(_2932_LIVE_SHAPE[-1])


def test_all_stop_batch_is_zero_of_m() -> None:
    """NEGATIVE CONTROL: a batch that finished cleanly counts 0 of M, which
    the loop prints (M>0) rather than hiding. Walks stub the fill so M=0
    and print nothing."""
    rows = [{"finish_reason": "stop", "stop": "", "followup": False}] * 9
    assert summarize_capped(rows) == (0, 9)
    assert summarize_capped([]) == (0, 0)


def test_collect_stream_meta_is_windowed_and_silent_outside() -> None:
    record_stream_meta({"finish_reason": "length"})
    with collect_stream_meta() as rows:
        record_stream_meta({"finish_reason": "length"})
        record_stream_meta({"finish_reason": "stop"})
        assert summarize_capped(rows) == (1, 2)
    record_stream_meta({"finish_reason": "length"})
    with collect_stream_meta() as rows:
        assert rows == []


# --------------------------------------------------------------------------- #
# followup rows: a second kind in the same window, folded apart
# --------------------------------------------------------------------------- #
def test_followup_rows_fold_separately_and_never_inflate_the_reply_total() -> None:
    """A synthetic mix: 8 stream_meta rows (followup=True, main finish_reason
    length) plus 5 followup_meta rows that themselves ended at `length`.

    That 5 is NOT the 82157 record. Computed on 82157's llm.stream_meta:
    8 of 8 file-first replies were forced followups; only the constructor
    followup was 8192 chars; the other seven were 194–6038; and no followup
    finish_reason existed (why followup_meta ships). The fold under test is
    that tagged followup rows do not inflate summarize_capped's denominator
    — 8 of 8 calls, plus a separate cut count of 5. The 82157-shaped
    control (8 followups, no followup_meta) is
    `test_recorded_followups_without_followup_meta_count_as_zero_cuts`."""
    with collect_stream_meta() as rows:
        for _ in range(8):
            record_stream_meta({"finish_reason": "length", "stop": None, "followup": True})
        for fr in ["length"] * 5 + ["stop"] * 3:
            record_followup_meta({"finish_reason": fr, "content_chars": 8192})
        assert summarize_capped(rows) == (8, 8)
        assert summarize_followup_cut(rows) == 5
        assert all(r["kind"] == "followup" for r in rows[8:])
        assert all("kind" not in r for r in rows[:8])


def test_recorded_followups_without_followup_meta_count_as_zero_cuts() -> None:
    """The 82157 shape that actually sat on disk: 8 of 8 stream_meta rows
    followup=True, and no followup_meta event at all. summarize_followup_cut
    is 0 — the cut count was unrecordable, which is why the event ships.
    summarize_capped still reads 8 of 8 (one row per CALL)."""
    with collect_stream_meta() as rows:
        for _ in range(8):
            record_stream_meta({"finish_reason": "length", "stop": None, "followup": True})
        assert summarize_capped(rows) == (8, 8)
        assert summarize_followup_cut(rows) == 0


def test_an_untagged_followup_row_would_inflate_the_total() -> None:
    """NEGATIVE CONTROL: the same 13 payloads with no `kind` tag read 13 of
    13, and the followup fold sees none of them — the tag is what keeps the
    two folds apart, and `record_followup_meta` is where it is applied."""
    untagged = ([{"finish_reason": "length", "stop": None, "followup": True}] * 8
                + [{"finish_reason": "length", "content_chars": 8192}] * 5)
    assert summarize_capped(untagged) == (13, 13)
    assert summarize_followup_cut(untagged) == 0


def test_followup_cut_ignores_stream_rows_and_clean_followups() -> None:
    rows = [
        {"finish_reason": "length", "stop": None, "followup": True},           # stream, capped
        {"kind": "followup", "finish_reason": "stop"},                          # followup, clean
        {"kind": "followup", "finish_reason": None, "stop": "think_cap"},       # followup, client break
    ]
    assert summarize_followup_cut(rows) == 0
    assert summarize_capped(rows) == (1, 1)
    assert summarize_followup_cut([]) == 0


# --------------------------------------------------------------------------- #
# the per-unit join: which units' EVERY reply was capped
# --------------------------------------------------------------------------- #
def test_serving_units_stamps_rows_and_the_fold_reads_them() -> None:
    """Run `358833-aa15d93c` iter 3 (2026-09-01) wrote 8 FLAT rows in a batch
    whose own audit said `3 of 4 fill replies reasoning-capped` — the count
    and the grades shared a window with no join. `serving_units` is the join:
    rows recorded inside carry the units the call serves, and
    `units_all_replies_capped` names the units every one of whose joined
    replies was truncated."""
    from pipeline_core.domain.reasoning_cap import (
        serving_units,
        units_all_replies_capped,
    )
    with collect_stream_meta() as rows:
        with serving_units(("src/a.rs::width", "src/a.rs::depth")):
            record_stream_meta({"finish_reason": "length", "stop": "",
                                "followup": True})
            record_followup_meta({"finish_reason": "length"})
        with serving_units(("src/a.rs::depth",)):
            record_stream_meta({"finish_reason": "stop", "stop": "",
                                "followup": False})
        # An unjoined call (a retrospective) counts toward no unit.
        record_stream_meta({"finish_reason": "length", "stop": "",
                            "followup": True})
    assert rows[0]["units"] == ("src/a.rs::width", "src/a.rs::depth")
    assert "units" not in rows[-1]
    # width: 1 reply, capped -> in. depth: 2 replies, one clean -> out.
    # The followup row is a second completion of the same call: no vote.
    assert units_all_replies_capped(rows) == frozenset({"src/a.rs::width"})


def test_units_fold_negative_controls() -> None:
    from pipeline_core.domain.reasoning_cap import units_all_replies_capped
    # An uncapped joined reply keeps the unit out (a measured refusal).
    rows = [{"finish_reason": "stop", "units": ("src/a.rs::f",)}]
    assert units_all_replies_capped(rows) == frozenset()
    # No joined rows at all: empty, so the FLAT arm is untouched.
    assert units_all_replies_capped(
        [{"finish_reason": "length"}]) == frozenset()
    assert units_all_replies_capped([]) == frozenset()

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
    record_stream_meta,
    summarize_capped,
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

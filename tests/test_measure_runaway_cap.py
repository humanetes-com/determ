"""The runaway-cap measurement re-asks the request a route actually sent and
proposes a cap only from healthy finishes."""

from __future__ import annotations

from pipeline_core.domain.text import turns_digest_text
from scripts.measure_runaway_cap import proposal, transcript_messages


def test_a_stored_transcript_is_the_turn_list_it_was_digested_from():
    msgs = [{"role": "system", "content": "disclaimer\n\nsys"},
            {"role": "user", "content": "files\n<<not a role>>\nask"}]
    assert transcript_messages(turns_digest_text(msgs)) == msgs


def test_cap_is_the_healthy_maximum_plus_a_quarter():
    rows = [{"finish": "stop", "content_chars": 9, "reasoning_chars": 100_000},
            {"finish": "stop", "content_chars": 9, "reasoning_chars": 40_000},
            {"finish": "length", "content_chars": 0, "reasoning_chars": 380_000}]
    assert proposal(rows)[0] == 125_000


def test_a_sample_mostly_ending_on_the_context_proposes_no_cap():
    rows = [{"finish": "length", "content_chars": 0, "reasoning_chars": 380_000},
            {"finish": "stop", "content_chars": 9, "reasoning_chars": 40_000}]
    assert proposal(rows)[0] is None
    # A healthy call with no answer is not healthy.
    assert proposal([{"finish": "stop", "content_chars": 0, "reasoning_chars": 10}])[0] is None

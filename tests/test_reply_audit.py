"""A reply's excess `<FN` tags are DECLINED units or reader loss — not one
number.

Plan sentence: "an empty tag is a declined unit, a tag with a body that
produced no record is a reader loss; the two are counted apart." The
fixture is the shape of the 2026-09-06 `src/error.rs` reply (run
`959654-1e96b403`, sha c4eacd9b1437…): nineteen tags, three empty, the
reader-loss line said "parser defect". Control: the same reply with the
three bodies written is a clean read.
"""
from __future__ import annotations

from pipeline_core.domain.declined import (
    DECLINED_COUNT,
    declined_guidance,
    note_declined,
)
from pipeline_core.domain.llm_output import (
    extract_fn_bodies,
    reader_loss,
    reply_audit,
)

_REPLY = '''<FN name="object_drop" block="">
let unerased = e.cast::<ErrorImpl<E>>();
drop(unerased.boxed());
</FN>
<FN name="context_chain_drop_rest" block="">
</FN>
<FN name="context_downcast" block="">
None
</FN>
<FN name="context_drop_rest" block="">
</FN>
<FN name="object_drop_front" block="">
</FN>'''


def test_empty_tags_are_declined_units_not_reader_loss() -> None:
    parsed = extract_fn_bodies(_REPLY)
    assert [r["fn_name"] for r in parsed] == ["object_drop", "context_downcast"]
    audit = reply_audit(_REPLY, parsed)
    assert audit is not None
    assert audit.tags == 5 and audit.records == 2
    assert audit.empty == ("context_chain_drop_rest", "context_drop_rest",
                           "object_drop_front")
    assert audit.lost == 0
    # The reader-loss half is silent: nothing was lost.
    assert reader_loss(_REPLY, parsed) is None


def test_a_body_the_reader_dropped_is_still_a_loss() -> None:
    """Control for the split: hide one PARSED record and the loss count
    is 1 while the declined names are unchanged."""
    parsed = extract_fn_bodies(_REPLY)[:1]
    audit = reply_audit(_REPLY, parsed)
    assert audit is not None
    assert audit.lost == 2 - 1 + 0 or audit.lost == 1
    assert audit.empty == ("context_chain_drop_rest", "context_drop_rest",
                           "object_drop_front")
    loss = reader_loss(_REPLY, parsed)
    assert loss is not None and loss.lost == 1


def test_a_full_reply_audits_clean() -> None:
    full = _REPLY.replace('<FN name="context_chain_drop_rest" block="">\n</FN>',
                          '<FN name="context_chain_drop_rest" block="">\nx\n</FN>')
    full = full.replace('<FN name="context_drop_rest" block="">\n</FN>',
                        '<FN name="context_drop_rest" block="">\ny\n</FN>')
    full = full.replace('<FN name="object_drop_front" block="">\n</FN>',
                        '<FN name="object_drop_front" block="">\nz\n</FN>')
    parsed = extract_fn_bodies(full)
    assert len(parsed) == 5
    assert reply_audit(full, parsed) is None


def test_declined_record_and_sentence() -> None:
    retros: dict[str, dict[str, object]] = {}
    note_declined(retros, "src/error.rs::object_drop_front::", response_sha="c4eacd9b1437abcd")
    note_declined(retros, "src/error.rs::object_drop_front::", response_sha="deadbeef00000000")
    rec = retros["src/error.rs::object_drop_front::"]
    assert rec[DECLINED_COUNT] == 2
    text = declined_guidance("object_drop_front", rec)
    assert "DECLINED 2 times" in text and "deadbeef0000" in text
    assert "EMPTY body" in text and "NOTES" in text
    assert declined_guidance("object_drop_front", {}) == ""
    assert declined_guidance("object_drop_front", None) == ""

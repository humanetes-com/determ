"""A DECLINED unit: the model answered it with an empty body.

Run `959654-1e96b403` (2026-09-06, cluster `stable`, iter 1): the file-first
reply for `src/error.rs` carried 19 `<FN>` tags; three were empty
(`context_chain_drop_rest`, `context_drop_rest`, `object_drop_front`), the
thinking read "implement drop_rest as no-op", the reader dropped the empty
bodies by design, and the only record was a reader-loss line asserting "a
parser defect, NOT a model failure". Those three were the stubs that held
25 of 34 leftover tests hollow. Nothing re-asked, nothing told the next
prompt, and the per-fn route re-opened the units cold.

This module is the record and the sentence. `note_declined` stamps the
unit's retrospective record (the store every fill route reads before it
prompts); `declined_guidance` renders the one instruction the next prompt
for that unit must carry. Generic: the names come from the reply, never
from code, and the sentence names no crate.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

__all__ = ["DECLINED_COUNT", "DECLINED_SHA", "declined_guidance", "note_declined"]

DECLINED_COUNT = "declined_count"
DECLINED_SHA = "declined_response_sha"
DECLINED_RUN = "declined_run_id"


def note_declined(
    retros: MutableMapping[str, Any],
    key: str,
    *,
    response_sha: str,
    run_id: str = "",
) -> dict[str, Any]:
    """Stamp `retros[key]` with one more decline. Returns the record."""
    rec = dict(retros.get(key, {}) or {})
    rec[DECLINED_COUNT] = int(rec.get(DECLINED_COUNT, 0) or 0) + 1
    rec[DECLINED_SHA] = response_sha
    if run_id:
        rec[DECLINED_RUN] = run_id
    retros[key] = rec
    return rec


def declined_guidance(fn_name: str, rec: Mapping[str, Any] | None) -> str:
    """The instruction for a unit whose previous reply was an empty body;
    "" when the record carries no decline."""
    if not rec:
        return ""
    n = int(rec.get(DECLINED_COUNT, 0) or 0)
    if n <= 0:
        return ""
    times = "once" if n == 1 else f"{n} times"
    sha = str(rec.get(DECLINED_SHA, "") or "")[:12]
    ref = f" (reply {sha}…)" if sha else ""
    return (
        f"`{fn_name}` — DECLINED {times}{ref}: your previous reply for this fn "
        "carried an EMPTY body. An empty body is not an answer and is never "
        "spliced; the fn stays a stub and every test that reaches it stays "
        "unjudged. Write the body. If something the prompt does not show is "
        "needed to write it, say exactly what in NOTES instead of returning "
        "nothing.")


def declined_guidance_for(
    fn_names: Iterable[str],
    records: Mapping[str, Mapping[str, Any] | None],
) -> str:
    """The declined sentences for several units, joined; `records` is keyed
    by fn name."""
    parts = [declined_guidance(n, records.get(n)) for n in fn_names]
    return "\n".join(p for p in parts if p)

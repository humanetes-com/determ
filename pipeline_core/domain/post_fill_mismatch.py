"""Which stubs an accepted file reply left on disk without saying so.

A splice can report ok while the function stays `unimplemented!()` on disk
("FILE FILLED 5" with one fn still a stub). That silent loss is what the
file route's re-scan after an accept exists to name (`POST-FILL MISMATCH`,
`phase_c.file.fill.mismatch`). A stub the route put back or refused on
purpose, with its own log line and verdict row, is not a silent loss: the
peel's reverts, and a body refused for its own text or declined for
ignoring its inputs. Those are owed the retry the route runs next.

Run `1804897-1ad432ba` iter 4 (2026-10-20): the ignored-inputs gate declined
seven `src/error.rs` bodies, and the next line printed the same seven as a
mismatch, so the monitor counted a decision as a splice loss.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping


def still_stubbed_after_accept(
    stub_records: Iterable[Mapping[str, object]],
    *,
    file_rel: str,
    asked: Collection[str],
    put_back: Collection[str],
    norm: Callable[[str], str],
) -> list[str]:
    """The asked stubs of `file_rel` still on disk that the route did not
    put back or refuse on purpose (`put_back`), sorted. ``stub_records``
    are the extractor's stub rows (``file``, ``fn_name``); ``norm`` maps a
    file path to the form the route compares in."""
    want = set(asked) - set(put_back)
    target = norm(file_rel)
    return sorted({
        str(r.get("fn_name", "")) for r in stub_records
        if norm(str(r.get("file", ""))) == target
        and str(r.get("fn_name", "")) in want})


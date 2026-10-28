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

The comparison is per body, never per name. A file holds several bodies of
one name (`new` in three impl blocks, `fmt` for two traits); matched by name,
a stubbed homonym the reply was never asked for read as a lost answer of the
one it was. Both sides are :class:`BodyKey`, so a bare name cannot be passed.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from typing import NamedTuple

from pipeline_core.domain.attribution import container_key
from pipeline_core.domain.ignored_inputs import identity_container, identity_name


class BodyKey(NamedTuple):
    """One body of a file: its name and its container, normalised as the
    router compares containers (`container_key`); "" for a free fn."""

    fn_name: str
    container: str

    @classmethod
    def of(cls, fn_name: str, container: str) -> BodyKey:
        return cls(fn_name, container_key(container) if container else "")

    def render(self) -> str:
        return f"{self.fn_name} [{self.container}]" if self.container else self.fn_name


def bodies_named(names: Iterable[str], asked: Collection[BodyKey]) -> frozenset[BodyKey]:
    """The asked bodies a name-keyed decision covers. A gate that keys by
    name (the peel's reverts, a body identity with no container) decided
    every body of that name it was handed; one that told homonyms apart
    passes :class:`BodyKey` instead."""
    want = set(names)
    return frozenset(b for b in asked if b.fn_name in want)


def put_back_bodies(peeled: Collection[BodyKey], refused: Iterable[str],
                    asked: Collection[BodyKey]) -> frozenset[BodyKey]:
    """The asked bodies the route put back on purpose: the bodies the peel
    dropped (it drops by body, so these are bodies), and the bodies refused
    for their own text or declined (`ignored_inputs.body_identity`: by name,
    or by name and container when the keep told homonyms apart). A bare name
    names the one asked body of that name; when the asked set holds two or
    more, it names none of them."""
    out = set(peeled)
    by_name: dict[str, list[BodyKey]] = {}
    for body in asked:
        by_name.setdefault(body.fn_name, []).append(body)
    for ident in refused:
        container = identity_container(ident)
        if container:
            out.add(BodyKey(identity_name(ident), container))
            continue
        named = by_name.get(ident, [])
        # One asked body of that name is the body the bare identity names.
        # Two or more: the refusal did not say which, and putting every
        # one back hides a stubbed homonym the reply never answered.
        if len(named) == 1:
            out.add(named[0])
    return frozenset(out)


def still_stubbed_after_accept(
    stub_records: Iterable[Mapping[str, object]],
    *,
    file_rel: str,
    asked: Collection[BodyKey],
    put_back: Collection[BodyKey],
    norm: Callable[[str], str],
) -> list[str]:
    """The asked bodies of `file_rel` still stubbed on disk that the route
    did not put back or refuse on purpose (`put_back`), rendered and sorted.
    ``stub_records`` are the extractor's stub rows (``file``, ``fn_name``,
    ``container``); ``norm`` maps a file path to the form the route compares
    in."""
    want = set(asked) - set(put_back)
    target = norm(file_rel)
    return sorted({
        key.render() for r in stub_records
        if norm(str(r.get("file", ""))) == target
        and (key := BodyKey.of(str(r.get("fn_name", "")),
                               str(r.get("container", "") or ""))) in want})

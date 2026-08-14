"""Which fns PRODUCE a type — the futility-escalation question, as domain rules.

When every fn attributed to a failing test has been rewritten to the same dead
end, the frontier escalates to "whoever produces the type those accessors read
from". That escalation asked its question two ways that cannot be answered, and
both returned a near-total frontier carrying no attribution signal.

**The name must be one this crate can be said to produce.** The escalation's
subject came from the futile unit's impl container Self-type, unfiltered. On the
live `anyhow` tree that yields ``E`` (from ``impl<E> StdError for E``),
``Option``, ``Result``, ``Box`` and ``bool`` alongside the real ones. None of
those is answerable:

* a generic PARAMETER names no type at all — every concrete type substitutes for
  it, so "producers of ``E``" is "the crate". The extractor drops an impl's
  generic list from ``container`` (``impl<E> StdError for E`` arrives as
  ``impl StdError for E``), so the container text alone cannot tell a parameter
  from a type; the crate's own type declarations can.
* a FOREIGN type (``Option``, ``Box``, ``bool``) is produced everywhere and by
  nothing in particular.
* a type ALIAS (anyhow's ``Result``, ``StdError``) is a spelling, not something
  constructed. Its "producers" are every fallible fn in the crate.

**And the match must be on a token, not a substring.** ``tn in ret`` made
``E`` match ``-> Result<T, Error>``, and ``f"{tn} {{" in body`` made ``Error``
match ``MyError { .. }``. Measured on the live tree at 2026-08-01, before this
module: ``Error`` -> 40 producers of 186 fns (22 % of the crate), ``E`` -> 33,
``Result`` -> 18, ``Option`` -> 16, ``Ref`` -> 10.

Both rules narrow; neither can widen. An escalation that names nothing is a
correct answer here — the frontier keeps the units it already had.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pipeline_core.domain.rust_idents import identifier_tokens

# A struct or an enum is a nominal type: it is declared here and it is
# constructed. A `trait` is implemented, not produced; a `type` alias is a
# spelling of something else. Both of the latter answer "who produces this?"
# with the whole crate.
NOMINAL_KINDS = frozenset({"struct", "enum"})


@dataclass(frozen=True, slots=True)
class ProducibleTypes:
    """The nominal type names this crate declares.

    The set an escalation subject must belong to for the escalation to carry any
    signal. Deliberately a value object rather than a bare ``frozenset``: the
    membership test is a domain claim ("this name denotes something a fn in this
    crate can hand back"), and `EMPTY` gives callers an explicit, non-widening
    answer for the case where the crate's types could not be read at all.
    """

    names: frozenset[str]

    @classmethod
    def from_type_records(cls, records: Iterable[Any] | None) -> ProducibleTypes:
        """Build from `StructureReader.enumerate_types` output.

        Tolerates both the typed `TypeRecord` and its wire dict, and a `None`
        enumeration (extractor unavailable) — which yields `EMPTY`, i.e. no
        escalation rather than an unfiltered one."""
        if records is None:
            return EMPTY
        names: set[str] = set()
        for rec in records:
            kind = _field(rec, "kind")
            name = _field(rec, "name")
            if name and kind in NOMINAL_KINDS:
                names.add(name)
        return cls(frozenset(names))

    def __contains__(self, name: object) -> bool:
        return name in self.names

    def __bool__(self) -> bool:
        return bool(self.names)

    def filter(self, candidates: Iterable[str]) -> frozenset[str]:
        """The candidates this crate actually declares as nominal types."""
        return frozenset(n for n in candidates if n in self.names)


EMPTY = ProducibleTypes(frozenset())


def _field(rec: Any, attr: str) -> str:
    value = rec.get(attr, "") if isinstance(rec, dict) else getattr(rec, attr, "")
    return str(value or "")


def return_type_text(sig_text: str) -> str:
    """The return clause of a signature — everything after the first ``->``.

    Empty for a unit-returning fn. A `where` clause trailing the return type is
    left in: it is part of what the fn hands back being spelled out, and every
    consumer here matches tokens rather than parsing the type."""
    return sig_text.split("->", 1)[1] if "->" in sig_text else ""


def returns_type(sig_text: str, type_name: str, *, own_type: str = "") -> bool:
    """True iff the signature hands back `type_name`.

    Two ways to say it: the return clause names the type, or it says ``Self``
    from that type's own impl. Both are token tests — ``E`` must not match
    ``Error``, and ``Self`` must not match a type called ``SelfRef``."""
    if not type_name:
        return False
    tokens = identifier_tokens(return_type_text(sig_text))
    if type_name in tokens:
        return True
    return bool(own_type) and own_type == type_name and "Self" in tokens


def constructs_type(body_text: str, type_name: str) -> bool:
    """True iff the body contains a struct-literal construction of `type_name`.

    ``Error { .. }`` counts; ``MyError { .. }`` must not, which the old
    ``f"{name} {{" in body`` substring test could not express. Whitespace
    between the name and the brace is allowed — the extractor's bodies are
    re-emitted by a pretty-printer that chooses its own spacing."""
    if not type_name:
        return False
    pos = body_text.find(type_name)
    while pos != -1:
        end = pos + len(type_name)
        before_ok = pos == 0 or not _is_ident_char(body_text[pos - 1])
        rest = body_text[end:].lstrip()
        if before_ok and rest.startswith("{"):
            return True
        pos = body_text.find(type_name, pos + 1)
    return False


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def called_method_names(body_text: str) -> frozenset[str]:
    """The bare names of METHODS the body calls on a value — the `foo` of
    `x.foo(`. Token scan, same discipline as `constructs_type`: a name is only
    a call when an identifier ends at an open paren and is preceded by a dot.

    Field accesses (`self.ptr`), paths (`Type::foo()`) and free calls (`foo()`)
    are all excluded on purpose. The first two are not dispatch, and the third
    names a fn the caller already reaches by ordinary reachability."""
    out: set[str] = set()
    i = 0
    n = len(body_text)
    while True:
        dot = body_text.find(".", i)
        if dot == -1 or dot + 1 >= n:
            break
        i = dot + 1
        # `..` (range) and `.0` (tuple index) are not method names.
        j = i
        while j < n and _is_ident_char(body_text[j]):
            j += 1
        name = body_text[i:j]
        if not name or name[0].isdigit():
            continue
        rest = body_text[j:].lstrip()
        # A turbofish still calls the method: `.parse::<u32>()`.
        if rest.startswith("::<"):
            close = rest.find(">")
            if close != -1:
                rest = rest[close + 1:].lstrip()
        if rest.startswith("("):
            out.add(name)
    return frozenset(out)


def trait_impl_fns_named(
    fn_records: Iterable[Any], names: Iterable[str],
) -> list[tuple[str, str]]:
    """(file, fn_name) of every fn in `fn_records` that implements one of
    `names` inside a TRAIT impl (`impl Trait for Type`).

    The traversal half of producer escalation. A chain iterator does not
    produce the types it walks and it does not call their constructors: it
    calls one method on each link and follows the result. `producers_for_types`
    can never name that method's impls — they neither return nor construct the
    walked type — so a defect in one of them is invisible to the escalation
    that is supposed to explain why the walk's accessors are futile.

    Measured (`anyhow`, cluster `stable`, 2026-08-14): the four chain tests
    escalated to `chain.rs::new`, `chain.rs::default`, `error.rs::construct`,
    `error.rs::chain` and `error.rs::erase` on every run, while the fn that
    actually severed the chain — the `source` of
    `impl<C> StdError for ContextError<C, Error>`, returning `None` — was in
    no escalated set on either run.

    Restricted to TRAIT impls because that is what a walked link dispatches
    through; an inherent method of the same name is reached by ordinary
    reachability and would widen the set for nothing."""
    wanted = {n for n in names if n}
    if not wanted:
        return []
    out: list[tuple[str, str]] = []
    for r in fn_records:
        fn_name = _field(r, "fn_name")
        if fn_name not in wanted:
            continue
        container = _field(r, "container")
        if not container.strip().startswith("impl") or " for " not in container:
            continue
        file = _field(r, "file")
        if file and (file, fn_name) not in out:
            out.append((file, fn_name))
    return out

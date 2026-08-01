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

"""Which traits the crate's own tests require a layout type to derive.

The layout-authoring prompt asks the model to design a private type, and the
frozen tests are the specification it has to satisfy — but the tests are not IN
that prompt. So a trait the tests reach for has to be INFERRED, and the failure
when the inference misses is out of all proportion to it:
`tests/integration.rs` calls `Counter::default()`, the authored struct carries
no `#[derive(Default)]`, and the whole cluster stops compiling with
`error[E0599]: no function or associated item named 'default' found for struct
'Counter'`. No test binary is produced, so NO test reports a verdict and the
sweep scores 0 out of the entire oracle — a one-line omission reads as total
failure, and the DESIGN_CONFLICT retrigger then spends sweeps re-authoring.

Measured on 2026-08-11 across eight authorings (microcrate and hardcrate,
Qwen3.6-27B and Muse-Glimmer-30B, context 32768 and 131072, reasoning strengths
high and xhigh): the inference came out right 3 times and wrong 5. Adding a
general rule to the prompt ("derive traits the tests call") fixed the microcrate
in 2 of 2 runs and did NOT fix the hardcrate, whose component enumerates ~100
items — one paragraph of principle does not survive that much competition.

So this computes the requirement instead of asking for it. The tests are on
disk and already read by this phase (`AuthorLayoutDeps.oracle_sources`, the
write-protected `gen_tests` siblings), and a `Type::method()` call names its
type. Stating "Counter must derive Default, because tests/integration.rs:22
calls Counter::default()" turns a deduction across 100 items into a fact about
one, which is the same move `oracle_field_paths` makes for member paths.

Scope is deliberately narrow: only call forms that name their own type are
scanned. `x.clone()` needs to know what `x` is and is not attempted — a wrong
requirement here would push a derive onto a type that cannot carry it (a struct
holding a raw pointer cannot `#[derive(Clone)]` for free), which is worse than
a missing one because it fails the same way and blames the wrong place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

__all__ = ["RequiredDerive", "scan_required_derives", "derive_requirements_text"]


@dataclass(frozen=True, slots=True)
class RequiredDerive:
    """One trait a test requires of `type_name`, and the call that proves it."""

    type_name: str
    trait: str
    evidence: str = ""          # "Counter::default()"
    origin: str = ""            # "tests/integration.rs:22"


# `Type::method(` — an associated-function call that NAMES its receiver type, so
# attribution needs no type inference. Only these forms are trusted.
_ASSOC_CALL = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\s*::\s*([a-z_][a-z0-9_]*)\s*\(")

# The associated fn a derive provides -> the derive that provides it. `new` is
# deliberately absent: it is an inherent constructor the layout author writes,
# not a derived trait, and requiring a derive for it would be wrong.
_ASSOC_TO_DERIVE = {
    "default": "Default",
    "clone": "Clone",
}

# A type named inside an equality assertion must compare and must print: the
# macro needs `PartialEq` for the comparison and `Debug` for the panic message.
_EQ_ASSERT = re.compile(r"\bassert_(?:eq|ne)!\s*\((.*?)\)\s*;", re.S)
_TYPE_TOKEN = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def scan_required_derives(
    sources: Mapping[str, str], type_names: Iterable[str],
) -> tuple[RequiredDerive, ...]:
    """Traits the `sources` demand of each name in `type_names`.

    Deduped on (type, trait), keeping the first citation — one concrete call is
    enough to justify the requirement and a list of twenty is noise in a prompt.
    """
    wanted = {n for n in type_names if n}
    if not wanted:
        return ()
    found: dict[tuple[str, str], RequiredDerive] = {}

    def _add(ty: str, trait: str, evidence: str, origin: str) -> None:
        if ty in wanted:
            found.setdefault((ty, trait),
                             RequiredDerive(ty, trait, evidence, origin))

    for path, src in sorted(sources.items()):
        for m in _ASSOC_CALL.finditer(src):
            ty, fn = m.group(1), m.group(2)
            trait = _ASSOC_TO_DERIVE.get(fn)
            if trait:
                _add(ty, trait, f"{ty}::{fn}()",
                     f"{path}:{_line_of(src, m.start())}")
        for m in _EQ_ASSERT.finditer(src):
            args, origin = m.group(1), f"{path}:{_line_of(src, m.start())}"
            for ty in set(_TYPE_TOKEN.findall(args)):
                _add(ty, "PartialEq", "compared with assert_eq!/assert_ne!", origin)
                _add(ty, "Debug", "printed by a failing assert_eq!/assert_ne!", origin)
    return tuple(sorted(found.values(), key=lambda r: (r.type_name, r.trait)))


def derive_requirements_text(reqs: Iterable[RequiredDerive]) -> str:
    """The requirements as an authoring instruction, or "" when there are none.

    Grouped per type, because that is the unit the model emits: one
    `<LAYOUT_ITEM>` block carries one definition and therefore one derive list.
    """
    by_type: dict[str, list[RequiredDerive]] = {}
    for r in reqs:
        by_type.setdefault(r.type_name, []).append(r)
    if not by_type:
        return ""
    rows = []
    for ty in sorted(by_type):
        rs = by_type[ty]
        traits = ", ".join(sorted({r.trait for r in rs}))
        why = "; ".join(f"{r.evidence} at {r.origin}" if r.origin else r.evidence
                        for r in rs[:3])
        rows.append(f"  - `{ty}` must derive: {traits}\n      because {why}")
    return ("\n\nDERIVES THE CRATE'S OWN TESTS REQUIRE — read off those tests, "
            "which are WRITE-PROTECTED and which no later stage may edit. A "
            "missing derive here does not fail gracefully: the cluster stops "
            "compiling, no test binary is produced, and EVERY test scores as "
            "failing regardless of the bodies.\n" + "\n".join(rows)
            + "\nPut these on the item's `#[derive(...)]`. Add any others the "
            "type can support; these are the minimum.\n")

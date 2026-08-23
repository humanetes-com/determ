#!/usr/bin/env python3
"""How much of a crate's PRIVATE layout is already dictated by the frozen tests?

WHY THIS EXISTS
---------------
t1.5 tells the model, in `prompts/layout_authoring.py:_RULES`, that it is "FREE
to choose any representation — you are NOT reproducing the original library's
internals". That freedom is the reason the phase exists: the reconstruction
should not have to re-derive anyhow's hand-rolled vtable.

The freedom is not actually total, and nothing measures how partial it is. The
crate's `gen_tests` siblings were written at task0c against the ORIGINAL source,
where the private layout was visible, and they are write-protected from then on
(`domain/oracle_immutability`). Every private name such a test reads is a name
the authored layout MUST provide, or the cluster stops compiling with a
diagnostic no later stage is allowed to fix.

That has happened. Run `1171697-3838c847` (2026-08-06 17:27 UTC): the authored
`Error` was `#[repr(transparent)] pub struct Error { ptr: NonNull<ErrorImpl> }`,
it satisfied all 6 harvested pins and compiled, and `src/error.gen_tests_3.rs:29`
reads `err.inner.ptr.as_ptr()`. `cargo check --lib` never builds `#[cfg(test)]`
code, so the gate saw nothing; the first `cargo test` lost all 56 unit tests —
44 % of a 126-test oracle — and the cluster stopped, correctly, because the only
diagnostic pointed into content the fill loop may not rewrite. A second instance,
2026-08-11 on the hardcrate: `Counter::default()` in `tests/integration.rs:22`
needs `#[derive(Default)]`, the prompt never said so, and the guess came out
wrong in 5 of 8 authorings across two models and two reasoning strengths.

Each instance was answered with its own extractor — `domain/oracle_field_paths`
for member paths, `domain/oracle_derives` for derives, plus the autotrait and
derived-trait rules in the prompt. Each was paid for with a dead cluster. What
nobody has is the denominator: of the private items t1.5 is asked to invent, how
many does the frozen oracle already name, and how many of those does the gate
actually decide. Without that number, "author this component" is a coin flip
whose odds are unknown, and the answer to each new incident is a sixth extractor.

This script computes it. It runs no cargo, calls no model, writes nothing.

USAGE
-----
    .venv/bin/python scripts/measure_oracle_pinning.py [--project tmp/anyhow]
                                                       [--cluster stable]
                                                       [--detail] [--max-rows N]

  --project    workspace root holding `.skeleton/` (default `tmp/anyhow`)
  --cluster    one cfg-cluster; default every cluster with a baked surface
  --detail     also list every ungated constraint, with file:line evidence
  --max-rows   cap the detail listing (default 40; 0 = no cap)

Preconditions: the bake and t1.5 must have run for the project, so that
`.skeleton/_private_surface_<cluster>.json` exists. `.skeleton/_layout_outcomes_
<cluster>.json` is optional — when present the report shows what t1.5 actually
decided for each component. No GPU, no vLLM, no lock on a live run: safe to run
while the pipeline is working.

HOW TO INTERPRET THE OUTPUT
---------------------------
One row per connected component — the unit t1.5 authors in a single model turn.

  items      private-surface items in the component (what the model must emit)
  pinned     items the frozen oracle names at least once, outside comments
  gated      pinned items whose constraint the t1.5 compile gate can DECIDE
             today, i.e. a member path that `build_field_path_probe` renders
             into a lib-target item. Renderable, not calibrated: the live gate
             additionally compiles the probe against the frozen scaffold and
             drops it if the diagnostic says the probe is wrong about itself,
             so `gated` is an upper bound on what really gates.
  ungated    pinned items whose constraint reaches the model as prompt text at
             best, and as nothing at all at worst. This is the live exposure:
             an authored layout may violate any of them and still be accepted.
  free       items the oracle never names — where the phase's premise holds and
             a model call buys real design freedom.
  outcome    what t1.5 recorded for this component (AUTHORED / GATED / -).

THE DECISION RULE
-----------------
Two readings come out of this, and they point at different work.

1. PER COMPONENT — should it be authored at all? The frozen layout always
   satisfies the oracle by construction: the tests were written against it. So
   freezing a component is always a valid answer, and it is the CORRECT answer
   for a component the oracle has already specified — authoring there spends a
   model call to re-derive names the tests dictate, and risks the cluster's
   build to do it. Read `free`: a component with a high `free` count has design
   room worth a call; a component whose `free` is 0 or near it has none, and
   `AUTHORED` on such a row is the pipeline paying full price for nothing.
   Set the freeze threshold from the observed distribution, not from a guess —
   the summary prints the per-component `free` values so the split is visible.

2. CRATE-WIDE — is the gate keeping up? Read the `ungated` total. Every unit in
   it is a way the 2026-08-06 failure can recur under a different diagnostic:
   an associated fn the layout renames, an enum variant it drops, a trait it
   omits. If `ungated` is small and static, extending the extractor family is a
   reasonable answer. If it is large, the extractor-per-incident approach is
   losing, and the structural fix is to let the gate compile the oracle itself
   (stage `tests/` plus the siblings into `.skeleton/<cluster>/` and run
   `cargo check --tests` instead of `cargo check --lib --quiet`), which decides
   the whole class at once — fields, calls, variants, derives, arity,
   visibility — and needs no new scanner per failure mode.

WHAT THIS DOES NOT MEASURE, AND WHAT IT DELIBERATELY DISCARDS
------------------------------------------------------------
Only what a token scan can see. A test that reaches a private member through a
helper, a macro, or a binding whose type is inferred is invisible here, so
`pinned` is a LOWER bound and `free` an upper one. Doc-tests are out of scope
(the crate's `///` examples exercise the public API). And `gated` counts probes
that RENDER, not probes that survive the live gate's calibration compile.

Six filters drop occurrences that are not constraints. Each was added because
the unfiltered run reported it as one, and an overstated `pinned` argues for
freezing a component that has real design room — the more expensive error here:

  - string and char literal contents (`write!(f, "source error")` is not a
    reference to the private fn `error`), lifetimes excepted;
  - names a file imports from `std`/`core`/`alloc` (`use std::backtrace::
    Backtrace;` makes `Backtrace::force_capture()` std's, not the crate's);
  - names bound by `let` in the same file (`let error = Error::from_boxed(..)`
    then `error.to_string()`);
  - `use` lines themselves — an import binds a name, it does not constrain a
    layout;
  - every reference to a PUB_FIELDS item's NAME — its declaration head is
    frozen API the model re-emits verbatim, so only its member paths, its
    struct literals and its derives constrain what it is free to choose;
  - the crate's own `tests/` for private names — an integration test cannot
    reach a private item, so `trait Trait: Sized {` there is a homonym. The
    derive scan still reads the whole write-protected set, because a derive
    requirement is about a PUBLIC type and `Chain::default()` really is in
    `tests/test_chain.rs:54`.

Receiver calls (`err.backtrace()`) are listed under NOT COUNTED rather than
filtered away: they are a constraint exactly when the receiver is the private
item of that name, and this scan cannot decide which.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline_core.adapters.gen_tests_storage import (  # noqa: E402
    read_frozen_test_sources,
    read_oracle_sibling_sources,
)
from pipeline_core.adapters.private_surface_store import (  # noqa: E402
    load_private_surface,
)
from pipeline_core.domain.layout_probe import build_field_path_probe  # noqa: E402
from pipeline_core.domain.oracle_derives import scan_required_derives  # noqa: E402
from pipeline_core.domain.oracle_field_paths import (  # noqa: E402
    FieldPath,
    read_field_paths,
)

# The pipeline's own comment stripper, imported rather than re-implemented: a
# doc comment naming a field must not count as a read, and this measurement is
# worthless if it reads the oracle differently from the phase it is measuring.
# `error.gen_tests_5.rs:32` mentions `error_mut` in prose only — a local copy
# that drifted would report an exposure the pipeline does not have. If the name
# moves, this import fails loudly; follow it rather than forking the logic.
from pipeline_core.domain.oracle_field_paths import (  # noqa: E402
    _strip_line_comment as strip_line_comment,
)
from pipeline_core.domain.private_surface import (  # noqa: E402
    PrivateSurface,
    PrivateSurfaceItem,
    SurfaceItemKind,
)
from pipeline_core.domain.rust_scan import contains_word  # noqa: E402
from pipeline_core.domain.type_header import parse_type_header  # noqa: E402

# Mirrors `application/author_layout._DERIVABLE_KINDS`: only these can carry a
# `#[derive(...)]`, so only these can be pinned by a derive requirement.
_DERIVABLE_KINDS = frozenset({
    SurfaceItemKind.STRUCT, SurfaceItemKind.ENUM, SurfaceItemKind.PUB_FIELDS,
})

_IDENT_OK = set("abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

# Path roots that make an occurrence somebody else's name. `use
# std::error::Error as StdError;` in `src/backtrace.gen_tests_1.rs:7` is the
# std trait, not this crate's private `StdError`, and counting it would report
# a constraint the layout cannot violate.
_FOREIGN_ROOTS = frozenset({"std", "core", "alloc"})


@dataclass(frozen=True, slots=True)
class Pin:
    """One constraint the frozen oracle places on one private item."""

    item: str
    kind: str            # member_path | assoc_path | call | struct_literal |
                         # generic_use | derive | mention | method_call
    evidence: str
    origin: str          # "src/error.gen_tests_6.rs:42"
    gated: bool = False          # can the t1.5 compile gate decide it today?
    attributable: bool = True    # False: the reference cannot be tied to THIS
                                 # item without type inference (a `.name(`
                                 # receiver call). Reported, never counted.


def _strip_string_literals(line: str) -> str:
    """`line` with the CONTENTS of every string and char literal blanked out.

    `write!(f, "source error")` is not a reference to the private fn `error`,
    and `Error::msg("backtrace coverage")` is not one to `backtrace`; both were
    counted as constraints before this filter existed. Rust lifetimes are not
    char literals — `&'static str` opens a quote that never closes — so a `'`
    is treated as a literal only when it closes within two characters or begins
    an escape.
    """
    out: list[str] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "'" and not (i + 1 < n and line[i + 1] == "\\") \
                and not (i + 2 < n and line[i + 2] == "'"):
            out.append(c)          # a lifetime, not a literal
            i += 1
            continue
        if c in ('"', "'"):
            out.append(" ")
            q, i = c, i + 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == q:
                    break
                i += 1
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _shadowed_names(src: str) -> frozenset[str]:
    """Names this file imports from OUTSIDE the crate.

    A sibling that opens `use std::backtrace::Backtrace;` uses `Backtrace` to
    mean std's, even though the crate declares a private type of that name —
    `Backtrace::force_capture()` in `src/fmt.gen_tests_1.rs:16` is std's API,
    and an authored layout is free to rename the private one without touching
    it. The import line is where the two are told apart, which is why the scan
    skips `use` lines for evidence but reads them for scope.
    """
    out: set[str] = set()
    for _n, line in _code_lines(src):
        t = line.strip()
        if not (t.startswith("use ") or t.startswith("pub use ")):
            continue
        t = t.split("use ", 1)[1].split(";")[0].strip()
        if t.split("::")[0].strip() not in _FOREIGN_ROOTS:
            continue
        head, _, brace = t.partition("{")
        paths = ([b.strip() for b in brace.rstrip("}").split(",")] if brace
                 else [head.strip()])
        for path in paths:
            if not path:
                continue
            bound = (path.split(" as ")[-1] if " as " in path
                     else path.split("::")[-1]).strip()
            if bound:
                out.add(bound)
    return frozenset(out)


def _locally_bound(src: str) -> frozenset[str]:
    """Names this file binds with `let`, which then mean the local everywhere
    below it.

    `let error = Error::from_boxed(boxed);` followed by
    `assert_eq!(error.to_string(), ...)` in `src/error.gen_tests_2.rs` reads as
    two references to the crate's private fn `error` unless the binding is
    honoured. Shadowing the name for the whole file is deliberately blunt: it
    can hide a genuine reference elsewhere in the same file, which pushes
    `pinned` down. This measurement is a lower bound by design, and an
    overstated constraint is the more expensive error — it would argue for
    freezing a component that has real design room.
    """
    out: set[str] = set()
    for _n, line in _code_lines(src):
        t = line.strip()
        for lead in ("let mut ", "let "):
            if t.startswith(lead):
                name = t[len(lead):].split("=")[0].split(":")[0].strip()
                if name and all(c in _IDENT_OK for c in name):
                    out.add(name)
                break
    return frozenset(out)


def _is_let_binding(line: str, at: int) -> bool:
    """Is this occurrence the NAME being bound by a `let`, not a reference?

    `let error = Error::from_boxed(boxed);` in `src/error.gen_tests_2.rs:27`
    binds a local that happens to share its name with the crate's private fn
    `error`. Renaming that fn does not touch the line.
    """
    before = line[:at].rstrip()
    return before.endswith("let") or before.endswith("let mut")


def _path_root(line: str, at: int) -> str:
    """The leftmost segment of the `a::b::Name` path `Name` sits in, or ""."""
    j = at
    while j > 0 and (line[j - 1] in _IDENT_OK or line[j - 1] == ":"):
        j -= 1
    seg = line[j:at]
    return seg.split("::")[0] if seg else ""


def _preceded_by_dot(line: str, at: int) -> bool:
    """Is this occurrence a `.method(` receiver call?

    Such a call cannot be attributed without type inference — `e.chain()` in
    `tests/test_chain.rs:10` is the PUBLIC `Error::chain`, not the private
    `chain` helper of the same name — so these are reported separately and
    never counted as constraints. Same rule `domain/oracle_derives` applies
    when it trusts only the `Type::method(` form.
    """
    j = at - 1
    while j >= 0 and line[j] == " ":
        j -= 1
    return j >= 0 and line[j] == "."


def _word_at(line: str, at: int, name: str) -> bool:
    """Is `name` at `at` a whole word (same boundary rule as the pipeline's)?"""
    before = line[at - 1] if at else ""
    after = line[at + len(name)] if at + len(name) < len(line) else ""
    return before not in _IDENT_OK and after not in _IDENT_OK


def _code_lines(src: str) -> list[tuple[int, str]]:
    """(lineno, code) for every line, comments removed.

    Line comments go through the pipeline's stripper. Block comments are handled
    here with a small state machine — `/* */` is rare in these tests but a
    commented-out block naming a private type would otherwise read as a
    constraint, which is the same error the line stripper exists to prevent.
    """
    out: list[tuple[int, str]] = []
    in_block = False
    for n, raw in enumerate(src.splitlines(), start=1):
        line = raw
        if in_block:
            end = line.find("*/")
            if end < 0:
                out.append((n, ""))
                continue
            line = line[end + 2:]
            in_block = False
        while (start := line.find("/*")) >= 0:
            end = line.find("*/", start + 2)
            if end < 0:
                line = line[:start]
                in_block = True
                break
            line = line[:start] + " " + line[end + 2:]
        out.append((n, _strip_string_literals(strip_line_comment(line))))
    return out


def _classify(line: str, at: int, name: str) -> str:
    """What KIND of reference this occurrence is — by the text that follows it.

    The classes are the ways an authored layout can break a frozen test, in the
    order the compiler would report them: a renamed associated fn (E0599), a
    changed constructor or variant shape (E0061/E0559), a dropped generic
    parameter (E0107). A bare mention is weakest — a `use`, a type annotation —
    but it still requires the NAME to survive, which an authored layout is free
    to change, so it is a constraint and it is counted as one.
    """
    rest = line[at + len(name):].lstrip()
    if rest.startswith("::"):
        return "assoc_path"
    if rest.startswith("("):
        return "call"
    if rest.startswith("{"):
        return "struct_literal"
    if rest.startswith("<"):
        return "generic_use"
    return "mention"


def _component_field_paths(
    comp: tuple[PrivateSurfaceItem, ...], oracle: dict[str, str],
) -> tuple[FieldPath, ...]:
    """The member paths the oracle reads on this component's types.

    Same two filters as `application/author_layout._component_field_paths`: only
    names this component owns, and — unless the root is PUB_FIELDS, whose fields
    the freeze deleted — the frozen definition must actually declare the first
    hop, or the binding was resolved to the wrong type.
    """
    if not oracle:
        return ()
    by_name = {i.name: i for i in comp}
    out: dict[tuple[str, tuple[str, ...]], FieldPath] = {}
    for rel, src in sorted(oracle.items()):
        for p in read_field_paths(rel, src, types=by_name.keys()):
            item = by_name[p.root_type]
            if (item.kind is not SurfaceItemKind.PUB_FIELDS
                    and not contains_word(item.definition, p.fields[0])):
                continue
            out.setdefault((p.root_type, p.fields), p)
    return tuple(out.values())


def _gated_path_exprs(
    comp: tuple[PrivateSurfaceItem, ...], paths: tuple[FieldPath, ...],
) -> frozenset[str]:
    """The member-path exprs `build_field_path_probe` can render into the gate.

    Grouped by declaring file exactly as `_calibrated_field_path_probes` does,
    with the same `generic_roots` exclusion — a probe for a generic root would
    have to invent its type parameters, so the live gate skips it and so does
    this count.
    """
    if not paths:
        return frozenset()
    by_name = {i.name: i for i in comp}
    generic_roots = frozenset(
        i.name for i in comp
        if (h := parse_type_header(i.definition)) is not None and h.generics)
    grouped: dict[str, list[FieldPath]] = {}
    for p in paths:
        grouped.setdefault(by_name[p.root_type].file, []).append(p)
    gated: set[str] = set()
    for _rel, ps in sorted(grouped.items()):
        probe = build_field_path_probe(tuple(ps), generic_roots=generic_roots)
        gated |= set(probe.gated)
    return frozenset(gated)


def _pins_for_component(
    comp: tuple[PrivateSurfaceItem, ...],
    oracle: dict[str, str],
    frozen: dict[str, str],
) -> list[Pin]:
    """Every constraint the frozen tests place on this component's items."""
    pins: list[Pin] = []
    paths = _component_field_paths(comp, oracle)
    gated_exprs = _gated_path_exprs(comp, paths)
    for p in paths:
        pins.append(Pin(p.root_type, "member_path", p.expr, p.origin,
                        p.expr in gated_exprs))

    derivable = [i.name for i in comp if i.kind in _DERIVABLE_KINDS]
    for d in scan_required_derives(frozen, derivable):
        # Prompt-only: a missing derive breaks the TEST target, and the gate
        # compiles `--lib`. This is the class the hardcrate measurement priced.
        pins.append(Pin(d.type_name, "derive", f"{d.evidence} needs {d.trait}",
                        d.origin, False))

    # NAME references, and only where a reference to the private item is
    # possible at all. A private item is nameable only from INSIDE the crate,
    # so the scan runs over the in-crate siblings, not the wider frozen set:
    # `trait Trait: Sized {` in `tests/test_ensure.rs:50` is an integration
    # test declaring its own homonym, and `#[error("outer")]` in
    # `tests/test_boxed.rs:12` is a thiserror attribute. Neither can reach the
    # crate's private `Trait` or `error`, and both were counted before this
    # scope was applied.
    #
    # A PUB_FIELDS item is excluded here for the mirror-image reason: its
    # declaration head is FROZEN API that the model re-emits verbatim, so a
    # test naming `Chain` or calling `Chain::default()` constrains nothing the
    # model is free to choose. Only its member paths and its derives do, and
    # both are collected above.
    names = {i.name for i in comp if i.kind is not SurfaceItemKind.PUB_FIELDS}
    seen: set[tuple[str, str]] = set()
    for rel, src in sorted(oracle.items()):
        in_scope = names - _shadowed_names(src) - _locally_bound(src)
        for lineno, line in _code_lines(src):
            if not line or line.lstrip().startswith(("use ", "pub use ")):
                continue                      # an import binds a name, it does
                                              # not constrain a layout
            for name in in_scope:
                at = 0
                while (at := line.find(name, at)) >= 0:
                    at_end = at + len(name)
                    if _word_at(line, at, name):
                        root = _path_root(line, at)
                        if root in _FOREIGN_ROOTS:
                            at = at_end
                            continue
                        if _is_let_binding(line, at):
                            at = at_end
                            continue
                        kind = ("method_call" if _preceded_by_dot(line, at)
                                else _classify(line, at, name))
                        key = (name, kind)
                        if key not in seen:
                            seen.add(key)
                            pins.append(Pin(
                                name, kind, line.strip()[:100],
                                f"{rel}:{lineno}",
                                gated=False,
                                attributable=kind != "method_call"))
                    at = at_end
    return pins


def _outcomes(project: Path, cluster: str) -> dict[frozenset[str], str]:
    """Recorded t1.5 outcome per component, keyed by its item-name set."""
    p = project / ".skeleton" / f"_layout_outcomes_{cluster}.json"
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {frozenset(r.get("item_names") or []): str(r.get("outcome", "-"))
            for r in rows if isinstance(r, dict)}


def _clusters(project: Path, only: str | None) -> list[str]:
    if only:
        return [only]
    skel = project / ".skeleton"
    if not skel.is_dir():
        return []
    names = []
    for f in sorted(skel.glob("_private_surface_*.json")):
        names.append(f.stem[len("_private_surface_"):])
    return names


def _report_cluster(
    project: Path, cluster: str, detail: bool, max_rows: int,
) -> tuple[int, int, int, int]:
    surface: PrivateSurface = load_private_surface(project, cluster)
    if not surface.replaceable:
        print(f"\ncluster {cluster}: no baked private surface — nothing to measure")
        return (0, 0, 0, 0)
    oracle = read_oracle_sibling_sources(project)
    frozen = read_frozen_test_sources(project)
    outcomes = _outcomes(project, cluster)
    comps = surface.components

    print(f"\ncluster {cluster}")
    print(f"  oracle: {len(oracle)} gen_tests sibling(s), "
          f"{len(frozen)} write-protected test file(s) in total")
    print(f"  surface: {len(surface.replaceable)} private item(s) "
          f"in {len(comps)} component(s)")
    print()
    print("  comp  items  pinned  gated  ungated  free  outcome    items")

    tot_items = tot_pinned = tot_gated = tot_free = 0
    all_ungated: list[Pin] = []
    unattributable: list[Pin] = []
    for idx, comp in enumerate(comps):
        names = tuple(i.name for i in comp)
        found = _pins_for_component(comp, oracle, frozen)
        unattributable.extend(x for x in found if not x.attributable)
        pins = [x for x in found if x.attributable]
        pinned_items = {p.item for p in pins}
        gated_items = {p.item for p in pins if p.gated}
        ungated = [p for p in pins if not p.gated and p.item not in gated_items]
        free = len(names) - len(pinned_items)
        outcome = outcomes.get(frozenset(names), "-")
        shown = ", ".join(names[:6]) + (" …" if len(names) > 6 else "")
        print(f"  {idx:>4}  {len(names):>5}  {len(pinned_items):>6}  "
              f"{len(gated_items):>5}  {len(ungated):>7}  {free:>4}  "
              f"{outcome:<9}  {shown}")
        tot_items += len(names)
        tot_pinned += len(pinned_items)
        tot_gated += len(gated_items)
        tot_free += free
        all_ungated.extend(ungated)

    pct = (100.0 * tot_pinned / tot_items) if tot_items else 0.0
    print(f"\n  totals: {tot_items} item(s), {tot_pinned} pinned ({pct:.0f}%), "
          f"{tot_gated} of them gated, {tot_free} free")
    by_kind = Counter(p.kind for p in all_ungated)
    if by_kind:
        print("  ungated constraint kinds: "
              + ", ".join(f"{k}={n}" for k, n in by_kind.most_common()))
    if unattributable:
        print(f"  plus {len(unattributable)} receiver call(s) on a name this "
              "surface also declares — not counted, not attributable without "
              "type inference (--detail lists them)")

    if detail and all_ungated:
        print("\n  UNGATED CONSTRAINTS — an authored layout may violate any of "
              "these and still be accepted:")
        rows = all_ungated if max_rows <= 0 else all_ungated[:max_rows]
        for p in rows:
            print(f"    {p.item:<24} {p.kind:<14} {p.origin:<34} {p.evidence}")
        if len(rows) < len(all_ungated):
            print(f"    … {len(all_ungated) - len(rows)} more "
                  "(raise --max-rows, or 0 for all)")
    if detail and unattributable:
        print("\n  NOT COUNTED — receiver calls whose type this scan cannot "
              "resolve; each is a constraint only if the receiver really is "
              "the private item of that name:")
        for x in unattributable:
            print(f"    {x.item:<24} {x.kind:<14} {x.origin:<34} {x.evidence}")
    return (tot_items, tot_pinned, tot_gated, tot_free)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure how much of a crate's private layout the frozen "
                    "tests already dictate.")
    ap.add_argument("--project", default="tmp/anyhow", type=Path)
    ap.add_argument("--cluster", default=None)
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--max-rows", default=40, type=int)
    args = ap.parse_args()

    project = args.project
    if not (project / ".skeleton").is_dir():
        print(f"no .skeleton/ under {project} — bake the project first",
              file=sys.stderr)
        return 2
    clusters = _clusters(project, args.cluster)
    if not clusters:
        print(f"no baked private surface under {project}/.skeleton",
              file=sys.stderr)
        return 2

    print(f"oracle pinning — {project}")
    grand = [0, 0, 0, 0]
    for c in clusters:
        for i, v in enumerate(_report_cluster(
                project, c, args.detail, args.max_rows)):
            grand[i] += v
    if len(clusters) > 1 and grand[0]:
        pct = 100.0 * grand[1] / grand[0]
        print(f"\nall clusters: {grand[0]} item(s), {grand[1]} pinned "
              f"({pct:.0f}%), {grand[2]} gated, {grand[3]} free")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

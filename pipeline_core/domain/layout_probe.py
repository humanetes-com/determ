"""Layout pins as a COMPILE-TIME gate — pure domain.

t1.5 feeds the harvested layout pins (``size_of::<Error>() == size_of::<usize>()``,
the NPO niche, ``Send + Sync + Unpin``) into the authoring prompt and then accepts
the model's layout on ``cargo check --lib`` alone. That check runs over a skeleton
whose bodies are all ``unimplemented!()``, and the pins live in ``tests/`` — so a
layout that VIOLATES every pin still passes the gate. The violation surfaces a
whole sweep later as repr/autotrait failures the fill stage can never fix, blamed
on individual fn bodies rather than on the layout that made them unsatisfiable.

This module turns those pins into Rust items that fail at COMPILE time, so the
existing gate can decide the question it was always meant to decide:

  ``assert_eq!(mem::size_of::<Error>(), mem::size_of::<usize>());``
    → ``const _: () = assert!((core::mem::size_of::<Error>()) == (…));``
  ``fn assert<E: Unpin + Send + Sync + 'static>() {}`` (+ its ``::<Error>()`` call)
    → ``fn _probe() { fn _a<T: Unpin + Send + Sync + 'static>() {} _a::<Error>(); }``

Both forms need no bodies and no test harness — exactly what a stubbed skeleton
can answer. Everything here is a depth-aware token scan (Hard Rule: no regex).

CALIBRATION IS THE SAFETY NET (see `author_layout`): a probe is only used to
reject an authored layout after it has been shown to HOLD on the pristine frozen
skeleton. A pin this module renders wrong therefore disables pin-gating loudly
instead of failing good layouts — a false negative costs nothing, a false
positive would freeze a component that was fine.
"""
from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    OutcomeKind,
    StaticAssertKind,
)
from pipeline_core.domain.layout_pins import autotrait_bound_list
from pipeline_core.domain.oracle_field_paths import FieldPath

_AUTOTRAIT_MACROS = ("assert_impl_all!", "assert_not_impl_all!", "assert_impl_any!")
_CONST_KINDS: frozenset[StaticAssertKind] = frozenset({
    StaticAssertKind.SIZE, StaticAssertKind.NICHE, StaticAssertKind.ALIGN})


@dataclass(frozen=True, slots=True)
class LayoutProbe:
    """Rust source that makes a set of layout pins compile-time checkable."""

    source: str = ""                    # "" when nothing could be rendered
    gated: tuple[str, ...] = ()         # pin exprs this probe actually checks
    skipped: tuple[str, ...] = ()       # pin exprs left unchecked (named, never silent)

    @property
    def checks_anything(self) -> bool:
        return bool(self.source and self.gated)


@dataclass(frozen=True, slots=True)
class LayoutGate:
    """Every probe installed for ONE compile of a candidate layout, and the file
    each one has to live in.

    ``root`` is the layout pins. They belong in the crate root: they name public
    types (``size_of::<Error>()``) and the crate root is where those names are in
    scope.

    ``in_file`` maps a skeleton-relative path to the probe that must sit IN that
    file. A private field is visible to its own module and that module's
    descendants and nowhere else, so a member-path probe written at the crate
    root reads ``error[E0616]: field `inner` of `Error` is private`` against a
    CORRECT layout as soon as the type it borrows through is declared in a
    submodule — and nothing before the first authoring attempt can tell, because
    the frozen scaffold has that field elided and answers ``E0609`` first. The
    oracle's own tests can read the field because they are generated as a child
    module of the file that declares it; putting the probe in that same file is
    what gives it the same view. Measured on a two-module crate (2026-08-06):
    probe at the crate root, field elided → E0609; field present and private →
    E0616; field present and ``pub`` → clean — and the authoring splice rejects
    a ``pub`` field outright ("every field of this struct must stay private"),
    so at the crate root NO authored layout could ever have passed.
    """

    root: LayoutProbe = LayoutProbe()
    in_file: tuple[tuple[str, LayoutProbe], ...] = ()

    @property
    def gated(self) -> tuple[str, ...]:
        return self.root.gated + tuple(e for _, p in self.in_file for e in p.gated)

    @property
    def skipped(self) -> tuple[str, ...]:
        return self.root.skipped + tuple(e for _, p in self.in_file for e in p.skipped)

    def without_member_paths(self) -> LayoutGate:
        """The same gate with every member-path probe demoted from checked to
        skipped — what a probe that turned out to be unusable leaves behind.

        The paths are demoted, not dropped: `gated` is what the run log may
        claim was verified, `skipped` is what it must still name, and a path that
        vanished from both would be a constraint the log stopped mentioning
        halfway through a component. Nothing installs a demoted probe, so the
        remaining attempts neither re-place it nor pay a compile to re-learn the
        same verdict."""
        return LayoutGate(root=self.root, in_file=tuple(
            (rel, LayoutProbe(skipped=p.gated + p.skipped))
            for rel, p in self.in_file))


# Probe regions are marker-delimited, never length-based, so an interrupted run
# cannot leave a half-probe behind for the next install to nest inside itself.
# Two pairs, not one, because both regions can land in the SAME file — the pins
# always go to the crate root and a member-path probe goes there too whenever
# its root type is declared there (`anyhow` declares `Error` and `Chain` in
# lib.rs) — and each strip must leave the other region untouched.
PIN_PROBE_MARKERS = ("// __LAYOUT_PIN_PROBE_BEGIN__", "// __LAYOUT_PIN_PROBE_END__")
FIELD_PROBE_MARKERS = ("// __LAYOUT_FIELD_PROBE_BEGIN__",
                       "// __LAYOUT_FIELD_PROBE_END__")


def install_probe_region(
    text: str, source: str, markers: tuple[str, str],
) -> str:
    """`text` with `source` installed as this marker pair's region. Strip-then-
    append, so it is idempotent: a retried attempt replaces the region it wrote
    last time instead of stacking a second one."""
    begin, end = markers
    base = strip_probe_region(text, markers)
    return f"{base.rstrip()}\n\n{begin}\n{source.rstrip()}\n{end}\n"


def strip_probe_region(text: str, markers: tuple[str, str]) -> str:
    """`text` with this marker pair's region removed; unchanged when absent. An
    unterminated region (a run killed mid-install) takes the rest of the file
    with it — the alternative is to leave a fragment that never compiles again."""
    begin, end = markers
    at = text.find(begin)
    if at < 0:
        return text
    close = text.find(end, at)
    if close < 0:
        return text[:at].rstrip() + "\n"
    return text[:at].rstrip() + "\n" + text[close + len(end):].lstrip("\n")


def _balanced_span(text: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the `open_ch`…`close_ch` group beginning at `start`, or
    -1 if it never closes. Depth-aware so nested generics/calls survive."""
    if start >= len(text) or text[start] != open_ch:
        return -1
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split on `sep` at bracket depth 0 — `size_of::<Result<(), Error>>()` must
    stay ONE argument, so a naive `.split(",")` cannot be used."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in text:
        if c in "(<[":
            depth += 1
        elif c in ")>]":
            depth -= 1
        if c == sep and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    out.append("".join(cur))
    return [s.strip() for s in out]


def _macro_args(line: str, macro: str) -> str | None:
    """The verbatim argument text of `macro` in `line`, or None if absent."""
    at = line.find(macro)
    if at < 0:
        return None
    open_at = line.find("(", at + len(macro) - 1)
    if open_at < 0:
        return None
    end = _balanced_span(line, open_at, "(", ")")
    if end < 0:
        return None
    return line[open_at + 1:end - 1]


def _const_assert(expr_line: str) -> str | None:
    """A SIZE/NICHE/ALIGN pin rendered as a const assertion, or None.

    Handles the two shapes a layout test writes: `assert_eq!(A, B);` and a bare
    `assert!(A == B);`. `mem::` is rewritten to `core::mem::` so the probe does
    not depend on the crate having imported `std::mem` (and works under no_std).
    """
    args = _macro_args(expr_line, "assert_eq!")
    if args is not None:
        parts = _split_top_level(args, ",")
        # a trailing format argument is not part of the compared pair
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        lhs, rhs = _qualify_mem(parts[0]), _qualify_mem(parts[1])
        return f"const _: () = assert!(({lhs}) == ({rhs}));"
    args = _macro_args(expr_line, "assert!")
    if args is not None and "==" in args:
        return f"const _: () = assert!({_qualify_mem(args)});"
    return None


def _qualify_mem(expr: str) -> str:
    """`mem::size_of::<T>()` → `core::mem::size_of::<T>()`, leaving an already
    qualified path (`std::mem::`, `core::mem::`) alone. Whole-segment match so a
    identifier merely ENDING in `mem` is untouched."""
    out: list[str] = []
    i = 0
    while True:
        at = expr.find("mem::", i)
        if at < 0:
            out.append(expr[i:])
            return "".join(out)
        prev = expr[at - 1] if at else ""
        already = prev == ":" or (prev.isalnum() or prev == "_")
        out.append(expr[i:at])
        out.append("mem::" if already else "core::mem::")
        i = at + len("mem::")


def _macro_autotrait(expr_line: str) -> tuple[str, str] | None:
    """`assert_impl_all!(Error: Send, Sync)` → ("Error", "Send + Sync")."""
    for macro in _AUTOTRAIT_MACROS:
        args = _macro_args(expr_line, macro)
        if args is None or ":" not in args:
            continue
        ty, rest = args.split(":", 1)
        bounds = " + ".join(b for b in _split_top_level(rest, ",") if b)
        if ty.strip() and bounds:
            return ty.strip(), bounds
    return None


def build_layout_probe(
    pins: tuple[BehaviourSpec, ...] | list[BehaviourSpec],
    *,
    pinned_types: tuple[str, ...] = (),
) -> LayoutProbe:
    """Render `pins` as compile-checkable Rust items.

    `pinned_types` are the crate-public type names whose layout the component
    feeds; a generic autotrait helper (`fn assert<E: Send + Sync>()`) names its
    BOUNDS but not the type it is applied to — that type comes from the caller's
    surface, not from re-parsing the neighbouring call line.
    """
    items: list[str] = []
    gated: list[str] = []
    skipped: list[str] = []
    for spec in pins:
        outcome = spec.then
        if outcome is None or outcome.kind is not OutcomeKind.STATIC_ASSERTION:
            continue
        expr = outcome.static_expr.strip()
        rendered: list[str] = []
        if outcome.static_kind in _CONST_KINDS:
            one = _const_assert(expr)
            if one:
                rendered.append(one)
        elif outcome.static_kind is StaticAssertKind.AUTOTRAITS:
            direct = _macro_autotrait(expr)
            if direct is not None:
                ty, bounds = direct
                rendered.append(_autotrait_item(len(items), ty, bounds))
            else:
                generic_bounds = autotrait_bound_list(expr)
                if generic_bounds:
                    rendered.extend(
                        _autotrait_item(len(items) + n, ty, generic_bounds)
                        for n, ty in enumerate(pinned_types))
        if rendered:
            items.extend(rendered)
            gated.append(expr)
        else:
            skipped.append(expr)
    if not items:
        return LayoutProbe(skipped=tuple(skipped))
    body = "\n".join(items)
    source = (
        "// Layout-pin probe (t1.5 gate) — generated, compile-time only.\n"
        "// A pin harvested from the crate's own tests, rendered so the\n"
        "// authoring gate can reject a layout that violates it instead of\n"
        "// discovering the violation a full sweep later.\n"
        "#[allow(dead_code, clippy::all)]\n"
        "const _LAYOUT_PIN_PROBE: () = ();\n"
        f"{body}\n")
    return LayoutProbe(source=source, gated=tuple(gated), skipped=tuple(skipped))


def _autotrait_item(n: int, ty: str, bounds: str) -> str:
    """A never-called fn whose body forces `ty: bounds` to hold at compile time."""
    return (f"#[allow(dead_code)]\nfn _layout_pin_autotrait_{n}() {{ "
            f"fn _a<T: {bounds}>() {{}} _a::<{ty}>(); }}")


def build_field_path_probe(
    paths: tuple[FieldPath, ...] | list[FieldPath],
    *,
    generic_roots: frozenset[str] = frozenset(),
) -> LayoutProbe:
    """Render the oracle's member paths as lib-target items.

    ``err.inner.ptr`` on an `Error` becomes::

        fn _layout_pin_field_path_0(v: &Error) { let _ = &v.inner.ptr; }

    — a borrow, so no constructor and no value are needed, which is what makes it
    answerable by a skeleton whose bodies are all ``unimplemented!()``. rustc
    resolves the whole chain, including the hops through types the same component
    authors (`Own::ptr` behind `Error::inner`), so nothing here has to model field
    types.

    `generic_roots` are root names whose frozen declaration takes type parameters:
    a probe would have to invent them (`fn p(v: &ContextError)` is E0107), so they
    are SKIPPED and named, never guessed at."""
    items: list[str] = []
    gated: list[str] = []
    skipped: list[str] = []
    for path in paths:
        if not path.fields or path.root_type in generic_roots:
            skipped.append(path.expr)
            continue
        chain = "".join(f".{f}" for f in path.fields)
        items.append(
            f"#[allow(dead_code)]\nfn _layout_pin_field_path_{len(items)}"
            f"(v: &{path.root_type}) {{ let _ = &v{chain}; }}")
        gated.append(path.expr)
    if not items:
        return LayoutProbe(skipped=tuple(skipped))
    source = (
        "// Field-path probe (t1.5 gate) — generated, compile-time only.\n"
        "// Each path is read by a WRITE-PROTECTED test of this crate; a layout\n"
        "// that does not provide it makes that test uncompilable, and no fill\n"
        "// route may edit a test to match. Rejecting here is the only place the\n"
        "// question can still be answered by re-authoring.\n"
        + "\n".join(items) + "\n")
    return LayoutProbe(source=source, gated=tuple(gated), skipped=tuple(skipped))

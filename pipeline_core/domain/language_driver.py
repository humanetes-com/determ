"""Who is the language expert in fill and layout prompts.

The fill model is the language expert. The pipeline's job is the gate:
who may write, what evidence sits in the prompt, whether a two-file
change is one transaction. A catalog of std-trait tutorials, a
Display/Debug name-set, a hardcoded derive list, a Send/Sync
tutorial, a recipe keyed on two crate constructors, or a conversion
family of five crate names is the pipeline taking the language job —
the next leftover of a different construct then needs a sibling of
each function.

One driver, :class:`GenericLanguageDriver`: it keys on shape the record
already has (a ``fmt::Result`` return, a harvested pin, a computed derive
list injected elsewhere, an unsized ``Box<dyn`` argument, a by-value
``self`` that copies a pointer field, a Then that moved under an env flip,
rustc saying a name or field is missing). The catalog driver (the
pre-2026-09-09 tables, a recipe keyed on two crate constructors and a
conversion family of five crate names) was deleted on 2026-10-17: it was
original-derived guidance one ``LANGUAGE_DRIVER`` flip from a prompt. The
last commit that carries it is tagged ``catalog-driver-last``; an A/B
restores its files from that tag into a branch and ports it to the
then-current protocol.

Bind once at the composition root (:func:`bind`). The default is
generic so unit tests that never import the pipeline see the
production driver. Domain methods themselves are pure; bind is the
composition seam (same shape as ``prompts.runtime``).
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pipeline_core.domain.installable_fns import fn_decl_shape
from pipeline_core.domain.rust_idents import impl_self_type, impl_trait_name


@runtime_checkable
class LanguageDriver(Protocol):
    """Bound language policy for fill and layout prompts."""

    @property
    def name(self) -> str: ...
    def trait_contract(self, container: str) -> str: ...
    def is_formatting(self, container: str, sig: str) -> bool: ...
    def format_follow_default(self) -> tuple[str, ...]: ...
    def derived_trait_rule(self) -> str: ...
    def autotrait_rule(self) -> str: ...
    def class_prompts(self, *, pin_env_debug: bool) -> dict[str, str]: ...
    def exact_format_observe_ok(self, observe: str) -> bool: ...
    def exact_format_probe_covers(self, spec: object) -> bool: ...
    def gotchas_summary(self) -> str: ...
    def gotchas_for_error(self, error_excerpt: str) -> str: ...
    def construction_recipe(self, name: str, container: str, sig: str = "") -> str: ...
    def constructs_identity_subject(
        self, name: str, container: str, sig: str,
        identity_subject: str = "") -> bool: ...
    def identity_must_admits(
        self, name: str, container: str, sig: str,
        identity_subject: str = "") -> bool: ...
    def is_vtable_dispatch(self, sig_text: str, container: str) -> bool: ...
    def vtable_fast_exit_guidance(self) -> str: ...
    def vtable_category_line(self) -> str: ...
    def access_licenses_conversion(
        self, symbols: str, crate_types: Collection[str] = (),
        crate_traits: Collection[str] = ()) -> bool: ...
    def origin_licenses_conversion(self, origin: Sequence[Mapping[str, object]]) -> bool: ...
    def unique_called_name(self, source_line: str) -> str | None: ...
    def called_name_at_column(self, source_line: str, col: int) -> str | None: ...
    def ownership_transfer_must(self, names: Collection[str]) -> str: ...
    def file_fill_tag_examples(self) -> str: ...
    def sig_change_example(self, fn_name: str) -> str: ...


def _return_type(sig: str) -> str:
    shape = fn_decl_shape(sig)
    return shape[2] if shape is not None else ""


def _params(sig: str) -> tuple[str, ...]:
    shape = fn_decl_shape(sig)
    return shape[1] if shape is not None else ()


def _has_by_value_self(sig: str) -> bool:
    return "self" in _params(sig)


def _has_box_dyn_arg(sig: str) -> bool:
    return any("Box<dyn" in p or "Box< dyn" in p for p in _params(sig))


def _box_dyn_plus_other_arg(sig: str) -> bool:
    """Unsized box payload plus another argument (construction context).

    A single-argument ``Box<dyn …> -> Self`` is already a boxed value;
    wrapping it a second time is the miss the name-keyed catalog refused
    by refusing a near-miss constructor. The extra argument is the shape
    that distinguishes a constructor that must wrap from a conversion
    that already holds the box.
    """
    params = [p for p in _params(sig) if p not in ("self", "&self", "&mut self")]
    if len(params) < 2:
        return False
    return any("Box<dyn" in p or "Box< dyn" in p for p in params)


def _inherent_self_type(container: str) -> str:
    if impl_trait_name(container):
        return ""
    return impl_self_type(container) or ""


def _single_ident_wrapper(ty: str) -> bool:
    """``Ident<Ident>`` — one wrapper of one named type, no dyn, no comma."""
    lt = ty.find("<")
    if lt <= 0 or not ty.endswith(">"):
        return False
    inner = ty[lt + 1:-1].strip()
    if not inner or "," in inner or " " in inner:
        return False
    if inner.startswith("dyn"):
        return False
    head = ty[:lt]
    return head.isidentifier() and inner.replace("_", "x").isalnum() and inner[0].isalpha()


def _unsafe_fn(sig: str, container: str) -> bool:
    combined = sig + " " + container
    return "unsafe fn" in combined or combined.strip().startswith("unsafe ")


def _called_idents(line: str) -> list[str]:
    """Bare identifiers reached as ``.name(`` / ``::name(`` / turbofish call,
    in source order. The conversion on an ASAN test line is the first
    call; trailing adapters (``unwrap``) sit after it.
    """
    return [name for name, _start, _end in _called_spans(line)]


def _path_start(line: str, sep: int) -> int:
    """Where the path ending in the ``::`` at ``sep`` begins: back over
    ``Ident::`` segments (``Error::msg`` begins at ``E``)."""
    k = sep
    while k > 0:
        if line[k - 1].isalnum() or line[k - 1] == "_":
            k -= 1
        elif k > 1 and line[k - 2:k] == "::":
            k -= 2
        else:
            break
    return k


def _called_spans(line: str) -> list[tuple[str, int, int]]:
    """Every call :func:`_called_idents` finds, as ``(name, start, end)``,
    0-based and end-exclusive: ``start`` is where the callee is written
    (the name after ``.``, the path's first segment before ``::``), ``end``
    is past the name. A debugger's caller column falls in this span."""
    found: list[tuple[str, int, int]] = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] == "." or (line[i] == ":" and i + 1 < n and line[i + 1] == ":"):
            start_of = (_path_start(line, i) if line[i] == ":" else None)
            i = i + 2 if line[i] == ":" else i + 1
            while i < n and line[i].isspace():
                i += 1
            if i < n and (line[i].isalpha() or line[i] == "_"):
                j = i + 1
                while j < n and (line[j].isalnum() or line[j] == "_"):
                    j += 1
                name = line[i:j]
                k = j
                while k < n and line[k].isspace():
                    k += 1
                if k + 1 < n and line[k:k + 2] == "::":
                    k += 2
                    while k < n and line[k].isspace():
                        k += 1
                    if k < n and line[k] == "<":
                        depth = 0
                        while k < n:
                            if line[k] == "<":
                                depth += 1
                            elif line[k] == ">":
                                depth -= 1
                                if depth == 0:
                                    k += 1
                                    break
                            k += 1
                        while k < n and line[k].isspace():
                            k += 1
                if k < n and line[k] == "(":
                    found.append((name, i if start_of is None else start_of, j))
                i = j
            continue
        i += 1
    return found


_WRAP_UNSIZED = (
    "the argument is unsized; wrap it in a sized type this crate already "
    "declares. Do not pass the unsized value to a producer whose bound "
    "requires a sized type."
)

_SELF_ALREADY_IDENTITY = (
    "self is already this crate's erased identity type; do not call a "
    "producer whose bound that type does not satisfy."
)

_OWNERSHIP_AFTER_COPY = (
    "MUST: after a pointer field is copied out of a by-value receiver, "
    "the original owner must not drop."
)

# Rustc's own wording that a name or field is not there. That is the
# pipeline gate (use TYPES_IN_SCOPE; do not add items), not a language
# tutorial and not a recipe that names a type this crate declares.
_UNKNOWN_NAME_MARKS = (
    "cannot find value",
    "cannot find function",
    "no field",
    "has no field named",
    "does not have a field",
    "no method",
    "no function or associated item",
    "no associated item named",
)

_IN_SCOPE_ONLY_GATE = (
    "You are filling a function body. You may not add a new top-level item "
    "(no new static, const, free function, or use). Use only names and fields "
    "listed in TYPES_IN_SCOPE, the impl, and its siblings. Build any needed "
    "value inline from those items. Do not invent a field, method, or "
    "associated function the type definition does not show."
)


def _unknown_name_diagnostic(excerpt: str) -> bool:
    low = excerpt.lower()
    return any(m in low for m in _UNKNOWN_NAME_MARKS)


# Rustc's own wording that a cfg name is not a cargo feature. That note
# is already in COMPILE_ERRORS. Teaching a WRONG/RIGHT pair, or naming
# a cfg this crate declares, is the catalog.
_CFG_NAMESPACE_MARKS = (
    "expected values for `feature`",
    "expected values for feature",
    "unexpected `cfg` condition",
    "unexpected cfg condition",
    "unexpected_cfgs",
)


def _cfg_namespace_diagnostic(excerpt: str) -> bool:
    low = excerpt.lower()
    return any(m in low for m in _CFG_NAMESPACE_MARKS)


_GENERIC_CLASS_ASKS: dict[str, str] = {
    "IDENTITY": (
        "the concrete type a downcast finds on a value this crate constructed "
        "— the TypeId round-trip, named by type"
    ),
    "TRAIT_OBJECT_IDENTITY": (
        "the type observed through a trait object, which a wrapper "
        "representation can answer differently"
    ),
    "EXACT_FORMAT": (
        "an exact rendering of a value that moves with the environment"
    ),
    "BOUNDED_RESOURCE": (
        "what happens at a size or capacity boundary, and just past it"
    ),
    "OPTION_SEMANTICS": (
        "when an optional observable is present and when it is absent"
    ),
}


@dataclass(frozen=True, slots=True)
class GenericLanguageDriver:
    """The fill model is the language expert; the pipeline is the gate.

    Does not read the catalog tables. A leftover of a different construct
    pays through the same functions.
    """

    name: str = "generic"

    def trait_contract(self, container: str) -> str:
        # The trait name is already in the impl header the prompt quotes.
        return ""

    def is_formatting(self, container: str, sig: str) -> bool:
        # Shape: a fmt::Result return. An impl that returns that type is
        # formatting; an impl whose signature failed to parse is a parser
        # bug, not a reason to name the trait. `container` is accepted so
        # the catalog driver can use it; this path does not read it.
        del container
        return "fmt::Result" in _return_type(sig)

    def format_follow_default(self) -> tuple[str, ...]:
        # The std format method every format-trait dispatcher declares.
        # Production passes `_follow_methods_for_trait(trait)` and does
        # not use this default. The catalog default also followed a
        # second method named for one leftover; this path does not.
        return ("fmt",)

    def derived_trait_rule(self) -> str:
        # `derive_requirements_text` already injects the computed list
        # from the tests. A standard-set paragraph invents derives the
        # tests did not ask for (E0119 on a hand-written impl).
        return ""

    def autotrait_rule(self) -> str:
        # Harvested autotrait pins already sit in LAYOUT-PINNING TESTS.
        return ""

    def class_prompts(self, *, pin_env_debug: bool) -> dict[str, str]:
        # Env-moving is the scarcity signal, not a keyword in the ask.
        # The catalog switch that names one leftover's observe tag is
        # accepted so the signature matches; this path does not read it.
        del pin_env_debug
        return dict(_GENERIC_CLASS_ASKS)

    def exact_format_observe_ok(self, observe: str) -> bool:
        # Any non-empty observe can pin a render that moves. A display
        # observe that moved under the env flip counts; an empty tag
        # does not.
        return bool((observe or "").strip())

    def exact_format_probe_covers(self, spec: object) -> bool:
        # The Then moved under the env flip. A probe whose required_env
        # is empty did not move, whatever render kind it used.
        return bool(getattr(spec, "required_env", ()))

    def gotchas_summary(self) -> str:
        # The always-on cheatsheet is the catalog of language tutorials.
        # The rustc excerpt already sits in COMPILE_ERRORS; TYPES_IN_SCOPE
        # is the standing evidence. A missing-name note pays the gate in
        # gotchas_for_error, not a dump of every recipe.
        return ""

    def gotchas_for_error(self, error_excerpt: str) -> str:
        # Language DETAIL is omitted. When rustc says a name or field is
        # missing, the pipeline gate is: do not add items; use
        # TYPES_IN_SCOPE. When rustc says a cfg is not a cargo feature,
        # that note is already in COMPILE_ERRORS — do not name a cfg
        # this crate declares. Crate recipes stay on the catalog driver.
        if not (error_excerpt or "").strip():
            return ""
        if _cfg_namespace_diagnostic(error_excerpt):
            return ""
        if _unknown_name_diagnostic(error_excerpt):
            return _IN_SCOPE_ONLY_GATE
        return ""

    def construction_recipe(self, name: str, container: str, sig: str = "") -> str:
        del name
        if not sig:
            return ""
        if _box_dyn_plus_other_arg(sig):
            return _WRAP_UNSIZED
        if (_has_by_value_self(sig)
                and _return_type(sig) == "Self"
                and _inherent_self_type(container)):
            return _SELF_ALREADY_IDENTITY
        return ""

    def constructs_identity_subject(
        self, name: str, container: str, sig: str,
        identity_subject: str = "") -> bool:
        del name
        if not identity_subject or self.is_formatting(container, sig):
            return False
        ret = _return_type(sig)
        if ret == identity_subject:
            return True
        self_ty = impl_self_type(container) or ""
        return ret == "Self" and self_ty == identity_subject

    def identity_must_admits(
        self, name: str, container: str, sig: str,
        identity_subject: str = "") -> bool:
        del name
        if self.is_formatting(container, sig):
            return False
        ret = _return_type(sig)
        if "TypeId" in sig and ret.startswith("Option"):
            return True
        return self.constructs_identity_subject(
            "", container, sig, identity_subject)

    def is_vtable_dispatch(self, sig_text: str, container: str) -> bool:
        # unsafe fn, a wrapper-of-named-type parameter, no where-bound
        # that would open a typed path. The inner type's fn-pointer
        # fields are the extractor's job when records are present; the
        # wrapper shape is what the signature already shows.
        if not _unsafe_fn(sig_text, container):
            return False
        if "where " in sig_text:
            return False
        return any(_single_ident_wrapper(p) for p in _params(sig_text))

    def vtable_fast_exit_guidance(self) -> str:
        return (
            "fn requires layout access via a wrapper of a type whose fields "
            "are function pointers, with no where-bound that opens a typed "
            "path. The table is not exposed — do not retry."
        )

    def vtable_category_line(self) -> str:
        return (
            "  NEEDS_VTABLE_ACCESS     — requires reading a function-pointer "
            "table; layout is needed → classify as GENUINELY_HARD immediately; "
            "vtable layout cannot be satisfied via trait methods alone, and "
            "the skeleton does not expose the table"
        )

    def access_licenses_conversion(
        self, symbols: str, crate_types: Collection[str] = (),
        crate_traits: Collection[str] = ()) -> bool:
        if "Box<dyn " in symbols:
            return True
        if any(name and name in symbols for name in crate_types):
            return True
        return any(trait and f"dyn {trait}" in symbols for trait in crate_traits)

    def origin_licenses_conversion(
            self, origin: Sequence[Mapping[str, object]]) -> bool:
        # Destructor-then-caller on the free stack is the shape; the
        # catalog then returns a name set. This path only licenses.
        from pipeline_core.domain.crash_site import first_test_file_after_destructor
        return first_test_file_after_destructor(origin) is not None

    def unique_called_name(self, source_line: str) -> str | None:
        found = _called_idents(source_line)
        return found[0] if found else None

    def called_name_at_column(self, source_line: str, col: int) -> str | None:
        # `col` is the detector's 1-based column of a caller frame.
        at = {name for name, start, end in _called_spans(source_line)
              if start <= col - 1 < end}
        return next(iter(at)) if len(at) == 1 else None

    def ownership_transfer_must(self, names: Collection[str]) -> str:
        # A non-empty crashed set at this slot is already the conversion the
        # localizer named; no name set decides it.
        return _OWNERSHIP_AFTER_COPY if names else ""

    def file_fill_tag_examples(self) -> str:
        return (
            'WRONG: <FN name="Type: method" block="impl Type">\n'
            'WRONG: <FN name="Inner::method" block="impl Inner">\n'
            'RIGHT: <FN name="method" block="impl Type">\n'
            'RIGHT: <FN name="method" block="impl Inner">'
        )

    def sig_change_example(self, fn_name: str) -> str:
        return f"  fn {fn_name}(arg: &mut T, extra: usize) {{\n"


_DRIVER: LanguageDriver = GenericLanguageDriver()


def bind(driver: LanguageDriver) -> None:
    """Bind once at the composition root. Tests restore via :func:`using_driver`."""
    global _DRIVER
    _DRIVER = driver


def language_driver() -> LanguageDriver:
    return _DRIVER


@contextmanager
def using_driver(driver: LanguageDriver) -> Iterator[LanguageDriver]:
    """Bind `driver` for the block and restore the previous driver after."""
    prev = language_driver()
    bind(driver)
    try:
        yield driver
    finally:
        bind(prev)

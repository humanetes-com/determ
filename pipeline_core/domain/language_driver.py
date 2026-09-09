"""Who is the language expert in fill and layout prompts.

The fill model is the language expert. The pipeline's job is the gate:
who may write, what evidence sits in the prompt, whether a two-file
change is one transaction. A catalog of std-trait tutorials, a
Display/Debug name-set, a hardcoded derive list, a Send/Sync
tutorial, a recipe keyed on two crate constructors, or a conversion
family of five crate names is the pipeline taking the language job —
the next leftover of a different construct then needs a sibling of
each function.

Two drivers, selected by ``LANGUAGE_DRIVER`` at composition
(``generic`` default, ``catalog`` the pre-2026-09-09 tables for an A/B):

- :class:`GenericLanguageDriver` keys on shape the record already has
  (a ``fmt::Result`` return, a harvested pin, a computed derive list
  injected elsewhere, an unsized ``Box<dyn`` argument, a by-value
  ``self`` that copies a pointer field, a Then that moved under an
  env flip, rustc saying a name or field is missing). It does not
  import or call the catalog tables.
- :class:`CatalogLanguageDriver` is those tables, kept so an A/B can
  turn them back on. The factory
  ``pipeline_core.prompts.language_catalog.catalog_driver`` injects the
  original functions so this module never imports prompts.

Bind once at the composition root (:func:`bind`). The default is
generic so unit tests that never import the pipeline see the
production driver. Domain methods themselves are pure; bind is the
composition seam (same shape as ``prompts.runtime``).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
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
    def conversion_family(self) -> frozenset[str]: ...
    def access_licenses_conversion(
        self, symbols: str, crate_types: Collection[str] = (),
        crate_traits: Collection[str] = ()) -> bool: ...
    def origin_licenses_conversion(self, origin: Sequence[Mapping[str, object]]) -> bool: ...
    def unique_called_name(self, source_line: str) -> str | None: ...
    def is_destructor_fn_name(self, fn_name: str) -> bool: ...
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
    found: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] == "." or (line[i] == ":" and i + 1 < n and line[i + 1] == ":"):
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
                    found.append(name)
                i = j
            continue
        i += 1
    return found


_WRAP_UNSIZED = (
    "the argument is unsized; wrap it in a sized StdError this crate already "
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
        # TYPES_IN_SCOPE. Crate recipes stay on the catalog driver.
        if not (error_excerpt or "").strip():
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

    def conversion_family(self) -> frozenset[str]:
        # Membership is shape (by-value receiver that copies a pointer
        # field), computed at the call site from records. No hardcoded
        # name set lives here.
        return frozenset()

    def access_licenses_conversion(
        self, symbols: str, crate_types: Collection[str] = (),
        crate_traits: Collection[str] = ()) -> bool:
        if "Box<dyn " in symbols:
            return True
        for name in crate_types:
            if name and name in symbols:
                return True
        for trait in crate_traits:
            if trait and f"dyn {trait}" in symbols:
                return True
        return False

    def origin_licenses_conversion(
            self, origin: Sequence[Mapping[str, object]]) -> bool:
        # Destructor-then-caller on the free stack is the shape; the
        # catalog then returns a name set. This path only licenses.
        from pipeline_core.domain.crash_site import first_test_file_after_destructor
        return first_test_file_after_destructor(list(origin)) is not None

    def unique_called_name(self, source_line: str) -> str | None:
        found = _called_idents(source_line)
        return found[0] if found else None

    def is_destructor_fn_name(self, fn_name: str) -> bool:
        # The faulting frame already names the fn. Synthesizing a family
        # from a token in crate fn names is the hop this path drops.
        # ASAN drop-glue marks stay evidence parsing at the call site.
        del fn_name
        return False

    def ownership_transfer_must(self, names: Collection[str]) -> str:
        if not names:
            return ""
        if self.conversion_family().intersection(names):
            return _OWNERSHIP_AFTER_COPY
        # No hardcoded family: a non-empty crashed set at this slot is
        # already the conversion the localizer named.
        return _OWNERSHIP_AFTER_COPY

    def file_fill_tag_examples(self) -> str:
        return (
            'WRONG: <FN name="Type: method" block="impl Type">\n'
            'WRONG: <FN name="Inner::method" block="impl Inner">\n'
            'RIGHT: <FN name="method" block="impl Type">\n'
            'RIGHT: <FN name="method" block="impl Inner">'
        )

    def sig_change_example(self, fn_name: str) -> str:
        return f"  unsafe fn {fn_name}(arg: Own<T>, target: TypeId) {{\n"


@dataclass(frozen=True, slots=True)
class CatalogLanguageDriver:
    """The pre-2026-09-09 language catalogs, kept for an A/B.

    The tables live in prompts / ``catalog_is_formatting``. This class
    holds injected callables so domain never imports prompts; the
    factory is :func:`pipeline_core.prompts.language_catalog.catalog_driver`.
    Constructing this with empty callables would silently no-op — do not;
    go through the factory.
    """

    trait_contract_fn: Callable[[str], str]
    is_formatting_fn: Callable[[str, str], bool]
    derived_trait_rule_text: str
    autotrait_rule_text: str
    gotchas_summary_fn: Callable[[], str]
    gotchas_for_error_fn: Callable[[str], str]
    class_prompts_fn: Callable[[bool], dict[str, str]]
    name: str = "catalog"
    format_follow: tuple[str, ...] = ("fmt", "debug")

    def trait_contract(self, container: str) -> str:
        return self.trait_contract_fn(container)

    def is_formatting(self, container: str, sig: str) -> bool:
        return self.is_formatting_fn(container, sig)

    def format_follow_default(self) -> tuple[str, ...]:
        return self.format_follow

    def derived_trait_rule(self) -> str:
        return self.derived_trait_rule_text

    def autotrait_rule(self) -> str:
        return self.autotrait_rule_text

    def class_prompts(self, *, pin_env_debug: bool) -> dict[str, str]:
        return self.class_prompts_fn(pin_env_debug)

    def exact_format_observe_ok(self, observe: str) -> bool:
        from pipeline_core.domain.behaviour_probe import catalog_exact_format_observe_ok
        return catalog_exact_format_observe_ok(observe)

    def exact_format_probe_covers(self, spec: object) -> bool:
        from pipeline_core.domain.behaviour_probe import catalog_exact_format_probe_covers
        return catalog_exact_format_probe_covers(spec)

    def gotchas_summary(self) -> str:
        return self.gotchas_summary_fn()

    def gotchas_for_error(self, error_excerpt: str) -> str:
        return self.gotchas_for_error_fn(error_excerpt)

    def construction_recipe(self, name: str, container: str, sig: str = "") -> str:
        from pipeline_core.domain.construction_recipe import catalog_construction_recipe
        del sig
        return catalog_construction_recipe(name, container)

    def constructs_identity_subject(
        self, name: str, container: str, sig: str,
        identity_subject: str = "") -> bool:
        from pipeline_core.domain.behaviour_select import catalog_constructs_identity_subject
        del identity_subject
        return catalog_constructs_identity_subject(name, container, sig)

    def identity_must_admits(
        self, name: str, container: str, sig: str,
        identity_subject: str = "") -> bool:
        from pipeline_core.domain.behaviour_select import catalog_identity_must_admits
        del identity_subject
        return catalog_identity_must_admits(name, container, sig)

    def is_vtable_dispatch(self, sig_text: str, container: str) -> bool:
        # Catalog: Own<ErrorImpl>/Ref<ErrorImpl> and no where E: bound.
        if "unsafe fn" not in sig_text:
            return False
        combined = sig_text + " " + container
        if "Own<ErrorImpl" not in combined and "Ref<ErrorImpl" not in combined:
            return False
        return "where E:" not in sig_text

    def vtable_fast_exit_guidance(self) -> str:
        return (
            "fn requires ErrorVTable layout access via Own<ErrorImpl> or "
            "Ref<ErrorImpl> without a where-bound. The vtable struct is not "
            "exposed by the skeleton — do not retry."
        )

    def vtable_category_line(self) -> str:
        return (
            "  NEEDS_VTABLE_ACCESS     — requires reading the ErrorVTable; "
            "layout is needed → classify as GENUINELY_HARD immediately; "
            "vtable layout cannot be satisfied via trait methods alone, and "
            "the skeleton does not expose the vtable struct"
        )

    def conversion_family(self) -> frozenset[str]:
        from pipeline_core.domain.crash_site import CONVERSION_FAMILY
        return CONVERSION_FAMILY

    def access_licenses_conversion(
        self, symbols: str, crate_types: Collection[str] = (),
        crate_traits: Collection[str] = ()) -> bool:
        from pipeline_core.domain.crash_site import catalog_access_licenses_conversion
        del crate_types, crate_traits
        return catalog_access_licenses_conversion(symbols)

    def origin_licenses_conversion(
            self, origin: Sequence[Mapping[str, object]]) -> bool:
        from pipeline_core.domain.crash_site import catalog_origin_licenses_conversion
        return catalog_origin_licenses_conversion(origin)

    def unique_called_name(self, source_line: str) -> str | None:
        from pipeline_core.domain.crash_site import (
            CONVERSION_FAMILY,
            crate_fn_named_on_source_line,
        )
        return crate_fn_named_on_source_line(source_line, CONVERSION_FAMILY)

    def is_destructor_fn_name(self, fn_name: str) -> bool:
        from pipeline_core.domain.crash_site import catalog_is_destructor_fn_name
        return catalog_is_destructor_fn_name(fn_name)

    def ownership_transfer_must(self, names: Collection[str]) -> str:
        from pipeline_core.domain.crash_site import catalog_ownership_transfer_must
        return catalog_ownership_transfer_must(names)

    def file_fill_tag_examples(self) -> str:
        return (
            'WRONG: <FN name="Error: root_cause" block="impl Error">\n'
            'WRONG: <FN name="ErrorImpl::provide" block="impl ErrorImpl">\n'
            'RIGHT: <FN name="root_cause" block="impl Error">\n'
            'RIGHT: <FN name="provide" block="impl ErrorImpl">'
        )

    def sig_change_example(self, fn_name: str) -> str:
        return (
            "  unsafe fn " + fn_name
            + "<E: 'static>(e: Own<ErrorImpl>, target: TypeId) {\n"
        )


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

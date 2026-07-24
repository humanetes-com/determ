"""Domain records for the Python↔Rust extractor seam (CODE_ARCHITECTURE.md §4, §5 item 6).

These frozen dataclasses mirror the ``serde::Serialize`` structs in
``tools/skeleton_extractor/src/lib.rs`` field-for-field (the JSON record contract). They
replace the untyped ``dict`` records that flowed out of the ``Extractor`` adapter, so
consumers get attribute access (``rec.fn_name``) that mypy can check instead of stringly
``rec["fn_name"]`` / ``rec.get("sig_text")`` lookups that fail only at runtime.

The seam is sacred: ``from_dict`` is tolerant (missing keys take the field default, extra
keys are ignored) and ``to_dict`` round-trips back to the exact wire shape, so nothing
about the byte-level JSON contract changes — only the in-process representation.

Pure domain: stdlib only, no I/O. Parsing happens in the adapter, which is the seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from pipeline_core.domain.cfg import CfgAtom
from pipeline_core.domain.import_repair import ImportFix


def _strs(v: Any) -> tuple[str, ...]:
    """Coerce a JSON list (or None) into a tuple of strings."""
    if not v:
        return ()
    return tuple(str(x) for x in v)


class FnLocator(NamedTuple):
    """Identity of one reconstructable function: its ``file`` (repo-relative
    ``.rs`` path) and ``fn_name``. This ``(file, fn_name)`` pair is the pipeline's
    ubiquitous fill-unit key — the frontier key, the container-index key, the
    attribution reverse-map value, the leaf-first ordering unit, the fix-loop
    repair target.

    A ``NamedTuple`` (deliberately, not a frozen dataclass): it stays structurally
    a 2-tuple, so it is a *drop-in* for every existing ``set`` membership, ``dict``
    key, positional unpack (``for file, fn in …``) and tuple-equality that already
    treats the pair as a bare ``tuple[str, str]`` — while adding the ``.file`` /
    ``.fn_name`` vocabulary and a named constructor. Producers may be migrated
    one slice at a time; a not-yet-converted plain tuple still interoperates.

    Caveat: it does NOT structurally differ from the unrelated ``(rel_path,
    content)`` pair also spelled ``tuple[str, str]`` — the guard against confusing
    the two is reader/annotation-level, not runtime. See PLAN.md PORT CAMPAIGN 3.
    """

    file: str
    fn_name: str


class FnSite(NamedTuple):
    """Container-qualified identity of a reconstructable function: ``(file,
    fn_name, container)``. Where :class:`FnLocator` keys the file-and-name plane
    (frontier, dispatch, leaf-ordering — one fn per name in the common case),
    ``FnSite`` disambiguates the SAME name across impls in one file: the free
    ``fn not(cond)`` (``container=""``) vs the ``Bool`` trait method ``fn not``
    (``container="impl Bool for &bool"``); the six ``fn from`` across the
    ``From<…>`` impls; the eight ``fn fmt`` across Debug/Display.

    Used by the U2 blame partition: a test that panics in a still-stubbed fn is
    only the unit's OWN failure when the panic site's ``(file, fn_name,
    container)`` matches the unit — a name-only match wrongly charges a unit for
    a same-named SIBLING's stub. Container equality must be taken canonically
    (``canon_container``) because the extractor and the impl-manifest render the
    same header with different spacing (``impl Bool for & bool`` vs ``&bool``)."""

    file: str
    fn_name: str
    container: str


@dataclass(frozen=True, slots=True)
class SkeletoniseResult:
    """Outcome of one ``--skeletonise`` extractor invocation: whether it
    succeeded, the per-file JSON reports (empty on failure), and captured stderr
    (the failure reason when ``ok`` is False). Replaces the anonymous
    ``{ok, reports, stderr}`` dict the ``Extractor.skeletonise`` port returned."""

    ok: bool
    reports: list[dict[str, Any]]
    stderr: str


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """A struct field (model.rs FieldRecord)."""
    name: str = ""
    ty: str = ""
    is_pub: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldRecord:
        return cls(name=d.get("name", ""), ty=d.get("ty", ""), is_pub=bool(d.get("is_pub", False)))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ty": self.ty, "is_pub": self.is_pub}


@dataclass(frozen=True, slots=True)
class MethodRecord:
    """A method declared on a type (model.rs MethodRecord)."""
    name: str = ""
    sig: str = ""
    is_pub: bool = False
    # Strictly `Visibility::Public` — see TypeRecord.is_exactly_pub for why
    # this is a separate field, not a redefinition of `is_pub`.
    is_exactly_pub: bool = True
    cfg_predicates: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MethodRecord:
        return cls(name=d.get("name", ""), sig=d.get("sig", ""),
                   is_pub=bool(d.get("is_pub", False)),
                   is_exactly_pub=bool(d.get("is_exactly_pub", True)),
                   cfg_predicates=_strs(d.get("cfg_predicates")))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "sig": self.sig, "is_pub": self.is_pub,
                "is_exactly_pub": self.is_exactly_pub,
                "cfg_predicates": list(self.cfg_predicates)}


@dataclass(frozen=True, slots=True)
class TypeRecord:
    """A struct / enum / type-alias / trait definition (model.rs TypeRecord)."""
    file: str = ""
    kind: str = ""           # "struct" | "enum" | "type" | "trait"
    name: str = ""
    generics: str = ""       # "<T, E>" or ""
    definition: str = ""
    fields: tuple[FieldRecord, ...] = ()
    variants: tuple[str, ...] = ()
    methods: tuple[MethodRecord, ...] = ()
    is_pub: bool = False
    # Strictly `Visibility::Public` — false for `pub(crate)`/`pub(super)`/
    # `pub(in path)`, which `is_pub` folds into True (deliberate, for
    # cross-module skeletonising callers). Defaults to True so existing
    # fixtures/tests that only set `is_pub` keep their prior meaning; the
    # extractor always emits both fields explicitly.
    is_exactly_pub: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TypeRecord:
        return cls(
            file=d.get("file", ""),
            kind=d.get("kind", ""),
            name=d.get("name", ""),
            generics=d.get("generics", ""),
            definition=d.get("definition", ""),
            fields=tuple(FieldRecord.from_dict(f) for f in (d.get("fields") or [])),
            variants=_strs(d.get("variants")),
            methods=tuple(MethodRecord.from_dict(m) for m in (d.get("methods") or [])),
            is_pub=bool(d.get("is_pub", False)),
            is_exactly_pub=bool(d.get("is_exactly_pub", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "kind": self.kind,
            "name": self.name,
            "generics": self.generics,
            "definition": self.definition,
            "fields": [f.to_dict() for f in self.fields],
            "variants": list(self.variants),
            "methods": [m.to_dict() for m in self.methods],
            "is_pub": self.is_pub,
            "is_exactly_pub": self.is_exactly_pub,
        }


@dataclass(frozen=True, slots=True)
class FnRecord:
    """A free fn / inherent or trait-impl method / trait method (model.rs FnRecord)."""
    file: str = ""              # relative path from input root
    fn_name: str = ""
    sig_text: str = ""          # verbatim, up to but excluding `{`
    body_text: str = ""         # verbatim between `{` and `}`, no braces
    sig_byte_start: int = 0
    body_byte_start: int = 0    # byte offset just AFTER the opening `{`
    body_byte_end: int = 0      # byte offset of the closing `}` (exclusive)
    cfg_predicates: tuple[str, ...] = ()
    is_pub: bool = False
    # Strictly `Visibility::Public` — see TypeRecord.is_exactly_pub for why
    # this is a separate field, not a redefinition of `is_pub`. Defaults to
    # True (matches TypeRecord's default) so existing fixtures/tests that
    # only set `is_pub` keep their prior meaning.
    is_exactly_pub: bool = True
    container: str = ""         # "" free fn | "impl X for Y" | "trait T"
    # `::`-joined inline-module path within the file ("" at file scope). Rust
    # forbids two items of one name in one module, so (file, mod_path, fn_name)
    # names at most one COMPILED fn even when the source declares several
    # variants gated by an attribute macro no cfg evaluation can decide.
    mod_path: str = ""
    has_unimplemented_body: bool = False
    is_test: bool = False       # carries a test attr (#[test]/#[tokio::test]/#[rstest]/…)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FnRecord:
        return cls(
            file=d.get("file", ""),
            fn_name=d.get("fn_name", ""),
            sig_text=d.get("sig_text", ""),
            body_text=d.get("body_text", ""),
            sig_byte_start=int(d.get("sig_byte_start", 0)),
            body_byte_start=int(d.get("body_byte_start", 0)),
            body_byte_end=int(d.get("body_byte_end", 0)),
            cfg_predicates=_strs(d.get("cfg_predicates")),
            is_pub=bool(d.get("is_pub", False)),
            is_exactly_pub=bool(d.get("is_exactly_pub", True)),
            container=d.get("container", ""),
            mod_path=d.get("mod_path", ""),
            has_unimplemented_body=bool(d.get("has_unimplemented_body", False)),
            is_test=bool(d.get("is_test", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "fn_name": self.fn_name,
            "sig_text": self.sig_text,
            "body_text": self.body_text,
            "sig_byte_start": self.sig_byte_start,
            "body_byte_start": self.body_byte_start,
            "body_byte_end": self.body_byte_end,
            "cfg_predicates": list(self.cfg_predicates),
            "is_pub": self.is_pub,
            "is_exactly_pub": self.is_exactly_pub,
            "container": self.container,
            "mod_path": self.mod_path,
            "has_unimplemented_body": self.has_unimplemented_body,
            "is_test": self.is_test,
        }


@dataclass(frozen=True, slots=True)
class ModRecord:
    """A `mod` declaration (model.rs ModRecord)."""
    name: str = ""
    file: str = ""
    cfg_predicates: tuple[str, ...] = ()
    declared_in: str = ""
    # The `mod` declaration's OWN visibility: "public" | "restricted" | "private".
    # A non-"public" mod wall makes everything behind it externally
    # unreachable regardless of the nested items' own visibility — see
    # domain/module_reachability.py.
    vis: str = "private"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModRecord:
        return cls(
            name=d.get("name", ""),
            file=d.get("file", ""),
            cfg_predicates=_strs(d.get("cfg_predicates")),
            declared_in=d.get("declared_in", ""),
            vis=d.get("vis", "private"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file": self.file,
            "vis": self.vis,
            "cfg_predicates": list(self.cfg_predicates),
            "declared_in": self.declared_in,
        }


@dataclass(frozen=True, slots=True)
class UseRecord:
    """One name a file's TOP-LEVEL `use` declarations bind (model.rs UseRecord).

    A bare type identifier does not identify a crate item on its own: with
    `use std::error::Error as StdError;` the ident `StdError` denotes std's
    trait even in a crate that also declares a private `StdError` of its own.
    These records are what `domain.rust_name_scope` resolves names against."""

    file: str = ""
    alias: str = ""             # ident bound in the file's scope; "*" for a glob
    path: tuple[str, ...] = ()  # segments as written, terminal included
    is_glob: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UseRecord:
        return cls(
            file=d.get("file", ""),
            alias=d.get("alias", ""),
            path=_strs(d.get("path")),
            is_glob=bool(d.get("is_glob", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "alias": self.alias,
            "path": list(self.path),
            "is_glob": self.is_glob,
        }


@dataclass(frozen=True, slots=True)
class CfgOccurrence:
    """One `#[cfg(...)]` / `#[cfg_attr(COND, ...)]` occurrence and the leaf atoms
    of its condition (model.rs CfgOccurrence). Emitted by `--enumerate-cfgs`;
    consumed by `domain.cluster_plan` to derive compiler clusters."""
    file: str = ""
    atoms: tuple[CfgAtom, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CfgOccurrence:
        return cls(
            file=d.get("file", ""),
            atoms=tuple(
                CfgAtom(key=a.get("key", ""), value=a.get("value"))
                for a in (d.get("atoms") or [])
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "atoms": [{"key": a.key, "value": a.value} for a in self.atoms],
        }


@dataclass(frozen=True, slots=True)
class ConstRecord:
    """A `const` / `static` item (model.rs ConstRecord)."""
    file: str = ""
    name: str = ""
    ty: str = ""
    is_pub: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConstRecord:
        return cls(
            file=d.get("file", ""),
            name=d.get("name", ""),
            ty=d.get("ty", ""),
            is_pub=bool(d.get("is_pub", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "name": self.name, "ty": self.ty, "is_pub": self.is_pub}


@dataclass(frozen=True, slots=True)
class DispatchFamily:
    """A method-dispatch family: trait + macro chain (model.rs DispatchFamily)."""
    method: str = ""
    traits: tuple[str, ...] = ()
    macros: tuple[str, ...] = ()
    trait_files: tuple[str, ...] = ()
    macro_defs: tuple[str, ...] = ()
    return_types: tuple[str, ...] = ()
    chained_methods: tuple[str, ...] = ()
    member_files: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DispatchFamily:
        return cls(
            method=d.get("method", ""),
            traits=_strs(d.get("traits")),
            macros=_strs(d.get("macros")),
            trait_files=_strs(d.get("trait_files")),
            macro_defs=_strs(d.get("macro_defs")),
            return_types=_strs(d.get("return_types")),
            chained_methods=_strs(d.get("chained_methods")),
            member_files=_strs(d.get("member_files")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "traits": list(self.traits),
            "macros": list(self.macros),
            "trait_files": list(self.trait_files),
            "macro_defs": list(self.macro_defs),
            "return_types": list(self.return_types),
            "chained_methods": list(self.chained_methods),
            "member_files": list(self.member_files),
        }


# A reconstruction frontier: maps (file_rel, fn_name) → the set of test names (or the
# sentinel "<compile>") that reach that still-broken fn at runtime/compile time. Produced
# by the TDD driver from cargo output and consumed by the per-fn / per-impl fill planners.
# It is a mapping keyed by identity, not a record with fields, so a type alias — not a
# dataclass — is its correct domain form. The key is a `FnLocator` (file, fn_name).
Frontier = dict[FnLocator, set[str]]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One rustc error from cargo's `--message-format=json` output — the cargo↔Python
    seam. Produced by `_cargo_diagnostics_from_json`; `code` is "" when rustc gave no
    error code, `file`/`line` come from the primary span ("" / 0 when absent)."""
    code: str = ""           # "E0277" or ""
    file: str = ""           # primary span file, relative; "" if none
    line: int = 0            # primary span start line; 0 if none
    message: str = ""
    rendered: str = ""       # rustc's own rendered text (may carry ANSI)
    # Unambiguous "consider importing ..." suggestions rustc attached to this
    # error — applied mechanically by the import-repair step (a body-only
    # splice cannot add `use` items, so the compiler supplies them).
    import_fixes: tuple[ImportFix, ...] = ()
    # When the error surfaces INSIDE a macro expansion, the primary span points
    # at the macro DEFINITION (`backtrace_if_absent!`'s body), never at the fn
    # that invoked it. rustc's `expansion.span` chain names each invocation
    # call-site — the real culprit a fn-granular revert must reach. Empty for a
    # non-macro error. Each entry is (file_rel, line) of one call-site.
    macro_call_sites: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Diagnostic:
        return cls(
            code=d.get("code", ""),
            file=d.get("file", ""),
            line=int(d.get("line", 0)),
            message=d.get("message", ""),
            rendered=d.get("rendered", ""),
            import_fixes=tuple(
                ImportFix(**f) for f in d.get("import_fixes", ())),
            macro_call_sites=tuple(
                (str(f), int(ln)) for f, ln in d.get("macro_call_sites", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "code": self.code,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "rendered": self.rendered,
        }
        if self.import_fixes:  # wire-compat: absent key ⇔ no fixes
            d["import_fixes"] = [
                {"file": f.file, "byte_start": f.byte_start,
                 "byte_end": f.byte_end, "replacement": f.replacement}
                for f in self.import_fixes]
        if self.macro_call_sites:  # wire-compat: absent key ⇔ no expansion
            d["macro_call_sites"] = [[f, ln] for f, ln in self.macro_call_sites]
        return d


@dataclass
class BuildCluster:
    """One compiler configuration (e.g. stable, nightly, feature/async).

    Mutable: `requires_nightly` is flipped at runtime when an E0554 is detected.
    `cfg_true`/`cfg_false` expand each bare active/excluded predicate into both the
    bare form (`N`) and the cargo-feature form (`feature = "N"`) because source code
    may use either; the extractor's CfgSet ignores keys that match no real predicate.
    """
    name: str                           # short identifier used in logs and state keys
    env_extra: dict[str, str]           # env vars merged into _cargo_env() for this cluster
    tarpaulin_exclude_files: list[str]  # --exclude-files patterns for tarpaulin
    excluded_cfg: list[str]             # cfg predicates inactive in this cluster
    active_cfg: list[str]               # cfg predicates active (on top of default)
    prompt_nightly_rule: str            # nightly boundary sentence(s) for generation prompts
    is_default: bool = True             # True for the baseline stable cluster
    gated_files: set[str] | None = None  # source files (rel paths) gated behind excluded_cfg — skip augmentation
    requires_nightly: bool = False      # cluster needs nightly toolchain (set at runtime when E0554 detected)

    def _expand(self, preds: list[str]) -> list[str]:
        out: list[str] = []
        for pred in (preds or []):
            out.append(pred)
            if "=" not in pred and "(" not in pred:
                out.append(f'feature = "{pred}"')
        return out

    @property
    def cfg_true(self) -> list[str]:
        """cfg predicates that evaluate true under this cluster (bare + feature form)."""
        return self._expand(self.active_cfg)

    @property
    def cfg_false(self) -> list[str]:
        """cfg predicates that evaluate false under this cluster (bare + feature form)."""
        return self._expand(self.excluded_cfg)

    @property
    def features(self) -> list[str]:
        """Cargo feature names enabled in this cluster: `feature = "X"` → X, plus bare
        lowercase identifiers in active_cfg treated as feature names. (Parsed with plain
        string ops — no regex — per the project's structured-parsing rule.)"""
        out: list[str] = []
        for pred in (self.active_cfg or []):
            key, eq, val = pred.partition("=")
            if eq and key.strip() == "feature":
                v = val.strip()
                if len(v) > 2 and v[0] == '"' and v[-1] == '"':   # feature = "X", X non-empty
                    out.append(v[1:-1])
            elif "=" not in pred and "(" not in pred and pred \
                    and (pred[0] == "_" or (pred[0].isalpha() and pred[0].islower())) \
                    and all(c.isalnum() or c == "_" for c in pred):
                out.append(pred)
        return out

    @property
    def toolchain(self) -> str | None:
        """Required toolchain name (e.g. "nightly") or None for the stable default."""
        return "nightly" if self.requires_nightly else None


@dataclass
class ModuleAugmentState:
    """Per-(module, cluster) augmentation lifecycle.

    Consolidates the five outer-loop state variables that used to live as parallel
    dicts (_c_retries, _c_best_covered, _c_total + _stall + _at_ceiling). Every exit
    path goes through `status()`, so adding a new termination condition is a single-case
    edit instead of a five-place edit.

    Lifecycle:
      record_pass(...) is called after each task0c_augment_module() return.
      status(crate_type) returns one of:
        'done'       — coverage 100%, or crate is proc-macro (cannot augment)
        'ceiling'    — task0c reported at_ceiling (no actionable lines remain)
        'hard_block' — stall_passes >= STALL_LIMIT (genuinely stuck)
        'retry'      — keep going
    """
    module_name: str
    cluster_name: str
    total_passes: int = 0
    best_covered_lines: int = -1
    stall_passes: int = 0    # consecutive passes with no improvement in covered count
    ceiling_passes: int = 0  # consecutive passes where task0c reported at_ceiling
    last_pct: float = 0.0
    last_covered: int = 0
    last_at_ceiling: bool = False
    target_pct: float = 100.0  # coverage % at/above which the module is 'done'

    STALL_LIMIT: int = 10
    CEILING_CONFIRM: int = 2  # consecutive at_ceiling reports required before accepting ceiling

    def record_pass(self, pct: float, covered: int, at_ceiling: bool) -> None:
        self.total_passes += 1
        self.last_pct = pct
        self.last_covered = covered
        self.last_at_ceiling = at_ceiling
        if at_ceiling:
            self.ceiling_passes += 1
        else:
            self.ceiling_passes = 0
        if covered > self.best_covered_lines:
            self.best_covered_lines = covered
            self.stall_passes = 0
        else:
            self.stall_passes += 1

    def status(self, crate_type: str) -> str:
        if self.last_pct >= self.target_pct:
            return 'done'
        if crate_type == 'proc-macro':
            return 'done'
        if self.ceiling_passes >= self.CEILING_CONFIRM:
            return 'ceiling'
        if self.stall_passes >= self.STALL_LIMIT:
            return 'hard_block'
        return 'retry'


@dataclass(frozen=True, slots=True)
class FillResult:
    """Outcome of one per-fn fill unit (R3.1; P2 de-closure of
    `_process_one_stub`). The unit returns this instead of mutating the
    pass's counters through a closure — the dispatcher aggregates under its
    own lock.

    attempts counts the LLM attempts this fn consumed (0 when skipped before
    any model call). `error` carries the exhaustion message destined for the
    pass's errors list; None for success, skips, and silent early exits."""
    file: str = ""
    fn_name: str = ""
    filled: bool = False
    attempts: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FnFixContext:
    """The per-fn prompt context the fill engine assembles before each model
    call (P2): the impl where-clause, the TYPES_IN_SCOPE section, and the
    relevance-filtered model notes. A pure value object so the fill use-cases
    can type the `PromptContext.gather(...)` port result without importing the
    static-analysis machinery that derives it (that lives behind the port)."""
    container: str = ""
    impl_where: str = ""
    types_section: str = ""
    notes_section: str = ""
    bound_methods_section: str = ""
    # The crate blueprint slice relevant to this fn ("" without one).
    blueprint_section: str = ""


@dataclass(frozen=True, slots=True)
class ClusterFillResult:
    """Outcome of one cluster's phase-C fill — the typed result every fill
    engine (per-fn, per-impl, TDD driver) returns end-to-end (plan #77 T1;
    the legacy dict bridge is gone)."""
    fns_filled: int = 0
    bytes_filled: int = 0
    attempts: int = 0
    errors: tuple[str, ...] = ()
    remaining: int = 0
    impls_filled: int = 0
    impls_failed: int = 0


@dataclass(frozen=True, slots=True)
class Stub:
    """The input to one per-fn fill unit (R3.2): an `unimplemented!()` body to
    reconstruct, identified by location + signature. A narrower projection than
    `FnRecord` — the fill engine only needs these four fields to locate, prompt,
    and splice. Threading this instead of bare dicts kills the `"fn"` vs
    `"fn_name"` key drift across the engine.

    `from_dict` is tolerant of BOTH a `FnRecord.to_dict()` (extra keys ignored)
    and the hand-built `{file, fn_name, container, sig_text}` fallback dict, so
    it accepts either stub-source path unchanged."""
    file: str = ""
    fn_name: str = ""
    container: str = ""
    sig_text: str = ""
    # The fn's accumulated `#[cfg(...)]` chain — lets the
    # splice's `locate_fn_by_record` cfg tier pick the RIGHT one of two
    # cfg-variant fns that share file+name+container+sig (the cap_limit pair),
    # so each variant fills into its own body instead of colliding on the first.
    cfg_predicates: tuple[str, ...] = ()
    # A BEHAVIORAL RE-FILL target — not an `unimplemented!()` stub but
    # an already-filled body that COMPILES yet whose attributed test fails (the
    # B3-kept-wrong case). The fill unit then splices with `allow_nonstub=True`
    # and seeds the prompt with the current body + failing test (fix framing),
    # so the TDD driver can CORRECT wrong bodies, not only fill stubs. Default
    # False = ordinary stub fill (all scanned stubs, unchanged).
    refill: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Stub:
        return cls(
            file=d.get("file", ""),
            fn_name=d.get("fn_name", ""),
            container=d.get("container", "") or "",
            sig_text=d.get("sig_text", "") or "",
            cfg_predicates=tuple(d.get("cfg_predicates") or ()),
        )

    @classmethod
    def from_record(cls, r: FnRecord) -> Stub:
        return cls(file=r.file, fn_name=r.fn_name,
                   container=r.container, sig_text=r.sig_text,
                   cfg_predicates=tuple(r.cfg_predicates or ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "fn_name": self.fn_name,
            "container": self.container,
            "sig_text": self.sig_text,
            "cfg_predicates": list(self.cfg_predicates),
        }

    @property
    def key(self) -> FnLocator:
        """The (file, fn_name) identity used as a frontier / ordering key."""
        return FnLocator(self.file, self.fn_name)


@dataclass(frozen=True, slots=True)
class ImplBlock:
    """One impl/trait/free-fn container grouped for the per-impl fill path (R3.2),
    mirroring the dict `_collect_impl_blocks` builds. `members`/`stub_members` are
    `FnRecord`s (threading the typed record through the impl engine — kills the
    raw-dict member access); `stub_members` is the subset with an unimplemented
    body. `container` is the FULL impl header text (incl. where-clauses, "" for
    free fns); `related_containers` are sibling blocks in the same file sharing
    this block's trait name.

    `from_dict` parses members tolerantly (FnRecord.from_dict per member), so it
    accepts the legacy block dict unchanged; `to_dict` round-trips the exact wire
    shape the existing consumers read.

    `refill_members` (block-level analog of `Stub.refill`): already-filled,
    compiling members whose attributed test still fails — a co-design correction
    target. Populated only when the caller supplies a TDD frontier + failing-test
    map; the whole point of tracking these at BLOCK (not fn) granularity is that
    two co-designed members needing joint correction (e.g. a shared private tag
    scheme two fns must agree on) reach the model in the SAME LLM call, which a
    per-fn refill can never do."""
    file: str = ""
    container: str = ""
    members: tuple[FnRecord, ...] = ()
    stub_members: tuple[FnRecord, ...] = ()
    related_containers: tuple[str, ...] = ()
    refill_members: tuple[FnRecord, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ImplBlock:
        return cls(
            file=d.get("file", ""),
            container=d.get("container", "") or "",
            members=tuple(FnRecord.from_dict(m) for m in (d.get("members") or [])),
            stub_members=tuple(FnRecord.from_dict(m) for m in (d.get("stub_members") or [])),
            related_containers=_strs(d.get("related_containers")),
            refill_members=tuple(FnRecord.from_dict(m) for m in (d.get("refill_members") or [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "container": self.container,
            "members": [m.to_dict() for m in self.members],
            "stub_members": [m.to_dict() for m in self.stub_members],
            "related_containers": list(self.related_containers),
            "refill_members": [m.to_dict() for m in self.refill_members],
        }

    @property
    def id(self) -> str:
        """Stable hashable id (file::container) — the per-block lock / frontier key."""
        return f"{self.file}::{self.container}"

    @property
    def needs_fill(self) -> bool:
        """True if this block has ANY work left — a stub to fill or a wrong-but-
        compiling member to correct. Named once so `_viable`-style checks don't
        re-derive "does this block have work" from raw truthiness in multiple
        places (one of which would otherwise miss `refill_members`)."""
        return bool(self.stub_members or self.refill_members)


__all__ = [
    "FieldRecord", "MethodRecord", "TypeRecord",
    "FnRecord", "ModRecord", "ConstRecord", "DispatchFamily", "Diagnostic",
    "BuildCluster", "ModuleAugmentState", "FillResult", "Stub", "ImplBlock",
    "FnFixContext", "ClusterFillResult",
]

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
from typing import Any


def _strs(v: Any) -> tuple[str, ...]:
    """Coerce a JSON list (or None) into a tuple of strings."""
    if not v:
        return ()
    return tuple(str(x) for x in v)


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """A struct field (model.rs FieldRecord)."""
    name: str = ""
    ty: str = ""
    is_pub: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> FieldRecord:
        return cls(name=d.get("name", ""), ty=d.get("ty", ""), is_pub=bool(d.get("is_pub", False)))

    def to_dict(self) -> dict:
        return {"name": self.name, "ty": self.ty, "is_pub": self.is_pub}


@dataclass(frozen=True, slots=True)
class MethodRecord:
    """A method declared on a type (model.rs MethodRecord)."""
    name: str = ""
    sig: str = ""
    is_pub: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> MethodRecord:
        return cls(name=d.get("name", ""), sig=d.get("sig", ""), is_pub=bool(d.get("is_pub", False)))

    def to_dict(self) -> dict:
        return {"name": self.name, "sig": self.sig, "is_pub": self.is_pub}


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

    @classmethod
    def from_dict(cls, d: dict) -> TypeRecord:
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
        )

    def to_dict(self) -> dict:
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
    container: str = ""         # "" free fn | "impl X for Y" | "trait T"
    has_unimplemented_body: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> FnRecord:
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
            container=d.get("container", ""),
            has_unimplemented_body=bool(d.get("has_unimplemented_body", False)),
        )

    def to_dict(self) -> dict:
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
            "container": self.container,
            "has_unimplemented_body": self.has_unimplemented_body,
        }


@dataclass(frozen=True, slots=True)
class ModRecord:
    """A `mod` declaration (model.rs ModRecord)."""
    name: str = ""
    file: str = ""
    cfg_predicates: tuple[str, ...] = ()
    declared_in: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> ModRecord:
        return cls(
            name=d.get("name", ""),
            file=d.get("file", ""),
            cfg_predicates=_strs(d.get("cfg_predicates")),
            declared_in=d.get("declared_in", ""),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "file": self.file,
            "cfg_predicates": list(self.cfg_predicates),
            "declared_in": self.declared_in,
        }


@dataclass(frozen=True, slots=True)
class ConstRecord:
    """A `const` / `static` item (model.rs ConstRecord)."""
    file: str = ""
    name: str = ""
    ty: str = ""
    is_pub: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> ConstRecord:
        return cls(
            file=d.get("file", ""),
            name=d.get("name", ""),
            ty=d.get("ty", ""),
            is_pub=bool(d.get("is_pub", False)),
        )

    def to_dict(self) -> dict:
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
    def from_dict(cls, d: dict) -> DispatchFamily:
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

    def to_dict(self) -> dict:
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
# dataclass — is its correct domain form.
Frontier = dict[tuple[str, str], set[str]]


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

    @classmethod
    def from_dict(cls, d: dict) -> Diagnostic:
        return cls(
            code=d.get("code", ""),
            file=d.get("file", ""),
            line=int(d.get("line", 0)),
            message=d.get("message", ""),
            rendered=d.get("rendered", ""),
        )

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "rendered": self.rendered,
        }


@dataclass
class BuildCluster:
    """One compiler configuration (e.g. stable, nightly, feature/async).

    Mutable: `requires_nightly` is flipped at runtime when an E0554 is detected.
    `cfg_true`/`cfg_false` expand each bare active/excluded predicate into both the
    bare form (`N`) and the cargo-feature form (`feature = "N"`) because source code
    may use either; the extractor's CfgSet ignores keys that match no real predicate.
    """
    name: str                           # short identifier used in logs and state keys
    env_extra: dict                     # env vars merged into _cargo_env() for this cluster
    tarpaulin_exclude_files: list       # --exclude-files patterns for tarpaulin
    excluded_cfg: list                  # cfg predicates inactive in this cluster
    active_cfg: list                    # cfg predicates active (on top of default)
    prompt_nightly_rule: str            # nightly boundary sentence(s) for generation prompts
    is_default: bool = True             # True for the baseline stable cluster
    gated_files: set | None = None      # source files (rel paths) gated behind excluded_cfg — skip augmentation
    requires_nightly: bool = False      # cluster needs nightly toolchain (set at runtime when E0554 detected)

    def _expand(self, preds: list) -> list[str]:
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
        if self.last_pct >= 100.0:
            return 'done'
        if crate_type == 'proc-macro':
            return 'done'
        if self.ceiling_passes >= self.CEILING_CONFIRM:
            return 'ceiling'
        if self.stall_passes >= self.STALL_LIMIT:
            return 'hard_block'
        return 'retry'


__all__ = [
    "FieldRecord", "MethodRecord", "TypeRecord",
    "FnRecord", "ModRecord", "ConstRecord", "DispatchFamily", "Diagnostic",
    "BuildCluster", "ModuleAugmentState",
]

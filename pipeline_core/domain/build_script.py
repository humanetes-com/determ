"""Build-script cfg facts — the deterministic authority on which `#[cfg]`
predicates a crate's `build.rs` actually turns on (Hard Rule 9: extract from
build truth, don't guess from source atoms).

A `#[cfg(foo)]` atom in source is NOT evidence that `foo` is a real build
configuration: many crates (anyhow is the canonical case) gate code on cfgs that
their `build.rs` only sets on an OLD toolchain (`rustc < 81`), only declares for
lint-checking (`cargo::rustc-check-cfg`) without ever setting, or pass only to an
isolated probe sub-compile. The cluster planner must therefore consult what the
build script *emits* — captured per toolchain by running it — not the raw source
atoms.

Pure domain: stdlib only, no I/O. The adapter runs `cargo check` and feeds the
build-script ``output`` text here; this module parses it.
"""

from __future__ import annotations

from dataclasses import dataclass


def _leading_ident(token: str) -> str:
    """The leading `[A-Za-z0-9_]` run of ``token`` (`anyhow_build_probe)` →
    `anyhow_build_probe`, `feature, values(...)` → `feature`)."""
    out: list[str] = []
    for ch in token:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            break
    return "".join(out)


@dataclass(frozen=True, slots=True)
class BuildScriptCfg:
    """What one `build.rs` run emitted, under one toolchain.

    ``set_cfgs``    — cfg names SET via ``cargo:rustc-cfg=NAME`` (active in this build).
    ``declared_cfgs`` — cfg names merely DECLARED via ``cargo:rustc-check-cfg=cfg(NAME)``
    (known-to-the-lint, but only active if also set). A cfg that is declared but
    never set on any available toolchain is build-script-controlled-and-off → not a
    reachable cluster.
    """

    set_cfgs: frozenset[str] = frozenset()
    declared_cfgs: frozenset[str] = frozenset()

    @classmethod
    def empty(cls) -> BuildScriptCfg:
        return cls()


def parse_build_script_output(text: str) -> BuildScriptCfg:
    """Parse a cargo build-script ``output`` file into a :class:`BuildScriptCfg`.

    Lines look like ``cargo:rustc-cfg=NAME`` / ``cargo::rustc-cfg=NAME`` (both the
    legacy single-colon and the 1.77+ double-colon syntax) and
    ``cargo:rustc-check-cfg=cfg(NAME, ...)``. Key=value cfgs (``feature="x"``) reduce
    to their leading ident (``feature``) — harmless, since features are planned
    separately. Plain string scan, no regex."""
    set_cfgs: set[str] = set()
    declared: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("cargo::"):
            body = line[len("cargo::"):]
        elif line.startswith("cargo:"):
            body = line[len("cargo:"):]
        else:
            continue
        directive, _, val = body.partition("=")
        directive = directive.strip()
        val = val.strip()
        if directive == "rustc-cfg":
            name = _leading_ident(val)
            if name:
                set_cfgs.add(name)
        elif directive == "rustc-check-cfg":
            inner = val[len("cfg("):] if val.startswith("cfg(") else val
            name = _leading_ident(inner)
            if name:
                declared.add(name)
    return BuildScriptCfg(set_cfgs=frozenset(set_cfgs), declared_cfgs=frozenset(declared))

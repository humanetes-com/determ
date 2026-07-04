"""Frozen per-family prompt-section records (P7).

Each builder family takes ONE of these instead of a bag of keyword args that
default to "". The fields have NO defaults: a section a call site forgets to
inject is a `TypeError` at construction — a VISIBLE hole — instead of a silent
empty string. This is the bug class P7 targets (e.g. the BoxedError fn that
silently got no TYPES_IN_SCOPE because a call site omitted `types_section`).

The records hold only the injected SECTIONS (the silent-hole risk set); the
prompt's identity (file/fn/sig/tests) and genuine config (fr_path,
allow_sig_change, attempt_history) stay explicit builder params — those are
structural and always passed, never silently empty.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PerFnSections:
    """Sections injected into the initial per-fn fill prompt."""
    macros: str
    notes: str
    container: str
    types: str
    invariants: str
    siblings: str
    crate_idioms: str
    # Methods the fn's FROZEN where-bounds make callable (corrected R2/R3,
    # debt #28). "" when no generic param is bounded by an in-scope trait.
    bound_methods: str


@dataclass(frozen=True, slots=True)
class PerFnFixSections(PerFnSections):
    """Per-fn FIX prompt sections = the initial set plus the retrospective
    guidance the fix loop accumulates on stall."""
    retrospective_guidance: str


@dataclass(frozen=True, slots=True)
class PerImplSections:
    """Sections injected into the per-impl-block prompt (container comes from the
    ImplBlock itself, so it is not a section here)."""
    types: str
    invariants: str
    notes: str
    crate_idioms: str
    dispatch_family: str
    macros: str
    siblings: str
    retrospective_guidance: str
    # Methods the impl's FROZEN where-bounds make callable (corrected R2/R3,
    # debt #28). "" when no generic param is bounded by an in-scope trait.
    bound_methods: str
    # The crate blueprint slice (P3) — the model's OWN design sections
    # relevant to this block. "" when no blueprint exists.
    blueprint: str = ""

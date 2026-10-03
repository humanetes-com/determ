"""Frozen per-family prompt-section records.

Each builder family takes ONE of these instead of a bag of keyword args that
default to "". The fields have NO defaults: a section a call site forgets to
inject is a `TypeError` at construction — a VISIBLE hole — instead of a silent
empty string. This is the bug class these records target (e.g. the BoxedError fn that
silently got no TYPES_IN_SCOPE because a call site omitted `types_section`).

The records hold only the injected SECTIONS (the silent-hole risk set); the
prompt's identity (file/fn/sig/tests) and genuine config (fr_path,
allow_sig_change, attempt_history) stay explicit builder params — those are
structural and always passed, never silently empty.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipeline_core.domain.last_error import LastErrorKind


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
    # Methods the fn's FROZEN where-bounds make callable — the bound-provided
    # trait method the model should prefer over a mis-bound free constructor.
    # "" when no generic param is bounded by an in-scope trait.
    bound_methods: str
    # The crate blueprint slice — the model's OWN design sections
    # relevant to this fn. "" when no blueprint exists.
    blueprint: str = ""
    # Harvested behaviours that demonstrate this fn's public usage — concrete RIGHT examples.
    # "" when none matched / no suite baked.
    usage_examples: str = ""
    # The behaviour book's instruction (`behaviour_must_instruction`: the
    # identity MUST and the rules aimed at this fn), rendered as an
    # instruction after the signature — not as USAGE_EXAMPLES. "" when the
    # book has nothing for this fn.
    behaviour_must: str = ""
    # Localized crash backtrace implicating this fn (faulting + origin
    # frames). "" when no binary crash points here.
    crash_evidence: str = ""
    # The bodies coupled to this fn (`domain.write_set.render_coupled`),
    # writable with it as extra <FN> blocks. "" when none, or when the
    # route cannot write extras.
    coupled: str = ""


@dataclass(frozen=True, slots=True)
class PerFnFixSections(PerFnSections):
    """Per-fn FIX prompt sections = the initial set plus the retrospective
    guidance the fix loop accumulates on stall. (Default "" only because the
    base class ends in the defaulted `blueprint` field — every construction
    site passes it explicitly.)"""
    retrospective_guidance: str = ""


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
    # Methods the impl's FROZEN where-bounds make callable — prefer a
    # bound-provided trait method over a mis-bound free constructor, the
    # E0277-thrash root. "" when no generic param is bounded by an in-scope
    # trait.
    bound_methods: str
    # The crate blueprint slice — the model's OWN design sections
    # relevant to this block. "" when no blueprint exists.
    blueprint: str = ""
    # Localized crash backtrace implicating this block's fns (faulting +
    # origin frames). "" when no binary crash points here.
    crash_evidence: str = ""
    # The PREVIOUS attempt's focused compile/test errors. "" on attempt 1.
    # Until this section existed, the fix loop's only error feedback into the
    # per-impl prompt was TYPES_IN_SCOPE re-selection from the error text — an
    # error naming only already-selected types changed nothing, so attempt 2
    # re-sent attempt 1's prompt byte-identically (same sha at 18:48:29 and
    # 18:58:59, both E0277 units of run `275772-425da59c`, 2026-08-19) and
    # bought one temperature resample of an unchanged question.
    last_error: str = ""
    # What produced the excerpt above, from the code that MEASURED the
    # failure. `UNKNOWN` (the default) means no provenance travelled with
    # it and the builder falls back to reading the prose — which is the
    # defect this field closes: the prose reader's substring and the
    # renderer's wording drifted, so every compiling crash reached the
    # prompt dressed as a rustc rejection and kept the joint-write
    # sentence that re-orders the faulting call (run `681204-b695ab3f`,
    # 2026-09-14, whose own launch carried the fix that reads it).
    last_error_kind: LastErrorKind = LastErrorKind.UNKNOWN
    # Per re-fill member, what the record supports (`domain.refill_evidence`):
    # DIRECT vs INFERRED, the attributed tests, and their failure text. "" when
    # the block has no re-fill members. Replaces a section that asserted "its
    # attributed test (see Tests below) still FAILS" over tests the prompt did
    # not carry (run `902902-daee577d`, 2026-08-20).
    refill_evidence: str = ""
    # Re-fill members whose attribution is inference only — rendered with the
    # `[REFILL?]` tag so a possibly-correct body is not declared failing.
    inferred_refill_names: frozenset[str] = frozenset()
    # Re-fill members a runtime fault's own frame named — rendered `[REFILL!]`.
    # Kept separate from the set above rather than subtracted from it because
    # the crash is what STRIPS these units of a convicting test, so they arrive
    # looking like inference and would otherwise be told to stand pat.
    crashed_refill_names: frozenset[str] = frozenset()
    # Re-fill members a callee of the faulting frame — rendered as
    # re-derive-from-types, never `[REFILL?]`.
    aimed_refill_names: frozenset[str] = frozenset()
    # The previous attempt's rejected expressions that the SIBLING_REFERENCE
    # body carries (`domain.sibling_steer`). Non-empty flips that section's
    # prefix from "follow the same structural pattern" to "this is the pattern
    # LAST_ERROR rejected under THIS block's bounds". () on attempt 1 and
    # whenever the two sections agree. Run `902902-daee577d` (2026-08-20),
    # prompt `11899c17478d`: both sections present, the sibling won, E0277 x2.
    sibling_rejected: tuple[str, ...] = ()
    # The behaviour book's instruction (`behaviour_must_instruction`: the
    # identity MUST and the rules aimed at this block's units), rendered as an
    # instruction after the stub list — not as USAGE_EXAMPLES. "" when the
    # book has nothing for them.
    behaviour_must: str = ""
    # The whole file the block lives in, as the cluster compiles it (inactive
    # cfg arms removed). The per-fn prompt always carried its file; this one
    # carried only the target block, the same-trait related blocks and other
    # files' API, and 66 of the 105 context requests per-impl fills made from
    # 2026-09-01 to 2026-09-24 asked for exactly that file (40 of them for
    # `src/error.rs`). "" leaves the prompt as it was.
    own_file: str = ""

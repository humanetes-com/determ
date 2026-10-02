"""Reconstruction-loop domain (pure — no I/O). The value object that threads
state across TASK 2-4 iterations, plus the policies that decide *what* context a
struggling iteration needs. Extracted from `main()` so the transitions are
unit-testable in isolation and the application layer holds no loose loop locals.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NamedTuple

# Why a body splice applied or was refused. Every refusal is a DISTINCT cause
# (the old blanket "splice_rejected" hid whether the model emitted a
# stub body vs the pipeline re-targeting an already-filled fn):
#   file_missing    — target file does not exist
#   read_failed     — target file unreadable
#   fn_not_found    — locator found no matching fn record
#   bad_offsets     — caller's byte range does not fit the file
#   target_not_stub — current body is already filled and allow_nonstub=False
#   body_is_stub    — the NEW body is empty or itself `unimplemented!()`
#   parse_broken    — the write would leave the file unparseable (syn reject);
#                     bytes restored to the pre-image, so the tree never persists
#                     an unparseable state (the error.rs doc-comment corruption class)
#   ambiguous_name  — a bare-name splice was asked to target a fn whose name is
#                     NOT unique in the file (e.g. `from` across the six
#                     `From<…> for …` impls, `fn not` across the `Bool` impls,
#                     `fmt` across Debug/Display). A name-only locate would pick
#                     the FIRST definition and splice a body meant for one impl
#                     into another → E0308/E0424 corruption. The caller must
#                     re-route through the container-aware locator; refusing is
#                     the only safe outcome without a container.
#   sig_not_bare    — a signature-only splice got a candidate carrying a body
#                     (any `{`); writing it in front of the body already there
#                     would define the fn twice
#   item_set_changed — the write moved the file's top-level item list, so the
#                     body escaped its own braces and declared items at file
#                     scope (E0255 against the real definition, poisoning every
#                     later fill in that file)
#   ok_xml_unescaped — committed, but only after resolving XML entities the
#                     model emitted in the body (`match &amp;mut self.state`).
#                     A distinct reason because the verbatim body was NOT valid
#                     Rust: it says the reply used the escaped rendering, which
#                     is a protocol signal worth seeing, not a plain success
#   ok_own_brace_dropped — committed, but only after removing the fn's own
#                     closing brace, which the reply restated at the end of the
#                     body. Verbatim it is short one `{`, so it closes the fn
#                     early and trips `item_set_changed`. Distinct for the same
#                     reason as the entity repair: it reports that the reply did
#                     not answer in the protocol's shape, which is worth
#                     counting per model even though the body itself was fine
SpliceReason = Literal[
    "ok", "ok_xml_unescaped", "ok_own_brace_dropped",
    "file_missing", "read_failed", "fn_not_found",
    "bad_offsets", "target_not_stub", "body_is_stub", "parse_broken",
    "ambiguous_name", "sig_not_bare", "item_set_changed", "body_redeclares_fn",
    # The fn's every stub occurrence is `#[cfg]`-gated OUT of the cluster doing
    # the splicing. Landing a body there writes code this cluster's build never
    # compiles — unverified by construction, and the seed then hands it to a
    # sibling cluster as if it had been checked.
    "not_in_cluster_cfg",
]


class FnLocation(NamedTuple):
    """Where one fn sits in a source file, plus the body found THERE.

    ``body_byte_start``/``body_byte_end`` are BYTE offsets bracketing the text
    between the fn's braces (the extractor reports `{`-end and `}`-start). They
    are only meaningful against the exact file state the enumeration read, so
    ``body_text`` is carried alongside them rather than left to the caller to
    re-derive: three call sites had independently written
    ``some_earlier_snapshot[body_byte_start:body_byte_end]``, which indexes a
    `str` by CHARACTERS with BYTE offsets and reads a stale image besides. Both
    errors yield a plausible-looking fragment spanning the end of one body and
    the start of the next signature — silently, and then written back as if it
    were the original body. Taking the body from the same read that produced
    the offsets makes that impossible instead of merely discouraged."""

    sig_byte_start: int
    body_byte_start: int
    body_byte_end: int
    sig_text: str
    body_text: str = ""


class SpliceOutcome(NamedTuple):
    """Result of splicing a body into a function: whether it applied, the
    :data:`SpliceReason` cause, and the byte delta the edit introduced. A
    ``NamedTuple`` so it stays a drop-in for the ``(ok, reason, delta)`` tuple
    callers already unpack while the fields are now named."""

    ok: bool
    reason: SpliceReason
    delta: int


class SpliceRefusal(SpliceOutcome):
    """A refused splice that carries what the parser said about the text it
    refused. Still the `(ok, reason, delta)` tuple every caller unpacks; the
    diagnostic rides beside it for the one reader that turns a refusal into
    feedback (`splice_feedback.reply_refusal_feedback`).

    A refusal used to be a bare reason. The reply that caused it was never
    shown why, so the cheapest failure to repair — a syntax slip — ended the
    unit's fix loop: run `473471-33301154` (2026-09-29), the frontier's top
    blocker answered with `x as &dyn A + B + 'static`, an ambiguous `+` in a
    cast type, was refused `parse_broken`, and got one call per fill."""

    diagnostic: str = ""

    def __new__(cls, reason: SpliceReason, diagnostic: str = "") -> SpliceRefusal:
        self = super().__new__(cls, False, reason, 0)
        self.diagnostic = diagnostic
        return self

# Crate-type constraint appended to the generation context for proc-macro crates
# so the model emits a valid macro crate (entry fns over `proc_macro::TokenStream`,
# helpers in private submodules). Policy text, not I/O — lives in the domain.
PROC_MACRO_CONSTRAINTS = """
--- RUST PROC-MACRO CRATE CONSTRAINTS (mandatory) ---
This is a Rust procedural macro crate. Strict rules apply:

1. lib.rs MUST ONLY export functions tagged with #[proc_macro_derive], #[proc_macro_attribute],
   or #[proc_macro]. NO pub structs, enums, traits, or type aliases at the crate root.

2. Proc-macro entry functions MUST use `proc_macro::TokenStream` (the stdlib crate),
   NOT `proc_macro2::TokenStream`, for their input and return types:
   CORRECT: pub fn my_derive(input: proc_macro::TokenStream) -> proc_macro::TokenStream
   WRONG:   pub fn my_derive(input: proc_macro2::TokenStream) -> proc_macro2::TokenStream

3. All helper types (structs, enums, traits) MUST live in private submodules (e.g. parse_attrs.rs,
   errors.rs). They should NOT be pub in lib.rs.

4. Inside private submodules, proc_macro2::TokenStream is fine for token manipulation.
--- END PROC-MACRO CONSTRAINTS ---
"""


@dataclass(frozen=True, slots=True)
class IterationState:
    """Immutable per-module reconstruction state, advanced once per TASK 2-4
    iteration. Replaces the loose `module_iter`/`prev_pass_count`/
    `prev_test_output`/`no_improvement_streak`/`prev_task2_source` locals."""

    iteration: int = 1
    prev_pass_count: int = 0
    prev_test_output: str = ""
    no_improvement_streak: int = 0
    prev_task2_source: str = ""

    def improved_over_prev(self, current_pass: int) -> bool:
        """True if this iteration beat the previous pass count (streak reset)."""
        return current_pass > self.prev_pass_count

    def stuck(self, threshold: int) -> bool:
        """True once the no-improvement streak has reached the escalation threshold."""
        return self.no_improvement_streak >= threshold

    def advance(self, *, current_pass: int, test_output: str,
                source_context: str) -> IterationState:
        """Return the next iteration's state: bump the counter, reset the streak
        on improvement (else increment), and carry forward the latest pass
        count / test output / generated source."""
        improved = self.improved_over_prev(current_pass)
        return IterationState(
            iteration=self.iteration + 1,
            prev_pass_count=current_pass,
            prev_test_output=test_output,
            no_improvement_streak=0 if improved else self.no_improvement_streak + 1,
            prev_task2_source=source_context,
        )


@dataclass(frozen=True, slots=True)
class WholeProjectPlan:
    """Whether (and why) a reconstruction iteration should receive the whole
    project's test suite as extra signal."""

    include: bool
    reason: str
    escalating: bool  # True when triggered by a no-improvement stall (worth logging)


def plan_whole_project_context(
    *, module_has_tests: bool, module_name: str,
    no_improvement_streak: int, threshold: int,
) -> WholeProjectPlan:
    """Decide the whole-project-context policy for one iteration: a zero-test
    module always needs it (infer the API from consumers); a stalled module gets
    it as escalation; otherwise it is omitted."""
    if not module_has_tests:
        return WholeProjectPlan(
            include=True,
            reason=(f"Module '{module_name}' has no tests. "
                    f"Infer its public interface from how dependent modules import and use it."),
            escalating=False,
        )
    if no_improvement_streak >= threshold:
        return WholeProjectPlan(
            include=True,
            reason=(f"No improvement for {no_improvement_streak} iterations on '{module_name}'. "
                    f"Full project test suite provided for additional signal."),
            escalating=True,
        )
    return WholeProjectPlan(include=False, reason="", escalating=False)

"""Authoring-time admission of a generated test block — pure domain.

Coverage augmentation writes its tests while the implementation they specify is
still on disk, so every block is checkable at the moment it is authored: its
tests must PASS on that implementation. A block that compiles but asserts
behaviour the code does not have becomes an oracle entry that no later
reconstruction can satisfy — the fns it covers can never take the clean
acceptance path, and every run re-attempts them forever. Compiling was never
evidence that an expectation is true.

Refusing such a block is not test loss: it never enters the suite at all,
exactly like a block that fails to compile. Preservation covers the tests the
oracle admitted, not expectations proven false the moment they were written.

Names compare on their last `::` segment — libtest prints
`foo::gen_tests_2::test_x` where the block declares `test_x`.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .behaviour import BehaviourSpec
from .behaviour_pinning import behaviours_pinned_by, weak_assertion_reason
from .oracle_visibility import names_private_items
from .rust_scan import attributed_test_fn_names, count_attrs, count_test_attrs
from .test_report import TestFailure, cap_at_line_boundary, parse_test_output


def _leaf(name: str) -> str:
    return name.rsplit("::", 1)[-1]


@dataclass(frozen=True, slots=True)
class BlockAdmission:
    """Verdict on one freshly authored test block, judged by running it against
    the implementation it claims to specify."""

    declared: frozenset[str]
    failures: tuple[TestFailure, ...]   # the block's OWN failing tests
    vanished: frozenset[str]            # declared, never reported, run died

    @property
    def admitted(self) -> bool:
        return not self.failures and not self.vanished

    @property
    def failed_names(self) -> frozenset[str]:
        return frozenset(_leaf(f.test_name) for f in self.failures) | self.vanished

    def detail(self, cap: int) -> str:
        """Panic output of this block's failing tests, for the fix prompt. Each
        section is capped separately so one huge body cannot crowd the others
        out of the budget."""
        if not self.failures and not self.vanished:
            return ""
        per = max(200, cap // max(1, len(self.failures) + (1 if self.vanished else 0)))
        parts = [f"---- {_leaf(f.test_name)} ----\n{cap_at_line_boundary(f.body.strip(), per)}"
                 for f in self.failures]
        if self.vanished:
            parts.append("---- no verdict (the test binary died before reporting) ----\n"
                         + ", ".join(sorted(self.vanished)))
        return "\n\n".join(parts)


def judge_block(block_text: str, output: str, *, run_ok: bool) -> BlockAdmission:
    """Judge `block_text` against one `cargo test` run's `output`.

    The run MUST be filtered to this block's own tests: the vanished rule reads
    a non-zero exit plus a missing verdict as "the binary died before reporting",
    which only holds when nothing else could have failed the run. Failures are
    matched by declared name regardless, so an unrelated test that slips through
    the filter never convicts this block.
    """
    declared = frozenset(attributed_test_fn_names(block_text))
    report = parse_test_output(output)
    mine = tuple(f for f in report.failures if _leaf(f.test_name) in declared)
    reported = ({_leaf(n) for n in report.passed}
                | {_leaf(f.test_name) for f in report.failures})
    vanished = frozenset() if run_ok else declared - reported
    return BlockAdmission(declared, mine, vanished)


def weakening_reason(before: str, after: str) -> str | None:
    """Why `after` silences a failure instead of correcting the expectation, else
    None.

    `#[ignore]` and `#[should_panic]` both turn a red test green without
    changing what the code does, and an ignored test still counts in the static
    test inventory — so it later reads as a VANISHED oracle entry rather than as
    a test that was never admitted. Dropping a test outright is allowed (that is
    just a narrower block); emptying the block is not.
    """
    for attr in ("ignore", "should_panic"):
        added = count_attrs(after, attr) - count_attrs(before, attr)
        if added > 0:
            return f"added {added} `#[{attr}]` attribute(s)"
    if count_test_attrs(before) > 0 and count_test_attrs(after) == 0:
        return "dropped every #[test] fn"
    return None


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    """What the contract gate demands of a block the oracle already admitted.

    `require_pin`: the block must pin at least one of the behaviours it was
    shown (`behaviour_pinning`); off when the file has nothing left to pin or
    the behaviour target is 0. `refuse_private`: the block may not name a
    private fn / field / type of the file (`oracle_visibility`). Both False is
    the pre-contract admission — a compiling, passing block, whatever it
    asserts and whatever it reads."""

    require_pin: bool
    refuse_private: bool


@dataclass(frozen=True, slots=True)
class ContractVerdict:
    """The contract's reading of one admitted block. `pinned` are the listed
    behaviour ids the block pins exactly; `private` the non-`pub` items it
    names; `weak` why its assertions pin no exact value (None when some test
    asserts one). `refused_reason` applies `policy`: naming a private item
    outranks a missing pin, because a private read is refused whatever the
    block asserts."""

    pinned: frozenset[str]
    private: tuple[str, ...]
    weak: str | None
    policy: AdmissionPolicy

    @property
    def refused_reason(self) -> str | None:
        if self.policy.refuse_private and self.private:
            return "names private items: " + ", ".join(self.private)
        if self.policy.require_pin and not self.pinned:
            why = self.weak or "its exact assertions match no listed observable"
            return f"pins no listed behaviour ({why})"
        return None

    @property
    def stop_word(self) -> str:
        """The log word for a second refusal of the same file/pass."""
        return "NEEDS_INTERNALS" if (self.policy.refuse_private and self.private) else "WEAK_ASSERTION"


def judge_block_contract(
    block_text: str, *,
    specs: Iterable[BehaviourSpec],
    file_source: str,
    policy: AdmissionPolicy,
) -> ContractVerdict:
    """Judge an admitted block against the test contract: pins a listed
    behaviour exactly, stays on the public API. Pure — `specs` are the
    behaviours the block was SHOWN (the file's unpinned, pinnable ones), and
    `file_source` is the file the block tests, with its gen_tests declarations
    stripped."""
    listed = tuple(specs)
    return ContractVerdict(
        pinned=behaviours_pinned_by(block_text, listed),
        private=names_private_items(block_text, file_source),
        weak=weak_assertion_reason(block_text),
        policy=policy,
    )

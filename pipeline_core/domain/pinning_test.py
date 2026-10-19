"""Whether a candidate test pins a blocking body: the end gate's retro (pure).

A body is blocking when a baked cfg-set compiles it, the crate's public API
reaches it, it is still `unimplemented!()`, and no oracle test reaches it in
the reconstruction (`domain/end_verdict`). Augmentation cannot see this one
coming: its criterion asks whether the ORIGINAL's suite executes each
function, and on run `1680589-7a6824f8` (2026-10-19) it did execute both
`impl StdError for ContextError` `provide` bodies. The rebuilt callers produce
the same observables by a route that does not call them, so every oracle test
passes and the gap only shows on the rebuilt tree.

The retro's artifact for that failure is a test that turns the blocking body
into a red test the next t2a owns. It is admitted only when two runs agree:

- on the ORIGINAL crate every test it declares builds and passes: what it
  asserts is the crate's behaviour, not a guess;
- on the REBUILT tree at least one of its tests fails with its first panic
  inside the blocking body: it reaches the body, and the body is what fails it.

A test that only reaches the body is not yet a pin: `let _ = call();` fails
on the stub and passes on ANY body that returns, a body that does nothing
included. So the candidate runs once more on the ORIGINAL crate with every
blocking body of its world emptied (`{}`), and must fail there
(`observes_blocking`): in a crate whose every other body is right, some
assertion depends on what the blocking bodies do, so no do-nothing filling
of them can turn it green.

Two measurements on run `1680589-7a6824f8`'s merged tree (2026-10-19) fixed
the shape of that run. On the original, not the rebuilt tree: with
`provide` emptied on the rebuilt tree, `assert!(request_ref::<Backtrace>(
..).is_none())` failed on the other bodies' differences while the emptied
original still passed it. The whole blocking set, not the body the rebuilt
run panicked in: the rebuilt `context()` wraps every error as the crate's
own before adding context, so a test whose original path runs one `provide`
impl panics in the other on the rebuilt tree, and emptying only that one on
the original changes nothing the test reads. A body that returns a value
has no neutral body to write generically; it is not emptied. When no
blocking body could be emptied, or that run does not build, the check is
not measured and the candidate is refused: a missing reading is not
evidence the test observes the body.

A test the original fails is withdrawn from the candidate, not held against
the file (`withdraw_original_failures`): the oracle's rule is per test, and
the behaviour emitter already withdraws a capture the original fails
(`emit_behaviour_tests._withdraw`). On run `4184066-ce008393` (2026-10-19)
the one candidate that built declared two tests; the original failed one
and passed the other, and the other passed every check on its own (the
rebuilt tree panicked inside the blocking body, the emptied original failed
it). The file-level rule refused both, and the pass ended with no pin. The
rest of the file is run again on the original and then judged. `judge_pin`
raises when the file still contains a failure that can be removed, so a
caller that skips the withdrawal cannot refuse the whole file as
`original_failed`. A file whose every test the original fails, or whose
failing test cannot be cut out, stays refused.

A test that passes on the rebuilt tree is evidence the route it took does not
reach the body there; it is refused, never admitted as a green addition. A
test that fails on the rebuilt tree somewhere else is evidence about another
body, and would put a red test on the frontier owned by the wrong unit; it is
refused too. `#[ignore]` and `#[should_panic]` are refused before either run:
a `should_panic` test passes on exactly the stub it is meant to pin.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pipeline_core.domain.oracle_admission import weakening_reason
from pipeline_core.domain.rust_scan import attributed_test_fn_names, without_test_fns

__all__ = ["PinRefusal", "PinCriterion", "PIN_CRITERIA", "TestRun", "PinJudgement",
           "check_criteria", "judge_pin", "pin_shape_refusal", "observes_blocking",
           "PinWithdrawal", "withdraw_original_failures"]


class PinRefusal(StrEnum):
    SHAPE = "shape"                              # no test, or a weakened one
    ORIGINAL_DID_NOT_RUN = "original_did_not_run"
    ORIGINAL_FAILED = "original_failed"
    REBUILT_DID_NOT_RUN = "rebuilt_did_not_run"
    REBUILT_PASSED = "rebuilt_passed"
    REBUILT_FAILED_ELSEWHERE = "rebuilt_failed_elsewhere"
    UNOBSERVED = "unobserved"                    # passes with the body emptied
    UNMEASURED = "unmeasured"                    # the emptied run gave no reading


@dataclass(frozen=True, slots=True)
class PinCriterion:
    """One check the judge applies, stated for the writer: the refusals it
    issues and the sentence that tells the writer what it demands.

    The writer's prompt renders `PIN_CRITERIA` as its judging contract. On
    run `3991945-1b7ae3e9` (2026-10-19) the prompt listed two checks (passes
    on the original, panics in the body on the rebuilt tree) while the judge
    applied a third (fails on the original with the blocking bodies
    emptied). All six candidates met the two stated checks' intent and
    discarded what the reaching call handed back; the third check's refusal
    reached attempt 3 only as an earlier candidate's reason, and that reply
    still discarded it. Every refusal belongs to exactly one criterion, so a
    check the judge gains without a sentence the writer reads fails at
    import."""

    refusals: frozenset[PinRefusal]
    statement: str


PIN_CRITERIA: tuple[PinCriterion, ...] = (
    PinCriterion(
        frozenset({PinRefusal.SHAPE}),
        "The file declares at least one `#[test]` fn, and no test carries "
        "`#[ignore]` or `#[should_panic]`."),
    PinCriterion(
        frozenset({PinRefusal.ORIGINAL_DID_NOT_RUN, PinRefusal.ORIGINAL_FAILED}),
        "It is built and run against the ORIGINAL crate: it must build, and "
        "every test in it must pass. A test that fails there is removed from "
        "the file and the rest is judged without it; when every test fails "
        "there, the file is refused. Assert only what the public API "
        "documents or what the call must do; a guessed value fails here."),
    PinCriterion(
        frozenset({PinRefusal.REBUILT_DID_NOT_RUN, PinRefusal.REBUILT_PASSED,
                   PinRefusal.REBUILT_FAILED_ELSEWHERE}),
        "It is run against the REBUILT crate: at least one test must fail "
        "with its first panic inside a blocking body (its "
        "`unimplemented!()`), because the test's calls reach it. A test that "
        "passes there, or whose first panic is somewhere else, does not "
        "count."),
    PinCriterion(
        frozenset({PinRefusal.UNOBSERVED, PinRefusal.UNMEASURED}),
        "It is run against the ORIGINAL crate once more, with every blocking "
        "body of this build world replaced by an empty body `{}`: at least "
        "one of the tests that reached a blocking body must FAIL there. So a "
        "test must assert on something the blocking body itself produces or "
        "hands back, not only on what the call that reaches it returns. A "
        "test that calls through the body and discards what comes back "
        "passes with an empty body and is refused. When no blocking body can "
        "be emptied, or that run does not build, the file is refused too."),
)



def check_criteria(criteria: tuple[PinCriterion, ...]) -> None:
    """Raise unless every `PinRefusal` is stated by exactly one criterion."""
    stated = [r for c in criteria for r in c.refusals]
    unstated = sorted(str(r) for r in set(PinRefusal) - set(stated))
    twice = sorted({str(r) for r in stated if stated.count(r) > 1})
    if unstated or twice:
        raise RuntimeError(
            "every PinRefusal must be stated by exactly one pin criterion: "
            f"unstated {unstated}, stated twice {twice}")


check_criteria(PIN_CRITERIA)


@dataclass(frozen=True, slots=True)
class TestRun:
    """One `cargo test --test <file>` run, read: whether it built and
    reported, and which of the file's tests passed and failed (leaf names)."""

    __test__ = False  # a value, not a pytest class

    ran: bool
    passed: frozenset[str] = frozenset()
    failed: frozenset[str] = frozenset()
    excerpt: str = ""


@dataclass(frozen=True, slots=True)
class PinJudgement:
    admitted: bool
    refusal: PinRefusal | None = None
    detail: str = ""
    # The tests that fail on the rebuilt tree inside the blocking body.
    pinning: frozenset[str] = frozenset()


def pin_shape_refusal(text: str) -> PinJudgement | None:
    """Refuse a candidate before any run: no `#[test]`, or a weakened one."""
    declared = frozenset(attributed_test_fn_names(text))
    if not declared:
        return PinJudgement(False, PinRefusal.SHAPE, "the file declares no #[test] fn")
    weak = weakening_reason("", text)
    if weak:
        return PinJudgement(False, PinRefusal.SHAPE, weak)
    return None


@dataclass(frozen=True, slots=True)
class PinWithdrawal:
    """The candidate without the tests the original failed: the text that is
    judged instead, the tests taken out, and the reason the writer is shown
    for them."""

    text: str
    withdrawn: frozenset[str]
    detail: str


def withdraw_original_failures(text: str, original: TestRun) -> PinWithdrawal | None:
    """The candidate with every test the original run FAILED removed, or None
    when there is nothing to withdraw: the run gave no reading, no declared
    test failed, every declared test failed (nothing is left to judge; the
    file stays `original_failed`), or the failed tests cannot be removed
    without touching another test (`without_test_fns`).

    Only measured failures are withdrawn. A declared test the run did not
    report is not evidence of anything; it stays in the file and the
    judge's `declared <= original.passed` refuses it after the re-run."""
    if not original.ran:
        return None
    declared = frozenset(attributed_test_fn_names(text))
    failed = declared & original.failed
    if not failed or failed == declared:
        return None
    rest = without_test_fns(text, failed)
    if rest is None:
        return None
    names = ", ".join(sorted(failed))
    return PinWithdrawal(
        rest, failed,
        f"on the original crate {names} did not pass, so {names} was removed "
        "from the file and the rest was judged without it: " + original.excerpt)


def judge_pin(text: str, original: TestRun, rebuilt: TestRun,
              body_hits: frozenset[str]) -> PinJudgement:
    """Admit or refuse. `body_hits` are the rebuilt run's tests whose first
    panic lies inside the blocking body (the caller resolves panic sites to
    sites with the extractor's records of the rebuilt tree).

    A file that still contains a test the original failed, when that test can
    be removed without touching another, is not a judgement. The caller
    removes it, re-runs the original, and judges what remains. Returning
    `original_failed` for that file is how a skipped withdrawal refuses the
    tests that passed, so this raises instead."""
    shape = pin_shape_refusal(text)
    if shape is not None:
        return shape
    if withdraw_original_failures(text, original) is not None:
        raise RuntimeError(
            "a test the original failed can be removed from the file; "
            "judge_pin refuses the file only when every test failed or "
            "the failing test cannot be cut out")
    declared = frozenset(attributed_test_fn_names(text))
    if not original.ran:
        return PinJudgement(False, PinRefusal.ORIGINAL_DID_NOT_RUN,
                            "the file did not build or run on the original crate: "
                            + original.excerpt)
    if not declared <= original.passed:
        missing = sorted(declared - original.passed)
        return PinJudgement(False, PinRefusal.ORIGINAL_FAILED,
                            f"on the original crate {', '.join(missing)} did not pass: "
                            + original.excerpt)
    if not rebuilt.ran:
        return PinJudgement(False, PinRefusal.REBUILT_DID_NOT_RUN,
                            "the file did not build or run on the rebuilt tree: "
                            + rebuilt.excerpt)
    failing = declared & rebuilt.failed
    if not failing:
        return PinJudgement(False, PinRefusal.REBUILT_PASSED,
                            "every test passed on the rebuilt tree, so none reaches "
                            "the blocking body there")
    pinning = failing & body_hits
    if not pinning:
        return PinJudgement(False, PinRefusal.REBUILT_FAILED_ELSEWHERE,
                            f"{', '.join(sorted(failing))} failed on the rebuilt tree "
                            "with the first panic outside the blocking body: "
                            + rebuilt.excerpt)
    return PinJudgement(True, None, "", pinning)


def observes_blocking(pinning: frozenset[str], emptied: TestRun) -> bool | None:
    """Whether a pinning test (it passed on the original) fails on the
    original with the blocking bodies emptied. None when that run gave no
    reading (did not build, reported no test, nothing could be emptied).
    None is not a pass: the caller refuses the candidate."""
    if not emptied.ran:
        return None
    return bool(pinning & emptied.failed)

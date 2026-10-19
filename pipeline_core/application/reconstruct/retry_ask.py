"""What one task 3 per-fn ask is told about the body it asks for.

The retry used to learn that from the whole cargo stream:
`resolve_kind(None, focus_cargo_errors(output, file))`, then always the fix
prompt. On run `641699-ae684bdb` (2026-10-19) the tree had run, the one red
test had panicked in the `unimplemented!()` of `provide` under `impl
StdError for ContextError<C, Error>`, and the ask for that definition opened
"Previous attempt broke cargo check" over a `<COMPILE_ERRORS>` block holding
the `ui` trybuild harness's `error[E0599]`, a test the same retry had
already excluded as not body-repairable. `focus_cargo_errors` keeps rustc
blocks only, so the panic was not in it, and `kind_from_text` read the
E0599 as a compile failure. The sibling `<C, E>` definition, whose body was
right, got the same excerpt.

The fill loop already answers this question with one mapping
(`fill_rules.seed_attempt_failure` then `fix_prompt_kind`): a stub has not
failed, so its first ask is the stub prompt; a compiling body's first ask
is a TEST seed with no run of it quoted (UNPASSED); a measured attempt is
quoted with its own failure. :func:`plan_retry_ask` routes task 3 through
that mapping, grades a compiling body by `refill_evidence.site_attribution`
(the per-definition rule the fill routes use), and turns a body the accept
gate reverted into the next ask's evidence (:func:`rejection_of`).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pipeline_core.application.reconstruct.fill_rules import (
    AttemptFailure,
    fix_prompt_kind,
    is_unimplemented_body,
    seed_attempt_failure,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.last_error import LastErrorKind
from pipeline_core.domain.models import FnLocator, FnSite
from pipeline_core.domain.refill_evidence import (
    Attribution,
    RefillEvidence,
    observed_stub_excerpts,
    render_refill_evidence,
    render_test_excerpts,
    site_attribution,
    sweep_excerpts,
)
from pipeline_core.domain.refill_stamp import convicting_tests_for_member
from pipeline_core.domain.retry_outcome import BodyJudgement
from pipeline_core.domain.test_report import failure_evidence_by_test


class AskForm(Enum):
    """Which builder the ask uses."""

    STUB = "stub"   # build_per_fn_prompt: the body on disk has never run
    FIX = "fix"     # build_per_fn_fix_prompt


@dataclass(frozen=True, slots=True)
class RejectedAttempt:
    """A body this retry wrote and the accept gate reverted, with how it
    failed and the words of the cluster that refused it. After the revert
    the file holds the pre-image again, so this record is the only place the
    body and its diagnostic survive."""

    body: str
    failure: AttemptFailure   # COMPILE or TEST: what the refusing cluster saw
    diagnostic: str
    verdict: str


@dataclass(frozen=True, slots=True)
class RetryAsk:
    """Everything the prompt claims about the target body.

    `excerpt` is the OBSERVED section for a STUB ask and the LAST_ERROR /
    COMPILE_ERRORS / FAILING_TESTS body for a FIX ask. `attribution` is set
    only on a first ask over a body the sweep graded; a retry of a rejected
    attempt carries the attempt's own failure instead."""

    form: AskForm
    kind: LastErrorKind
    excerpt: str
    prev_body: str
    attribution: Attribution | None = None
    history: tuple[dict[str, Any], ...] = ()


def plan_retry_ask(
    *,
    site: FnSite,
    body_on_disk: str,
    homonyms: Sequence[str],
    ran: bool,
    compile_excerpt: Callable[[], str],
    frontier: FailureFrontier | None,
    rejected: Sequence[RejectedAttempt] = (),
    read_source: Callable[[str], str | None] | None = None,
    max_tests: int = 6,
) -> RetryAsk:
    """The ask for one definition, from what was measured about it.

    In order: a body this retry already wrote and had reverted is asked
    again with that body and its refusal; a sweep that produced no verdicts
    is a compile reading (`compile_excerpt`, called only then); a stub is
    asked as a stub, with what the failing tests that run past every stub
    printed about it; a compiling body is graded, DIRECT when a test's own
    failure convicts this definition and NAMED otherwise, and shown the
    tests that grade rests on."""
    if rejected:
        last = rejected[-1]
        return RetryAsk(
            form=AskForm.FIX,
            kind=fix_prompt_kind(last.failure, quoted_run=True),
            excerpt=last.diagnostic,
            prev_body=last.body,
            history=tuple(
                {"attempt": i + 1, "body": r.body, "error_sig": r.diagnostic,
                 "outcome": r.verdict}
                for i, r in enumerate(rejected[:-1])))
    if not ran:
        return RetryAsk(
            form=AskForm.FIX,
            kind=fix_prompt_kind(AttemptFailure.COMPILE, quoted_run=True),
            excerpt=compile_excerpt(), prev_body=body_on_disk)
    loc = FnLocator(site.file, site.fn_name)
    if is_unimplemented_body(body_on_disk, site.fn_name):
        return RetryAsk(
            form=AskForm.STUB, kind=LastErrorKind.UNKNOWN,
            excerpt=observed_stub_excerpts(
                (loc,), frontier, read_source=read_source,
                max_tests=max_tests),
            prev_body=body_on_disk)
    grade = site_attribution(frontier, site.file, site.fn_name,
                             site.container, homonyms)
    if frontier is None:
        tests: tuple[str, ...] = ()
    elif grade is Attribution.DIRECT:
        tests = tuple(sorted(convicting_tests_for_member(
            frontier, site.file, site.fn_name, site.container,
            homonyms)))[:max_tests]
    else:
        tests = tuple(sorted(frontier.tests_for_body(
            site.file, site.fn_name, site.container)))[:max_tests]
    # A test held in a stub says "not implemented" about that stub and
    # nothing about this body, so its words are not this body's evidence.
    said = tuple(t for t in tests if frontier is not None
                 and t.rsplit("::", 1)[-1] not in frontier.held)
    evidence = RefillEvidence(
        fn_name=site.fn_name, tests=tests, attribution=grade,
        sibling_containers=(),
        excerpts=sweep_excerpts(said, frontier, read_source=read_source))
    return RetryAsk(
        form=AskForm.FIX,
        kind=fix_prompt_kind(
            seed_attempt_failure(refill=True, crash_implicated=False),
            quoted_run=False),
        excerpt=render_refill_evidence((evidence,)),
        prev_body=body_on_disk,
        attribution=grade)


def rejection_of(
    judgement: BodyJudgement, *, body: str, file_rel: str,
    focus: Callable[[str, str], str], unrepairable: frozenset[str] = frozenset(),
) -> RejectedAttempt:
    """The refusal of `body` in the words of the cluster that refused it.

    A cluster that stopped producing verdicts (or gained compile errors while
    broken) is a COMPILE failure, its diagnostic focused on `file_rel`; a
    cluster that ran and gained failing tests is a TEST failure, quoted as
    those tests' own panics. `unrepairable` (bare names) never enters the
    quote — the same exclusion the frontier applies."""
    aft, bef = judgement.after, judgement.before
    verdict = str(judgement.verdict)
    if aft is None or bef is None:
        return RejectedAttempt(body, AttemptFailure.COMPILE, "", verdict)
    head = f"[cluster {aft.cluster}] "
    if not aft.ran:
        return RejectedAttempt(
            body, AttemptFailure.COMPILE,
            head + "no test verdicts — the build failed:\n"
            + focus(aft.output, file_rel), verdict)
    new = {t.rsplit("::", 1)[-1] for t in aft.failing - bef.failing}
    new -= unrepairable
    said = [(t, text) for t, text in sorted(
        failure_evidence_by_test(aft.output).items())
        if t.rsplit("::", 1)[-1] in new]
    quoted = "\n".join(render_test_excerpts(said)) or ", ".join(sorted(new))
    return RejectedAttempt(
        body, AttemptFailure.TEST,
        head + "tests that passed before this body and fail with it:\n"
        + quoted, verdict)

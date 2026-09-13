# CLAUDE.md — conventions for code written in this repo

## Current reading — replace this block when LAST_ERROR quotes the previous pair bodies

A new session starts here, then reads `handover.md`. AGENTS.md is a symlink to this file.

Focus: the hop-pair fill of the three leftover format tests. Honoured RETROSPECTIVE_GUIDANCE is taken on run `186665-40892dbd` (fills 1–3 named as_ptr instead of by_ref). The plateau after that is still wrong body: fill 1 and fill 2 attempt 1 resubmitted `self.inner.by_ref()` on the dispatcher; LAST_ERROR carried CRASH and not the previous bodies, so "do not resubmit the same approach" had no referent. Fill 2 attempt 2 compiled a private-field access then crash-reverted with `Ref::from_raw`. The next fill's pair LAST_ERROR must contain the previous FN bodies under "Your previous attempt wrote". Off-theory: LAST_ERROR with CRASH only (no previous bodies), then the same by_ref dispatcher and five-binary crash-revert. Do not cut `PIPELINE_PARALLELISM`. Do not start entry 222.

Launch after this ships: `bash pre_run.sh` then `bash continue.sh --y --no-tail`. Driver-half on the existing filled tree. Fills stay. `--y` is the stop; do not probe pids. Never wrap in timeout. Full ON RETURN is in `handover.md`.

## Replies to the user

Chat replies are short and plain. No markdown: no headings, lists, tables, bold, fences, or backticks. Name files and symbols in ordinary words. Lead with the answer. Do not recap what the user can already see. This file and the code stay as they are.

## After every implementation, name the launch command — never probe whether a pipeline is running

A finished fix, mechanism, or gate is not finished until the reply names the exact command that starts the reading that can see it. The assistant does not pgrep, does not inspect live pids, does not curl a process list, and does not hedge with "if something is already running". The user owns the live process. `--y` on the launcher stops whatever is live; that is the stop, not a pre-check.

Pick one lever from "Pick the restart lever by the phase your fix lives in" by the phase that executes the change, and write that command (pre_run.sh first — the stamp moved). The cases:

- driver-half on the existing filled tree (dispatch, hop, attribution, charging, keep/revert, schedule, pair fill, session): `bash pre_run.sh` then `bash continue.sh --y --no-tail`. If that phase is already marked done in pipeline_state.md, `bash restart_from.sh task2a --force` instead (anyhow). Fills stay on disk.
- construction of the tree (t1.5 layout, bake, strip, FR synthesis): `bash pre_run.sh` then `bash redo.sh --full --force`. A tree built by the old code cannot measure this class.
- hardcrate recovery smoke: `bash pre_run.sh` then `bash run_hardcrate.sh`. Add `--from=STAGE` only when the fills in tmp/hardcrate are the state the reading needs.
- stop only, no relaunch: `touch /tmp/pipeline_graceful_stop`.

Never wrap any of these in timeout. `--no-tail` is required on continue.sh. The launch command is the last fact of the implementation reply, after last_commit_message.txt.

## What this file is FOR: breaking the plateau — an analysis is judged by whether the next iteration progresses

Everything in this file serves one outcome: an iteration of the loop that
moves the score. A whole iteration that lands fills and changes no verdict
is the failure this project exists to fix; the ideal iteration is ONE loop
in which every fill is dispatched and every fill cashes into verdicts. So
when the user asks for an analysis of a stall, a flat run, or a plateau,
the deliverable is NOT a narrative of what happened — it is the change (or
the named existing mechanism plus why it did not fire) that makes the next
iteration progress where the last one did not. A report the user could
produce themselves with a grep and low effort is not the deliverable; the
per-fill causality below, cross-checked against the record, is why the
analysis is worth asking for at all.

**Fill by fill, never aggregate.** A `pass=N → N` sweep is not explained
at the sweep level — "the design accepts flat sweeps" is a description,
not a finding. For EACH fill that landed and bought nothing, name which of
these it was, with the log line that establishes it:

- **mis-targeted** — the unit's attributed tests are blocked upstream, so
  no body written here could have moved them. The question that matters:
  what evidence already in the record said so before the GPU was spent,
  and why didn't dispatch read it?
- **wrong body** — the right unit, an answer that compiles and is wrong.
  The question: what was missing from the prompt (context section, crash
  evidence, sibling source), and which mechanism owns supplying it?
- **right body, unreachable verdict** — the attributed test cannot run:
  still-stubbed sibling, crashing binary, cfg-world mismatch, oracle
  miscount. A measurement defect; fixing the body was never the move.
- **answer dropped** — the model answered and a route/parser/splicer lost
  it (the `HEALTH ‼` / routing_drop class). Always a pipeline defect.

The four have different owners and different fixes, and one sweep's fills
usually mix them. The per-unit evidence is already printed — fix-loop exit
lines, the escalation's futility verdicts, `kept … blocked by
still-stubbed sibling(s)`, splice refusals — so the cost is reading, not
new instrumentation.

**The instance that motivated the rule, 2026-08-19, run pid 4169156.** 163
minutes and 26 LLM calls landed 16 compiling bodies; the sweep's verdict
vector came back byte-identical, and the first analysis offered was "flat
is expected mid-pass." The per-fill answer was one screen away: all 16
were accessor rewrites, and the escalation's own measurement one sweep
later declared every one of them futile — the convicted units sit
upstream, in a slice never yet dispatched. The finding was not "no
progress, by design"; it was "the iteration spent its entire budget on
units the record could already convict as unable to pay."

**What counts as progress, so the bar is checkable.** One of: test
verdicts moved; or the iteration dispatched units never tried before,
under evidence that says they can pay; or a defect was named and fixed
such that a walk shows the next iteration doing one of the first two. An
iteration that does none of these is the pathology, whatever the log calls
it — and an analysis that ends without a change aimed at making the next
iteration clear this bar is unfinished.

## A plan addendum is judged by whether the proposed change would work, not by whether its hashes match

When the user asks to analyze a `technical_debt.md` point and add an
addendum, the deliverable is whether the PLAN, walked through the source,
would actually close the class it names — a fix, an improvement, or a
named gap the code already shows the plan cannot close. It is not a
proofreading pass.

Cosmetic findings are not findings. A launch `code_id` that does not match
the working-tree hash, a floor field whose value is `98` rather than `93`,
a log line whose wording is off by a clause, a count that is 6 rather than
7: those are recoverable from one grep of a line the plan already cited.
They belong in an addendum only when they change whether the proposed
mechanism would fire or what it would do. If they do not, leave them out.

**The question the addendum has to answer, checkable.** For each change
the plan proposes, name the function (or the absence of one) that would
carry it, and say one of: it would close the class; it would not, because
<this path already does X / still does Y>; or the class is real and the
plan aims at the wrong owner. Then say what that means for the next
iteration — the same bar as the section above. An addendum that lists
identity nits and never walks the proposed code path is the same failure
as a stall write-up that never names a fill: a report the user could
produce themselves.

**The instance that motivated the rule, 2026-08-25.** Asked to analyze the
re-buy-class roadmap and add an addendum, the first screen of the
addendum was that a launch hash was the pre-edit identity, that the
frontier was 6 not 7, that a floor field was empty. All true, all already
on the cluster-entry line and the on-disk floor. The question that
mattered was one screen later and was the actual validation: the plan's
persistence protocol would wrap stores that already atomically write and
already resume per-premise, and would not touch the in-memory producer
dict that is still the re-buy; a confirmation gated on "dispatch a
producer rather than the seven accessors" cannot distinguish the theory
from a tree whose failing set had already moved. That is the addendum.
The hashes were not.

## A proposed lever is judged by the builder the live fill actually ran, not by a sibling channel that describes the same defect

When a stall is a wrong body, the next change is whatever would have sat
in THAT prompt, not whatever tracker entry names the same defect. The FR
file, the generated tests, the notes store, and the write-list are four
channels; a fill uses one builder; a change to a channel that builder does
not take cannot convert that fill.

**The rule, checkable.** Before proposing a prompt-contract / FR /
gen-test / notes / packing change as the lever for a recorded fill, name
(1) the prompt builder that fill used (`build_file_fill_prompt`,
`build_per_fn_prompt`, `build_per_impl_prompt`, …) and (2) the argument —
or the absence of one — that would carry the proposed content. Then one
of: it would have been in that prompt; it would not, because this builder
omits that argument; or the content was already in that prompt and the
fill still wrapped — so the missing thing is a different slot of the
**same** builder (instruction vs evidence).

**The instance, 2026-08-26, run `2932-cf516410`.** The remaining Defect 1
lever was filed as FR completeness: put `Error::msg("x").is::<&str>() ==
true` in `functional_requirements_anyhow.md`. The live constructor call
was file-first. `build_file_fill_prompt` has no `functional_req`
argument. Completing the FR file cannot enter that prompt. What that call
actually carried — quoted `test_downcast.rs`, the panic line
`bail_literal().unwrap_err().downcast::<&str>()`, `#[repr(transparent)]`
on `MessageError` — the thinking used (trace line 45 names the TypeId
wrap) and the reply still re-emitted `construct_from_std(MessageError(…))`.
The missing slot was `_plan_line` (the instruction), not another evidence
channel and not the FR file. Shipping FR completeness as the Defect 1
lever for that fill would have been the addenda-judging failure: a named
owner the live path does not call.

**Corollary: do not undo a load-bearing complement to shrink a prompt.**
The 25-fn dump is `installable_fns` added after the `PHASE_C_FILE_MAX_FNS`
check — so an escalated producer is dispatched with the table it
installs. Splitting constructors from `object_*` re-opens producers
dispatched without that table. A prompt that is large because a previous
class was closed is not a packing bug.

**The tells.** “The FR should name X” without reading
`build_file_fill_prompt`’s signature; “isolate the constructors into
their own prompt” against a file whose installable complement was the
previous session’s entire fight; “the observable is already in the quoted
test, so a write-list sentence is redundant” offered after a live fill
that had the quote and still wrapped.

## A plan's fixture is COMPUTED on the live records, never described — and a test asserts the plan's claim, not the code's output

When a plan names a checkable fixture ("on `95878`'s 27-unit frontier the
first batch of 12 contains all five `construct_from_*`"), shipping the
mechanism means RUNNING that fixture on the real inputs — the extractor's
records from the tree the loop reads, the frontier from the events, the
ordering the loop actually applies — and pasting the OUTPUT into the tracker
beside the claim. "Validated on the live cohort" followed by a list of what
the function returned is not a validation; it is the function's output read
as confirmation. The test module pins the same claim with the recorded input
as its fixture and the pre-fix behaviour as its negative control — never a
hand-made list of five units that the function is guaranteed to pass because
the test was written after the code, from the code.

**The rule, checkable.** Before writing "validated", "verified live" or
"confirmed" about a mechanism, the session must be able to point at three
things: the command that computed the plan's fixture on the live records;
its output, quoted; and the negative control — the same computation under
the old code or the old input, producing the bad outcome. Missing any one,
the sentence is "implemented, not yet validated". The test asserts the
PLAN's sentence ("the first 12 contain all six constructors"), not a
paraphrase of the implementation's rule ("builders sort first"): a test that
mirrors the code is a second copy of the same guess and fails on nothing.
And the class the fix names is checked at the contract that WRITES the
artefact — the docstring of the function that renders the definition, not
three examples that happen to work.

**The instance, 2026-08-25: three defects in one review batch, one
failure.** The escalated-producer ranking shipped with the tracker note
"validated on the live cohort (builders = `construct_from_std`/`construct`/
`construct_from_boxed`/`construct_from_context`/`context`)". That list is the
function's own output, and it is missing exactly the two wrong constructors
(`construct_from_adhoc`, `construct_from_display`) the ordering existed to
promote — the session read their absence as "correctly mid-chain callees".
The plan's fixture was never computed; when it was, one screen of Python
later, `construct_from_display` sat at slot 13 and `__dispatch_ensure` had
been DEMOTED out of the batch it was already in. The domain tests asserted
builder-first on a 6-unit toy list and could not fail. The same batch's
type-line slice verified that three UNDOCUMENTED types render their
`#[repr]` and never read the extractor's rendering contract one docstring
away (`render_item_definition`: doc comments print FIRST), so every
documented type rendered nothing. And the first attribution cut checked "the
test now has an owner" instead of "the owner is the body the verdict pins",
and attributed the dispatcher. Each check that was run was a check the code
was guaranteed to pass.

**Why a higher effort setting does not cover this.** More deliberation per
step improves the answer to the question asked; it does not make a session
run the check it decided it did not need. Deliberation applied to the wrong
question produces a confident, well-argued wrong answer — which is what
those tracker notes read like. The skipped check is always the cheap one:
compute the fixture, on real inputs, before writing the word.

**The tells.** "Validated on the live cohort" followed by a list and no
batch; a test whose fixture is a hand-made list of 3–6 units carrying the
plan's names; a tracker note quoting what the function returned rather than
what the plan predicted; "correctly ranks as …" explaining away a unit the
plan named as a target; a walk script that gets an empty join and moves on
(records enumerated from `src/` carry `error.rs`, the loop's carry
`src/error.rs` — an empty intersection is a rooting bug, not a no-op). On
any of them, run the fixture.

## The most correct fix, never the cheapest — this project is pre-launch and exists to validate a theory

The theory under test is that the loop can reconstruct the crate. Every fix
is judged by one question: after it ships, does the next reading measure the
THEORY, or does it measure the patch? A cheap fix that mutes a symptom —
filtering one bad note, special-casing one shape, suppressing one log line —
leaves the mechanism that produced it in place, so the next reading is
contaminated: a pass proves the patch papered over the instance, a failure
does not localize, and either way the theory is no better tested than before.
We are not in production; there is no uptime to protect and no reason to
prefer the small diff. Cost, diff size and LOC are NEVER the criterion for
choosing between two candidate fixes — correctness of the mechanism is. When
the correct fix wants a refactoring or a plan across several mechanisms,
plan and refactor; the final goal outranks the session's convenience.

**The rule, checkable.** When two fixes are on the table, name the mechanism
defect each one leaves alive. The right fix is the one after which the defect
CLASS cannot recur — not the one after which the observed instance stops. The
tell that the rule is being broken is the sentence "the minimal fix is …" (or
sizing a fix by its diff): minimal scopes the fix to the instance, and the
instance is never the defect. The instance that named the rule, 2026-08-24:
a stored `[E0425]` lesson ("use the construction helper that omits the
vtable") was steering escalated rewrites toward the exact wrong body holding
the pass-93 lock. The cheap fix was to filter that note from escalated
prompts. The defect was the channel's contract — notes are ordered at write
time to erase their premise ("do NOT mention the function name"), are
retirable only by compile evidence, and carry no scope — so ANY situational
workaround the model ever records becomes immortal standing guidance, and
the next one would have needed the same session over again. Fix the
contract, not the note.

This rule composes with, and does not license violating, the others in this
file: the correct fix still may not touch the crate under reconstruction,
still requires the walk + negative control, still names the reading that
validates it before launch, and still may not be a session steering the
loop at a named test or unit (the next section).

## Never steer the loop — every solution is a generic pipeline mechanism

The assistant does not aim dispatch, attribution, ranking, exhaustion, or a
prompt at a unit, test, or fill the session just diagnosed. That is
influencing the pipeline: the next score measures the steer, not whether the
loop found the body. The crate under reconstruction is already off-limits;
this is the same contamination one layer up, in the machinery that chooses
what to fill.

**The assistant does not put a failing test on a function's owner list.**
Not by naming the leftover tests. Not by writing a "generic" hop whose
purpose in this session is that those leftovers gain a new owner. Adding,
removing, or reassigning a test-to-unit edge is the pipeline's job; doing
it from a stall write-up is the steer with extra steps. If correspondence
is wrong, name the existing extractor that should have derived the owner
and why it did not fire. Do not add the edge.

**Generic means the mechanism does not know the instance.** A change is
generic only if the same function, with the same arguments, would fire for
any crate, any remaining-failure set, and any test whose source has the same
shape — including a test and crate that do not exist yet. If the fix (or
the test that asserts it) has to name a live leftover (`test_altdebug`,
`fmt.rs::debug`, `construct_from_adhoc`, "the three Debug failures at
pass 134"), it is a steer. A predicate written to convert this run's
leftovers is the same steer even when the leftover names are not in the
diff.

**The rule, checkable.** Before shipping a stall fix, name the function that
would carry it and the inputs it reads. Then one of: it is a generic
mechanism (those inputs contain no live test name, no live unit name, no
live remaining-failure set, and the change does not attach this session's
failing tests to a unit they did not already own); or it is a steer —
stop, do not ship. A test whose fixture is the live leftover names is the
same steer with a passing suite. The negative control is the same predicate
on a different crate shape, producing the bad outcome without those names.

**The instance that motivated the rule, 2026-08-31, run `451330-7e8a7ca9`
at pass 134.** Three tests still failed, all on Debug output. The session
proposed putting them on the Debug formatter's owner list so the next batch
would rewrite those bodies instead of constructors — first by naming the
three tests, then by offering a hop that would have attached them without
naming them. Both are the assistant assigning owners. The next score would
have measured that assignment, not reconstruction.

**The tells.** "Put this test on X's owner list"; a hop or ranking change
whose first customer is this run's leftovers; a match on a leftover test
or symbol from the live frontier; a plan-line MUST that names the crate's
remaining failures; any comment or test fixture whose force is the live
names rather than the shape. On any of them, stop. A stall fix that names
no leftover and still branches on a language construct (Debug, Drop, Hash)
is Hard Rule 11's catalog clause, not a pass of this section.

## Hard Rule 11 — no original source after strip; the fill crate is never the fix; the pipeline does not name a language

The original crate is read once, pre-strip, to bake artifacts and augment
tests. After strip there is no post-strip read of original source into a
prompt, a note, a write-list, a plan-line, or any other channel the fill
model sees. Prompt-visible text is never original crate source. The
implementation may differ from the original; the target is test-passing
behaviour, not fidelity, not upstream shape. Do not steer a fill toward
the original implementation. New source-derived data extends the bake,
never a post-strip read.

**The LLM that follows these rules cannot modify the crate under
reconstruction at all.** That includes anyhow and every other project the
pipeline is filling: not a body, not a test, not a one-line obvious fix,
not a "I will put it back." The crate is the measurement and the model's
job. The editable surface is the pipeline (`pipeline.py`, `pipeline_core`,
`tools`, the extractor's Rust sources, the scripts and the trackers).
This is the same ban as "The assistant does not write, delete or repair a
single line of the crate under reconstruction" below; Hard Rule 11 is why
that ban exists for the original-source half as well as the hand-edit half.

**Any solution or fix is on the pipeline, not on the repository being
filled.** It is a generic robust mechanism: the same function, with the
same arguments, would fire for any crate, any remaining-failure set, and
any test whose source has the same shape — including a crate that does
not exist yet. A change whose purpose is that this anyhow leftover, this
msg, this Debug body, would look like original anyhow is not a pipeline
fix. It is a steer plus a Hard Rule 11 violation, even when the original
body never hits the disk.

**The rule, checkable.** Before shipping a stall fix, name the pipeline
function that would carry it. Then one of: it reads no original crate
source, writes no fill-crate source, and does not know this repository's
names; or it is a Hard Rule 11 / steer — stop, do not ship. Using the
original as a private diagnostic in chat ("original msg captures at
construct, this tree's msg passes None") does not license putting that
body, or a paraphrase of it, into a prompt or into the tree.

**The instance, 2026-09-09, run `3985224-5484d810`.** A stall analysis
treated "write msg like original anyhow" as the lever for three leftover
Debug tests. That is reading the original as the answer and aiming a body
at this crate. The legal lever is the pipeline mechanism that failed to
land a body the tests already pin, without ever showing the original.

**The tells.** "Do it like original anyhow"; pasting or paraphrasing an
original body into a prompt, a note, or a MUST; editing
`tmp/<project>/`, `.skeleton-filled/`, `.skeleton/`, `.best_tree`, or the
generated tests; a fix whose first customer is this repository's leftovers.

**The pipeline does not special-case a language name the fill model is
the expert on.** The fill model is the language expert. The pipeline's
job is the gate the model cannot open: who may write, what evidence sits
in the prompt, whether a two-file change is one transaction. A stall fix
that adds `if trait != "Debug"`, a Drop hop, a Hash handler, is the
pipeline taking the language job. The next leftover of a different
construct needs a sibling of that function, and the next score measures
the catalog, not reconstruction. That is the same contamination as
writing the original body: the mechanism now knows a name from this
problem. A name the pipeline already had for a job only it can do is not
this class — mapping `{:?}` in a test's source to Debug is attribution
from syntax that names no crate fn. A function that then special-cases
Debug because this leftover was Debug is the class. The generic-mechanism
sentence above is not satisfied by avoiding original source while
branching on a keyword.

**The rule, checkable.** Before shipping a stall fix, name the function
and every language construct or crate symbol it branches on. Then one
of: it keys on a shape the record already has (a qualifier a body named,
a trait the test's source already mapped, coverage unentered, a FILE
request) and contains no language-name or crate-symbol branch; or it is
a catalog — stop, do not ship. A test whose fixture is this leftover's
trait or keyword is the same catalog with a passing suite. The negative
control is the same function on a different construct, producing the
paying outcome without that name.

**The instance, 2026-09-09, commit `b4131c0`.** A Debug dispatcher whose
body called Qual::display needed the uncalled implementer in the write
set. The function shipped was `_debug_implementer_not_yet_called` with
`if trait != "Debug"`. Muse already knew Debug is not Display; it could
not write a function the session did not list as editable. The Debug
branch closed the instance and left the class alive: the next leftover
that is Display-via-debug, or Drop, would want the next sibling. The
class-closing owner keys on a qualifier the body already named and on
the trait the test's source already mapped, and a FILE request admits
those units. No trait name is the reason the function exists.

**The tells.** `if trait != "Debug"` / `== "Display"` / `== "Drop"`; a
function named for one trait or keyword; "for Debug we should pair
debug"; a match on a leftover's language construct rather than on the
shape (a qualifier the body named, a trait the test already mapped). On
any of them, stop.

## Nothing leaves this machine

**Never transmit repository or workspace content to any external service.**
No claude.ai Artifact publishing, no gists or pastebins, no third-party APIs,
no cloud-side agents or remote sessions. This covers everything: source,
logs, prompts, spec docs, trackers, and the crate under reconstruction. The
only sanctioned LLM endpoint is the locally served model (vLLM/Ollama on
localhost).

**Why.** The pipeline's whole design assumes a closed loop on this machine —
uncommitted trees, untracked working notes, and logs that quote both. An
upload publishes whatever it carries; external services may cache or index
it, and deletion is not retrieval.

**Instead, render and share locally.** A markdown preview in the IDE, a
local mermaid render to SVG, a file path in the chat. If a preview genuinely
cannot be produced locally, say so and stop — do not fall back to an upload.

**Never reference a tracker ID in source, tests, prompts, log lines, event
payloads, code comments or COMMIT MESSAGES — suggested ones included.** That
means no `debt 91.2`, no `debt #46 point 2`, no `FR-018`, no `plan #77 T1`,
no `#82`, and no bare `91.5)` shorthand. Commit messages are in the list for
the same reason the rest are: `git log` travels with the repository and the
trackers do not, so a message opening `debt 187.3:` promises context the
reader cannot redeem — and suggested commit messages in a summary or a
handover become real ones by copy-paste, which is exactly how the pattern
propagated (a 2026-08-18 session copied the `debt N:` prefix from the
previous session's handover). State the fact the entry stood for; run ids,
dates, symbols and measured numbers are all fine.

**Why.** The person reading the file has the repository and nothing else. The
`.md` trackers are not distributed with it — `technical_debt.md`,
`handover.md` and the plan files are untracked working notes, and several have
been rewritten or archived since the IDs in the code were written. A number
that resolves to nothing is worse than no citation: it reads as a promise of
context that cannot be redeemed, and it invites the next reader to go looking
for a file that is not there.

**Instead, write the fact.** Everything a tracker entry would have supplied
belongs in the comment itself, in whatever length the point deserves:

- what the defect was, in terms of this code;
- the observation that establishes it — a measured count, a log line, a date,
  a run id, a file and symbol;
- why the fix is shaped the way it is, and what it deliberately does not do.

```python
# WRONG
# Cap the producer set (debt 91.2).

# RIGHT
# Escalating on a generic parameter names every type and therefore none:
# `impl<E> StdError for E` yielded the subject `E`, which matched 33 of the
# crate's 186 fns on the live tree (2026-08-01). Only names the crate declares
# as a struct or enum are answerable.
```

Run ids, dates, file paths, symbol names and measured numbers are all
encouraged — they are verifiable from the repository or from the logs the
repository produces. Session numbers ("session 308") are tracker references by
another name; leave them out too.

**On touch, scrub.** When you edit a file that already carries such a
reference, remove it and replace it with the fact it stood for. The trackers
keep their own numbering; the code does not mirror it.

## `technical_debt.md` holds only open work — implemented entries move to `completed_tech_debt.md`

When every point of a debt entry is implemented (or refuted / withdrawn),
move the entry **verbatim** to `completed_tech_debt.md` — never reworded,
never summarized, so its run ids and measurements stay quotable.

**The move is the rotate tool, in the same turn the last remainder closes.**
Do not cut-and-paste the span by hand. Do not leave an IMPLEMENTED / SHIPPED /
refuted / withdrawn heading in `technical_debt.md` as a standing note so a
later session will "find it" — that is unfinished archival, and it is how the
open file fills with work nobody can act on. The investigation grep for a
closed class is `completed_tech_debt.md` (and `technical_debt_august.md` for
ids 1–216).

    python tools/rotate_technical_debt.py <entry-number> [<entry-number> ...] \
        [--date YYYY-MM-DD] [--force]

`<entry-number>` is the integer in the top-level `# <N> — ...` heading.
Re-running an already-archived id prints `skip <N>: already archived` and
touches neither file. Mechanics, the OPEN-marker refusal, range headings and
`--force` are the next section ("Archiving a closed technical_debt.md entry").

An entry with ANY open remainder — an unshipped sub-item, an open question, a pending
live validation — stays in `technical_debt.md` until the remainder closes or
is split into its own entry. A validation/amendment section moves with the
entry it validates. Group headings whose children split (some shipped, some
open) stay in `technical_debt.md` while any child does; copy the intro into
the archive alongside the moved children.

**The assistant does not write, delete or repair a single line of the crate under
reconstruction.** That covers `tmp/<project>/src` (the merged tree), every
`.skeleton-filled/<cluster>/src`, every `.skeleton/<cluster>/src`, `.best_tree`,
and the generated tests — everything the pipeline authors. No exception for "it is
one obvious line", "the loop is stuck on it", or "I will put it back". The editable
surface is `pipeline.py`, `pipeline_core`, `tools`, the extractor's Rust sources,
the scripts and the trackers. Nothing else.

**Why, in three parts.** The crate IS the measurement: pass counts, verdicts, the
ratchet and every conviction describe a tree the pipeline produced, and one
hand-authored line makes every later number a statement about a tree a human
repaired — unfalsifiable and worthless as evidence that any fix works. The crate is
also the model's job: the whole point is whether the loop can reconstruct it, so
doing the loop's work for it converts an open question into a hidden assumption.
And a broken body is EVIDENCE about a pipeline defect; deleting it destroys the
reproduction before the defect is understood, which is the fastest known way to
ship a fix that addresses nothing.

**The instance, 2026-08-11, run `3337398-fbf7e143`.** The merged tree stopped
compiling on `error[E0592]`: `ErrorImpl::debug` defined twice, at
`src/error.rs:301` and `src/fmt.rs:11`, identical signature. The fix loop had
written it — `debug` exists only in `fmt.rs` in both authored layouts and both
filled cluster trees, `error.rs` carried it nowhere upstream of the merge, and the
run log holds three `[fix loop/impl] spliced src/error.rs::debug` lines against
zero in the run before. The loop then could not recover: a unit-scope fix may only
rewrite a body, never remove a definition, so every attempt re-checked to the
identical error, escalated to whole-file rewrites of `lib.rs`/`kind.rs`/`chain.rs`/
`context.rs` — none of which held the duplicate — and cycled 9 → 6 → 10 → 7 → 7 →
9 → 10 of 12 clean files across two full passes with zero scored sweeps and zero
verdict rows in 4h45m. The assistant proposed deleting the duplicate by hand. That
was the wrong instinct twice over: it would have patched the symptom of a splicer
defect, and it would have contaminated the tree whose numbers were about to be used
to judge whether the splicer fix worked.

**What to do instead — find the mechanism that already owns the removal.** The
pipeline has one for most damage, and it is usually cheaper than it looks. Here it
was the merge: `merge_cluster_outputs` passes the workspace file as
`--merge-output` only and takes its inputs exclusively from the `--cluster` paths,
so re-running task2b regenerates `src/` from cluster trees that never held the
duplicate — the pipeline removes it, by its own rule, with the provenance intact.
Other owners: the crash/layout retrigger re-stubs, heal restores `.best_tree`, the
batch ratchet restores an older tree. If no mechanism owns it, that absence IS the
finding — write the guard that stops the damage being created, and let the next run
build the tree. State the cost of the in-process route honestly (a re-merge
discards task2c's merged-tree fills, which live only in the workspace `src/`)
rather than reaching for an editor because the honest route is expensive.

## Archiving a closed technical_debt.md entry is `rotate_technical_debt.py`, never a hand cut-and-paste

This is the mechanical how of the section above. When the entry is completed,
run the tool — that is the archival. The judgment that every point is closed
happens before the invocation, by reading the entry.

    python tools/rotate_technical_debt.py <entry-number> [<entry-number> ...] \
        [--date YYYY-MM-DD] [--force]

`<entry-number>` is the integer in a top-level `# <N> — ...` heading. The
script moves the whole span from that heading to the next top-level `# `
heading — which carries every `## <N>.<M>` subsection and trailing
validation/amendment along with it, since they already live inside that
span — appends it verbatim under a dated `## Archived <date> from
technical_debt.md (entry ...)` header in `completed_tech_debt.md`, and
deletes it from `technical_debt.md`. It is idempotent (a re-run on an
already-archived id prints `skip <N>: already archived` and touches
neither file) and it refuses rather than guesses: an id that resolves to
more than one top-level heading, or to none, is left untouched in both
files, and so is one whose span still contains the word `OPEN` — that
word inside a span the caller believes is closed is worth a second read
before `--force` waves it through.

**What it deliberately will not do.** Range headings (`# 151–158 — ...`,
`# 132–140 — ...`) and the individually-numbered `##` children living
under them are not resolved by number — the entry `# 132–140` alone
contains `## 140` down through `## 89`, arbitrarily interleaved, sharing
one intro paragraph. A bare id there is ambiguous (which sibling under
the shared intro?), and per the rule above, pulling one child out from
under an open group means copying the group's intro into the archive
alongside it — a per-entry judgment call, not a mechanical span cut. Move
those by hand with Edit. The script's whole job is the mechanical half of
the operation; deciding an entry is fully closed happens before it is
invoked, by reading the entry, exactly as `tools/split_tech_debt.py` (the
one-shot, hardcoded-line-range predecessor this replaces for every case
but the range-heading one) had no way to do either.

## A deliberate destruction invalidates a measurement for EVERY consumer of it

Several mechanisms here destroy progress **on purpose**: the crash/layout
retrigger reverts filled bodies to `unimplemented!()`, the batch ratchet restores
an older tree, the merge replaces a cluster's file. Each one leaves a score behind
that no longer describes the tree. Every gate reading that score has to be told.
The gates nobody tells do not merely mis-report — **a gate that governs a RETRY
becomes a permanent lock, because the thing that would unlock it is the progress
the destruction just removed.**

**The instance that cost two days.** Run `259637-36db9aad` (2026-08-07, cluster
`stable`) resumed a tree at 81 passing. The crash retrigger fired attempt 2 of a
cap of 2, re-authored the `Error|ErrorImpl|ErrorVTable|Own|Ref|construct*|
object_*|vtable` component and reverted **56 of 178 filled bodies**. The next
sweep read 21. Three consumers of the 81 knew the drop was deliberate, and each
says so in the log:

- the batch ratchet — `breach (pass_count 21<81) NOT charged: this sweep measures
  the layout retrigger's deliberate re-stub of 56 body(ies)`;
- the stall budget — `state.stalls = 0` every iteration while
  `RefillGrace.bodies_in_flight`;
- the stub-progress exemption — a sweep that strictly shrank the stub set is not a
  plateau.

The fourth consumer was never told. `readmission_warranted` (`domain/refine.py`)
releases the disk-persisted `fully_exhausted` flags only on
`best_metric > metric_at_last_reset`, and `best_pass` in `run_phase_c_tdd` is a
monotone high-water mark — still **81**, set by the sweep *before* the re-stub. So
from iteration 4 the loop printed `109 fill(s) since the last check moved pass no
further than 81 — NOT re-admitting exhausted fn(s)` on every sweep and never
re-admitted anything again. By then `.phase_c_logs/retrospectives.json` had
convicted the only two units that can reach the ~60 missing tests —
`src/error.rs::*impl*::Error` and `src/error.rs::new::Error`. **Passing 81 needed
those two; releasing those two needed passing 81.** Nine hours, 148 landed fills,
+5 tests, and no available exit except the plateau exit at 26/110.

**The generalisation to apply before shipping any exemption.** When you write "do
not charge X, this drop was deliberate", grep for every other reader of the same
quantity in the same breath. Any comparison of the form
`progress_since(baseline)` where `baseline` is a high-water taken before an
intentional destruction is unsatisfiable *exactly when the destruction was
intentional* — which is the case the exemption was written for.

## Diagnose a stalled loop from the frontier, not from the score

The pass count cannot distinguish "filling a producer chain that has not closed
yet" from "locked out of the only fns that matter". The frontier and the exhausted
set can, and both are printed on the `[phase-c/tdd] iter N: cargo test rc=...`
line. Read them as a pair across iterations:

- healthy re-climb — `7445-00ee8a3c`, 2026-08-07: frontier **84 → 26 → 44**,
  `skipped as exhausted` 0 → 50 → 1, and iteration 3's 75-fn file-first batch took
  the score 13 → 83 in one sweep;
- deadlock — `259637-36db9aad`, same day: frontier **63 → 44 → 18 → 17**,
  `skipped as exhausted` 0 → 29 → 24 → 30, fills still landing at 39–109 an
  iteration.

The second tell is what the frontier entries *are*. In that run's last iteration
all **17 of 17** logged `kept ... (compiles; N attributed test(s) blocked by
still-stubbed sibling(s) — deferred to outer loop)`: three hours of model calls
rewriting bodies that already compiled, whose tests cannot run, kept on no
evidence. **When every candidate is blocked, the iteration is being spent on the
blocked instead of on the blockers, and the score movement is noise.**

Corollary for the retrospectives store: a conviction is a claim about a moment.
`src/error.rs::new::Error` was convicted `GENUINELY_HARD` with the guidance "the
vtable functions referenced in the body ... are not implemented in the crate", and
`object_drop`, `object_drop_front`, `object_ref`, `object_boxed`,
`object_downcast`, `object_reallocate_boxed`, `context_drop_rest`,
`context_chain_drop_rest` and `construct` were all filled on disk by the time it
mattered. Check a conviction's stated premise against the tree before believing it.

## A stop knob set to "spare GPU" is the most expensive setting in `.env` — size every stop to the reading it must not prevent

The loop has four flat-score stops, all in `.env`: the no-verdict clock
(`TDD_NO_VERDICT_SWEEPS`, `NO_VERDICT_STOP` in the log), the flat-score
GPU guard (`TDD_FLAT_SCORE_REPEATS` / `_WINDOW`, `GPU_GUARD_STOP`), the
plateau exit (`TDD_MAX_STALLS`) and its post-merge twin
(`POST_MERGE_MAX_STALLS`) — plus one empty-frontier stop
(`TDD_NO_FRONTIER_STOP`, `NO_FRONTIER_STOP`) that is not a flat-score stop
at all and is listed below because it shares the rule, and one fill-phase
wall clock (`TDD_FILL_PHASE_MAX_MINUTES`, `FILL_PHASE_STOP`) that is also
not a flat-score stop: `stall` is written only when `cargo test` finishes
an iteration, so a mid-pass fill that runs all night prints `stall=0`.
Each one exists to stop a run that is spending

GPU on fills that change no verdict, and each one is a MEASUREMENT
BOUNDARY: the reading the run was launched to take arrives at a specific
iteration, and a stop that fires before that iteration does not save the
GPU — it converts every batch already spent into nothing, because the
reading they were building toward is never taken.

**The instance, 2026-08-17 → 2026-08-20.** The question of the week was one
reading: the sweep after the first COMPLETE 43-unit escalated pass (4
batches of 12 under the dispatch cap — iters 1–4 dispatch, iter 5 reads).
With `TDD_FLAT_SCORE_REPEATS=2`, thirteen of 21 runs ended `GPU_GUARD_STOP`
at iter 2 — one batch each, ~2 h each, every one of them the accessor batch
the record already convicted as unable to pay; with
`TDD_NO_VERDICT_SWEEPS=3`, two more ended at iter 4, one of them 7 units
short of the pass (`902902-daee577d`). By the standing rule that a stopped
run stays stopped, each stop cost a session; each session shipped a code
change; each code change moved `code_id` and restarted the crawl at 0/43 —
16 code identities in four days. The one time the same code ran twice
(`396995` + `626784`) the pass completed in ~5 GPU-hours. A knob worth
~2 h per flat run bought one completed pass in four days and roughly 30
batch-hours that answered nothing. That is the multiplier: the stop's
saving is one batch; its cost is every batch before it, plus the relaunch
that re-spends them, plus the calendar.

**The rule.** Before launching a run whose purpose is to validate a
mechanism, name the iteration its reading arrives at, and check EVERY stop
against it. A stop that can fire earlier makes the validation structurally
unreachable — the run will stop, the session will diagnose the stop, and
the mechanism will be "awaiting live validation" for another day. The
arithmetic, with `B` = batches the traversal needs (`ceil(cohort /
dispatch cap)`; 43/12 → 4) and the reading at iter `B+1`:

- `TDD_NO_VERDICT_SWEEPS` — counts judged sweeps that landed bodies and
  moved no verdict, from iter 2; the `L`-th fires at iter `L+1`, BEFORE
  that iteration's dispatch. To take the pass-complete reading: `L >= B`
  (`L = B` stops exactly on that reading — 4 for the 43-unit pass). `0`
  disables. Default 3 stops 7 units short of a 4-batch pass.
- `TDD_FLAT_SCORE_REPEATS` / `TDD_FLAT_SCORE_WINDOW` — counts identical
  readings INCLUDING the entry sweep (iter 1), so the pass-complete reading
  is the `B+1`-th. To take it: `REPEATS >= B+1` and `WINDOW >= REPEATS`
  (5 / ≥5 for 43 units; 6 leaves room for one batch of a second pass).
  `REPEATS < 2` disables. `TDD_FLAT_SCORE_REQUIRE_TRAVERSAL=1` says the
  same thing structurally — the guard cannot fire mid-pass — and is the
  better setting whenever a completed pass IS the goal; it was switched off
  after three runs parked the traversal clock and burned 3.5–10 h, so
  re-enabling it re-accepts that risk unless the parked-clock class has
  been fixed (it was the subject of the 2026-08-18/19 batches).
- `TDD_MAX_STALLS` — charges only on a COMPLETED flat traversal (the
  mid-pass exemption), and closes the CLUSTER on the `L`-th charge, after
  its reading is taken. To observe `P` completed passes: `L >= P`. `0`
  disables the EXIT, not the loop: the cluster walks passes while anything
  is dispatchable, and once every frontier unit is exhausted it spins an
  EMPTY frontier — `no actionable frontier this iter but stall=N/0 —
  continuing`, ~2 iterations a second, `cargo test` cached, two events per
  iteration — until `PHASE_C_TDD_BACKSTOP` (10 000) closes the cluster
  EXHAUSTED and the flow advances to the merge (run `1246060-2339497b`,
  2026-08-21: iter 9 at 04:18 UTC, iter 3 797 at 04:51, 29 MB of
  `events.jsonl` in 23 minutes, the log rotating every 3 minutes). That
  spin was closed the same day: under `0` an undispatchable iteration now
  spends the exhausted set's bounded re-admission first
  (`no actionable frontier and no other move — re-admitting …`,
  `RETRO_RESET_MAX_ATTEMPTS` times) and then stops under
  `TDD_NO_FRONTIER_STOP` below. A human can still stop it earlier with
  `touch /tmp/pipeline_graceful_stop`.
- `TDD_NO_FRONTIER_STOP` (default 2) — the empty-frontier terminator,
  active ONLY while `TDD_MAX_STALLS=0` (with a stall budget, the stall exit
  already ends an empty frontier within `max_stalls` cached iterations).
  It fires at the `N`-th CONSECUTIVE iteration that found nothing
  dispatchable AFTER the no-other-move re-admission budget is spent, logs
  `NO_FRONTIER_STOP`, emits `phase_c.tdd.no_frontier_stop` and stops the
  RUN at the iteration boundary (`bash continue.sh` resumes the cluster;
  the cluster is NOT marked done). Nothing dispatchable means no verdict
  can move, so it prevents no reading; `0` disables it and restores the
  spin to the backstop.
- `TDD_FILL_PHASE_MAX_MINUTES` (default 0) — wall-clock bound on one TDD
  fill phase (file-first + impl + per-fn). Fires mid-fill, at the next
  `check_graceful_stop`, logs `FILL_PHASE_STOP`, emits
  `phase_c.tdd.fill_phase_stop`, and takes the process-wide graceful-stop
  event (no leftover sentinel file). Can prevent a pass-complete sweep if
  the bound is shorter than a healthy fill that must return; run
  `463713-b7c666c8` (2026-08-28/29) spent eight hours inside iter 2 at
  `stall=0/0` because stall cannot see a mid-pass fill. After dropping
  same-iteration refill-only blocks the remaining fill was ~2.5 h
  (`impl Error` alone). Do not set below that until a post-fix fill phase
  is measured. `0` disables. Minutes are read once at cluster entry
  (same as the other five) and passed to `fill_phase_clock.arm` each
  fill — not re-read from the env per iteration. The clock is a boundary
  between units and reaches no subprocess; the bound on a subprocess is
  the runner's (`adapters.process.run_bounded`), `PHASE_C_TEST_TIMEOUT`
  for everything that executes the reconstructed tree.
- `TDD_FILL_BUDGET_MINUTES` (default 0) — NOT a stop: the fill schedule's
  wall-clock budget (`domain/fill_schedule.py`), the minutes one fill's
  route runs share by weight (never-served and paying runs 2, served 1,
  re-buys 0 and deferred). An unmeasurable run — every aimed test is one
  whose binary did not complete, and no target is the crash-blamed
  locator — ranks as a re-buy (weight 0) even with no ledger row, so a
  positive budget defers it. The check before a positive budget is the
  entry sweep's own crash lines: with a crash-blamed conversion on the
  frontier, a budget would defer its only route as unmeasurable, which is
  a stop that fires too early; with no crashed binary at the sweep (run
  `3698228-901de95e`, 2026-09-08: `rc=101`, no crash line), nothing the
  budget defers is a reading. Set to 200 on that run's fill: 364 min under
  budget 0 — sessions 224, a per-fn re-buy the previous fill had bought
  flat 107, two `rebuy_flat` files 34 — and the 360-min clock ended the
  process before iter 2's sweep. It can prevent no reading: the
  sweep runs at every iteration boundary regardless, a deferred run is
  recorded `deferred` in `logs/route_ledger/` and is the NEXT fill's
  never-served candidate (a re-buy deferred twice is re-admitted with
  weight 1), and a unit, turn or session in flight always completes, so a
  fill overruns the budget by at most one unit. `0` = order only
  (blockers first, never-served before re-buys, no shares, nothing
  deferred; unmeasurable runs still execute).
  `PHASE_C_FILL_SCHEDULE=0` restores the fixed pre-schedule walk
  (sessions → pairs → file-first → per-impl → per-fn) for an A/B, and is
  the negative control of the schedule's walk tests. Read once at cluster
  entry beside the six stops; confirmed on the `[phase-c/schedule] fill
  N:` line of every fill, which prints the order, each run's reason and
  share, and the deferred list. `TDD_FILL_PHASE_MAX_MINUTES` stays the
  process STOP and must be `0` or `>= budget + the longest healthy single
  unit`.
- `SLICE_SESSION_MAX_PER_FILL` (default 0 = unbounded; `.env` sets 4) — NOT
  a stop: how many slice sessions one fill OPENS. The fill schedule
  (`domain/fill_schedule.py`, `max_per_kind`) defers the sessions beyond
  it in schedule order (`DEFERRED … kind_cap`, share 0), and a deferred
  session is the next fill's never-served candidate, so the cap prevents
  no reading — it moves the sweep that reads the fill earlier. Sized on
  run `3014175-d98ed09e` iter 3 (2026-09-08): eight packed sessions of
  14.3–50.1 min ran back to back for 4 h 18 min before the file wave
  (34 min) and the per-fn route opened. Read once at cluster entry with
  the six stops; confirmed on the cluster-entry line (`sessions-per-fill=N`)
  and on the `[phase-c/schedule] fill N:` line's deferred list.
  `TDD_FILL_PHASE_MAX_MINUTES` bounds the product of this cap and
  `SLICE_SESSION_MAX_MINUTES` (90 per session): raise it before raising
  either. It was set to 360 on the same reading — four sessions at the
  measured max (200 min) + the file wave + per-fn ≈ 270 min healthy worst
  case, 90 min over it.
- `POST_MERGE_MAX_STALLS` — the same clock for the post-merge wave (task2c),
  whose traversal is `ceil(frontier / POST_MERGE_MAX_DISPATCH)` iterations;
  same arithmetic, same `0`. Its empty-frontier branch
  (`task2_generator.py`, `kind=no_frontier_continue`) has NO named stop
  yet: under `0` it spins exactly as task2a did.
- `PHASE_C_TDD_BACKSTOP` (10000) is the only bound that should remain when
  the others are disabled; leave it.

All of these are read ONCE, at cluster entry (`phase_c_tdd.py`, the

`os.getenv` block near `UnmovedVerdicts(` / `FlatScoreGuard(`), so a change
takes effect at the next launch — and the cluster-entry line
(`driver=tdd, max_stalls=…, gpu-guard=… repeat(s) of a reading in …`) is
where the new values are confirmed before any GPU is spent. `.env` is
outside `code_id` and outside the tree stamp: a knob change restarts
nothing and needs no gate run. The same rule applies to every future stop
anyone adds: document its off switch, state which iteration it can fire at,
and put it in this list.

The tell that the rule is being broken is the sentence "the stop saved us
two hours". Ask what reading those two hours were on the way to, and
whether the run ever took it.

## A recurring finding is not a finding — check the record before diagnosing

The costliest loop in this project is not in the pipeline; it is the working
ritual around it: a run stalls → the stall is diagnosed from scratch → the
diagnosis is confirmed as "recurring" → a bespoke mechanism ships → the next
instance of the same class starts the ritual over. Measured 2026-08-14
(`scripts/pattern_census.py` over 34 runs): the frame-blind crash blamed NOBODY
in 8 runs across ten days before it was named; the four chain tests escalated
to the identical producer set in 11 consecutive runs; `test_large_alignment`
in 29 of 34 runs across 18 days. Every one of those instances was individually
"detected", discussed, and agreed to be recurring — the detection itself
recurred, and each pass through the ritual cost a session and shipped one more
run-scoped mechanism against a pathology that lives across runs.

**The rule.** Before diagnosing any stall, crash class, burn, or lockout as
new, interrogate the record first — it costs two commands:

    python scripts/pattern_census.py        # the five classes + cross-run chronics
    grep -n "<symptom>" technical_debt.md   # the entry that already names it

If the class is already recorded, the deliverables CHANGE. Do not re-derive
the diagnosis; cite the entry. Do not confirm "yes, this recurs"; the census
row already proves it, and restating it is the ritual. Do not design a sibling
mechanism; the standing consolidation program (debt 171: observability →
shadow detector → lever vocabulary → diagnostician) exists precisely so that
recurring classes get one answer — advance it, or state concretely why the
instance falls outside it. The one genuinely new question a recurrence raises
is: **why did the existing answer not fire?** That gap — a detector that
missed, a reaction scoped to the wrong lifetime, a class with no log
signature — is the finding. Everything else is re-reading.

The tells that the ritual has started: a diagnosis that re-derives from raw
logs a conclusion the tracker already states with run ids; the sentence "this
is the same issue as ..." arriving as a discovery rather than as the premise;
a proposed fix whose trigger condition matches a census class. On any of
them, stop and run the census. A new pathology no census detector catches is
real work twice over: fix it AND extend `pattern_census.py` so its next
instance is a lookup, not an investigation.

## The crate under reconstruction is not in `src/`, and a path-scoped negative proves nothing

`src/` in the workspace is a shell: after strip it holds `*.gen_tests_*.rs` and a
one-line `lib.rs`. The crate the loop builds, tests and scores is
`.skeleton-filled/<cluster>/src` — `run_phase_c_tdd`'s `target_src_dir` is
`filled_dir = project_dir / ".skeleton-filled" / cluster.name`
(`application/generators/task2_generator.py`), which is what cargo runs against,
what the task2a checkpoint stages, and what a resume picks up (the seed at cluster
entry is `if not dst.exists()`, so prior fills are never overwritten). Two siblings
complete the picture: `.skeleton/<cluster>/src` is the authored layout those bodies
answer to, and `.best_tree/<cluster>/src` is heal's durable restore tier. **All
three are tracked in the workspace repo** — its `.gitignore` is `/target/` and
`/Cargo.lock`, nothing else.

**The instance, and it cost a plan.** A debt entry concluded that the commit
`pipeline: task2a checkpoint — stable:95 passing (iter 3)` "contains no
reconstruction", on one measurement:

    $ git ls-tree -r --name-only 1f4099b -- src | grep -v gen_tests
    src/lib.rs                          # 1 line

True, and evidence of nothing. The same commit carries
`.skeleton-filled/stable/src/error.rs` at 40,645 bytes with 9 `unimplemented!()` —
25 stubs across the cluster against the 107 in the tree that replaced it — and the
`gen_tests` blobs are byte-identical between the two commits, so the counts compare
directly. The record tree was recoverable the entire time it was being written up
as lost, and "the checkpoint is broken" became the premise of the next session's
fix batch.

**The rule.** A query that returns nothing has two explanations — the thing is
absent, or the query was scoped where it never was — and only one is a finding.
Before reporting an absence, establish the path from the code that WRITES the
artefact, or drop the pathspec and enumerate whole: `git ls-tree -r <sha>` with no
`-- path`, `find` with no `-path`, `grep -r` from the repo root. It costs one
command, and it is the difference between a broken mechanism and a working one
whose output nobody had located.

**Restoring a past tree is a pair, never one directory.** t1.5 re-authors the
layout on every run, so bodies from one run do not compile against another's
skeleton: between `1f4099b` and the tree that replaced it, `ErrorImpl` lost
`pub(crate) backtrace: crate::backtrace::Backtrace`. Take `.skeleton/<cluster>` and
`.skeleton-filled/<cluster>` from the same commit or take neither. Extract with
`git archive <sha> <paths> | tar -x -C <scratch>` and copy deliberately — a
`git checkout <sha> -- <path>` overwrites the working tree, which in this workspace
is permanently dirty and holds fills newer than any commit.

**The tracking is also what makes `--restart-from-augmented` destructive.**
`RestoreFromAugmented.run` (`adapters/workspace_restore.py`) is
`git reset --hard <task0c-augmented-* tag>`, and the tag predates every fill, so the
reset DELETES those tracked dot-directories rather than leaving them alone. Measured
across the 2026-08-08 restart: 5,137 files removed, 5,067 of them
`.skeleton-filled`. The artefact survives in the object store — after such a reset,
reach a past tree by sha, never by looking at the working tree.

## A fix that silences a failure deletes whatever guard was keying on the noise

`carry_frozen_repr` (`domain/authored_layout.py`) is correct and it is what made
the above expensive. Before it, the identical re-author dropped
`#[repr(transparent)]`, `tests/test_ffi.rs` stopped compiling, the sweep read 0,
the batch ratchet convicted the tree, reverted 8 files, and **81 came back inside
40 minutes** — the loop was escaping the bad layout *through the crash*. With the
repr carried, the same bad state compiles and scores 21, so the ratchet has
nothing to convict and the loop sits in it for the full 19-iteration grace window.

So: when a change converts a loud failure into a quiet wrong state, find what was
keying on the loudness and ship its replacement **in the same change**. The
question to ask of any such fix is not "is the new state valid" but "what used to
fire here, and what fires now".

## A repo operation has a tool that owns it — find it before doing the operation by hand

`tools/` holds ~55 helpers, `scripts/` 8, the repo root ~30 user-owned
launchers. Nobody remembers that inventory, which is exactly why the rule is
mechanical rather than a matter of recall: **before performing any repo
operation by hand — saving a handover, rotating a log, archiving debt,
restoring a tree, distilling a walk, resetting a counter — run one `ls`+`grep`
for a tool that already owns it.** It costs one command:

    ls tools/ scripts/ *.sh | grep -i <operation>

**The instance, 2026-08-17.** Asked to save the handover, the assistant read
`handover.md`, saw sessions 348, 347, 346, 343 and 325 stacked newest-first,
inferred "prepend a new block", mapped the file's section structure to do it by
hand, and took a manual backup. All of that was wrong. `tools/rotate_handover.sh`
owns the operation and the convention is **archive-then-FILL-the-stub**, not
prepend: it moves the current `handover.md` into `previous_handovers.md`, writes
the next session number/date/empty sections with the pins already in the file,
is idempotent (it refuses to double-archive a session, and a second run on the
unwritten stub is a no-op), strips `<!-- PINNED:BEGIN … -->` blocks out of the
archived copy because standing reference has a different lifecycle from the
session it sits in, and mirrors them to `handover_pinned.md`. The writer does
not grep for `session N` and does not paste pins — both are how the old
overwrite lost a number or a block. Doing it by hand would have buried the
pinned block in an archive nobody loads. The five stacked sessions that made
prepend *look* like the convention were not the convention — they were evidence
the rotation had not been run in a while.

**Why this is the same rule as the path-scoped negative above, not a new one.**
Both are "establish the mechanism from the code that owns the artefact, never
from the artefact's current shape". A tree's layout does not tell you what
writes it; a file's accumulated state does not tell you how it is meant to be
written. In both cases the artefact is the *output* of a convention and reading
it backwards recovers a plausible convention that is not the real one.

**The tell** is the sentence "the file looks like it wants X" — or any plan
whose first step is to hand-edit a file the pipeline or the workflow also
writes. On that sentence, stop and grep. If no tool owns it, that absence is
worth a line in the summary, and a durable operation with no owner is a
candidate for `scripts/` under the section below.

## A script the assistant writes goes in `scripts/`, with a usage-and-interpretation header

Every durable script the assistant creates lives in `scripts/` — never the
repo root, which holds the user-owned launchers (`continue.sh`, `pre_run.sh`,
`redo.sh`, ...), and never `tools/` unless it is part of the pipeline itself
(`tools/` is inside the tree stamp, so adding a helper there invalidates the
pre-run gate for no reason; `scripts/` is outside it). One-off working files
still go to the session scratchpad, not `scripts/`.

Each script opens with a header comment that answers, in this order: WHY the
script exists (the measured fact or defect that motivated it, with run ids);
USAGE (arguments, env knobs, preconditions — especially GPU/vLLM ownership
and cost); and HOW TO INTERPRET THE OUTPUT — every field of the summary it
prints, and the decision rule the numbers feed (e.g. "place the cap above the
healthy cell's max; would-hit-cap(X)=k/N is how many healthy calls the
current cap would have broken"). A script whose output needs its author to
read is not finished. `scripts/measure_muse_reasoning.sh` is the template.

## A finished batch writes `last_commit_message.txt`; `./commit.sh` commits it

Commits are user-owned and the tree is permanently dirty: the assistant never
runs `git commit` and never checks commit state. The flip side is that the
user commits from the summary, not from a diff crawl — so **every finished
batch of changes ends by WRITING the commit message to
`last_commit_message.txt` in the repo root, overwriting whatever was there**,
and the final summary's last line says so. One message per coherent batch; if
the batch is the session's work, one message for the session. The user then
runs:

    ./commit.sh          # git commit -F last_commit_message.txt (staged only)
    ./commit.sh -a       # ... also staging tracked modifications
    ./commit.sh --amend  # ... reworking the previous commit

The file is untracked (`.gitignore`) and holds the CURRENT batch only — it is
overwritten, never appended, so it can never offer a stale message to the next
`./commit.sh`.

**Why a file and not the chat.** A message pasted out of a chat summary carries
the terminal's hard wraps into the object. `git log` then shows the subject
running into the body, ragged leading indentation and 100-column lines — the
four-point message on `abc2179` is what that looks like from the other side.
`git commit -F` takes the file verbatim, so the wrapping is the writer's
decision instead of the pane width's.

**Which makes the formatting the writer's job — and "no stray EOLs" cuts both
ways.** Write the file as git wants it structurally: a subject line, no
trailing period, then a BLANK line, then the body. Within that structure, an
EOL is only ever a STRUCTURAL boundary — the subject/body separator, or a
blank line between points — never a wrap inserted mid-paragraph to hit a
column target. A hard-wrap-at-72 rule was tried here and produced exactly the
symptom this section was written to prevent: a sentence broken by a newline
that has nothing to do with its meaning, indistinguishable from the
pane-width wrapping the file format was introduced to escape (2026-08-21). So
each point/paragraph is ONE line, however long, with no manual break inside
it; `-` or `1.` for a list, one list item per line, at the left margin, blank
lines between points. `commit.sh` normalizes the file before committing —
joining any line that isn't blank into its paragraph's single line — as a
backstop against a wrap slipping in anyway, so a stray mid-paragraph EOL costs
nothing even if one is written. The tracker-id ban above applies here as it
does everywhere else, and `commit.sh` refuses a subject that opens with one
(`debt 91.2`, `FR-018`, `#82`) rather than letting it reach `git log`.

**The subject line stays under 72 characters — it is a summary sentence, not
the seventh place the batch's points get restated.** "One line, however long"
above governs each BODY paragraph; the subject is a different object; it names
the batch, and the batch's individual points already have a home, one bullet
each, below the blank line. A subject built by concatenating every bullet's
headline with "and" defeats the point of having bullets at all: `git log
--oneline` truncates past 72 and shows nothing usable, and the subject stops
being a summary a reader can act on. `commit.sh` already warns past 72 chars
but does not refuse, so writing a short subject is the drafter's discipline to
keep, not a check to lean on.

**The instance, `975e5d4`, 2026-09-08.** The subject ran 362 characters —
"Keep a session's best judged state, re-ask a reasoning-capped empty reply and
grade it unmeasured, read the fill class off the fill reading, audit the TDD
loop's own iterations, authorize the offered installables, move the route
adapters behind the port, and cap sessions per fill, and score a hung test
binary as a regression instead of an inconclusive reading" — one clause per
body bullet, all seven of them, joined by commas and "and". The body already
carried each of those seven as its own point in full detail; the subject added
nothing the reader could not get faster from `git log -1` and cost every
`--oneline` view its usefulness on this commit. The fix is not a script
change: pick the one fact that best names the batch (or, if the batch has no
single center, the dominant change) for the subject, and leave the rest to the
bullets that already exist for it.

**No assistant attribution, ever — no `Co-Authored-By:`, no "Generated with",
no mention of Claude, Anthropic, Cursor, Grok, Fable, or 🤖.** Not in the
subject, not in the body, not as a trailer. This holds even though the tooling's
own default convention appends a `Co-Authored-By: Claude ...` line to every
commit message it drafts: that default is overridden here and the override is
not negotiable per-batch. The commits are the user's, authored under the user's
name; `git log` is the project's record of what changed and why, not of which
editor typed it. A trailer naming a model adds nothing a reader can act on,
survives forever in the object, and travels to everyone who ever clones the
repository — the same reason the tracker-id ban exists one paragraph up.
`commit.sh` refuses a message carrying any of these markers rather than
letting one reach `git log`, so the rule is mechanical and not a matter of
remembering; a refusal means rewriting `last_commit_message.txt` without the
line, never `--no-verify`-ing past it. The match is a substring, so the
conventions filename itself also refuses — name the file by its role
("the repo conventions") instead.

A batch that touched only the untracked trackers (`technical_debt.md`,
`handover.md`, the plan files — `.gitignore` is `*.md`) has nothing to commit:
say that plainly and leave `last_commit_message.txt` alone.

## Saving a handover is `rotate_handover.sh`, then FILL the stub — never grep the number, never paste pins

    bash tools/rotate_handover.sh   # archives current handover.md -> previous_handovers.md
                                    # writes handover.md as the NEXT session stub:
                                    # number, date, empty sections, pinned blocks already in the file
    # stdout names `next session N` and `pinned K` — that is the number; do not grep handover.md for it
    # then FILL the bold title and the three sections in place

`handover.md` holds ONE session plus its pinned blocks — not a stack. The
rotation is what keeps it that way, so a `load handover` reads a few hundred
lines instead of paging through superseded sessions (it had reached 274 KB and
five stacked sessions before the 2026-08-17 rotation; it is 795 lines after).
The script refuses to re-archive a header already in `previous_handovers.md`,
and a second run on the unwritten stub is a no-op, so running it twice is safe
and running it when unsure is the cheap move.

**Do not grep for the session number and do not paste pins.** Both used to be
the writer's job and both were a read of an artefact the first line / the pin
markers already carried — the number-grep is how a session opened on the wrong
N, and the paste is how a PINNED block got dropped. The script parses `session
N` off the first line, writes `session N+1`, and puts the stripped blocks back
into the stub (or, if the current file had none, the last mirror in
`handover_pinned.md`). The writer replaces `**UNWRITTEN — replace this title**`
and fills `## ON RETURN` / `## State at handover` / `## Key facts`. A full
overwrite of `handover.md` that forgets the pins is the old failure; edit the
stub.

Content between `<!-- PINNED:BEGIN name -->` and `<!-- PINNED:END name -->` is
standing reference with its own lifecycle — the score history, the standing
cautions — and archiving it with the session would bury it in a file nobody
loads. The script strips it from the archived copy, mirrors it to
`handover_pinned.md`, and leaves it in the stub. Drop the interactive-run
block only when that loop has ended.

A session block opens `# Handover — session N (date) — **<what the session
established, in bold>**` — the script hard-fails on any other first line — and
closes with `## ON RETURN`, `## State at handover`, `## Key facts` and the
suggested commit message(s) — the handover keeps its own copy of the message
for the record; the copy `./commit.sh` reads is `last_commit_message.txt`.

## The commands this repo's gate is made of

    bash pre_run.sh                 # extractor release build + check.sh + walks, then stamp
    bash pre_run.sh --walks-only    # walks alone; writes NO stamp, for the fix loop
    bash continue.sh                # refuses (exit 3) unless the stamp matches
    bash continue.sh --no-pre-run   # launch untested, printed loudly
    pytest -m walk                  # the walks alone, outside the gate
    python tools/distill_walk.py    # a run's log -> a walk fixture

`lib/tree_stamp.sh` computes the identity everything above compares on:
`pipeline_tree_stamp <root>` hashes file CONTENTS and repo-relative paths for
pipeline.py, pipeline_core, tools and the extractor's Rust sources — tests and
docs are deliberately outside it, since a new test cannot make a passing gate
stale. `pipeline_stamp_file <root>` is where the stamp lives (`.pre_run_stamp`,
untracked). Both are sourced, never executed.

## A run does not start until something has vouched for the code

`bash pre_run.sh` is the gate; `continue.sh` refuses to launch without it. Three
stages, in this order because each is unreadable while the one before it is red:

1. **extractor build** — `cargo build --release` in `tools/skeleton_extractor`.
   The stamp hashes the extractor's Rust sources and prunes `target/`; the
   pipeline executes the compiled binary. Without this stage a passing gate
   can stamp sources the running binary does not implement: run
   `955468-7b947bd0` (2026-08-29) executed a four-day-old release binary whose
   `TypeRecord.mod_path` was absent, so a shipped private-mod wall was inert
   while `code_id` and the stamp both read as current. `--walks-only` skips
   it. Building rather than warning is what makes the class unrepeatable.
2. **check.sh** — is the code correct. Static gates, the pytest suite, the
   coverage ratchet. Compiles the extractor under the pinned gate toolchain in
   the debug profile — that green line is not a statement about
   `target/release`.
3. **walks** (`pytest -m walk`) — does the loop's decision sequence go where the
   theory says. A walk replays a recorded pathological run through the real
   `run_phase_c_tdd` with cargo and the model stubbed.

Adding a walk costs pointing the distiller at the log the pathology is in — the
run's iterations, its retrigger fires and its refusals are all already printed:

    python tools/distill_walk.py --cluster stable --run 259637-36db9aad \
        logs/pipeline_anyhow.log.1 logs/pipeline_anyhow.log \
        -o tests/fixtures/walks/259637-36db9aad.json

then a test module under `tests/walks/` driving `tests.walks.harness.replay`.
The harness names the three things it MODELS rather than replays (the entry
high-water, the iteration a retrigger's `continue` swallowed, and test identity);
extend that list rather than quietly widening one of them.

On a full pass `pre_run.sh` writes `.pre_run_stamp`: the content hash of
pipeline.py, pipeline_core, tools and the extractor's Rust sources
(`lib/tree_stamp.sh`). `continue.sh` recomputes it and exits 3 when it has moved.
Git cannot play this role — runs are launched from an uncommitted tree, so
`HEAD` is identical across the edit that introduced a deadlock and the edit that
fixed it. A relaunch after a crash with no edits still matches and costs nothing.

**What a walk may assert.** A recorded walk is a FIXED observation sequence, and
it stops describing reality at the first iteration where the new code would have
done something different. It cannot tell you the run would have reached 108
passing. It can tell you a decision became reachable — re-admission fires by
iter N, the durable slot is never overwritten by a worse tree, the plateau exit
reports a number describing the tree it exits on. Assert reachability, never the
score.

**Every walk carries its negative control.** A fix-verification test that does
not fail on the pre-fix code has proved nothing. Run the same walk with the old
grading injected and assert the bad outcome reproduces. That is the difference
between "the new code is correct" and "the theory bites on run
`259637-36db9aad`", which cost nine hours, 148 landed fills and +5 tests.

## Pick the restart lever by the phase your fix lives in

Every launch goes through `continue.sh`, so **`bash pre_run.sh` comes first, always**
— `restart_from.sh` and `redo.sh` both `exec continue.sh`, which recomputes the
stamp and exits 3 when it moved. Answer this before choosing: which phase executes
the code I changed, and does validating it need a tree that has not seen it?
Name that command in the implementation reply (previous section). Do not probe
whether a pipeline is already running; `--y` is the stop.

**Every launch is detached (`--no-tail`).** `continue.sh` already `nohup`s the
pipeline; `--no-tail` makes the launcher exit instead of `tail -f`. A Cursor
terminal (or a `&` without `--no-tail`, or a `timeout` wrap) that dies SIGTERMs
the process group — run `929966-abe13d11`, 2026-08-29, 54m, exit 143. `&` is not
the lever; `--no-tail` is. Stop without relaunch: `touch /tmp/pipeline_graceful_stop`.
Stop-and-relaunch: `bash continue.sh --y --no-tail`. Never wrap in `timeout`.

**The phases, and what each lever re-enters.** `task0a/0c` clone + augment,
`strip` removes the bodies, `task1` synthesises FRs, `t1.5` re-authors the layout,
`task2a` fills per cluster (the TDD loop, `run_phase_c_tdd`), `task2b` merges,
`task2c` fills the merged tree, `task3` tests and repairs, `task4` genesis. The
fills live in `tmp/anyhow/.skeleton-filled/<cluster>/src`, the layout they answer
to in `.skeleton/<cluster>/src`.

- **`bash continue.sh`** — resume at the first phase `pipeline_state.md` does not
  mark done. Cheapest and the default; on 2026-08-10 it re-entered a killed run at
  the merge phase in under two minutes and zero model calls. It validates only what
  runs AFTER the resume point: a run that died in t3 resumes in t3, and a t2a-phase
  fix is never executed. Flags: `--y` (stop a running pipeline first), `--force`
  (SIGTERM+SIGKILL), `--no-tail` (**required** — exit after launch; omit it and
  aborting the terminal kills the run), `--no-pre-run` (bypass the
  gate, prints as the escape hatch it is). `--wipe` is gone and refuses loudly.
  Never wrap it in `timeout` — coreutils signals the whole process group and
  SIGTERMs the pipeline it just started. `continue.sh --y` stops then
  relaunches; a stop the user asked for and nothing else is the sentinel.
- **`bash restart_from.sh <stage>`** (anyhow; `<stage>` ∈ task1|task2|task2a|task2b|
  task2c|task3|task4, `--force` to skip the graceful stop) — clears the matching
  `[<key>]` lines from `pipeline_state.md` plus the `passing-*` lock tags, then
  execs `continue.sh --y --no-tail`. **It preserves every fill on disk**: the cluster seed is
  `if not dst.exists()`, so re-entering task2a resumes the TDD loop ON the existing
  bodies rather than re-stubbing them. This is the lever for a driver-half fix
  (charging, taint, epochs, re-admission, the red-build budget) that only executes
  inside `run_phase_c_tdd`. Note the real boundaries are task1 and task2 —
  task2a…task4 are visibility labels for sub-stages of one atomic `task2-4`
  reconstruct phase, so task2a/2b/2c all re-run the loop; task3/task4 clear only
  `task2-4` and go straight to test/fix. `resume_from_t2.sh` is a shim for
  `restart_from.sh task2`.
- **`bash run_hardcrate.sh [--complexity=N] [--from=STAGE]`** — the hardcrate smoke
  in `tmp/hardcrate`, same t1…t4 vocabulary via the shared
  `lib/restart_from_lib.sh`. Without `--from` it re-assembles the crate and
  `rm -rf`s the workspace; with `--from` it preserves fills exactly as
  `restart_from.sh` does. Requires vLLM already up on :8000.
- **`bash redo.sh [--force] [--full]`** — the only lever that DESTROYS the
  reconstruction: it wipes `.skeleton-filled`, and with `--full` also `.skeleton`
  (forcing the bake to re-run), then relaunches `continue.sh --y --keep-augmented --no-tail`,
  i.e. `pipeline.py --restart-from-augmented`, whose `git reset --hard` to the
  task0c tag removed 5,137 tracked files on 2026-08-08.

**When to wipe, and when wiping destroys the measurement.** Wipe when the thing
under test is the construction of the tree itself — a t1.5 layout change, a bake or
strip change, an FR-synthesis change — because those cannot be observed on a tree
already built by the old code (`redo.sh --full --force` skips clone+task0c and is
what re-arms the bake/pin harvest). Do NOT wipe to validate a loop-behaviour fix:
the fills ARE the state the gates read, and a from-scratch run spends hours
re-reaching the position the pathology needs before it can even reproduce. And
never wipe to "start clean" while a diagnosis is open — the run log and the tree are
the only two records of what earlier runs achieved, which is why `--wipe` was
removed from `continue.sh` rather than fixed.

**Comparing two runs means neither of them wipes.** A/B is
`compare_models.sh snapshot <name>` once, then `arm <seed> <qwen|muse>` per arm —
`arm` restores the seed by `rm -rf`ing `.skeleton` and `.skeleton-filled` and
copying the snapshot back, so both arms start from the same tree AND the same
layout (they are a pair; taking one without the other gives bodies that do not
compile). A `redo.sh` between arms replaces the seed with a fresh one and the
comparison is over. Sequence: `snapshot baseline`, then
`arm baseline qwen && bash pre_run.sh && bash continue.sh`, same for the other arm,
then `report qwen=<run-id> muse=<run-id>`; `compare_models.sh list` recovers a run
id you did not note. What is compared is progress per SWEEP and per hour, not
tokens/sec. Take the cheap validation first — `run_microcrate.sh` on a new model
before spending anyhow hours, so a model that ignores the FN/FILE output contract
fails in minutes instead of looking like a slow model six hours in.

**The model and the server.** `MODEL=` in `.env` is the one switch —
`VLLM_MODEL_NAME` falls back to it, `TaskModels.from_env` defaults every
`TASK*_MODEL` to it, and `MODEL_CAPS`/`model_tier` key the reasoning dialect and
prompt tier on the same string. `docker_vllm.sh` derives image, parsers and a
max-len CEILING from it (`--y` to stop what is running, `--allow-download` to let
vLLM fetch an uncached model, `--no-pull` for an offline relaunch). **It pulls
`:latest` on every launch by default**, because `docker run` never re-fetches a tag
it already holds — this box sat on a 2026-05-08 image serving vLLM 0.20.2 for three
months, which is how a model needing a newer server fails as "unknown architecture"
instead of "your image is stale". The corollary: restarting the server also
upgrades it, so do not restart vLLM in the middle of a measurement you intend to
compare against an earlier run — check `curl -s localhost:8000/version` and leave a
healthy container alone.

## A live-run ping is a crime scene — `monitor.py` already has the score

When the user asks to monitor a run, the deliverable is not the number on the
summary line. `monitor.py` already shows coverage, pass counts, iteration,
stall. Restating `83.9%` or `iter N` is a report the user could produce
themselves; it is not why the ping was asked for.

Treat every wake as a crime scene. After a batch of pipeline changes the
surface will usually look fine — the original crate still measures 83.9%,
cargo still returns, the loop still prints an iteration summary. That is the
expected disguise, not evidence the new code worked. Statistically there is
an error. Do not stop at the sign that would look healthy on the dashboard.

**The rule, checkable.** A ping is finished only when it names, from the
artefacts the loop actually wrote this iteration:

- what the LLM was sent (the builder, the output-format slot, a quoted slice
  of the prompt);
- what the LLM produced (the stream log / raw prefix, not the pipeline's
  paraphrase of it);
- what the pipeline did with that reply (extracted, dropped, spliced,
  refused — the log line that establishes it).

A matching score against last time, or against "what this phase always
starts at", is not that check. If you cannot point at the reply body and the
function that parsed it, you have not monitored.

**What this rule is not.** It is not a standing list of the last defect
(fenced replies, budget caps, a named extractor). The next miss will look
like a different healthy number. The stance is the investigation, not a
checklist of yesterday's hole.

**The instance, 2026-08-27, run `2881435-34981ab0`.** t0c sat at 83.9%
(677/807) — the original crate, measured before any generated test landed.
Muse had written real `#[test]` files inside markdown fences;
`extract_test_mod` keyed only on `#[cfg(test)] mod gen_` / `<FILE>` and
returned empty; split-and-merge logged `kept 0/0`; the pass was accepted as
a coverage ceiling. The score was already on `monitor.py`. The crime scene
was `/tmp/stream_loop_*_gen*.log` (`Empty generation … Raw: ```rust
#![allow…] #[test] fn …`). Reporting "still 83.9%" was the miss this rule
exists to stop.

**The tells.** A wake that opens with the coverage or pass count; "same as
last time" / "on-theory, nothing to do" keyed only on the summary line; a
glance at `iter N:` on a deadline re-arm that never opens a stream log or a
prompt.

## Watch a live run at the iteration boundary, in-session

The loop emits exactly one summary per iteration and it carries the whole state
vector:

    iter N: cargo test rc=... pass=X fail=Y oracle=Z (improved=..., stall=a/b);
    frontier=F fn(s) (merged), E skipped as exhausted

That vector is what `monitor.py` already displays — do not spend a wake
restating it. It is also the vector a walk fixture is distilled from, so what
is asserted offline is what arrives online. Watch with a persistent in-session
monitor over `logs/pipeline_anyhow.log`, not a cold `claude -p`: a cold spawn
re-derives the run from the log every wake and reports symptoms, having no
memory of which fix is under test or what trajectory it predicted.

**Silence is not success.** A filter matching only the summary line cannot tell a
crashloop from a slow iteration. It must also carry the terminal signatures —
`MODULE_PLATEAU_ABORT`, `VERDICT MISMATCH`, `TIMED OUT`, `Traceback`,
`high-water re-based`, `NOT re-admitting`. Keep the alternation tight: a broad
`Error` match over a half-megabyte log is a firehose.

**On each wake**, do the crime-scene read (previous section). Then compare
that read against the trajectory the fix predicted — not the score against
last time. On-theory means you can point at the log line that shows the
shipped mechanism fired (the block was extracted, the new argument sat in
the builder, the gate charged). A number that matches last time is not
on-theory. Off-theory — the gate the fix targeted did not fire, or the
reply never entered the tree — stop, fix, validate; not "watching, N
iterations left". A pathology no walk covers: distill it from the live log
into a fixture, so the expensive path funds the cheap one.

**Every monitor also watches the stall, and a stall is a finding, not weather.**
Whatever a monitor was armed to check, it ALSO evaluates the `stall=a/b` field on
every iteration summary — because a run that is not crashing can still be
spending GPU on fills that move no verdict, which is the most expensive silent
failure this project has. A non-zero and CLIMBING stall is normally bad: the
sweeps since the last improvement landed bodies and changed no verdict. Do NOT
dismiss it as routine, and in particular do not dismiss it because `max_stalls=0`
has disabled the exit — a disabled exit means the stall will never STOP the run,
not that it is harmless; it makes an uninvestigated stall more costly, not less,
because nothing else will end the churn either. On a rising stall, investigate
the cause from the frontier and the fill outcomes per "Diagnose a stalled loop
from the frontier, not from the score" (is the frontier all blocked-by-sibling,
are the fills reverting, is the dispatched slice a re-buy the record already
convicts?), name which of the four per-fill classes it is (this file's opening
section), and when the cause is a pipeline defect write it — and any fix — to
`technical_debt.md`. A stall investigated and found to be an expected bounded
transient (a producer chain that has not closed yet) is fine to note and pass; a
stall waved off as "nothing to act on" is the pathology this rule exists to stop.

The instance, 2026-08-25, run `4911-9ab8852e`: `stall=1/0` was reported and
dismissed as "nothing to act on" because `max_stalls=0` meant it could fire no
exit. That was true and beside the point — the escalated producer pass was
reverting (`context.rs`/`ensure.rs` on `E0277`/`test_too_long`), the documented
re-buy of producers that cannot cash the crate's remaining test-pinned failures
without a prompt-contract change, so the run was burning GPU on a known-flat pass
while a due reading waited. The stall was the signal to stop the churn and take
the reading; reading it as "not an exit trigger, therefore nothing to do" is the
exact miss the rule forbids.

## An armed watcher's wait loop carries a hard deadline — always

Any background wait the assistant arms — a death-watch on a pipeline process,
a poll for a log line, a sleep-until-condition loop — loops on BOTH clauses:

    while <condition not met> AND <elapsed < deadline>   # default: 1 hour

and on expiry it WAKES AND REPORTS the timeout loudly, never exits silently.
The deadline is not politeness; it is the bound on a predicate bug, and the
predicates keep having bugs — this is a repeating class (it has cost idle
hours on multiple days), not a one-off.

The instance, 2026-08-18: a watcher armed to relaunch `continue.sh` when the
pipeline exited waited on `pgrep -f "pipeline.py --project anyhow"` — a
pattern its own shell's command line contains, so the loop matched itself and
could never exit. The pipeline had stopped cleanly at 02:44 (the flat-score
guard, by design, one relaunch away from dispatching the escalated producer
slice) and the machine sat idle for over an hour; the watcher was 4h53m old
when found. A one-hour deadline converts every instance of this class into a
bounded, self-reporting failure regardless of which predicate is wrong.

Defense in depth, not a substitute for the deadline: write self-non-matching
predicates (`pgrep -f "[p]ipeline.py --project anyhow"` — the bracket keeps
the pattern from matching its own command line) and run the predicate once
from the arming shell to see what it returns before trusting it.

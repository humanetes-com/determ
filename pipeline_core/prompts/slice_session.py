"""The red-slice debugging session's opening turn and its harness turns.

Turn 0 is the single document that opens a session: the failing tests and
what they printed and observed, the slice — every crate fn those tests
execute — rendered per file with the editable units named, the same type,
invariant, macro and specification sections the per-impl prompt carries,
and the session protocol (change set / request / notes). Later turns are
written by :mod:`pipeline_core.domain.slice_session` from measurements; this
module only shapes text.

Why the whole slice and not one unit: run `3003-93e9a9bf` (2026-09-02)
dispatched the 20-unit coverage slice of two red Debug tests as four
per-file prompts plus a two-fn pair per iteration, each judged alone,
and the loop's own log called 17 of them re-buys before dispatching them.
The path a failing assertion depends on is the unit of evidence; the
session makes it the unit of work.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.language_driver import language_driver
from pipeline_core.domain.last_error import LastErrorKind, last_error_prefix, resolve_kind
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.slice_session import NOTES_CLOSE, NOTES_OPEN, SessionSlice
from pipeline_core.prompts.fix_prompts import FN_LITERAL_RULE
from pipeline_core.prompts.runtime import promoted_error_rules, repo_spec_section
from pipeline_core.prompts.sections import tag_block

# A file over this many chars is rendered condensed: the editable fns in
# full, every other fn as its signature. Sized so a five-file slice stays
# within the transcript's first ~40K tokens (the recorded pair prompt was
# 88K chars for two fns).
DEFAULT_FILE_CHARS = 36_000


def render_slice_file(
    file_rel: str,
    text: str,
    editable: Sequence[FnRecord],
    all_records: Sequence[FnRecord],
    *,
    file_chars: int = DEFAULT_FILE_CHARS,
    executed: Collection[FnSite] = (),
    coverage_known: bool = False,
    installable: Mapping[FnSite, str] | None = None,
) -> str:
    """One `<FILE>` section: the editable units named with their impl
    headers — and, when the sweep's coverage is known, whether the failing
    tests EXECUTE each one on the current tree — then the file, whole when
    it fits, otherwise condensed to `use` lines, the editable fns in full
    and the rest as signatures.

    The executed mark is what run `164454-8ec9c4d4` (2026-09-02) lacked:
    its first session rewrote `ErrorImpl::debug` five times while the Debug
    dispatcher never called it on the failing arm, and nothing in the
    transcript said so.

    `installable` maps an editable free fn to the `Table.field` whose
    signature it fits, for a fn no table installs yet. Its mark says that
    instead of the coverage mark: "NOT executed" is true of it and tells the
    model to leave it alone, while its whole reason for being editable is
    that the table an editable unit builds may install it."""
    def _mark(r: FnRecord) -> str:
        slot = (installable or {}).get(record_site(r))
        if slot:
            return (f" [installable: fits {slot}; referenced by nothing but "
                    "its own declaration — a table an editable unit builds "
                    "may install it, rewritten in the same change set]")
        if not coverage_known:
            return ""
        return (" [executed by the failing tests]"
                if record_site(r) in executed
                else " [NOT executed by the failing tests on the current tree]")
    named = ", ".join(
        f"{r.fn_name} ({r.container or 'free fn'}){_mark(r)}" for r in editable)
    head = (f'<FILE path="{file_rel}" editable="{named}">\n')
    if len(text) <= file_chars:
        return head + text.rstrip("\n") + "\n</FILE>"
    edit_keys = {(r.fn_name, r.container) for r in editable}
    parts: list[str] = [ln for ln in text.splitlines()
                        if ln.lstrip().startswith(("use ", "pub use ", "pub(crate) use "))]
    parts.append(f"// … {file_rel} condensed: editable fns in full, others as signatures …")
    by_container: dict[str, list[FnRecord]] = {}
    for r in all_records:
        if r.file == file_rel:
            by_container.setdefault(r.container, []).append(r)
    for container, recs in by_container.items():
        if container:
            parts.append(f"{container} {{")
        for r in recs:
            pad = "    " if container else ""
            if (r.fn_name, r.container) in edit_keys:
                body = r.body_text.strip("\n")
                parts.append(f"{pad}{r.sig_text.strip()} {{\n{body}\n{pad}}}")
            else:
                parts.append(f"{pad}{r.sig_text.strip()} {{ /* … */ }}")
        if container:
            parts.append("}")
    return head + "\n".join(parts) + "\n</FILE>"


def _protocol(slice_: SessionSlice, records: Mapping[FnSite, FnRecord]) -> str:
    first = slice_.units[0] if slice_.units else None
    example_block = records[first].container if first is not None else ""
    example_name = first.fn_name if first is not None else "name"
    return f"""SESSION PROTOCOL — reply with ONE of:

(a) A CHANGE SET: one or more
<FN name="{example_name}" block="{example_block}">
  // body only — no signature, no outer braces
</FN>
blocks, ONLY for fns named in a FILE's editable list above (block= is the
impl header verbatim, or block="" for a free fn). A body may open with a
`where` line to tighten that fn's bounds. Change as FEW fns as the failing
tests need; a body you do not send stays as it is. Each change set is applied
on the tree as it stands after the previous KEPT turn — a change set that
failed to compile was NOT applied and its bodies must be resent.
{FN_LITERAL_RULE}

(b) A CONTEXT REQUEST, when a fact you need is not in this transcript:
<REQUEST kind="file" arg="src/helper.rs"/> · <REQUEST kind="test_source" arg="test_name"/>
· <REQUEST kind="type_def" arg="TypeName"/> · <REQUEST kind="full_error"/>
A FILE request for a crate file that holds a formatter method of a type an
editable dispatcher already calls adds that method to this session's
editable set — you may then return an FN body for it.

(c) NOTES you want carried into the next session on this slice:
{NOTES_OPEN} what you established, what the printed values showed, what remains {NOTES_CLOSE}
— notes may accompany (a) or (b).

After each change set the harness compiles the crate and runs the WHOLE test
suite, then writes back the compiler's diagnostics, or every target test's
verdict with the values its assertions printed and observed, plus any
previously-passing test that went red. The session ends when every target
passes with no regression, when you reply with no change set and no request,
or when the turn budget runs out. Do not guess at a value you can observe:
change the body, read what it printed."""


def build_slice_session_prompt(
    *,
    module_name: str,
    cluster_name: str,
    slice_: SessionSlice,
    records: Mapping[FnSite, FnRecord],
    all_records: Sequence[FnRecord],
    file_texts: Mapping[str, str],
    target_evidence: Mapping[str, str],
    test_blocks: Sequence[tuple[str, str]],
    types_section: str,
    bound_methods: str = "",
    invariants: str = "",
    macros: str = "",
    notes: str = "",
    prior_regression: str = "",
    kind: LastErrorKind | None = None,
    blueprint: str = "",
    fr_path: Path | None = None,
    max_turns: int = 8,
    file_chars: int = DEFAULT_FILE_CHARS,
    executed: Collection[FnSite] | None = None,
    installable: Mapping[FnSite, str] | None = None,
) -> str:
    """Turn 0 of a session over `slice_`.

    `target_evidence` maps each failing test to its own words — the panic
    line, the quoted source line and the probe's `observed:` lines — and is
    rendered FIRST, before any code: the values are what the model reasons
    from. `test_blocks` are the test files (attributed first) under the
    same caps the other routes use. `notes` is the previous session's own
    `<NOTES>` for this slice, after the contradiction screen. `prior_regression`
    is what the previous session's reddened tests printed (`render_prior_regression`).
    `kind` is the stored verdict of that attempt when the record carries one;
    a faulting-call kind prefixes PRIOR_REGRESSION with the same
    `last_error_prefix` sentence the pair builder already uses, so the
    quoted expression is an instruction not only evidence."""
    tests_part = "\n\n".join(
        f"* {t} — FAILING. It said:\n"
        + "\n".join(f"    {ln}" for ln in (target_evidence.get(t) or "(no captured output)").splitlines())
        for t in slice_.tests)
    files_part = "\n\n".join(
        render_slice_file(
            f, file_texts.get(f, ""),
            [records[u] for u in slice_.units if u.file == f],
            all_records, file_chars=file_chars,
            executed=executed or (), coverage_known=executed is not None,
            installable=installable)
        for f in slice_.files)
    coverage_legend = ""
    if executed is not None:
        coverage_legend = (
            "\nEach editable unit is marked with what the sweep's coverage "
            "MEASURED: whether the failing tests execute it on the current "
            "tree. A unit they do NOT execute cannot change what they observe "
            "until a unit they do execute routes into it (a dispatcher that "
            "does not yet call its implementer is the usual shape: change the "
            "dispatcher's arm AND the implementer in one change set).\n")
    if installable:
        coverage_legend += (
            "A unit marked installable is executed by nothing because no "
            "table installs it yet; it is editable so that a table an "
            "editable unit builds can name it, with both written in one "
            "change set.\n")
    tests_section = "\n\n".join(
        f'<TEST path="{p}">\n{txt}\n</TEST>' for p, txt in test_blocks) or "(none)"
    first_file = slice_.files[0] if slice_.files else ""
    regression_body = prior_regression
    if (prior_regression.strip()
            and resolve_kind(kind, prior_regression).has_faulting_call):
        regression_body = (
            last_error_prefix(prior_regression, kind) + prior_regression)
    return f"""You are DEBUGGING a Rust crate that already compiles: {len(slice_.tests)} test(s)
fail, and every crate fn those tests execute is listed below as EDITABLE. Your
job over this session is to change as few of those bodies as needed so the
failing tests pass while EVERY other test in the suite stays green. You will
be shown, after each change set, exactly what the assertions printed.

Crate: {module_name} | Cluster: {cluster_name} | Session budget: {max_turns} turn(s)

FAILING TESTS (the targets):

{tests_part}

{tag_block("PREVIOUS_SESSION_NOTES", notes, prefix="Your own notes from the previous session on this slice:\n")}{tag_block("PRIOR_REGRESSION", regression_body)}
<RUST_GOTCHAS>
{language_driver().gotchas_summary()}
</RUST_GOTCHAS>
{tag_block("REPOSITORY_SPECIFICATIONS", repo_spec_section(fr_path, file_rel=first_file), prefix="The crate's own specification documents. When your plan conflicts with them, follow the spec.\n")}{tag_block("DESIGN_BLUEPRINT", blueprint)}{tag_block("TYPES_IN_SCOPE", types_section)}{tag_block("BOUND_METHODS", bound_methods)}{tag_block("CLUSTER_INVARIANTS", invariants)}{tag_block("RECURRING_ERRORS", promoted_error_rules(fr_path))}{tag_block("MACROS", macros)}
THE SLICE — the crate fns the failing tests execute (plus the formatter
implementers their dispatchers delegate to), per file; editable units are
named in each FILE header, everything else in these files is read-only.
{coverage_legend}
{files_part}

Tests (read-only):
{tests_section}

{_protocol(slice_, records)}"""

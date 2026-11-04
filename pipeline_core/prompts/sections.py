"""Pure prompt-section renderers + curated guidance data.

Text→text only: no I/O, no run state, no regex. The two impure
collaborators the builders need (the pipeline's file-view annotator and
promoted-error rules) live behind :mod:`pipeline_core.prompts.runtime`.
Bodies moved verbatim from pipeline.py (leading underscores dropped on
extraction — they are this module's public entry points).
"""

from __future__ import annotations

from collections.abc import Sequence

from pipeline_core.domain.models import (
    DispatchFamily,
    FieldRecord,
    ImplBlock,
    MethodRecord,
    SlotFit,
    TraitImplRecord,
    TypeRecord,
)
from pipeline_core.domain.refill_evidence import collapse_ws
from pipeline_core.domain.type_refs import (
    accessor_methods_for_field,
    field_accessible_from,
)


def extract_trait_from_container(container: str) -> str | None:
    """Pull the trait name out of an `impl Trait for X` container string.
    Returns None for inherent impls (no trait) or unsupported forms.

    Regex-free (project rule) depth-aware walk. Improvement over the old
    regex, which required whitespace after `impl` and stopped generic lists
    at the first `>` — so every `impl<T> Trait for X` and every nested
    generic (`impl Trait<Vec<U>> for X`) silently got NO trait contract.
    """

    def _skip_generics(s: str) -> str | None:
        """`s` starts with `<` — return the text after its balanced close."""
        depth = 0
        for i, ch in enumerate(s):
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
                if depth == 0:
                    return s[i + 1:]
        return None

    s = container.lstrip()
    if not s.startswith("impl"):
        return None
    s = s[len("impl"):]
    if not s or not (s[0] == "<" or s[0].isspace()):
        return None  # e.g. `implFoo` — not an impl header
    s = s.lstrip()
    if s.startswith("<"):
        skipped = _skip_generics(s)
        if skipped is None:
            return None
        s = skipped.lstrip()
    # Trait path: `Ident` / `path::To::Ident` (chars valid in a Rust path).
    j = 0
    while j < len(s) and (s[j].isalnum() or s[j] in "_:"):
        j += 1
    path = s[:j]
    if not path or not (path[0].isalpha() or path[0] == "_"):
        return None
    rest = s[j:]
    if rest.startswith("<"):  # the trait's own generic arguments
        skipped = _skip_generics(rest)
        if skipped is None:
            return None
        rest = skipped
    # A trait impl requires ` for `: whitespace, `for`, whitespace.
    if not rest or not rest[0].isspace():
        return None
    rest = rest.lstrip()
    if not (rest.startswith("for") and len(rest) > 3 and rest[3].isspace()):
        return None
    # Last path segment is the trait name (`fmt::Display` → `Display`).
    return path.rsplit("::", 1)[-1]


# How a trait-impl method is reached. The method has no caller by name: it runs
# when something dispatches the trait on a value of the type (including one
# handed out as `dyn Trait`), so "no code calls it" is not "no public path
# reaches it". Observed 2026-10-03 (anyhow): the generation model wrote tests
# asserting `Debug for DisplayError` was "never called by the public API" while
# `format!("{:#?}", err)` on an `Option::context` error runs it, and
# `format!("{:?}", Box<dyn Error>)` runs `Debug for ErrorImpl<E>`.
TRAIT_DISPATCH_ROUTES: dict[str, str] = {
    "Debug": "`{:?}`, `{:#?}` or `dbg!` on a value of this type or on a `dyn Debug`/`dyn Error` holding it; "
             "a wrapper's own Debug may take another branch for `{:#?}` than for `{:?}`",
    "Display": "`{}`, `format!`, `.to_string()` on a value of this type or on a `dyn Display`/`dyn Error` holding it",
    "Clone": "an explicit `.clone()` / `Clone::clone` call on the type (a plain copy of a `Copy` value does NOT call it), "
             "or a container's clone/`cloned()`/`to_vec()` that clones this element type",
    "Drop": "a value of this type going out of scope or being passed to `drop`",
    "Default": "`Default::default()`, `T::default()` or `..Default::default()` for this type",
    "PartialEq": "`==`, `!=` or `assert_eq!` between values of this type",
    "PartialOrd": "`<`, `>`, `<=`, `>=` or `.partial_cmp()` between values of this type",
    "Ord": "`.cmp()`, `.max()`, `.min()` or sorting values of this type",
    "Hash": "using a value of this type as a `HashMap`/`HashSet` key or calling `.hash(&mut hasher)`",
    "Iterator": "`.next()`, a `for` loop, or any adapter (`.map`, `.collect`, ...) over a value of this type",
    "Deref": "`*value` or an auto-deref'd method call on a value of this type",
    "From": "`T::from(x)`, `x.into()` or the `?` operator converting into this type",
    "Error": "`Error::source()` / `Error::provide` called on a value of this type or on a `dyn Error` holding it "
             "(`std::error::request_ref` drives `provide`)",
}


def trait_dispatch_route(container: str) -> str:
    """Where a trait-impl method is reached from, or "" for an inherent
    method and a trait the table does not know."""
    name = extract_trait_from_container(container)
    return TRAIT_DISPATCH_ROUTES.get(name, "") if name else ""


def tag_block(tag: str, content: str, *, prefix: str = "", suffix: str = "") -> str:
    """Render one optional prompt section:
    `\\n<TAG>\\n{prefix}{content}{suffix}\\n</TAG>\\n`, or "" when content is
    empty. The one wrapper for every optional section — previously
    re-implemented inline per builder, which is how builders drifted apart
    (a section added to one but not the others)."""
    if not content:
        return ""
    return f"\n<{tag}>\n{prefix}{content}{suffix}\n</{tag}>\n"


def format_impl_block_for_prompt(
    block: ImplBlock,
    mark_target: bool = True,
    refill_fn_names: frozenset[str] = frozenset(),
    *,
    fill_fn_names: frozenset[str] | None = None,
    inferred_fn_names: frozenset[str] = frozenset(),
    crashed_fn_names: frozenset[str] = frozenset(),
    aimed_fn_names: frozenset[str] = frozenset(),
    candidate_fn_names: frozenset[str] = frozenset(),
    companion_fn_names: frozenset[str] = frozenset(),
) -> str:
    """Render an impl block in TARGET form: the impl header followed by every
    member's signature + a one-line body status. Stub members are tagged
    [STUB] so the model knows which to fill; already-filled members carry a
    shortened body preview so the model can mirror their structural shape
    when designing the new ones. Members named in `refill_fn_names`
    (behavioral re-fill targets: compiling but test-failing) get the FULL body
    (no truncation) tagged [REFILL] instead of the FILLED preview, so the model
    can actually rewrite them, not just pattern-match them.

    `fill_fn_names` — the stubs THIS response is asked for. None keeps the old
    reading (every stub is a target). When given, a stub outside it is a
    member the cluster's cfg never compiles (or one already handled): it is
    rendered as a stub the model must NOT answer. Until this distinction
    existed, `provide` under the stable cluster read "[STUB] — fill in this
    response" while the header listed only the active members, the model
    answered it, and the splice refused the body (`not_in_cluster_cfg`) —
    four blocks on 2026-08-20 (run `902902-daee577d`), two of which lost
    their whole reply that way.

    `inferred_fn_names` — re-fill members whose attribution is inference only
    (`domain.refill_evidence`): tagged `[REFILL?]` with an instruction to
    return the body unchanged when it is right, instead of the certainty
    "its test below FAILS — rewrite" that had a correct `source` body rewritten
    and `construct_from_context` degraded on the same day.

    `crashed_fn_names` — re-fill members a runtime fault's own frame named.
    They are tagged `[REFILL!]` and NEVER `[REFILL?]`: a crashed binary
    reports no verdict, so these units reach the prompt with no convicting
    test and used to render as "implicated by NAME only … else return it
    unchanged" — the weakest instruction in the vocabulary, handed to the one
    body the tree had just faulted inside. `src/error.rs::drop` was given that
    tag on every sweep of runs `842437`…`1519852` (2026-08-29/30), returned
    unchanged as instructed, and kept 13 tests unmeasurable the whole time.

    `aimed_fn_names` — re-fill members a callee of the faulting frame. Tagged
    `[REFILL]` with re-derive-from-types, never `[REFILL?]`. Aimed producers
    are kept out of `crash_blamed`, so without this set they arrive as
    inference. Run `1911637-d0fab212` listed `object_downcast` as
    `[REFILL?]` while `test_large_alignment` still SIGABRTed.

    `candidate_fn_names` — for a RELATED block: members that are the
    same-named re-fill candidates for the same failing tests, shown in full
    as the other candidate rather than as a settled `/* FILLED */` preview.

    `companion_fn_names` — filled members the pair will call and must not
    rewrite (a crash-recovered table slot whose attributed tests already
    pass). Shown in full with an instruction not to emit an `<FN>`. Empty
    (the default) leaves every non-refill filled member as the truncated
    `/* FILLED */` preview.

    `block` is an `ImplBlock` produced by `_collect_impl_blocks`.
    """
    container = block.container
    lines: list[str] = []
    if container:
        # Trim the {fns…} body from the container header — we render each fn
        # explicitly below. The extractor's container string captures the
        # entire impl header up to the opening brace; some variants tack on
        # the first fn signature. Cut at the first newline or `{` to keep
        # just the header.
        header = container
        brace_pos = header.find("{")
        if brace_pos != -1:
            header = header[:brace_pos].rstrip()
        lines.append(f"{header} {{")
    else:
        lines.append("// free fns at file level")
    for m in block.members:
        sig = m.sig_text.strip()
        is_stub = m.has_unimplemented_body
        if is_stub:
            if not mark_target:
                tag = " // [STUB]"
            elif fill_fn_names is None or m.fn_name in fill_fn_names:
                tag = " // [STUB] — fill in this response"
            else:
                tag = (" // [STUB] — NOT in this response (cfg-gated out of "
                       "this cluster or handled elsewhere): do NOT emit an "
                       "<FN> for it")
            lines.append(f"    {sig} {tag}")
        elif m.fn_name in refill_fn_names:
            body = m.body_text.strip().replace("\n", "\n    ")
            if m.fn_name in crashed_fn_names:
                # Checked before `inferred_fn_names`: the crash is what
                # removed this unit's convicting test, so it arrives graded
                # as inference and would otherwise be told to stand pat.
                lines.append(
                    f"    {sig} {{ /* [REFILL!] a test binary CRASHED inside "
                    f"this body — see CRASH_EVIDENCE. It compiles; it is "
                    f"still WRONG. Rewrite it — do NOT return it unchanged "
                    f"*/ {body} }}")
            elif m.fn_name in aimed_fn_names:
                lines.append(
                    f"    {sig} {{ /* [REFILL] a test binary CRASHED and this "
                    f"body is a callee of the faulting frame — the pointer "
                    f"the blamed frame dereferenced is produced here. "
                    f"Re-derive it from the types it is generic over; do "
                    f"NOT return it unchanged */ {body} }}")
            elif m.fn_name in inferred_fn_names:
                lines.append(
                    f"    {sig} {{ /* [REFILL?] compiles; implicated by NAME "
                    f"only — see REFILL_TARGETS. Rewrite ONLY if wrong for "
                    f"these bounds, else return it unchanged */ {body} }}")
            else:
                lines.append(f"    {sig} {{ /* [REFILL] compiles but its test "
                              f"below FAILS — rewrite the BEHAVIOR */ {body} }}")
        elif m.fn_name in companion_fn_names:
            body = m.body_text.strip().replace("\n", "\n    ")
            lines.append(
                f"    {sig} {{ /* FILLED — this pair will call this body; "
                f"do NOT emit an <FN> for it. It compiles and its tests "
                f"pass; rewriting it reddens tests it already pays */ "
                f"{body} }}")
        elif m.fn_name in candidate_fn_names:
            body = m.body_text.strip().replace("\n", "\n    ")
            lines.append(
                f"    {sig} {{ /* FILLED — same-named re-fill candidate for "
                f"the SAME failing tests (tried separately); shown in full */ "
                f"{body} }}")
        else:
            body = m.body_text.strip()
            preview = body
            if len(preview) > 200:
                preview = preview[:200].rsplit("\n", 1)[0] + " /* ... */"
            preview = preview.replace("\n", "\n    ")
            lines.append(f"    {sig} {{ /* FILLED */ {preview} }}")
    if container:
        lines.append("}")
    return "\n".join(lines)


def _tuple_struct_fields(fields: tuple[FieldRecord, ...]) -> bool:
    """True iff `fields` are the positional fields of a tuple struct
    (numeric or empty names) — `Quoted<D>(D)` records a single field `0`."""
    return bool(fields) and all((not f.name) or f.name.isdigit() for f in fields)


def world_gate_attr(member: FieldRecord | MethodRecord) -> str:
    """`#[cfg(…)] ` for a member only some cfg worlds compile, else "".

    The gate is the member's own effective cfg, spelled so a body can copy it:
    a reference to the member is legal only under the same predicate, and a
    struct literal has to carry the attribute on that field's initialiser.
    The extractor's token-spaced spelling is tightened; the predicate is
    otherwise verbatim."""
    if not member.worlds or not member.cfg_predicates:
        return ""
    preds = [p.replace(" (", "(").replace("( ", "(").replace(" )", ")")
              .replace(" ,", ",") for p in member.cfg_predicates]
    pred = preds[0] if len(preds) == 1 else "all(" + ", ".join(preds) + ")"
    return f"#[cfg({pred})] "


def world_only_note(member: FieldRecord | MethodRecord) -> str:
    """The trailing comment naming the worlds a gated member exists in."""
    if not member.worlds:
        return ""
    return ("   // exists ONLY in cfg world "
            + ", ".join(f"`{w}`" for w in member.worlds))


def _construct_hint(rec: TypeRecord) -> str | None:
    """A `construct:` line showing HOW to build `rec`, derived deterministically
    from the frozen definition/fields (a legitimate, always-correct input).

    Kills the dominant E0599 class: the model pulls a type, sees its fields, then
    writes the universal `Type::new(...)` idiom that doesn't exist. Showing the
    real literal (tuple `Type(a, b)` / struct `Type { f: … }`) replaces the guess.
    Placeholders are the field TYPES so the shape is unambiguous.
    """
    if rec.kind != "struct":
        return None
    name = rec.name or "?"
    fields = rec.fields
    deff = rec.definition or ""
    # Tuple struct: positional fields (numeric/empty names), or a `Name( … )`
    # definition with no brace. Named struct: real field names / a `{ … }` def.
    if _tuple_struct_fields(fields):
        args = ", ".join(f"<{f.ty}>" for f in fields)
        return (f"  construct: {name}({args})   "
                f"// tuple struct — positional; NOT {name}::new(...)")
    if not fields and "(" in deff and "{" not in deff:
        return (f"  construct: {name}(<fields...>)   "
                f"// tuple struct — positional args; NOT {name}::new(...)")
    if fields:
        # A field only some cfg worlds compile is initialised under its own
        # gate; the literal without it fails E0560 where the field is absent,
        # and the literal without the field fails E0063 where it exists.
        body = ", ".join(f"{world_gate_attr(f)}{f.name}: <{f.ty}>" for f in fields)
        gated = (" — keep each `#[cfg(…)]` on its field"
                 if any(world_gate_attr(f) for f in fields) else "")
        return (f"  construct: {name} {{ {body} }}   "
                f"// struct literal; NOT {name}::new(...){gated}")
    if "{" not in deff and "(" not in deff:
        return f"  construct: {name}   // unit struct — no fields"
    return None


# Bounds on the doc lines a type carries into a fill prompt: the constructor
# fill prompt already measures 121 147 chars on the live `stable` tree
# (2026-08-26), and a contract sentence is one or two lines by the authoring
# rule — three lines / 200 chars each keeps a documented type to ~600 chars.
_TYPE_DOC_MAX_LINES = 3
_TYPE_DOC_MAX_CHARS = 200

# Budget for doc regions kept BEYOND the leading window because they state an
# observable a failing test pins. The leading-window cap above is sized for the
# one-or-two-line contract sentence the layout author writes; the UPSTREAM
# crate's own doc on the same item is a worked example of runtime behaviour and
# is long by nature — `Error` on the live `stable` tree is 101 doc lines /
# 3 087 chars, with `Stack backtrace:` at doc line 50 (2026-09-01, run
# `617614-0abaf2d0`). Two dispatches of the formatter pair guessed at that
# header from an assertion because the block stating it was cut; neither
# guess is satisfiable, since no formatting of a `std::backtrace::Backtrace`
# emits it. These bound what a pinned region may add to a prompt already
# measuring ~77 000 chars.
_TYPE_DOC_PINNED_MAX_LINES = 30
_TYPE_DOC_PINNED_MAX_CHARS = 1600
_DOC_ELISION = "/// …"


def _leading_doc_texts(definition: str) -> list[str]:
    """Texts of the leading `///` block in order, blank doc lines INCLUDED as
    `""` and inner indentation preserved (one rustdoc leading space removed).

    Structure is what the pinned-region selection reads: a fenced example and
    the paragraph introducing it are both runs of these entries. Blank RAW
    lines and `//!` / `//` are skipped without ending the block; the first
    attribute or the item head ends it, exactly where `_type_layout_attrs`
    begins reading."""
    out: list[str] = []
    for raw in definition.splitlines():
        line = raw.strip()
        if not line or line.startswith("//!") or (
                line.startswith("//") and not line.startswith("///")):
            continue
        if not line.startswith("///"):
            break
        text = line[3:]
        out.append(text[1:] if text.startswith(" ") else text)
    return out


def _fence_spans(texts: list[str]) -> list[tuple[int, int]]:
    """(open, close) index pairs of the ``` fenced blocks in a doc block. An
    unclosed fence runs to the end — a doc that opens one and stops is still
    an example, and dropping it would lose exactly the content this selection
    exists to keep."""
    spans: list[tuple[int, int]] = []
    open_i: int | None = None
    for i, t in enumerate(texts):
        if not t.strip().startswith("```"):
            continue
        if open_i is None:
            open_i = i
        else:
            spans.append((open_i, i))
            open_i = None
    if open_i is not None:
        spans.append((open_i, len(texts) - 1))
    return spans


def _doc_region(texts: list[str], hit: int,
                spans: list[tuple[int, int]]) -> tuple[int, int]:
    """The doc region a hit at index `hit` belongs to.

    Inside a fence: the whole fence PLUS the prose paragraph immediately
    above it. Rustdoc's universal shape is that a fenced example is
    introduced by the sentence over it, and that sentence is where the
    CONDITION under which the example holds is stated — on the live tree,
    `and if there is a backtrace available:` sits directly above the block
    carrying `Stack backtrace:`, and it is the conditional two fills failed
    to establish. Outside a fence: the contiguous non-blank paragraph."""
    for a, b in spans:
        if a <= hit <= b:
            j = a - 1
            while j >= 0 and not texts[j].strip():
                j -= 1
            if j < 0 or texts[j].strip().startswith("```"):
                return a, b
            while j >= 0 and texts[j].strip() and not texts[
                    j].strip().startswith("```"):
                j -= 1
            return j + 1, b
    start = hit
    while start > 0 and texts[start - 1].strip():
        start -= 1
    end = hit
    while end + 1 < len(texts) and texts[end + 1].strip():
        end += 1
    return start, end


def _pinned_doc_indices(texts: list[str],
                        observables: frozenset[str]) -> list[int]:
    """Indices of the doc lines to keep because some observable is stated in
    their region, in source order, under both pinned budgets. Match is a
    plain substring of the whitespace-collapsed line — the observables arrive
    collapsed and unescaped for exactly that (`asserted_output_literals`)."""
    if not observables:
        return []
    spans = _fence_spans(texts)
    regions: list[tuple[int, int]] = []
    for i, t in enumerate(texts):
        line = collapse_ws(t)
        if not line or not any(o in line for o in observables):
            continue
        region = _doc_region(texts, i, spans)
        if not any(a <= region[0] and region[1] <= b for a, b in regions):
            regions.append(region)
    kept: list[int] = []
    used = 0
    for a, b in sorted(set(regions)):
        for i in range(a, b + 1):
            if i in kept:
                continue
            cost = len(texts[i][:_TYPE_DOC_MAX_CHARS]) + 5
            if (len(kept) >= _TYPE_DOC_PINNED_MAX_LINES
                    or used + cost > _TYPE_DOC_PINNED_MAX_CHARS):
                return kept
            kept.append(i)
            used += cost
    return kept


def _type_doc_lines(definition: str,
                    observables: frozenset[str] = frozenset()) -> list[str]:
    """The LEADING `///` doc lines of a type's rendered `definition`, trimmed,
    at most `_TYPE_DOC_MAX_LINES` of `_TYPE_DOC_MAX_CHARS` each (an over-long
    line ends in `…`, and so does the last kept line when more follow) — plus,
    when `observables` is non-empty, the doc regions further down that STATE
    one of them.

    Authored contract lines reach the fill prompts only through this renderer.
    `format_type_for_prompt` rebuilds the type line from kind/name/fields/
    methods and, until this function existed, read `definition` for nothing
    but its `repr`/`derive` attributes — so a `///` line t1.5 wrote on a type
    was spliced into the skeleton, survived the extractor (which prints doc
    comments FIRST in `render_item_definition`), and was dropped here: the
    live `stable` tree's documented `Chain` rendered bare (2026-08-26). Only
    the leading block counts — `//!` inner docs and `//` comments are not docs
    of the item and are skipped; the first attribute or the item head ends the
    block, exactly where `_type_layout_attrs` begins reading.

    `observables` is the set of literal texts the target's attributed failing
    tests assert appear in some rendering. A crate that documents its own
    output format documents the answer to those assertions, and the leading
    window — three lines, sized for an authored contract sentence — cuts it
    out: `Error`'s doc rendered as two lines and an ellipsis while its
    `Stack backtrace:` example sat at doc line 50, and two formatter fills
    guessed the header from the assertion instead (run `617614-0abaf2d0`,
    2026-09-01, both reverted on the same ten sibling tests). Empty
    `observables` renders byte-identically to before this argument existed,
    so every prompt with no attributed failing test is unchanged."""
    texts = _leading_doc_texts(definition)
    out: list[str] = []
    truncated = False
    leading_idx: set[int] = set()
    for i, text in enumerate(texts):
        text = text.strip()
        if not text:
            continue
        if len(out) >= _TYPE_DOC_MAX_LINES:
            truncated = True
            break
        if len(text) > _TYPE_DOC_MAX_CHARS:
            text = text[:_TYPE_DOC_MAX_CHARS - 1].rstrip() + "…"
        out.append(f"/// {text}")
        leading_idx.add(i)
    if truncated and out and not out[-1].endswith("…"):
        out[-1] += " …"
    pinned = [i for i in _pinned_doc_indices(texts, observables)
              if i not in leading_idx]
    if not pinned:
        return out
    if out:
        out.append(_DOC_ELISION)
    prev: int | None = None
    for i in pinned:
        if prev is not None and i != prev + 1:
            out.append(_DOC_ELISION)
        text = texts[i]
        if len(text) > _TYPE_DOC_MAX_CHARS:
            text = text[:_TYPE_DOC_MAX_CHARS - 1].rstrip() + "…"
        out.append(f"/// {text}".rstrip())
        prev = i
    return out


def _type_layout_attrs(definition: str) -> list[str]:
    """The layout-deciding outer attributes on a type — `#[repr(...)]` and
    `#[derive(...)]` — pulled from the record's rendered `definition`.

    A `repr` is part of a type's contract exactly as its fields are: it decides
    which casts are legal, and the pipeline already treats it as load-bearing
    elsewhere (`carry_frozen_repr`, the layout pins). The compact type line the
    prompt renders from `name`/`fields` drops it, so a producer rewrite that has
    to reason about a transparent wrapper — `object_downcast::<M>` is sound only
    because `MessageError<M>` is `#[repr(transparent)]` — never sees the fact
    that makes its own cast legal (measured: `95878` iter 2, the constructor
    REWRITE reached the delegation defect in its own reasoning and could not
    name the transparent-cast escape; the type line read bare
    `struct MessageError< M >`). `cfg` is deliberately excluded — it gates
    presence, not the type's cast/trait contract, and is carried on other
    channels. Scans only the LEADING attribute block (before the `struct`/
    `enum`/`pub` keyword) so an attribute on an inner field is never hoisted.

    The block is whatever precedes the item's first code line: `definition`
    is `prettyplease::unparse` of the whole item (`render_item_definition`
    in the extractor), which prints the doc comments FIRST, as `///` lines,
    and only then the attributes — so a documented type reads
    `/// Iterator of a chain…` / `#[derive(Clone)]` / `pub struct Chain`.
    Comment lines (`///`, `//!`, `//`) and blank lines are skipped, not
    treated as the end of the block, or every documented type would hide its
    own repr behind its docs (live stable tree: `Chain`). An attribute the
    printer wraps across lines (`#[derive(\\n    Clone,\\n    Debug\\n)]`) is
    joined on its bracket depth before the head is read."""
    out: list[str] = []
    pending: list[str] = []
    depth = 0
    for raw in definition.splitlines():
        line = raw.strip()
        if pending:
            pending.append(line)
            depth += line.count("[") - line.count("]")
            if depth <= 0:
                _emit_layout_attr(" ".join(pending), out)
                pending = []
                depth = 0
            continue
        if not line or line.startswith(("///", "//!", "//")):
            continue
        if not line.startswith("#["):
            break  # the item's first code line ends the outer-attribute block
        depth = line.count("[") - line.count("]")
        if depth > 0:
            pending = [line]
            continue
        _emit_layout_attr(line, out)
    return out


def _emit_layout_attr(attr_line: str, out: list[str]) -> None:
    """Append `#[repr(...)]` / `#[derive(...)]` from one complete attribute
    line; any other attribute (`cfg`, `doc`, `must_use`, …) is dropped."""
    inner = attr_line[2:].rsplit("]", 1)[0].strip()
    head = inner.split("(", 1)[0].strip()
    if head in ("repr", "derive"):
        out.append(f"#[{' '.join(inner.split())}]")


def _trim_sig(sig: str) -> str:
    """A method signature cut to the prompt's budget: keep up to 120 chars
    (covers virtually all real fn sigs; verbose where-clauses are what goes)."""
    sig = sig or ""
    if len(sig) > 120:
        sig = sig[:117] + "..."
    return sig.strip()


def _private_field_lines(
    rec: TypeRecord, private: list[FieldRecord], viewer_file: str,
) -> list[str]:
    """The `private fields` block of a struct rendered for a body in ANOTHER
    module: one header naming the declaring file and the viewer file, then
    one line per field carrying the accessor that returns it — or the fact
    that none is declared.

    Run `3003-93e9a9bf` (2026-09-02, cluster `stable`): the pair prompt for
    `src/error.rs::fmt` + `src/fmt.rs::debug` rendered `ErrorImpl` (declared
    in `src/error.rs`; `vtable`, `backtrace`, `_object`, none `pub`) under
    `fields (access with .name):` 15 times. The `src/fmt.rs` half read
    `impl_ref.backtrace` directly — `error[E0616]: field backtrace of struct
    ErrorImpl is private` on 3 of its 6 attempts (two on `backtrace`, one on
    `vtable`), roughly 25 GPU-minutes each — while the record's own
    `unsafe fn backtrace(this: Ref<Self>) -> &Backtrace` sat a few lines
    below under methods. The rendering invited the error; this block states
    the rule the compiler applies and puts the accessor on the field's line.
    """
    with_accessors = [(f, accessor_methods_for_field(rec, f)) for f in private]
    reach = ("reach each one through the accessor on its line"
             if any(ms for _, ms in with_accessors)
             else "no method of this type returns them")
    # The construction a body there cannot write: a named literal is E0451
    # (private field in a struct literal); a tuple struct's constructor call
    # `Name(..)` is E0603 (private tuple struct constructor).
    literal = (f"the `{rec.name or '?'}(..)` constructor is E0603 there"
               if _tuple_struct_fields(rec.fields)
               else "a struct literal naming them is E0451 there")
    lines = [
        f"  private fields — declared in {rec.file}, NOT accessible from "
        f"{viewer_file} (no .name access: E0616; {literal}; {reach}):",
    ]
    for f, ms in with_accessors:
        via = (" / ".join(_trim_sig(m.sig) for m in ms) if ms
               else "(no accessor declared here)")
        lines.append(f"    {world_gate_attr(f)}{f.name}: {f.ty}   → {via}"
                     f"{world_only_note(f)}")
    return lines


#: The sentence that makes the list usable. A list the reader can see is
#: partial is a list it cannot reason from by absence: run `450977-5f48b68a`
#: (2026-09-21) had crate types rendered with no impls beside external types
#: rendered with theirs, and the fill's trace asked about one impl 14 times
#: and concluded 9 times that it was "omitted from the snippet". Auto traits
#: and the standard library's blanket impls are not headers this crate writes,
#: so the sentence says so rather than claim they are absent.
_TRAIT_IMPLS_BODY = (
    "  trait impls (COMPLETE: every impl this crate writes whose self type is "
    "this type, any file; a trait not listed is NOT implemented for it by an "
    "impl that names this type — auto traits and std blanket impls aside"
)
TRAIT_IMPLS_HEADER = _TRAIT_IMPLS_BODY + "):"
TRAIT_IMPLS_HEADER_WITH_BLANKETS = (
    _TRAIT_IMPLS_BODY
    + "; an impl this crate writes for a type parameter is not in this list, "
    "it is in the crate blanket list in this section):")
# The record's crate writes type-parameter impls and this rendering does not
# print them: the sentence must not point at a list that is not there, and
# must not let the per-type list read as covering them either.
TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN = (
    _TRAIT_IMPLS_BODY
    + "; this crate also writes impls for a type parameter, not shown here, "
    "which may implement a trait not listed):")
BLANKET_IMPLS_HEADER = (
    "  crate blanket impls (COMPLETE: every impl this crate writes whose self "
    "type is a type parameter, any file; a trait not listed is NOT "
    "implemented for any type by such an impl — auto traits and std blanket "
    "impls aside):")
# A cap that lands inside either list used to keep the COMPLETE sentence and
# drop the lines under it, which is the absence lie the sentence forbids.
_LIST_OMITTED = (
    "  trait-impl list omitted: this record did not fit the section, so the "
    "list is not shown and is not a statement that any impl is absent.")
_COMPLETE_HEADERS = (
    TRAIT_IMPLS_HEADER, TRAIT_IMPLS_HEADER_WITH_BLANKETS,
    TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN, BLANKET_IMPLS_HEADER)


def trait_impl_lines(rec: TypeRecord) -> list[str]:
    """The `trait impls` block of a crate type rendered on its own: nothing
    beside it prints the crate's type-parameter impls, so when the record
    carries any the sentence says they exist and are not shown."""
    return _trait_impl_lines(rec, blankets_shown=False)


def _trait_impl_lines(rec: TypeRecord, *, blankets_shown: bool) -> list[str]:
    """The `trait impls` block of a crate type: one line per header the
    extractor found, the declaring file beside it. Headers only (the line the
    skeleton keeps after strip). Nothing for an external record (it carries
    its own `trait impls:` note) or a record whose extractor predates the
    field (`trait_impls is None`) — a list printed as complete must be
    complete. An empty reading prints the header with `(none)`, which is a
    statement the reader can use. When the record carries blanket impls,
    absence here does not cover an impl whose self type is a type parameter:
    the sentence points at the crate blanket list when `blankets_shown` (only
    `render_types_section`, which prints that list, passes it), and says the
    list is not shown otherwise."""
    if rec.external_path or rec.trait_impls is None or rec.kind == "trait":
        return []
    if not rec.blanket_impls:
        header = TRAIT_IMPLS_HEADER
    elif blankets_shown:
        header = TRAIT_IMPLS_HEADER_WITH_BLANKETS
    else:
        header = TRAIT_IMPLS_HEADER_BLANKETS_NOT_SHOWN
    out = [header]
    if not rec.trait_impls:
        out.append("    (none)")
    for t in rec.trait_impls:
        where = f"  // {t.file}" if t.file else ""
        out.append(f"    {t.header}{where}")
    return out


def blanket_impl_block(records: Sequence[TypeRecord]) -> str:
    """The crate's type-parameter impls, once. Empty when no record carries
    any: the per-type sentence then does not point at a list that is not
    here. The same headers are stored on every record of one enumeration;
    the first occurrence wins so the block is stable."""
    chosen: list[TraitImplRecord] = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        for t in rec.blanket_impls or ():
            key = (t.header, t.file)
            if key in seen:
                continue
            seen.add(key)
            chosen.append(t)
    if not chosen:
        return ""
    out = [BLANKET_IMPLS_HEADER]
    for t in chosen:
        where = f"  // {t.file}" if t.file else ""
        out.append(f"    {t.header}{where}")
    return "\n".join(out)


def _first_complete_header(block: str) -> int | None:
    found = [i for h in _COMPLETE_HEADERS if (i := block.find(h)) >= 0]
    return min(found) if found else None


def _fit_block(block: str, cap: int) -> str:
    """`block` when it fits. Otherwise a prefix that does not end inside a
    list whose header says the list is complete, plus a note that the list
    was omitted. A block with no such header is sliced: it makes no absence
    claim."""
    if len(block) <= cap:
        return block
    at = _first_complete_header(block)
    if at is None:
        return block[:cap]
    prefix = block[:at].rstrip()
    if not prefix:
        return _LIST_OMITTED[:cap]
    candidate = prefix + "\n" + _LIST_OMITTED
    if len(candidate) <= cap:
        return candidate
    room = cap - len(_LIST_OMITTED) - 1
    if room > 0:
        return prefix[:room].rstrip() + "\n" + _LIST_OMITTED
    return prefix[:cap]


def join_type_blocks(blocks: list[str], cap: int) -> str:
    """Join rendered type blocks under a char cap. A later block that does
    not fit is dropped whole. A first block that does not fit is cut before
    a COMPLETE list (`TRAIT_IMPLS_HEADER`, `BLANKET_IMPLS_HEADER`): slicing
    after that header would state that an impl the cut removed does not
    exist."""
    out: list[str] = []
    used = 0
    for b in blocks:
        sep = 2 if out else 0
        if used + sep + len(b) <= cap:
            out.append(b)
            used += sep + len(b)
            continue
        if not out:
            fitted = _fit_block(b, cap)
            if fitted:
                out.append(fitted)
        break
    return "\n\n".join(out)


def render_types_section(
    records: Sequence[TypeRecord],
    observables: frozenset[str] = frozenset(),
    *,
    viewer_file: str = "",
    cap: int | None = None,
) -> str:
    """Type blocks for a prompt, with the crate's blanket impls printed once
    ahead of them. The one renderer whose per-type sentence may point at the
    blanket list, because it is the one that prints it.

    `cap` drops later blocks whole and will not slice a complete list. The
    first record outranks the blanket list: when the two do not fit
    together, the list is left out (the per-type sentence then says it is
    not shown) and the first record is shown, cut before its own impl list
    if it alone exceeds the cap. Printing the list first and dropping the
    record whole would leave a section with no type in it."""
    blanket = blanket_impl_block(records)
    if blanket:
        shown = [_type_block(r, observables, viewer_file=viewer_file,
                             blankets_shown=True) for r in records]
        if cap is None:
            return "\n\n".join([blanket, *shown])
        if len(blanket) + 2 + len(shown[0]) <= cap:
            return join_type_blocks([blanket, *shown], cap)
    plain = [_type_block(r, observables, viewer_file=viewer_file,
                         blankets_shown=False) for r in records]
    if cap is None:
        return "\n\n".join(plain)
    return join_type_blocks(plain, cap)


def render_types_section_for_viewers(
    entries: Sequence[tuple[TypeRecord, str]],
    observables: frozenset[str] = frozenset(),
    *,
    cap: int | None = None,
) -> str:
    """One TYPES_IN_SCOPE for a prompt that shows several files: each entry
    is a record and the file it is rendered for (`viewer_file`), in rank
    order. The blanket list is printed once, and a block whose rendering is
    already in the section is not printed again — the same type seen from
    two files renders the same unless a field's accessibility differs, and
    then both renderings stay.

    A session's section used to be one `render_types_section` per slice
    file, concatenated with exact-string dedup of whole sections: on run
    `3203804-8c6a0066`'s fill-5 session (2026-11-03) 6,660 of the section's
    37,087 chars were blocks already printed above (`Result`, `Chain`,
    `core::fmt::Formatter`, `std::backtrace::Backtrace`), and the blanket
    list was printed once per file. `cap` behaves as in
    `render_types_section`: later blocks drop whole, so rank order decides
    what a narrower cap keeps."""
    records = [r for r, _ in entries]
    blanket = blanket_impl_block(records)

    def _blocks(shown: bool) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for rec, viewer in entries:
            block = _type_block(rec, observables, viewer_file=viewer,
                                blankets_shown=shown)
            if block in seen:
                continue
            seen.add(block)
            out.append(block)
        return out

    if blanket:
        shown = _blocks(True)
        if cap is None:
            return "\n\n".join([blanket, *shown])
        if not shown or len(blanket) + 2 + len(shown[0]) <= cap:
            return join_type_blocks([blanket, *shown], cap)
    plain = _blocks(False)
    if cap is None:
        return "\n\n".join(plain)
    return join_type_blocks(plain, cap)


def _slot_fit_lines(f: FieldRecord, type_file: str) -> list[str]:
    """For a fn-pointer field, the crate fns that fit it — the only names a
    table literal can put there — or the statement that none does. Nothing
    for any other field, or a record read without the crate's fns
    (`fits is None`): a list printed as complete must be complete."""
    if f.fits is None:
        return []
    if not f.fits:
        return ["      fits: NONE — no crate fn is declared with a signature "
                "this field can hold"]
    def _one(c: SlotFit) -> str:
        notes = [n for n in (c.file if c.file and c.file != type_file else "",
                             f"only if {c.requires}" if c.requires else "") if n]
        return f"{c.name} ({'; '.join(notes)})" if notes else c.name

    names = ", ".join(_one(c) for c in f.fits)
    return [f"      fits: {names}"]


def format_type_for_prompt(
    rec: TypeRecord, observables: frozenset[str] = frozenset(),
    *, viewer_file: str = "",
) -> str:
    """One type record on its own. It cannot point at the crate blanket list,
    because it does not print one; a prompt section is
    `render_types_section`."""
    return _type_block(rec, observables, viewer_file=viewer_file,
                       blankets_shown=False)


def _type_block(
    rec: TypeRecord, observables: frozenset[str] = frozenset(),
    *, viewer_file: str, blankets_shown: bool,
) -> str:
    """Render a type record as a compact Rust-like definition for prompts.

    The rendering includes a field-vs-method annotation: each name is shown with `.x`
    for fields and `.x()` for methods so the model doesn't confuse them.

    Methods are shown with their FULL SIGNATURE so the model knows
    exactly what each takes/returns. Without this it kept calling e.g.
    `Ref::new(NonNull<T>)` when `Ref::new` actually takes `&T` — visible
    method NAME but invisible argument types.

    The layout-deciding outer attributes (`repr`, `derive`) are rendered above
    the type line — see `_type_layout_attrs` — and the type's leading `///`
    doc lines above those (`_type_doc_lines`): the contract sentence t1.5
    authors on an item has no other route into a fill prompt, and was
    dropped here before that renderer existed.

    `observables` are the literal texts the target's attributed failing tests
    assert appear in some rendering; a doc region of this type that STATES one
    of them survives the leading-lines cap. Default empty renders exactly as
    before the argument existed.

    `viewer_file` is the file the prompt is writing. Given, a struct's fields
    split by `field_accessible_from`: the accessible ones render under the
    `fields (access with .name):` header as before, the ones private to
    another file under a `private fields` header that names both files and
    carries each field's accessor (`_private_field_lines`), and the
    `construct:` hint is dropped — a struct literal from another module
    cannot name a private field. Empty (every caller without a file) renders
    byte-identically to before the argument existed.
    """
    kind = rec.kind or "?"
    name = rec.name or "?"
    generics = rec.generics or ""
    fields = rec.fields
    variants = rec.variants
    methods = rec.methods
    definition = rec.definition or ""
    lines = [*_type_doc_lines(definition, observables),
             *_type_layout_attrs(definition),
             f"{kind} {name}{generics}"]
    private: list[FieldRecord] = []
    if fields:
        accessible: list[FieldRecord] = []
        for f in fields:
            (accessible if field_accessible_from(rec, f, viewer_file)
             else private).append(f)
        if accessible:
            lines.append(
                "  fields (access with .name; `fits:` under a fn-pointer field "
                "is the COMPLETE list of crate fns it can hold):"
                if any(f.fits is not None for f in accessible)
                else "  fields (access with .name):")
            for f in accessible:
                pub = "pub " if f.is_pub else ""
                lines.append(f"    {world_gate_attr(f)}{pub}{f.name}: {f.ty}"
                             f"{world_only_note(f)}")
                lines.extend(_slot_fit_lines(f, rec.file))
        if private:
            lines.extend(_private_field_lines(rec, private, viewer_file))
    if variants:
        lines.append("  variants:")
        for v in variants[:8]:
            lines.append(f"    {v}")
        if len(variants) > 8:
            lines.append(f"    ... ({len(variants) - 8} more)")
    # A struct literal written in another module cannot name a private field
    # (E0451), so the literal the hint shows is not writable there.
    _ctor = None if private else _construct_hint(rec)
    if _ctor:
        lines.append(_ctor)
    if methods:
        # Dedup by method name (same fn can appear in multiple impl blocks);
        # keep the first sig we saw. Then render WITH SIGNATURE.
        seen: set[str] = set()
        # (name, sig, is_exactly_pub, cfg gate, worlds note)
        sigs: list[tuple[str, str, bool, str, str]] = []
        for m in methods:
            mn = m.name
            if mn and mn not in seen:
                seen.add(mn)
                # Conjunction: is_exactly_pub defaults True when absent (pre-
                # field artifact / stale extractor) — alone it would render a
                # PRIVATE method as `pub`; is_pub can only narrow.
                sigs.append((mn, _trim_sig(m.sig),
                             bool(m.is_pub and m.is_exactly_pub),
                             world_gate_attr(m), world_only_note(m)))
        if sigs:
            lines.append("  methods (sig shown; call with .name(args)):")
            # Cap to top 10 methods to keep prompts compact for types with
            # many. The first 10 are typically the most-used.
            for mn, sig, is_pub, gate, only in sigs[:10]:
                # `pub ` is literal rendered text, so it must reflect TRUE
                # external visibility (is_exactly_pub), not `pub(crate)`.
                vis = "pub " if is_pub else ""
                if sig:
                    lines.append(f"    {gate}{vis}{sig}{only}")
                else:
                    lines.append(f"    {gate}{vis}fn {mn}(...)  // sig unknown{only}")
            if len(sigs) > 10:
                rest = ", ".join(n for n, *_ in sigs[10:])
                if len(rest) > 200:
                    rest = rest[:197] + "..."
                lines.append(f"    // also: {rest}")
    # After the methods, so a section cap that cannot fit this record cuts
    # the complete impl list off the end and keeps the fields and methods.
    lines.extend(_trait_impl_lines(rec, blankets_shown=blankets_shown))
    return "\n".join(lines)


def build_dispatch_family_section(
    family: DispatchFamily, *, single_call_char_limit: int,
) -> str:
    """Render the <DISPATCH_FAMILY> prompt block: the family's traits, the
    dispatch method, and the dispatching macro_rules! source — the behavioural
    spec the co-design fill must satisfy. Macro defs are capped so the section
    can't dominate the budget."""
    traits = ", ".join(family.traits)
    method = family.method
    macros = ", ".join(family.macros)
    chained = ", ".join(family.chained_methods)
    defs = list(family.macro_defs)
    _cap = max(2000, single_call_char_limit // 8)
    macro_text = "\n\n".join(defs)
    if len(macro_text) > _cap:
        macro_text = macro_text[:_cap] + "\n/* … macro truncated … */"
    tag_list = family.return_types
    trait_list = family.traits
    # Concrete WRONG→RIGHT examples (generic placeholder names, NOT crate
    # source) — the model acts on examples, not on "autoref-specialization"
    # jargon. Two examples so the pattern generalises.
    ex = (
        "Each trait's `" + (method or "kind") + "` returns ITS OWN distinct tag; "
        "the macro chains a builder on that tag. Get any one tag/return wrong and "
        "the macro silently resolves to the wrong branch everywhere.\n"
        "  WRONG (two impls return the SAME tag → dispatch collapses):\n"
        "    impl KindA for A { fn kind(self) -> TagA { TagA } }\n"
        "    impl KindB for B { fn kind(self) -> TagA { TagA } }   // should be TagB\n"
        "  RIGHT (each returns its OWN tag; builders match per tag):\n"
        "    impl KindA for A { fn kind(self) -> TagA { TagA } }\n"
        "    impl KindB for B { fn kind(self) -> TagB { TagB } }\n"
        "    impl TagA { fn build(self, x: A) -> Out { /* A-specific */ } }\n"
        "    impl TagB { fn build(self, x: B) -> Out { /* B-specific */ } }\n"
        "  WRONG (inventing one shared name across impls — there is no such item):\n"
        "    fn kind(self) -> Tag { SHARED_TAG }                   // E0425, undefined\n"
        "  RIGHT (return the per-impl tag value directly):\n"
        "    fn kind(self) -> TagA { TagA }\n"
    )
    _map = ""
    if trait_list and tag_list and len(trait_list) == len(tag_list):
        _map = ("In THIS family map each trait to its own tag: "
                + ", ".join(f"`{t}`→`{g}`" for t, g in zip(trait_list, tag_list, strict=False))
                + (f", then `.{chained}(...)` builds the result on that tag. "
                   if chained else ". "))
    return (
        "\n<DISPATCH_FAMILY>\n"
        f"`{traits}` are dispatched together by macro(s) `{macros}` "
        f"(`x.{method}()" + (f".{chained}()" if chained else "") + "...`) — fill "
        "them in this one response, consistently.\n"
        f"{ex}{_map}"
        "The dispatching macro (the behaviour these must satisfy):\n```rust\n"
        f"{macro_text}\n```\n</DISPATCH_FAMILY>\n"
    )


from pipeline_core.domain.text import smart_truncate


def format_api_surface_section(api_surface: str, *, cap: int) -> str:
    """Wrap the captured API surface into a labelled prompt section, head+tail
    truncated to `cap`. Returns '' if api_surface is empty."""
    if not api_surface or not api_surface.strip():
        return ""
    text = smart_truncate(
        api_surface,
        total_limit=cap,
        head=2 * cap // 3,
        tail=cap // 3,
    )
    return (
        "\n--- Public API surface (ground truth — pre-strip skeleton) ---\n"
        "Use these signatures and type definitions exactly. Do not invent items not\n"
        "listed here, do not change signatures, and do not contradict the shapes shown\n"
        "below. The behavioral semantics (what each function returns, invariants)\n"
        "are still derived from the tests; this skeleton only defines the SHAPE.\n"
        f"{text}\n"
    )

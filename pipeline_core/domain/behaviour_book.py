"""The behaviour book: the harvested examples as a specification — Feature →
Rule → Example — with a typed machine view (the suite JSON) and a generated,
read-only Gherkin view.

Why. Until 2026-09-29 the suite was an index of the crate's own assertions:
197 of the anyhow `stable` suite's 231 specs were `testassert` rows that each
carried their whole test item, grouped by `BehaviourClass` — the verifier's
categories (how an observable is compared), not the crate's capabilities —
and nothing stated the invariant an example illustrates. The 2026-08-26
file-first `construct` fill had the quoted test, the panic line and
`#[repr(transparent)]`, reasoned about the TypeId wrap, and still wrapped:
the missing slot was the INSTRUCTION, and the store had no field that could
hold one. A Rule is instruction-slot content: one sentence in the crate's
public vocabulary, kept only when it makes the examples it cites
predictable.

What this module holds, all pure:

- the GLOSSARY — the crate's public names (what an example crate can call:
  `is_public_crate_fn`, public types, `#[macro_export]` macros) with the
  first sentence of each item's doc comment, read pre-strip at t0d;
- FEATURE grouping — each example goes under the public item whose result
  it observes (the outermost call, a qualified fn by its full spelling),
  looked for in its `when` first, then its setup, then its helpers; an
  example naming no public item goes under the crate itself, so every
  example lands somewhere by construction;
- RULE drafting and checking — parse a model's `<RULE covers="…">` blocks,
  refuse a sentence that names a private item or restates a cited Then
  verbatim, and judge a chunk's drafts as one set (`judge_rule_set`): a
  rule is kept for the examples the set predicts, the checker without any
  rule misses, and the set without that rule misses. A sentence the checker
  can ignore is not an instruction (the ablation is the control a plain
  "predict with the rule" check lacks — for an `assert_eq!` the when already
  is the expression and the Then the literal);
- AIMING — which rules a fill of given units carries, keyed on the unit's own
  declaration (name, container) the way the identity instruction is aimed,
  never on coverage `exercises` (coverage of the original put 55 of 85
  stubbed units under the identity MUST on 2026-08-28);
- the GHERKIN view — `render_feature` and `render_givens_index`, generated
  from the model, never read. A Given is a pointer to a test-file item;
  the item's text is printed once, in the index.

No function here names a crate, a test, a trait or a language construct:
the glossary, the grouping and the aiming key on the extractor's records and
on the examples' own text.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from pipeline_core.domain.behaviour import (
    BehaviourClass,
    BehaviourSpec,
    ExampleGiven,
    Feature,
    GlossaryEntry,
    ObservableOutcome,
    OutcomeKind,
    Rule,
    example_code,
    example_given,
    example_when,
    helper_items,
    outcome_matches,
    then_value_text,
)
from pipeline_core.domain.behaviour_probe import is_public_crate_fn
from pipeline_core.domain.candidate_tiers import culprits, feature_reach
from pipeline_core.domain.cfg import CfgSet
from pipeline_core.domain.example_setup import IMPORTS_KEY_SUFFIX, helper_key_parts
from pipeline_core.domain.models import FnRecord, TypeRecord
from pipeline_core.domain.rust_idents import impl_self_type, impl_trait_name
from pipeline_core.domain.rust_lex import code_mask
from pipeline_core.domain.text import strip_think_blocks

# The feature an example that names no public item is grouped under.
CRATE_FEATURE = "(crate)"
# Rules one fill prompt carries. Three matches the identity instruction's cap:
# enough for a unit's own feature plus a sibling, not a dump.
RULES_PER_PROMPT = 3
# A cited Then this long, quoted verbatim in a rule, is a restatement.
_RESTATEMENT_MIN = 12
_SUMMARY_CAP = 200

_IDENT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


# --- glossary ---------------------------------------------------------------

def _byte_to_char(text: str, byte_offset: int) -> int:
    return len(text.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))


def doc_summary_before(text: str, offset: int) -> str:
    """First sentence of the `///` block that documents the item whose
    declaration contains char `offset`: the doc lines directly above it,
    skipping attribute lines between. Code fences end the summary."""
    line_start = text.rfind("\n", 0, max(0, offset)) + 1
    lines = text[:line_start].split("\n")[:-1]
    doc: list[str] = []
    for raw in reversed(lines):
        t = raw.strip()
        if t.startswith("///"):
            doc.append(t[3:].strip())
            continue
        if t.startswith("#[") or t.startswith("#!") or (doc == [] and t.startswith("//")
                                                         and not t.startswith("///")):
            if doc:
                break
            continue
        break
    doc.reverse()
    body: list[str] = []
    for ln in doc:
        if ln.startswith("```"):
            break
        body.append(ln)
    joined = " ".join(" ".join(body).split())
    for stop in (". ", ".\n"):
        k = joined.find(stop)
        if k >= 0:
            joined = joined[:k + 1]
            break
    return joined[:_SUMMARY_CAP]


def exported_macros(sources: Iterable[tuple[str, str]]) -> list[tuple[str, str, int]]:
    """`(file, name, offset)` of every `macro_rules! NAME` preceded (through
    attribute and doc lines) by `#[macro_export]` and not `#[doc(hidden)]`
    (an exported support macro the crate's own macros expand to is not part
    of the vocabulary a user writes)."""
    out: list[tuple[str, str, int]] = []
    for rel, text in sources:
        mask = code_mask(text)
        i = 0
        while (at := text.find("macro_rules!", i)) >= 0:
            i = at + 12
            if mask[at]:
                continue
            j = i
            while j < len(text) and text[j].isspace():
                j += 1
            s = j
            while j < len(text) and text[j] in _IDENT:
                j += 1
            name = text[s:j]
            if not name:
                continue
            line_start = text.rfind("\n", 0, at) + 1
            exported = hidden = False
            for raw in reversed(text[:line_start].split("\n")[:-1]):
                t = raw.strip()
                if t.startswith("#[macro_export"):
                    exported = True
                    continue
                if t.replace(" ", "").startswith("#[doc(hidden"):
                    hidden = True
                    continue
                if t.startswith("#[") or t.startswith("///") or t.startswith("//"):
                    continue
                break
            if exported and not hidden:
                out.append((rel, name, at))
    return out


def _source_for(file: str, sources: Mapping[str, str]) -> str:
    if file in sources:
        return sources[file]
    for rel, text in sources.items():
        if rel.endswith("/" + file) or file.endswith("/" + rel):
            return text
    return ""


def glossary_entries(
    fn_records: Sequence[FnRecord],
    type_records: Sequence[TypeRecord],
    sources: Iterable[tuple[str, str]],
    *,
    reachable_files: frozenset[str] | None = None,
    cfg_set: CfgSet | None = None,
    public_index: Mapping[str, frozenset[str]] | None = None,
) -> tuple[GlossaryEntry, ...]:
    """The crate's public vocabulary, sorted by name.

    Types: exactly-`pub` struct/enum/trait/type records at file scope in a
    reachable file (no external records). Fns: what an example crate can call
    (`is_public_crate_fn`), spelled as a user spells them — `Type::fn` for an
    inherent method, `Trait::fn` for a method of one of those public traits
    (declared or implemented), `fn` for a free fn; a method of a trait the
    crate does not declare (`Display::fmt`) is the std vocabulary, not the
    crate's, and is left out. Macros: `#[macro_export]` `macro_rules!`.
    Each entry carries the first sentence of its doc comment; a fn also
    carries its declaration (`sig_text`). Two records of one spelling merge
    field-wise (`GlossaryEntry.merged`), so the signature of the record
    without a doc comment is not lost to the one with it."""
    src = dict(sources)
    # With a measured public index (``public_index``, rustdoc's names by
    # defining file) a type is in the vocabulary when the index lists it —
    # a `pub use` from a private module included, a `pub` trait of a private
    # inline module nobody re-exports excluded. Without one, the mod-chain
    # walk (``reachable_files``) and file-scope declaration stand in.
    types = [t for t in type_records
             if t.is_pub and t.is_exactly_pub and not t.external_path
             and t.kind in ("struct", "enum", "trait", "type")
             and (t.name in public_index.get(t.file, frozenset())
                  if public_index else
                  not t.mod_path
                  and (reachable_files is None or t.file in reachable_files))]
    type_files = {t.name: t.file for t in types}
    public_traits = {t.name for t in types if t.kind == "trait"}
    out: dict[str, GlossaryEntry] = {}
    for t in types:
        text = _source_for(t.file, src)
        m = re.search(rf"\b(?:struct|enum|trait|type)\s+{re.escape(t.name)}\b", text)
        summary = doc_summary_before(text, m.start()) if m else ""
        out.setdefault(t.name, GlossaryEntry(t.name, t.kind, summary))
    for rec in fn_records:
        name = _user_spelling(rec, public_traits)
        if not name or rec.is_test:
            continue
        # A method of a public crate trait is callable through the trait
        # whatever its own (absent) visibility says: trait impl methods carry
        # no `pub`, and the impl's self type may be a std type (`Result`).
        via_trait = name.split("::", 1)[0] in public_traits and "::" in name
        if not via_trait and not is_public_crate_fn(rec, reachable_files, type_files, cfg_set,
                                                    public_index):
            continue
        text = _source_for(rec.file, src)
        summary = doc_summary_before(text, _byte_to_char(text, rec.sig_byte_start)) if text else ""
        entry = GlossaryEntry(name, "fn", summary, signature=" ".join(rec.sig_text.split()))
        prior = out.get(name)
        out[name] = entry if prior is None else prior.merged(entry)
    for rel, name, at in exported_macros(src.items()):
        out.setdefault(f"{name}!", GlossaryEntry(f"{name}!", "macro",
                                                 doc_summary_before(src[rel], at)))
    return tuple(out[k] for k in sorted(out))


def _user_spelling(rec: FnRecord, public_traits: Collection[str]) -> str:
    c = rec.container.strip()
    if not c:
        return rec.fn_name
    if c.startswith("trait"):
        trait = c[len("trait"):].strip().split("<", 1)[0].split()[0] if c[5:].strip() else ""
        return f"{trait}::{rec.fn_name}" if trait in public_traits else ""
    impl_trait = impl_trait_name(c)
    if impl_trait:
        return f"{impl_trait}::{rec.fn_name}" if impl_trait in public_traits else ""
    self_ty = impl_self_type(c)
    return f"{self_ty}::{rec.fn_name}" if self_ty else ""


def private_names(
    fn_records: Sequence[FnRecord], type_records: Sequence[TypeRecord],
    glossary: Sequence[GlossaryEntry],
    specs: Sequence[BehaviourSpec] = (),
) -> frozenset[str]:
    """Names of the crate's items an example crate cannot name: fns and types
    that are not in the glossary under any spelling. A rule mentioning one is
    describing the implementation, not the observable. A name the examples'
    own code uses (`len`, `next`, a test's `StdError` alias) is the tests'
    vocabulary whatever the crate calls its private items, and is not
    private."""
    public = {g.leaf for g in glossary} | {g.name for g in glossary}
    used: set[str] = set()
    for s in specs:
        used.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", example_code(s)))
    out = {r.fn_name for r in fn_records if r.fn_name and not r.is_test}
    out |= {t.name for t in type_records if t.name and not t.external_path}
    return frozenset(n for n in out if n not in public and n not in used)


# --- name occurrences in code -------------------------------------------------

def feature_key_of(name: str) -> str:
    """The feature a glossary spelling belongs to: the spelling itself.

    A qualified fn is not collapsed onto its leaf. `Error::new` and
    `Chain::new` are different features; aiming by the leaf handed one
    rule to every `new`.
    """
    return name


def feature_spellings(feature: str, glossary: Sequence[GlossaryEntry]) -> tuple[str, ...]:
    """The glossary names a rule of `feature` may aim by.

    The feature's own spelling, when the glossary has that name. A leaf
    that belongs to exactly one qualified fn aims by that spelling, so a
    method call (`.msg(`) still reaches the unit that declares it. A leaf
    shared by several fns aims by none of them: examples that wrote
    `Type::leaf` grouped under that spelling, and adding every same-named
    fn is how one `new` reached every `new`.
    """
    exact = tuple(sorted(g.name for g in glossary if g.name == feature))
    if exact:
        return exact
    owners = tuple(sorted(
        g.name for g in glossary
        if g.kind == "fn" and "::" in g.name and g.leaf == feature))
    if len(owners) == 1:
        return owners
    return ()


@dataclass(frozen=True, slots=True)
class _Vocab:
    """The glossary indexed for scanning."""

    macros: frozenset[str]  # bare macro names
    fn_leaves: frozenset[str]
    qualified: frozenset[tuple[str, str]]  # (qualifier, leaf)
    free_fns: frozenset[str]
    types: frozenset[str]
    kind_of: Mapping[str, str]
    crate: str = ""

    @classmethod
    def of(cls, glossary: Sequence[GlossaryEntry], crate: str = "") -> _Vocab:
        macros = {g.name.rstrip("!") for g in glossary if g.kind == "macro"}
        fns = [g for g in glossary if g.kind == "fn"]
        return cls(
            macros=frozenset(macros),
            fn_leaves=frozenset(g.leaf for g in fns),
            qualified=frozenset((g.name.rsplit("::", 1)[0], g.leaf) for g in fns if "::" in g.name),
            free_fns=frozenset(g.name for g in fns if "::" not in g.name),
            types=frozenset(g.name for g in glossary if g.kind not in ("fn", "macro")),
            kind_of=_kind_of(glossary),
            crate=crate,
        )

    def resolved_in(self, code: str) -> _Vocab:
        """This vocabulary as `code` can see it: a crate FREE fn is in scope
        only when `code` imports it (`use krate::{…, name}`), glob-imports
        the crate, or spells the crate path (`krate::name(`). A bare `Ok(`
        in a test that never imported the crate's `Ok` is the prelude's —
        Rust's own resolution, not a list of prelude names."""
        imported: set[str] = set()
        glob = False
        for m in re.finditer(r"\buse\s+([^;]*);", code):
            body = m.group(1)
            root = body.split("::", 1)[0].strip()
            if self.crate and root not in (self.crate, "crate"):
                continue
            imported.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body))
            glob = glob or body.rstrip().endswith("*")
        qualified = {f for f in self.free_fns
                     if self.crate and re.search(rf"\b{re.escape(self.crate)}::{re.escape(f)}\b", code)}
        keep = self.free_fns if glob else frozenset(f for f in self.free_fns
                                                     if f in imported or f in qualified)
        return replace(self, free_fns=keep)

    def foreign_path(self, code: str, start: int) -> bool:
        """True when the path segment starting at `start` is reached through
        a longer path that is not this crate's (`io::Error`, not
        `anyhow::Error` or `crate::Error`): the name belongs to another
        crate."""
        if not code.startswith("::", start - 2):
            return False
        k = start - 2
        while k > 0 and code[k - 1] in _IDENT:
            k -= 1
        root = code[k:start - 2]
        if code.startswith("::", k - 2):
            return self.foreign_path(code, k)
        return root not in ("crate", "self", "super", self.crate)


def _kind_of(glossary: Sequence[GlossaryEntry]) -> dict[str, str]:
    """Feature key → kind. A method call spells only the leaf, so the leaf
    is a call too; a qualified call spells `Type::leaf` and that key is its
    own feature."""
    kind = {g.name: g.kind for g in glossary}
    for g in glossary:
        if g.kind == "fn" and "::" in g.name:
            kind[g.leaf] = "fn"
    return kind


def name_hits(code: str, vocab: _Vocab) -> list[tuple[int, str]]:
    """`(offset, feature key)` for each public name `code` uses, outside
    literals and comments, in order. A macro counts as `name!`; a method as
    `.leaf(` / `.leaf::<`; a qualified call as `Type::leaf` (the glossary
    spelling, not the leaf); a free fn as a bare `leaf(`; a type as the word."""
    mask = code_mask(code)
    out: list[tuple[int, str]] = []
    n = len(code)
    i = 0
    while i < n:
        if mask[i] or code[i] not in _IDENT or code[i].isdigit():
            i += 1
            continue
        s = i
        while i < n and code[i] in _IDENT:
            i += 1
        word = code[s:i]
        before = code[s - 1] if s > 0 else ""
        if before in _IDENT:
            continue
        j = i
        while j < n and code[j] == " ":
            j += 1
        nxt = code[j] if j < n else ""
        if word in vocab.macros and nxt == "!" and not code.startswith("!=", j):
            out.append((s, f"{word}!"))
            continue
        if word in vocab.fn_leaves:
            if before == "." and (nxt == "(" or code.startswith("::<", j)):
                out.append((s, word))
                continue
            if code.startswith("::", s - 2):
                q_end = s - 2
                q_start = q_end
                while q_start > 0 and code[q_start - 1] in _IDENT:
                    q_start -= 1
                if (code[q_start:q_end], word) in vocab.qualified \
                        and not vocab.foreign_path(code, q_start):
                    out.append((s, f"{code[q_start:q_end]}::{word}"))
                    continue
            if nxt == "(" and word in vocab.free_fns and (
                    before not in (".", ":")
                    or (vocab.crate and code.startswith(f"{vocab.crate}::", s - len(vocab.crate) - 2))):
                out.append((s, word))
                continue
        if word in vocab.types and before != "." and not vocab.foreign_path(code, s):
            # A type that qualifies a public fn (`Error::msg`) is the fn's
            # path, not a use of the type on its own.
            if code.startswith("::", i):
                k = i + 2
                while k < n and code[k] in _IDENT:
                    k += 1
                if (word, code[i + 2:k]) in vocab.qualified:
                    continue
            out.append((s, word))
    return out


def _depths(code: str) -> list[int]:
    """Bracket depth at each offset of `code`, literals and comments skipped."""
    mask = code_mask(code)
    out = [0] * (len(code) + 1)
    d = 0
    for i, c in enumerate(code):
        out[i] = d
        if mask[i]:
            continue
        if c in "([{":
            d += 1
        elif c in ")]}":
            d = max(0, d - 1)
    out[len(code)] = d
    return out


def outermost_hit(code: str, vocab: _Vocab) -> str | None:
    """The feature key of the public item whose RESULT `code` observes: the
    call at the shallowest bracket depth, and among those the last one — in
    a method chain `a.b().c()` the call applied last is the outermost."""
    hits = name_hits(code, vocab)
    if not hits:
        return None
    # A call observes; a type named in a signature or a turbofish does not.
    # Types decide only when the code calls nothing public.
    calls = [h for h in hits if vocab.kind_of.get(h[1]) in ("fn", "macro")]
    depth = _depths(code)
    _pos, key = min(calls or hits, key=lambda h: (depth[h[0]], -h[0]))
    return key


def _binding_initializer(setup: str, name: str) -> str:
    """The initializer of the LAST `let <name> = …;` in `setup`, or ""."""
    found = ""
    for m in re.finditer(rf"\blet\s+(?:mut\s+)?{re.escape(name)}\s*(?::[^=]*)?=(?!=)", setup):
        rest = setup[m.end():]
        depth = 0
        end = len(rest)
        mask = code_mask(rest)
        for k, c in enumerate(rest):
            if mask[k]:
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif c == ";" and depth == 0:
                end = k
                break
        found = rest[:end]
    return found


# --- feature grouping ---------------------------------------------------------

def group_features(
    specs: Sequence[BehaviourSpec], glossary: Sequence[GlossaryEntry], *, crate: str = "",
) -> tuple[Feature, ...]:
    """Every example under exactly one feature, features sorted by key.

    The feature is the public item whose RESULT the example observes: the
    outermost public call of its `when` (:func:`outermost_hit`). A `when`
    that is a bare binding (`test`, handed to an asserting helper) is read
    through the binding's initializer in the setup (`|| Ok(ensure!(…))` →
    `ensure!`). A `when` naming no public item falls back to the setup's
    statements, last first, then to the helpers it calls, in order. An
    example naming no public item anywhere is grouped under
    :data:`CRATE_FEATURE`, so no example is dropped by grouping. `crate` is
    the crate's module name: a path through it names this crate's item, a
    path through any other root (`io::Error::new`) does not."""
    base = _Vocab.of(glossary, crate)
    members: dict[str, list[str]] = {}
    for s in specs:
        vocab = base.resolved_in(example_code(s))
        when = s.when.strip()
        if when and all(c in _IDENT for c in when):
            when = _binding_initializer(s.setup, when) or when
        called = [t for k, t in helper_items(s)
                  if not k.endswith(IMPORTS_KEY_SUFFIX)]
        sections = [when, *reversed(_statements_of(s.setup)), *called]
        key = next((k for k in (outermost_hit(sec, vocab) for sec in sections) if k), CRATE_FEATURE)
        members.setdefault(key, []).append(s.behaviour_id)
    return tuple(
        Feature(item=k, kind=base.kind_of.get(k, "crate"), example_ids=tuple(ids))
        for k, ids in sorted(members.items()))


def _statements_of(setup: str) -> list[str]:
    """`setup` split at depth-0 `;`, in order."""
    out: list[str] = []
    depth = 0
    start = 0
    mask = code_mask(setup)
    for k, c in enumerate(setup):
        if mask[k]:
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            out.append(setup[start:k])
            start = k + 1
    if setup[start:].strip():
        out.append(setup[start:])
    return [x for x in out if x.strip()]


def rule_checkable(spec: BehaviourSpec) -> bool:
    """An example a rule can be checked against: it has a runtime Then (a
    doc fence's asserts and a compile-time pin have nothing to predict)."""
    return spec.then is not None and spec.then.kind is not OutcomeKind.STATIC_ASSERTION


# --- rules: drafting, refusal, the check --------------------------------------

@dataclass(frozen=True, slots=True)
class RuleDraft:
    """A model's proposed rule, before the check."""

    sentence: str
    covers: tuple[str, ...]


_RULE_RE = re.compile(r"<RULE\s+covers\s*=\s*\"([^\"]*)\"\s*>(.*?)</RULE>", re.S | re.I)
_THEN_RE = re.compile(r"<THEN\s+id\s*=\s*\"([^\"]*)\"\s*>(.*?)</THEN>", re.S | re.I)


def parse_rule_drafts(reply: str) -> list[RuleDraft]:
    """Every `<RULE covers="id,id">sentence</RULE>` block, in order;
    whitespace in the sentence collapsed; ids split on commas/space."""
    out: list[RuleDraft] = []
    for covers, body in _RULE_RE.findall(strip_think_blocks(reply)):
        sentence = " ".join(body.split())
        ids = tuple(x for x in re.split(r"[,\s]+", covers.strip()) if x)
        if sentence and ids:
            out.append(RuleDraft(sentence, ids))
    return out


def parse_predictions(reply: str) -> dict[str, str]:
    """`id → predicted Then text` from `<THEN id="…">…</THEN>` blocks. The
    text is kept exact except one leading and one trailing newline (a model
    that puts the value on its own line)."""
    out: dict[str, str] = {}
    for bid, body in _THEN_RE.findall(strip_think_blocks(reply)):
        text = body[1:] if body.startswith("\n") else body
        text = text[:-1] if text.endswith("\n") else text
        out.setdefault(bid.strip(), text)
    return out


def _mentions(sentence: str, name: str) -> bool:
    """`name` used as a term in prose: backticked, called (`name(`), or
    qualified (`X::name`) — or anywhere as a whole word when it cannot be an
    English word (CamelCase, `snake_case`, a `name!` macro)."""
    if not name:
        return False
    bare = name.rstrip("!")
    esc = re.escape(bare)
    if re.search(rf"`[^`]*\b{esc}\b[^`]*`", sentence):
        return True
    if re.search(rf"(?<![\w.]){esc}\s*\(|::{esc}\b|\b{esc}!", sentence):
        return True
    distinctive = name.endswith("!") or "_" in bare or any(c.isupper() for c in bare[1:]) \
        or bare[:1].isupper()
    return distinctive and re.search(rf"(?<![\w]){esc}(?![\w])", sentence) is not None


def rule_terms(sentence: str, glossary: Sequence[GlossaryEntry]) -> tuple[str, ...]:
    """The glossary names `sentence` uses, sorted.

    A qualified name counts only when the sentence spells that full name.
    Matching the leaf inside ``Type::leaf`` would mark every other fn of
    that leaf as used, so a rule whose sentence only says ``Error::new``
    would also aim at ``Chain::new``. A bare fn or a non-fn still matches
    by its own mention.
    """
    out: set[str] = set()
    for g in glossary:
        if "::" in g.name:
            if g.name in sentence:
                out.add(g.name)
        elif _mentions(sentence, g.leaf if g.kind == "fn" else g.name):
            out.add(g.name)
    return tuple(sorted(out))


# A sentence end inside prose: `.` or `?` closing a word (optionally inside a
# closing quote or bracket), then space, then a capital opening the next
# sentence. `!` is not one: in this prose `name!` spells a macro. Nor is the
# last dot of a dotted abbreviation (`e.g.`).
_SENTENCE_BREAK = re.compile(r"(?<=[^\W_])(?<!\.[^\W_])[.?][\"')\]]*\s+(?=[A-Z])")
_CODE_SPAN = re.compile(r"`[^`]*`")


def sentence_breaks(text: str) -> int:
    """How many sentence ends `text` has before its last sentence: 0 for one
    sentence, whatever its length.

    Code spans are read as one word each, so a `.` inside one (a path, a
    quoted message format) ends nothing. A break is a `.` or `?` that closes
    a word and is followed by a capitalised word. The test errs toward one
    sentence: a draft it misreads as one is still judged by the set on what
    it predicts, a draft it misreads as several is dropped unjudged.

    Why not a length. The bound here was 400 characters. On run
    `72627-751534fe` (2026-09-30) five of 69 drafts exceeded it (413 to 471
    chars), every one a single sentence stating a whole message format, and
    four were one half of the largest feature's base + suffix pair; with
    them judged, that feature's credited examples went 6 to 68 of 101 on the
    nightly book."""
    return len(_SENTENCE_BREAK.findall(_CODE_SPAN.sub("C", text)))


def sentence_refusal(
    sentence: str,
    cited: Sequence[BehaviourSpec],
    private: Collection[str],
) -> str | None:
    """Why a model-written sentence about `cited` examples is refused on its
    text alone, else None: it is more than one sentence, it names a private
    item, or it quotes a cited example's Then verbatim. The one mechanical
    rule a rule draft and a given phrase both pass."""
    if sentence_breaks(sentence):
        return "more than one sentence"
    for name in sorted(private):
        if _mentions(sentence, name):
            return f"names the private item `{name}`"
    for s in cited:
        if s.then is None:
            continue
        value = then_value_text(s.then)
        if len(value) >= _RESTATEMENT_MIN and value in sentence:
            return f"quotes {s.behaviour_id}'s Then verbatim"
    return None


def draft_refusal(
    draft: RuleDraft,
    examples: Mapping[str, BehaviourSpec],
    private: Collection[str],
) -> str | None:
    """Why a draft is dropped before the check, else None: it cites no
    checkable example of its feature, it is more than one sentence, it
    names a private item, or it quotes a cited example's Then verbatim (a
    restatement predicts that example and nothing else)."""
    cited = [examples[i] for i in draft.covers if i in examples]
    if not cited:
        return "cites no checkable example of its feature"
    return sentence_refusal(draft.sentence, cited, private)


# `predict(rule sentences, example ids) -> {id: predicted Then}`: one call of
# the checker. An empty sentence list is the ablation.
Predict = Callable[[Sequence[str], Sequence[str]], Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class CreditedRule:
    """A draft the set judge keeps: the examples it is credited with and the
    cited examples its set answered and got wrong."""

    draft: RuleDraft
    credited: tuple[str, ...]
    refuted_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SetVerdict:
    """One chunk's judgment: the kept rules in draft order, one drop reason
    per dropped draft, and the checker calls spent."""

    kept: tuple[CreditedRule, ...]
    dropped: tuple[str, ...]
    calls: int


def judge_rule_set(
    drafts: Sequence[RuleDraft],
    examples: Mapping[str, BehaviourSpec],
    *,
    predict: Predict,
) -> SetVerdict:
    """Judge a chunk's surviving drafts as ONE set, and keep each for what
    it contributes to that set.

    Calls: the ablation (no rule) and the set (every draft) over every
    example of `examples`; then, only when the set GAINED an example (the
    set answers it right, the ablation answers it wrong), one leave-one-out
    call per draft citing a gained example, over those gained examples,
    carrying the set without that draft. A draft alone in its set needs no
    leave-one-out: the set without it is the ablation.

    Credit. A draft is NECESSARY for a gained example when its leave-one-out
    answered that example and got it wrong; every necessary draft is
    credited. A gained example no citing draft is necessary for (two drafts
    that say the same thing) is credited to its first citing draft in draft
    order — only when every citing draft's leave-one-out answered it and got
    it right, so duplicates keep one copy, not zero. A draft is kept iff it
    is credited with at least one example; its `refuted_on` are the cited
    examples the set answered wrong. A cited example the set answered wrong
    is recorded, not a veto; a cited example no gain covers earns nothing.

    Silence is never a miss. An empty reply parses to `{}`, and reading the
    gap as "got it wrong" keeps a restatement (the ablation side) or credits
    a draft its leave-one-out never judged. An id absent from the ablation,
    the set or a leave-one-out is not a gain, not a refutation and not a
    necessity.

    Why a set. On run `72627-751534fe` (2026-09-30) the largest feature's
    drafts split one message into a base sentence and a suffix sentence
    ("the base message above"); each was checked alone, each predicted
    nothing alone, and the feature kept 0 rules over 103 examples. And the
    old verdict was all-or-nothing over up to 24 cited examples, so one
    predictor slip convicted a correct sentence."""
    order = list(examples)
    cited = [tuple(dict.fromkeys(i for i in d.covers if i in examples)) for d in drafts]
    sentences = [d.sentence for d in drafts]
    if not drafts:
        return SetVerdict((), (), 0)

    def right(reply: Mapping[str, str], bid: str) -> bool | None:
        """True/False when `reply` answered `bid`, None when it did not."""
        then = examples[bid].then
        if bid not in reply or then is None:
            return None
        return outcome_matches(reply[bid], then)

    ablation = predict((), order)
    with_set = predict(sentences, order)
    calls = 2
    gained = [i for i in order if right(with_set, i) is True and right(ablation, i) is False]
    refuted = [tuple(i for i in c if right(with_set, i) is False) for c in cited]
    gained_set = set(gained)
    necessary: list[set[str]] = [set() for _ in drafts]
    judged_right: list[set[str]] = [set() for _ in drafts]
    loo_silent: list[set[str]] = [set() for _ in drafts]
    for k, c in enumerate(cited):
        mine = [i for i in c if i in gained_set]
        if not mine:
            continue
        if len(drafts) == 1:
            without: Mapping[str, str] = ablation
        else:
            without = predict(sentences[:k] + sentences[k + 1:], mine)
            calls += 1
        for i in mine:
            verdict = right(without, i)
            if verdict is False:
                necessary[k].add(i)
            elif verdict is True:
                judged_right[k].add(i)
            else:
                loo_silent[k].add(i)
    credited: list[set[str]] = [set(n) for n in necessary]
    for i in gained:
        citing = [k for k, c in enumerate(cited) if i in c]
        if not citing or any(i in necessary[k] for k in citing):
            continue
        if all(i in judged_right[k] for k in citing):
            credited[citing[0]].add(i)
    kept: list[CreditedRule] = []
    dropped: list[str] = []
    for k, d in enumerate(drafts):
        if credited[k]:
            kept.append(CreditedRule(d, tuple(i for i in order if i in credited[k]), refuted[k]))
            continue
        dropped.append(_no_credit_reason(
            cited[k], gained_set, refuted[k], loo_silent[k],
            set_silent=[i for i in cited[k] if right(with_set, i) is None],
            ablation_silent=[i for i in cited[k] if right(ablation, i) is None]))
    return SetVerdict(tuple(kept), tuple(dropped), calls)


def _no_credit_reason(
    cited: Sequence[str],
    gained: Collection[str],
    refuted: Sequence[str],
    loo_silent: Collection[str],
    *,
    set_silent: Sequence[str],
    ablation_silent: Sequence[str],
) -> str:
    """Why a draft earned no credit, most specific first."""
    n = len(cited)
    mine = [i for i in cited if i in gained]
    if mine and loo_silent:
        return f"leave-one-out did not answer {len(loo_silent)} of its {len(mine)} gained example(s)"
    if mine:
        return f"not necessary for its {len(mine)} gained example(s)"
    if refuted:
        return f"refuted on {len(refuted)} of its {n} example(s)"
    if set_silent:
        return f"set did not answer {len(set_silent)} of its {n} example(s)"
    if ablation_silent:
        return f"ablation did not answer {len(ablation_silent)} of its {n} example(s)"
    return "adds nothing: every cited Then is predicted without it"


@dataclass(frozen=True, slots=True)
class ChunkJudgment:
    """One chunk of a feature after its own set judgment: its examples, the
    sentences its set carried (every surviving draft, kept or not), and the
    rules it kept."""

    example_ids: tuple[str, ...]
    judged: tuple[str, ...]
    kept: tuple[CreditedRule, ...]


@dataclass(frozen=True, slots=True)
class SiblingVisit:
    """A second judgment owed to one chunk: the feature's kept sentences,
    over the chunk's examples no rule is credited with yet."""

    chunk: int
    sentences: tuple[str, ...]
    example_ids: tuple[str, ...]


def sibling_visits(chunks: Sequence[ChunkJudgment]) -> list[SiblingVisit]:
    """The chunks of one feature that are asked again, and with what.

    A chunk judges only the drafts written from its own examples, so a
    sentence that holds for the whole feature is credited only where it was
    drafted. The feature's kept sentences (every chunk's, in chunk then
    draft order, one copy of a repeated sentence) are therefore judged
    together on each chunk's uncovered examples — only when they include a
    sentence that chunk's set never carried: the same sentences over the
    same examples is the question its own judgment already answered.

    Only kept sentences travel. A draft its own chunk refuted contradicts
    the kept ones on the same observable, and a set that carries both
    predicts neither.

    Run `72627-751534fe` (2026-09-30): the largest feature's five chunks
    each drafted alone; one chunk's pair was credited 23 + 18 examples
    while a sibling chunk of 24 examples, whose only draft left out the
    backticks, kept nothing."""
    sentences = tuple(dict.fromkeys(c.draft.sentence for ch in chunks for c in ch.kept))
    out: list[SiblingVisit] = []
    for n, ch in enumerate(chunks):
        covered = {i for c in ch.kept for i in c.credited}
        open_ids = tuple(i for i in ch.example_ids if i not in covered)
        if open_ids and set(sentences) - set(ch.judged):
            out.append(SiblingVisit(n, sentences, open_ids))
    return out


def judge_visit(
    visit: SiblingVisit,
    examples: Mapping[str, BehaviourSpec],
    *,
    predict: Predict,
) -> tuple[dict[str, tuple[str, ...]], int]:
    """`sentence -> the visit's examples it is credited with`, and the
    checker calls spent. The judgment is `judge_rule_set`'s, every sentence
    standing for every example of the visit: gain against the ablation,
    necessity by leave-one-out, one copy of a duplicate, silence credits
    nobody. A set miss here is recorded on no rule: no sentence cited these
    examples, so the miss has no owner."""
    pool = {i: examples[i] for i in visit.example_ids}
    verdict = judge_rule_set([RuleDraft(s, visit.example_ids) for s in visit.sentences],
                             pool, predict=predict)
    return {c.draft.sentence: c.credited for c in verdict.kept}, verdict.calls


@dataclass(frozen=True, slots=True)
class BookReading:
    """What one book build produced — the t0d reading line and event.

    `lanes` is how many chunks ran at once. `worlds` are the build worlds
    the one book serves; `residue_examples` / `residue_names` count the
    examples and glossary names only some of them compile (the rest is the
    core, asked once for all). `predict_temperature` is the sampling
    temperature of the judge's calls (ablation, set, leave-one-out); None
    is the server default, printed `server`. `sibling_examples` are the
    covered examples credited to a rule drafted in another chunk of their
    feature (`sibling_visits`). `unaided_examples` are the checkable examples
    the checker predicted with no rule at all (`Feature.unaided_ids`).
    `think` is whether the book's draft, judge and phrase calls asked the
    model to reason (`BEHAVIOUR_BOOK_THINK`), printed `think=on|off`."""

    features: int = 0
    features_asked: int = 0
    checkable_examples: int = 0
    covered_examples: int = 0
    proposed: int = 0
    kept: int = 0
    dropped: Mapping[str, int] = field(default_factory=dict)
    skipped_features: int = 0
    lanes: int = 1
    worlds: tuple[str, ...] = ()
    residue_examples: int = 0
    residue_names: int = 0
    predict_temperature: float | None = None
    think: bool = True
    sibling_examples: int = 0
    unaided_examples: int = 0
    # The roots the examples are given, the phrases kept for them, and why
    # the others have none (`given_phrase_drops`).
    given_roots: int = 0
    given_phrases: int = 0
    given_refused: Mapping[str, int] = field(default_factory=dict)

    def line(self) -> str:
        drop = ", ".join(f"{k} {v}" for k, v in sorted(self.dropped.items())) or "none"
        where = (f"; worlds={'+'.join(self.worlds)} ({self.residue_examples} residue "
                 f"example(s), {self.residue_names} residue name(s))" if self.worlds else "")
        return (f"behaviour-book: {self.features} features ({self.features_asked} asked, "
                f"{self.skipped_features} over the cap), {self.proposed} rules "
                f"({self.kept} kept), {self.covered_examples}/{self.checkable_examples} "
                f"checkable examples covered ({self.sibling_examples} by a sibling chunk's rule; "
                f"{self.unaided_examples} predicted with no rule); "
                f"dropped: {drop}; lanes={self.lanes}; "
                f"predict-temp={self._temp()}; think={'on' if self.think else 'off'}"
                f"{where}{self._givens()}")

    def _givens(self) -> str:
        if not self.given_roots:
            return ""
        why = ", ".join(f"{k} {v}" for k, v in sorted(self.given_refused.items())) or "none"
        return (f"; givens: {self.given_phrases} phrase(s) for {self.given_roots} root(s) "
                f"(without one: {why})")

    def _temp(self) -> str:
        return "server" if self.predict_temperature is None else f"{self.predict_temperature:g}"

    def payload(self) -> dict[str, object]:
        return {
            "features": self.features, "features_asked": self.features_asked,
            "skipped_features": self.skipped_features,
            "checkable_examples": self.checkable_examples,
            "covered_examples": self.covered_examples,
            "proposed": self.proposed, "kept": self.kept, "dropped": dict(self.dropped),
            "lanes": self.lanes,
            "worlds": list(self.worlds),
            "residue_examples": self.residue_examples, "residue_names": self.residue_names,
            "predict_temperature": self.predict_temperature,
            "think": self.think,
            "sibling_examples": self.sibling_examples,
            "unaided_examples": self.unaided_examples,
            "given_roots": self.given_roots, "given_phrases": self.given_phrases,
            "given_refused": dict(self.given_refused),
        }


def drop_reason_class(reason: str) -> str:
    """The histogram bucket of a drop reason (its text before any id)."""
    for prefix in ("names the private item", "quotes", "refuted on", "not necessary",
                   "leave-one-out did not answer", "set did not answer",
                   "ablation did not answer", "adds nothing", "cites no checkable",
                   "more than one sentence"):
        if reason.startswith(prefix):
            return prefix
    return reason


# --- aiming -------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AimedUnit:
    """A fill target as rule aiming sees it: its declaration only."""

    name: str
    container: str = ""
    file: str = ""


def _unit_qualifiers(unit: AimedUnit) -> set[str]:
    c = unit.container.strip()
    if not c:
        return set()
    if c.startswith("trait"):
        rest = c[5:].strip()
        return {rest.split("<", 1)[0].split()[0]} if rest else set()
    out = set()
    trait = impl_trait_name(c)
    if trait:
        out.add(trait)
    self_ty = impl_self_type(c)
    if self_ty:
        out.add(self_ty)
    return out


def _names_unit(term: str, unit: AimedUnit) -> bool:
    """Does the glossary name `term` spell this unit's own declaration?"""
    if term.endswith("!"):
        return False
    if "::" in term:
        qual, leaf = term.rsplit("::", 1)
        return leaf == unit.name and qual in _unit_qualifiers(unit)
    return term == unit.name and not unit.container.strip()


def rules_for_units(
    specs: Sequence[BehaviourSpec],
    units: Sequence[AimedUnit],
    *,
    identity_ids: Collection[str] = (),
    cap: int = RULES_PER_PROMPT,
) -> tuple[Rule, ...]:
    """The kept rules a fill of `units` carries, at most `cap`, in order:

    1. rules whose FEATURE is one of the units: one of the feature's own
       glossary spellings among the rule's terms (`build_behaviour_book`
       always puts them there) names the unit's declaration — `Error::new`
       reaches `new` in `impl Error`, not `new` in `impl Chain`;
    2. rules whose sentence names one of the units by its glossary spelling
       (`Type::fn`, `Trait::fn`, or a free fn);
    3. rules whose feature's examples are the only ones that run one of the
       units (`candidate_tiers.culprits`, read off the specs' `exercises` and
       the features attached to them). A macro feature (`ensure!`) names no
       function, so aims 1 and 2 reach nothing; the functions only its
       examples execute are the ones that implement it. A unit with no file
       (an aiming caller that did not pass one) is skipped by this aim;
    4. rules citing an example in `identity_ids` — the identity examples the
       identity instruction already aims at these units by signature and
       container (`select_identity_must`), so a constructor that decides a
       TypeId gets the rule those examples illustrate.

    `exercises` never aim a rule by themselves: every example runs the whole
    call chain (77 of 94 `ensure!` examples run the same 23 functions on the
    anyhow harvest), so "this example ran the unit" says nothing about what
    decides its Then. Only exclusivity does: a function no other feature's
    example runs."""
    if cap <= 0 or not units:
        return ()
    rules: dict[str, Rule] = {}
    for s in specs:
        for r in s.rules:
            rules.setdefault(r.rule_id, r)
    ordered = sorted(rules.values(), key=lambda r: r.rule_id)
    picked: list[Rule] = []

    def take(pred: Callable[[Rule], bool]) -> None:
        for r in ordered:
            if len(picked) >= cap:
                return
            if r not in picked and pred(r):
                picked.append(r)

    def feature_aims(r: Rule) -> bool:
        own = [t for t in r.terms if feature_key_of(t) == r.feature]
        return any(_names_unit(t, u) for t in own for u in units)

    take(feature_aims)
    take(lambda r: any(_names_unit(t, u) for t in r.terms for u in units))
    keyed = [f"{u.file}::{u.name}" for u in units if u.file]
    if keyed:
        reach = feature_reach(specs)
        only: dict[str, frozenset[str]] = {}

        def culprit_aims(r: Rule) -> bool:
            if r.feature not in only:
                only[r.feature] = culprits(r.feature, reach)
            return any(k in only[r.feature] for k in keyed)

        take(culprit_aims)
    ident = set(identity_ids)
    if ident:
        take(lambda r: bool(ident & set(r.example_ids)))
    return tuple(picked)


def render_rules(rules: Sequence[Rule], specs_by_id: Mapping[str, BehaviourSpec]) -> str:
    """The RULES instruction: each sentence, and one compact example of it
    (`when → then`) so the model sees the rule's shape on a real call."""
    if not rules:
        return ""
    lines = ["RULES (observable invariants this crate's tests hold; each was "
             "checked against the examples it cites):"]
    for r in rules:
        lines.append(f"- {r.sentence}")
        cited = [specs_by_id[i] for i in r.example_ids if i in specs_by_id]
        # A bounded-resource example states the limit and the fallback the
        # sentence names; the first plain example usually shows only the
        # common case, which is what a body can already get right.
        ex = next((c for c in cited
                   if c.klass is BehaviourClass.BOUNDED_RESOURCE),
                  cited[0] if cited else None)
        if ex is not None and ex.then is not None:
            when = " ".join(example_when(ex).split())[:160]
            value = then_value_text(ex.then).replace("\n", "\\n")[:160]
            lines.append(f"  e.g. `{when}` → {value!r}")
    return "\n".join(lines)


# --- the Gherkin view -----------------------------------------------------------

GHERKIN_HEADER = ("# Generated from the behaviour book (.skeleton/_behaviours_<cluster>.json)"
                  " — edits are not read.")


def feature_file_name(feature: Feature) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", feature.item.rstrip("!")).strip("_") or "crate"
    return f"{slug}{'_macro' if feature.item.endswith('!') else ''}.feature"


def _then_step(then: ObservableOutcome | None, observer: str = "") -> list[str]:
    """The Then in words. `observer` is the asserting helper the test routes
    its literal through, as a pointer: the Then is what that helper compares."""
    lines = _then_lines(then)
    if observer:
        lines[0] = f"Then, as {observer} compares it, " + lines[0].removeprefix("Then ")
    return lines


def _then_lines(then: ObservableOutcome | None) -> list[str]:
    if then is None:
        return ["Then the example's own assertions hold"]
    kind = then.kind
    if kind is OutcomeKind.TYPE_IDENTITY:
        verb = "is" if then.ok else "is not"
        return [f"Then the value {verb} a `{then.value}` (`is::<{then.value}>()`)"]
    if kind is OutcomeKind.DOWNCAST_OK:
        return [f"Then the downcast finds {'a value' if then.ok else 'nothing'}"]
    if kind is OutcomeKind.STATIC_ASSERTION:
        return [f"Then `{then.static_expr}` holds at compile time"]
    if kind is OutcomeKind.CHAIN_SEQUENCE:
        return ["Then the chain yields, in order:", '  """', *(f"  {c}" for c in then.chain), '  """']
    what = {
        OutcomeKind.DISPLAY_STRING: "it displays as",
        OutcomeKind.DEBUG_STRING: "its Debug render is",
        OutcomeKind.ALT_DEBUG_STRING: "its alternate Debug render is",
        OutcomeKind.PANIC_MESSAGE: "it panics with",
        OutcomeKind.RETURN_RENDERED: "the value is",
        OutcomeKind.NORMALIZED_STDOUT: "it prints",
    }.get(kind, "the observable is")
    lead = f"Then {what}" + (" text starting with" if then.prefix else "")
    if "\n" in then.value:
        return [f"{lead}:", '  """', *(f"  {ln}" for ln in then.value.split("\n")), '  """']
    return [f"{lead} {then.value!r}"]


def _code_block(code: str) -> list[str]:
    return ['  """rust', *(f"  {ln}" for ln in code.split("\n")), '  """']


def render_feature(
    feature: Feature,
    glossary: Sequence[GlossaryEntry],
    specs_by_id: Mapping[str, BehaviourSpec],
    phrases: Mapping[str, str] | None = None,
) -> str:
    """One `.feature` file: the item and its glossary line(s), each kept rule
    with the examples it cites, then the examples no kept rule covers under a
    rule-less `Rule:` heading. Pure; deterministic for a given book.

    An example's Given is one step per root (:func:`example_given`), a
    pointer `name (file)` into the test file that declares it, then the
    example's own setup. No helper text is printed here: each root, observer
    and import block is printed once, in :func:`render_givens_index`.
    `phrases` (`root key → sentence`, the book's `givens`) leads a step with
    its sentence; a root without one prints the pointer alone, so the
    pointer never depends on a model."""
    aimed = set(feature_spellings(feature.item, glossary))
    names = [g for g in glossary if g.name in aimed]
    lines = [GHERKIN_HEADER, f"Feature: {feature.item}"]
    for g in names:
        lines.append(f"  {g.name} — {g.summary}" if g.summary else f"  {g.name}")
    covered: set[str] = set()

    def example(bid: str) -> list[str]:
        s = specs_by_id.get(bid)
        if s is None:
            return []
        out = [f"    Example: {bid}"]
        given = example_given(s)
        steps = [_given_step(k, phrases or {}) for k in given.roots]
        for n, step in enumerate(steps):
            out.append(f"      {'And' if n else 'Given'} {step}")
        if given.setup.strip():
            out.append(f"      {'And' if steps else 'Given'} this setup")
            out.extend("      " + ln for ln in _code_block(given.setup))
        out.append("      When")
        out.extend("      " + ln for ln in _code_block(example_when(s)))
        out.extend("      " + ln for ln in _then_step(s.then, _observer_pointer(s, given)))
        return out

    for r in feature.rules:
        lines.append("")
        lines.append(f"  Rule: {r.sentence}")
        for bid in r.example_ids:
            covered.add(bid)
            lines.extend(example(bid))
    rest = [b for b in feature.example_ids if b not in covered]
    if rest:
        lines.append("")
        lines.append("  Rule: (no checked rule states these yet)")
        for bid in rest:
            lines.extend(example(bid))
    return "\n".join(lines) + "\n"


GIVENS_INDEX_NAME = "_givens.md"


def _pointer(key: str) -> str:
    file, name, _is_impl = helper_key_parts(key)
    return f"`{name}` ({file})"


def _observer_pointer(spec: BehaviourSpec, given: ExampleGiven) -> str:
    """The asserting helper as a pointer, or its bare name when the store
    holds no item for it; empty for an example compared directly."""
    if not spec.via:
        return ""
    for key in (*given.observer, *given.roots):
        if helper_key_parts(key)[1] == spec.via:
            return _pointer(key)
    return f"`{spec.via}`"


def _given_step(key: str, phrases: Mapping[str, str]) -> str:
    phrase = phrases.get(key, "")
    return f"{phrase} — {_pointer(key)}" if phrase else _pointer(key)


@dataclass(frozen=True, slots=True)
class GivenRoot:
    """One root the examples are given, across the suite: its key, the keys
    it pulls in, its text followed by theirs, and the examples given it."""

    key: str
    pulled: tuple[str, ...]
    text: str
    example_ids: tuple[str, ...]

    @property
    def file(self) -> str:
        return helper_key_parts(self.key)[0]


def _joined(keys: Sequence[str], text: Mapping[str, str]) -> str:
    return "\n\n".join(text[k].strip() for k in keys if text.get(k, "").strip())


def given_roots(specs: Sequence[BehaviourSpec]) -> tuple[GivenRoot, ...]:
    """Every root of `specs` once, sorted by key (:func:`example_given`)."""
    text: dict[str, str] = {}
    pulled: dict[str, list[str]] = {}
    ids: dict[str, list[str]] = {}
    for s in specs:
        text.update(helper_items(s))
        for root, more in example_given(s).reach:
            have = pulled.setdefault(root, [])
            have.extend(k for k in more if k not in have)
            ids.setdefault(root, []).append(s.behaviour_id)
    return tuple(GivenRoot(k, tuple(pulled[k]), _joined((k, *pulled[k]), text), tuple(ids[k]))
                 for k in sorted(pulled))


_GIVEN_RE = re.compile(r"<GIVEN\s+key\s*=\s*\"([^\"]*)\"\s*>(.*?)</GIVEN>", re.S | re.I)


def parse_given_phrases(reply: str) -> dict[str, str]:
    """`key → sentence` from `<GIVEN key="…">sentence</GIVEN>` blocks, the
    first block of a key winning; whitespace in the sentence collapsed."""
    out: dict[str, str] = {}
    for key, body in _GIVEN_RE.findall(strip_think_blocks(reply)):
        sentence = " ".join(body.split())
        if sentence:
            out.setdefault(key.strip(), sentence)
    return out


NO_PHRASE = "no phrase in the reply"
NOT_ASKED = "not asked"


def keep_given_phrases(
    roots: Sequence[GivenRoot],
    answered: Mapping[str, str],
    examples: Mapping[str, BehaviourSpec],
    private: Collection[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """`(root key → kept phrase, root key → why it has none)` for `roots`,
    given the model's `answered` sentences. A phrase passes the mechanical
    rule a rule draft passes (:func:`sentence_refusal`), against the examples
    given that root. A sentence on a key that is no root is read by nobody."""
    kept: dict[str, str] = {}
    refused: dict[str, str] = {}
    for r in roots:
        phrase = answered.get(r.key, "")
        if not phrase:
            refused[r.key] = NO_PHRASE
            continue
        why = sentence_refusal(phrase, [examples[i] for i in r.example_ids if i in examples], private)
        if why:
            refused[r.key] = why
        else:
            kept[r.key] = phrase
    return kept, refused


def render_givens_index(specs: Sequence[BehaviourSpec],
                        phrases: Mapping[str, str] | None = None) -> str:
    """The step-definition index of the view: every root a `Given` step
    points at, once, with its phrase when the book has one, the keys it
    pulls in and their text; then every observer and every import block,
    once. Empty when no example calls a test-file item. The text is the test
    files' own (the oracle), which is what executes it; nothing here is run."""
    text: dict[str, str] = {}
    observers: dict[str, list[str]] = {}
    imports: list[str] = []
    for s in specs:
        text.update(helper_items(s))
        given = example_given(s)
        if given.observer:
            have = observers.setdefault(given.observer[0], [])
            have.extend(k for k in given.observer[1:] if k not in have)
        imports.extend(k for k in given.imports if k not in imports)
    roots = given_roots(specs)
    if not (roots or observers or imports):
        return ""

    def block(body: str) -> list[str]:
        return ["```rust", body, "```", ""]

    out = [GHERKIN_HEADER, "", "# Givens", "",
           "Each `Given` step of a `.feature` file names one of these: an item of the "
           "crate's own test files, which is what runs it.", ""]
    for r in roots:
        out += [f"## {_pointer(r.key)}", ""]
        if (phrases or {}).get(r.key):
            out += [(phrases or {})[r.key], ""]
        out += [f"- key: `{r.key}`", f"- given in {len(r.example_ids)} example(s)"]
        if r.pulled:
            out.append("- pulls in: " + ", ".join(f"`{k}`" for k in r.pulled))
        out += ["", *block(r.text)]
    if observers:
        out += ["# Observers", "",
                "The asserting helper a `Then` is compared through. Not a precondition.", ""]
        for key in sorted(observers):
            out += [f"## {_pointer(key)}", "", f"- key: `{key}`"]
            if observers[key]:
                out.append("- pulls in: " + ", ".join(f"`{k}`" for k in observers[key]))
            out += ["", *block(_joined((key, *observers[key]), text))]
    if imports:
        out += ["# Imports", "", "Each test file's `use` block: what its examples' names resolve to.", ""]
        for key in sorted(imports):
            out += [f"## {helper_key_parts(key)[0]}", "", *block(_joined((key,), text))]
    return "\n".join(out).rstrip("\n") + "\n"

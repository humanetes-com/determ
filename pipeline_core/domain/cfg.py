"""Cfg predicate domain — a typed, algebraic model of `#[cfg(...)]`.

This replaces the string-slicing cfg helpers that used to live in ``pipeline.py``
(``_eval_cfg_pred``, ``_cfg_leaf_atoms``, ``_cfg_false_for_cargo``,
``_cfg_atom_is_off_for_cargo``, ``_normalize_cfg_pred``) with an explicit model:

  * a leaf condition is a :class:`CfgAtom` — a bare ident (``unix``, ``nightly``)
    or a ``key = "value"`` (``feature = "std"``, ``target_os = "linux"``);
  * a full ``#[cfg(...)]`` condition is a :class:`CfgPred` tree —
    :class:`All` / :class:`Any` / :class:`Not` over leaves;
  * a cluster's cfg context is a :class:`CfgSet` — the predicates known true,
    the predicates known false, and the crate's default features.

Evaluation is a clean recursive ``match`` over the tree instead of substring
slicing, which is what the old code did and what made the space-sensitivity and
operator bugs (R7.4b) possible. The whole-string membership pre-check at every
node is preserved, so behaviour is byte-identical to ``_eval_cfg_pred``.

Pure domain: stdlib only, no I/O, no regex (predicates are parsed with a
char-scan splitter, never ``re``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pipeline_core.domain.rust_scan import contains_word

# Built-in cfg identifiers cargo evaluates from the build TARGET (not from a
# `--cfg`/feature flag). Undecidable to a cluster's cfg sets, but cargo MAY set
# them, so cargo-faithful exclusion must never force them off (never over-exclude).
# Everything else that is a bare ident is a custom `--cfg NAME`, default OFF.
BUILTIN_CFG_IDENTS: frozenset[str] = frozenset({
    "unix", "windows", "test", "debug_assertions", "doc", "doctest", "miri",
    "proc_macro", "overflow_checks",
})


def normalize_pred(pred: str) -> str:
    """Canonical form for set comparison: strip all spaces (``feature = "x"`` →
    ``feature="x"``, ``all (a, b)`` → ``all(a,b)``). Matches the legacy
    ``_normalize_cfg_pred`` exactly, including its whitespace-in-string-values
    limitation (cfg values practically never contain spaces)."""
    return pred.replace(" ", "")


def split_top_level_commas(s: str) -> list[str]:
    """Split ``s`` on commas that are not nested inside parentheses."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in s:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


@dataclass(frozen=True, slots=True)
class CfgAtom:
    """A leaf cfg condition. ``value is None`` → bare ident (``nightly``);
    otherwise ``key = value`` where ``value`` keeps its source quoting
    (``feature`` → ``value == '"std"'``)."""

    key: str
    value: str | None = None

    @staticmethod
    def parse(token: str) -> CfgAtom:
        """Parse one already-normalized (space-free) leaf token into an atom."""
        key, eq, val = token.partition("=")
        return CfgAtom(key=key, value=val if eq else None)

    @property
    def normalized(self) -> str:
        return f"{self.key}={self.value}" if self.value is not None else self.key

    @property
    def is_feature(self) -> bool:
        return self.key == "feature" and self.value is not None

    @property
    def feature_name(self) -> str | None:
        """The bare feature name (``feature = "std"`` → ``std``), else None."""
        return self.value.strip('"') if self.is_feature and self.value else None

    @property
    def cluster_key(self) -> str | None:
        """The predicate name this atom contributes to cluster derivation: the
        feature name for ``feature = "X"``, the bare ident for ``nightly``, and
        ``None`` for ``target_os = "linux"`` / other ``key = "v"`` forms (a build
        target facet, never its own cluster)."""
        if self.is_feature:
            return self.feature_name
        return self.key if self.value is None else None

    def is_off_for_cargo(self, cfg_true_norm: frozenset[str],
                         default_features: frozenset[str]) -> bool:
        """True when cargo leaves this leaf OFF in the cluster: a custom ``--cfg``
        flag enabled nowhere, or a non-default ``feature = "X"`` not enabled here.
        Builtin/target cfgs and unknown ``key = "v"`` forms return False — never
        force-exclude what cargo might set."""
        if self.normalized in cfg_true_norm:
            return False
        if self.value is not None:
            if self.key == "feature":
                return (self.feature_name or "") not in default_features
            return False                       # target_os=… / unknown key → leave to cargo
        return self.key not in BUILTIN_CFG_IDENTS


class CfgPred:
    """Base of the parsed cfg-condition tree. Subclasses: :class:`Leaf`,
    :class:`Not`, :class:`All`, :class:`Any`."""

    @property
    def normalized(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def atoms(self) -> frozenset[CfgAtom]:  # pragma: no cover - overridden
        raise NotImplementedError

    def evaluate(self, ctx: CfgSet) -> bool | None:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Leaf(CfgPred):
    atom: CfgAtom

    @property
    def normalized(self) -> str:
        return self.atom.normalized

    def atoms(self) -> frozenset[CfgAtom]:
        return frozenset({self.atom})

    def evaluate(self, ctx: CfgSet) -> bool | None:
        return ctx.membership(self.normalized)


@dataclass(frozen=True, slots=True)
class Not(CfgPred):
    child: CfgPred

    @property
    def normalized(self) -> str:
        return f"not({self.child.normalized})"

    def atoms(self) -> frozenset[CfgAtom]:
        return self.child.atoms()

    def evaluate(self, ctx: CfgSet) -> bool | None:
        decided = ctx.membership(self.normalized)
        if decided is not None:
            return decided
        v = self.child.evaluate(ctx)
        return None if v is None else (not v)


@dataclass(frozen=True, slots=True)
class All(CfgPred):
    parts: tuple[CfgPred, ...]

    @property
    def normalized(self) -> str:
        return "all(" + ",".join(p.normalized for p in self.parts) + ")"

    def atoms(self) -> frozenset[CfgAtom]:
        return frozenset().union(*(p.atoms() for p in self.parts)) if self.parts else frozenset()

    def evaluate(self, ctx: CfgSet) -> bool | None:
        decided = ctx.membership(self.normalized)
        if decided is not None:
            return decided
        # Truth-dominant: one False makes the whole `all(...)` False even when
        # other arms are undecidable; only undecided-without-a-False is None.
        vals = [p.evaluate(ctx) for p in self.parts]
        if any(v is False for v in vals):
            return False
        if any(v is None for v in vals):
            return None
        return True


@dataclass(frozen=True, slots=True)
class Any(CfgPred):
    parts: tuple[CfgPred, ...]

    @property
    def normalized(self) -> str:
        return "any(" + ",".join(p.normalized for p in self.parts) + ")"

    def atoms(self) -> frozenset[CfgAtom]:
        return frozenset().union(*(p.atoms() for p in self.parts)) if self.parts else frozenset()

    def evaluate(self, ctx: CfgSet) -> bool | None:
        decided = ctx.membership(self.normalized)
        if decided is not None:
            return decided
        # Truth-dominant: one True makes the whole `any(...)` True even when
        # other arms are undecidable; only undecided-without-a-True is None.
        vals = [p.evaluate(ctx) for p in self.parts]
        if any(v is True for v in vals):
            return True
        if any(v is None for v in vals):
            return None
        return False


def lines_are_cluster_specific(
    source_text: str, lines: set[int], contexts: Iterable[CfgSet],
) -> bool:
    """True if any of ``lines`` is compiled OUT under at least one cluster context
    (#26) — i.e. the lines live inside a cluster-specific `#[cfg]` region, so a test
    covering them cannot be safely shared across clusters and must stay gated.

    When every line is compiled in under every context the test is cluster-agnostic
    and may be emitted UNGATED so a later cluster sees those lines already covered
    (no re-augmentation). This is exactly the criterion that keeps a `not(X)`
    stable-only test gated to stable while letting an always-compiled test be shared
    — the documented failure mode where ungating stable's `not(egma)` siblings broke
    the nightly harness."""
    return any(ctx.excluded_uncovered_lines(source_text, lines) for ctx in contexts)


def cluster_specific_symbol_names(
    fn_records: Iterable[object], contexts: Iterable[CfgSet],
) -> frozenset[str]:
    """Names of fns whose `#[cfg(...)]` chain is compiled OUT under at least one
    cluster context (#26 residual). A test BODY that references such a symbol
    cannot be shared across clusters even when the LINES it targets are agnostic
    — under a cluster that excludes the symbol the shared sibling fails to
    compile. Accepts FnRecord dicts (`fn_name`/`cfg_predicates`) or objects with
    those attrs; `#[cfg(test)]`-only fns carry the builtin `test` atom which is
    never cargo-excluded, so they are correctly NOT flagged."""
    ctxs = list(contexts)
    names: set[str] = set()
    for r in fn_records:
        if isinstance(r, dict):
            name = r.get("fn_name", "")
            preds = r.get("cfg_predicates") or ()
        else:
            name = getattr(r, "fn_name", "")
            preds = getattr(r, "cfg_predicates", ()) or ()
        if not name or not preds:
            continue
        if any(ctx.excludes_for_cargo(p) for ctx in ctxs for p in preds):
            names.add(name)
    return frozenset(names)


def body_uses_cluster_specific_symbol(
    body: str, names: frozenset[str] | set[str],
) -> bool:
    """True if `body` references any cluster-specific symbol name as a standalone
    identifier (#26 residual). Word-boundary match so `new` does not match
    `new_inner`."""
    if not body or not names:
        return False
    return any(contains_word(body, n) for n in names)


_OPS = {"not": Not, "all": All, "any": Any}


# --- `#[cfg(...)]` ATTRIBUTE EXTRACTION from rendered source text ---
# Structured replacements for the source regexes that pulled predicate strings out
# of cfg attributes before handing them to `CfgSet.evaluate`. Each is byte-equivalent
# to the exact regex it replaced (fuzz-verified). Naive of string literals, as the
# regexes were.

def cfg_inline_predicates(text: str) -> list[str]:
    """Predicate string of every inline ``#[cfg(P)]`` — byte-equivalent to
    ``[m.group(1) for m in re.finditer(r'#\\s*\\[\\s*cfg\\s*\\(([^]]+)\\)\\s*\\]', text)]``.
    Flexible inter-token whitespace; capture runs to the ``)`` (whitespace allowed
    before ``]``) that precedes the first ``]``."""
    out: list[str] = []
    n = len(text)
    i = 0
    while (h := text.find("#", i)) >= 0:
        i = h + 1
        p = h + 1
        while p < n and text[p].isspace():  # \s*
            p += 1
        if p >= n or text[p] != "[":
            continue
        p += 1
        while p < n and text[p].isspace():  # \s*
            p += 1
        if not text.startswith("cfg", p):
            continue
        p += 3
        while p < n and text[p].isspace():  # \s*
            p += 1
        if p >= n or text[p] != "(":
            continue
        op = p
        cb = text.find("]", op + 1)  # first ] ends the [^]]+ capture
        if cb == -1:
            continue
        rp = cb - 1
        while rp > op and text[rp].isspace():  # \s* between ) and ]
            rp -= 1
        if rp < op + 2 or text[rp] != ")":  # capture is [^]]+ (≥1) then )
            continue
        out.append(text[op + 1:rp])
        i = cb + 1
    return out


def match_cfg_attr_line(line: str) -> str | None:
    """If `line` is exactly a ``#[cfg(P)]`` attribute line (``[ \\t]`` indent, then
    ``\\s*$``), return ``P``; else None — byte-equivalent to ``re.match(
    r'^[ \\t]*#\\[cfg\\(([^\\]]+(?:\\([^\\]]*\\)[^\\]]*)*)\\)\\]\\s*$', line)``. ``#[cfg(`` is
    matched literally; capture runs to the ``)`` directly before the first ``]``."""
    n = len(line)
    p = 0
    while p < n and line[p] in " \t":  # [ \t]*
        p += 1
    if not line.startswith("#[cfg(", p):
        return None
    op = p + 5  # the '(' of "#[cfg("
    cb = line.find("]", op + 1)
    if cb == -1 or cb < op + 3 or line[cb - 1] != ")":  # \)\] adjacent; capture ≥1
        return None
    if any(not c.isspace() for c in line[cb + 1:]):  # \s*$
        return None
    return line[op + 1:cb - 1]


def crate_cfg_predicates(text: str) -> list[str]:
    """Predicate of every top-level ``#![cfg(P)]`` line — byte-equivalent to
    ``[m.group(1) for m in re.finditer(r'^\\s*#!\\[cfg\\((.*?)\\)\\]\\s*$', text,
    re.MULTILINE)]``. ``#![cfg(`` is literal; capture is non-greedy to the first
    ``)]`` (no newline within); the line is whitespace-only around the attribute."""
    out: list[str] = []
    n = len(text)
    i = 0
    needle = "#![cfg("
    while (h := text.find(needle, i)) >= 0:
        i = h + 1
        bol = text.rfind("\n", 0, h) + 1
        if any(not c.isspace() for c in text[bol:h]):  # ^\s* (line lead all ws)
            continue
        cs = h + len(needle)
        rb = -1
        j = cs
        while j < n - 1:  # (.*?)\)\] : first ")]" with no newline in the capture
            if text[j] == "\n":
                break
            if text[j] == ")" and text[j + 1] == "]":
                rb = j
                break
            j += 1
        if rb < 0:
            continue
        after = rb + 2
        eol = text.find("\n", after)
        if eol == -1:
            eol = n
        if any(not c.isspace() for c in text[after:eol]):  # \s*$
            continue
        out.append(text[cs:rb])
        i = rb + 2
    return out


def _capture_one_level(text: str, op: int) -> tuple[str, int] | None:
    """Parse ``(?:[^()]|\\([^()]*\\))+`` starting just after the ``(`` at `op`; return
    ``(inner, close)`` where `close` is the top-level ``)`` index, or None if the
    one-or-more body is empty / a nested ``(`` breaks the single-level unit.

    ONE nested level is the regex's limit, not a cfg property: the ordinary
    ``all(not(a), feature = "std")`` is two levels deep and is NOT a match. This
    scanner exists only to keep `last_trailing_cfg_pred` byte-equivalent to the
    regex it replaced; an item's own gate is read with `cfg_attr_predicate`,
    which captures balanced parens to any depth."""
    n = len(text)
    i = op + 1
    start = i
    got = False
    while i < n:
        c = text[i]
        if c == "(":                       # `\([^()]*\)` unit (no nested parens)
            j = i + 1
            while j < n and text[j] not in "()":
                j += 1
            if j < n and text[j] == ")":
                i = j + 1
                got = True
                continue
            return None
        if c == ")":                        # top-level close
            break
        i += 1                              # `[^()]` char
        got = True
    if not got or i >= n or text[i] != ")":
        return None
    return text[start:i], i


def last_trailing_cfg_pred(text: str) -> str:
    """Inner predicate of the LAST ``#[cfg(P)]`` alone on its line — byte-equivalent
    to the final match of MULTILINE
    ``#\\[cfg\\(((?:[^()]|\\([^()]*\\))+)\\)\\]\\s*$`` (one nested-paren level allowed;
    the ``]`` is followed only by whitespace to the line end). ``""`` when none; the
    caller strips the result.

    Not a way to find an item's gate. "The last match in a window of text" has no
    notion of which item a line belongs to, and the one-level capture skips the
    very gates that matter, so the last match falls through to a PREVIOUS item's
    ``#[cfg]``: the macro-variant enumerator used it over an 800-char lookback and
    tagged every ``backtrace_if_absent!`` arm of the live tree with the first
    arm's predicate (2026-09-01). Kept byte-equivalent for the regex-equivalence
    suite; item gates go through `item_attributes.outer_attributes_before` and
    `cfg_attr_predicate`."""
    n = len(text)
    found = ""
    needle = "#[cfg("
    i = 0
    while (h := text.find(needle, i)) >= 0:
        i = h + 1
        cap = _capture_one_level(text, h + len(needle) - 1)  # index of the '('
        if cap is None:
            continue
        inner, close = cap
        if close + 1 >= n or text[close + 1] != "]":          # `\)\]`
            continue
        eol = text.find("\n", close + 2)                      # `\s*$` (MULTILINE)
        if eol == -1:
            eol = n
        if any(not c.isspace() for c in text[close + 2:eol]):
            continue
        found = inner
        i = close + 2
    return found


def cfg_attr_predicate(attr: str) -> str | None:
    """Predicate ``P`` of ONE outer attribute ``#[cfg(P)]``, captured by a balanced
    parenthesis walk, else None.

    ``P`` may nest to any depth — ``all(not(a), any(b, not(c)))`` is the ordinary
    shape of a crate's cfg gate, and a capture limited to one nested level
    (`_capture_one_level`) rejects it outright, which is how a window scanner came
    to return a neighbouring item's gate instead. Whitespace is flexible around
    every token (``# [ cfg ( … ) ]``, or an attribute wrapped over several lines);
    parens inside a string literal do not count. The result has its outer
    whitespace stripped and inner whitespace runs collapsed to one space, so a
    wrapped multi-line predicate compares equal to its single-line spelling.
    ``#[cfg_attr(…)]`` is a different attribute and is not matched."""
    n = len(attr)
    p = 0
    while p < n and attr[p].isspace():
        p += 1
    if p >= n or attr[p] != "#":
        return None
    p += 1
    while p < n and attr[p].isspace():
        p += 1
    if p >= n or attr[p] != "[":
        return None
    p += 1
    while p < n and attr[p].isspace():
        p += 1
    if not attr.startswith("cfg", p):
        return None
    p += 3
    while p < n and attr[p].isspace():
        p += 1
    if p >= n or attr[p] != "(":
        return None
    op = p
    depth = 0
    in_str = False
    escaped = False
    q = op
    close = -1
    while q < n:
        c = attr[q]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                close = q
                break
        q += 1
    if close == -1:
        return None
    q = close + 1
    while q < n and attr[q].isspace():
        q += 1
    if q >= n or attr[q] != "]":
        return None
    q += 1
    if any(not c.isspace() for c in attr[q:]):
        return None
    inner = " ".join(attr[op + 1:close].split())
    return inner or None


def conjoined_cfg_predicate(preds: Sequence[str]) -> str:
    """The single predicate equivalent to an item carrying every ``#[cfg]`` in
    `preds`: rustc applies stacked cfg attributes conjunctively, so two gates are
    ``all(p1, p2)``; one gate is itself; none is ``""``."""
    ps = [p for p in preds if p]
    if not ps:
        return ""
    if len(ps) == 1:
        return ps[0]
    return "all(" + ", ".join(ps) + ")"


def _cfg_attr_inner_predicates(text: str) -> list[str]:
    """Inner string of every ``#[cfg(...)]``/``#[cfg_attr(...)]`` — byte-equivalent to
    ``re.finditer(r'#\\[cfg(?:_attr)?\\(([^)]+(?:\\([^)]*\\)[^)]*)*)\\)', text)``. The
    greedy ``[^)]+`` stops at the first ``)`` (which trailing ``\\)`` then consumes),
    so the capture is exactly the ≥1 chars from ``(`` to the first ``)``."""
    out: list[str] = []
    n = len(text)
    i = 0
    while (h := text.find("#[cfg", i)) >= 0:
        p = h + 5
        if text.startswith("_attr", p):
            p += 5
        if p < n and text[p] == "(":
            cp = text.find(")", p + 1)
            if cp != -1 and cp > p + 1:        # `[^)]+` ≥1 char
                out.append(text[p + 1:cp])
                i = cp + 1
                continue
        i = h + 1
    return out


def _scan_feature_matches(pred: str) -> list[tuple[int, int, str]]:
    """``(start, end, value)`` of every ``\\bfeature\\s*=\\s*"([^"]*)"`` in `pred`,
    non-overlapping in source order."""
    res: list[tuple[int, int, str]] = []
    n = len(pred)
    i = 0
    while (f := pred.find("feature", i)) >= 0:
        if f > 0 and (pred[f - 1].isalnum() or pred[f - 1] == "_"):  # `\b` before
            i = f + 1
            continue
        p = f + 7
        while p < n and pred[p].isspace():    # `\s*`
            p += 1
        if p < n and pred[p] == "=":
            p += 1
            while p < n and pred[p].isspace():  # `\s*`
                p += 1
            if p < n and pred[p] == '"':
                q = pred.find('"', p + 1)        # `"([^"]*)"`
                if q != -1:
                    res.append((f, q + 1, pred[p + 1:q]))
                    i = q + 1
                    continue
        i = f + 1
    return res


def _feature_stripped(pred: str) -> str:
    """`pred` with every ``feature = "…"`` pair removed — byte-equivalent to
    ``feat_re.sub("", pred)``."""
    spans = _scan_feature_matches(pred)
    if not spans:
        return pred
    out: list[str] = []
    last = 0
    for s, e, _v in spans:
        out.append(pred[last:s])
        last = e
    out.append(pred[last:])
    return "".join(out)


def _bare_flag_idents(s: str) -> list[str]:
    """Lowercase/underscore-led idents in `s` NOT immediately followed by ``=`` —
    byte-equivalent to ``re.finditer(r'\\b([a-z_]\\w*)(?!\\s*=)', s)``. One candidate
    per maximal ``\\w+`` run (``\\b`` fires only at run edges); a run followed by
    ``\\s*=`` forces the negative lookahead to drop the run's last char (or yields
    nothing for a 1-char run)."""
    out: list[str] = []
    n = len(s)
    i = 0
    while i < n:
        if s[i].isalnum() or s[i] == "_":
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            first = s[i]
            if ("a" <= first <= "z") or first == "_":   # `[a-z_]` lead
                p = j
                while p < n and s[p].isspace():          # lookahead `\s*`
                    p += 1
                if not (p < n and s[p] == "="):
                    out.append(s[i:j])
                elif j - i >= 2:                          # backtrack one char off the run
                    out.append(s[i:j - 1])
            i = j
        else:
            i += 1
    return out


def gated_predicate_names(text: str, skip: frozenset[str]) -> list[str]:
    """Feature values + bare cfg flags counted by the cluster-prune scan, in source
    order — byte-equivalent to the `pred_re`/`feat_re`/`bare_re` loop in
    `_prune_redundant_clusters`. Per ``#[cfg(...)]``/``#[cfg_attr(...)]``: each
    non-empty ``feature = "X"`` value, then each bare ident (after the `feature`
    pairs are stripped) that is not a structural keyword (`skip`)."""
    out: list[str] = []
    for inner in _cfg_attr_inner_predicates(text):
        for _s, _e, v in _scan_feature_matches(inner):
            if v:
                out.append(v)
        for name in _bare_flag_idents(_feature_stripped(inner)):
            if name and name not in skip:
                out.append(name)
    return out


def parse_cfg_pred(pred: str) -> CfgPred:
    """Parse a ``#[cfg(...)]`` condition string into a typed :class:`CfgPred`.
    Normalizes whitespace first, so ``all (a, b)`` and ``all(a,b)`` parse alike."""
    return _parse(normalize_pred(pred))


def _parse(s: str) -> CfgPred:
    for op in ("not", "all", "any"):
        if s.startswith(op + "(") and s.endswith(")"):
            parts = [_parse(p) for p in split_top_level_commas(s[len(op) + 1:-1])]
            if op == "not":
                # `not()` with no inner is degenerate → evaluates undecidable.
                return Not(parts[0] if parts else Leaf(CfgAtom("")))
            return All(tuple(parts)) if op == "all" else Any(tuple(parts))
    return Leaf(CfgAtom.parse(s))


@dataclass(frozen=True, slots=True)
class CfgSet:
    """A cluster's cfg evaluation context: predicates known true / known false
    (both stored already-normalized) plus the crate's default features.

    ``of`` is the constructor callers use; it normalizes the raw predicate
    strings once. Use :meth:`evaluate` for plain truth and
    :meth:`excludes_for_cargo` for the cargo-faithful "does this gate keep the
    item out of THIS build?" question."""

    true: frozenset[str]
    false: frozenset[str]
    default_features: frozenset[str] = frozenset()

    @classmethod
    def of(cls, cfg_true: Iterable[str] = (), cfg_false: Iterable[str] = (),
           default_features: Iterable[str] = ()) -> CfgSet:
        return cls(
            true=frozenset(normalize_pred(x) for x in cfg_true),
            false=frozenset(normalize_pred(x) for x in cfg_false),
            default_features=frozenset(default_features),
        )

    def membership(self, normalized: str) -> bool | None:
        if normalized in self.true:
            return True
        if normalized in self.false:
            return False
        return None

    def evaluate(self, pred: str | CfgPred) -> bool | None:
        """Truth of a cfg predicate: True / False if fully decidable, else None."""
        tree = pred if isinstance(pred, CfgPred) else parse_cfg_pred(pred)
        return tree.evaluate(self)

    def excluded_uncovered_lines(self, source_text: str, uncov_lines: set[int]) -> set[int]:
        """The subset of ``uncov_lines`` that sit inside a single-line-`#[cfg(P)]`
        block whose predicate ``P`` is DECIDABLY false under this context — i.e.
        compiled out in this cluster, so no test could ever cover them.

        Replaces the two regex line-scanners ``_filter_cfg_incompatible_lines``
        (``not(X)`` for active X) and ``_filter_cfg_excluded_lines`` (bare X for
        excluded X) with one structured pass over the typed predicate tree (#20):
        build a ``CfgSet`` with the cluster's active cfgs in ``true`` and excluded
        cfgs in ``false`` and both cases reduce to ``evaluate(P) is False``. Plain
        ``evaluate`` (not ``excludes_for_cargo``) — only explicit true/false atoms
        decide, so undecidable target/feature atoms never over-exclude a coverable
        line. The block span is found by brace depth (regex-free)."""
        if not uncov_lines:
            return set()
        lines = source_text.splitlines()
        n = len(lines)
        excluded: set[int] = set()
        i = 0
        while i < n:
            stripped = lines[i].strip()
            if stripped.startswith("#[cfg(") and stripped.endswith(")]"):
                pred = stripped[len("#[cfg("):-len(")]")]
                if self.evaluate(pred) is False:
                    depth = 0
                    started = False
                    for j in range(i, min(n, i + 500)):
                        depth += lines[j].count("{") - lines[j].count("}")
                        if "{" in lines[j]:
                            started = True
                        if started and depth <= 0:
                            for lno in range(i + 1, j + 2):
                                if lno in uncov_lines:
                                    excluded.add(lno)
                            i = j
                            break
            i += 1
        return excluded

    def excludes_for_cargo(self, pred: str | CfgPred) -> bool:
        """cargo-faithful: does this cfg keep the gated item from compiling/running
        in this cluster? Provably-off custom atoms (a `--cfg` enabled nowhere, a
        non-default feature) are added to the false-set, then the tree is
        evaluated — builtin/target atoms stay undecidable so we never over-exclude."""
        tree = pred if isinstance(pred, CfgPred) else parse_cfg_pred(pred)
        aug_false = set(self.false)
        for atom in tree.atoms():
            if atom.is_off_for_cargo(self.true, self.default_features):
                aug_false.add(atom.normalized)
        aug = CfgSet(true=self.true, false=frozenset(aug_false),
                     default_features=self.default_features)
        return tree.evaluate(aug) is False


def cluster_cfg_set(cluster: object) -> CfgSet:
    """The ONE evaluation context for "is this item compiled in this cluster".

    Duck-typed on `BuildCluster` (`eval_cfg_true` / `cfg_false` /
    `default_features`) so this stays in the domain with no model import —
    hence `object` rather than a typing `Any`, which this module's own
    `Any` cfg-predicate node shadows.
    `cluster is None` is the no-cluster context — everything undecidable,
    which is what every caller's conservative branch already expects.

    It exists because ten reconstruct-side call sites open-coded
    `CfgSet.of(cluster.cfg_true, cluster.cfg_false)` and therefore evaluated
    without the crate's default features. `is_off_for_cargo` reads a
    `feature = "X"` not in `default_features` as provably off, so on the live
    `stable` cluster (`cfg_true: []`, default `["std"]`)
    `excludes_for_cargo('feature = "std"')` answered True and
    `excludes_for_cargo('not(feature = "std"))'` answered False: the arm the
    build compiles read as excluded and the dead arm as kept, for every crate
    with a default-on feature. Measured on the live tree (2026-09-01), the
    corrected context moves 36 of 359 fn records from undecidable to
    decidably ACTIVE and none to inactive, and proves exactly one type record
    dead — `src/backtrace.rs`'s `#[cfg(not(feature = "std"))] enum Backtrace`,
    whose method-less rendering in TYPES_IN_SCOPE is what a fill cited when
    it rejected calling `status()` on a `std::backtrace::Backtrace`."""
    if cluster is None:
        return CfgSet.of()
    true_ = getattr(cluster, "eval_cfg_true", None) or getattr(
        cluster, "cfg_true", ()) or ()
    return CfgSet.of(
        tuple(true_),
        tuple(getattr(cluster, "cfg_false", ())),
        tuple(getattr(cluster, "default_features", ()) or ()),
    )


def filter_cfg_inactive_branches(text: str, cfg_set: CfgSet) -> str:
    """Strip `#[cfg(P)]` blocks whose predicate evaluates decidably False under
    ``cfg_set``. Prompt-render use only — the model sees the active variant of
    each cfg-gated alternative (e.g. a single StdError definition) instead of
    all three simultaneously, which confuses method resolution.

    Each stripped unit is: the `#[cfg(P)]` line + any stacked attribute lines
    that follow it + the governed item (brace-delimited block or single line).
    Undecidable predicates (None) are left in place."""
    lines = text.splitlines(keepends=True)
    n = len(lines)
    out: list[str] = []
    i = 0
    while i < n:
        pred = match_cfg_attr_line(lines[i].rstrip("\n").rstrip("\r"))
        if pred is not None and cfg_set.evaluate(pred) is False:
            i += 1  # skip the inactive #[cfg(...)] line
            # Skip stacked attribute lines that follow (e.g. #[allow(...)])
            while i < n and lines[i].lstrip().startswith("#["):
                i += 1
            # Skip the governed item: brace block or single non-blank line
            if i < n:
                if "{" in lines[i]:
                    depth = 0
                    while i < n:
                        depth += lines[i].count("{") - lines[i].count("}")
                        i += 1
                        if depth <= 0:
                            break
                else:
                    i += 1
        else:
            out.append(lines[i])
            i += 1
    return "".join(out)


def cluster_gate_predicate(active_cfg: list[str], excluded_cfg: list[str]) -> str:
    """Build the cfg predicate body (without `#[cfg(...)]` wrapping) that is true
    for this cluster and false for all others — the gate used when several
    clusters have divergent versions of the same item.

    Each cfg may be activated EITHER as a bare ``--cfg N`` (RUSTFLAGS) OR as a
    cargo ``feature = "N"``, and a cluster sets only ONE of the two. So each
    active predicate becomes ``any(N, feature = "N")`` (true under whichever form
    this cluster uses) and each excluded predicate becomes ``not(any(...))``; the
    whole gate ANDs them. Built from active/excluded bare names — never the
    double ``all(N, feature = "N")`` form, which is unsatisfiable for a
    ``--cfg``-activated cluster (the feature is off)."""
    def _either(pred: str) -> str:
        return f'any({pred}, feature = "{pred}")'

    terms: list[str] = [_either(p) for p in (active_cfg or [])]
    terms += [f"not({_either(p)})" for p in (excluded_cfg or [])]
    if not terms:
        return ""
    if len(terms) == 1:
        return terms[0]
    return "all(" + ", ".join(terms) + ")"


def unreachable_under_every_cfg_set(
    predicates: Iterable[str], cfg_sets: Iterable[CfgSet],
) -> bool:
    """True when EVERY baked cfg-set compiles this item out.

    The union of the baked cfg-sets is the whole space the run ever built, so
    an item excluded by all of them was never reachable by any cluster's Phase
    C and can never be filled — it is unreachable BY CONSTRUCTION, not
    un-attempted. Live on `anyhow`: `nightly.rs` lines 12/17/22 (the `Debug`,
    `Display` and `Error::provide` impls for the local `MyError`) sit inside
    `#[cfg(anyhow_build_probe)]`, a probe cfg no `cargo test` sets. They are
    stubbed IDENTICALLY in both cluster trees, so t2b's pre-merge backfill has
    no donor either, and the final cfg-blind scan would `sys.exit(1)` on them
    after a fully green suite — converting a genuine success into a failure at
    the last step, having paid the run's whole cost.

    With NO cfg-sets on hand nothing is provably unreachable: absence of
    evidence must not become the exemption, or a dropped cluster's real stubs
    would be excused by the same rule that excuses the probe trio.
    """
    sets = list(cfg_sets)
    preds = [p for p in predicates if p and p.strip()]
    if not sets or not preds:
        return False
    return all(
        any(cs.excludes_for_cargo(p) for p in preds)
        for cs in sets)

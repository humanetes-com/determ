"""Cfg predicate domain — a typed, algebraic model of `#[cfg(...)]` (CODE_ARCHITECTURE.md §4 P5).

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

from collections.abc import Iterable
from dataclasses import dataclass

# Built-in cfg identifiers cargo evaluates from the build TARGET (not from a
# `--cfg`/feature flag). Undecidable to a cluster's cfg sets, but cargo MAY set
# them, so cargo-faithful exclusion must never force them off (never over-exclude,
# FR-018). Everything else that is a bare ident is a custom `--cfg NAME`, default OFF.
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
        force-exclude what cargo might set (FR-018)."""
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


_OPS = {"not": Not, "all": All, "any": Any}


# --- `#[cfg(...)]` ATTRIBUTE EXTRACTION from rendered source text (P7) ---
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


def cluster_gate_predicate(active_cfg: list[str], excluded_cfg: list[str]) -> str:
    """Build the cfg predicate body (without `#[cfg(...)]` wrapping) that is true
    for this cluster and false for all others — the gate used when several
    clusters have divergent versions of the same item (FR-032).

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

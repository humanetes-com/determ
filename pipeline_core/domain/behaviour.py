"""BDD behaviour oracle — pure domain.

A BEHAVIOUR is an observable Given/When/Then spec over a crate's PUBLIC API. We
harvest behaviours from the original library during the legal pre-strip window
(doc-tests, ``examples/``, integration ``tests/``) and — only for the gaps —
synthesise new ones from the public surface. Post-strip they serve two roles:
an executable oracle BEYOND unit tests (does the reconstruction reproduce the
observable outcome?) and a concrete RIGHT usage example injected into fill
prompts.

This is BEHAVIOUR extraction, never IMPLEMENTATION extraction: a behaviour
records an OBSERVABLE outcome — the same legality class as a test assertion —
never a body or a private call-sequence. The ``then`` is the RESULT of the
implementation, so a behaviour can be authored/checked without ever reading a
stripped body.

Pure VOs + enums, no dict payloads, no IO — the adapters do the parsing and the
capturing; this module only names the language and enforces its invariants.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Any

from pipeline_core.domain.example_setup import (
    IMPORTS_KEY_SUFFIX,
    helper_closure,
    helper_key_parts,
    stored_index,
    uses,
)


class OutcomeKind(Enum):
    """The observable a behaviour pins. STRUCTURED variants are preferred over
    ``NORMALIZED_STDOUT`` (better attribution, less nondeterminism);
    ``STATIC_ASSERTION`` is the compile-time class (``size_of``/autotraits/niche)
    that the layout-authoring stage consumes as its layout-pinning selector.

    ``TYPE_IDENTITY`` and ``ALT_DEBUG_STRING`` were added 2026-08-26 because the
    suite the bake wrote could not express two of the three observables the
    98-tree failures pin: `Error::msg("x").is::<&str>()` (a TypeId round-trip —
    DOWNCAST_OK carries a bool but not WHICH type) and the `{:#?}` alternate
    Debug render (`EXPECTED_ALTDEBUG_*` in tests/test_fmt.rs, a different
    string from `{:?}`)."""

    DISPLAY_STRING = auto()  # `x.to_string()` / `format!("{x}")`
    DEBUG_STRING = auto()  # `format!("{x:?}")`
    CHAIN_SEQUENCE = auto()  # the `.chain()` / iterator render, element by element
    DOWNCAST_OK = auto()  # `x.downcast_ref::<T>().is_some()` → bool
    PANIC_MESSAGE = auto()  # a `should_panic` payload (message only, no location)
    RETURN_RENDERED = auto()  # a returned value rendered deterministically
    NORMALIZED_STDOUT = auto()  # fallback: captured stdout, normalized
    STATIC_ASSERTION = auto()  # compile-time: size/align/autotraits/niche
    TYPE_IDENTITY = auto()  # `x.is::<T>()` / `x.downcast::<T>()` holds (value = T)
    ALT_DEBUG_STRING = auto()  # `format!("{x:#?}")` — the alternate Debug render


class StaticAssertKind(Enum):
    """Which layout property a ``STATIC_ASSERTION`` pins. These are the pins
    t1.5 MUST satisfy while it is free to author the private layout."""

    SIZE = auto()  # `size_of::<T>() == …`
    ALIGN = auto()  # `align_of::<T>() == …`
    AUTOTRAITS = auto()  # `assert_impl_all!(T: Send + Sync + Unpin + 'static)`
    NICHE = auto()  # `size_of::<Option<T>>() == size_of::<T>()` (null-ptr-opt)


# Runtime observables that require a compiling, runnable tree to check.
RUNTIME_OUTCOME_KINDS = frozenset(
    {
        OutcomeKind.DISPLAY_STRING,
        OutcomeKind.DEBUG_STRING,
        OutcomeKind.CHAIN_SEQUENCE,
        OutcomeKind.DOWNCAST_OK,
        OutcomeKind.PANIC_MESSAGE,
        OutcomeKind.RETURN_RENDERED,
        OutcomeKind.NORMALIZED_STDOUT,
        OutcomeKind.TYPE_IDENTITY,
        OutcomeKind.ALT_DEBUG_STRING,
    }
)

# The kinds whose Then is a rendered string the reconstruction must reproduce
# byte-for-byte (Display / Debug / alternate Debug).
FORMAT_OUTCOME_KINDS = frozenset(
    {
        OutcomeKind.DISPLAY_STRING,
        OutcomeKind.DEBUG_STRING,
        OutcomeKind.ALT_DEBUG_STRING,
    }
)

# The sentence every behaviour-rendering prompt carries: the model owns the
# representation, the behaviours own the observable. Other prompt renderers
# import it by this name — keep the name and the wording stable.
MAY_REPRESENTATION_NOTE = (
    "The layout, allocation strategy and private names are yours (MAY); every behaviour listed is a MUST."
)


@dataclass(frozen=True, slots=True)
class ObservableOutcome:
    """The Then of a behaviour — a SUM over :class:`OutcomeKind`. Exactly one
    payload shape is valid per kind (enforced in ``__post_init__``). A behaviour
    NEVER pins an internal call-graph (unobservable + needs bodies) — only the
    observable result."""

    kind: OutcomeKind
    value: str = ""  # DISPLAY/DEBUG/PANIC/RETURN/STDOUT text; TYPE_IDENTITY type
    chain: tuple[str, ...] = ()  # CHAIN_SEQUENCE elements, in order
    ok: bool = False  # DOWNCAST_OK / TYPE_IDENTITY
    static_kind: StaticAssertKind | None = None  # STATIC_ASSERTION only
    static_expr: str = ""  # the asserted expression, verbatim
    #: `value` is a PREFIX of the observation, not the whole of it, and the
    #: observation is strictly longer. A render whose tail is not
    #: reproducible across processes (a captured backtrace's frames, an
    #: address, an elapsed time) has no exact Then that can replay: the
    #: capture runs in the probe binary and the replay in the test binary,
    #: so an `assert_eq!` on the whole string is red by construction (run
    #: `330242-0be2c3e1`, 2026-09-03, withdrew the only Debug probe of both
    #: clusters as "replay disagreed with the capture"). The prefix is what
    #: two independent captures of the same world agreed on, so it is the
    #: part of the render this crate decides and the process does not.
    prefix: bool = False

    def __post_init__(self) -> None:
        k = self.kind
        if k is OutcomeKind.STATIC_ASSERTION:
            if self.static_kind is None:
                raise ValueError("STATIC_ASSERTION requires static_kind")
        elif self.static_kind is not None:
            raise ValueError(f"static_kind set on non-static outcome {k.name}")
        if k is OutcomeKind.CHAIN_SEQUENCE and not self.chain:
            raise ValueError("CHAIN_SEQUENCE requires a non-empty chain")
        if self.prefix and k in (OutcomeKind.TYPE_IDENTITY,
                                 OutcomeKind.DOWNCAST_OK,
                                 OutcomeKind.STATIC_ASSERTION,
                                 OutcomeKind.CHAIN_SEQUENCE):
            # A prefix is a statement about rendered text. A bool, a
            # compile-time assertion and an element sequence have no
            # prefix to take, and pretending otherwise would assert
            # nothing while reading as a pin.
            raise ValueError(f"prefix outcome is not a text render: {k.name}")
        if self.prefix and not self.value:
            raise ValueError("a prefix outcome with an empty value pins nothing")
        if k is OutcomeKind.TYPE_IDENTITY and not self.value.strip():
            # A TypeId pin with no type names nothing — `is::<?>()` is not an
            # observable, and an empty value would silently pass any type.
            raise ValueError("TYPE_IDENTITY requires the type name as value")

    # -- named constructors (keep call sites honest about the sum) ----------
    @classmethod
    def display(cls, text: str) -> ObservableOutcome:
        return cls(OutcomeKind.DISPLAY_STRING, value=text)

    @classmethod
    def debug(cls, text: str) -> ObservableOutcome:
        return cls(OutcomeKind.DEBUG_STRING, value=text)

    @classmethod
    def alt_debug(cls, text: str) -> ObservableOutcome:
        """The `{:#?}` render — a DIFFERENT string from `{:?}` for every type
        whose Debug impl honours the alternate flag."""
        return cls(OutcomeKind.ALT_DEBUG_STRING, value=text)

    @classmethod
    def chain_seq(cls, elems: tuple[str, ...]) -> ObservableOutcome:
        return cls(OutcomeKind.CHAIN_SEQUENCE, chain=tuple(elems))

    @classmethod
    def downcast_ok(cls, ok: bool) -> ObservableOutcome:
        return cls(OutcomeKind.DOWNCAST_OK, ok=ok)

    @classmethod
    def type_identity(cls, type_name: str, ok: bool) -> ObservableOutcome:
        """`x.is::<type_name>()` / `x.downcast::<type_name>()` holds (`ok`) or
        is refused (`not ok`). The type is the value so the pin says WHICH
        TypeId the round-trip must preserve."""
        return cls(OutcomeKind.TYPE_IDENTITY, value=type_name, ok=ok)

    @classmethod
    def panic(cls, message: str) -> ObservableOutcome:
        return cls(OutcomeKind.PANIC_MESSAGE, value=message)

    @classmethod
    def rendered(cls, text: str) -> ObservableOutcome:
        return cls(OutcomeKind.RETURN_RENDERED, value=text)

    @classmethod
    def stdout(cls, text: str) -> ObservableOutcome:
        return cls(OutcomeKind.NORMALIZED_STDOUT, value=text)

    @classmethod
    def static_assertion(
        cls,
        static_kind: StaticAssertKind,
        expr: str,
    ) -> ObservableOutcome:
        return cls(OutcomeKind.STATIC_ASSERTION, static_kind=static_kind, static_expr=expr)

    @property
    def is_runtime(self) -> bool:
        """True iff checking this outcome needs a running tree (oracle-use
        gate; STATIC_ASSERTION is compile-time and layout-authoring-consumed instead)."""
        return self.kind in RUNTIME_OUTCOME_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.name,
            "value": self.value,
            "chain": list(self.chain),
            "ok": self.ok,
            "static_kind": self.static_kind.name if self.static_kind else None,
            "static_expr": self.static_expr,
            **({"prefix": True} if self.prefix else {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ObservableOutcome:
        sk = d.get("static_kind")
        return cls(
            kind=OutcomeKind[str(d["kind"])],
            value=str(d.get("value", "")),
            chain=tuple(str(x) for x in (d.get("chain") or [])),
            ok=bool(d.get("ok", False)),
            static_kind=StaticAssertKind[str(sk)] if sk else None,
            static_expr=str(d.get("static_expr", "")),
            prefix=bool(d.get("prefix", False)),
        )


_TRUE_WORDS = frozenset({"true", "some", "ok", "yes"})
_FALSE_WORDS = frozenset({"false", "none", "err", "no"})


def outcome_matches(observed: str, then: ObservableOutcome) -> bool:
    """Does the text `observed` (a checker's predicted Then, or a capture)
    satisfy `then` under the oracle's own comparison?

    TYPE_IDENTITY / DOWNCAST_OK: the text is a truth word (`true`/`false`,
    `Some`/`None`) equal to `ok`. CHAIN_SEQUENCE: one element per line, equal
    in order. A `prefix` Then: the observation STARTS WITH the value and is
    strictly longer — the same two assertions `render_replay_test` emits for
    a replay, so a text this function accepts is a text the replay accepts.
    Everything else: equal after dropping one trailing newline (what
    `println!` adds). STATIC_ASSERTION is compile-time and matches nothing."""
    text = observed[:-1] if observed.endswith("\n") else observed
    kind = then.kind
    if kind is OutcomeKind.STATIC_ASSERTION:
        return False
    if kind in (OutcomeKind.TYPE_IDENTITY, OutcomeKind.DOWNCAST_OK):
        word = text.strip().strip("`").lower()
        if word in _TRUE_WORDS:
            return then.ok
        if word in _FALSE_WORDS:
            return not then.ok
        return False
    if kind is OutcomeKind.CHAIN_SEQUENCE:
        return tuple(ln for ln in text.split("\n")) == then.chain
    if then.prefix:
        return text.startswith(then.value) and len(text) > len(then.value)
    return text == then.value


def then_value_text(then: ObservableOutcome) -> str:
    """The Then as the text a reader (or a checker) is asked to produce: a
    truth word for an identity or downcast, one element per line for a
    chain, the value otherwise."""
    if then.kind in (OutcomeKind.TYPE_IDENTITY, OutcomeKind.DOWNCAST_OK):
        return "true" if then.ok else "false"
    if then.kind is OutcomeKind.CHAIN_SEQUENCE:
        return "\n".join(then.chain)
    if then.kind is OutcomeKind.STATIC_ASSERTION:
        return then.static_expr
    return then.value


class BehaviourProvenance(Enum):
    """Where a behaviour came from. HARVESTED = lifted verbatim from the real
    lib's doc-tests/examples/tests (zero hallucination). SYNTHESIZED = authored
    post-hoc from the PUBLIC surface only, then validated against the real lib
    (a non-compiling/mismatching synthesis is discarded → self-filtering)."""

    HARVESTED = auto()
    SYNTHESIZED = auto()


class BehaviourSubKind(Enum):
    """The oracle mechanic. ASSERTION = the test's own ``assert!`` IS the Then
    (pass = ran panic-free). OUTCOME = capture the observable and compare it to
    the frozen golden captured from the real lib pre-strip."""

    ASSERTION = auto()
    OUTCOME = auto()


class FenceDirective(Enum):
    """A doc-test code-fence info-string attribute — parsed and HONORED, never
    guessed. Mishandling silently corrupts the oracle (e.g. treating a
    ``compile_fail`` NEGATIVE behaviour as a positive assertion)."""

    RUN = auto()  # plain ``` — compile + run
    NO_RUN = auto()  # `no_run` — compile only, don't execute
    IGNORE = auto()  # `ignore` — skip entirely
    SHOULD_PANIC = auto()  # `should_panic` — the Then IS a panic, not a value
    COMPILE_FAIL = auto()  # `compile_fail` — asserts NON-compilation (negative)


# Fences whose behaviour is usable as a POSITIVE runtime oracle. `ignore` is
# skipped; `no_run` compiles but yields no runtime observable; `compile_fail`
# is a negative behaviour (using it as a positive oracle is backwards).
POSITIVE_RUNNABLE_FENCES = frozenset(
    {
        FenceDirective.RUN,
        FenceDirective.SHOULD_PANIC,
    }
)


class BehaviourClass(Enum):
    """WHAT KIND of promise a behaviour makes about the reconstruction — the
    axis a prompt renderer groups on and a census histograms on. Distinct from
    :class:`OutcomeKind` (HOW the observable is captured): two DISPLAY strings
    can be an exact-format promise and a bounded-resource promise.

    IDENTITY               a TypeId round-trip: what went in comes back out
                           through `is::<T>()` / `downcast::<T>()`.
    TRAIT_OBJECT_IDENTITY  the same, observed THROUGH a trait object
                           (`as &dyn …`) — the vtable, not the payload, answers.
    EXACT_FORMAT           a Display/Debug/alternate-Debug string, byte-exact.
    BOUNDED_RESOURCE       a length/size/capacity boundary (a heuristic — see
                           :func:`classify_behaviour`).
    OPTION_SEMANTICS       `Some`/`None` at the observable.
    LAYOUT_PIN             a compile-time size/align/niche/autotrait fact.
    CFG_WORLD              a promise that only holds in one cfg cluster.
    OTHER                  anything not recognised; never a failure to classify.
    """

    IDENTITY = auto()
    TRAIT_OBJECT_IDENTITY = auto()
    EXACT_FORMAT = auto()
    BOUNDED_RESOURCE = auto()
    OPTION_SEMANTICS = auto()
    LAYOUT_PIN = auto()
    CFG_WORLD = auto()
    OTHER = auto()


class OriginKind(Enum):
    """What an example was lifted from."""

    TEST = auto()  # one assertion of a `#[test]` item in `tests/`
    DOCTEST = auto()  # one runnable doc-comment fence
    STATIC_PIN = auto()  # a compile-time layout fact (doc fence or test file)
    PROBE = auto()  # a model-authored capture run against the original


@dataclass(frozen=True, slots=True)
class ExampleOrigin:
    """Where one example came from, typed. `file` is the source file; `name`
    the test fn (TEST) or probe unit (PROBE); `index` the assertion number
    within the test, the doc fence's line, or the pin's ordinal."""

    kind: OriginKind
    file: str = ""
    name: str = ""
    index: int = 0

    @property
    def key(self) -> str:
        """The item this example belongs to: `file::test` for a test
        assertion, `file:line` for a doc fence, `probe::unit` for a probe."""
        if self.kind is OriginKind.TEST:
            return f"{self.file}::{self.name}"
        if self.kind is OriginKind.PROBE:
            return f"probe::{self.name}"
        if self.kind is OriginKind.STATIC_PIN:
            return f"{self.file}:{self.name}" if self.name else self.file
        return f"{self.file}:{self.index}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.name, "file": self.file, "name": self.name, "index": self.index}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExampleOrigin:
        return cls(
            kind=OriginKind[str(d["kind"])],
            file=str(d.get("file", "")),
            name=str(d.get("name", "")),
            index=int(d.get("index", 0) or 0),
        )

    @classmethod
    def from_behaviour_id(cls, bid: str) -> ExampleOrigin | None:
        """The origin a legacy (format 1) id spells, or None. Ids are
        `testassert::<file>::<test>::<i>`, `doctest::<file>::<line>`,
        `static::<file>[::<line>]::<i>`, `probe::<cluster>::<unit>::<n>`."""
        parts = bid.split("::")
        head = parts[0] if parts else ""
        try:
            if head == "testassert" and len(parts) == 4:
                return cls(OriginKind.TEST, parts[1], parts[2], int(parts[3]))
            if head == "doctest" and len(parts) == 3:
                return cls(OriginKind.DOCTEST, parts[1], "", int(parts[2]))
            if head == "static" and len(parts) == 3:
                return cls(OriginKind.STATIC_PIN, parts[1], "", int(parts[2]))
            if head == "static" and len(parts) == 4:
                return cls(OriginKind.STATIC_PIN, parts[1], parts[2], int(parts[3]))
            if head == "probe" and len(parts) == 4:
                return cls(OriginKind.PROBE, "", parts[2], int(parts[3]))
        except ValueError:
            return None
        return None


@dataclass(frozen=True, slots=True)
class Rule:
    """One sentence stating the observable invariant a set of examples
    illustrates, in the crate's public vocabulary (the glossary). Authored at
    t0d from the examples and the public docs, and KEPT only when a checker
    given the sentence predicts every cited example's Then while the same
    checker without it does not (`domain/behaviour_book.py`).

    `terms` are the glossary names the sentence uses (computed, never
    authored); `feature` is the public item the rule's examples are grouped
    under. Neither names a private item: a sentence that did was dropped.

    `example_ids` are the examples the rule is CREDITED with — the ones its
    chunk's rule set predicts, the ablation misses, and the set without this
    rule misses (`behaviour_book.judge_rule_set`). `refuted_on` are cited
    examples the set answered and got wrong: recorded, not a veto of the
    whole sentence."""

    rule_id: str
    sentence: str
    example_ids: tuple[str, ...]
    feature: str
    terms: tuple[str, ...] = ()
    refuted_on: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rule_id": self.rule_id,
            "sentence": self.sentence,
            "example_ids": list(self.example_ids),
            "feature": self.feature,
            "terms": list(self.terms),
        }
        if self.refuted_on:
            out["refuted_on"] = list(self.refuted_on)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Rule:
        return cls(
            rule_id=str(d["rule_id"]),
            sentence=str(d["sentence"]),
            example_ids=tuple(str(x) for x in (d.get("example_ids") or [])),
            feature=str(d.get("feature", "")),
            terms=tuple(str(x) for x in (d.get("terms") or [])),
            refuted_on=tuple(str(x) for x in (d.get("refuted_on") or [])),
        )


@dataclass(frozen=True, slots=True)
class BehaviourSpec:
    """One EXAMPLE: a frozen setup / when / then over the public API.
    ``exercises`` (the set of unit ids the example executes, from coverage of
    the original) is the localizer a mismatch escalates through: a diverging
    behaviour names the fns that produced it, so the escalation targets bodies
    rather than the whole crate.

    ``setup`` is what ``when`` needs and nothing else. For a test assertion it
    is the def-use slice of the test item before the assertion
    (`domain/example_setup.py`), never the whole item; for a probe it is the
    model's setup statements; a doc fence and a layout pin carry their whole
    program in ``when``. ``helper_keys`` name the file-level items of the test
    file the example calls (`bail_literal`, `f`, the asserting helper), stored
    ONCE in :attr:`BehaviourSuite.helpers`; ``helpers`` is their text, filled
    by the suite on load and never serialized per example. ``via`` is the
    asserting helper the test routes the literal through (`assert_err`), so a
    reader knows the Then is what that helper compares, not ``when`` itself.
    ``rules`` are the kept rules citing this example, attached by the suite.

    Every reader that wants "the code this example runs" calls
    :func:`example_code`; nothing re-derives it."""

    behaviour_id: str
    setup: str  # the statements `when` needs (see above)
    when: str  # PublicApiAction — the exercised call
    then: ObservableOutcome | None = None  # None for ASSERTION (the assert IS the Then)
    exercises: frozenset[str] = frozenset()
    provenance: BehaviourProvenance = BehaviourProvenance.HARVESTED
    subkind: BehaviourSubKind = BehaviourSubKind.ASSERTION
    fence: FenceDirective = FenceDirective.RUN
    cfg_cluster: str = "default"  # nightly backtrace behaviour ≠ stable
    source_ref: str = ""  # where harvested (file:line / example name)
    klass: BehaviourClass = BehaviourClass.OTHER  # see classify_behaviour
    # Env the replay must run under. Empty = the value does not move with
    # OBSERVABLE_ENV_KEYS (verified under both the capture env and the
    # perturb). Non-empty is load-bearing: render_replay_test writes set_var
    # from it before the setup, and the emitter verifies that test only as a
    # pin of that world. A previous capture_env field was write-only and
    # caught nothing; this one is consumed at render and at verify.
    required_env: tuple[tuple[str, str], ...] = ()
    origin: ExampleOrigin | None = None
    via: str = ""
    helper_keys: tuple[str, ...] = ()
    helpers: tuple[str, ...] = field(default=(), compare=False)
    rules: tuple[Rule, ...] = field(default=(), compare=False)
    # The book features listing this example, attached by the suite like
    # `rules` and never serialized: a function's reach (which features'
    # examples run it) is read off this (`candidate_tiers.feature_reach`).
    features: tuple[str, ...] = field(default=(), compare=False)
    # The crate type an identity example is asked through: the self type of
    # the inherent impl that declares the method carrying the identity's type
    # argument (`err.downcast::<T>()` -> the impl declaring `downcast`).
    # Resolved against the cluster's records when the specs are loaded for a
    # fill (`behaviour_select.resolve_identity_subjects`), never serialized.
    subject: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        # An OUTCOME-behaviour compares a captured observable to a golden Then,
        # so it MUST carry one; an ASSERTION-behaviour's own `assert!` is the
        # check, so a Then would be redundant/misleading.
        if self.subkind is BehaviourSubKind.OUTCOME and self.then is None:
            raise ValueError("OUTCOME behaviour requires a `then` outcome")
        if self.subkind is BehaviourSubKind.ASSERTION and self.then is not None:
            raise ValueError("ASSERTION behaviour's assert IS the Then — drop `then`")

    @property
    def is_positive_oracle(self) -> bool:
        """Usable as a positive runtime oracle: a runnable fence AND (for
        outcome-behaviours) a runtime observable. compile_fail/ignore/no_run and
        pure static assertions are excluded from the runtime oracle gate."""
        if self.fence not in POSITIVE_RUNNABLE_FENCES:
            return False
        if self.subkind is BehaviourSubKind.OUTCOME:
            return self.then is not None and self.then.is_runtime
        return True

    @property
    def has_runtime_then(self) -> bool:
        """An OUTCOME spec whose Then needs a running tree to check."""
        return self.then is not None and self.then.is_runtime

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "behaviour_id": self.behaviour_id,
            "setup": self.setup,
            "when": self.when,
            "then": self.then.to_dict() if self.then else None,
            "exercises": sorted(self.exercises),
            "provenance": self.provenance.name,
            "subkind": self.subkind.name,
            "fence": self.fence.name,
            "cfg_cluster": self.cfg_cluster,
            "source_ref": self.source_ref,
            "klass": self.klass.name,
        }
        if self.required_env:
            d["required_env"] = dict(self.required_env)
        if self.origin is not None:
            d["origin"] = self.origin.to_dict()
        if self.via:
            d["via"] = self.via
        if self.helper_keys:
            d["helper_keys"] = list(self.helper_keys)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, legacy: bool = False) -> BehaviourSpec:
        """`legacy` reads a format-1 row: its `given` (the whole test item for
        a test assertion) becomes `setup` unchanged, and the origin is parsed
        from the id. A legacy store is read so a tree baked before format 2
        keeps working as it did; it is never written back in that shape."""
        raw_env = d.get("required_env") or ()
        if isinstance(raw_env, dict):
            required_env = tuple(sorted((str(k), str(v)) for k, v in raw_env.items()))
        else:
            required_env = tuple((str(a), str(b)) for a, b in raw_env)
        bid = str(d["behaviour_id"])
        origin_raw = d.get("origin")
        if isinstance(origin_raw, dict):
            origin: ExampleOrigin | None = ExampleOrigin.from_dict(origin_raw)
        else:
            origin = ExampleOrigin.from_behaviour_id(bid) if legacy else None
        setup = str(d.get("given", "")) if legacy else str(d.get("setup", ""))
        return cls(
            behaviour_id=bid,
            setup=setup,
            when=str(d.get("when", "")),
            then=(ObservableOutcome.from_dict(d["then"]) if d.get("then") is not None else None),
            exercises=frozenset(str(x) for x in (d.get("exercises") or [])),
            provenance=BehaviourProvenance[str(d.get("provenance", "HARVESTED"))],
            subkind=BehaviourSubKind[str(d.get("subkind", "ASSERTION"))],
            fence=FenceDirective[str(d.get("fence", "RUN"))],
            cfg_cluster=str(d.get("cfg_cluster", "default")),
            source_ref=str(d.get("source_ref", "")),
            klass=BehaviourClass[str(d.get("klass", "OTHER"))],
            required_env=required_env,
            origin=origin,
            via=str(d.get("via", "")),
            helper_keys=tuple(str(x) for x in (d.get("helper_keys") or [])),
        )


def example_when(spec: BehaviourSpec) -> str:
    """The when as a reader should see it: routed through the asserting
    helper when there is one (`assert_err(test, …)` — the Then is what the
    helper compares), else the expression itself."""
    when = spec.when.strip()
    return f"{spec.via}({when}, …)" if spec.via else when


def example_code(spec: BehaviourSpec) -> str:
    """Everything an example runs, as one text: the test-file helpers it
    calls, its setup, its when. The one accessor every reader that matches
    names against an example, or shows one, goes through — so no reader
    reaches for a whole test item again."""
    parts = [*spec.helpers, spec.setup, spec.when]
    return "\n".join(p for p in (x.strip() for x in parts) if p)


def helper_items(spec: BehaviourSpec) -> tuple[tuple[str, str], ...]:
    """`(key, text)` of the test-file items the example calls, in stored
    order (the harvester's first-reached order)."""
    return tuple(zip(spec.helper_keys, spec.helpers, strict=False))


@dataclass(frozen=True, slots=True)
class ExampleGiven:
    """What one example is given, as four kinds of helper key and its own
    setup. The harvester stores an example's helpers as one closure; a view
    that pasted that closure under `Given` printed 6,796 of 8,818 lines of
    the anyhow `stable` view as helper text (books written 2026-10-01), 96%
    of it copies, and called an asserting helper and a `use` block
    preconditions.

    - ``roots``: the file-level items the example's own code names — one key
      per name, the declaration when the file has one. These are its givens.
    - ``closure``: what the roots pull in (a root type's impls, the items
      their text names), and any key nothing below claims.
    - ``observer``: the asserting helper the Then is compared through
      (``via``) and what only it pulls in. How the Then is read, not a
      precondition.
    - ``imports``: the file's `use` block. Name resolution, not a
      precondition.

    Keys only; the text stays in the suite's table (:func:`helper_text`).
    ``reach`` is each root's own share of the closure, for a reader that
    shows a root once with what it stands on."""

    roots: tuple[str, ...] = ()
    closure: tuple[str, ...] = ()
    observer: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    setup: str = ""
    reach: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def keys(self) -> frozenset[str]:
        """Every helper key of the example."""
        return frozenset((*self.roots, *self.closure, *self.observer, *self.imports))


def example_given(spec: BehaviourSpec) -> ExampleGiven:
    """How to show or send an example's precondition (:class:`ExampleGiven`).
    A pure function of what the store holds: a name-in-code test over the
    example's setup and when, the same `uses` and the same closure walk the
    harvester resolved the keys with, the import key suffix it writes, and
    ``via``. :func:`example_code` stays the accessor for everything the
    example runs."""
    items = helper_items(spec)
    imports = tuple(k for k, _ in items if k.endswith(IMPORTS_KEY_SUFFIX))
    index = stored_index(items)
    named = uses(f"{spec.setup}\n{spec.when}")
    root_of: dict[str, str] = {}
    for key, _text in items:
        _file, name, _is_impl = helper_key_parts(key)
        if key in imports or name not in named or name in root_of:
            continue
        declared = [k for k, item in index[name] if not item.is_impl]
        root_of[name] = (declared or [k for k, _ in index[name]])[0]
    roots = list(root_of.values())
    reach = [(root, tuple(k for k, _ in helper_closure("", index, extra=(name,))
                          if k not in roots))
             for name, root in root_of.items()]
    claimed = {*roots, *(k for _, pulled in reach for k in pulled)}
    observer = tuple(k for k, _ in helper_closure("", index, extra=(spec.via,))
                     if k not in claimed) if spec.via else ()
    rest = tuple(k for k, _ in items
                 if k not in roots and k not in observer and k not in imports)
    return ExampleGiven(roots=tuple(roots), closure=rest, observer=observer, imports=imports,
                        setup=spec.setup, reach=tuple(reach))


def helper_text(spec: BehaviourSpec, keys: Collection[str]) -> tuple[str, ...]:
    """The text of the example's helpers among `keys`, in stored order."""
    return tuple(t for k, t in helper_items(spec) if k in keys)


# --- classification ---------------------------------------------------------

# A resource boundary is a NUMBER next to one of these tokens on the same line
# (`s.len() == 3`, `"x".repeat(1024)`, `&buf[..16]`, `Vec::with_capacity(8)`).
_RESOURCE_TOKENS = ("len(", "repeat(", "[..", "capacity")
# ... or a COMMENT that names a limit.
_LIMIT_WORDS = ("limit", "maximum", "max ", "bound", "capacity", "overflow", "truncat", "at most", "no more than")
_CFG_GATE_MARKERS = ("#[cfg(", "cfg!(", "#[rustversion", "rustversion::cfg!")


def _ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def _skip_space(text: str, i: int) -> int:
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    return i


def _skip_word(text: str, i: int, word: str) -> int:
    """Past `word` at `i` (and the space after it) when it is there as a whole
    token, else `i` unchanged."""
    end = i + len(word)
    if text.startswith(word, i) and (end >= len(text) or not _ident_char(text[end])):
        return _skip_space(text, end)
    return i


def _skip_lifetime(text: str, i: int) -> int:
    """Past a `'a` / `'static` lifetime at `i`, else `i` unchanged."""
    if i >= len(text) or text[i] != "'":
        return i
    i += 1
    while i < len(text) and _ident_char(text[i]):
        i += 1
    return _skip_space(text, i)


def observes_through_trait_object(when: str) -> bool:
    """True when `when` casts the value to a trait object (`as &dyn …`).

    The letters `dyn` anywhere are not enough: a TYPE_IDENTITY whose type
    argument is `Box<dyn StdError + Send + Sync>` is a concrete downcast of
    that Box, not an observation through a vtable. Measured on the live
    230-spec suite (2026-08-27): 35 TYPE_IDENTITY, 0 with a trait-object
    cast, 1 with `dyn` inside a turbofish — that one must stay IDENTITY.

    `type_id()` is also not enough. A capture of `(&*err as &dyn Error).is::<&str>()`
    never writes `type_id()`; keying on that substring made TRAIT_OBJECT_IDENTITY
    unstorable for every sensible capture while still asking for it.

    The reference forms accepted are the ones Rust accepts, in Rust's order:
    `&dyn`, `&mut dyn`, `&\'a dyn`, `&\'a mut dyn`. Reading `mut` before the
    lifetime accepted `&mut \'a dyn`, which does not parse, and rejected
    `&\'a mut dyn`, which is the only legal spelling when both appear.
    `*const dyn` / `*mut dyn` are the same observation through a vtable with a
    raw pointer instead of a reference, so they count too."""
    i = 0
    n = len(when)
    while True:
        j = when.find(" as ", i)
        if j < 0:
            return False
        i = j + 4
        k = _skip_space(when, i)
        if k < n and when[k] == "&":
            k = _skip_space(when, k + 1)
            k = _skip_word(when, _skip_lifetime(when, k), "mut")
        elif k < n and when[k] == "*":
            k = _skip_space(when, k + 1)
            before = k
            k = _skip_word(when, _skip_word(when, k, "const"), "mut")
            if k == before:  # `*` that is not a raw-pointer type
                continue
        else:
            continue
        # `&(dyn Error + Send)`: the parenthesised form of the same cast.
        if k < n and when[k] == "(":
            k = _skip_space(when, k + 1)
        if _skip_word(when, k, "dyn") != k:
            return True


def _line_has_numeric_literal(line: str) -> bool:
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c.isdigit():
            before = line[i - 1] if i else ""
            if not (before.isalpha() or before == "_"):
                return True
        i += 1
    return False


def has_resource_boundary(text: str) -> bool:
    """HEURISTIC: does `text` (a test item) pin a length/size/capacity bound?

    True when a line carries a numeric literal beside `len(` / `repeat(` /
    `[..` / `capacity`, or a `//` comment names a limit (`limit`, `maximum`,
    `bound`, `capacity`, `overflow`, `truncat…`, `at most`). It is a token
    heuristic, deliberately: telling an exact-format promise from a bounded
    one needs the test's intent, which the text only hints at, and a wrong
    BOUNDED_RESOURCE costs one histogram bucket, never an oracle verdict.
    Keep it a heuristic — do not grow it into a parser."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        comment_at = line.find("//")
        code = line if comment_at < 0 else line[:comment_at]
        comment = "" if comment_at < 0 else line[comment_at + 2 :].lower()
        if comment and any(w in comment for w in _LIMIT_WORDS):
            return True
        if any(t in code for t in _RESOURCE_TOKENS) and _line_has_numeric_literal(code):
            return True
    return False


def classify_behaviour(
    spec: BehaviourSpec,
    *,
    default_cluster: str = "default",
) -> BehaviourClass:
    """The :class:`BehaviourClass` of `spec`, from its kind and its text. Pure;
    never raises; OTHER when nothing matches. Precedence, first hit wins:

    1. STATIC_ASSERTION                       → LAYOUT_PIN
    2. TYPE_IDENTITY                          → TRAIT_OBJECT_IDENTITY when the
       `when` casts through a trait object (`as &dyn …`), else IDENTITY
    3. `when` has `.is_none()`/`.is_some()`, or the Then value is `None` /
       starts with `Some(`                    → OPTION_SEMANTICS
    4. DISPLAY / DEBUG / ALT_DEBUG            → BOUNDED_RESOURCE when the test
       example's code (:func:`example_code` — its helpers, setup and
       when, never the whole test item) carries a resource boundary
       (:func:`has_resource_boundary`), else EXACT_FORMAT
    5. cfg_cluster ≠ `default_cluster` AND the example's code carries a cfg
       gate (`#[cfg(`, `cfg!(`, `#[rustversion`) → CFG_WORLD
    6. otherwise OTHER.

    `default_cluster` is the NAME of the crate's default cluster (anyhow's is
    `stable`, not `default`); the caller that knows the cluster manifest
    passes it, so a stable-only spec is never read as another world."""
    then = spec.then
    if then is not None and then.kind is OutcomeKind.STATIC_ASSERTION:
        return BehaviourClass.LAYOUT_PIN
    if then is not None and then.kind is OutcomeKind.TYPE_IDENTITY:
        if observes_through_trait_object(spec.when):
            return BehaviourClass.TRAIT_OBJECT_IDENTITY
        return BehaviourClass.IDENTITY
    then_value = then.value.strip() if then is not None else ""
    from pipeline_core.domain.behaviour_probe import observes_presence
    if observes_presence(spec.when) or (
        then is not None and (then_value == "None" or then_value.startswith("Some("))
    ):
        return BehaviourClass.OPTION_SEMANTICS
    if then is not None and then.kind in FORMAT_OUTCOME_KINDS:
        if has_resource_boundary(example_code(spec)):
            return BehaviourClass.BOUNDED_RESOURCE
        return BehaviourClass.EXACT_FORMAT
    if spec.cfg_cluster != default_cluster:
        text = example_code(spec)
        if any(m in text for m in _CFG_GATE_MARKERS):
            return BehaviourClass.CFG_WORLD
    return BehaviourClass.OTHER


def classified(spec: BehaviourSpec, *, default_cluster: str = "default") -> BehaviourSpec:
    """`spec` with its `klass` set by :func:`classify_behaviour`."""
    return replace(spec, klass=classify_behaviour(spec, default_cluster=default_cluster))


# The store's shape. 1 (no `format` key) held the whole test item in every
# example's `given`; 2 holds the setup slice, the typed origin, the test-file
# helpers once per suite, and the behaviour book (features, rules, glossary).
SUITE_FORMAT = 2


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    """One public name of the crate and the first sentence of its doc
    comment: the vocabulary rules are written in. `kind` is `fn`, `macro`,
    `struct`, `enum`, `trait` or `type`; `name` is how a user spells it
    (`Error::msg`, `ensure!`, `Context::context`, `Error`).

    `worlds` is empty when every build world compiles the name, else the
    worlds that do (`cfg_worlds.world_mark`). The book is drafted once over
    every world's vocabulary, so a name only some builds have is shown once,
    marked, instead of forking the whole book over it: on run
    `72627-751534fe` (2026-09-30) the two clusters' book prompts differed by
    exactly that one glossary line.

    `signature` is a public fn's declaration as written (`FnRecord.sig_text`,
    whitespace collapsed): the frozen public API the bake's surface already
    shows every fill. Without it a rule sentence can invent a return shape:
    a kept rule on run `72627-751534fe` said `Error::downcast` "yields Some"
    when the vocabulary line gave only the doc sentence (it returns a
    `Result`)."""

    name: str
    kind: str
    summary: str = ""
    worlds: tuple[str, ...] = ()
    signature: str = ""

    @property
    def leaf(self) -> str:
        """The name a call site spells: the last `::` segment, `!` dropped."""
        return self.name.rsplit("::", 1)[-1].rstrip("!")

    def merged(self, other: GlossaryEntry) -> GlossaryEntry:
        """This entry with each of `summary` and `signature` taken from
        `other` where this one has none: two records (or two worlds) of one
        name each contribute what they carry, first non-empty wins per field.
        Replacing the whole entry by the one with a summary dropped the other
        record's signature."""
        return replace(self, summary=self.summary or other.summary,
                       signature=self.signature or other.signature)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "kind": self.kind, "summary": self.summary}
        if self.worlds:
            out["worlds"] = list(self.worlds)
        if self.signature:
            out["signature"] = self.signature
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GlossaryEntry:
        return cls(name=str(d["name"]), kind=str(d.get("kind", "")), summary=str(d.get("summary", "")),
                   worlds=tuple(str(w) for w in (d.get("worlds") or ())),
                   signature=str(d.get("signature", "")))


@dataclass(frozen=True, slots=True)
class Feature:
    """A public item and the examples grouped under it
    (`behaviour_book.group_features`), plus the rules kept for it."""

    item: str
    kind: str
    example_ids: tuple[str, ...]
    rules: tuple[Rule, ...] = ()
    # The examples the book's checker predicted with NO rule (its ablation
    # answered them right): what any reader of the code would expect, so a
    # test of one constrains a body least. Empty when the feature was not
    # asked. Recorded, never a gate.
    unaided_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "item": self.item,
            "kind": self.kind,
            "example_ids": list(self.example_ids),
            "rules": [r.to_dict() for r in self.rules],
        }
        if self.unaided_ids:
            out["unaided_ids"] = list(self.unaided_ids)
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Feature:
        return cls(
            item=str(d["item"]),
            kind=str(d.get("kind", "")),
            example_ids=tuple(str(x) for x in (d.get("example_ids") or [])),
            rules=tuple(Rule.from_dict(r) for r in (d.get("rules") or [])),
            unaided_ids=tuple(str(x) for x in (d.get("unaided_ids") or [])),
        )


@dataclass(frozen=True, slots=True)
class BehaviourSuite:
    """Every example for one cfg-cluster (nightly ≠ stable — Hard Rule 8), the
    test-file helpers they call (stored once, keyed `file::name`), and the
    behaviour book built over them: features, their rules, the glossary.

    ``specs`` as constructed or loaded carry their ``helpers`` and ``rules``
    resolved from the suite's tables (:meth:`resolved`), so a reader holding
    only the specs has both."""

    cfg_cluster: str
    specs: tuple[BehaviourSpec, ...] = field(default_factory=tuple)
    helpers: tuple[tuple[str, str], ...] = ()
    features: tuple[Feature, ...] = ()
    glossary: tuple[GlossaryEntry, ...] = ()
    format_version: int = SUITE_FORMAT
    # The build worlds the book was built for, together (`world_book`); empty
    # for a book built over this cluster alone. A book whose worlds are not
    # the workspace's is not the crate's one specification: run
    # `72627-751534fe` (2026-09-30) left two per-cluster books over 227
    # identical examples, 2 rules kept in one and 3 in the other.
    book_worlds: tuple[str, ...] = ()
    # `root key → phrase`: one model-written sentence per root an example is
    # given (:func:`example_given`), drafted at the bake from the test
    # file's own text. Nothing checks that a phrase is true (a sentence
    # about a fixture cannot be replayed), so it is shown beside the pointer
    # in the generated view and sent in no prompt. A book written before the
    # table existed has none and renders the identifier alone.
    givens: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # The table is the union of what was handed in and what the specs
        # carry: a suite rebuilt from specs alone (`BehaviourSuite(c, specs)`)
        # must not lose the helper text on save.
        table = {k: t for s in self.specs for k, t in zip(s.helper_keys, s.helpers, strict=False)}
        table.update(dict(self.helpers))
        object.__setattr__(self, "helpers", tuple(sorted(table.items())))
        object.__setattr__(self, "specs", _resolve(self.specs, table, self.features))

    @property
    def positive_oracle_specs(self) -> tuple[BehaviourSpec, ...]:
        return tuple(s for s in self.specs if s.is_positive_oracle)

    @property
    def rules(self) -> tuple[Rule, ...]:
        return tuple(r for f in self.features for r in f.rules)

    @property
    def is_legacy(self) -> bool:
        return self.format_version < SUITE_FORMAT

    def with_specs(self, specs: Iterable[BehaviourSpec]) -> BehaviourSuite:
        """The same suite over `specs` (helpers, book and format kept)."""
        return replace(self, specs=tuple(specs))

    def to_dict(self) -> dict[str, Any]:
        used = {k for s in self.specs for k in s.helper_keys}
        return {
            "format": self.format_version,
            "cfg_cluster": self.cfg_cluster,
            "specs": [s.to_dict() for s in self.specs],
            "helpers": {k: v for k, v in sorted(self.helpers) if k in used},
            "features": [f.to_dict() for f in self.features],
            "glossary": [g.to_dict() for g in self.glossary],
            **({"book_worlds": list(self.book_worlds)} if self.book_worlds else {}),
            **({"givens": {k: v for k, v in sorted(self.givens) if k in used}}
               if any(k in used for k, _ in self.givens) else {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BehaviourSuite:
        version = int(d.get("format", 1) or 1)
        legacy = version < SUITE_FORMAT
        raw_helpers = d.get("helpers") or {}
        return cls(
            cfg_cluster=str(d.get("cfg_cluster", "default")),
            specs=tuple(BehaviourSpec.from_dict(x, legacy=legacy) for x in (d.get("specs") or [])),
            helpers=tuple(sorted((str(k), str(v)) for k, v in raw_helpers.items())),
            features=tuple(Feature.from_dict(f) for f in (d.get("features") or [])),
            glossary=tuple(GlossaryEntry.from_dict(g) for g in (d.get("glossary") or [])),
            format_version=version,
            book_worlds=tuple(str(w) for w in (d.get("book_worlds") or ())),
            givens=tuple(sorted((str(k), str(v)) for k, v in (d.get("givens") or {}).items())),
        )


def _resolve(
    specs: Sequence[BehaviourSpec], helpers: dict[str, str], features: Sequence[Feature],
) -> tuple[BehaviourSpec, ...]:
    """`specs` with `helpers` (text of their helper keys), `rules` (the
    kept rules citing them — the book's word, so a suite without features
    attaches none) and `features` (the features listing them) attached.
    Idempotent."""
    by_example: dict[str, list[Rule]] = {}
    feats_of: dict[str, list[str]] = {}
    for f in features:
        for r in f.rules:
            for eid in r.example_ids:
                by_example.setdefault(eid, []).append(r)
        for eid in f.example_ids:
            feats_of.setdefault(eid, []).append(f.item)
    out: list[BehaviourSpec] = []
    for s in specs:
        # One text per key, so `helper_items` pairs each key with its own
        # text: a key the table lacks holds an empty text, never a shift.
        texts = (tuple(helpers.get(k, "") for k in s.helper_keys)
                 if any(k in helpers for k in s.helper_keys) else s.helpers)
        rules = tuple(by_example.get(s.behaviour_id, ()))
        feats = tuple(dict.fromkeys(feats_of.get(s.behaviour_id, ())))
        if texts == s.helpers and rules == s.rules and feats == s.features:
            out.append(s)
        else:
            out.append(replace(s, helpers=texts, rules=rules, features=feats))
    return tuple(out)


PROBE_ID_PREFIX = "probe::"


SUITE_FILE_PREFIX = "_behaviours_"
HELD_OUT_FILE_PREFIX = "_heldout_"


def suite_cluster_of(file_name: str) -> str | None:
    """The cluster a `.skeleton/` file name is the baked suite of, else
    None. The one rule for "this file is a cluster's suite": every reader
    that enumerates suites on disk asks it, so an artefact stored beside
    them under another prefix (the held-out captures) is never loaded as a
    cluster, whatever a glob would have matched."""
    if file_name.startswith(SUITE_FILE_PREFIX) and file_name.endswith(".json"):
        return file_name[len(SUITE_FILE_PREFIX):-len(".json")] or None
    return None


def held_out_file_name(cfg_cluster: str) -> str:
    """The file a cluster's held-out captures are stored in."""
    return f"{HELD_OUT_FILE_PREFIX}{cfg_cluster}.json"


def is_probe_id(behaviour_id: str) -> bool:
    """A probe row: a program t0d ran on the ORIGINAL crate, its stdout
    recorded as the Then."""
    return behaviour_id.startswith(PROBE_ID_PREFIX)


def merge_suites(existing: BehaviourSuite, incoming: BehaviourSuite) -> BehaviourSuite:
    """`existing` ∪ `incoming`, keyed by behaviour_id, EXISTING WINS on a
    collision, specs sorted by id. The rule the bake applies so it never
    overwrites a richer suite a prior phase wrote: ids are deterministic
    per source, so a same-id spec from a later harvest of the same text is the
    same spec, and a spec only the earlier writer could produce (a probe with
    a recorded stdout, an `exercises` set filled by coverage) survives.

    A legacy (format 1) `existing` is not a richer suite, it is an older
    shape: its test-assertion rows carry the whole test item as setup, and
    letting them win would keep that shape forever. Those rows are discarded
    and re-harvested, never converted. Its PROBE rows are kept: their Thens
    were printed by the original crate before strip, no later harvest can
    re-measure them, and their shape did not change. Run `273278-22529e44`
    (2026-09-29): an augmented restart's bake read the format-1 store, the
    whole-store discard dropped its four probe rows (`231 already on disk
    → 227`), and the rows were gone for good.
    The helpers tables union (same key, same test file text); the book (and
    the worlds it was built for) is the existing one when it has features,
    else the incoming one's."""
    if existing.is_legacy:
        kept = {s.behaviour_id: s for s in existing.specs
                if is_probe_id(s.behaviour_id)}
        if not kept:
            return incoming
        for s in incoming.specs:
            kept[s.behaviour_id] = s
        return replace(incoming, specs=tuple(kept[k] for k in sorted(kept)))
    by_id: dict[str, BehaviourSpec] = {s.behaviour_id: s for s in existing.specs}
    for s in incoming.specs:
        by_id.setdefault(s.behaviour_id, s)
    helpers = dict(incoming.helpers)
    helpers.update(dict(existing.helpers))
    has_book = bool(existing.features or existing.glossary)
    return BehaviourSuite(
        cfg_cluster=incoming.cfg_cluster or existing.cfg_cluster,
        specs=tuple(by_id[k] for k in sorted(by_id)),
        helpers=tuple(sorted(helpers.items())),
        features=existing.features if has_book else incoming.features,
        glossary=existing.glossary if has_book else incoming.glossary,
        book_worlds=existing.book_worlds if has_book else incoming.book_worlds,
        givens=existing.givens if has_book else incoming.givens,
    )


def union_cluster_suites(
    suites: Sequence[BehaviourSuite],
) -> tuple[BehaviourSpec, ...]:
    """Concatenate per-cluster suites for a caller that has no single cluster.

    A shared ``behaviour_id`` whose Then differs is two observables — both
    stay. Identical Then is a duplicate and is dropped. Insertion order does
    not decide a Then: the on-disk glob is lexical (``_behaviours_nightly.json``
    before ``_behaviours_stable.json``), and first-wins on id alone would
    silently take nightly's value. Computed on anyhow 2026-08-28: 37 stable /
    36 nightly IDENTITY specs, 35 shared ids, zero whose Then differs — this
    crate would not have noticed first-wins. ``merge_suites`` (existing wins,
    bake) is a different collision rule and is not this helper.
    """
    seen: set[tuple[str, tuple[str, str, bool] | None]] = set()
    out: list[BehaviourSpec] = []
    for suite in suites:
        for spec in suite.specs:
            then = spec.then
            tkey = None if then is None else (then.kind.name, then.value, then.ok)
            key = (spec.behaviour_id, tkey)
            if spec.behaviour_id and key in seen:
                continue
            if spec.behaviour_id:
                seen.add(key)
            out.append(spec)
    return tuple(out)


class BehaviourVerdict(Enum):
    """Result of running one behaviour against the reconstruction. HOLD = the
    observable matched (or the assertion ran panic-free). MISMATCH = a
    compiles-but-wrong defect a unit test did not catch — escalates to the
    behaviour's ``exercises`` producers."""

    HOLD = auto()
    MISMATCH = auto()
    # The behaviour's program did not build, panicked or timed out on the
    # reconstruction: no observable to compare. Not a mismatch (nothing was
    # printed) and not a hold.
    NOT_RUN = auto()

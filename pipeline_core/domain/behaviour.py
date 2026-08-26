"""BDD behaviour oracle — pure domain (F10).

A BEHAVIOUR is an observable Given/When/Then spec over a crate's PUBLIC API. We
harvest behaviours from the original library during the legal pre-strip window
(doc-tests, ``examples/``, integration ``tests/``) and — only for the gaps —
synthesise new ones from the public surface. Post-strip they serve two roles:
an executable oracle BEYOND unit tests (does the reconstruction reproduce the
observable outcome?) and a concrete RIGHT usage example injected into fill
prompts.

This is BEHAVIOUR extraction, never IMPLEMENTATION extraction (Hard Rule 11 /
FR-022): a behaviour records an OBSERVABLE outcome — the same legality class as
a test assertion — never a body or a private call-sequence. The ``then`` is the
RESULT of the implementation, so a behaviour can be authored/checked without
ever reading a stripped body.

Pure VOs + enums, no dict payloads, no IO — the adapters do the parsing and the
capturing; this module only names the language and enforces its invariants.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Any


class OutcomeKind(Enum):
    """The observable a behaviour pins. STRUCTURED variants are preferred over
    ``NORMALIZED_STDOUT`` (better attribution, less nondeterminism);
    ``STATIC_ASSERTION`` is the compile-time class (``size_of``/autotraits/niche)
    that F11's layout-authoring stage consumes as its layout-pinning selector.

    ``TYPE_IDENTITY`` and ``ALT_DEBUG_STRING`` were added 2026-08-26 because the
    suite the bake wrote could not express two of the three observables the
    98-tree failures pin: `Error::msg("x").is::<&str>()` (a TypeId round-trip —
    DOWNCAST_OK carries a bool but not WHICH type) and the `{:#?}` alternate
    Debug render (`EXPECTED_ALTDEBUG_*` in tests/test_fmt.rs, a different
    string from `{:?}`)."""

    DISPLAY_STRING = auto()      # `x.to_string()` / `format!("{x}")`
    DEBUG_STRING = auto()        # `format!("{x:?}")`
    CHAIN_SEQUENCE = auto()      # the `.chain()` / iterator render, element by element
    DOWNCAST_OK = auto()         # `x.downcast_ref::<T>().is_some()` → bool
    PANIC_MESSAGE = auto()       # a `should_panic` payload (message only, no location)
    RETURN_RENDERED = auto()     # a returned value rendered deterministically
    NORMALIZED_STDOUT = auto()   # fallback: captured stdout, normalized
    STATIC_ASSERTION = auto()    # compile-time: size/align/autotraits/niche
    TYPE_IDENTITY = auto()       # `x.is::<T>()` / `x.downcast::<T>()` holds (value = T)
    ALT_DEBUG_STRING = auto()    # `format!("{x:#?}")` — the alternate Debug render


class StaticAssertKind(Enum):
    """Which layout property a ``STATIC_ASSERTION`` pins. These are the pins
    F11's t1.5 MUST satisfy while it is free to author the private layout."""

    SIZE = auto()        # `size_of::<T>() == …`
    ALIGN = auto()       # `align_of::<T>() == …`
    AUTOTRAITS = auto()  # `assert_impl_all!(T: Send + Sync + Unpin + 'static)`
    NICHE = auto()       # `size_of::<Option<T>>() == size_of::<T>()` (null-ptr-opt)


# Runtime observables that require a compiling, runnable tree to check.
RUNTIME_OUTCOME_KINDS = frozenset({
    OutcomeKind.DISPLAY_STRING, OutcomeKind.DEBUG_STRING,
    OutcomeKind.CHAIN_SEQUENCE, OutcomeKind.DOWNCAST_OK,
    OutcomeKind.PANIC_MESSAGE, OutcomeKind.RETURN_RENDERED,
    OutcomeKind.NORMALIZED_STDOUT, OutcomeKind.TYPE_IDENTITY,
    OutcomeKind.ALT_DEBUG_STRING,
})

# The kinds whose Then is a rendered string the reconstruction must reproduce
# byte-for-byte (Display / Debug / alternate Debug).
FORMAT_OUTCOME_KINDS = frozenset({
    OutcomeKind.DISPLAY_STRING, OutcomeKind.DEBUG_STRING,
    OutcomeKind.ALT_DEBUG_STRING,
})

# The sentence every behaviour-rendering prompt carries: the model owns the
# representation, the behaviours own the observable. Other prompt renderers
# import it by this name — keep the name and the wording stable.
MAY_REPRESENTATION_NOTE = (
    "The layout, allocation strategy and private names are yours (MAY); "
    "every behaviour listed is a MUST."
)


@dataclass(frozen=True, slots=True)
class ObservableOutcome:
    """The Then of a behaviour — a SUM over :class:`OutcomeKind`. Exactly one
    payload shape is valid per kind (enforced in ``__post_init__``). A behaviour
    NEVER pins an internal call-graph (unobservable + needs bodies) — only the
    observable result."""

    kind: OutcomeKind
    value: str = ""                       # DISPLAY/DEBUG/PANIC/RETURN/STDOUT text; TYPE_IDENTITY type
    chain: tuple[str, ...] = ()           # CHAIN_SEQUENCE elements, in order
    ok: bool = False                      # DOWNCAST_OK / TYPE_IDENTITY
    static_kind: StaticAssertKind | None = None   # STATIC_ASSERTION only
    static_expr: str = ""                 # the asserted expression, verbatim

    def __post_init__(self) -> None:
        k = self.kind
        if k is OutcomeKind.STATIC_ASSERTION:
            if self.static_kind is None:
                raise ValueError("STATIC_ASSERTION requires static_kind")
        elif self.static_kind is not None:
            raise ValueError(f"static_kind set on non-static outcome {k.name}")
        if k is OutcomeKind.CHAIN_SEQUENCE and not self.chain:
            raise ValueError("CHAIN_SEQUENCE requires a non-empty chain")
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
        cls, static_kind: StaticAssertKind, expr: str,
    ) -> ObservableOutcome:
        return cls(OutcomeKind.STATIC_ASSERTION,
                   static_kind=static_kind, static_expr=expr)

    @property
    def is_runtime(self) -> bool:
        """True iff checking this outcome needs a running tree (F10 oracle-use
        gate; STATIC_ASSERTION is compile-time and F11-consumed instead)."""
        return self.kind in RUNTIME_OUTCOME_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.name,
            "value": self.value,
            "chain": list(self.chain),
            "ok": self.ok,
            "static_kind": self.static_kind.name if self.static_kind else None,
            "static_expr": self.static_expr,
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
        )


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

    RUN = auto()           # plain ``` — compile + run
    NO_RUN = auto()        # `no_run` — compile only, don't execute
    IGNORE = auto()        # `ignore` — skip entirely
    SHOULD_PANIC = auto()  # `should_panic` — the Then IS a panic, not a value
    COMPILE_FAIL = auto()  # `compile_fail` — asserts NON-compilation (negative)


# Fences whose behaviour is usable as a POSITIVE runtime oracle. `ignore` is
# skipped; `no_run` compiles but yields no runtime observable; `compile_fail`
# is a negative behaviour (using it as a positive oracle is backwards).
POSITIVE_RUNNABLE_FENCES = frozenset({
    FenceDirective.RUN, FenceDirective.SHOULD_PANIC,
})


class BehaviourClass(Enum):
    """WHAT KIND of promise a behaviour makes about the reconstruction — the
    axis a prompt renderer groups on and a census histograms on. Distinct from
    :class:`OutcomeKind` (HOW the observable is captured): two DISPLAY strings
    can be an exact-format promise and a bounded-resource promise.

    IDENTITY               a TypeId round-trip: what went in comes back out
                           through `is::<T>()` / `downcast::<T>()`.
    TRAIT_OBJECT_IDENTITY  the same, through `type_id()` on a trait object —
                           the vtable, not the payload, answers.
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


@dataclass(frozen=True, slots=True)
class BehaviourSpec:
    """One frozen Given/When/Then over the public API. ``exercises`` (the set of
    unit ids the behaviour touches, from return-type + construction token-scan)
    is the localizer a mismatch escalates through (#82 attribution machinery)."""

    behaviour_id: str
    given: str                              # PublicApiSetup — setup code
    when: str                               # PublicApiAction — the exercised call
    then: ObservableOutcome | None = None   # None for ASSERTION (the assert IS the Then)
    exercises: frozenset[str] = frozenset()
    provenance: BehaviourProvenance = BehaviourProvenance.HARVESTED
    subkind: BehaviourSubKind = BehaviourSubKind.ASSERTION
    fence: FenceDirective = FenceDirective.RUN
    cfg_cluster: str = "default"            # nightly backtrace behaviour ≠ stable
    source_ref: str = ""                    # where harvested (file:line / example name)
    klass: BehaviourClass = BehaviourClass.OTHER   # see classify_behaviour

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
        return {
            "behaviour_id": self.behaviour_id,
            "given": self.given,
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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BehaviourSpec:
        return cls(
            behaviour_id=str(d["behaviour_id"]),
            given=str(d.get("given", "")),
            when=str(d.get("when", "")),
            then=(ObservableOutcome.from_dict(d["then"])
                  if d.get("then") is not None else None),
            exercises=frozenset(str(x) for x in (d.get("exercises") or [])),
            provenance=BehaviourProvenance[str(d.get("provenance", "HARVESTED"))],
            subkind=BehaviourSubKind[str(d.get("subkind", "ASSERTION"))],
            fence=FenceDirective[str(d.get("fence", "RUN"))],
            cfg_cluster=str(d.get("cfg_cluster", "default")),
            source_ref=str(d.get("source_ref", "")),
            klass=BehaviourClass[str(d.get("klass", "OTHER"))],
        )


# --- classification ---------------------------------------------------------

# A resource boundary is a NUMBER next to one of these tokens on the same line
# (`s.len() == 3`, `"x".repeat(1024)`, `&buf[..16]`, `Vec::with_capacity(8)`).
_RESOURCE_TOKENS = ("len(", "repeat(", "[..", "capacity")
# ... or a COMMENT that names a limit.
_LIMIT_WORDS = ("limit", "maximum", "max ", "bound", "capacity", "overflow",
                "truncat", "at most", "no more than")
_OPTION_WHEN_MARKERS = (".is_none()", ".is_some()")
_CFG_GATE_MARKERS = ("#[cfg(", "cfg!(", "#[rustversion", "rustversion::cfg!")


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
        comment = "" if comment_at < 0 else line[comment_at + 2:].lower()
        if comment and any(w in comment for w in _LIMIT_WORDS):
            return True
        if any(t in code for t in _RESOURCE_TOKENS) and _line_has_numeric_literal(code):
            return True
    return False


def classify_behaviour(
    spec: BehaviourSpec, *, default_cluster: str = "default",
) -> BehaviourClass:
    """The :class:`BehaviourClass` of `spec`, from its kind and its text. Pure;
    never raises; OTHER when nothing matches. Precedence, first hit wins:

    1. STATIC_ASSERTION                       → LAYOUT_PIN
    2. TYPE_IDENTITY                          → TRAIT_OBJECT_IDENTITY when the
       `when` mentions `type_id()`, else IDENTITY
    3. `when` has `.is_none()`/`.is_some()`, or the Then value is `None` /
       starts with `Some(`                    → OPTION_SEMANTICS
    4. DISPLAY / DEBUG / ALT_DEBUG            → BOUNDED_RESOURCE when the test
       item text (`given`) carries a resource boundary
       (:func:`has_resource_boundary`), else EXACT_FORMAT
    5. cfg_cluster ≠ `default_cluster` AND the given/when text carries a cfg
       gate (`#[cfg(`, `cfg!(`, `#[rustversion`) → CFG_WORLD
    6. otherwise OTHER.

    `default_cluster` is the NAME of the crate's default cluster (anyhow's is
    `stable`, not `default`); the caller that knows the cluster manifest
    passes it, so a stable-only spec is never read as another world."""
    then = spec.then
    if then is not None and then.kind is OutcomeKind.STATIC_ASSERTION:
        return BehaviourClass.LAYOUT_PIN
    if then is not None and then.kind is OutcomeKind.TYPE_IDENTITY:
        if "type_id()" in spec.when:
            return BehaviourClass.TRAIT_OBJECT_IDENTITY
        return BehaviourClass.IDENTITY
    then_value = then.value.strip() if then is not None else ""
    if any(m in spec.when for m in _OPTION_WHEN_MARKERS) or (
            then is not None and (then_value == "None" or then_value.startswith("Some("))):
        return BehaviourClass.OPTION_SEMANTICS
    if then is not None and then.kind in FORMAT_OUTCOME_KINDS:
        if has_resource_boundary(spec.given):
            return BehaviourClass.BOUNDED_RESOURCE
        return BehaviourClass.EXACT_FORMAT
    if spec.cfg_cluster != default_cluster:
        text = spec.given + "\n" + spec.when
        if any(m in text for m in _CFG_GATE_MARKERS):
            return BehaviourClass.CFG_WORLD
    return BehaviourClass.OTHER


def classified(spec: BehaviourSpec, *, default_cluster: str = "default") -> BehaviourSpec:
    """`spec` with its `klass` set by :func:`classify_behaviour`."""
    return replace(spec, klass=classify_behaviour(spec, default_cluster=default_cluster))


@dataclass(frozen=True, slots=True)
class BehaviourSuite:
    """Every behaviour for one cfg-cluster (nightly ≠ stable — Hard Rule 8)."""

    cfg_cluster: str
    specs: tuple[BehaviourSpec, ...] = field(default_factory=tuple)

    @property
    def positive_oracle_specs(self) -> tuple[BehaviourSpec, ...]:
        return tuple(s for s in self.specs if s.is_positive_oracle)

    def to_dict(self) -> dict[str, Any]:
        return {"cfg_cluster": self.cfg_cluster,
                "specs": [s.to_dict() for s in self.specs]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BehaviourSuite:
        return cls(
            cfg_cluster=str(d.get("cfg_cluster", "default")),
            specs=tuple(BehaviourSpec.from_dict(x)
                        for x in (d.get("specs") or [])),
        )


def merge_suites(existing: BehaviourSuite, incoming: BehaviourSuite) -> BehaviourSuite:
    """`existing` ∪ `incoming`, keyed by behaviour_id, EXISTING WINS on a
    collision, specs sorted by id. The rule the bake applies so it never
    overwrites a richer suite a prior phase (t0d) wrote: ids are deterministic
    per source, so a same-id spec from a later harvest of the same text is the
    same spec, and a spec only the earlier writer could produce (a probe with
    a recorded stdout, an `exercises` set filled by coverage) survives."""
    by_id: dict[str, BehaviourSpec] = {s.behaviour_id: s for s in existing.specs}
    for s in incoming.specs:
        by_id.setdefault(s.behaviour_id, s)
    return BehaviourSuite(cfg_cluster=incoming.cfg_cluster or existing.cfg_cluster,
                          specs=tuple(by_id[k] for k in sorted(by_id)))


class BehaviourVerdict(Enum):
    """Result of running one behaviour against the reconstruction. HOLD = the
    observable matched (or the assertion ran panic-free). MISMATCH = a
    compiles-but-wrong defect a unit test did not catch — escalates to the
    behaviour's ``exercises`` producers."""

    HOLD = auto()
    MISMATCH = auto()

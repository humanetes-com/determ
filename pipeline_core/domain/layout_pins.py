"""Layout-pinning assertion classifier — the shared primitive for F10 and F11.

Given a test / doc-test body it names the COMPILE-TIME layout facts the code
pins: ``size_of``/``align_of`` equalities, autotrait bounds (``assert_impl_all!``
and ``fn _assert() where T: Send + Sync``), and the null-pointer-optimization
niche (``size_of::<Option<T>>() == size_of::<T>()``). These are the pins F11's
t1.5 layout-authoring stage MUST satisfy while it is otherwise free to invent
the private layout, and the ``STATIC_ASSERTION`` behaviours F10 harvests.

Token-based, whole-word scanning only (Hard Rule: no regex). Pure domain — the
adapter reads the file, this classifies the text.
"""
from __future__ import annotations

from pipeline_core.domain.behaviour import (
    BehaviourProvenance,
    BehaviourSpec,
    BehaviourSubKind,
    ExampleOrigin,
    FenceDirective,
    ObservableOutcome,
    OriginKind,
    StaticAssertKind,
)
from pipeline_core.domain.rust_scan import (
    contains_word,
    count_word,
    fn_name_on_line,
)

# The autotrait markers a layout-pinning test uses to freeze Send/Sync/Unpin/…
_AUTOTRAIT_MACROS = ("assert_impl_all", "assert_not_impl_all", "assert_impl_any")
_AUTOTRAIT_BOUNDS = ("Send", "Sync", "Unpin")


def _nonempty_lines_with(text: str, word: str) -> list[str]:
    """The stripped source lines on which whole-word `word` appears."""
    return [ln.strip() for ln in text.splitlines() if contains_word(ln, word)]


def classify_layout_assertions(test_src: str) -> tuple[ObservableOutcome, ...]:
    """Every layout-pinning STATIC_ASSERTION observable in `test_src`, deduped
    by (kind, expr). Order: SIZE/NICHE, ALIGN, AUTOTRAITS. A NICHE pin (an
    ``Option<T>`` sized equal to ``T``) is reported as NICHE, not SIZE."""
    seen: set[tuple[StaticAssertKind, str]] = set()
    out: list[ObservableOutcome] = []

    def _add(kind: StaticAssertKind, expr: str) -> None:
        key = (kind, expr)
        if key not in seen:
            seen.add(key)
            out.append(ObservableOutcome.static_assertion(kind, expr))

    for ln in _nonempty_lines_with(test_src, "size_of"):
        # NPO niche: the SAME line mentions Option AND has two size_of calls
        # (size_of::<Option<T>>() == size_of::<T>()).
        if contains_word(ln, "Option") and count_word(ln, "size_of") >= 2:
            _add(StaticAssertKind.NICHE, ln)
        else:
            _add(StaticAssertKind.SIZE, ln)
    for ln in _nonempty_lines_with(test_src, "align_of"):
        _add(StaticAssertKind.ALIGN, ln)
    # Autotrait pins: a static-assert macro, or an explicit `where T: Send…` in
    # a `fn _assert_*` helper. Report the whole line as the pinned expression.
    for macro in _AUTOTRAIT_MACROS:
        for ln in _nonempty_lines_with(test_src, macro):
            _add(StaticAssertKind.AUTOTRAITS, ln)
    for ln in test_src.splitlines():
        if is_autotrait_assertion(ln):
            _add(StaticAssertKind.AUTOTRAITS, autotrait_pin_expr(test_src, ln))
    return tuple(out)


def autotrait_pin_expr(test_src: str, decl_line: str) -> str:
    """An autotrait pin's expression: the generic helper's declaration PLUS the
    turbofish calls that apply it, or the declaration alone when it is applied
    nowhere.

    The subject has to be in here or the pin reaches no component. Everything
    that routes a pin reads type names off this string — `_spec_pins_types` in
    the t1.5 pin selector, `pinned_type_names` in the M13 harvest gate — and the
    idiom the crate's own tests use is TWO statements:

        fn assert_send<T: Send>() {}
        assert_send::<Error>();

    Read from the declaration alone the pin is a bound with no subject, so
    `camelcase_type_names` yielded `{Send}` and the selector matched it against
    no component's items and no frozen-public name. Measured on `anyhow`
    2026-08-06: all four autotrait pins (three in tests/test_autotrait.rs, one
    in tests/test_repr.rs) selected into zero components, while the two
    `size_of::<Error>()` pins in the same file routed correctly — the log's
    `[2 layout pin(s) verified]` on a component of 34 items. t1.5 then authored
    `pub struct Error { inner: NonNull<ErrorImpl> }`, which satisfies both size
    pins and is neither `Send` nor `Sync`, and every test binary in both
    clusters failed to build on `E0277 NonNull<ErrorImpl> cannot be sent
    between threads safely`.

    The rendered probe still takes the type it applies from the component's own
    surface (`build_layout_probe(pinned_types=…)`) — the calls are carried here
    so the pin can be ROUTED to that component in the first place."""
    decl = decl_line.strip()
    helper = fn_name_on_line(decl)
    if not helper:
        return decl
    subjects = autotrait_subject_types(test_src, helper)
    if not subjects:
        return decl
    calls = " ".join(f"{helper}::<{s}>();" for s in subjects)
    return f"{decl} {calls}"


def autotrait_subject_types(test_src: str, helper: str) -> list[str]:
    """The types `helper` is instantiated with anywhere in `test_src`, in source
    order and deduped — ``assert_send::<Error>();`` → ``["Error"]``.

    Whole-word on the helper name so `assert` never matches the `assert_eq` of
    a neighbouring size pin, and the argument list is read by bracket depth so
    a nested generic (``assert::<Result<(), Error>>()``) stays one type."""
    out: list[str] = []
    i = 0
    while (at := test_src.find(helper, i)) >= 0:
        i = at + len(helper)
        before = test_src[at - 1] if at else ""
        if before and (before.isalnum() or before == "_"):
            continue
        j = i
        while j < len(test_src) and test_src[j].isspace():
            j += 1
        if not test_src.startswith("::", j):
            continue
        j += 2
        while j < len(test_src) and test_src[j].isspace():
            j += 1
        end = _balanced_angle(test_src, j)
        if end < 0:
            continue
        for arg in _split_top_level_commas(test_src[j + 1:end - 1]):
            if (ty := arg.strip()) and ty not in out:
                out.append(ty)
    return out


def is_autotrait_assertion(line: str) -> bool:
    """True iff `line` ASSERTS an autotrait bound, rather than merely mentioning
    one.

    The pinning idiom is a generic helper the test instantiates —
    ``fn assert<T: Send + Sync>() {}`` or ``fn _a() where Error: Send`` — so the
    line must be a `fn` declaration carrying the bound. Accepting any line with
    an autotrait word and a colon swept up ordinary code: a type annotation
    (``let e: Box<dyn StdError + Send + Sync> = x.into();``), a parameter list
    (``fn from_boxed(e: Box<dyn StdError + Send>) -> Self``) and a turbofish
    (``.downcast_ref::<Box<dyn StdError + Send>>()``) all matched, and every one
    of them then reached the t1.5 authoring prompt as a layout constraint the
    model was told to satisfy."""
    s = line.strip()
    if "size_of" in s or not contains_word(s, "fn"):
        return False
    if not any(contains_word(s, b) for b in _AUTOTRAIT_BOUNDS):
        return False
    if contains_word(s, "where"):
        return True
    return autotrait_bound_list(s) is not None


def autotrait_bound_list(line: str) -> str | None:
    """The bounds of a single-parameter generic helper — ``fn assert<E: Unpin +
    Send>() {}`` → ``Unpin + Send``; None when the line has no such parameter.

    Structural (bracket-depth) scan, never a text match: the bound list may
    itself contain generics (``T: AsRef<[u8]> + Send``) and the parameter list
    may sit anywhere on the line."""
    at = line.find("<")
    if at < 0:
        return None
    end = _balanced_angle(line, at)
    if end < 0:
        return None
    params = _split_top_level_commas(line[at + 1:end - 1])
    if len(params) != 1 or ":" not in params[0]:
        return None
    return params[0].split(":", 1)[1].strip() or None


def _balanced_angle(text: str, start: int) -> int:
    """Index just past the ``<``…``>`` group at `start`, or -1 if unbalanced."""
    if start >= len(text) or text[start] != "<":
        return -1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "<":
            depth += 1
        elif text[i] == ">":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _split_top_level_commas(text: str) -> list[str]:
    """Split on commas at bracket depth 0 (``T: AsRef<[u8], u8>`` is ONE param)."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in text:
        if c in "(<[":
            depth += 1
        elif c in ")>]":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(c)
    out.append("".join(cur))
    return [s.strip() for s in out]


def is_layout_pinning(test_src: str) -> bool:
    """True iff the test pins at least one compile-time layout fact — the F11
    layout-pinning-test selector. Such tests MUST be fed to the t1.5 authoring
    prompt so the model's private layout satisfies them by construction."""
    return bool(classify_layout_assertions(test_src))


def harvest_layout_pin_behaviours(
    test_src: str, *, source_file: str, cfg_cluster: str = "default",
) -> list[BehaviourSpec]:
    """STATIC_ASSERTION behaviours from a whole integration/unit test FILE.

    The layout pins F11's t1.5 must satisfy (``size_of::<Error>()==usize``, NPO,
    ``Send+Sync+Unpin``) live in ``tests/*.rs`` function bodies, NOT in doc-test
    fences — so the doc-test harvester never sees them. This lifts them straight
    from the test source (the whole file is the ``test_src``), the same legality
    class as any harvested assertion (an observable layout fact, never a body).
    ``exercises`` is left empty; the caller fills it from a type-name scan."""
    out: list[BehaviourSpec] = []
    for i, outcome in enumerate(classify_layout_assertions(test_src)):
        out.append(BehaviourSpec(
            behaviour_id=f"static::{source_file}::{i}",
            setup="", when=outcome.static_expr, then=outcome,
            provenance=BehaviourProvenance.HARVESTED,
            subkind=BehaviourSubKind.OUTCOME, fence=FenceDirective.RUN,
            cfg_cluster=cfg_cluster, source_ref=source_file,
            origin=ExampleOrigin(OriginKind.STATIC_PIN, source_file, "", i)))
    return out


def pinned_type_names(specs: tuple[BehaviourSpec, ...]) -> frozenset[str]:
    """The type names any STATIC_ASSERTION in `specs` pins (CamelCase idents in
    the asserted expression). A private type whose name is in here but which
    reaches t1.5 with NO pin fed to the prompt is a HARVEST GAP, not a free
    layout — the caller (M13 gate) refuses to author it blind."""
    from pipeline_core.domain.behaviour import OutcomeKind
    from pipeline_core.domain.rust_idents import camelcase_type_names

    names: set[str] = set()
    for s in specs:
        if s.then is not None and s.then.kind is OutcomeKind.STATIC_ASSERTION:
            names |= camelcase_type_names(s.then.static_expr)
    return frozenset(names)

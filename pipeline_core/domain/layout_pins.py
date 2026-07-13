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

from pipeline_core.domain.behaviour import ObservableOutcome, StaticAssertKind
from pipeline_core.domain.rust_scan import contains_word, count_word

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
    if contains_word(test_src, "where") or contains_word(test_src, "fn"):
        for ln in test_src.splitlines():
            s = ln.strip()
            if any(contains_word(s, b) for b in _AUTOTRAIT_BOUNDS) and (
                    contains_word(s, "where") or ":" in s) and "size_of" not in s:
                _add(StaticAssertKind.AUTOTRAITS, s)
    return tuple(out)


def is_layout_pinning(test_src: str) -> bool:
    """True iff the test pins at least one compile-time layout fact — the F11
    layout-pinning-test selector. Such tests MUST be fed to the t1.5 authoring
    prompt so the model's private layout satisfies them by construction."""
    return bool(classify_layout_assertions(test_src))

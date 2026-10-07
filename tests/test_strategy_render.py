"""The computed part of `coverage_strategy.md`, and the store that renders it.

The model text below is anyhow's stored strategy file (t0b, 2026-08-09), cut
to the sections the renderer reads. It lists `MessageError`, `ContextError`
and `BoxedError` with a construction (`BoxedError: Box::<dyn StdError + Send +
Sync>::from(err)` is wrong: the type is internal and built by
`Error::from_boxed`) and has no `DisplayError` line. The facts are the ones
`domain.fn_routes` computes on the original crate for those four types.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline_core.adapters.strategy_facts import StrategyFacts
from pipeline_core.application.tasks.fn_criterion import WorldRead
from pipeline_core.domain.fn_routes import ROUTES_VERSION, PublicRoute, TypeFact, WorldFacts, facts_to_json
from pipeline_core.domain.strategy_render import (
    BEGIN,
    END,
    computed_hash,
    drop_contradicted,
    render_strategy,
    strip_computed,
)
from pipeline_core.domain.workspace_layout import (
    CONSTRUCTION_FACTS_NAME,
    COVERAGE_STRATEGY_HASH_NAME,
    COVERAGE_STRATEGY_NAME,
)

_MODEL = """IMPORTS:
  anyhow: use anyhow::{anyhow, bail, ensure, Chain, Context, Error, Result as AnyhowResult};
  wrong: use anyhow::DisplayError;
  NEVER: crate::kind # internal tagged dispatch traits
  NEVER: crate::ptr # internal pointer wrappers (Own, Ref, Mut)

TYPES:
  Error: anyhow!("msg")|Error::msg("msg")|err.context("ctx")  # narrow pointer
  Chain: error.chain()|Chain::default()  # DoubleEndedIterator
  BoxedError: Box::<dyn StdError + Send + Sync>::from(err)  # opaque boxed error
  ContextError: internal, created via .context()  # wraps context + source
  MessageError: internal wrapper for Error::msg  # requires Display + Debug

PITFALLS:
  MessageError and DisplayError are internal → reach them through the public API
"""


def _route(public: str, via: str, ty: str) -> PublicRoute:
    return PublicRoute(f"`{public}`", (f"`{via}`",), f"builds `{ty}`")


_STABLE = WorldFacts("stable", unnameable=(
    TypeFact("ContextError", "src/context.rs", (_route("Error::context", "Error::construct_from_context", "ContextError"),)),
    TypeFact("BoxedError", "src/wrapper.rs", (_route("Error::from_boxed", "Error::construct_from_boxed", "BoxedError"),)),
    TypeFact("DisplayError", "src/wrapper.rs", (_route("<Option<T> as Context<T, Infallible>>::context",
                                                       "Error::construct_from_display", "DisplayError"),)),
    TypeFact("MessageError", "src/wrapper.rs", (_route("Error::msg", "Error::construct_from_adhoc", "MessageError"),)),
))
_NIGHTLY = WorldFacts("nightly", unnameable=_STABLE.unnameable + (TypeFact("Request", "src/nightly.rs"),))


def test_the_computed_part_comes_first_and_states_every_unnameable_type() -> None:
    out, _ = render_strategy(_MODEL, [_STABLE, _NIGHTLY])
    assert out.startswith(BEGIN)
    block = out.split(END, 1)[0]
    assert ("DisplayError: not nameable from tests/; built by `<Option<T> as Context<T, Infallible>>::context`"
            " → `Error::construct_from_display` (builds `DisplayError`)") in block
    assert "NEVER: DisplayError  # pub in a private module of src/wrapper.rs" in block
    # A type only one world leaves unnameable says which.
    assert "Request: not nameable from tests/; no public call" in block and "[worlds: nightly]" in block
    assert "[worlds:" not in block.split("Request")[0]


def test_the_model_lines_it_contradicts_are_dropped_and_the_rest_kept() -> None:
    out, dropped = render_strategy(_MODEL, [_STABLE])
    model = out.split(END, 1)[1]
    for gone in ("use anyhow::DisplayError", "BoxedError: Box::<dyn", "ContextError: internal",
                 "MessageError: internal wrapper"):
        assert gone not in model, gone
        assert any(gone in d for d in dropped), gone
    for kept in ("use anyhow::{anyhow, bail, ensure, Chain, Context, Error",
                 "Chain: error.chain()", "Error: anyhow!(\"msg\")", "NEVER: crate::kind",
                 # A PITFALLS line names the types without claiming anything the
                 # computed part states: it stays.
                 "MessageError and DisplayError are internal"):
        assert kept in model, kept


def test_negative_control_no_facts_leaves_the_text_unchanged() -> None:
    assert render_strategy(_MODEL, []) == (_MODEL, [])
    assert drop_contradicted(_MODEL, frozenset()) == (_MODEL, [])


def test_rendering_twice_is_rendering_once() -> None:
    once, _ = render_strategy(_MODEL, [_STABLE])
    twice, dropped = render_strategy(once, [_STABLE])
    assert twice == once and dropped == []
    assert strip_computed(once).count(BEGIN) == 0


def test_the_hash_moves_with_the_facts() -> None:
    assert computed_hash([_STABLE]) == computed_hash([_STABLE])
    assert computed_hash([_STABLE]) != computed_hash([_STABLE, _NIGHTLY])


# --- the store ---------------------------------------------------------------

class _Reader:
    def __init__(self, facts: WorldFacts | None) -> None:
        self.facts = facts
        self.calls = 0

    def __call__(self, project_dir: Path, cluster: Any) -> WorldRead:
        self.calls += 1
        return WorldRead({}, self.facts, "" if self.facts else "no reading")


class _Cluster:
    name = "stable"


def _store(reader: _Reader, logs: list[str]) -> StrategyFacts:
    return StrategyFacts(read_world=reader, log=logs.append)


def test_an_original_tree_with_no_stored_facts_is_measured_once(tmp_path: Path) -> None:
    reader, logs = _Reader(_STABLE), list[str]()
    store = _store(reader, logs)
    assert store.facts(tmp_path, [_Cluster()], may_measure=True) == (_STABLE,)
    assert (tmp_path / CONSTRUCTION_FACTS_NAME).exists()
    assert store.facts(tmp_path, [_Cluster()], may_measure=True) == (_STABLE,)
    assert reader.calls == 1


def test_a_stripped_tree_is_never_measured(tmp_path: Path) -> None:
    reader, logs = _Reader(_STABLE), list[str]()
    store = _store(reader, logs)
    assert store.facts(tmp_path, [_Cluster()], may_measure=False) == ()
    assert reader.calls == 0
    (tmp_path / CONSTRUCTION_FACTS_NAME).write_text(
        facts_to_json([_STABLE]).replace(f'"version": "{ROUTES_VERSION}"', '"version": "0"'),
        encoding="utf-8")
    assert store.facts(tmp_path, [_Cluster()], may_measure=False) == (_STABLE,)
    assert reader.calls == 0
    assert any("not '" in ln and "using them as stored" in ln for ln in logs)
    # Control: the same stale file on an original tree is re-measured.
    assert store.facts(tmp_path, [_Cluster()], may_measure=True) == (_STABLE,)
    assert reader.calls == 1


def test_a_source_edit_under_the_same_version_is_remeasured(tmp_path: Path) -> None:
    """The review remainder: a stored file whose version matched was reused
    after `src/` changed. On an original tree it is remeasured when the
    source fingerprint it recorded is not the tree's; a stripped tree still
    uses what is stored."""
    reader, logs = _Reader(_STABLE), list[str]()
    store = _store(reader, logs)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn a() {}\n", encoding="utf-8")
    assert store.facts(tmp_path, [_Cluster()], may_measure=True) == (_STABLE,)
    assert reader.calls == 1
    assert store.facts(tmp_path, [_Cluster()], may_measure=True) == (_STABLE,)
    assert reader.calls == 1
    (tmp_path / "src" / "lib.rs").write_text("pub fn a() { b() }\n", encoding="utf-8")
    assert store.facts(tmp_path, [_Cluster()], may_measure=False) == (_STABLE,)
    assert reader.calls == 1
    assert store.facts(tmp_path, [_Cluster()], may_measure=True) == (_STABLE,)
    assert reader.calls == 2
    assert any("remeasuring" in ln for ln in logs)


def test_rendered_writes_the_file_and_hash_only_when_they_differ(tmp_path: Path) -> None:
    logs: list[str] = []
    store = _store(_Reader(None), logs)
    (tmp_path / COVERAGE_STRATEGY_NAME).write_text(_MODEL, encoding="utf-8")
    out = store.rendered(tmp_path, _MODEL.strip(), [_STABLE])
    assert out.startswith(BEGIN)
    assert (tmp_path / COVERAGE_STRATEGY_NAME).read_text(encoding="utf-8").strip() == out
    assert (tmp_path / COVERAGE_STRATEGY_HASH_NAME).read_text(encoding="utf-8").strip() == computed_hash([_STABLE])
    assert len(logs) == 1
    # The loaded text is now the rendered one: rendering it again writes nothing.
    assert store.rendered(tmp_path, out, [_STABLE]) == out
    assert len(logs) == 1
    # No facts: the text passes through and nothing is written.
    assert store.rendered(tmp_path, "x", []) == "x"

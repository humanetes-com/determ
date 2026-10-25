"""What a fill is shown of a type, and told about the cfg worlds, when the run
has more than one world.

Run `292191-d60a5308` (2026-10-02): the `stable` fills were shown
`ErrorVTable.object_backtrace` with no gate, were told the other world's items
"DO NOT EXIST", and wrote six constructors that initialise it unguarded; none
built in `nightly`. With the world plan in hand the projection keeps every
member some world compiles and marks the gated ones, the renderer prints the
gate, and the invariants section describes every world. Invented records.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.cluster_membership import (
    cluster_filter_type_members,
)
from pipeline_core.domain.cluster_plan import fill_invariants_section
from pipeline_core.domain.models import (
    BuildCluster,
    FieldRecord,
    MethodRecord,
    TypeRecord,
)
from pipeline_core.prompts.sections import format_type_for_prompt

_BASE = BuildCluster(name="base", env_extra={}, tarpaulin_exclude_files=[],
                     excluded_cfg=["alt"], active_cfg=[], prompt_nightly_rule="",
                     is_default=True)
_ALT = BuildCluster(name="alt", env_extra={"RUSTFLAGS": "--cfg alt"},
                    tarpaulin_exclude_files=[], excluded_cfg=[], active_cfg=["alt"],
                    prompt_nightly_rule="", is_default=False)
_PLAN = [_BASE, _ALT]

_TABLE = TypeRecord(
    name="Table", kind="struct", file="src/table.rs",
    definition="pub(crate) struct Table { open: fn(), trace: fn(), probe: fn() }",
    fields=(
        FieldRecord(name="open", ty="fn()"),
        FieldRecord(name="trace", ty="fn()", cfg_predicates=("not (alt)",)),
        FieldRecord(name="probe", ty="fn()", cfg_predicates=("alt",)),
        FieldRecord(name="ghost", ty="fn()", cfg_predicates=("never_set",)),
    ),
    methods=(
        MethodRecord(name="len", sig="fn len(&self) -> usize"),
        MethodRecord(name="provide", sig="fn provide(&self)", cfg_predicates=("alt",)),
    ))


def _fields(rec: TypeRecord) -> dict[str, tuple[str, ...]]:
    return {f.name: f.worlds for f in rec.fields}


def test_every_world_keeps_and_marks_what_only_some_worlds_compile() -> None:
    """The plan's sentence: a fill in one world sees the member that exists
    only here AND the one that exists only elsewhere, each with its worlds;
    a member no world compiles is gone; an ungated one carries no mark."""
    plan = [dataclass_with(c, excluded=["never_set"]) for c in _PLAN]
    [base] = cluster_filter_type_members([_TABLE], plan[0], plan)
    assert _fields(base) == {"open": (), "trace": ("base",), "probe": ("alt",)}
    assert {m.name: m.worlds for m in base.methods} == {"len": (), "provide": ("alt",)}
    # ...and the other world's fill is shown the same members, same marks.
    [alt] = cluster_filter_type_members([_TABLE], plan[1], plan)
    assert _fields(alt) == _fields(base)


def dataclass_with(c: BuildCluster, *, excluded: list[str]) -> BuildCluster:
    import dataclasses
    return dataclasses.replace(c, excluded_cfg=[*c.excluded_cfg, *excluded])


def test_one_world_or_no_plan_is_the_per_cluster_projection_unchanged() -> None:
    """NEGATIVE CONTROL and identity: without the plan the other world's
    members are dropped and nothing is marked — what every fill was shown."""
    [base] = cluster_filter_type_members([_TABLE], _BASE)
    assert _fields(base) == {"open": (), "trace": (), "ghost": ()}
    assert [m.name for m in base.methods] == ["len"]
    assert cluster_filter_type_members([_TABLE], _BASE, [_BASE]) == \
        cluster_filter_type_members([_TABLE], _BASE)
    assert cluster_filter_type_members([_TABLE], None, _PLAN) == [_TABLE]


def test_the_rendering_prints_the_gate_on_the_field_the_literal_and_the_method() -> None:
    [base] = cluster_filter_type_members([_TABLE], _BASE, _PLAN)
    text = format_type_for_prompt(base)
    assert "    #[cfg(not(alt))] trace: fn()   // exists ONLY in cfg world `base`" in text
    assert "    #[cfg(alt)] probe: fn()   // exists ONLY in cfg world `alt`" in text
    assert "    open: fn()\n" in text
    assert ("construct: Table { open: <fn()>, #[cfg(not(alt))] trace: <fn()>, "
            "#[cfg(alt)] probe: <fn()>") in text
    assert "keep each `#[cfg(…)]` on its field" in text
    assert "    #[cfg(alt)] fn provide(&self)   // exists ONLY in cfg world `alt`" in text


def test_an_unmarked_record_renders_exactly_as_before() -> None:
    [base] = cluster_filter_type_members([_TABLE], _BASE)
    text = format_type_for_prompt(base)
    assert "#[cfg(" not in text and "exists ONLY" not in text
    assert "construct: Table { open: <fn()>, trace: <fn()>, ghost: <fn()> }" in text


def test_the_invariants_section_describes_every_world_when_there_are_several() -> None:
    text = fill_invariants_section(_BASE, no_std="", macro_invariants="", worlds=_PLAN)
    assert text.startswith("This source is ONE tree compiled in EVERY cfg world below (`alt`, `base`).")
    # The loop identity's name is the HOME world's; which world judges a test
    # is that test's fact, carried by its quoted failure. Run
    # `1700492-f6c4d66c` (2026-10-24) was told its nightly-only test ran in
    # `stable`.
    assert "run in `base`" not in text and "run in `alt`" not in text
    assert "each failure quoted below names the world it failed in" in text
    assert "- cfg world `alt`:" in text and "- cfg world `base`:" in text
    assert "only from a context gated by the SAME predicate" in text
    # NEGATIVE CONTROL: the one-world text names its own world only and says
    # the other world's items do not exist.
    one = fill_invariants_section(_BASE, no_std="", macro_invariants="")
    assert "cfg world `alt`" not in one and "DO NOT EXIST in this world" in one
    assert fill_invariants_section(_BASE, no_std="", macro_invariants="",
                                   worlds=[_BASE]) == one
    assert fill_invariants_section(None, no_std="n", macro_invariants="m",
                                   worlds=_PLAN) == "n\n\nm"

"""TYPES_IN_SCOPE ranks the types a signature names before the ones only a
body mentions, and reaches the enum a method returns.

The instance: the session prompt built on run `3003-93e9a9bf`'s slice
(2026-09-02) carried `Backtrace` in a signature and `BacktraceStatus` in
the type records, and rendered neither — the selection was in record order,
the external records sat last, and the section's char cap cut them.
"""
from __future__ import annotations

from pipeline_core.domain.models import MethodRecord, TypeRecord
from pipeline_core.domain.type_refs import types_referenced_in_text


def _rec(name: str, *, methods: tuple[MethodRecord, ...] = (), fields=(),
         file: str = "src/lib.rs") -> TypeRecord:
    return TypeRecord(file=file, kind="struct", name=name, definition=f"struct {name}",
                      fields=tuple(fields), methods=methods, is_pub=True)


def test_a_method_return_type_is_pulled_in_one_hop() -> None:
    trace = _rec("Trace", methods=(MethodRecord(
        name="status", sig="fn status(&self) -> TraceStatus", is_pub=True),))
    for kind in ("enum", "external enum"):
        status = TypeRecord(file="", kind=kind, name="TraceStatus",
                            definition="enum TraceStatus", variants=("Off", "On"),
                            is_pub=True)
        got = types_referenced_in_text("fn f(t: &Trace)", [trace, status])
        assert [r.name for r in got] == ["Trace", "TraceStatus"], kind


def test_a_parameter_fn_arrow_is_not_a_return_hop() -> None:
    holder = _rec("Holder", methods=(MethodRecord(
        name="apply", sig="fn apply<F: Fn() -> Status>(&self, f: F)",
        is_pub=True),))
    status = TypeRecord(file="", kind="external enum", name="Status",
                        definition="enum Status", variants=("A",), is_pub=True)
    got = types_referenced_in_text("fn f(h: &Holder)", [holder, status])
    assert [r.name for r in got] == ["Holder"]


def test_a_type_named_nowhere_is_not_pulled_in() -> None:
    other = TypeRecord(file="", kind="enum", name="Other", definition="enum Other",
                       variants=("A",), is_pub=True)
    trace = _rec("Trace")
    assert [r.name for r in types_referenced_in_text("fn f(t: &Trace)", [trace, other])] == ["Trace"]


def test_a_returned_struct_is_not_hopped_into_scope() -> None:
    holder = _rec("Holder", methods=(MethodRecord(
        name="inner", sig="fn inner(&self) -> Inner", is_pub=True),))
    inner = _rec("Inner")
    assert [r.name for r in types_referenced_in_text("fn f(h: &Holder)", [holder, inner])] == ["Holder"]


def test_signature_types_rank_before_body_only_types() -> None:
    from pipeline_core.application.reconstruct.fix_context import (
        FixContextAssembler as FixContext,
    )
    body_only = [_rec(f"Body{i}") for i in range(6)]
    sig_type = _rec("Trace", methods=(MethodRecord(
        name="status", sig="fn status(&self) -> TraceStatus", is_pub=True),))
    status = TypeRecord(file="", kind="external enum", name="TraceStatus",
                        definition="enum TraceStatus", variants=("Off", "On"), is_pub=True)
    # Record order: the body-only types first, the signature's type and its
    # return enum LAST — the shape the external records arrive in.
    records = [*body_only, sig_type, status]
    from types import SimpleNamespace
    ctx = FixContext(
        budgets=SimpleNamespace(types_count=3, types_section=100_000),  # type: ignore[arg-type]
        filter_notes=lambda *_a, **_k: "", guard=lambda *_a, **_k: None)
    body = " ".join(f"Body{i}::new()" for i in range(6))
    section = FixContext.types_section_for_fix(
        ctx, "fn f(t: &Trace) -> u32", body, "", "", "", records)
    assert "struct Trace" in section and "enum TraceStatus" in section
    assert "struct Body0" in section and "struct Body1" not in section


def test_external_and_other_file_types_rank_before_the_file_own_types() -> None:
    from types import SimpleNamespace

    from pipeline_core.application.reconstruct.fix_context import (
        FixContextAssembler,
    )
    own = _rec("Own", file="src/lib.rs")
    other = _rec("Other", file="src/other.rs")
    ext = TypeRecord(file="", kind="struct", name="Ext", definition="struct Ext",
                     is_pub=True, external_path="std::ext::Ext")
    ctx = FixContextAssembler(
        budgets=SimpleNamespace(types_count=2, types_section=100_000),  # type: ignore[arg-type]
        filter_notes=lambda *_a, **_k: "", guard=lambda *_a, **_k: None)
    section = FixContextAssembler.types_section_for_fix(
        ctx, "fn f(a: Own, b: Other, c: Ext)", "", "", "", "", [own, other, ext],
        file_rel="src/lib.rs")
    assert "struct Ext" in section and "struct Other" in section
    assert "struct Own" not in section

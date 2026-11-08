"""The end gate's fixed point: which remaining stubs nothing live can call.

Run `3323407-984b07f2` (2026-11-08) passed every oracle test and exited 1 on
one stub, a `Write` impl on a private wrapper the rebuilt crate never builds
a value for: rustc's dead-code lint, the gate's only reader, never reports a
trait-impl method. The first fix decided trait-impl stubs by removal plus a
MIR guard that kept any use whose owner the lint did not name dead; its
review (2026-11-08) found two uses that guard kept with nothing live behind
them: a use inside a closure (`debug::{closure#0}` has no record), and a use
inside a trait-impl method only dead code calls (the lint never names one).
Walking the fix also found the opposite: a stub `Drop` impl has no MIR use
at all, deletes cleanly, and runs at every scope exit.

Claims (invented shapes, no cargo):
- a closure's uses are its creator's;
- the fixed point is the greatest one: a use in a dead closure, in a trait
  method only dead code calls, or in a cycle of units only each other call
  does not keep the stub; a use in a live fn, a const, or a block whose owner
  cannot be told does;
- an impl on a type an outside crate can hold, and one on a generic or
  foreign self type, are never dead;
- the certificate is the compiler's: a deletion may remove MIR items, never
  change a surviving one (deletion blanks, so positions stay; allocation ids
  normalized), so a drop the
  compiler stops inserting or a call that falls back keeps the stub, with no
  trait named anywhere (review of 2026-11-08);
- a stub's bundle is its unit and every non-seed dead unit its uses sit in;
  it is proved only when that bundle built in every compiling world;
- the use case reads no compiler view when the lint already proves every
  stub, and asks the removal build only for the bundles of dead stub units.
Negative controls: the pre-review owner rule (lint-named owner, else live)
keeps the closure and trait-method shapes; with no removal reading the stub
stays orphaned; a live caller keeps the chain; the build alone accepts both
changed-MIR copies.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pipeline_core.application.unreached_stub_reading import (
    UnreachedStubDeps,
    read_unreached_stub_proofs,
)
from pipeline_core.domain.attribution import record_site
from pipeline_core.domain.dead_code import WorldDeadCode, names_record
from pipeline_core.domain.fn_reachability import PublicSurface, WorldReading, parse_mir
from pipeline_core.domain.models import FnRecord
from pipeline_core.domain.stub_removal import (
    CompilerView,
    WorldUnreached,
    block_owner,
    creator_header,
    dead_units,
    delete_ranges,
    outside_caller,
    removable_subset,
    removal_bundles,
    units_of,
    unreached_stub_proofs,
)

# An invented file. Line numbers matter: MIR headers name an impl by its line.
_SRC = (
    "struct Wrap<C>(C);\n"                                   # 1
    "impl Write for Wrap<&mut F> {\n"                        # 2
    "    fn write_str(&mut self, s: &str) -> R {\n"          # 3
    "        ::core::unimplemented!()\n"                     # 4
    "    }\n"                                                # 5
    "}\n"                                                    # 6
    "impl Show for Holder {\n"                               # 7
    "    fn show(&self) -> R { todo }\n"                     # 8
    "}\n"                                                    # 9
    "fn helper() -> R { todo }\n"                            # 10
    "fn other() -> R { todo }\n"                             # 11
    "struct Guard;\n"                                        # 12
    "impl Drop for Guard {\n"                                # 13
    "    fn drop(&mut self) { ::core::unimplemented!() }\n"  # 14
    "}\n"                                                    # 15
    "struct Holder;\n"                                       # 16
)
_FILE = "src/w.rs"


def _span(needle: str, end_needle: str) -> tuple[int, int]:
    a = _SRC.index(needle)
    return a, _SRC.index(end_needle, a) + len(end_needle)


def _rec(name: str, sig: str, container: str, impl: tuple[int, int] | None,
         stub: bool) -> FnRecord:
    a = _SRC.index(sig)
    b = _SRC.index("{", a) + 1
    end = _SRC.index("}\n", b) + 1
    return FnRecord(file=_FILE, fn_name=name, container=container,
                    sig_byte_start=a, body_byte_start=b, body_byte_end=end - 1,
                    item_byte_start=a, item_byte_end=end,
                    impl_byte_start=impl[0] if impl else 0,
                    impl_byte_end=impl[1] if impl else 0,
                    has_unimplemented_body=stub)


_I_WRITE = _span("impl Write for Wrap", "    }\n}")
_I_SHOW = _span("impl Show for Holder", "}\n}")
_I_DROP = _span("impl Drop for Guard", "}\n}")
_WRITE = _rec("write_str", "fn write_str", "impl Write for Wrap < & mut F >", _I_WRITE, True)
_SHOW = _rec("show", "fn show", "impl Show for Holder", _I_SHOW, False)
_HELPER = _rec("helper", "fn helper", "", None, False)
_OTHER = _rec("other", "fn other", "", None, False)
_DROP = _rec("drop", "fn drop", "impl Drop for Guard", _I_DROP, True)
_TREE = [_WRITE, _SHOW, _HELPER, _OTHER, _DROP]
_TEXTS = {_FILE: _SRC}
_W_KEY = (_FILE, *_I_WRITE)
_S_KEY = (_FILE, *_I_SHOW)
_H_KEY = (_FILE, _HELPER.item_byte_start, _HELPER.item_byte_end)
_D_KEY = (_FILE, *_I_DROP)
_PRIVATE = PublicSurface(nameable=frozenset(), exposed=frozenset(), fn_spans={},
                         opaque_return=False, item_names={})
# `other` (line 11) is public: what it calls is live.
_OTHER_PUBLIC = PublicSurface(nameable=frozenset(), exposed=frozenset(),
                              fn_spans={_FILE: ((11, 11),)}, opaque_return=False,
                              item_names={})

_CALL_W = ("    _3 = <Wrap<&mut F> as std::fmt::Write>::write_str(move _4, copy _5)"
           " -> [return: bb1, unwind continue];")
_CALL_S = "    _2 = <Holder as Show>::show(copy _1) -> [return: bb1, unwind continue];"
_CALL_H = "    _1 = w::helper() -> [return: bb1, unwind continue];"


def _blk(header: str, *lines: str) -> str:
    return header + "\n" + "\n".join(lines) + "\n}\n"


_HELPER_HDR = "fn w::helper() -> R {"
_HELPER_CLOSURE = "fn w::helper::{closure#0}(_1: {closure@src/w.rs:10:20: 10:22}) -> R {"
_SHOW_HDR = "fn w::<impl at src/w.rs:7:1: 7:21>::show(_1: &Holder) -> R {"
_SHOW_CLOSURE = "fn w::<impl at src/w.rs:7:1: 7:21>::show::{closure#0}(_1: &Holder) -> R {"
_OTHER_HDR = "fn w::other() -> R {"
_CONST_HDR = "const w::VTABLE: &W = {"


def _lint_dead(*recs: FnRecord) -> WorldDeadCode:
    return WorldDeadCode("w", frozenset((r.file, r.sig_byte_start + 1) for r in recs))


def _view(dead: WorldDeadCode, *mir: str,
          surface: PublicSurface | None = _PRIVATE) -> CompilerView:
    return CompilerView(dead, ("".join(mir),), surface)


def test_a_closure_belongs_to_the_fn_that_created_it() -> None:
    assert creator_header(_HELPER_CLOSURE) == "fn w::helper("
    assert creator_header(_SHOW_CLOSURE).endswith("::show(")
    assert creator_header(_HELPER_HDR) == _HELPER_HDR
    assert block_owner(parse_mir(_blk(_HELPER_CLOSURE, _CALL_W))[0], _TREE, _TEXTS) == _HELPER
    assert block_owner(parse_mir(_blk(_SHOW_CLOSURE, _CALL_W))[0], _TREE, _TEXTS) == _SHOW
    assert block_owner(parse_mir(_blk(_CONST_HDR, _CALL_W))[0], _TREE, _TEXTS) is None


def test_a_use_in_a_closure_of_a_dead_fn_does_not_keep_the_stub() -> None:
    units = dead_units(_TREE, _view(_lint_dead(_HELPER), _blk(_HELPER_CLOSURE, _CALL_W)), _TEXTS)
    assert _W_KEY in units.dead
    # The same closure in a live fn keeps it.
    live = dead_units(_TREE, _view(_lint_dead(), _blk(_HELPER_CLOSURE, _CALL_W),
                                   _blk(_OTHER_HDR, _CALL_H), surface=_OTHER_PUBLIC), _TEXTS)
    assert _W_KEY not in live.dead and "helper" in live.live_why[_W_KEY]


def test_a_use_in_a_trait_method_only_dead_code_calls_does_not_keep_the_stub() -> None:
    mir = (_blk(_SHOW_HDR, _CALL_W), _blk(_HELPER_HDR, _CALL_S))
    units = dead_units(_TREE, _view(_lint_dead(_HELPER), *mir), _TEXTS)
    assert {_W_KEY, _S_KEY} <= units.dead
    assert removal_bundles([_WRITE], _TREE, units) == {_W_KEY: frozenset({_W_KEY, _S_KEY})}
    # A live caller of the dead fn makes the whole chain live.
    live = dead_units(_TREE, _view(_lint_dead(), *mir, _blk(_OTHER_HDR, _CALL_H),
                                   surface=_OTHER_PUBLIC), _TEXTS)
    assert _S_KEY not in live.dead and _W_KEY not in live.dead


def test_control_the_pre_review_owner_rule_kept_both_shapes() -> None:
    """The rule the review replaced: a use is ignored only when the lint names
    its owner dead. A closure's block has no record of its own, and the lint
    never names a trait-impl method, so both uses read as live."""
    def old_live(mir: str, dead: WorldDeadCode) -> bool:
        for blk in parse_mir(mir):
            name = blk.header.split("(", 1)[0].rsplit("::", 1)[-1]
            owner = next((r for r in _TREE if r.fn_name == name), None)
            if owner is None or not names_record(owner, dead.spans):
                return True
        return False
    assert old_live(_blk(_HELPER_CLOSURE, _CALL_W), _lint_dead(_HELPER))
    assert old_live(_blk(_SHOW_HDR, _CALL_W), _lint_dead(_HELPER))
    # The fixed point reads both as dead.
    assert _W_KEY in dead_units(
        _TREE, _view(_lint_dead(_HELPER), _blk(_HELPER_CLOSURE, _CALL_W)), _TEXTS).dead


def test_units_only_each_other_call_are_dead_together() -> None:
    back = ("    _2 = <Wrap<&mut F> as std::fmt::Write>::write_str(move _1, copy _2)"
            " -> [return: bb1, unwind continue];")
    mir = (_blk(_SHOW_HDR, back),
           _blk("fn w::<impl at src/w.rs:2:1: 2:27>::write_str(_1: &mut Wrap<&mut F>) -> R {",
                _CALL_S))
    units = dead_units(_TREE, _view(_lint_dead(), *mir), _TEXTS)
    assert {_W_KEY, _S_KEY} <= units.dead


def test_a_use_in_a_const_keeps_the_stub() -> None:
    units = dead_units(_TREE, _view(_lint_dead(), _blk(_CONST_HDR, _CALL_W)), _TEXTS)
    assert _W_KEY not in units.dead and "VTABLE" in units.live_why[_W_KEY]


def test_what_an_outside_crate_calls_is_never_dead() -> None:
    types = frozenset({"Wrap", "Holder", "Guard"})
    assert outside_caller(_WRITE, _SRC, _PRIVATE, types) == ""
    held = PublicSurface(nameable=frozenset({"Wrap"}), exposed=frozenset(),
                         fn_spans={}, opaque_return=False, item_names={})
    assert "nameable" in outside_caller(_WRITE, _SRC, held, types)
    assert "generic or not defined" in outside_caller(_WRITE, _SRC, _PRIVATE, frozenset())
    listed = PublicSurface(nameable=frozenset(), exposed=frozenset(),
                           fn_spans={_FILE: ((10, 10),)}, opaque_return=False, item_names={})
    assert "public index" in outside_caller(_HELPER, _SRC, listed, types)


# The certificate: a deletion may remove MIR items, never change one that
# survives. Shapes measured on `-Zunpretty=mir` 2026-11-08.
_GUARDED = _blk("fn guarded(_1: u8) -> u8 {", "    let _2: Guard;",
                "    bb0: {", "        _2 = Guard(copy _1);",
                "        drop(_2) -> [return: bb1, unwind continue];", "    }",
                "    bb1: {", "        return;", "    }")
_GUARDED_NO_DROP = _blk("fn guarded(_1: u8) -> u8 {", "    let _2: Guard;",
                        "    bb0: {", "        _2 = Guard(copy _1);", "        return;", "    }")
_CALL = _blk("fn call() -> u8 {", "    bb0: {",
             "        _0 = <Outer as Go>::go(move _1) -> [return: bb1, unwind continue];", "    }")
_CALL_FALLBACK = _blk("fn call() -> u8 {", "    bb0: {",
                      "        _0 = Inner::go(copy _1) -> [return: bb2, unwind continue];", "    }")
_IMPL_A = _blk("fn <impl at src/w.rs:9:1: 9:20>::fmt(_1: &A) -> R {",
               "    let _3: {closure@src/w.rs:10:20: 10:22};", "    _0 = const {alloc7: &str};")
_IMPL_B = _blk("fn <impl at src/w.rs:14:1: 14:20>::fmt(_1: &A) -> R {", "    _0 = const 2;")
_STUB = _blk("fn <impl at src/w.rs:3:1: 3:20>::write_str(_1: &mut Sink) -> R {",
             "    _2 = panic(const \"not implemented\");")


def test_a_deletion_that_only_removes_items_leaves_the_mir_unchanged() -> None:
    from pipeline_core.domain.stub_removal import mir_unchanged
    base = _STUB + _GUARDED + _IMPL_A + _IMPL_B
    # The deleted impl's own block is gone and an allocation id renumbered;
    # positions do not move, because the deletion blanks.
    cut = _GUARDED + _IMPL_A.replace("alloc7", "alloc3") + _IMPL_B
    assert mir_unchanged(base, cut)
    # A position that moved is a change: blanking cannot move one.
    assert not mir_unchanged(base, _GUARDED + _IMPL_A.replace("10:20", "5:20") + _IMPL_B)


def test_a_deletion_the_compiler_answers_differently_changes_the_mir() -> None:
    """The two ways a deletion builds while the program changed: the compiler
    stops inserting a drop, or a call falls back to another candidate."""
    from pipeline_core.domain.stub_removal import mir_unchanged
    assert not mir_unchanged(_STUB + _GUARDED, _GUARDED_NO_DROP)
    assert not mir_unchanged(_STUB + _CALL, _CALL_FALLBACK)
    # Items are a multiset: one may not
    # stand in for the other.
    assert not mir_unchanged(_IMPL_A + _IMPL_B, _IMPL_B + _IMPL_B)
    # No dump is no reading.
    assert not mir_unchanged(_GUARDED, "")


def test_the_rule_names_no_trait() -> None:
    """The class this replaced: a liveness rule for one trait (an early
    return, then a spelling heuristic, then a constructor-shim exception).
    The negative control that the build alone proves a held type's drop is
    the integration module's, on real cargo."""
    import inspect

    from pipeline_core.domain import stub_removal as mod
    src = inspect.getsource(mod)
    for name in ("Drop", "Deref", "Debug", "Clone"):
        assert f'"{name}"' not in src and f"'{name}'" not in src, name
    assert "NEVER_PROVED_TRAITS" not in src


def test_no_compiler_view_proves_nothing_beyond_the_lint() -> None:
    units = dead_units(_TREE, _view(_lint_dead(_HELPER), surface=None), _TEXTS)
    assert units.dead == {_H_KEY} and _W_KEY in units.live_why


def test_units_group_a_trait_impl_and_leave_provided_methods_out() -> None:
    sibling = _rec("flush", "fn write_str", "impl Write for Wrap < & mut F >", _I_WRITE, False)
    provided = FnRecord(file=_FILE, fn_name="p", container="trait T",
                        item_byte_start=1, item_byte_end=5)
    units = units_of([_WRITE, sibling, provided, _HELPER])
    assert units[(_FILE, _WRITE.sig_byte_start, "write_str")].records == (_WRITE, sibling)
    assert (_FILE, provided.sig_byte_start, "p") not in units
    assert units[(_FILE, _HELPER.sig_byte_start, "helper")].kind == "fn"


def test_deletion_merges_nested_ranges_and_cuts_bytes() -> None:
    text = b"0123456789"
    assert delete_ranges(text, [(2, 5), (3, 4), (7, 9)]) == b"01   56  9"
    assert delete_ranges(text, []) == text
    # Blanking keeps every line and column: newlines stay, a multi-byte
    # character becomes one space.
    src = "a\nimpl X {\n  \u00e9\n}\nfn f() {}\n".encode()
    a = src.index(b"impl")
    b = src.index(b"}\n") + 1
    cut = delete_ranges(src, [(a, b)])
    assert cut.count(b"\n") == src.count(b"\n")
    assert cut.decode().splitlines()[2] == "   " and cut.endswith(b"fn f() {}\n")


def test_group_testing_keeps_what_built_and_bisects_only_on_failure() -> None:
    calls: list[frozenset] = []
    needed = ("f", 3, 4)

    def builds(keys: frozenset) -> bool:
        calls.append(keys)
        return needed not in keys

    items = [("f", 1, 2), ("f", 3, 4), ("f", 5, 6), ("f", 7, 8)]
    assert removable_subset(items, builds) == frozenset(items) - {needed}
    assert len(calls) < 2 * len(items)


def _everywhere(_r: FnRecord, _c: str) -> bool:
    return True


def test_a_stub_is_proved_only_when_every_world_built_its_bundle() -> None:
    site = record_site(_WRITE)
    built = WorldUnreached("a", WorldDeadCode("a"), removable=frozenset({_W_KEY}))
    assert site in unreached_stub_proofs([_WRITE], _TREE, [built], _everywhere)
    other = WorldUnreached("b", WorldDeadCode("b"))
    assert unreached_stub_proofs([_WRITE], _TREE, [built, other], _everywhere) == {}
    # NEGATIVE CONTROL: lint only, which never names a trait-impl method.
    assert unreached_stub_proofs([_WRITE], _TREE, [WorldUnreached("a", _lint_dead(_WRITE))],
                                 _everywhere) == {}
    # A lint-dead free fn stub is proved by the lint alone.
    stub_helper = _rec("helper", "fn helper", "", None, True)
    lint = WorldUnreached("a", _lint_dead(stub_helper))
    assert "rustc reports" in unreached_stub_proofs(
        [stub_helper], _TREE, [lint], _everywhere)[record_site(stub_helper)]


# ── the use case ────────────────────────────────────────────────────────────

_ONE = [SimpleNamespace(name="w", cfg_true=[], cfg_false=[])]


def _deps(tmp_path: Path, mir: str, dead: WorldDeadCode, removal, asked: list,
          views: list, fns=None) -> UnreachedStubDeps:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / _FILE).write_text(_SRC)

    def view(_p: Path, c: SimpleNamespace) -> WorldReading:
        views.append(c.name)
        return WorldReading(c.name, mir=mir, rustdoc="{}", mir_test="")

    def measure(_p, _c, bundles, base):
        asked.append((dict(bundles), base))
        return removal(bundles), ""

    return UnreachedStubDeps(
        enumerate_fns=lambda _p: fns if fns is not None else _TREE,
        enumerate_mods=lambda _p: [],
        measure_dead=lambda _p, c: dead, read_compiler_view=view,
        measure_removal=measure, log=lambda _m: None)


def _private(monkeypatch) -> None:
    from pipeline_core.application import unreached_stub_reading as mod
    monkeypatch.setattr(mod, "public_surface", lambda _t: _PRIVATE)


def test_the_use_case_builds_the_bundle_and_proves_the_stub(tmp_path: Path, monkeypatch) -> None:
    _private(monkeypatch)
    asked: list = []
    mir = _blk(_SHOW_HDR, _CALL_W) + _blk(_HELPER_HDR, _CALL_S)
    deps = _deps(tmp_path, mir, _lint_dead(_HELPER), frozenset, asked, [])
    proofs = read_unreached_stub_proofs(tmp_path, _ONE, deps)
    assert record_site(_WRITE) in proofs
    assert record_site(_DROP) in proofs                     # its removal certified it
    (bundles, base), = asked
    assert bundles == {_W_KEY: frozenset({_W_KEY, _S_KEY}), _D_KEY: frozenset({_D_KEY})}
    assert base == frozenset({_H_KEY})                      # the lint-dead caller


def test_the_use_case_keeps_a_stub_whose_bundle_does_not_build(tmp_path: Path, monkeypatch) -> None:
    _private(monkeypatch)
    deps = _deps(tmp_path, _blk(_OTHER_HDR, "    _0 = const 1;"), _lint_dead(),
                 lambda _b: frozenset(), [], [])
    assert record_site(_WRITE) not in read_unreached_stub_proofs(tmp_path, _ONE, deps)


def test_the_use_case_reads_no_compiler_view_when_the_lint_proves_every_stub(
        tmp_path: Path) -> None:
    stub_helper = _rec("helper", "fn helper", "", None, True)
    views: list = []
    deps = _deps(tmp_path, "", _lint_dead(stub_helper), frozenset, [], views,
                 fns=[stub_helper, _OTHER])
    assert record_site(stub_helper) in read_unreached_stub_proofs(tmp_path, _ONE, deps)
    assert views == []

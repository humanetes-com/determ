"""Phase-C fill engine use-cases: FillOneFn + PhaseCFillEngine driven
through their `ports.fill_engine` roles with one recording fake adapter.

The REAL engine (real splice/revert/FloorStall over scripted cargo+model) is
covered end-to-end by tests/test_e2e_fill_loop.py et al. via the
`pipeline._task2_fill_fns_one_by_one` shim; these tests pin the de-monolithed
units directly: the skip/accept branches of FillOneFn and the dispatch shells of
PhaseCFillEngine (impl-path early return, empty-cluster)."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from pipeline_core.application.reconstruct.context import PerFnPassCtx
from pipeline_core.application.reconstruct.fill_one_fn import FillCaps, FillOneFn
from pipeline_core.application.reconstruct.phase_c_engine import (
    EngineConfig,
    PhaseCFillEngine,
)
from pipeline_core.domain.attribution import FailureFrontier
from pipeline_core.domain.enums import FillUnit, RouteOrder
from pipeline_core.domain.models import (
    ClusterFillResult,
    FnFixContext,
    Stub,
)
from pipeline_core.domain.reconstruction import FnLocation, SpliceOutcome
from pipeline_core.domain.refine import CompilingSalvage, FixProgress
from pipeline_core.domain.retro_gate import (
    ConvictionEvidence,
    RetroProvenance,
    stamp_conviction,
)
from pipeline_core.domain.stub_inventory import StubInventory
from pipeline_core.domain.test_results import RegressionComparison


class FakeFill:
    """Implements every fill-engine role; records events and returns scripted
    values. Satisfies the Protocols structurally (same as `_PhaseCFillAdapter`)."""

    def __init__(self, **scripted: Any) -> None:
        self.s = scripted
        self.events: list[str] = []
        self.event_data: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[str] = []
        self.lock = threading.Lock()

    # EngineTelemetry
    def log(self, msg: str) -> None: self.logs.append(msg)
    def emit(self, event: str, *, duration_ms: int | None = None,
             data: dict[str, Any] | None = None) -> None:
        self.events.append(event)
        self.event_data.append((event, data or {}))
    def check_stop(self, context: str) -> None: ...

    # Mutator
    def locate_fn_by_record(self, *a: Any) -> Any: return self.s.get("loc_rec")
    def locate_fn(self, *a: Any) -> Any: return self.s.get("loc", ("fn x()", 0, 5, 7))
    def splice_fn_body_at(self, *a: Any, **k: Any) -> SpliceOutcome:
        self.s["last_allow_nonstub"] = k.get("allow_nonstub", False)
        return self.s.get("splice", SpliceOutcome(True, "ok", 4))
    def splice_fn_body(self, *a: Any, **k: Any) -> SpliceOutcome:
        self.s["last_allow_nonstub"] = k.get("allow_nonstub", False)
        return self.s.get("splice", SpliceOutcome(True, "ok", 4))
    def splice_body_scoped(self, *a: Any, **k: Any) -> SpliceOutcome:
        self.s["last_allow_nonstub"] = k.get("allow_nonstub", False)
        return self.s.get("splice", SpliceOutcome(True, "ok", 4))
    def parse_validator(self) -> Any:
        return self.s.get("parse_ok")
    def revert_body_scoped(self, *a: Any, **k: Any) -> SpliceOutcome:
        self.s.setdefault("reverts", []).append(a)
        return SpliceOutcome(True, "ok", 0)
    def replace_fn_signature(self, *a: Any) -> bool: return False
    def extract_signature_block(self, *a: Any) -> str | None: return None
    def lock_for(self, tree: Path) -> threading.Lock: return self.lock

    # Cargo
    def run(self, *a: Any, **k: Any) -> tuple[int, str]:
        seq = self.s.get("run_seq")
        if seq:
            return seq.pop(0)
        return self.s.get("run", (0, ""))
    def check_diag(self, *a: Any, **k: Any) -> tuple[int, str, list[Any]]:
        rc, out = self.run(*a, **k)
        return rc, out, []
    def build_test_cmd(self, *a: Any) -> str: return "cargo test"
    def u2_test_gate(self, *a: Any) -> Any:
        from pipeline_core.domain.test_report import U2Verdict
        return self.s.get("u2") or U2Verdict.accept()
    def test_regression_baseline(self, *a: Any) -> Any: return self.s.get("regr_base")
    def test_regression_gate(self, *a: Any, **kw: Any) -> Any: return self.s.get("regr")
    def focus_errors(self, *a: Any, **k: Any) -> str: return self.s.get("err", "E")
    def env(self) -> dict[str, str]: return {}

    # PromptContext
    def ask_per_fn_body(self, *a: Any, **k: Any) -> tuple[str | None, str]:
        return self.s.get("body", ("ok()", "RESP"))
    def gather(self, **k: Any) -> FnFixContext: return FnFixContext()
    def collect_sibling_filled_bodies(self, *a: Any) -> str: return ""
    def extract_crate_idioms(self, *a: Any, **k: Any) -> str: return ""
    def collect_crate_macros(self, *a: Any) -> str: return ""
    def compute_cluster_macro_invariants(self, *a: Any) -> str: return ""
    def detect_crate_no_std_invariant(self, *a: Any) -> str: return ""

    # WorkspaceModel
    def scan_stub_records(self, *a: Any) -> Any: return self.s.get("stub_records")
    def scan_stub_tuples(self, *a: Any) -> StubInventory:
        return StubInventory.of_units(self.s.get("stub_tuples", []))
    def enumerate_fns(self, *a: Any) -> Any: return self.s.get("fns", [])
    def enumerate_types(self, *a: Any) -> Any: return []
    def collect_impl_blocks(self, *a: Any) -> Any: return []
    def collect_impl_blocks_for_cluster(self, *a: Any) -> Any:
        return self.s.get("cluster_blocks", [])
    def fn_container_index(self, *a: Any) -> dict[tuple[str, str], str]: return {}
    def fn_belongs_to_cluster(self, *a: Any, **k: Any) -> bool: return True
    def cluster_filter_type_members(self, records: Any, cluster: Any) -> Any: return []
    def is_gen_tests_file(self, name: str) -> bool: return False
    def leaf_first_fn_order(self, tuples: Any, counts: Any) -> Any: return tuples

    # Retros
    def load(self, *a: Any) -> dict[str, Any]: return {}
    def save(self, *a: Any) -> None: ...
    def retro_key(self, *a: Any) -> str: return "k"
    def is_fn_fully_exhausted(self, *a: Any) -> bool: return self.s.get("exhausted", False)
    def diagnose(self, **k: Any) -> dict[str, Any]: return {"signal": "", "category": ""}
    def append_note(self, *a: Any, **k: Any) -> bool: return False
    def extract_self_note(self, *a: Any) -> str | None: return None
    def record_error_codes(self, *a: Any) -> None: ...

    # TestLinker
    def find_tests_for_fn(self, *a: Any, **k: Any) -> list[tuple[str, str]]: return []
    def classify_failures(self, *a: Any, **k: Any) -> dict[str, dict[str, Any]]: return {}
    def write_test_fix_log(self, **k: Any) -> None: ...

    # StallPolicy
    def effective_stalls(self, budget: int, module: str | None) -> int: return budget
    def converge_in_cluster(self, cluster: Any) -> str | None: return None

    # ImplFillPath
    def fill_impl_blocks(self, **k: Any) -> ClusterFillResult:
        return self.s.get("impl_stats", ClusterFillResult())

    def prepare_impl_fill(self, **k: Any) -> Any:
        from pipeline_core.application.reconstruct.fill_routes import PreparedFill
        stats = self.s.get("impl_stats", ClusterFillResult())
        return PreparedFill(routes=(), hollow={}, finish=lambda: stats,
                            bodies_sha=lambda _t: "", landed=lambda: stats.fns_filled)


_CAPS = FillCaps(per_fn_fix_hard_cap=8, per_fn_fix_max_stalls=2, fix_err_cap=2000)


def _ctx(ws: Path) -> PerFnPassCtx:
    return PerFnPassCtx(
        project_dir=ws, target_src_dir=ws / "src", module_name="m",
        cluster_name="t", check_cmd="cargo check", cluster_env={}, fr_path=None,
        tests_dir_files=[], macros_section="", invariants_section="",
        crate_type_records=[], src_for_macros=ws / "src",
        failures_dir=ws / ".skeleton" / "_failures",
        tdd_frontier_tests=None, tdd_test_cmd=None,
        splice_check_lock=threading.Lock(), retros_cache={}, idiom_blocks=[],
        pass_n=1, total=1, cluster_cfg_set=None,
    )


def _mk_ws(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / ".skeleton" / "_failures").mkdir(parents=True)
    (tmp_path / "src" / "lib.rs").write_text("pub fn answer() {\n unimplemented!()\n}\n")
    return tmp_path


def _fill_one(fake: FakeFill) -> FillOneFn:
    return FillOneFn(tel=fake, mut=fake, cargo=fake, prompt=fake, retros=fake,
                     tests=fake, stall=fake, caps=_CAPS)


# ---- domain value objects ----

def test_fn_fix_context_defaults() -> None:
    c = FnFixContext()
    assert (c.container, c.impl_where, c.types_section, c.notes_section) == ("", "", "", "")


def test_stub_carries_cfg_predicates_for_variant_splice() -> None:
    # the Stub must thread the fn's cfg chain so the splice's
    # locate_fn_by_record cfg tier can pick the right one of two cfg-variant fns
    # sharing file+name+container+sig (the cap_limit pair).
    s = Stub.from_dict({"file": "lib.rs", "fn_name": "cap_limit",
                        "cfg_predicates": ["extra_cap"]})
    assert s.cfg_predicates == ("extra_cap",)
    assert s.to_dict()["cfg_predicates"] == ["extra_cap"]
    # Tolerant of a missing key (hand-built / AST-less fallback) → empty tuple.
    assert Stub.from_dict({"file": "lib.rs", "fn_name": "x"}).cfg_predicates == ()


# ---- exhaustion salvage: a compiling body beats the stub ----

def _salvage_call(fake: FakeFill, ws: Path, salvage: CompilingSalvage,
                  reverts: list[int]) -> bool:
    return _fill_one(fake)._salvage_compiling_body(
        _ctx(ws), salvage, ws / "src" / "lib.rs", "lib.rs", "answer", "", "",
        Stub(file="lib.rs", fn_name="answer"),
        lambda: (reverts.append(1), True)[1])


def _regressed() -> tuple[RegressionComparison, str]:
    return (RegressionComparison(
        newly_red=frozenset({"test_x"}), newly_crashed=(), excluded_crashed=(),
        not_run=(), measured=("test_x",), base_pass=1, cur_pass=0,
        base_executed=1, cur_executed=1), "detail")


def test_exhaustion_keeps_the_best_compiling_body_over_the_stub(
        tmp_path: Path) -> None:
    # The stub is not a neutral fallback — it panics, aborting the test binary
    # and un-judging every sibling test in it. A body that compiles and reddens
    # nothing is therefore the better end state even though its own tests fail.
    ws = _mk_ws(tmp_path)
    fake = FakeFill(run=(0, ""), regr=None)
    salvage = CompilingSalvage()
    salvage.offer("Ok(())", FixProgress(0, 2))
    reverts: list[int] = []
    assert _salvage_call(fake, ws, salvage, reverts) is True
    assert reverts == []
    assert any("KEEPING best compiling body" in m for m in fake.logs)
    assert "phase_c.fn.fill.salvaged" in fake.events


def test_exhaustion_reverts_to_the_stub_when_the_salvage_regresses(
        tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = FakeFill(run=(0, ""), regr=_regressed())
    salvage = CompilingSalvage()
    salvage.offer("Ok(())", FixProgress(0, 2))
    reverts: list[int] = []
    assert _salvage_call(fake, ws, salvage, reverts) is False
    assert reverts == [1]
    assert "phase_c.fn.fill.salvaged" not in fake.events


def test_exhaustion_reverts_when_the_salvage_no_longer_compiles(
        tmp_path: Path) -> None:
    # The tree moved while this fn queued; the banked body may not build here.
    ws = _mk_ws(tmp_path)
    fake = FakeFill(run=(101, "error[E0308]"), regr=None)
    salvage = CompilingSalvage()
    salvage.offer("Ok(())", FixProgress(0, 2))
    reverts: list[int] = []
    assert _salvage_call(fake, ws, salvage, reverts) is False
    assert reverts == [1]


def test_exhaustion_with_no_compiling_attempt_costs_no_cargo_run(
        tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = FakeFill(run_seq=[])
    reverts: list[int] = []
    assert _salvage_call(fake, ws, CompilingSalvage(), reverts) is False
    assert reverts == [] and fake.logs == []


# ---- FillOneFn branches ----

def test_fill_one_fully_exhausted_skips(tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = FakeFill(exhausted=True)
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=_ctx(ws))
    assert not res.filled and res.attempts == 0
    assert "phase_c.fn.fill.skip" in fake.events


def test_fill_one_exhaustion_not_honoured_when_guidance_contradicts_layout(
        tmp_path: Path) -> None:
    # A conviction reached while the prompt taught a field the current layout
    # does not have counted no fair attempt: the stored guidance says `_object`
    # but the file declares `error`, so the skip must not fire. (Live: one
    # retro store serves two clusters whose private layouts differ; the shared
    # guidance held every unit of the plateau file permanently skipped.)
    ws = _mk_ws(tmp_path)
    (ws / "src" / "lib.rs").write_text(
        "struct ErrorImpl { vtable: u8, error: u8 }\n"
        "pub fn answer() {\n unimplemented!()\n}\n")
    fake = FakeFill(exhausted=True)
    ctx = _ctx(ws)
    ctx.retros_cache["k"] = {
        "fully_exhausted": True,
        "guidance": "Access the wrapped error via the `_object` field of "
                    "ErrorImpl, never `error`.",
    }
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert "phase_c.fn.fill.skip" not in fake.events
    assert any("layout-contradicted" in m for m in fake.logs)
    assert res is not None


def test_fill_one_exhaustion_honoured_when_guidance_matches_layout(
        tmp_path: Path) -> None:
    # The override is narrow: guidance consistent with the declared fields is
    # a genuine conviction and still skips.
    ws = _mk_ws(tmp_path)
    (ws / "src" / "lib.rs").write_text(
        "struct ErrorImpl { vtable: u8, error: u8 }\n"
        "pub fn answer() {\n unimplemented!()\n}\n")
    fake = FakeFill(exhausted=True)
    ctx = _ctx(ws)
    ctx.retros_cache["k"] = stamp_conviction(
        {"fully_exhausted": True,
         "guidance": "Read the wrapped error from the `error` field of "
                     "ErrorImpl.",
         "blocked_on": ["other_stub"]},
        RetroProvenance.current("t", (ws / "src" / "lib.rs").read_text()),
        ConvictionEvidence(frozenset()))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert not res.filled and res.attempts == 0
    assert "phase_c.fn.fill.skip" in fake.events


def test_fill_one_exhaustion_not_honoured_when_blocked_on_is_empty(
        tmp_path: Path) -> None:
    """A layout-consistent corrective record with empty `blocked_on` cannot
    convict the unit into a skip: the next launch would never screen, and
    the ninth rule would have nothing to retire. Run `9933-efe68f37` left
    `src/error.rs::msg::Error` fully_exhausted with `blocked_on: []`."""
    ws = _mk_ws(tmp_path)
    (ws / "src" / "lib.rs").write_text(
        "struct ErrorImpl { vtable: u8, error: u8 }\n"
        "pub fn answer() {\n unimplemented!()\n}\n")
    fake = FakeFill(exhausted=True)
    ctx = _ctx(ws)
    ctx.retros_cache["k"] = stamp_conviction(
        {"fully_exhausted": True,
         "guidance": "Read the wrapped error from the `error` field of "
                     "ErrorImpl.",
         "blocked_on": []},
        RetroProvenance.current("t", (ws / "src" / "lib.rs").read_text()),
        ConvictionEvidence(frozenset()))
    _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert "phase_c.fn.fill.skip" not in fake.events
    assert any("no_tree_premise" in m for m in fake.logs)


def test_fill_one_exhaustion_not_honoured_when_earned_elsewhere(
        tmp_path: Path) -> None:
    # Same layout-consistent guidance, but the verdict was earned on the OTHER
    # cluster — whose private layout this file does not share. A conviction is
    # proof of hardness only for the context that produced it; unstamped and
    # foreign-stamped records convict nothing.
    ws = _mk_ws(tmp_path)
    (ws / "src" / "lib.rs").write_text(
        "struct ErrorImpl { vtable: u8, error: u8 }\n"
        "pub fn answer() {\n unimplemented!()\n}\n")
    fake = FakeFill(exhausted=True)
    ctx = _ctx(ws)
    ctx.retros_cache["k"] = stamp_conviction(
        {"fully_exhausted": True,
         "guidance": "Read the wrapped error from the `error` field of "
                     "ErrorImpl.",
         "blocked_on": ["other_stub"]},
        RetroProvenance.current(
            "other-cluster", (ws / "src" / "lib.rs").read_text()),
        ConvictionEvidence(frozenset()))
    _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert "phase_c.fn.fill.skip" not in fake.events
    assert any("stale_context" in m for m in fake.logs)


def test_fill_one_exhaustion_not_honoured_when_its_tests_no_longer_convict(
        tmp_path: Path) -> None:
    # The plateau: a fn convicted while the frontier blamed it for a test with
    # no evidence at it. Once attribution narrows, that verdict rests on a
    # failure the fn never answered for — honour it and the fn is skipped
    # before a single attempt, every run, forever.
    from dataclasses import replace

    from pipeline_core.domain.models import FnLocator
    ws = _mk_ws(tmp_path)
    fake = FakeFill(exhausted=True)
    ctx = replace(_ctx(ws), tdd_frontier_tests=FailureFrontier.of_direct(
        {FnLocator("lib.rs", "answer"): {"test_mine"}}))
    ctx.retros_cache["k"] = stamp_conviction(
        {"fully_exhausted": True, "guidance": "",
         "blocked_on": ["other_stub"]},
        RetroProvenance.current("t", (ws / "src" / "lib.rs").read_text()),
        ConvictionEvidence(frozenset({"test_mine", "test_someone_elses"})))
    _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert "phase_c.fn.fill.skip" not in fake.events
    assert any("stale_evidence" in m for m in fake.logs)


def test_fill_one_tallies_sieve_verdicts_for_the_pass(tmp_path: Path) -> None:
    # One line per batch beats one line per unit: without the denominator,
    # "the sieve retires everything" and "it retires nothing" both read as
    # silence in the log.
    ws = _mk_ws(tmp_path)
    (ws / "src" / "lib.rs").write_text(
        "struct ErrorImpl { vtable: u8, error: u8 }\n"
        "pub fn answer() {\n unimplemented!()\n}\n")
    ctx = _ctx(ws)
    ctx.retros_cache["k"] = {
        "guidance": "Access the wrapped error via the `_object` field of "
                    "ErrorImpl."}
    _fill_one(FakeFill(exhausted=True)).run(
        1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert (ctx.retro_gate.retired, ctx.retro_gate.honoured) == (1, 0)
    assert "retired=1" in ctx.retro_gate.render()


def test_fill_one_missing_file_skips(tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = FakeFill()
    res = _fill_one(fake).run(1, Stub(file="nope.rs", fn_name="x"), ctx=_ctx(ws))
    assert not res.filled and res.attempts == 0


def test_fill_one_happy_path_first_attempt(tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    # locate_fn_by_record None → regex locator path; splice ok; cargo rc=0; u2 None.
    fake = FakeFill(loc=("pub fn answer()", 0, 16, 35),
                    splice=SpliceOutcome(True, "ok", 4),
                    run=(0, ""), u2=None, body=("42", "RESP"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=_ctx(ws))
    assert res.filled and res.attempts == 1
    assert "phase_c.fn.fill.done" in fake.events


def test_fill_one_stub_splices_without_allow_nonstub(tmp_path: Path) -> None:
    # an ordinary stub fill must NOT allow_nonstub (refuse a filled body).
    ws = _mk_ws(tmp_path)
    fake = FakeFill(loc_rec=FnLocation(0, 0, 5, "pub fn answer()", "unimplemented!()"),
                    splice=SpliceOutcome(True, "ok", 4),
                    run=(0, ""), u2=None, body=("42", "RESP"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=_ctx(ws))
    assert res.filled
    assert fake.s["last_allow_nonstub"] is False


def test_fill_one_refill_splices_with_allow_nonstub(tmp_path: Path) -> None:
    # a refill target replaces an already-filled body → the
    # splice MUST pass allow_nonstub=True (else target_not_stub refuses it).
    ws = _mk_ws(tmp_path)
    (ws / "src" / "lib.rs").write_text("pub fn answer() {\n 0\n}\n")
    fake = FakeFill(loc_rec=FnLocation(0, 0, 5, "pub fn answer()", "unimplemented!()"),
                    splice=SpliceOutcome(True, "ok", 4),
                    run=(0, ""), u2=None, body=("42", "RESP"))
    res = _fill_one(fake).run(
        1, Stub(file="lib.rs", fn_name="answer", refill=True), ctx=_ctx(ws))
    assert res.filled
    assert fake.s["last_allow_nonstub"] is True
    assert fake.events[0] == "phase_c.fn.fill.start"


def test_fill_one_empty_response_stops(tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = FakeFill(loc=("pub fn answer()", 0, 16, 35), body=(None, ""))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=_ctx(ws))
    # No usable body on every attempt → exhausted → annotated stub, not filled.
    assert not res.filled
    assert "phase_c.fn.fill.fail" in fake.events


# ---- PhaseCFillEngine shells ----

def _engine(fake: FakeFill, fill_unit: FillUnit,
            route_order: RouteOrder = RouteOrder.DEFAULT) -> PhaseCFillEngine:
    cfg = EngineConfig(parallelism=1, fill_unit=fill_unit,
                       per_pass_test_fix_hard_cap=6, per_pass_test_fix_max_stalls=2,
                       pass_max_stalls=2, fix_err_cap=2000,
                       route_order=route_order)
    return PhaseCFillEngine(tel=fake, mut=fake, cargo=fake, prompt=fake, ws=fake,
                            retros=fake, tests=fake, stall=fake, impl_path=fake,
                            fill_one=_fill_one(fake), config=cfg)


def test_engine_impl_path_early_return(tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    stats = ClusterFillResult(remaining=0, fns_filled=3, impls_filled=1,
                              impls_failed=0)
    fake = FakeFill(impl_stats=stats)
    out = _engine(fake, FillUnit.IMPL).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={})
    assert out is stats


def test_engine_per_fn_empty_cluster(tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    # No stub records, no stub tuples → loop breaks immediately, zero result.
    fake = FakeFill(stub_records=[], stub_tuples=[])
    out = _engine(fake, FillUnit.FN).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={},
        skip_end_pass_test=True)
    assert out.fns_filled == 0 and out.remaining == 0
    assert "phase_c.cluster.done" in fake.events


def test_collect_refill_targets_only_filled_frontier_fns(tmp_path: Path) -> None:
    # a frontier fn that is NOT a stub but has a failing test
    # becomes a refill=True target; stubs and tests-less keys are excluded.
    from pipeline_core.domain.models import FnLocator
    fns = [
        {"file": "src/a.rs", "fn_name": "wrongfn", "container": "impl A",
         "sig_text": "fn wrongfn()"},                       # filled+failing → refill
        {"file": "src/a.rs", "fn_name": "stubfn", "sig_text": "fn stubfn()"},
        {"file": "src/a.rs", "fn_name": "greenfn", "sig_text": "fn greenfn()"},
    ]
    eng = _engine(FakeFill(fns=fns), FillUnit.FN)
    stubs = [Stub(file="src/a.rs", fn_name="stubfn", sig_text="fn stubfn()")]
    keys = {FnLocator("src/a.rs", "wrongfn"), FnLocator("src/a.rs", "stubfn"),
            FnLocator("src/a.rs", "greenfn")}
    # only wrongfn + stubfn have failing tests; stubfn is still a stub → excluded.
    tests = FailureFrontier.of_direct({FnLocator("src/a.rs", "wrongfn"): {"t1"},
                                       FnLocator("src/a.rs", "stubfn"): {"t2"}})
    out = eng._collect_refill_targets(tmp_path, stubs, keys, tests)
    assert [(s.fn_name, s.refill) for s in out] == [("wrongfn", True)]
    # No frontier map (legacy fn_walk driver) → never produces refill targets.
    assert eng._collect_refill_targets(tmp_path, stubs, None, None) == []
    assert eng._collect_refill_targets(tmp_path, stubs, keys, None) == []


def test_collect_refill_targets_dispatch_projection_does_not_stamp(
        tmp_path: Path) -> None:
    """of_dispatch is the plan's negative control: the test is on the unit
    for dispatch, not for rewriting a compiling body."""
    from pipeline_core.domain.models import FnLocator
    fns = [
        {"file": "src/a.rs", "fn_name": "wrongfn", "container": "impl A",
         "sig_text": "fn wrongfn()"},
    ]
    eng = _engine(FakeFill(fns=fns), FillUnit.FN)
    keys = {FnLocator("src/a.rs", "wrongfn")}
    tests = FailureFrontier.of_dispatch(
        {FnLocator("src/a.rs", "wrongfn"): {"t1"}})
    assert eng._collect_refill_targets(tmp_path, [], keys, tests) == []


def test_collect_refill_targets_dispatch_stamps_free_fn(tmp_path: Path) -> None:
    """Per-fn is the route that fills free fns. of_dispatch must still stamp
    them or an escalated filled free fn is stranded (`vtable`, 2026-08-04)."""
    from pipeline_core.domain.models import FnLocator
    fns = [
        {"file": "src/error.rs", "fn_name": "vtable", "container": "",
         "sig_text": "fn vtable()"},
    ]
    eng = _engine(FakeFill(fns=fns), FillUnit.FN)
    keys = {FnLocator("src/error.rs", "vtable")}
    tests = FailureFrontier.of_dispatch(
        {FnLocator("src/error.rs", "vtable"): {"test_downcast"}})
    out = eng._collect_refill_targets(tmp_path, [], keys, tests)
    assert [(s.fn_name, s.refill) for s in out] == [("vtable", True)]


def test_collect_refill_targets_dispatch_stamps_oversized_block(
        tmp_path: Path) -> None:
    """A block whose stub+refill count exceeds the per-impl cap is never
    attempted by the impl route; per-fn is its rewrite planner."""
    from pipeline_core.domain.models import FnLocator
    fns = [
        {"file": "src/a.rs", "fn_name": f"m{i}", "container": "impl Big",
         "sig_text": f"fn m{i}()"}
        for i in range(13)
    ]
    eng = _engine(FakeFill(fns=fns), FillUnit.FN)
    keys = {FnLocator("src/a.rs", f"m{i}") for i in range(13)}
    tests = FailureFrontier.of_dispatch({k: {"t"} for k in keys})
    out = eng._collect_refill_targets(tmp_path, [], keys, tests)
    assert len(out) == 13 and all(s.refill for s in out)


def test_refill_target_key_matching_no_fn_is_reported(tmp_path: Path) -> None:
    """A frontier key that resolves to no enumerated fn must be announced. It
    used to vanish here, which is how a crash-blamed fn stayed unfilled for whole
    runs: the blame was printed every sweep and then dropped without a trace."""
    from pipeline_core.domain.models import FnLocator
    fake = FakeFill(fns=[{"file": "src/a.rs", "fn_name": "realfn",
                          "container": "impl A", "sig_text": "fn realfn()"}])
    eng = _engine(fake, FillUnit.FN)
    keys = {FnLocator("src/a.rs", "realfn"), FnLocator("nowhere.rs", "ghostfn")}
    tests = FailureFrontier.of_direct({FnLocator("src/a.rs", "realfn"): {"t1"},
                                       FnLocator("nowhere.rs", "ghostfn"): {"t2"}})
    out = eng._collect_refill_targets(tmp_path, [], keys, tests)
    assert [s.fn_name for s in out] == ["realfn"]
    assert "phase_c.refill.dangling_keys" in fake.events
    assert any("nowhere.rs::ghostfn" in m and "match no enumerated fn" in m
               for m in fake.logs)


def test_collect_refill_targets_file_primary_stamps_dispatch_only_constructor(
        tmp_path: Path) -> None:
    """Per-fn twin of the impl stamp: under file-primary a coverable impl
    Error member that only has dispatch tests is still a refill. Negative
    control is the default route order, which keeps convicting_for."""
    from pipeline_core.domain.models import FnLocator
    fns = [
        {"file": "src/error.rs", "fn_name": "construct_from_boxed",
         "container": "impl Error",
         "sig_text": "fn construct_from_boxed()"},
    ]
    loc = FnLocator("src/error.rs", "construct_from_boxed")
    keys = {loc}
    tests = FailureFrontier.of_dispatch({loc: {"test_boxed_str"}})
    default = _engine(FakeFill(fns=fns), FillUnit.FN)
    assert default._collect_refill_targets(tmp_path, [], keys, tests) == [], (
        "negative control: default order on a coverable block omits the "
        "dispatch-only constructor")
    primary = _engine(FakeFill(fns=fns), FillUnit.FN,
                      route_order=RouteOrder.FILE_PRIMARY)
    out = primary._collect_refill_targets(tmp_path, [], keys, tests)
    assert [(s.fn_name, s.refill) for s in out] == [
        ("construct_from_boxed", True)]


def test_collect_refill_targets_stamps_crash_blamed_without_tests(
        tmp_path: Path) -> None:
    """Per-fn twin of the impl stamp: a crash-blamed filled body with no
    verdict is still a refill. Negative control is the same call without
    crash_blamed."""
    from pipeline_core.domain.models import FnLocator
    fns = [
        {"file": "src/error.rs", "fn_name": "into_boxed_dyn_error",
         "container": "impl Error",
         "sig_text": "fn into_boxed_dyn_error(self)"},
    ]
    eng = _engine(FakeFill(fns=fns), FillUnit.FN)
    loc = FnLocator("src/error.rs", "into_boxed_dyn_error")
    keys = {loc}
    tests = FailureFrontier.of_direct(
        {FnLocator("src/error.rs", "other"): {"t"}})
    assert eng._collect_refill_targets(tmp_path, [], keys, tests) == []
    out = eng._collect_refill_targets(
        tmp_path, [], keys, tests, crash_blamed=frozenset({loc}))
    assert [(s.fn_name, s.refill) for s in out] == [
        ("into_boxed_dyn_error", True)]


def test_stale_refill_dropped_when_its_test_passes_after_earlier_fills(
        tmp_path: Path) -> None:
    """The re-fill list is computed from the PREVIOUS iteration's test run; the
    impl/file routes write bodies in the SAME engine call before the per-fn
    pass consumes it. Measured 2026-08-11/12: the per-fn route re-derived
    `checked_div` for 99 s to a byte-identical body seconds after the file
    route had already fixed its test — 11-27% of every A/B/C run's wall. The
    probe (one cargo test between routes) must drop a target whose attributed
    tests all pass now."""
    from pipeline_core.domain.models import FnLocator
    ws = _mk_ws(tmp_path)
    key = FnLocator("src/a.rs", "wrongfn")
    fake = FakeFill(
        # Impl route landed 2 fills and left work → falls through to per-fn.
        impl_stats=ClusterFillResult(remaining=1, fns_filled=2,
                                     dispatched_fns=()),
        stub_records=[], stub_tuples=[],
        fns=[{"file": "src/a.rs", "fn_name": "wrongfn",
              "container": "impl A", "sig_text": "fn wrongfn()"}],
        run=(0, "test t1 ... ok\ntest result: ok. 1 passed; 0 failed"),
    )
    out = _engine(fake, FillUnit.IMPL).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={},
        tdd_frontier_keys={key},
        tdd_frontier_tests=FailureFrontier.of_direct({key: {"t1"}}),
        tdd_test_cmd="cargo test", skip_end_pass_test=True)
    assert "phase_c.refill.stale_dropped" in fake.events
    assert any("re-fill target(s) dropped" in m and "src/a.rs::wrongfn" in m
               for m in fake.logs)
    # Nothing left to do: the per-fn pass never dispatched the stale target.
    assert out.fns_filled == 2  # the impl carryover only


def test_refill_kept_when_its_test_still_fails_at_the_probe(
        tmp_path: Path) -> None:
    """NEGATIVE CONTROL for the stale-refill probe: the attributed test still
    fails after the earlier fills → the target stays a re-fill candidate (the
    pre-probe behaviour for a genuinely-wrong body must be unchanged)."""
    from pipeline_core.domain.models import FnLocator
    ws = _mk_ws(tmp_path)
    key = FnLocator("src/a.rs", "wrongfn")
    fake = FakeFill(
        impl_stats=ClusterFillResult(remaining=1, fns_filled=2,
                                     dispatched_fns=()),
        stub_records=[], stub_tuples=[],
        fns=[{"file": "src/a.rs", "fn_name": "wrongfn",
              "container": "impl A", "sig_text": "fn wrongfn()"}],
        run=(101, "test t1 ... FAILED\n"
                  "test result: FAILED. 0 passed; 1 failed"),
    )
    _engine(fake, FillUnit.IMPL).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={},
        tdd_frontier_keys={key},
        tdd_frontier_tests=FailureFrontier.of_direct({key: {"t1"}}),
        tdd_test_cmd="cargo test", skip_end_pass_test=True)
    assert "phase_c.refill.stale_dropped" not in fake.events
    assert any("behavioral re-fill target(s)" in m for m in fake.logs)


def test_refill_kept_when_its_test_is_dangling_at_the_probe(
        tmp_path: Path) -> None:
    """A SIGABRT emits no FAILED line. The probe used to treat that as
    "already pass" and drop the blamed unit — run `1911637-d0fab212`
    iters 3–5, `test_large_alignment` dangling, `downcast_ref` and
    `object_downcast` dropped. `still_open_names` is the third cargo
    outcome; a unit whose `tests_for` contains the dangling name stays."""
    from pipeline_core.domain.models import FnLocator
    ws = _mk_ws(tmp_path)
    key = FnLocator("src/error.rs", "object_downcast")
    fake = FakeFill(
        impl_stats=ClusterFillResult(remaining=1, fns_filled=2,
                                     dispatched_fns=()),
        stub_records=[], stub_tuples=[],
        fns=[{"file": "src/error.rs", "fn_name": "object_downcast",
              "container": "", "sig_text": "fn object_downcast()"}],
        run=(101,
             "    Running tests/test_downcast.rs "
             "(target/debug/deps/test_downcast-abc)\n"
             "running 2 tests\n"
             "test test_downcast ... ok\n"
             "test test_large_alignment ...\n"),
    )
    _engine(fake, FillUnit.IMPL).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={},
        tdd_frontier_keys={key},
        tdd_frontier_tests=FailureFrontier.of_direct(
            {key: {"test_large_alignment"}}),
        tdd_test_cmd="cargo test", skip_end_pass_test=True)
    assert "phase_c.refill.stale_dropped" not in fake.events
    assert any("behavioral re-fill target(s)" in m for m in fake.logs)


def _cluster(name: str = "stable"):
    from pipeline_core.domain.models import BuildCluster
    return BuildCluster(name=name, env_extra={}, tarpaulin_exclude_files=[],
                        excluded_cfg=[], active_cfg=[], prompt_nightly_rule="")


def test_cfg_dead_stub_is_refused_at_dispatch(tmp_path: Path) -> None:
    """One cfg policy across routes. The impl/file routes refuse a splice at a
    fn whose every stub occurrence is cfg-gated out of the cluster; on
    2026-08-11/12 the per-fn DISPATCH side never consulted that verdict and
    every A/B/C arm spent 90-147 s filling `src/lib.rs::cap_limit` into a
    build that never compiles it (the same run's file route refused the
    identical target as `not_in_cluster_cfg`). The dispatch refusal keys on
    the same `cluster_inactive_stub_names` verdict the splice uses."""
    ws = _mk_ws(tmp_path)
    dead = {"file": "src/lib.rs", "fn_name": "cap_limit",
            "sig_text": "fn cap_limit()"}
    fake = FakeFill(stub_tuples=[])
    # Call sequence: cluster_inactive_stub_names scans (every, then active —
    # the verdict says cap_limit is inactive), then the per-fn route's plan
    # scan and the pass scan both return the record anyway (the observed
    # record-source drift that produced the live fill). The dispatch filter
    # must refuse it.
    seq: list[list[dict[str, Any]]] = [[dead], [], [dead], [dead]]
    fake.scan_stub_records = (  # type: ignore[method-assign]
        lambda d, c: list(seq.pop(0)) if seq else [])
    out = _engine(fake, FillUnit.FN).run(
        ws, ws / "src", [], "m", "stable", cluster=_cluster(),
        cluster_env={}, skip_end_pass_test=True)
    assert "phase_c.dispatch.cfg_dead_refused" in fake.events
    assert any("refused 1 dispatch(es) at cfg-dead stub(s)" in m and
               "src/lib.rs::cap_limit" in m for m in fake.logs)
    assert out.fns_filled == 0 and out.attempts == 0  # no call was spent


def test_cfg_live_stub_is_not_refused_at_dispatch(tmp_path: Path) -> None:
    """NEGATIVE CONTROL: when the verdict and the pass scan agree the stub is
    active, the dispatch filter stays out of the way."""
    ws = _mk_ws(tmp_path)
    live = {"file": "src/lib.rs", "fn_name": "answer",
            "sig_text": "fn answer()"}
    fake = FakeFill(stub_tuples=[], run=(0, ""))
    seq: list[list[dict[str, Any]]] = [[live], [live], [live]]
    fake.scan_stub_records = (  # type: ignore[method-assign]
        lambda d, c: list(seq.pop(0)) if seq else [])
    _engine(fake, FillUnit.FN).run(
        ws, ws / "src", [], "m", "stable", cluster=_cluster(),
        cluster_env={}, skip_end_pass_test=True)
    assert "phase_c.dispatch.cfg_dead_refused" not in fake.events


def test_engine_defers_impl_route_for_a_free_fn_stub(tmp_path: Path) -> None:
    """A frontier stub the impl route never attempts (free fn) must be filled
    BEFORE any impl-route re-fill: its panic aborts the test binary, so every
    re-fill judged against that binary is reverted as a sibling crash."""
    from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
    ws = _mk_ws(tmp_path)
    free = FnRecord(file="src/e.rs", fn_name="drop_rest")
    blocks = [ImplBlock(file="src/e.rs", container="", members=(free,),
                        stub_members=(free,))]
    key = FnLocator("src/e.rs", "drop_rest")
    other = FnLocator("src/e.rs", "wrongfn")
    fake = FakeFill(cluster_blocks=blocks, stub_records=[], stub_tuples=[],
                    impl_stats=ClusterFillResult(remaining=0, fns_filled=99))
    out = _engine(fake, FillUnit.IMPL).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={},
        tdd_frontier_keys={key, other},
        tdd_frontier_tests=FailureFrontier.of_direct({other: {"t1"}}),
        skip_end_pass_test=True)
    # The impl path's scripted result must NOT be what came back.
    assert out.fns_filled != 99
    assert "phase_c.dispatch.impl_route_deferred" in fake.events


def test_the_deferral_keeps_other_frontier_stubs_in_the_batch(
        tmp_path: Path) -> None:
    """THE FIX. The deferral's premise is about RE-fills — a body landed while a
    sibling is `unimplemented!()` is reverted as a sibling crash — and it was
    implemented as `tdd_frontier_keys = set(_impl_deferred_stubs)`, which throws
    away every other frontier key including the ones that are themselves stubs.
    A first fill of a stub cannot be undone by a sibling panic (there is no body
    to lose) and it is how a blocker stops being one.

    Run `1920662-40f711b2` iteration 6 (2026-08-17 05:14Z): the frontier held 41,
    the deferral rewrote it to 8 blockers, all 8 landed and all 8 logged
    `kept ... (compiles; N attributed test(s) blocked by still-stubbed
    sibling(s))` — zero verdicts could move by construction and the sweep read
    33/105 twice. The driver's own line at the end of that iteration named what
    they were waiting on: `src/context.rs::ext_context, src/error.rs::drop,
    src/error.rs::from, src/kind.rs::new`, all stubs, all impl members the impl
    route would have claimed, none dispatched once in the run's 7 iterations,
    10 hours and 191 model calls."""
    from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
    ws = _mk_ws(tmp_path)
    free = FnRecord(file="src/e.rs", fn_name="drop_rest")
    blocked = FnRecord(file="src/e.rs", fn_name="new")
    blocks = [ImplBlock(file="src/e.rs", container="", members=(free,),
                        stub_members=(free,)),
              ImplBlock(file="src/e.rs", container="impl E",
                        members=(blocked,), stub_members=(blocked,))]
    key = FnLocator("src/e.rs", "drop_rest")
    producer = FnLocator("src/e.rs", "new")          # a STUB the impl route owns
    refill = FnLocator("src/e.rs", "wrongfn")        # filled, wrong body
    fake = FakeFill(cluster_blocks=blocks, stub_records=[], stub_tuples=[],
                    impl_stats=ClusterFillResult(remaining=0, fns_filled=99))
    # The engine asks the workspace which fns are live stubs; both the blocker
    # and the producer are, `wrongfn` is not.
    fake.scan_stub_records = (  # type: ignore[method-assign]
        lambda d, c: [{"file": k.file, "fn_name": k.fn_name}
                      for k in (key, producer)])
    _engine(fake, FillUnit.IMPL).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={},
        tdd_frontier_keys={key, producer, refill},
        tdd_frontier_tests=FailureFrontier.of_direct({refill: {"t1"}}),
        skip_end_pass_test=True)
    assert "phase_c.dispatch.impl_route_deferred" in fake.events
    _kept = next(d.get("kept_stubs") for ev, d in fake.event_data
                 if ev == "phase_c.dispatch.impl_route_deferred")
    assert _kept == ["src/e.rs::new"], (
        f"the producer the blocker is waiting on was dropped: {_kept}")


def test_negative_control_the_deferral_still_withholds_the_refills(
        tmp_path: Path) -> None:
    """Without this the case above proves nothing: an engine that simply stopped
    deferring would also keep the producer. The RE-fill target — a filled body
    with a failing test, which is exactly what a sibling panic reverts — must
    still be withheld, so the deferral's own premise is intact."""
    from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
    ws = _mk_ws(tmp_path)
    free = FnRecord(file="src/e.rs", fn_name="drop_rest")
    blocks = [ImplBlock(file="src/e.rs", container="", members=(free,),
                        stub_members=(free,))]
    key = FnLocator("src/e.rs", "drop_rest")
    refill = FnLocator("src/e.rs", "wrongfn")
    fake = FakeFill(cluster_blocks=blocks, stub_records=[], stub_tuples=[],
                    impl_stats=ClusterFillResult(remaining=0, fns_filled=99))
    fake.scan_stub_records = (  # type: ignore[method-assign]
        lambda d, c: [{"file": key.file, "fn_name": key.fn_name}])
    out = _engine(fake, FillUnit.IMPL).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={},
        tdd_frontier_keys={key, refill},
        tdd_frontier_tests=FailureFrontier.of_direct({refill: {"t1"}}),
        skip_end_pass_test=True)
    assert out.fns_filled != 99, "the impl route must still stand down"
    _kept = next(d.get("kept_stubs") for ev, d in fake.event_data
                 if ev == "phase_c.dispatch.impl_route_deferred")
    assert _kept == [], (
        "a re-fill target is not a stub and must stay withheld")


def test_engine_runs_impl_route_when_every_stub_is_covered(tmp_path: Path) -> None:
    from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock
    ws = _mk_ws(tmp_path)
    m = FnRecord(file="src/e.rs", fn_name="inner")
    blocks = [ImplBlock(file="src/e.rs", container="impl E", members=(m,),
                        stub_members=(m,))]
    stats = ClusterFillResult(remaining=0, fns_filled=3)
    fake = FakeFill(cluster_blocks=blocks, impl_stats=stats)
    out = _engine(fake, FillUnit.IMPL).run(
        ws, ws / "src", [], "m", "t", cluster=None, cluster_env={},
        tdd_frontier_keys={FnLocator("src/e.rs", "inner")},
        tdd_frontier_tests=FailureFrontier.of_direct({}), skip_end_pass_test=True)
    assert out is stats
    assert "phase_c.dispatch.impl_route_deferred" not in fake.events


# ---- crash-blocked units (sibling's fault) and crash-evidence re-fills ----
#
# The live burn, 2026-08-10, run on `anyhow`: `src/context.rs::StdErrorforE`
# was reverted for `binary crashed: ['tests/test_context.rs'] (net -6)` five
# times across two retro rounds (~4-6 model calls each) while ASAN blamed
# `src/error.rs::object_drop` throughout — and the eight crash-evidence
# re-fills of the block holding `object_drop` were all reverted for no strict
# improvement, because a correct fix of a latent fault moves no test while
# its activating sibling is crash-reverted beside it.

def _crash_only_regression() -> tuple[RegressionComparison, str]:
    return (RegressionComparison(
        newly_red=frozenset(), newly_crashed=("tests/test_context.rs",),
        excluded_crashed=(), not_run=(), measured=(),
        base_pass=6, cur_pass=0, base_executed=6, cur_executed=0),
        "crash detail")


class RecordingFill(FakeFill):
    """FakeFill that counts model asks and retro diagnoses and records
    conviction saves — the three costs the crash-block exists to stop."""

    def __init__(self, **scripted: Any) -> None:
        super().__init__(**scripted)
        self.ask_calls = 0
        self.diagnose_calls = 0
        self.saved: list[Any] = []

    def ask_per_fn_body(self, *a: Any, **k: Any) -> tuple[str | None, str]:
        self.ask_calls += 1
        return super().ask_per_fn_body(*a, **k)

    def diagnose(self, **k: Any) -> dict[str, Any]:
        self.diagnose_calls += 1
        return {"signal": "", "category": "GENUINELY_HARD", "guidance": "g"}

    def save(self, *a: Any) -> None:
        self.saved.append(a)

    def retro_key(self, f: str, fn: str, c: str) -> str:
        return f"{f}::{c}::{fn}"


def test_crash_only_revert_with_outside_blame_charges_nothing(
        tmp_path: Path) -> None:
    from dataclasses import replace

    from pipeline_core.domain.models import FnLocator
    ws = _mk_ws(tmp_path)
    (ws / "src" / "other.rs").write_text("fn object_drop() { old }\n")
    blocked: dict[str, str] = {}
    ctx = replace(_ctx(ws),
                  crash_blamed=frozenset({FnLocator("other.rs", "object_drop")}),
                  crash_blocked=blocked)
    fake = RecordingFill(run=(0, ""), regr=_crash_only_regression(),
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert not res.filled
    assert fake.diagnose_calls == 0, "no retrospective for a sibling's fault"
    assert not fake.saved, "no conviction for a sibling's fault"
    assert blocked, "the unit must be marked crash-blocked"
    assert any("crash_blocked" in ev for ev in fake.events)
    # The next dispatch skips without a model call…
    asks = fake.ask_calls
    res2 = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert not res2.filled and fake.ask_calls == asks
    # …until the blamed file's content moves, which re-admits the unit.
    (ws / "src" / "other.rs").write_text("fn object_drop() { fixed }\n")
    _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert fake.ask_calls > asks


def test_crash_only_revert_without_blame_still_burns_the_old_way(
        tmp_path: Path) -> None:
    """The negative control, by injection: with the blame channel empty the
    exemption cannot fire, and the pre-fix behaviour reproduces — the unit
    re-enters retro and writes a conviction for a crash it did not cause.
    If this stops reproducing, the control has drifted and the test above
    proves nothing."""
    from dataclasses import replace
    ws = _mk_ws(tmp_path)
    ctx = replace(_ctx(ws), crash_blamed=None, crash_blocked={})
    fake = RecordingFill(run=(0, ""), regr=_crash_only_regression(),
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert not res.filled
    assert fake.diagnose_calls >= 1
    assert fake.saved, "the false conviction is exactly what the fix stops"


def test_crash_evidence_refill_is_kept_on_non_regression(
        tmp_path: Path) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from pipeline_core.domain.models import FnLocator
    from pipeline_core.domain.test_report import U2Decision, U2Verdict
    ws = _mk_ws(tmp_path)
    baseline = SimpleNamespace(passed_names=frozenset({"t_pass"}),
                               ignored_names=frozenset())
    reject = U2Verdict(U2Decision.REJECT,
                       self_failures=frozenset({"t_latent"}),
                       detail="Your body ABORTED the test binary")
    ctx = replace(_ctx(ws), crash_evidence={
        FnLocator("lib.rs", "answer"): "AddressSanitizer: heap-use-after-free"})
    fake = RecordingFill(run=(0, ""), u2=reject, regr=None,
                         regr_base=baseline, body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert res.filled, (
        "a crash-evidence re-fill whose self-failures were all failing at "
        "baseline regressed nothing — kept, judged at the next sweep")
    assert any("crash-evidence re-fill KEPT" in m for m in fake.logs)


def test_the_same_rejection_without_evidence_still_reverts(
        tmp_path: Path) -> None:
    """Control 1: no crash evidence → the strict gate stands (the pre-fix
    revert reproduces)."""
    from types import SimpleNamespace

    from pipeline_core.domain.test_report import U2Decision, U2Verdict
    ws = _mk_ws(tmp_path)
    baseline = SimpleNamespace(passed_names=frozenset({"t_pass"}),
                               ignored_names=frozenset())
    reject = U2Verdict(U2Decision.REJECT,
                       self_failures=frozenset({"t_latent"}), detail="red")
    fake = RecordingFill(run=(0, ""), u2=reject, regr=None,
                         regr_base=baseline, body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_ctx(ws))
    assert not res.filled


def test_a_real_regression_reverts_even_with_crash_evidence(
        tmp_path: Path) -> None:
    """Control 2: a self-failure that was PASSING at baseline is a true
    regression — the evidence buys no keep."""
    from dataclasses import replace
    from types import SimpleNamespace

    from pipeline_core.domain.models import FnLocator
    from pipeline_core.domain.test_report import U2Decision, U2Verdict
    ws = _mk_ws(tmp_path)
    baseline = SimpleNamespace(passed_names=frozenset({"t_pass"}),
                               ignored_names=frozenset())
    reject = U2Verdict(U2Decision.REJECT,
                       self_failures=frozenset({"t_pass"}), detail="red")
    ctx = replace(_ctx(ws), crash_evidence={
        FnLocator("lib.rs", "answer"): "AddressSanitizer: heap-use-after-free"})
    fake = RecordingFill(run=(0, ""), u2=reject, regr=None,
                         regr_base=baseline, body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert not res.filled


# ---- sibling-blocked U2 rejection: keep-and-defer BEFORE the fix loop ----
# The dispatch-time deferral cannot cover a hub: a stub unit is always ACTIVE
# (producers must fill), so its first body is judged while the spokes its
# tests also need are still hollow — that failure is attempt-invariant and
# the fix loop must not spend calls on it (3.73 call-hours across 6 units on
# the anyhow run `298540-f792975a`, 2026-08-12).

def _hub_ctx(ws: Path) -> PerFnPassCtx:
    from dataclasses import replace

    from pipeline_core.domain.models import FnLocator
    frontier = FailureFrontier.of_direct({
        FnLocator("lib.rs", "answer"): {"t_hub"},
        FnLocator("lib.rs", "spoke"): {"t_hub"},
    })
    return replace(_ctx(ws), tdd_frontier_tests=frontier,
                   still_stubbed=frozenset({FnLocator("lib.rs", "answer"),
                                            FnLocator("lib.rs", "spoke")}))


def _hub_reject() -> Any:
    from pipeline_core.domain.test_report import U2Decision, U2Verdict
    return U2Verdict(U2Decision.REJECT,
                     self_failures=frozenset({"t_hub"}), detail="red")


def test_sibling_blocked_rejection_is_kept_with_one_call(
        tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=None,
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_hub_ctx(ws))
    assert res.filled, "the compiling body is kept, not re-prompted"
    assert res.blocked_by, "the deferral is recorded on the result"
    assert fake.ask_calls == 1, "zero further calls — the fix loop never ran"
    assert fake.diagnose_calls == 0 and not fake.saved, (
        "no retrospective, no conviction during a sibling-blocked window")
    assert "phase_c.fn.u2_sibling_blocked" in fake.events
    assert any("fix loop skipped" in m for m in fake.logs)


def test_the_same_rejection_unblocked_still_burns_the_fix_loop(
        tmp_path: Path) -> None:
    """Negative control, by injection: with `still_stubbed` empty (spoke
    filled) the same REJECT re-enters the fix loop and the pre-fix burn
    reproduces. If this stops failing U2 repeatedly, the test above proves
    nothing."""
    from dataclasses import replace

    from pipeline_core.domain.models import FnLocator
    ws = _mk_ws(tmp_path)
    frontier = FailureFrontier.of_direct({
        FnLocator("lib.rs", "answer"): {"t_hub"},
        FnLocator("lib.rs", "spoke"): {"t_hub"},
    })
    ctx = replace(_ctx(ws), tdd_frontier_tests=frontier,
                  still_stubbed=frozenset({FnLocator("lib.rs", "answer")}))
    fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=None,
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=ctx)
    assert not res.filled
    assert fake.ask_calls > 1, "the fix loop re-prompted (the measured burn)"


# ---- coverage-unreached U2 rejection: the same refusal, measured ----
# The stub predicate above is a special case. On cluster `stable`, run
# `2450551-9a6a8683` (2026-08-30), one stub remained in 209 fillable units so
# nothing was sibling-blocked, and the fix loop still spent 4,550 s over five
# attempts and two retrospective rounds rewriting `src/lib.rs::Ok` for
# `test_trailer` — a test that enters 24 bodies, none of them `Ok`. The live
# records are pinned in `tests/test_coverage_reachability.py`; these pin the
# engine wiring.

def _cov_ctx(ws: Path) -> PerFnPassCtx:
    """A REJECT with nothing sibling-blocked — only coverage can refuse it."""
    from dataclasses import replace

    from pipeline_core.domain.models import FnLocator
    frontier = FailureFrontier.of_direct({
        FnLocator("lib.rs", "answer"): {"t_hub"},
    })
    return replace(_ctx(ws), tdd_frontier_tests=frontier,
                   still_stubbed=frozenset())


def test_coverage_unreached_rejection_is_kept_with_one_call(
        tmp_path: Path) -> None:
    from pipeline_core.domain.coverage_slice import CoverageSlice, bind
    from pipeline_core.domain.models import FnSite
    ws = _mk_ws(tmp_path)
    bind(CoverageSlice(owners={
        "t_hub": frozenset({FnSite("lib.rs", "elsewhere", "impl Hub")})}))
    try:
        fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=None,
                             body=("Ok(())", "R"))
        res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                                  ctx=_cov_ctx(ws))
    finally:
        bind(None)
    assert res.filled, "the compiling body is kept, not re-prompted"
    assert fake.ask_calls == 1, "zero further calls — the fix loop never ran"
    assert fake.diagnose_calls == 0 and not fake.saved, (
        "no retrospective, no conviction on a test that never enters the body")
    assert "phase_c.fn.u2_coverage_unreached" in fake.events
    assert any("no failing attributed test executes this body" in m
               for m in fake.logs)


def test_the_same_rejection_unmeasured_still_burns_the_fix_loop(
        tmp_path: Path) -> None:
    """Negative control: with nothing bound — every run before this change —
    the identical REJECT re-enters the fix loop and the burn reproduces."""
    from pipeline_core.domain.coverage_slice import bind
    ws = _mk_ws(tmp_path)
    bind(None)
    fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=None,
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_cov_ctx(ws))
    assert not res.filled
    assert fake.ask_calls > 1, "the fix loop re-prompted (the measured burn)"


def test_a_unit_the_failing_test_does_execute_still_enters_the_fix_loop(
        tmp_path: Path) -> None:
    """Over-refusal control: the live counterpart is `src/error.rs::deref_mut`,
    which both deref_mut tests do enter — the gate must decline there."""
    from pipeline_core.domain.coverage_slice import CoverageSlice, bind
    from pipeline_core.domain.models import FnSite
    ws = _mk_ws(tmp_path)
    bind(CoverageSlice(owners={
        "t_hub": frozenset({FnSite("lib.rs", "answer", "impl Hub"),
                            FnSite("lib.rs", "elsewhere", "impl Hub")})}))
    try:
        fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=None,
                             body=("Ok(())", "R"))
        res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                                  ctx=_cov_ctx(ws))
    finally:
        bind(None)
    assert not res.filled
    assert fake.ask_calls > 1, "the loop has real work and must do it"


def test_a_conviction_is_refused_when_no_convictor_executes_the_body(
        tmp_path: Path) -> None:
    """The write-side arm. The gate above never sees a COMPILE-failure loop —
    it lives in the U2 REJECT branch — so a conviction can still be reached on
    tests that do not execute the unit. Persisting it parks the unit AND
    stores a lesson every later prompt obeys: on run `2450551-9a6a8683` the
    record written here told the model to make `Ok<T>(v)` yield `Err(v)`,
    which cannot typecheck, and it reached disk as `fully_exhausted` where
    nothing could retire it (the retirement rules want compile evidence, and
    a body that never compiles is never adopted)."""
    from dataclasses import replace

    from pipeline_core.domain.coverage_slice import CoverageSlice, bind
    from pipeline_core.domain.models import FnLocator, FnSite
    ws = _mk_ws(tmp_path)
    frontier = FailureFrontier.of_direct({
        FnLocator("lib.rs", "answer"): {"t_hub"},
    })
    ctx = replace(_ctx(ws), tdd_frontier_tests=frontier,
                  still_stubbed=frozenset())
    bind(CoverageSlice(owners={
        "t_hub": frozenset({FnSite("lib.rs", "elsewhere", "impl Hub")})}))
    try:
        fake = RecordingFill(run=(1, "error[E0308]: mismatched types"),
                             body=("Ok(())", "R"))
        res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                                  ctx=ctx)
    finally:
        bind(None)
    assert not res.filled
    assert fake.saved, "the record is still written — only its verdict changes"
    stored = fake.saved[-1][-1]
    rec = stored[next(iter(stored))]
    assert rec["fully_exhausted"] is False, "the unit is not parked"
    assert rec["guidance"] == "", (
        "and stores no lesson: parking is reversible, stored guidance steers "
        "every future prompt for the unit")
    assert "phase_c.retro.coverage_unreached" in fake.events


def test_without_the_reading_the_same_conviction_is_persisted(
        tmp_path: Path) -> None:
    """Negative control for the write-side arm: unbound, the identical run
    parks the unit and stores the lesson — the pre-fix behaviour."""
    from dataclasses import replace

    from pipeline_core.domain.coverage_slice import bind
    from pipeline_core.domain.models import FnLocator
    ws = _mk_ws(tmp_path)
    frontier = FailureFrontier.of_direct({
        FnLocator("lib.rs", "answer"): {"t_hub"},
    })
    ctx = replace(_ctx(ws), tdd_frontier_tests=frontier,
                  still_stubbed=frozenset())
    bind(None)
    fake = RecordingFill(run=(1, "error[E0308]: mismatched types"),
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=ctx)
    assert not res.filled
    assert fake.saved
    stored = fake.saved[-1][-1]
    rec = stored[next(iter(stored))]
    assert rec["fully_exhausted"] is True
    assert rec["guidance"] == "g"
    assert "phase_c.retro.coverage_unreached" not in fake.events


def test_a_partially_blocked_rejection_still_enters_the_fix_loop(
        tmp_path: Path) -> None:
    """One failing test the unit owns outright -> the loop has real work; the
    deferral must not fire on a partial block."""
    from dataclasses import replace

    from pipeline_core.domain.models import FnLocator
    from pipeline_core.domain.test_report import U2Decision, U2Verdict
    ws = _mk_ws(tmp_path)
    frontier = FailureFrontier.of_direct({
        FnLocator("lib.rs", "answer"): {"t_hub", "t_own"},
        FnLocator("lib.rs", "spoke"): {"t_hub"},
    })
    ctx = replace(_ctx(ws), tdd_frontier_tests=frontier,
                  still_stubbed=frozenset({FnLocator("lib.rs", "answer"),
                                           FnLocator("lib.rs", "spoke")}))
    reject = U2Verdict(U2Decision.REJECT,
                       self_failures=frozenset({"t_hub", "t_own"}),
                       detail="red")
    fake = RecordingFill(run=(0, ""), u2=reject, regr=None,
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=ctx)
    assert not res.filled and fake.ask_calls > 1


# ---- an unpayable unit stops on its first failed attempt ----
# The two gates above only cleared the U2 signature. When the kept body then
# reddened a sibling, the regression gate reverted it and the failure path
# re-prompted; after the stall bound the retrospective refused the conviction
# as coverage-unreached and still re-entered the loop. Run `33522-b27db63d`
# (2026-09-17) rewrote one such unit 11 times in 41 minutes, each body
# reddening a passing sibling test. On this harness the pre-fix wiring spent
# 50 model asks and 25 retrospectives (24 re-entries) on the same shape.


class _ImprovingRetro(RecordingFill):
    """A retrospective that neither parks the unit nor claims it is hard —
    the round that is allowed to re-enter the loop."""

    def diagnose(self, **k: Any) -> dict[str, Any]:
        self.diagnose_calls += 1
        return {"signal": "", "category": "WRONG_TYPE_CHOICE", "guidance": "g"}


def _bound(entered: bool) -> Any:
    from pipeline_core.domain.coverage_slice import CoverageSlice
    from pipeline_core.domain.models import FnSite
    sites = {FnSite("lib.rs", "elsewhere", "impl Hub")}
    if entered:
        sites.add(FnSite("lib.rs", "answer", "impl Hub"))
    return CoverageSlice(owners={"t_hub": frozenset(sites)})


def _run_bound(fake: RecordingFill, ws: Path, ctx: PerFnPassCtx,
               entered: bool) -> Any:
    from pipeline_core.domain.coverage_slice import bind
    bind(_bound(entered))
    try:
        return _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                                   ctx=ctx)
    finally:
        bind(None)


def test_an_unreached_unit_whose_body_regresses_a_sibling_stops_after_one_call(
        tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=_regressed(),
                         body=("Ok(())", "R"))
    res = _run_bound(fake, ws, _cov_ctx(ws), entered=False)
    assert not res.filled
    assert fake.ask_calls == 1, "no re-prompt after the sibling regression"
    assert fake.diagnose_calls == 0 and not fake.saved, (
        "no retrospective and no conviction for a unit the test never enters")
    assert "phase_c.fn.unpayable_stop" in fake.events
    assert fake.s.get("reverts"), "the regressing body is reverted"
    assert res.fill_class == "mis"


def test_a_sibling_blocked_unit_whose_body_regresses_a_sibling_stops_too(
        tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=_regressed(),
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_hub_ctx(ws))
    assert not res.filled
    assert fake.ask_calls == 1 and fake.diagnose_calls == 0 and not fake.saved
    assert "phase_c.fn.unpayable_stop" in fake.events


def test_control_a_unit_the_test_enters_still_retries_after_a_regression(
        tmp_path: Path) -> None:
    """Over-refusal control: the same regression on a unit the failing test
    does execute is real work — the loop re-prompts."""
    ws = _mk_ws(tmp_path)
    fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=_regressed(),
                         body=("Ok(())", "R"))
    res = _run_bound(fake, ws, _cov_ctx(ws), entered=True)
    assert not res.filled
    assert fake.ask_calls > 1
    assert "phase_c.fn.unpayable_stop" not in fake.events


def test_a_retrospective_refused_as_unreached_does_not_re_enter_the_loop(
        tmp_path: Path) -> None:
    """The write-side arm on a compile-failure loop, where no U2 gate ran:
    the retrospective bought no evidence about the unit, so it is neither a
    conviction nor a reason for another round."""
    from dataclasses import replace
    ws = _mk_ws(tmp_path)
    ctx = replace(_cov_ctx(ws))
    fake = _ImprovingRetro(run=(1, "error[E0308]: mismatched types"),
                           body=("Ok(())", "R"))
    res = _run_bound(fake, ws, ctx, entered=False)
    assert not res.filled
    assert fake.diagnose_calls == 1
    assert "phase_c.fn.retro_retry" not in fake.events


def test_control_an_improving_retrospective_on_an_entered_unit_re_enters(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Control for the arm above: with the test entering the body, the same
    first-round retrospective re-enters the loop, so the refusal is the
    coverage reading and not a broken retry path."""
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "2")
    ws = _mk_ws(tmp_path)
    fake = _ImprovingRetro(run=(1, "error[E0308]: mismatched types"),
                           body=("Ok(())", "R"))
    res = _run_bound(fake, ws, _cov_ctx(ws), entered=True)
    assert not res.filled
    assert "phase_c.fn.retro_retry" in fake.events
    assert fake.diagnose_calls == 2


# ---- a vetoed conviction is not a bought re-entry ----
# Run `2657150-3139f958` (2026-09-18): one per-fn unit read `improved=False`
# at the same floor on rounds 2 to 7; each round's conviction was vetoed (the
# failure was the tree's, not the body's) and each veto re-entered the fix
# loop, holding one worker for 1h40m after its run's share was spent. The
# shape here is invented: an unchanging compile error whose conviction a veto
# arm refuses (a splice diagnostic), against a store that keeps the floor.


class _FlatFloorRetro(_ImprovingRetro):
    """Keeps what the retrospective saved, so round 2 compares against
    round 1's floor instead of re-baselining on an empty store."""

    def __init__(self, **scripted: Any) -> None:
        super().__init__(**scripted)
        self.store: dict[str, Any] = {}

    def load(self, *a: Any) -> dict[str, Any]:
        return dict(self.store)

    def save(self, *a: Any) -> None:
        super().save(*a)
        self.store = dict(a[-1])


_SPLICE_ERR = ("error: `self` parameter is only allowed in associated "
               "functions\n --> src/lib.rs:1:15")


def test_an_unimproved_round_whose_conviction_is_vetoed_does_not_re_enter(
        tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "6")
    ws = _mk_ws(tmp_path)
    fake = _FlatFloorRetro(run=(1, _SPLICE_ERR), err=_SPLICE_ERR,
                           body=("Ok(())", "R"))
    res = _run_bound(fake, ws, _cov_ctx(ws), entered=True)
    assert not res.filled
    assert fake.diagnose_calls == 2, (
        "round 1 baselines and re-enters; round 2 reads the same floor and "
        "stops, whatever the veto decided about conviction")
    assert fake.events.count("phase_c.fn.retro_retry") == 1
    assert any("NOT exhausting" in m for m in fake.logs), (
        "the veto arm must have fired, or this proves nothing")


def test_control_the_pre_fix_gate_re_enters_every_vetoed_round(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Negative control: the retry gate without the improvement clause, the
    shape that shipped before 2026-09-18, spends every round up to the cap on
    the same flat floor."""
    import inspect

    from pipeline_core.application.reconstruct import fill_one_fn as mod
    src = inspect.getsource(mod)
    fixed = "_retry_informed = (_improved and not _full_exhaust"
    assert fixed in src
    old = compile(src.replace(fixed, "_retry_informed = (not _full_exhaust"),
                  mod.__file__, "exec")
    ns: dict[str, Any] = {"__name__": mod.__name__}
    exec(old, ns)
    monkeypatch.setenv("PER_FN_RETRO_ROUNDS_MAX", "6")
    ws = _mk_ws(tmp_path)
    fake = _FlatFloorRetro(run=(1, _SPLICE_ERR), err=_SPLICE_ERR,
                           body=("Ok(())", "R"))
    from pipeline_core.domain.coverage_slice import bind
    bind(_bound(True))
    try:
        ns["FillOneFn"](tel=fake, mut=fake, cargo=fake, prompt=fake,
                        retros=fake, tests=fake, stall=fake, caps=_CAPS).run(
            1, Stub(file="lib.rs", fn_name="answer"), ctx=_cov_ctx(ws))
    finally:
        bind(None)
    assert fake.diagnose_calls == 6


# ---- the exhaustion summary agrees with the salvage decision ----
# On the anyhow run `298540-f792975a` (2026-08-12), `src/chain.rs::new`
# logged `KEEPING best compiling body` immediately followed by
# `reverted after 2 attempt(s)` while the on-disk file held the body.

def test_exhaustion_summary_says_kept_when_the_salvage_kept(
        tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=None,
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_ctx(ws))
    assert not res.filled, "exhaustion still reports no verdict-worthy fill"
    _summaries = [m for m in fake.logs if "fix loop exhausted after" in m]
    assert any("best compiling body KEPT" in m for m in _summaries)
    assert not any("reverted after" in m for m in fake.logs), (
        "the disposition summary must not contradict the salvage keep")


def test_exhaustion_summary_says_reverted_when_the_salvage_failed(
        tmp_path: Path) -> None:
    """Control: a salvage that would regress reverts, and the summary says
    so — the old line is correct exactly when the keep did not happen."""
    ws = _mk_ws(tmp_path)
    fake = RecordingFill(run=(0, ""), u2=_hub_reject(), regr=_regressed(),
                         body=("Ok(())", "R"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_ctx(ws))
    assert not res.filled
    assert any("reverted after" in m for m in fake.logs)
    assert not any("best compiling body KEPT" in m for m in fake.logs)


def _verdicts(fake: FakeFill) -> list[dict[str, Any]]:
    return [d for ev, d in fake.event_data if ev == "phase_c.verdict"]


def test_a_fill_with_no_test_observed_passing_is_not_a_positive_verdict(
        tmp_path: Path) -> None:
    """`U2Decision.ACCEPT` is "tests pass / gate off / inconclusive" — three
    states, one branch. Stamping POSITIVE on all three is how run
    `1064759-0f23fb68` (2026-08-15/16) recorded 50 successes across 7h48m in
    which the per-binary verdict vector never changed and no test flipped."""
    ws = _mk_ws(tmp_path)
    fake = FakeFill(run=(0, ""), regr=None)
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_ctx(ws))
    assert res.filled
    rows = _verdicts(fake)
    assert rows and rows[0]["disposition"] == "accept"
    assert rows[0]["outcome"] == "unmeasurable"
    assert "no attributed test was observed passing" in rows[0]["reason"]
    assert rows[0]["evidence_keys"] == []


def test_a_fill_that_turned_a_test_green_is_positive_and_names_it(
        tmp_path: Path) -> None:
    """The other half: with an attributed test observed PASSING in the fill's
    own scoped run, the row is POSITIVE and carries the name — which is what
    makes a per-unit payoff measurable at all."""
    from pipeline_core.domain.test_report import U2Verdict
    ws = _mk_ws(tmp_path)
    fake = FakeFill(run=(0, ""), regr=None,
                    u2=U2Verdict.accept(frozenset({"test_alpha"})))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_ctx(ws))
    assert res.filled
    rows = _verdicts(fake)
    assert rows and rows[0]["outcome"] == "positive"
    assert rows[0]["evidence_keys"] == ["test_alpha"]
    assert "1 attributed test(s) observed passing" in rows[0]["reason"]


def test_external_only_keep_names_the_blockers_in_log_event_and_verdict(
        tmp_path: Path) -> None:
    """Per-fn ACCEPT_EXTERNAL_ONLY used to log a count, emit no event, and
    write evidence_keys=[]. Run `155810-cbb3b9fc` had 23 keep_deferred fills
    and the only `phase_c.u2.external_only` event was from the impl route."""
    from pipeline_core.domain.models import FnSite
    from pipeline_core.domain.test_report import (
        FailureKind,
        TestFailure,
        U2Verdict,
    )
    ws = _mk_ws(tmp_path)
    blocker = FnSite("src/error.rs", "construct", "impl Error")
    ext = ((TestFailure("test_downcast", FailureKind.STUB_PANIC,
                        "src/error.rs", 1, 1, "not implemented"),
            blocker),)
    fake = FakeFill(run=(0, ""), regr=None,
                    u2=U2Verdict.accept_external_only(ext))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"),
                              ctx=_ctx(ws))
    assert res.filled
    assert blocker in res.blocked_by
    assert any("src/error.rs::construct" in m for m in fake.logs)
    evs = [d for e, d in fake.event_data if e == "phase_c.u2.external_only"]
    assert evs and "src/error.rs::construct" in evs[0]["blocking_fns"]
    rows = _verdicts(fake)
    assert rows and "src/error.rs::construct" in rows[0]["evidence_keys"]


def test_fill_one_skips_a_unit_whose_tests_binary_a_keep_crashed(tmp_path: Path) -> None:
    """Run `3500465-67c40e78` (2026-09-08): a session keep at 15:32 made a
    binary abort; a unit whose every attributed test is in that binary
    cannot move a verdict this fill. Skipped with no model call, unless it
    is the crash-blamed locator that can complete the binary."""
    from dataclasses import replace

    from pipeline_core.application.reconstruct import fill_reading
    from pipeline_core.domain.attribution import FailureFrontier
    from pipeline_core.domain.models import FnLocator
    from pipeline_core.domain.test_results import (
        RegressionComparison,
        TestStateReading,
        parse_binary_results,
    )

    suite = ("     Running tests/bin_x.rs (target/debug/deps/bin_x-0123456789abcdef)\n\n"
             "running 1 test\ntest t_x1 ... FAILED\n\n"
             "test result: FAILED. 0 passed; 1 failed; 0 ignored\n")
    fill_reading.reset_last_comparison()
    reading = TestStateReading(parse_binary_results(suite))
    fill_reading.note_last_reading(reading)
    label = reading.binaries[0].label
    fill_reading.note_kept_comparison(RegressionComparison(
        newly_red=frozenset(), newly_crashed=(), excluded_crashed=(),
        not_run=(), measured=(), base_pass=0, cur_pass=0,
        base_executed=1, cur_executed=0, crashed_all_red=(label,)))
    loc = FnLocator("lib.rs", "answer")
    frontier = FailureFrontier.of_direct({loc: {"t_x1"}})
    ws = _mk_ws(tmp_path)
    ctx = replace(_ctx(ws), tdd_frontier_tests=frontier)
    fake = FakeFill()
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx)
    assert not res.filled and res.attempts == 0
    assert "phase_c.fn.fill.skip" in fake.events
    # Negative control: the crash-blamed unit is the one that can complete
    # the binary — it is dispatched.
    ctx_blamed = replace(ctx, crash_blamed=frozenset({loc}))
    fake2 = FakeFill()
    _fill_one(fake2).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=ctx_blamed)
    assert "phase_c.fn.fill.skip" not in fake2.events
    fill_reading.reset_last_comparison()

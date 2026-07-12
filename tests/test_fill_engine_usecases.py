"""Phase-C fill engine use-cases (P2): FillOneFn + PhaseCFillEngine driven
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
from pipeline_core.domain.enums import FillUnit
from pipeline_core.domain.models import (
    ClusterFillResult,
    FnFixContext,
    Stub,
)
from pipeline_core.domain.reconstruction import SpliceOutcome


class FakeFill:
    """Implements every fill-engine role; records events and returns scripted
    values. Satisfies the Protocols structurally (same as `_PhaseCFillAdapter`)."""

    def __init__(self, **scripted: Any) -> None:
        self.s = scripted
        self.events: list[str] = []
        self.logs: list[str] = []
        self.lock = threading.Lock()

    # EngineTelemetry
    def log(self, msg: str) -> None: self.logs.append(msg)
    def emit(self, event: str, *, duration_ms: int | None = None,
             data: dict[str, Any] | None = None) -> None: self.events.append(event)
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
    def replace_fn_signature(self, *a: Any) -> bool: return False
    def extract_signature_block(self, *a: Any) -> str | None: return None
    def lock_for(self, tree: Path) -> threading.Lock: return self.lock

    # Cargo
    def run(self, *a: Any, **k: Any) -> tuple[int, str]:
        seq = self.s.get("run_seq")
        if seq:
            return seq.pop(0)
        return self.s.get("run", (0, ""))
    def build_test_cmd(self, *a: Any) -> str: return "cargo test"
    def u2_test_gate(self, *a: Any) -> Any:
        from pipeline_core.domain.test_report import U2Verdict
        return self.s.get("u2") or U2Verdict.accept()
    def test_regression_baseline(self, *a: Any) -> Any: return self.s.get("regr_base")
    def test_regression_gate(self, *a: Any) -> Any: return self.s.get("regr")
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
    def scan_stub_tuples(self, *a: Any) -> list[tuple[str, str]]:
        return self.s.get("stub_tuples", [])
    def enumerate_fns(self, *a: Any) -> Any: return self.s.get("fns", [])
    def enumerate_types(self, *a: Any) -> Any: return []
    def collect_impl_blocks(self, *a: Any) -> Any: return []
    def fn_container_index(self, *a: Any) -> dict[tuple[str, str], str]: return {}
    def fn_belongs_to_cluster(self, *a: Any, **k: Any) -> bool: return True
    def cluster_filter_type_methods(self, records: Any, cluster: Any) -> Any: return []
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
    # debt #17 residual: the Stub must thread the fn's cfg chain so the splice's
    # locate_fn_by_record cfg tier can pick the right one of two cfg-variant fns
    # sharing file+name+container+sig (the cap_limit pair).
    s = Stub.from_dict({"file": "lib.rs", "fn_name": "cap_limit",
                        "cfg_predicates": ["extra_cap"]})
    assert s.cfg_predicates == ("extra_cap",)
    assert s.to_dict()["cfg_predicates"] == ["extra_cap"]
    # Tolerant of a missing key (hand-built / AST-less fallback) → empty tuple.
    assert Stub.from_dict({"file": "lib.rs", "fn_name": "x"}).cfg_predicates == ()


# ---- FillOneFn branches ----

def test_fill_one_fully_exhausted_skips(tmp_path: Path) -> None:
    ws = _mk_ws(tmp_path)
    fake = FakeFill(exhausted=True)
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=_ctx(ws))
    assert not res.filled and res.attempts == 0
    assert "phase_c.fn.fill.skip" in fake.events


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
    # debt #46: an ordinary stub fill must NOT allow_nonstub (refuse a filled body).
    ws = _mk_ws(tmp_path)
    fake = FakeFill(loc_rec=("pub fn answer()", 0, 5, "pub fn answer()"),
                    splice=SpliceOutcome(True, "ok", 4),
                    run=(0, ""), u2=None, body=("42", "RESP"))
    res = _fill_one(fake).run(1, Stub(file="lib.rs", fn_name="answer"), ctx=_ctx(ws))
    assert res.filled
    assert fake.s["last_allow_nonstub"] is False


def test_fill_one_refill_splices_with_allow_nonstub(tmp_path: Path) -> None:
    # debt #46 point 1: a refill target replaces an already-filled body → the
    # splice MUST pass allow_nonstub=True (else target_not_stub refuses it).
    ws = _mk_ws(tmp_path)
    (ws / "src" / "lib.rs").write_text("pub fn answer() {\n 0\n}\n")
    fake = FakeFill(loc_rec=("pub fn answer()", 0, 5, "pub fn answer()"),
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

def _engine(fake: FakeFill, fill_unit: FillUnit) -> PhaseCFillEngine:
    cfg = EngineConfig(parallelism=1, fill_unit=fill_unit,
                       per_pass_test_fix_hard_cap=6, per_pass_test_fix_max_stalls=2,
                       pass_max_stalls=2, fix_err_cap=2000)
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
    # debt #46 point 1: a frontier fn that is NOT a stub but has a failing test
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
    tests = {FnLocator("src/a.rs", "wrongfn"): {"t1"},
             FnLocator("src/a.rs", "stubfn"): {"t2"}}
    out = eng._collect_refill_targets(tmp_path, stubs, keys, tests)
    assert [(s.fn_name, s.refill) for s in out] == [("wrongfn", True)]
    # No frontier map (legacy fn_walk driver) → never produces refill targets.
    assert eng._collect_refill_targets(tmp_path, stubs, None, None) == []
    assert eng._collect_refill_targets(tmp_path, stubs, keys, None) == []

"""Model calls that read no other call's reply run side by side, and the
process never has more calls on the server than it serves.

Exposed on run `336559-eb0222a5` (2026-11-07): every task 0d stage after the
book asked one call at a time (rule-probe judges about 9 minutes each, conc
1.00) on a server that serves 4 sequences, while the book next to them ran
on 4 lanes; each pool in the pipeline sized itself, and nothing bounded their
sum. Invented shapes throughout. What is asserted:

- `on_lanes`: results in item order at any width, lanes=1 is the serial loop,
  the first failing item's exception is the one raised;
- the process slot set (`llm/slots.py`) holds the calls on the server to its
  width however many pools ask at once, and emits each call's wait keyed on
  the prompt sha; control: a wider slot set lets the same two pools put 8 on;
- the readers that time a call from `llm.request` to `llm.response` take the
  wait off (`phase_cost.work_delta`, C10's concurrency count); control: a run
  without slot events is read as before;
- task 0d: the reach batches, the rule asks and the rule judges are in flight
  together at LANES=4 and one at a time at LANES=1, and the stage's output
  (suite, features, log line) is the same at both widths; the judge's `alone`
  call starts only after `together` returned;
- t1.5: a component's attempt-1 authoring is asked before the component
  ahead of it compiles, and the committed skeleton, the results and the set of
  prompts asked are those of the serial pass, retries included; control:
  lanes=1 asks nothing ahead.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from pipeline_core.application.author_layout import AuthorLayoutDeps, author_layout
from pipeline_core.application.lanes import on_lanes
from pipeline_core.application.tasks.task0d import run_rule_probes, run_task0d
from pipeline_core.audit import RunData, check_parallel_stream_integrity
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    Feature,
    GlossaryEntry,
    ObservableOutcome,
    Rule,
)
from pipeline_core.domain.behaviour_probe import ProbeResult
from pipeline_core.domain.models import TypeRecord
from pipeline_core.domain.phase_cost import work_delta
from pipeline_core.domain.private_surface import classify_private_surface
from pipeline_core.domain.rule_probe import judge_rule_on_captures
from pipeline_core.domain.text import PromptDigest
from pipeline_core.llm import Hooks, LLMController
from pipeline_core.llm.slots import reset_process_slots
from tests.test_author_layout import _has_pins
from tests.test_llm_controller import FakeTransport
from tests.test_task0d import _REPLY, _cluster, _World
from tests.test_task0d_reach import _FACTS, _HARVESTED, _REACH_REPLY


class _Peak:
    """Counts calls in flight; each call sleeps so overlapping ones meet."""

    def __init__(self, hold: float = 0.05) -> None:
        self.hold = hold
        self.now = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, fn: Callable[[], str]) -> str:
        with self._lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        try:
            # Not `time.sleep`: the suite's conftest makes it a no-op.
            threading.Event().wait(self.hold)
            return fn()
        finally:
            with self._lock:
                self.now -= 1


@pytest.fixture(autouse=True)
def _restore_slots() -> Iterator[None]:
    yield
    reset_process_slots()


# ── the lane helper ──────────────────────────────────────────────────────────

def test_lanes_keep_item_order_and_lanes_one_is_the_serial_loop() -> None:
    seen: list[int] = []

    def slow_first(i: int) -> int:
        threading.Event().wait(0.05 if i == 0 else 0.0)
        return i * 10

    assert on_lanes(slow_first, [0, 1, 2, 3], 4) == [0, 10, 20, 30]
    on_lanes(seen.append, [3, 1, 2], 1)
    assert seen == [3, 1, 2]

    def boom(i: int) -> int:
        if i in (1, 2):
            raise ValueError(f"item {i}")
        return i

    with pytest.raises(ValueError, match="item 1"):
        on_lanes(boom, [0, 1, 2, 3], 4)


# ── the process slot set ─────────────────────────────────────────────────────

class _SlowTransport(FakeTransport):
    def __init__(self, peak: _Peak, n: int) -> None:
        from pipeline_core.llm import Delta
        super().__init__([[Delta(content="ok")] for _ in range(n)], thinking=False)
        self._peak = peak
        self._lock = threading.Lock()

    def stream(self, messages, **kw):  # type: ignore[no-untyped-def,override]
        with self._lock:
            it = super().stream(messages, **kw)
        return iter(self._peak(lambda: list(it)))  # type: ignore[arg-type,return-value]


def _two_pools(width: int) -> tuple[int, list[dict[str, object]]]:
    reset_process_slots(width)
    events: list[dict[str, object]] = []
    lock = threading.Lock()

    def emit(event: str, data: dict[str, object]) -> None:
        with lock:
            events.append({"event": event, **data})

    peak = _Peak(hold=0.05)
    transport = _SlowTransport(peak, 8)
    ctl = LLMController(Hooks(emit=emit), default_sentinel=1024)

    def call(i: int) -> str:
        return ctl.run(transport, system=None, prompt=f"p{i}", model="fake", max_tokens=64)

    pools = [threading.Thread(target=on_lanes, args=(call, list(range(k, k + 4)), 4))
             for k in (0, 4)]
    for t in pools:
        t.start()
    for t in pools:
        t.join()
    return peak.peak, events


def test_two_pools_of_four_never_put_more_than_the_slot_width_on_the_server() -> None:
    peak, events = _two_pools(4)
    assert peak <= 4
    slots = [e for e in events if e["event"] == "llm.slot"]
    assert len(slots) == 8 and {e["width"] for e in slots} == {4}
    assert {e["prompt_sha"] for e in slots} == {PromptDigest.of(f"p{i}").sha256 for i in range(8)}
    assert max(int(str(e["wait_ms"])) for e in slots) > 0          # four of them queued here
    # NEGATIVE CONTROL: a slot set as wide as both pools lets all eight on.
    wide, _ = _two_pools(8)
    assert wide == 8


def test_the_readers_take_the_slot_wait_off_a_call() -> None:
    assert work_delta({"event": "llm.slot", "data": {"wait_ms": 1500}}).llm_ms == -1500  # type: ignore[union-attr]

    def ev(ts: str, event: str, thread: str) -> dict[str, object]:
        return {"ts": ts, "event": event, "thread": thread, "tid": hash(thread)}

    # Six calls requested at once, four on the server: in a run that emits
    # slots, C10 counts what was on the server.
    rows: list[dict[str, object]] = []
    for i in range(6):
        rows.append(ev(f"2026-11-07T10:00:0{i}", "llm.request", f"w{i}"))
    for i in range(4):
        rows.append(ev(f"2026-11-07T10:00:1{i}", "llm.slot", f"w{i}"))
    for i in range(4):
        rows.append(ev(f"2026-11-07T10:00:2{i}", "llm.response", f"w{i}"))
    for i in (4, 5):
        rows.append(ev(f"2026-11-07T10:00:3{i}", "llm.slot", f"w{i}"))
    for i in (4, 5):
        rows.append(ev(f"2026-11-07T10:00:4{i}", "llm.response", f"w{i}"))
    v = check_parallel_stream_integrity(RunData(events=rows))
    assert v.status == "PASS" and "max 4 concurrent" in v.evidence
    # NEGATIVE CONTROL: the same run without its slot events reads six.
    bare = [r for r in rows if r["event"] != "llm.slot"]
    assert "max 6 concurrent" in check_parallel_stream_integrity(RunData(events=bare)).evidence


# ── task 0d ──────────────────────────────────────────────────────────────────

BASE = "The message is `failed: ` then the condition."
WRONG = "`Report::take` yields Some of the value."


def _rule_stage(tmp_path: Path, lanes: int) -> tuple[BehaviourSuite, _World, _Peak, _Peak]:
    e1 = BehaviourSpec("testassert::tests/t.rs::t::0", "let r = make();", "r.show()",
                       ObservableOutcome.display("failed: a"), subkind=BehaviourSubKind.OUTCOME)
    e2 = BehaviourSpec("testassert::tests/t.rs::t::1", "let r = make();", "r.take()",
                       ObservableOutcome.display("true"), subkind=BehaviourSubKind.OUTCOME)
    book = BehaviourSuite("stable", (e1, e2), features=(
        Feature("show", "fn", (e1.behaviour_id,), (Rule("rule::show::0", BASE, (e1.behaviour_id,), "show"),)),
        Feature("take", "fn", (e2.behaviour_id,), (Rule("rule::take::0", WRONG, (e2.behaviour_id,), "take"),))),
        glossary=(GlossaryEntry("show", "fn"), GlossaryEntry("take", "fn")))
    w = _World()
    asks, judges = _Peak(), _Peak()
    lock = threading.Lock()

    def answer(prompt: str) -> str:
        with lock:
            w.prompts.append(prompt)
        if prompt.startswith("Test a rule"):
            name, when = ("show_more", "r.show_other()") if BASE in prompt else ("take_more", "r.take_other()")
            return (f'<BEHAVIOUR name="{name}">\n<GIVEN>\n    let r = make();\n</GIVEN>\n'
                    f"<WHEN>{when}</WHEN>\n<OBSERVE>display</OBSERVE>\n</BEHAVIOUR>")
        ids = re.findall(r'<EXAMPLE id="([^"]+)">', prompt)
        with_rules = "RULES (hold together" in prompt
        return "".join(
            f'<THEN id="{i}">{("failed: b" if "show" in i else "Some(1)") if with_rules else "?"}</THEN>'
            for i in ids)

    def ask(prompt: str) -> str:
        meter = asks if prompt.startswith("Test a rule") else judges
        return meter(lambda: answer(prompt))

    def run_probe(pd: Path, cl: object, program: str, name: str, **_k: object) -> ProbeResult:
        return ProbeResult(True, "failed: b\n" if "show_other" in program else "Ok(1)\n", "")

    def emit_tests(pd: Path, c: str, specs: object, **_k: object) -> tuple[None, tuple[str, ...], list[dict[str, str]], tuple[()]]:
        return None, (), [], ()

    deps = w.deps(ask=ask, run_probe=run_probe, emit_tests=emit_tests, RULE_PROBES_MAX=5, LANES=lanes)
    out = run_rule_probes(deps, tmp_path, "krate", _cluster(), book, default_cluster="stable", memo={})
    return out, w, asks, judges


def test_rule_asks_and_judges_run_together_and_the_stage_reads_the_same(tmp_path: Path) -> None:
    wide, w4, asks4, judges4 = _rule_stage(tmp_path, 4)
    serial, w1, asks1, judges1 = _rule_stage(tmp_path, 1)
    assert asks4.peak == 2 and judges4.peak >= 2              # two rules, both judged at once
    # NEGATIVE CONTROL: lanes=1 is the serial stage.
    assert asks1.peak == 1 and judges1.peak == 1
    assert wide == serial
    assert sorted(w4.prompts) == sorted(w1.prompts)
    line = [ln for ln in w4.logs if "rule-probes:" in ln]
    assert line == [ln for ln in w1.logs if "rule-probes:" in ln]


def test_alone_is_asked_only_after_together_returned_and_the_first_two_overlap() -> None:
    cap = BehaviourSpec("probe::stable::u::1", "let r = make();", "a()",
                        ObservableOutcome.display("x"), subkind=BehaviourSubKind.OUTCOME)
    order: list[tuple[str, str]] = []
    lock = threading.Lock()
    peak = _Peak()

    def predict(rules: Sequence[str], ids: Sequence[str]) -> dict[str, str]:
        kind = "ablation" if not rules else ("alone" if len(rules) == 1 else "together")

        def run() -> str:
            with lock:
                order.append(("start", kind))
            with lock:
                order.append(("end", kind))
            return ""

        peak(run)
        return dict.fromkeys(ids, "wrong") if kind != "ablation" else {}

    def both(a: Callable[[], object], b: Callable[[], object]) -> tuple[object, object]:
        x, y = on_lanes(lambda f: f(), [a, b], 2)
        return x, y

    judge_rule_on_captures("s", ["companion"], {cap.behaviour_id: cap}, predict=predict, both=both)  # type: ignore[arg-type]
    assert peak.peak == 2
    assert order.index(("start", "alone")) > order.index(("end", "together"))
    # NEGATIVE CONTROL: without `both` the first two run one after the other.
    peak.peak = 0
    judge_rule_on_captures("s", ["companion"], {cap.behaviour_id: cap}, predict=predict)
    assert peak.peak == 1


def _reach(tmp_path: Path, lanes: int) -> tuple[_World, list[object], _Peak]:
    w = _World(reply=_REPLY, exercises={_HARVESTED: frozenset({"src/fmt.rs::fmt"})})
    w.sites = {_HARVESTED: frozenset()}
    reach = _Peak()
    saved: list[object] = []
    lock = threading.Lock()

    def ask(prompt: str) -> str:
        with lock:
            w.prompts.append(prompt)
        if "reach_1" in prompt:
            return reach(lambda: _REACH_REPLY)
        return _REPLY

    deps = w.deps(ask=ask, load_construction_facts=lambda _p, c: _FACTS if c == "stable" else None,
                  save_reach=lambda _p, _c, out: saved.extend(out),
                  PROBE_BLOCKS_PER_REPLY=1, LANES=lanes)
    run_task0d(deps, tmp_path, "anyhow", [_cluster()], tmp_path / "src")
    return w, saved, reach


def test_reach_batches_are_asked_together_and_captured_in_batch_order(tmp_path: Path) -> None:
    w4, out4, peak4 = _reach(tmp_path / "w4", 4)
    w1, out1, peak1 = _reach(tmp_path / "w1", 1)
    assert peak4.peak == 2 and peak1.peak == 1               # two batches of one target
    assert out4 == out1
    assert [n for n, _p in w4.probes_run] == [n for n, _p in w1.probes_run]


# ── t1.5 ─────────────────────────────────────────────────────────────────────

def _two_components():
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="struct ErrorImpl { vtable: u8 }"),
        TypeRecord(file="src/lib.rs", kind="struct", name="Report", is_pub=True,
                   definition="pub struct Report { inner: Box<ReportImpl> }"),
        TypeRecord(file="src/report.rs", kind="struct", name="ReportImpl", is_pub=False,
                   definition="struct ReportImpl { tag: u8 }"),
    ]
    surface = classify_private_surface(types, [], cfg_cluster="stable")
    assert len(surface.components) == 2
    return surface


class _Layout:
    def __init__(self, lanes: int, compiles: Sequence[bool]) -> None:
        self.lanes = lanes
        self.files = {"src/error.rs": "struct ErrorImpl { vtable: u8 }\n",
                      "src/report.rs": "struct ReportImpl { tag: u8 }\n"}
        self.compiles = list(compiles)
        self.prompts: list[str] = []
        self.logs: list[str] = []
        self.second_asked = threading.Event()
        self.second_before_first_compile = False
        self.compiled = 0
        self._lock = threading.Lock()

    def deps(self) -> AuthorLayoutDeps:
        def run_model(p: str) -> str:
            with self._lock:
                self.prompts.append(p)
            if "ReportImpl" in p and "=== EVERY PREVIOUS" not in p:
                self.second_asked.set()
            name = "ErrorImpl" if "ErrorImpl" in p else "ReportImpl"
            body = "inner: Box<dyn StdError>" if name == "ErrorImpl" else "tag: u16"
            retry = " /* retry */" if "=== EVERY PREVIOUS" in p else ""
            return f'<LAYOUT_ITEM name="{name}" kind="struct">struct {name} {{ {body} }}{retry}</LAYOUT_ITEM>'

        def compile_check() -> tuple[bool, str]:
            if self.compiled == 0:
                # Wait a moment for the second component's ask: it is asked
                # ahead only when lanes > 1.
                self.second_before_first_compile = self.second_asked.wait(1.0 if self.lanes > 1 else 0.2)
            self.compiled += 1
            return (self.compiles.pop(0) if self.compiles else True), "error[E0308]: mismatched types"

        return AuthorLayoutDeps(
            run_model=run_model,
            read_skeleton=lambda rel: self.files.get(rel),
            write_skeleton=lambda rel, txt: self.files.__setitem__(rel, txt),
            compile_check=compile_check, log=self.logs.append, max_attempts=3,
            lanes=self.lanes)


def _author(lanes: int, compiles: Sequence[bool]) -> tuple[_Layout, object]:
    d = _Layout(lanes, compiles)
    res = author_layout(_two_components(), module_name="krate", public_surface="pub struct Error;",
                        select_pins=_has_pins, symbol_inventory="Box", deps=d.deps())
    return d, res


@pytest.mark.parametrize("compiles", [(), (False,)], ids=["first-try", "first-component-retries"])
def test_layout_asks_ahead_and_commits_what_the_serial_pass_commits(compiles: Sequence[bool]) -> None:
    wide, res4 = _author(4, compiles)
    serial, res1 = _author(1, compiles)
    assert wide.second_before_first_compile
    # NEGATIVE CONTROL: the serial pass asks the second component at its turn.
    assert not serial.second_before_first_compile
    assert wide.files == serial.files
    assert res4 == res1
    assert sorted(wide.prompts) == sorted(serial.prompts)    # nothing asked twice, nothing extra
    # Both components' attempt-1 replies were asked ahead and used at their
    # turn; a retry is asked at its turn, so it is not among them.
    assert any("asked ahead: 2 of 2 reply(ies) used" in ln for ln in wide.logs)
    assert not any("asked ahead" in ln for ln in serial.logs)

"""The session harness's keep/restore contracts, IO faked.

A Display crate — the never-steer check's own example. The loop reads the
slice, the replies and the suite readings; it must not keep a compiling
change it cannot judge, and a previously-passing test going red must not
survive peel even when net pass rose.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.slice_session import (
    SessionPorts,
    _peel,
    run_slice_session,
)
from pipeline_core.application.reconstruct.tree_txn import PreImage
from pipeline_core.domain.crash_keep import SameCrashJudge
from pipeline_core.domain.models import FnRecord, FnSite
from pipeline_core.domain.slice_session import SessionSlice
from pipeline_core.domain.test_results import BinaryResult, TestStateReading

_FMT = FnSite("src/lib.rs", "fmt", "impl fmt::Display for Report")
_RENDER = FnSite("src/render.rs", "render", "impl Renderer")
_RECS = {
    _FMT: FnRecord(file="src/lib.rs", fn_name="fmt",
                   container="impl fmt::Display for Report",
                   sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result",
                   body_text="write!(f, \"{}\", self.msg)"),
    _RENDER: FnRecord(file="src/render.rs", fn_name="render",
                      container="impl Renderer",
                      sig_text="pub fn render(&self) -> u32",
                      body_text="self.depth"),
}


def _reading(passed: set[str], failed: set[str]) -> TestStateReading:
    p, f = frozenset(passed), frozenset(failed)
    return TestStateReading(binaries=(BinaryResult(
        label="unit", completed=True, passed=p, failed=f,
        pass_count=len(p), fail_count=len(f)),))


def _tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("SESSION_LIB")
    (src / "render.rs").write_text("SESSION_REN")
    return tmp_path


def _base_pre_images(root: Path) -> dict[str, PreImage]:
    """The session-open byte pre-images the peel restores from."""
    return {"src/lib.rs": PreImage(root / "src" / "lib.rs", b"BASE_LIB", None),
            "src/render.rs": PreImage(root / "src" / "render.rs", b"BASE_REN", None)}


def _ports(*, measure, splice=None, ask=None, bodies=None):
    def _splice(path, container, name, body, where, **k):
        path.write_text(body)
        return True, "", 0

    return SessionPorts(
        ask_turns=ask or (lambda m: ""),
        splice=splice or _splice,
        check=lambda: (0, "", []),
        focus_errors=lambda out, f: out,
        blamed_type_api=lambda t: t,
        measure_suite=measure,
        read_source=lambda p: None,
        enumerate_bodies=lambda: bodies or {
            u: r.body_text for u, r in _RECS.items()},
        answer_pulls=lambda r, conversation="": None,
        log=lambda m: None, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))


def test_a_compiling_turn_that_is_still_the_crash_is_not_kept(tmp_path):
    """The session keep on run 371055-1a407b3c. The turn compiles, every
    aimed test is still a native crash, the written unit was a culprit at
    the sweep, and the localizer says it still is. The body goes back.
    Negative control: the same turn when the sweep blamed nothing it wrote
    is the old non-regression keep."""
    from pipeline_core.domain.models import FnLocator
    from pipeline_core.domain.test_isolation import IsolatedStatus, IsolatedTest, render_isolated_section

    root = _tree(tmp_path)
    baseline = _reading(set(), {"t_box"})
    after = _reading(set(), {"t_box"})
    capture = render_isolated_section([
        IsolatedTest("t_box", IsolatedStatus.ABORTED,
                     "thread 't_box' has overflowed its stack\n"),
    ])
    logs: list[str] = []

    def measure(raw):
        raw.append(capture)
        lib = (root / "src" / "lib.rs").read_text()
        return baseline if lib == "SESSION_LIB" else after

    def ask(_m: object) -> str:
        return '<FN name="fmt" block="impl fmt::Display for Report">NEW_FMT(f)</FN>'

    ports = _ports(measure=measure, ask=ask)
    ports = SessionPorts(
        ask_turns=ask,
        splice=ports.splice,
        check=lambda: (0, "", []),
        focus_errors=lambda out, f: out,
        blamed_type_api=lambda t: t,
        measure_suite=measure,
        read_source=lambda p: None,
        enumerate_bodies=lambda: {
            _FMT: (root / "src" / "lib.rs").read_text(),
            _RENDER: (root / "src" / "render.rs").read_text(),
        },
        answer_pulls=lambda r, conversation="": None,
        log=logs.append, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(
            frozenset({FnLocator(_FMT.file, _FMT.fn_name)}),
            lambda _out: [FnLocator(_FMT.file, _FMT.fn_name)]))
    out = run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_box",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=1)
    assert out.kept == frozenset()
    assert (root / "src" / "lib.rs").read_text() == "SESSION_LIB"
    assert any("still a native crash" in m for m in logs)

    # Negative control: the same turn with no written unit in the sweep's
    # blame is kept.
    def measure_open(raw):
        raw.append(capture)
        return after

    open_ports = SessionPorts(
        ask_turns=ask, splice=ports.splice, check=lambda: (0, "", []),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=measure_open, read_source=lambda p: None,
        enumerate_bodies=lambda: {
            _FMT: (root / "src" / "lib.rs").read_text(),
            _RENDER: (root / "src" / "render.rs").read_text(),
        },
        answer_pulls=lambda r, conversation="": None,
        log=lambda m: None, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    (root / "src" / "lib.rs").write_text("SESSION_LIB")
    kept = run_slice_session(
        open_ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_box",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=1)
    assert _FMT in kept.kept


def test_an_inconclusive_opening_suite_does_not_run_or_keep(tmp_path):
    root = _tree(tmp_path)
    asked: list = []

    def ask(m: object) -> str:
        asked.append(m)
        return "should not run"

    out = run_slice_session(
        _ports(measure=lambda raw: None, ask=ask),
        target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER)),
        records=_RECS, turn0="turn0")
    assert out.outcome == "no_change" and out.turns == 0 and asked == []
    assert (root / "src" / "lib.rs").read_text() == "SESSION_LIB"
    assert (root / "src" / "render.rs").read_text() == "SESSION_REN"


def test_peel_holds_a_net_positive_state_that_reddened_a_sibling_and_peels_a_flat_one(
        tmp_path):
    """The session's bar is the sweep's (`RegressionComparison.regressed`):
    a strict net gain HOLDS even with a newly-red test — the +15/−1 body of
    run 334902-2774708c's 05:39 turn (2026-09-06), which the old bar ("no
    previously-passing test red, whatever the net") would have peeled while
    the sweep one iteration later accepted it. Control: the same reddening
    at net zero is a regression and the peel restores the baseline."""
    root = _tree(tmp_path)
    baseline = _reading({"t_keep"}, {"t_pair"})
    # After peeling one unit: +1 pass, but t_keep went red — net positive.
    after = _reading({"t_pair", "t_bonus"}, {"t_keep"})
    assert after.pass_count - baseline.pass_count > 0
    from pipeline_core.domain.test_results import compare_readings
    assert not compare_readings(baseline, after).regressed
    assert compare_readings(baseline, after).newly_red == frozenset({"t_keep"})
    kept, reverted = _peel(
        _ports(measure=lambda raw: after),
        root,
        SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER)),
        _RECS, baseline,
        {_FMT: "BASE_LIB", _RENDER: "BASE_REN"},
        _base_pre_images(root),
        frozenset({_FMT, _RENDER}), peel_max=1, label="t")
    # One unit peeled (sorted pop takes render), the other holds.
    assert kept == frozenset({_FMT}) and reverted == frozenset({_RENDER})
    assert (root / "src" / "render.rs").read_text() == "BASE_REN"
    # Control: the reddening at net ZERO is a regression — nothing holds
    # within the one-step budget and the baseline is restored.
    (tmp_path / "flat").mkdir()
    root2 = _tree(tmp_path / "flat")
    flat = _reading({"t_pair"}, {"t_keep"})
    assert flat.pass_count == baseline.pass_count
    assert compare_readings(baseline, flat).regressed
    kept2, _reverted2 = _peel(
        _ports(measure=lambda raw: flat),
        root2,
        SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER)),
        _RECS, baseline,
        {_FMT: "BASE_LIB", _RENDER: "BASE_REN"},
        _base_pre_images(root2),
        frozenset({_FMT, _RENDER}), peel_max=1, label="t")
    assert kept2 == frozenset()
    assert (root2 / "src" / "lib.rs").read_text() == "BASE_LIB"
    assert (root2 / "src" / "render.rs").read_text() == "BASE_REN"


def test_a_hung_turn_restores_the_whole_change_set(tmp_path):
    """Plan: a hang is not a peelable subset — later binaries never ran,
    so no member's keep can be read. File-first already reverts on hang;
    the session's peel kept one half of a hung two-unit write (run
    19980-f9ea6adb fill 2). Control: the same two-unit write without a
    hang still peels to a holding subset (the existing net-positive peel
    test)."""
    root = _tree(tmp_path)
    baseline = TestStateReading(binaries=(
        BinaryResult(label="unit", completed=True,
                     passed=frozenset({"t_keep"}), failed=frozenset({"t_pair"}),
                     pass_count=1, fail_count=1),
        BinaryResult(label="tests/chain.rs", completed=True,
                     passed=frozenset({"t_chain"}), failed=frozenset(),
                     pass_count=1, fail_count=0),
    ))
    hung = TestStateReading(binaries=(
        BinaryResult(label="unit", completed=True,
                     passed=frozenset({"t_keep"}), failed=frozenset({"t_pair"}),
                     pass_count=1, fail_count=1),
        BinaryResult(label="tests/chain.rs", completed=False,
                     passed=frozenset(), failed=frozenset(),
                     pass_count=0, fail_count=0, dangling="t_chain", hung=True),
    ))
    hold = _reading({"t_keep"}, {"t_pair"})
    logs: list[str] = []

    def measure(_raw):
        lib = (root / "src" / "lib.rs").read_text()
        ren = (root / "src" / "render.rs").read_text()
        if lib == "SESSION_LIB" and ren == "SESSION_REN":
            return baseline
        if lib != "SESSION_LIB" and ren != "SESSION_REN":
            return hung
        # One member restored: the subset does not hang. Pre-fix peel
        # would keep it; the hung bar must not reach peel.
        return hold

    def enumerate():
        return {
            _FMT: (root / "src" / "lib.rs").read_text(),
            _RENDER: (root / "src" / "render.rs").read_text(),
        }

    def ask(_m: object) -> str:
        return (
            '<FN name="fmt" block="impl fmt::Display for Report">NEW_FMT(f)</FN>\n'
            '<FN name="render" block="impl Renderer">NEW_REN</FN>'
        )

    ports = SessionPorts(
        ask_turns=ask,
        splice=_ports(measure=measure).splice,
        check=lambda: (0, "", []),
        focus_errors=lambda out, f: out,
        blamed_type_api=lambda t: t,
        measure_suite=measure,
        read_source=lambda p: None,
        enumerate_bodies=enumerate,
        answer_pulls=lambda r, conversation="": None,
        log=logs.append, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    out = run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER)),
        records=_RECS, turn0="turn0", max_turns=1)
    assert out.outcome == "reverted" and out.kept == frozenset()
    assert _FMT in out.reverted and _RENDER in out.reverted
    assert (root / "src" / "lib.rs").read_text() == "SESSION_LIB"
    assert (root / "src" / "render.rs").read_text() == "SESSION_REN"
    assert any("hung binary" in m and "whole change set restored" in m
               for m in logs), logs


def test_a_failed_peel_splice_leaves_the_unit_on_the_kept_side(tmp_path):
    """Pop + continue without restore used to drop the unit from remaining
    while its post-session body stayed on disk. An early hold then reported
    it as neither kept nor reverted."""
    root = _tree(tmp_path)
    baseline = _reading({"t_keep"}, {"t_pair"})
    hold = _reading({"t_keep"}, {"t_pair"})

    def splice(path, container, name, body, where, **k):
        if name == "render":
            return False, "refused", 0
        path.write_text(body)
        return True, "", 0

    kept, reverted = _peel(
        _ports(measure=lambda raw: hold, splice=splice),
        root,
        SessionSlice(tests=("t_pair",), units=(_FMT, _RENDER)),
        _RECS, baseline,
        {_FMT: "BASE_LIB", _RENDER: "BASE_REN"},
        _base_pre_images(root),
        frozenset({_FMT, _RENDER}), peel_max=6, label="t")
    # Sorted (file, name): fmt then render; pop takes render first (fails),
    # then fmt (holds). render stays the session body and is kept.
    assert _RENDER in kept and _FMT in reverted
    assert (root / "src" / "render.rs").read_text() == "SESSION_REN"
    assert (root / "src" / "lib.rs").read_text() == "BASE_LIB"


def test_a_file_request_grows_the_write_set_so_the_next_turn_can_write_the_implementer(
        tmp_path):
    """Plan: a session that opened on a Debug dispatcher whose body calls
    Qual::display cannot write Qual::debug until a FILE request for that
    file admits it. Turn 1 asks; turn 2's debug body routes and is kept.
    Control: the same debug body on turn 1, before the request, is
    rejected as out of scope."""
    from pipeline_core.domain.attribution import record_site

    src = tmp_path / "src"
    src.mkdir()
    (src / "w.rs").write_text("DISP")
    (src / "i.rs").write_text("IMPL")
    disp = FnRecord(
        file="src/w.rs", fn_name="fmt",
        container="impl Debug for Wrapper < T >",
        sig_text="fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result",
        body_text="Inner::display(self.inner.by_ref(), f)")
    debug = FnRecord(
        file="src/i.rs", fn_name="debug",
        container="impl Inner < T >",
        sig_text="fn debug(this: Ref<Self>, f: &mut fmt::Formatter) -> fmt::Result",
        body_text="write!(f, \"{}\", this.value)")
    disp_site = record_site(disp)
    debug_site = record_site(debug)
    bodies = {disp_site: disp.body_text}
    replies = [
        '<REQUEST kind="file" arg="src/i.rs"/>',
        '<FN name="debug" block="impl Inner < T >">write!(f, "x{}", this.value)</FN>',
    ]
    logs: list[str] = []

    def ask(_m: object) -> str:
        return replies.pop(0) if replies else ""

    def splice(path, container, name, body, where, **k):
        path.write_text(body)
        return True, "", 0

    def extend(sites):
        for s in sites:
            if s == debug_site:
                bodies[s] = debug.body_text

    def enumerate():
        out = dict(bodies)
        if debug_site in bodies:
            text = (tmp_path / "src" / "i.rs").read_text()
            if text != "IMPL":
                out[debug_site] = text
        return out

    baseline = _reading({"t_keep"}, {"t_wrap"})
    after = _reading({"t_keep"}, {"t_wrap"})
    ports = SessionPorts(
        ask_turns=ask, splice=splice, check=lambda: (0, "", []),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=lambda raw: after if (tmp_path / "src" / "i.rs").read_text() != "IMPL"
        else baseline,
        read_source=lambda p: None,
        enumerate_bodies=enumerate,
        answer_pulls=lambda r, conversation="": "file src/i.rs follows." if "<REQUEST" in r else None,
        extend_body_watch=extend,
        log=logs.append, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    out = run_slice_session(
        ports, target_src_dir=tmp_path, cluster_name="t",
        slice_=SessionSlice(tests=("t_wrap",), units=(disp_site,)),
        records={disp_site: disp}, turn0="turn0", max_turns=4,
        all_records=(disp, debug))
    assert any("admitted 1 unit" in m for m in logs), logs
    assert debug_site in out.kept
    assert out.outcome == "kept"
    assert (tmp_path / "src" / "i.rs").read_text() == 'write!(f, "x{}", this.value)'

    # Control: writing debug on turn 1, with no request, is out of scope.
    src2 = tmp_path / "ctrl"
    src2.mkdir()
    (src2 / "src").mkdir()
    (src2 / "src" / "w.rs").write_text("DISP")
    (src2 / "src" / "i.rs").write_text("IMPL")
    rejected: list[str] = []

    def ask_ctrl(_m: object) -> str:
        return '<FN name="debug" block="impl Inner < T >">write!(f, "x{}", this.value)</FN>'

    def emit(_n, **kw):
        data = kw.get("data") or {}
        if data.get("kind") == "rejected":
            rejected.extend(data.get("rejected") or [])

    ports_ctrl = SessionPorts(
        ask_turns=ask_ctrl, splice=splice, check=lambda: (0, "", []),
        focus_errors=lambda out, f: out, blamed_type_api=lambda t: t,
        measure_suite=lambda raw: baseline,
        read_source=lambda p: None,
        enumerate_bodies=lambda: {disp_site: disp.body_text},
        answer_pulls=lambda r, conversation="": None,
        log=lambda m: None, emit=emit,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    out_ctrl = run_slice_session(
        ports_ctrl, target_src_dir=src2, cluster_name="t",
        slice_=SessionSlice(tests=("t_wrap",), units=(disp_site,)),
        records={disp_site: disp}, turn0="turn0", max_turns=1,
        all_records=(disp, debug))
    assert out_ctrl.kept == frozenset()
    assert any("not in this session's editable set" in r for r in rejected)


def test_a_turn_that_ignores_an_input_is_kept_only_when_a_test_turns_green(tmp_path):
    """Run `486447-5207101c`: the retreat body `let _ = (lhs, rhs);
    Error::msg(msg)` was kept on no evidence. A session turn whose body
    drops a declared input and turns nothing green is not kept, and the
    next turn is told which input it dropped. Control: the same body, when
    the aimed test passes after it, is kept — a test vouches for ignoring
    an input (serde's `IgnoredAny` is correct that way)."""
    asked: list[str] = []

    def ask(m: object) -> str:
        asked.append(str(m))
        return ('<FN name="fmt" block="impl fmt::Display for Report">'
                "let _ = f;\nOk(())</FN>")

    root = _tree(tmp_path)
    baseline = _reading(set(), {"t_box"})
    still_red = _reading(set(), {"t_box"})
    out = run_slice_session(
        _ports(measure=lambda raw: baseline
               if (root / "src" / "lib.rs").read_text() == "SESSION_LIB"
               else still_red, ask=ask),
        target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_box",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=2)
    assert out.kept == frozenset()
    assert (root / "src" / "lib.rs").read_text() == "SESSION_LIB"
    assert len(asked) == 2
    assert "never uses its input(s) `f`" in asked[1]

    (tmp_path / "control").mkdir()
    root2 = _tree(tmp_path / "control")
    green = _reading({"t_box"}, set())
    kept = run_slice_session(
        _ports(measure=lambda raw: baseline
               if (root2 / "src" / "lib.rs").read_text() == "SESSION_LIB"
               else green, ask=ask),
        target_src_dir=root2, cluster_name="t",
        slice_=SessionSlice(tests=("t_box",), units=(_FMT,)),
        records=_RECS, turn0="turn0", max_turns=1,
        executed={"t_box": (_FMT,)})
    assert kept.outcome == "accepted"
    assert (root2 / "src" / "lib.rs").read_text() == "let _ = f;\nOk(())"


_RENDER_DISCARD = FnSite("src/render.rs", "render", "impl Renderer")
_RENDER_RECS = {
    _FMT: _RECS[_FMT],
    _RENDER_DISCARD: FnRecord(
        file="src/render.rs", fn_name="render", container="impl Renderer",
        sig_text=("fn render(msg: &'static str, lhs: &dyn Debug, "
                  "rhs: &dyn Debug) -> Error"),
        body_text="unimplemented!()"),
}


def _sibling_turn(root: Path, *, after: TestStateReading,
                  executed: dict[str, tuple[FnSite, ...]]):
    """One turn that writes `render`'s discard beside a `fmt` body."""
    def ask(_m: object) -> str:
        return ('<FN name="fmt" block="impl fmt::Display for Report">'
                'write!(f, "{}", self.msg)</FN>\n'
                '<FN name="render" block="impl Renderer">'
                "let _ = (lhs, rhs);\nError::msg(msg)</FN>")

    baseline = _reading(set(), {"t_fmt", "t_render"})
    logs: list[str] = []
    ports = _ports(measure=lambda raw: baseline
                   if (root / "src" / "render.rs").read_text() == "SESSION_REN"
                   else after, ask=ask)
    ports = SessionPorts(**{
        **{f: getattr(ports, f) for f in ports.__slots__},
        "log": logs.append,
        "enumerate_bodies": lambda: {
            _FMT: (root / "src" / "lib.rs").read_text(),
            _RENDER_DISCARD: (root / "src" / "render.rs").read_text()}})
    out = run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_fmt", "t_render"),
                            units=(_FMT, _RENDER_DISCARD)),
        records=_RENDER_RECS, turn0="turn0", max_turns=1, executed=executed)
    return out, logs


def test_a_sibling_turning_green_does_not_vouch_for_a_discard(tmp_path):
    """The turn writes `render`'s `let _ = (lhs, rhs)`
    beside `fmt`, and `fmt`'s own test turns green while `render`'s stays
    red. The old flag (any test turned green) kept the discard. A body is
    vouched only by a test that executes it, so the change set is declined.
    Control: the same turn with `t_render` (which executes `render`) turned
    green is kept. A target already green at the session's baseline turns
    nothing green, so it vouches for nobody."""
    executed = {"t_fmt": (_FMT,), "t_render": (_RENDER_DISCARD,)}
    out, logs = _sibling_turn(
        _tree(tmp_path), after=_reading({"t_fmt"}, {"t_render"}),
        executed=executed)
    assert out.kept == frozenset()
    assert any("render ignores lhs, rhs" in m for m in logs), logs

    (tmp_path / "vouched").mkdir()
    root2 = _tree(tmp_path / "vouched")
    out2, _ = _sibling_turn(
        root2, after=_reading({"t_render"}, {"t_fmt"}), executed=executed)
    assert _RENDER_DISCARD in out2.kept
    assert (root2 / "src" / "render.rs").read_text().startswith("let _ = (lhs, rhs);")


def test_a_regressed_turn_that_gained_keeps_only_its_changed_bodies(tmp_path):
    """The joint ask's candidate is the best turn's CHANGED bodies, read
    while that turn is on the tree: a turn that turned the target green and
    a sibling red. An unchanged body is not a candidate (it was snapshotted
    whole before, every body of the slice)."""
    root = _tree(tmp_path)
    baseline = _reading({"t_sib"}, {"t_target"})
    after = _reading({"t_target"}, {"t_sib"})

    def measure(raw):
        lib = (root / "src" / "lib.rs").read_text()
        return baseline if lib == "SESSION_LIB" else after

    ports = _ports(measure=measure, ask=lambda _m: (
        '<FN name="fmt" block="impl fmt::Display for Report">NEW_FMT(f)</FN>'))
    ports = SessionPorts(
        ask_turns=ports.ask_turns, splice=ports.splice,
        check=lambda: (0, "", []), focus_errors=lambda out, f: out,
        blamed_type_api=lambda t: t, measure_suite=measure,
        read_source=lambda p: None,
        enumerate_bodies=lambda: {
            _FMT: (root / "src" / "lib.rs").read_text(),
            _RENDER: (root / "src" / "render.rs").read_text()},
        answer_pulls=lambda r, conversation="": None,
        log=lambda m: None, emit=lambda *a, **k: None,
        check_graceful_stop=lambda s: None,
        same_crash=SameCrashJudge(frozenset(), None))
    out = run_slice_session(
        ports, target_src_dir=root, cluster_name="t",
        slice_=SessionSlice(tests=("t_target",), units=(_FMT, _RENDER)),
        records=_RECS, turn0="turn0", max_turns=1)
    assert out.candidate_bodies == {"src/lib.rs::fmt": "NEW_FMT(f)"}

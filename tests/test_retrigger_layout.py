"""Pins for the F11 M3 layout re-trigger ACTION (application/retrigger_layout.py)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pipeline_core.application.author_layout import (
    AuthorLayoutDeps,
    ComponentResult,
    LayoutComponentRecord,
    LayoutOutcome,
)
from pipeline_core.application.retrigger_layout import (
    RetriggerLayoutDeps,
    retrigger_layout,
)
from pipeline_core.domain.behaviour import (
    BehaviourSpec,
    BehaviourSubKind,
    BehaviourSuite,
    ObservableOutcome,
    StaticAssertKind,
)
from pipeline_core.domain.layout_retrigger import CompileEvidence
from pipeline_core.domain.models import FnLocator, FnRecord, TypeRecord
from pipeline_core.domain.private_surface import classify_private_surface

# The two path spaces these pins exist to keep apart. A private-surface item's
# `file` is relative to the SRC dir it was enumerated from ("error.rs"); the fn
# records that drive the referencing-fn revert are enumerated from the CRATE
# ROOT cargo runs in ("src/error.rs"). The fakes below resolve every path for
# real, so a call that hands one base where the other belongs finds nothing —
# which is what production did while every stubbed-out pin stayed green.
_CRATE = Path("/x/.skeleton-filled/stable")
_SRC = _CRATE / "src"


def _surface():
    types = [
        TypeRecord(file="lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="error.rs", kind="struct", name="ErrorImpl", is_pub=False,
                   definition="struct ErrorImpl { vtable: u8 }"),
    ]
    return classify_private_surface(types, [], cfg_cluster="stable")


def _suite_with_pin():
    # avoids the M13 missing-pin gate — ErrorImpl is layout-sensitive (`Error`
    # references it directly) so it needs a pin to be authorable at all.
    return BehaviourSuite(cfg_cluster="stable", specs=(
        BehaviourSpec(
            behaviour_id="static::repr::0", given="", when="size_of",
            then=ObservableOutcome.static_assertion(
                StaticAssertKind.SIZE,
                "assert_eq!(size_of::<Error>(), size_of::<ErrorImpl>())"),
            subkind=BehaviourSubKind.OUTCOME),
    ))


_GOOD_REPLY = ('<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
               'struct ErrorImpl { inner: Box<dyn StdError> }</LAYOUT_ITEM>')

_ERR = "error[E0609]: no field `vtable` on type `ErrorImpl`"

_FROZEN_DEF = "struct ErrorImpl { vtable: u8 }"


class _Fakes:
    def __init__(self, *, frozen=True, compiles=True, reply=_GOOD_REPLY,
                 attempts=None, fn_records=None, skeleton_def=_FROZEN_DEF):
        self.compiles = compiles
        self.reply = reply
        self.attempts = dict(attempts or {})
        self.saved_attempts: dict[str, int] | None = None
        # EVERY save, in order — the in-flight marker only exists between two
        # of them, so a fake that keeps the last one alone cannot see it.
        self.attempt_saves: list[dict[str, int]] = []
        # Canonical records are SRC-relative; `_fns_for` re-bases them onto
        # whichever dir the caller enumerates, exactly as the extractor does.
        self.fn_records = fn_records if fn_records is not None else [
            FnRecord(file="error.rs", fn_name="fmt",
                     sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
                     body_text="let x: &ErrorImpl = self.inner.get(); write!(f, \"{}\", x)"),
            FnRecord(file="chain.rs", fn_name="next",
                     sig_text="fn next(&mut self) -> Option<u8>", body_text="None"),
        ]
        self._records = (
            [LayoutComponentRecord(frozenset({"ErrorImpl"}), LayoutOutcome.FROZEN)]
            if frozen else [])
        self.files = {"error.rs": skeleton_def + "\n"}
        # Backfill sources (parsed records over the scaffold): None = the
        # extractor is unavailable, so keyless AUTHORED rows must skip.
        self.skeleton_fns: list[FnRecord] | None = None
        self.skeleton_types: list[TypeRecord] | None = None
        self.committed: list[str] = []
        self.reverted_calls: list[list[FnLocator]] = []
        self.tree_plans: list[dict] = []
        self.tree_apply_dirs: list[Path] = []
        # Live-tree file text, keyed src-relative: type declarations are
        # SLICED from here, never taken from an extractor record (syn
        # re-renders them). Reached only through `_read_live`, so a wrong base
        # reads nothing — as it does on disk.
        self.live_files: dict[str, str] = {"error.rs": _FROZEN_DEF + "\n"}
        self.tree_apply_error = ""
        self.outcome_updates: list[ComponentResult] = []
        self.logs: list[str] = []
        self.prompts: list[str] = []

    def _read_live(self, src_dir, rel) -> str | None:
        """Resolve like the real adapter: only a path that actually lands
        under the crate's `src/` reads anything."""
        try:
            key = str((Path(src_dir) / rel).relative_to(_SRC))
        except ValueError:
            return None
        return self.live_files.get(key)

    def _fns_for(self, src_dir) -> list[FnRecord] | None:
        """Records with `file` relative to the enumerated dir — the extractor's
        actual contract, and the reason one enumeration cannot serve both the
        src-rooted propagation and the crate-rooted revert."""
        p = Path(src_dir)
        if p == _SRC:
            return list(self.fn_records)
        if p == _CRATE:
            return [replace(r, file=f"src/{r.file}") for r in self.fn_records]
        return []

    def deps(self) -> RetriggerLayoutDeps:
        def author_deps_for(pd, c):
            def run_model(p):
                self.prompts.append(p)
                return self.reply
            return AuthorLayoutDeps(
                run_model=run_model,
                read_skeleton=lambda rel: self.files.get(rel),
                write_skeleton=lambda rel, txt: self.files.__setitem__(rel, txt),
                compile_check=lambda: (self.compiles, "" if self.compiles
                                       else "error[E0308]: mismatched types"),
                log=self.logs.append, max_attempts=1)

        def revert_fns(fns, src_dir):
            self.reverted_calls.append(fns)
            return len(fns)

        def apply_to_tree(plan, src_dir):
            self.tree_plans.append(plan)
            self.tree_apply_dirs.append(Path(src_dir))
            if self.tree_apply_error:
                return 0, self.tree_apply_error
            for rel in plan:
                if self._read_live(src_dir, rel) is None:
                    return 0, f"live tree file unreadable: {rel}"
            return len(plan), ""

        def save_attempts(pd, c, a):
            self.saved_attempts = dict(a)
            self.attempt_saves.append(dict(a))
            return Path(pd) / "_attempts.json"

        def git_commit(pd, msg):
            self.committed.append(msg)
            return True

        def update_outcome(pd, c, component):
            self.outcome_updates.append(component)
            return Path(pd) / "_outcomes.json"

        return RetriggerLayoutDeps(
            log=self.logs.append,
            load_surface=lambda pd, c: _surface(),
            load_layout_records=lambda pd, c: self._records,
            update_layout_outcome=update_outcome,
            enumerate_skeleton_fns=lambda pd, c: self.skeleton_fns,
            enumerate_skeleton_types=lambda pd, c: self.skeleton_types,
            load_suite=lambda pd, c: _suite_with_pin(),
            public_surface_text=lambda pd, c: "pub struct Error;",
            symbol_inventory_text=lambda pd, c: "Box",
            author_deps_for=author_deps_for,
            enumerate_fns=self._fns_for,
            read_tree_file=self._read_live,
            revert_fns=revert_fns,
            apply_layout_to_tree=apply_to_tree,
            git_commit=git_commit,
            load_attempts=lambda pd, c: self.attempts,
            save_attempts=save_attempts,
            max_attempts=2,
        )


def test_fires_reauthors_and_reverts_only_referencing_fns():
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded
    assert res.reverted_count == 1
    assert f.reverted_calls == [[FnLocator("src/error.rs", "fmt")]]  # NOT chain.rs::next
    assert f.committed  # checkpointed before the attempt
    assert f.saved_attempts == {"ErrorImpl": 1}


def test_success_persists_authored_outcome_with_locate_keys():
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.succeeded
    assert len(f.outcome_updates) == 1
    upd = f.outcome_updates[0]
    assert upd.authored and upd.item_names == ("ErrorImpl",)
    # the stored locate-key is the AUTHORED def — the text now in the tree
    assert dict(upd.authored_defs)["ErrorImpl"] == (
        "struct ErrorImpl { inner: Box<dyn StdError> }")


def test_reauthor_failure_does_not_touch_outcome_store():
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and not res.succeeded
    assert f.outcome_updates == []


def test_authored_component_reauthors_via_stored_locate_keys():
    # The tree holds the previously AUTHORED def; the baked frozen def is
    # gone. The stored locate-key must find the live def or every re-author
    # attempt dies with "could not locate a frozen definition" (observed
    # live: attempt 2 burned exactly this way after attempt 1 succeeded).
    authored_def = "struct ErrorImpl { inner: Box<dyn StdError> }"
    new_reply = ('<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
                 'struct ErrorImpl { msg: String }</LAYOUT_ITEM>')
    f = _Fakes(reply=new_reply, skeleton_def=authored_def)
    f._records = [LayoutComponentRecord(
        frozenset({"ErrorImpl"}), LayoutOutcome.AUTHORED,
        (("ErrorImpl", authored_def),))]
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded
    assert "struct ErrorImpl { msg: String }" in f.files["error.rs"]


def test_authored_component_without_stored_keys_is_skipped_loudly():
    f = _Fakes()
    f._records = [LayoutComponentRecord(
        frozenset({"ErrorImpl"}), LayoutOutcome.AUTHORED)]
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert not res.fired
    assert not f.committed          # no attempt burned
    assert f.saved_attempts is None
    assert any("no stored locate-keys" in ln for ln in f.logs)


def test_keyless_authored_row_backfills_from_scaffold_records():
    # Store row predates locate-keys, but the AST extractor can read the
    # authored def back from the scaffold: keys are derived, PERSISTED, and
    # the re-author proceeds against the live def.
    authored_def = "struct ErrorImpl { inner: Box<dyn StdError> }"
    new_reply = ('<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
                 'struct ErrorImpl { msg: String }</LAYOUT_ITEM>')
    f = _Fakes(reply=new_reply, skeleton_def=authored_def)
    f._records = [LayoutComponentRecord(
        frozenset({"ErrorImpl"}), LayoutOutcome.AUTHORED)]
    f.skeleton_fns = []
    f.skeleton_types = [TypeRecord(
        file="error.rs", kind="struct", name="ErrorImpl", is_pub=False,
        definition=authored_def)]
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded
    assert "struct ErrorImpl { msg: String }" in f.files["error.rs"]
    # first update = the backfilled keys, second = the re-author's fresh keys
    assert dict(f.outcome_updates[0].authored_defs)["ErrorImpl"] == authored_def
    assert any("backfilled" in ln for ln in f.logs)


def test_gated_component_is_never_reauthored():
    f = _Fakes()
    f._records = [LayoutComponentRecord(
        frozenset({"ErrorImpl"}), LayoutOutcome.GATED)]
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert not res.fired
    assert not f.committed


def test_no_component_recorded_does_not_fire():
    f = _Fakes(frozen=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert not res.fired
    assert not f.committed


def test_error_not_naming_private_type_does_not_fire():
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                           "error[E0308]: mismatched types", f.deps())
    assert not res.fired
    assert not f.committed


def test_cap_reached_does_not_fire():
    f = _Fakes(attempts={"ErrorImpl": 2})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert not res.fired
    assert not f.committed


def test_reauthor_failure_is_fired_but_not_succeeded():
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and not res.succeeded
    assert f.reverted_calls == []  # never reached the revert step


def test_crash_evidence_fires_without_compile_text_and_adds_guidance():
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    f = _Fakes()
    # Output has NO layout-attributable compile error — only crash evidence
    # (a vanished test whose reverse-map target references ErrorImpl).
    ev = CrashEvidence(
        vanished=frozenset({"test_boxed_str"}),
        suspect_fns=frozenset({FnLocator("src/error.rs", "fmt")}))
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                           "test result: FAILED. 14 passed; 55 failed",
                           f.deps(), crash_evidence=ev)
    assert res.fired and res.succeeded and res.cause == "RUNTIME_CRASH"
    assert any("RUNTIME-CRASH GUIDANCE" in p for p in f.prompts)
    # Cap still applies to the crash channel.
    f2 = _Fakes(attempts={"ErrorImpl": 2})
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f2.deps(), crash_evidence=ev)
    assert not res2.fired


def test_crash_evidence_confounded_by_component_stubs_defers():
    # Most of the component's own fns are still stubs (e.g. right after a
    # successful re-author's revert) — a stub panic aborts binaries exactly
    # like a bad scheme, so the attempt must NOT be spent yet.
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    f = _Fakes(fn_records=[
        FnRecord(file="error.rs", fn_name="fmt",
                 sig_text="fn fmt(&self) -> fmt::Result",
                 body_text="unimplemented!()", has_unimplemented_body=True)])
    f._records = [LayoutComponentRecord(
        frozenset({"ErrorImpl", "fmt"}), LayoutOutcome.FROZEN)]
    ev = CrashEvidence(
        vanished=frozenset({"test_boxed_str"}),
        suspect_fns=frozenset({FnLocator("src/error.rs", "fmt")}))
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                           "", f.deps(), crash_evidence=ev)
    assert not res.fired
    assert not f.committed          # no attempt burned, no checkpoint
    assert any("confounded" in ln for ln in f.logs)


def test_crash_evidence_unrelated_suspects_do_not_fire():
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    f = _Fakes()
    ev = CrashEvidence(vanished=frozenset({"test_x"}),
                       suspect_fns=frozenset({FnLocator("src/chain.rs", "next")}))
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                           "", f.deps(), crash_evidence=ev)
    assert not res.fired
    assert not f.committed


def test_mechanical_splice_failure_refunds_the_attempt():
    # The cap bounds MODEL attempts. When authoring dies in the splice/locate
    # infrastructure the model's output was never evaluated against the tree,
    # so the attempt bought no evidence — a mechanical bug must not be able to
    # exhaust the budget (a live cap was spent entirely on a since-fixed
    # "could not locate a frozen definition" failure).
    f = _Fakes(skeleton_def="fn totally_different() {}")  # frozen def absent
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and not res.succeeded
    # 1 spent, then refunded — and the refund COUNTED, so an identical
    # mechanical failure cannot re-fire forever.
    assert f.saved_attempts == {"ErrorImpl": 0, "mech:ErrorImpl": 1}
    assert any("attempt refunded" in m for m in f.logs)
    assert "mechanical" in (res.reason or "")


def test_model_rejection_still_consumes_the_attempt():
    # The complement: the model DID author something and it failed to compile.
    # That is real evidence — the attempt must stay spent.
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and not res.succeeded
    assert f.saved_attempts == {"ErrorImpl": 1}
    assert not any("refunded" in m for m in f.logs)


def test_compile_evidence_fires_without_any_fill_attempt():
    f = _Fakes()
    # No fill attempt and no crash: the cluster's test binaries did not build.
    ev = CompileEvidence(
        digest="tests/test_autotrait.rs:5: error[E0277]: `NonNull<ErrorImpl>` "
               "cannot be sent between threads safely",
        lib_clean=True, consecutive_sweeps=1)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), compile_evidence=ev)
    assert res.fired and res.succeeded and res.cause == "DESIGN_CONFLICT"


def test_compile_evidence_with_broken_lib_does_not_fire():
    f = _Fakes()
    ev = CompileEvidence(
        digest="src/error.rs:5: error[E0277]: `ErrorImpl` cannot be sent",
        lib_clean=False, consecutive_sweeps=3)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), compile_evidence=ev)
    assert not res.fired
    assert not f.committed          # no attempt burned


def test_compile_evidence_takes_precedence_over_stale_error_text():
    # The channel is chosen by the evidence passed, not by whatever text the
    # caller happened to have: a build failure is classified as DESIGN_CONFLICT
    # even when the fill-attempt text would have said LAYOUT_EXHAUSTION.
    f = _Fakes()
    ev = CompileEvidence(
        digest="t.rs:1: error[E0277]: ErrorImpl is not Send", lib_clean=True)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps(), compile_evidence=ev)
    assert res.cause == "DESIGN_CONFLICT"


def test_compile_evidence_respects_the_attempt_cap():
    f = _Fakes(attempts={"ErrorImpl": 2})
    ev = CompileEvidence(
        digest="t.rs:1: error[E0277]: ErrorImpl is not Send", lib_clean=True)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), compile_evidence=ev)
    assert not res.fired


def test_reauthor_lands_in_the_live_tree_before_reverting():
    # `author_layout` splices the SKELETON; the tree cargo builds is seeded from
    # it once and owned thereafter. A re-author that reaches only the skeleton is
    # invisible to every later compile — the reverted fns re-fill against the old
    # layout — so the live splice must happen, and must precede the revert.
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and res.succeeded
    assert f.tree_plans, "the re-authored layout never reached the live tree"
    plan = f.tree_plans[0]
    assert "error.rs" in plan
    rep = plan["error.rs"][0]
    assert rep.old == _FROZEN_DEF            # locates the definition on disk now
    assert rep.new and rep.new != rep.old    # replaced by the authored text
    assert f.reverted_calls, "revert must still run after the live splice"


def test_live_tree_splice_failure_fails_the_retrigger_and_reverts_nothing():
    # Skeleton-only success is the drift this guards: reverting fns then would
    # re-fill them against a layout the build never saw.
    f = _Fakes()
    f.tree_apply_error = "could not locate a definition in src/error.rs"
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and not res.succeeded
    assert "live-tree splice failed" in res.reason
    assert f.reverted_calls == []
    assert any("could NOT be applied to the live tree" in ln for ln in f.logs)


def test_live_tree_keys_come_from_the_live_tree_not_the_skeleton():
    # The two trees agree only until one is edited alone — which is exactly the
    # state a failed propagation leaves behind. Deriving the keys per tree is
    # what lets the next attempt repair it instead of missing forever.
    f = _Fakes()
    drifted = "struct ErrorImpl { vtable: u8, extra: u16 }"
    f.live_files = {"error.rs": drifted + "\n"}
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and res.succeeded
    assert f.tree_plans[0]["error.rs"][0].old == drifted


def test_propagation_resolves_surface_paths_against_the_crate_src_dir():
    # The caller hands this use case the CRATE ROOT (what cargo runs in, what
    # the fn records and reverts are keyed to). A private-surface item's `file`
    # is relative to `src/`, so the propagation must resolve there — handing it
    # the crate root reads no file, locates no item, and fails EVERY component
    # with a mechanical error that reads like a store problem.
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded
    assert f.tree_apply_dirs == [_SRC]
    assert list(f.tree_plans[0]) == ["error.rs"]   # src-relative, not "src/…"


def test_revert_scope_leaves_the_oracle_alone():
    # The suite that has to judge the re-author names the retriggered types more
    # than any production fn does. Reverting those fns stubs the oracle out, and
    # the re-fill that follows is handed a test body to author.
    f = _Fakes(fn_records=[
        FnRecord(file="error.rs", fn_name="fmt",
                 sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
                 body_text="let x: &ErrorImpl = self.inner.get();"),
        FnRecord(file="error.gen_tests_1.rs", fn_name="test_repr", is_test=True,
                 sig_text="fn test_repr()",
                 body_text="assert_eq!(size_of::<ErrorImpl>(), 8);"),
        FnRecord(file="error.gen_tests_1.rs", fn_name="fmt",
                 sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
                 body_text="ErrorImpl::describe(f)"),
    ])
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded
    assert f.reverted_calls == [[FnLocator("src/error.rs", "fmt")]]
    assert res.reverted_count == 1


def test_live_splice_failure_still_records_the_authored_skeleton():
    # The skeleton now holds the new definitions whether or not they reached
    # the live tree. Leaving the store on the old keys strands the two apart
    # with nothing able to repair it; recording them lets the seed-time
    # reconciliation reinstall the layout with no model call at all.
    f = _Fakes()
    f.tree_apply_error = "could not locate a definition in error.rs"
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and not res.succeeded
    assert len(f.outcome_updates) == 1
    upd = f.outcome_updates[0]
    assert upd.authored
    assert dict(upd.authored_defs)["ErrorImpl"] == (
        "struct ErrorImpl { inner: Box<dyn StdError> }")


def test_repeated_mechanical_failures_stop_refiring():
    # A refunded attempt re-fires on the very next sweep, and each re-fire buys
    # a full authoring call. Refunding without bounding turns "a pipeline bug
    # must not exhaust the model budget" into an unbounded burn on that same
    # bug — the cap has to exist on the mechanical side too.
    f = _Fakes()
    f.tree_apply_error = "could not locate a definition in error.rs"
    for _ in range(2):
        assert retrigger_layout(
            Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps()).fired
        f.attempts = dict(f.saved_attempts or {})
    assert f.attempts.get("mech:ErrorImpl") == 2
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert not res.fired
    assert any("mechanical failure" in ln for ln in f.logs)


def test_a_success_clears_the_mechanical_failure_count():
    # The bound is on CONSECUTIVE mechanical failures: once a propagation
    # lands, the count that was blocking must not linger and pre-spend the
    # budget of a later, unrelated re-trigger.
    f = _Fakes(attempts={"mech:ErrorImpl": 1})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.succeeded
    assert "mech:ErrorImpl" not in (f.saved_attempts or {})


def test_unparseable_live_tree_refunds_the_attempt():
    # A propagation failure is mechanical — the model's layout compiled. A
    # pipeline-side bug must never be able to exhaust the capped budget.
    f = _Fakes()
    f.live_files = {}
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and not res.succeeded
    assert f.saved_attempts == {"ErrorImpl": 0, "mech:ErrorImpl": 1}
    assert any("attempt refunded" in ln for ln in f.logs)


# --- an attempt a process kill spent, and never measured ----------------------
def test_checkpoint_marks_the_attempt_in_flight_and_the_outcome_clears_it():
    """The charge is persisted before the authoring call — correctly, or a crash
    could retry it for free. The marker written in the SAME save is what makes
    that charge reversible: it is the only durable evidence distinguishing 'the
    model failed' from 'the process did not come back'."""
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded
    checkpoint = f.attempt_saves[0]
    assert checkpoint == {"ErrorImpl": 1, "inflight:ErrorImpl": 1}
    # …and no save after an outcome may still carry it.
    assert "inflight:ErrorImpl" not in f.attempt_saves[-1]
    assert f.saved_attempts == {"ErrorImpl": 1}


def test_a_failed_attempt_also_clears_the_marker():
    # A measured failure is still a measurement: only an UNRESOLVED attempt may
    # be refunded, or the cap stops bounding model attempts at all.
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and not res.succeeded
    assert "inflight:ErrorImpl" not in f.saved_attempts
    assert f.saved_attempts["ErrorImpl"] == 1     # charged, not refunded


def test_an_interrupted_attempt_is_refunded_before_the_cap_is_read():
    """The live case: `nightly`'s two attempts on the 35-name component were
    both consumed by a process restart mid-authoring — a checkpoint with no
    outcome line — and the third sweep found the cap reached without a single
    model evaluation behind it."""
    f = _Fakes(attempts={"ErrorImpl": 2, "inflight:ErrorImpl": 2})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded       # cap 2 no longer bars the way
    assert any("never reached an outcome" in m for m in f.logs)
    # The settlement is the FIRST write of the sweep: refunded, counted, and
    # the marker gone — all before the cap could be consulted.
    assert f.attempt_saves[0] == {"ErrorImpl": 1, "mech:ErrorImpl": 1}
    # The attempt then re-charges, and succeeding clears the mechanical streak.
    assert f.saved_attempts == {"ErrorImpl": 2}


def test_an_interrupted_attempt_is_refunded_once_not_every_sweep():
    # The reconciliation persists the cleared marker, so a second sweep over
    # the same store finds nothing to repay.
    f = _Fakes(attempts={"ErrorImpl": 2, "inflight:ErrorImpl": 2})
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    first = dict(f.saved_attempts or {})
    f.attempts = first
    f.logs.clear()
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert not any("never reached an outcome" in m for m in f.logs)

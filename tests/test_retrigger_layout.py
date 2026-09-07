"""Pins for the layout re-trigger ACTION (`application/retrigger_layout.py`):
the walk that grades every non-GATED component against the evidence, picks one,
spends an authoring attempt on it and reverts the bodies that referenced it.
The classification and the budgets it consults are pinned one layer down, in
`test_layout_retrigger.py`."""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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
from pipeline_core.domain.layout_retrigger import (
    PAYOFF_PENDING_KEY,
    PAYOFF_ZERO_KEY,
    REJECTION_CONTRACT_EPOCH,
    CompileEvidence,
)
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
                 attempts=None, fn_records=None, skeleton_def=_FROZEN_DEF,
                 code_id=None, stopped_pids=None):
        # `code_id=None` and `stopped_pids=None` are the unwired defaults: no
        # build stamp on a mechanical streak, and no death record to ask — the
        # behaviour every test written before 2026-08-15 pins.
        self.code_id = code_id
        self.stopped_pids = stopped_pids
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
        # The persisted rejection checklist, keyed by component key.
        self.rejection_notes: dict[str, list[str]] = {}

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
            load_rejection_notes=lambda pd, c: {
                k: list(v) for k, v in self.rejection_notes.items()},
            save_rejection_notes=self._save_rejection_notes,
            max_attempts=2,
            code_id=self.code_id,
            deliberate_stop_pids=(None if self.stopped_pids is None
                                  else lambda: frozenset(self.stopped_pids)),
        )

    def _save_rejection_notes(self, pd, c, notes):
        self.rejection_notes = {k: list(v) for k, v in notes.items()}
        return Path(pd) / "_rejections.json"


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
    f = _Fakes(fn_records=_stub_fmt_records())
    # Output has NO layout-attributable compile error — only crash evidence
    # (a vanished test and an observed frame, `fmt`, a still-stubbed body
    # that references ErrorImpl).
    ev = CrashEvidence(
        vanished=frozenset({"test_boxed_str"}),
        suspect_fns=frozenset({FnLocator("src/error.rs", "fmt")}),
        blamed_fns=frozenset({FnLocator("src/error.rs", "fmt")}))
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                           "test result: FAILED. 14 passed; 55 failed",
                           f.deps(), crash_evidence=ev)
    assert res.fired and res.succeeded and res.cause == "RUNTIME_CRASH"
    assert any("RUNTIME-CRASH GUIDANCE" in p for p in f.prompts)
    # Cap still applies to the crash channel.
    f2 = _Fakes(attempts={"ErrorImpl": 2}, fn_records=_stub_fmt_records())
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f2.deps(), crash_evidence=ev)
    assert not res2.fired
    # Reachability alone — the same `fmt` as a suspect and no frame — is an
    # inference and opens no re-author at all.
    f3 = _Fakes(fn_records=_stub_fmt_records())
    res3 = retrigger_layout(
        Path("/x"), "anyhow", "stable", _CRATE, "", f3.deps(),
        crash_evidence=CrashEvidence(
            vanished=frozenset({"test_boxed_str"}),
            suspect_fns=frozenset({FnLocator("src/error.rs", "fmt")})))
    assert not res3.fired and not f3.committed and f3.prompts == []
    assert res3.reason == "no component matched the evidence"


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
        suspect_fns=frozenset({FnLocator("src/error.rs", "fmt")}),
        blamed_fns=frozenset({FnLocator("src/error.rs", "fmt")}))
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


def test_a_rejected_reauthor_spends_the_rejection_budget_not_the_cap():
    """The destruction cap counts LANDED re-authors only. A rejected one —
    validation or rustc — destroyed nothing, yet charging it against the cap
    closed the only structural escape for a localized memory fault: on
    `anyhow` 2026-08-09/10 the 34-name component reached its cap of 2 on two
    t1.5 rejections (`struct ErrorInner` head change, then a `pub` field),
    with no re-author ever applied and nothing reverted, while ASAN kept
    blaming `object_drop` every iteration. Rejections get their own
    consecutive budget, cleared by an accepted re-author."""
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and not res.succeeded and res.rejected
    # `contract:` stamps the constraint set the streak was earned under, so a
    # later prompt that states a rule this one did not cannot be convicted by it.
    assert f.saved_attempts == {"ErrorImpl": 0, "rejected:ErrorImpl": 1,
                                "contract:ErrorImpl": REJECTION_CONTRACT_EPOCH}
    assert any("rejection" in m for m in f.logs)


def test_three_consecutive_rejections_close_the_walk_loudly():
    """Bounded, deliberately: a deterministic crash re-fires every iteration
    and each fire costs authoring calls, so rejections cannot be free — but
    the refusal is attempt-invariant and the line must read as RED, not as
    a budget ticking down."""
    f = _Fakes(attempts={"rejected:ErrorImpl": 3,
                         "contract:ErrorImpl": REJECTION_CONTRACT_EPOCH})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert not res.fired
    assert not f.committed
    assert any("REJECTED" in m and "3" in m for m in f.logs)


def test_a_streak_earned_under_an_older_prompt_does_not_close_the_walk():
    """The same three rejections WITHOUT the contract stamp are legacy: they
    were earned under an authoring prompt that stated fewer constraints than
    this one does. Run `929616-ee20d540` reached 2/3 on two restatements of the
    same dropped-member violation, under a prompt that named the rule only in a
    list of past violations; retiring the crashed family's only structural
    escape on that evidence is what this releases — once, and the budget then
    counts from zero under the current constraint set."""
    f = _Fakes(compiles=False, attempts={"rejected:ErrorImpl": 3})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and res.rejected
    assert f.saved_attempts["rejected:ErrorImpl"] == 1
    assert (f.saved_attempts["contract:ErrorImpl"]
            == REJECTION_CONTRACT_EPOCH)
    assert any("retired" in m for m in f.logs)


def test_an_accepted_reauthor_clears_the_rejection_streak():
    f = _Fakes(attempts={"rejected:ErrorImpl": 2})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.fired and res.succeeded
    assert f.saved_attempts is not None
    assert "rejected:ErrorImpl" not in f.saved_attempts


def test_a_rejected_fire_opens_no_payoff_claim():
    """The payoff budget asks what a fire BOUGHT. A rejected re-author changed
    nothing, so the claim it opens can only ever settle against the cluster's
    zero-payoff cap — the same argument the mechanical refund already states.
    A landed fire on the same walk still opens one."""
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps(), best_pass=50)
    assert res.fired and res.rejected
    assert f.saved_attempts is not None
    assert "payoff:pending" not in f.saved_attempts
    f2 = _Fakes()
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                            f2.deps(), best_pass=50)
    assert res2.fired and res2.succeeded
    assert f2.saved_attempts is not None
    assert "payoff:pending" in f2.saved_attempts


def test_rejection_reasons_reach_the_next_fires_prompt():
    """Across FIRES nothing was carried at all: six authoring attempts across
    the two 2026-08-09/10 fires cycled renamed struct -> pub field -> dropped
    member, each fixing the last violation while recommitting an earlier one.
    A rejection's reasons persist beside the attempts store and open the next
    fire's prompt as a checklist; an accepted re-author clears them."""
    f = _Fakes(compiles=False)
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert f.rejection_notes.get("ErrorImpl"), (
        "a rejection must persist its reasons for the next fire")
    f2 = _Fakes(attempts={"ErrorImpl": 1})
    f2.rejection_notes = {
        "ErrorImpl": ["authored Error marks a field `pub`, widening the "
                      "public API"]}
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f2.deps())
    assert res.fired and res.succeeded
    assert any("widening the public API" in p for p in f2.prompts), (
        "the next fire's authoring prompt must carry every constraint an "
        "earlier fire's reply violated")
    assert not f2.rejection_notes.get("ErrorImpl"), (
        "an accepted re-author retires the checklist")


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
    # The marker names the process holding it, so the next run can ask the death
    # record how THAT process ended instead of assuming the most recent death
    # was this one's.
    assert checkpoint == {"ErrorImpl": 1, "inflight:ErrorImpl": 1,
                          "inflightpid:ErrorImpl": os.getpid()}
    # …and no save after an outcome may still carry either row.
    assert "inflight:ErrorImpl" not in f.attempt_saves[-1]
    assert "inflightpid:ErrorImpl" not in f.attempt_saves[-1]
    assert f.saved_attempts == {"ErrorImpl": 1}


def test_a_failed_attempt_also_clears_the_marker():
    # A measured failure is still a measurement: the in-flight marker clears
    # and the outcome lands in a budget. Since a rejected re-author destroyed
    # nothing, that budget is the rejection one — the destruction cap counts
    # landed re-authors only.
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and not res.succeeded
    assert "inflight:ErrorImpl" not in f.saved_attempts
    assert f.saved_attempts["rejected:ErrorImpl"] == 1
    assert f.saved_attempts["ErrorImpl"] == 0     # measured, not destroyed


def test_an_operator_stop_is_refunded_with_no_mechanical_charge():
    """Live on `anyhow`, 2026-08-15: runs `462635-c8369188` (SIGTERM at 56m56s)
    and `496645-9b9de7e1` (SIGTERM at 24s) were stopped by the operator while
    the 34-name construct/vtable component was authoring. Each refund charged a
    mechanical failure, the cap of 2 filled, and run `500301-6a66bf86` refused
    that component at iter 1 having just graded it OWNS off an in-crate ASAN
    origin (`src/error.rs::drop`) — no model attempt had failed at all."""
    f = _Fakes(attempts={"ErrorImpl": 1, "inflight:ErrorImpl": 1,
                         "inflightpid:ErrorImpl": 462635},
               stopped_pids={462635})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded
    assert "mech:ErrorImpl" not in f.attempt_saves[0]
    assert any("NOT counted as a mechanical failure" in m for m in f.logs)
    assert any("stopped deliberately" in m for m in f.logs)


def test_a_death_no_record_calls_deliberate_still_charges_at_the_seam():
    """The negative control at the driver seam: identical store, and the pid the
    death record names is a different process. The charge stands — the case the
    bound exists for (an authoring call that kills its own process) is exactly
    the one no death record marks as a stop."""
    f = _Fakes(attempts={"ErrorImpl": 1, "inflight:ErrorImpl": 1,
                         "inflightpid:ErrorImpl": 462635},
               stopped_pids={999999})
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert f.attempt_saves[0]["mech:ErrorImpl"] == 1
    assert any("counted as a mechanical failure" in m for m in f.logs)


def test_a_streak_from_an_earlier_build_no_longer_refuses_the_aim():
    """The live store's shape on 2026-08-15 — `mech:` at the cap of 2, written
    by two builds ago, and no path in the code able to lower it (the only clear
    is the accepted re-author the cap itself refuses to reach). A build that has
    not failed here yet must be allowed to try; the pre-fix reader, which names
    no build, is the negative control below."""
    f = _Fakes(attempts={"mech:ErrorImpl": 2}, code_id="459594789b98")
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert res.fired and res.succeeded

    unstamped = _Fakes(attempts={"mech:ErrorImpl": 2})
    blocked = retrigger_layout(
        Path("/x"), "anyhow", "stable", _CRATE, _ERR, unstamped.deps())
    assert not blocked.fired
    assert any("mechanical failure(s) already refunded" in m
               for m in unstamped.logs)


def test_a_streak_this_build_earned_still_refuses_the_aim():
    f = _Fakes(attempts={"mech:ErrorImpl": 2,
                         "mechbuild:ErrorImpl": int("459594789b98", 16)},
               code_id="459594789b98")
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps())
    assert not res.fired
    assert any("mechanical failure(s) already refunded" in m for m in f.logs)


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


def test_a_capped_component_no_longer_hands_the_walk_to_the_next_one():
    """The 2026-08-02 `anyhow`/nightly limit cycle, at the action.

    The per-component cap held perfectly and bought nothing: the loop
    `continue`s past a capped component to the next candidate, so with two
    components recorded the cluster got a second re-author aimed by the same
    unchanged evidence. Live, that was 17 components — 34 attempts, each
    re-stubbing bodies for pass 93 -> 79 and each refilling to exactly 93.

    Two fires that leave `best_pass` where they found it now stop the walk, and
    the TDD driver's own plateau exit — which the module docstring always said
    a capped component degrades to — finally gets to run."""
    f = _Fakes(attempts={"ErrorImpl": 2})   # first component already at cap
    f._records = [
        LayoutComponentRecord(frozenset({"ErrorImpl"}), LayoutOutcome.FROZEN),
        # `_ERR` names this one too — as the live error text named each
        # component the walk reached in turn.
        LayoutComponentRecord(frozenset({"vtable"}), LayoutOutcome.FROZEN),
    ]
    # Two fires at the same score, settled by the third decision point.
    for _ in range(2):
        retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                         f.deps(), best_pass=93)
        f.attempts = dict(f.saved_attempts or f.attempts)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps(), best_pass=93)
    assert not res.fired
    assert res.reason == "cluster payoff budget spent"
    assert any("the walk stops here" in ln for ln in f.logs)


def test_a_cluster_that_starts_moving_again_gets_its_retriggers_back():
    """The budget bounds a walk, not the mechanism: one sweep that beats the
    high-water mark clears the streak, so the next stall may still retrigger."""
    f = _Fakes(attempts={"ErrorImpl": 2})
    f._records = [
        LayoutComponentRecord(frozenset({"ErrorImpl"}), LayoutOutcome.FROZEN),
        # `_ERR` names this one too — as the live error text named each
        # component the walk reached in turn.
        LayoutComponentRecord(frozenset({"vtable"}), LayoutOutcome.FROZEN),
    ]
    for _ in range(2):
        retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                         f.deps(), best_pass=93)
        f.attempts = dict(f.saved_attempts or f.attempts)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps(), best_pass=97)
    assert res.reason != "cluster payoff budget spent"


def _walk_pair() -> _Fakes:
    """A cluster whose first component is already at cap, so the walk hands the
    evidence to the next one — the shape the payoff budget bounds. `_ERR` names
    every one of them, as the live error text named each component the walk
    reached in turn."""
    f = _Fakes(attempts={"ErrorImpl": 2})
    f._records = [
        LayoutComponentRecord(frozenset({"ErrorImpl"}), LayoutOutcome.FROZEN),
        LayoutComponentRecord(frozenset({"vtable"}), LayoutOutcome.FROZEN),
        # A third, still unspent — 17 were recorded live. Reached only once the
        # two above are out of budget, which is exactly what "the walk" means.
        LayoutComponentRecord(frozenset({"ErrorImpl", "vtable"}),
                              LayoutOutcome.FROZEN),
    ]
    return f


def test_a_refunded_attempt_does_not_spend_the_cluster_budget():
    """A splice/locate failure REFUNDS the attempt — 'a pipeline bug can never
    exhaust the budget meant for the model'. A cluster-wide budget is still a
    budget: charged at the open, two mechanical failures would stop the walk
    across every remaining component, and the refusal would report bodies
    re-stubbed by attempts that reverted nothing and re-authored nothing."""
    f = _walk_pair()
    f.tree_apply_error = "live tree file unreadable: error.rs"
    reasons = []
    for _ in range(3):
        res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                               f.deps(), best_pass=93)
        f.attempts = dict(f.saved_attempts or f.attempts)
        reasons.append(res.reason)
    # Two refunded fires on one component, and the third call refused by that
    # component's OWN mechanical budget — the cap that is answerable for this
    # failure. Pre-fix the third call read "cluster payoff budget spent", which
    # also takes the untouched third component down with it.
    assert reasons[0].startswith("live-tree splice failed")
    assert "cluster payoff budget spent" not in reasons, reasons
    assert not any("the walk stops here" in ln for ln in f.logs)
    assert (f.saved_attempts or {}).get(PAYOFF_ZERO_KEY, 0) == 0


def test_a_kill_inside_the_authoring_call_leaves_no_payoff_claim():
    """`reconcile_interrupted_attempts` repays an attempt whose process died in
    the ~50-minute authoring call, so the budget means model attempts and not
    process lifetimes. The payoff claim has to obey the same rule, and the only
    way it can is to not exist yet: the store as it stands when the charge is
    written is exactly what a kill leaves behind."""
    f = _walk_pair()
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps(),
                     best_pass=93)
    at_charge = next(s for s in f.attempt_saves
                     if any(k.startswith("inflight:") for k in s))
    assert PAYOFF_PENDING_KEY not in at_charge, at_charge
    # …and replaying that state as the next process reads it refunds the
    # attempt without charging the cluster for it.
    g = _walk_pair()
    g.attempts = dict(at_charge)
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, g.deps(),
                     best_pass=93)
    assert any("never reached an outcome" in ln for ln in g.logs)
    assert (g.saved_attempts or {}).get(PAYOFF_ZERO_KEY, 0) == 0


def test_a_spent_walk_still_reaches_the_broken_build_repair_route():
    """The budget bounds the PLATEAU walk, whose evidence is the same error
    text every sweep. A cluster-wide BUILD failure is a new event and the only
    route that acts while no test can report a verdict — and refusing it here
    is a one-way door: nothing compiles, so `best_pass` can never rise, so the
    streak that refused it can never clear itself."""
    f = _walk_pair()
    for _ in range(2):
        retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                         f.deps(), best_pass=93)
        f.attempts = dict(f.saved_attempts or f.attempts)
    assert retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                            f.deps(), best_pass=93).reason == (
        "cluster payoff budget spent")
    res = retrigger_layout(
        Path("/x"), "anyhow", "stable", _CRATE, _ERR, f.deps(), best_pass=93,
        compile_evidence=CompileEvidence(digest=_ERR, lib_clean=True,
                                         consecutive_sweeps=1))
    assert res.fired
    assert res.reason != "cluster payoff budget spent"


def test_a_caller_that_never_scores_is_unaffected():
    """Every pin above this one calls without `best_pass`. An unscored fire
    records nothing and can never be refused, so the budget is strictly opt-in
    on measurement."""
    f = _Fakes()
    for _ in range(4):
        res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                               f.deps())
        f.attempts = dict(f.saved_attempts or f.attempts)
        assert res.reason != "cluster payoff budget spent"
    assert not any(k.startswith("payoff:") for k in (f.saved_attempts or {}))


def _blame_collision(*, stub_blamed: bool = False) -> _Fakes:
    """Cluster `stable` as it stood at 2026-08-08T08:19Z, reduced to the two
    components that mattered. Registry order puts the private `StdError` helper
    trait (`src/context.rs`) at a LOWER index than the component that declares
    the blamed fn, and the blamed fn's return type spells `StdError` because
    that is the crate's alias for `std::error::Error` — a different symbol.

    `stub_blamed=True` is the layout-route case: a stub body still needs a
    component selected. The live class is a filled body (`stub_blamed=False`)
    — that must not open a re-author at all.
    """
    rec = FnRecord(file="error.rs", fn_name="object_reallocate_boxed",
                 sig_text=("unsafe fn object_reallocate_boxed(e: Own<ErrorImpl>)"
                           " -> Box<dyn StdError + Send + Sync + 'static>"),
                 body_text="let unerased = e.cast::<ErrorImpl<E>>().boxed();")
    if stub_blamed:
        rec = replace(rec, has_unimplemented_body=True,
                      body_text="unimplemented!()")
    # Confound is stubbed*2 >= members. One stub and no siblings is always
    # confounded, so a stub blamed body would never reach the walk. Two
    # filled siblings keep the layout route open for the stub case.
    siblings = [
        FnRecord(file="error.rs", fn_name="object_drop",
                 body_text="unsafe { (e.vtable.object_drop)(e) }"),
        FnRecord(file="error.rs", fn_name="vtable",
                 body_text="ErrorVTable { object_drop: object_drop }"),
    ]
    f = _Fakes(fn_records=[rec, *siblings])
    f._records = [
        LayoutComponentRecord(frozenset({"StdError"}), LayoutOutcome.FROZEN),
        LayoutComponentRecord(
            frozenset({"ErrorImpl", "object_reallocate_boxed", "vtable",
                       "object_drop"}),
            LayoutOutcome.FROZEN),
    ]
    return f


def _stub_fmt_records() -> list[FnRecord]:
    """Canonical fakes with `fmt` flagged as a stub so the layout route
    stays open, while the body still spells ErrorImpl so REFERENCES
    grading is unchanged. The default `_Fakes` `fmt` is filled; blaming
    that must not fire."""
    return [
        FnRecord(file="error.rs", fn_name="fmt",
                 sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
                 body_text=("let x: &ErrorImpl = self.inner.get(); "
                            "write!(f, \"{}\", x)"),
                 has_unimplemented_body=True),
        FnRecord(file="chain.rs", fn_name="next",
                 sig_text="fn next(&mut self) -> Option<u8>", body_text="None"),
    ]


def _uaf_evidence():
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    return CrashEvidence(
        vanished=frozenset({"test_reallocate_into_boxed_dyn_error_without_backtrace_io"}),
        suspect_fns=frozenset(),
        blamed_fns=frozenset({FnLocator("src/error.rs",
                                        "object_reallocate_boxed")}))


def test_the_crash_walk_fires_at_the_component_that_declares_the_blamed_fn():
    """Registry index is not evidence. The declarer is recorded SECOND here and
    must still be the one re-authored. A stub body keeps the layout route
    open; a filled body is the next test."""
    f = _blame_collision(stub_blamed=True)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=_uaf_evidence())
    assert res.fired and res.succeeded and res.cause == "RUNTIME_CRASH"
    assert "object_reallocate_boxed" in (f.committed[0] if f.committed else "")
    assert "StdError" not in (f.committed[0] if f.committed else "")


def test_filled_body_frame_blame_does_not_open_a_reauthor():
    """The live class: ASAN named a filled conversion. Cause C's first move
    is to revert that body. The fill loop already has the blame as crash
    evidence. Negative control: the same records with a stub body still
    fire at the declarer, not StdError."""
    f = _blame_collision()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=_uaf_evidence())
    assert not res.fired
    assert res.reason == "filled-body frame blame"
    assert f.committed == []
    assert f.tree_plans == []
    assert f.reverted_calls == []
    assert any("names a FILLED body" in ln for ln in f.logs)
    g = _blame_collision(stub_blamed=True)
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                            g.deps(), crash_evidence=_uaf_evidence())
    assert res2.fired and res2.succeeded and res2.cause == "RUNTIME_CRASH"
    assert "object_reallocate_boxed" in (g.committed[0] if g.committed else "")
    assert "StdError" not in (g.committed[0] if g.committed else "")


def test_a_capped_declarer_stops_the_crash_walk_instead_of_demoting_to_a_mention():
    """The trap, at the action. ASAN blamed
    `src/error.rs::object_reallocate_boxed`; its component was at 2 of a cap of
    2, so the walk `continue`d and re-authored `StdError` — a five-line private
    trait in `src/context.rs` that the blamed fn cannot even see — reverting 29
    bodies that spell the token and taking the cluster from 49 passing to 24.

    'The fault is in code this component owns, and this component may not be
    re-authored again' is a complete answer. Nothing is re-authored, nothing is
    reverted, and the sweep goes on to fill: the blamed fn is already in the
    frontier carrying its ASAN report, which is the retry a body fault wants."""
    f = _blame_collision(stub_blamed=True)
    f.attempts = {"ErrorImpl|object_drop|object_reallocate_boxed|vtable": 2}
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=_uaf_evidence())
    assert not res.fired
    assert res.reason == "the faulting component is not re-authorable"
    assert f.reverted_calls == []
    assert f.committed == []
    assert f.prompts == []          # no layout-authoring model call was spent
    assert any("merely references" in ln for ln in f.logs)


def test_a_crash_no_component_declares_still_reaches_the_ones_it_mentions():
    """The tier must not make cause C weaker where it worked: with no declarer
    among the candidates the referencing tier is the whole of the evidence, and
    it still fires."""
    f = _Fakes(fn_records=_stub_fmt_records())
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    ev = CrashEvidence(vanished=frozenset({"test_boxed_str"}),
                       suspect_fns=frozenset(),
                       blamed_fns=frozenset({FnLocator("src/error.rs", "fmt")}))
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=ev)
    assert res.fired and res.succeeded and res.cause == "RUNTIME_CRASH"


# --- cause C fires remember the crash they were spent on -----------------------
# Run `58819-ee78a1d1` (2026-08-13), iters 3-6: `Quoted` landed attempt 1
# against a deterministic UAF, the identical crash reproduced two iterations
# later, and the walk re-picked `Quoted` on registry order — pass oscillated
# 79/74/79/74 at ~2.1h a cycle until the operator killed the run. The pins
# below are that cycle, replayed against the action.

def _uaf_ev():
    """A cause-C payload that ADMITS: an observed frame at `fmt`, a
    still-stubbed body (`_stub_fmt_records`) that references ErrorImpl.
    Reachability alone admitted this component until 2026-09-06; the cycle
    below is about the walk's memory, not the aim, so the payload carries
    the one evidence kind that still aims."""
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    return CrashEvidence(
        vanished=frozenset({"test_boxed_str"}),
        suspect_fns=frozenset({FnLocator("src/error.rs", "fmt")}),
        blamed_fns=frozenset({FnLocator("src/error.rs", "fmt")}))


def _stub_fmt_and_source_records() -> list[FnRecord]:
    """`_stub_fmt_records` plus a second stubbed ErrorImpl method, so a
    test can hand the walk a blame the first fire never had."""
    return _stub_fmt_records() + [
        FnRecord(file="error.rs", fn_name="source",
                 sig_text="fn source(&self) -> Option<&ErrorImpl>",
                 body_text="unimplemented!()", has_unimplemented_body=True),
    ]


_NEW_BLAME = frozenset({FnLocator("src/error.rs", "fmt"),
                        FnLocator("src/error.rs", "source")})


def _uaf_fakes(**kw) -> _Fakes:
    return _Fakes(fn_records=_stub_fmt_records(), **kw)


def test_a_landed_fire_is_refuted_when_the_same_crash_comes_back():
    """Fire 1 lands; the identical crash survives it; fire 2 on the same
    component is REFUSED. On the pre-fix code this second call landed attempt
    2 of the destruction cap — the recorded limit cycle."""
    f = _uaf_fakes()
    res1 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f.deps(), crash_evidence=_uaf_ev())
    assert res1.fired and res1.succeeded
    f2 = _uaf_fakes(attempts=f.saved_attempts)
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f2.deps(), crash_evidence=_uaf_ev())
    assert not res2.fired
    assert not f2.committed          # no checkpoint: no attempt was charged
    assert any("refuted for this signature" in ln for ln in f2.logs)


def test_a_rejected_fire_does_not_immunize_its_component():
    """A rejection changed nothing in the tree, so it proves nothing about
    the crash: the same component must stay eligible for the same signature.
    (Run `58819-ee78a1d1` iter 1: the mega component's 3 authorings were all
    rejected — treating that as refutation would have retired the plausible
    owner of the drop-glue fault untested.)"""
    f = _uaf_fakes(compiles=False)
    res1 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f.deps(), crash_evidence=_uaf_ev())
    assert res1.fired and res1.rejected and not res1.succeeded
    f2 = _uaf_fakes(attempts=f.saved_attempts)
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f2.deps(), crash_evidence=_uaf_ev())
    assert res2.fired and res2.succeeded
    assert not any("refuted for this signature" in ln for ln in f2.logs)


def test_a_new_localizer_blame_reopens_a_refuted_component():
    """The refutation is per-signature: an origin-path blame arriving is new
    evidence, and the component may be re-authored on it (still bounded by
    the destruction cap)."""
    from dataclasses import replace as dc_replace
    f = _uaf_fakes()
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                     "", f.deps(), crash_evidence=_uaf_ev())
    ev2 = dc_replace(_uaf_ev(), blamed_fns=_NEW_BLAME)
    f2 = _Fakes(attempts=f.saved_attempts,
                fn_records=_stub_fmt_and_source_records())
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f2.deps(), crash_evidence=ev2)
    assert res2.fired and res2.succeeded


def test_a_shrunken_vanished_set_does_not_reopen_the_rounds_budget():
    """The rounds budget is keyed on the BLAME signature, not the
    vanished-inclusive one. Two rejected rounds against vanished {a, b}; the
    loop then fixes one test and the crash comes back as {b} with the same
    blame. Keyed on the full signature the `sigr:` row is not found and the
    same blame buys a fresh cap of ~55-minute rounds — once per distinct
    subset, which for an N-test crash is up to 2^N−1 budgets. The refusal
    must hold: nothing the localizer knows has changed."""
    from dataclasses import replace as dc_replace
    ev_wide = dc_replace(_uaf_ev(),
                         vanished=frozenset({"test_boxed_str", "test_rev"}))
    f = _uaf_fakes(compiles=False)
    res1 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f.deps(), crash_evidence=ev_wide)
    assert res1.fired and res1.rejected
    f2 = _uaf_fakes(attempts=f.saved_attempts, compiles=False)
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f2.deps(), crash_evidence=ev_wide)
    assert res2.fired and res2.rejected
    ev_shrunk = dc_replace(_uaf_ev(), vanished=frozenset({"test_boxed_str"}))
    f3 = _uaf_fakes(attempts=f2.saved_attempts, compiles=False)
    res3 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f3.deps(), crash_evidence=ev_shrunk)
    assert not res3.fired, (
        "a vanished set the loop shrank by working re-opened the rounds "
        "budget — the sigr: key moved with the progress it is supposed to "
        "be indifferent to")
    assert any("authoring round(s) of a cap of" in ln for ln in f3.logs)


def test_a_new_blame_opens_a_fresh_rounds_budget():
    """…and the one event the refusal line promises DOES re-open it: the
    localizer learning something new. Same two spent rounds; the third call
    arrives with an origin blame the first two never had."""
    from dataclasses import replace as dc_replace
    f = _uaf_fakes(compiles=False)
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                     "", f.deps(), crash_evidence=_uaf_ev())
    f2 = _uaf_fakes(attempts=f.saved_attempts, compiles=False)
    retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                     "", f2.deps(), crash_evidence=_uaf_ev())
    ev_blamed = dc_replace(_uaf_ev(), blamed_fns=_NEW_BLAME)
    f3 = _Fakes(attempts=f2.saved_attempts,
                fn_records=_stub_fmt_and_source_records())
    res3 = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                            "", f3.deps(), crash_evidence=ev_blamed)
    assert res3.fired and res3.succeeded


def test_two_landed_fires_on_one_signature_close_the_whole_walk():
    """The signature payoff budget bounds the WALK: with two landed cause-C
    re-authors each survived by the identical crash, a third fire is refused
    before any component is graded — the projected iter-7 mega-component
    destruction is exactly what this stops."""
    from pipeline_core.domain.layout_retrigger import (
        crash_signature,
        record_crash_fire,
        settle_crash_payoff,
    )
    sig = crash_signature(_uaf_ev())
    attempts = settle_crash_payoff(record_crash_fire({}, sig, "Quoted"), sig)
    attempts = settle_crash_payoff(
        record_crash_fire(attempts, sig, "Backtrace|Error"), sig)
    f = _uaf_fakes(attempts=attempts)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE,
                           "", f.deps(), crash_evidence=_uaf_ev())
    assert not res.fired
    assert res.reason == "cause-C signature payoff budget spent"
    assert not f.committed
    assert any("left this exact crash reproducing" in ln for ln in f.logs)


def test_the_rejection_checklist_dedups_by_constraint_not_by_text():
    """`drops member(s) ... `ptr` (25 reads)` and the same sentence at
    `(29 reads)` are one constraint restated after four more bodies landed.
    Exact-text dedup stored both (live store, 2026-08-14); with a bounded list
    of 12 that is how one stubborn constraint evicts every other one."""
    from pipeline_core.application.retrigger_layout import _remember_rejections

    saved: dict[str, list[str]] = {}
    deps = SimpleNamespace(
        load_rejection_notes=lambda p, c: dict(saved),
        save_rejection_notes=lambda p, c, n: saved.update(n),
        log=lambda m: None)
    first = "authored Error drops member(s) ...: `ptr` (25 reads). Keep them."
    second = "authored Error drops member(s) ...: `ptr` (29 reads). Keep them."
    _remember_rejections(Path("/x"), "stable", "K", [first], deps)
    _remember_rejections(Path("/x"), "stable", "K", [second], deps)
    assert saved["K"] == [second], "the newest measurement keeps the one slot"


def test_a_mechanical_reason_never_becomes_a_constraint_to_satisfy():
    """The checklist is presented as "each line is a constraint an earlier
    reply violated; your reply must satisfy ALL of them at once". `could not
    locate a frozen definition to replace: CastTo in ptr.rs` is not satisfiable
    by any reply. It reaches the rejection path whenever ONE component of a
    mixed batch fails mechanically while another is really rejected — the
    refund branch tests `all()`."""
    from pipeline_core.application.retrigger_layout import _remember_rejections

    saved: dict[str, list[str]] = {}
    deps = SimpleNamespace(
        load_rejection_notes=lambda p, c: dict(saved),
        save_rejection_notes=lambda p, c, n: saved.update(n),
        log=lambda m: None)
    real = "authored Error marks a field `pub`, widening the public API"
    mech = "could not locate a frozen definition to replace: CastTo in ptr.rs"
    _remember_rejections(Path("/x"), "stable", "K", [real, mech], deps)
    assert saved["K"] == [real]


# --- the declined walk says why, and the corrected live expectation ------------
# Run `263217-d41a5bb5` (2026-08-15): the crash aim was handed the crashing
# test's own two `src/error.gen_tests_4.rs` frames as `blamed_fns`, they graded
# NONE against all 16 non-GATED components, `candidates` emptied, and the walk
# took `return RetriggerResult(fired=False, reason="no component matched the
# evidence")` — the one exit in this module that logged nothing. 308 minutes,
# zero `[layout-retrigger]` lines, while the prompt/void/frontier consumers of
# the same crash kept printing. Silence at this gate now reads as a regression
# signal because every reachable outcome prints.

# The live component key, verbatim from
# `tmp/anyhow/.skeleton/_layout_retrigger_attempts_stable.json` (2026-08-15).
_MEGA_KEY = (
    "Backtrace|CastTo|Error|ErrorImpl|ErrorVTable|Mut|Own|Ref|backtrace|chain|"
    "construct|construct_from_adhoc|construct_from_boxed|"
    "construct_from_context|construct_from_display|construct_from_std|"
    "context_backtrace|context_chain_downcast|context_chain_drop_rest|"
    "context_downcast|context_drop_rest|debug|display|erase|error|error_mut|"
    "no_backtrace|object_boxed|object_downcast|object_drop|object_drop_front|"
    "object_reallocate_boxed|object_ref|vtable")
_MEGA_NAMES = frozenset(_MEGA_KEY.split("|"))
_RESIDUE = frozenset({
    FnLocator("src/error.gen_tests_4.rs",
              "test_reallocate_into_boxed_dyn_error_without_backtrace"),
    FnLocator("src/error.gen_tests_4.rs", "{closure#0}"),
})
_CHAIN_CONSTRUCT = FnLocator("src/error.rs", "construct")


def _mega_fakes(**kw) -> _Fakes:
    """The cluster reduced to the component the chain implicates, with the
    live tree's `construct` present so nothing reads as confounded. The
    surface's `ErrorImpl` is inside the mega key, so the component is
    genuinely authorable. `file` is SRC-relative like every other fixture
    here — `_fns_for` re-bases it onto whichever dir the caller enumerates.

    `fn_records` is a DEFAULT the caller may replace (a component graded
    against a different set of filled bodies is the obvious next variation);
    setting it as a keyword rather than positionally is what keeps that call
    an override instead of a duplicate-argument TypeError."""
    kw.setdefault("fn_records", [
        FnRecord(file="error.rs", fn_name="construct",
                 sig_text=("unsafe fn construct<E>(error: E, vtable: &'static "
                           "ErrorVTable, backtrace: Option<Backtrace>) -> Self"),
                 body_text=("let inner = Box::new(ErrorImpl { vtable, "
                            "backtrace, _object: error });")),
    ])
    f = _Fakes(**kw)
    f._records = [LayoutComponentRecord(_MEGA_NAMES, LayoutOutcome.FROZEN)]
    return f


def _chain_evidence(blamed=frozenset()):
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    return CrashEvidence(
        vanished=frozenset(
            {"test_reallocate_into_boxed_dyn_error_without_backtrace"}),
        suspect_fns=frozenset({FnLocator("src/chain.rs", "next")}),
        blamed_fns=blamed,
        chain_blamed=frozenset({_CHAIN_CONSTRUCT}))


def test_a_declined_crash_walk_prints_its_tier_and_its_evidence_shape():
    """The 308-minute silence, in miniature: evidence that grades NONE
    everywhere. It still declines — that is correct — but it now says which
    route asked, which tier the grading reached, how many records survived,
    and what the evidence was made of, which is the whole diagnosis."""
    f = _Fakes()
    f._records = [LayoutComponentRecord(frozenset({f"Comp{i}"}),
                                        LayoutOutcome.FROZEN)
                  for i in range(16)]
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=_chain_evidence(_RESIDUE))
    assert not res.fired
    assert res.reason == "no component matched the evidence"
    assert not f.committed
    declined = [ln for ln in f.logs if "no component matched the evidence" in ln]
    assert len(declined) == 1
    line = declined[0]
    assert "crash route" in line and "blame tier NONE" in line
    assert "0 of 16 non-GATED record(s)" in line
    assert ("2 blamed frame(s), 1 chain-blamed, 1 suspect(s), 1 vanished"
            in line)


def test_a_refused_candidate_is_not_reported_as_evidence_that_matched_nothing():
    """The other half of the declined line. Below the OWNS tier the walk also
    ends after the loop, and one line for both outcomes said "no component
    matched the evidence" — false in the direction a reader acts on: the
    evidence named this component, the destruction cap refused it, and that
    refusal printed one line above. `fmt` REFERENCES `ErrorImpl` without
    declaring it, so the tier is REFERENCES and the OWNS exit does not catch
    this path."""
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    f = _Fakes(fn_records=_stub_fmt_records())
    f.attempts = {"ErrorImpl": 2}     # == the `max_attempts` deps() hands over
    ev = CrashEvidence(vanished=frozenset({"test_boxed_str"}),
                       suspect_fns=frozenset(),
                       blamed_fns=frozenset({FnLocator("src/error.rs", "fmt")}))
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=ev)
    assert not res.fired
    assert res.reason == "every matching component was refused"
    assert f.committed == [] and f.prompts == []
    assert any("cap (2) reached" in ln for ln in f.logs)
    refused = [ln for ln in f.logs if "were all refused above" in ln]
    assert len(refused) == 1
    assert "1 candidate(s)" in refused[0]
    assert "blame tier REFERENCES" in refused[0]
    # The line that would have sent the reader looking for missing evidence.
    assert not any("no component matched the evidence" in ln for ln in f.logs)


def test_a_walk_with_nothing_to_aim_at_says_how_many_records_were_gated():
    """The other silent return. `no re-authorable component recorded` cannot
    be told from `the walk was never called` unless it prints."""
    f = _Fakes(frozen=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert not res.fired
    assert any("no re-authorable component recorded" in ln
               and "0 layout record(s) loaded" in ln for ln in f.logs)
    # …and a cluster whose only rows are GATED reports the reason it has none.
    g = _Fakes()
    g._records = [LayoutComponentRecord(frozenset({"ErrorImpl"}),
                                        LayoutOutcome.GATED)]
    assert not retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                                g.deps()).fired
    assert any("1 layout record(s) loaded, 1 of them GATED" in ln
               for ln in g.logs)


def _grade_until_2026_09_06(evidence, fn_records, component_names):
    """`crash_candidate_grade` as it stood from 2026-08-15 to 2026-09-06,
    verbatim — the grade that consulted the chain when no frame implicated
    anything. Kept here as the negative control of the aim tests."""
    from pipeline_core.domain.layout_retrigger import (
        BlameGrade,
        crash_blame_grade,
    )
    if not evidence.vanished:
        return BlameGrade.NONE
    if evidence.blamed_fns:
        frame_grade = crash_blame_grade(
            evidence.blamed_fns, fn_records, component_names)
        if frame_grade is not BlameGrade.NONE or not evidence.chain_blamed:
            return frame_grade
    if evidence.chain_blamed:
        return crash_blame_grade(
            evidence.chain_blamed, fn_records, component_names)
    if not evidence.suspect_fns:
        return BlameGrade.NONE
    return crash_blame_grade(
        evidence.suspect_fns, fn_records, component_names)


def test_the_chain_does_not_aim_the_walk_and_nothing_is_charged(monkeypatch):
    """With `blamed_fns` empty and the chain naming `src/error.rs::construct`
    — a fn the mega component DECLARES — the walk declines: no tier, no
    attempt, no checkpoint, no authoring call, and the declined line names
    the evidence shape. The chain keeps its prompt, exhaustion-void and
    signature roles; it is not an aim.

    Negative control: the grade that stood until 2026-09-06 admits the mega
    component OWNS off that same chain and FIRES attempt 1 — the mechanism
    that re-authored `Quoted` on run `1577437-c93d5c26` iter 3, a type 0 of
    373 fns reference, on a fault the component never touched."""
    f = _mega_fakes(compiles=False,
                    attempts={f"rejected:{_MEGA_KEY}": 2})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=_chain_evidence())
    assert not res.fired
    assert res.reason == "no component matched the evidence"
    assert f.committed == [] and f.prompts == [] and f.reverted_calls == []
    assert f.saved_attempts is None or \
        f"contract:{_MEGA_KEY}" not in f.saved_attempts
    declined = [ln for ln in f.logs if "no component matched the evidence" in ln]
    assert len(declined) == 1
    assert "blame tier NONE" in declined[0]
    assert "0 blamed frame(s), 1 chain-blamed" in declined[0]
    assert not any("the observed fault is inside code" in ln for ln in f.logs)

    import pipeline_core.application.retrigger_layout as _rl
    monkeypatch.setattr(_rl, "crash_candidate_grade", _grade_until_2026_09_06)
    g = _mega_fakes(compiles=False,
                    attempts={f"rejected:{_MEGA_KEY}": 2})
    res_old = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                               g.deps(), crash_evidence=_chain_evidence())
    assert res_old.fired and res_old.rejected
    assert any("the observed fault is inside code 1 component(s) declare" in ln
               for ln in g.logs)
    assert g.committed and "attempt 1" in g.committed[0]


def _framed_mega_evidence():
    """The one evidence kind that still aims: an observed frame at a fn the
    mega component declares, with no record for it (so it cannot be shown
    filled and the layout route stays open)."""
    from dataclasses import replace as dc_replace
    return dc_replace(_chain_evidence(),
                      blamed_fns=frozenset({FnLocator("src/error.rs",
                                                      "object_drop")}))


def test_a_streak_stamped_at_this_epoch_is_the_red_lock_the_entry_predicted():
    """The companion. Three rejections STAMPED at the current epoch are a
    current-contract lock: the walk refuses before any attempt is charged and
    says so as RED. This is what run `263217-d41a5bb5`'s successor would print
    if the re-authors keep violating a contract the prompt now states."""
    f = _mega_fakes(compiles=False, attempts={
        f"rejected:{_MEGA_KEY}": 3,
        f"contract:{_MEGA_KEY}": REJECTION_CONTRACT_EPOCH})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, "",
                           f.deps(), crash_evidence=_framed_mega_evidence())
    assert not res.fired
    assert not f.committed and f.prompts == []
    assert any("REJECTED at validation" in ln for ln in f.logs)
    # The tier was still reached — the refusal is the budget's, not the aim's,
    # so the declined-walk line must NOT claim the evidence matched nothing.
    assert res.reason == "the faulting component is not re-authorable"


def test_negative_control_the_legacy_streak_closes_the_walk_pre_fix(monkeypatch):
    """The same fixture with the constraint epoch rolled back to the value a
    stamp-less record reads as: the staleness rule stops discriminating, the
    2026-08-14 legacy streak convicts again, and the component is refused with
    no attempt made. Without this the test above proves only that the new code
    runs, not that the theory bites on run `929616-ee20d540`."""
    import pipeline_core.domain.layout_retrigger as _lr
    monkeypatch.setattr(_lr, "REJECTION_CONTRACT_EPOCH", 0)
    f = _Fakes(compiles=False, attempts={"rejected:ErrorImpl": 3})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert not res.fired
    assert any("REJECTED" in m for m in f.logs)


# --- 180.1: a stale locate-key must be repaired, and must not kill the fire ---
#
# THE MEASUREMENT. `.skeleton/_layout_outcomes_stable.json` of `tmp/anyhow`
# stores, for `CastTo`, the trait FOLLOWED BY `impl<T> CastTo for T` — and
# `.skeleton/stable/src/ptr.rs` holds the trait alone. For `Mut` it stores
# `core::ptr::NonNull<T>` where the file (which imports `NonNull`) reads
# `NonNull<T>`. Their siblings `Own` and `Ref` in the same file carry the
# qualified spelling and match, which is why only those two components failed.
# Every layout retrigger of that cluster died on them — twice in run
# `1692702-94e6163a` (2026-08-16) alone, at 50m41s/14 model calls and
# 20m57s/7 — each time AFTER the 31-name `Error` component had authored a
# layout that compiled.

_PTR_SKELETON = (
    "use core::ptr::NonNull;\n"
    "pub trait CastTo {\n    type Target;\n}\n"
)
#: What the store holds: the text the model AUTHORED, two items wide.
_CASTTO_STALE = ("pub trait CastTo {\n    type Target;\n}\n\n"
                 "impl<T> CastTo for T {\n    type Target = T;\n}")


def _two_component_surface():
    """`ErrorImpl` (layout-sensitive, in error.rs) and `CastTo` (its own
    component, in ptr.rs) — the live shape: one fire, several components, and
    the crash names only one of them."""
    types = [
        TypeRecord(file="lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="error.rs", kind="struct", name="ErrorImpl",
                   is_pub=False, definition="struct ErrorImpl { vtable: u8 }"),
        TypeRecord(file="ptr.rs", kind="trait", name="CastTo", is_pub=False,
                   definition="pub trait CastTo {\n    type Target;\n}"),
    ]
    return classify_private_surface(types, [], cfg_cluster="stable")


def _authored_record(names, defs):
    return LayoutComponentRecord(frozenset(names), LayoutOutcome.AUTHORED,
                                 tuple(sorted(defs.items())))


def test_a_stale_locate_key_is_re_derived_from_the_scaffold_before_the_call():
    """The repair. The stored key is not in the file the splice will read, so
    it is re-derived from the scaffold — which IS that file — and the fire goes
    ahead instead of dying attempt-invariantly after the model has answered."""
    f = _Fakes(reply='<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
                     'struct ErrorImpl { inner: Box<dyn StdError> }'
                     '</LAYOUT_ITEM>')
    f._records = [_authored_record({"ErrorImpl"},
                                   {"ErrorImpl": "struct ErrorImpl { GONE: u8 }"})]
    # The scaffold as it actually stands — what the splice matches against.
    f.files = {"error.rs": _FROZEN_DEF + "\n"}
    f.skeleton_fns = []
    f.skeleton_types = [
        TypeRecord(file="error.rs", kind="struct", name="ErrorImpl",
                   is_pub=False, definition=_FROZEN_DEF)]

    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())

    assert res.fired and res.succeeded, \
        "a stale key killed the fire that the repair exists to save"
    assert not res.refunded
    assert any("Re-deriving them from the scaffold" in ln for ln in f.logs)
    assert f.prompts, "the model was never asked"


def test_an_unrepairable_component_is_dropped_not_allowed_to_kill_the_fire():
    """180.1's scope fix. `CastTo` cannot be located and cannot be re-derived
    (the scaffold has no record for it either). It is dropped from the aim and
    `ErrorImpl` — the component the evidence actually named — still authors.
    Pre-fix, `res.all_authored` was False and the whole fire refunded."""
    f = _Fakes(reply='<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
                     'struct ErrorImpl { inner: Box<dyn StdError> }'
                     '</LAYOUT_ITEM>')
    f._records = [_authored_record(
        {"ErrorImpl", "CastTo"},
        {"ErrorImpl": _FROZEN_DEF, "CastTo": _CASTTO_STALE})]
    # The skeleton's ptr.rs no longer declares `CastTo` at all, so neither the
    # stored key nor a re-derivation can find it — the case the drop exists
    # for. (A file that still holds the item is repaired by the slice instead,
    # which is the case above.)
    f.files = {"error.rs": _FROZEN_DEF + "\n", "ptr.rs": "use core::ptr;\n"}
    f.live_files = {"error.rs": _FROZEN_DEF + "\n", "ptr.rs": "use core::ptr;\n"}
    f.skeleton_fns = []
    f.skeleton_types = [
        TypeRecord(file="error.rs", kind="struct", name="ErrorImpl",
                   is_pub=False, definition=_FROZEN_DEF)]
    deps = replace(f.deps(), load_surface=lambda pd, c: _two_component_surface())

    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, deps)

    assert res.fired and res.succeeded, \
        "a component the evidence never named discarded one it did"
    assert any("DROPPING" in ln for ln in f.logs)
    # The dropped component was never authored, and the kept one was.
    assert f.prompts and "CastTo" not in "".join(f.prompts)


def test_a_fire_with_nothing_locatable_spends_no_call_at_all():
    """The floor of the same rule: when the drop empties the aim there is
    nothing to author, so the fire must not spend the authoring round it was
    spending before — it refuses and says why."""
    f = _Fakes()
    f._records = [_authored_record({"ErrorImpl"},
                                   {"ErrorImpl": "struct ErrorImpl { GONE: u8 }"})]
    # The item is in neither the file nor the records: nothing to slice, nothing
    # to re-derive.
    f.files = {"error.rs": "use core::fmt;\n"}
    f.skeleton_fns = []
    f.skeleton_types = []

    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())

    assert not f.prompts, "an unauthorable fire still called the model"
    assert not res.succeeded


def test_a_wrapped_declaration_is_repaired_by_slicing_not_by_the_extractor():
    """The correction the first relaunch measured. The scaffold repair used to
    take a type's key from `TypeRecord.definition`, which syn RE-RENDERS — its
    own wrapping, trailing commas the source never had — so for any
    declaration wide enough to wrap the "repaired" key still located nothing.
    Live at iteration 1 of run `1896714`: the backfill reported 34 keys and
    `ErrorVTable`, whose body carries `#[cfg(...)]` fields, matched none of the
    file. Slicing the declaration out of the skeleton is what the live tree's
    own propagation plan has always done, and it is byte-exact by
    construction."""
    wrapped = ("struct ErrorImpl {\n"
               "    #[cfg(feature = \"std\")]\n"
               "    vtable: u8,\n}")
    f = _Fakes(reply='<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
                     'struct ErrorImpl { inner: u8 }</LAYOUT_ITEM>',
               skeleton_def=wrapped)
    f._records = [_authored_record({"ErrorImpl"},
                                   {"ErrorImpl": "struct ErrorImpl { GONE: u8 }"})]
    f.files = {"error.rs": wrapped + "\n"}
    f.live_files = {"error.rs": wrapped + "\n"}
    f.skeleton_fns = []
    # The extractor's rendering — one line, no attribute — is what the old
    # repair stored, and it does not occur in the file.
    f.skeleton_types = [
        TypeRecord(file="error.rs", kind="struct", name="ErrorImpl",
                   is_pub=False, definition="struct ErrorImpl { vtable: u8 }")]

    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())

    assert res.fired and res.succeeded, \
        "the re-derived key still could not be located — the slice is the fix"
    assert not any("DROPPING" in ln for ln in f.logs)


def test_an_unlocatable_component_the_fault_NAMED_refuses_the_whole_fire():
    """The drop's guard, and the reason it exists. A landed re-author reverts
    every body that references the component, so firing at the REMAINDER after
    dropping the aimed component buys that destruction with a layout change the
    evidence never asked for. Measured 2026-08-16 on the first relaunch:
    `ErrorVTable` was unlocatable, its component is the 31-name `Error` one,
    the drop kept 3 of 34 items (`CastTo`, `Mut`, `error_mut` — none named by
    the crash), and the fire reverted 79 bodies and took the cluster 79 -> 25."""
    from pipeline_core.domain.layout_retrigger import CrashEvidence

    f = _Fakes(reply='<LAYOUT_ITEM name="CastTo" kind="trait">'
                     'pub trait CastTo { type Target; }</LAYOUT_ITEM>',
               fn_records=_stub_fmt_records())
    f._records = [_authored_record(
        {"ErrorImpl", "CastTo"},
        {"ErrorImpl": "struct ErrorImpl { GONE: u8 }", "CastTo": _CASTTO_STALE})]
    # `ErrorImpl` is in neither the skeleton file nor the records; `CastTo` is
    # in ptr.rs and repairs by slicing. So the drop would keep CastTo alone.
    f.files = {"error.rs": "use core::fmt;\n", "ptr.rs": _PTR_SKELETON}
    f.live_files = {"error.rs": "use core::fmt;\n", "ptr.rs": _PTR_SKELETON}
    f.skeleton_fns = []
    f.skeleton_types = []
    deps = replace(f.deps(), load_surface=lambda pd, c: _two_component_surface())

    # An ORIGIN blame inside the component that cannot be located: the fault
    # was OBSERVED in the very code the drop is about to discard.
    ev = CrashEvidence(vanished=frozenset({"test_drop"}),
                       suspect_fns=frozenset({FnLocator("src/error.rs", "fmt")}),
                       consecutive_sweeps=2,
                       blamed_fns=frozenset({FnLocator("src/error.rs", "fmt")}))

    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR, deps,
                           crash_evidence=ev)

    assert not res.succeeded
    assert not f.reverted_calls, \
        "bodies were reverted for a re-author of what the fault never named"
    assert any("NOT firing at the remainder" in ln for ln in f.logs), \
        "the fire stopped for some other reason — the guard did not decide it"


def test_the_revert_scope_is_what_was_authored_not_the_fire_s_name_set():
    """A revert exists to re-fill bodies written against a layout that MOVED,
    so a name this attempt never touched has no business in its scope. After
    the locate-key drop the fire's name set can be much wider than the set
    actually authored — 34 against 3 on the live tree — and reverting on the
    wide one is how 79 filled bodies were spent on a three-item re-author."""
    f = _Fakes(reply='<LAYOUT_ITEM name="ErrorImpl" kind="struct">'
                     'struct ErrorImpl { inner: Box<dyn StdError> }'
                     '</LAYOUT_ITEM>')
    # `fmt` reads ErrorImpl; `next` (chain.rs) reads nothing of it.
    res = retrigger_layout(Path("/x"), "anyhow", "stable", _CRATE, _ERR,
                           f.deps())
    assert res.succeeded
    assert f.reverted_calls == [[FnLocator("src/error.rs", "fmt")]]

"""Pins for the F11 M3 layout re-trigger ACTION (application/retrigger_layout.py)."""
from __future__ import annotations

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
from pipeline_core.domain.models import FnLocator, FnRecord, TypeRecord
from pipeline_core.domain.private_surface import classify_private_surface


def _surface():
    types = [
        TypeRecord(file="src/lib.rs", kind="struct", name="Error", is_pub=True,
                   definition="pub struct Error { inner: Box<ErrorImpl> }"),
        TypeRecord(file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
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
        self.fn_records = fn_records if fn_records is not None else [
            FnRecord(file="src/error.rs", fn_name="fmt",
                     sig_text="fn fmt(&self, f: &mut Formatter) -> fmt::Result",
                     body_text="let x: &ErrorImpl = self.inner.get(); write!(f, \"{}\", x)"),
            FnRecord(file="src/chain.rs", fn_name="next",
                     sig_text="fn next(&mut self) -> Option<u8>", body_text="None"),
        ]
        self._records = (
            [LayoutComponentRecord(frozenset({"ErrorImpl"}), LayoutOutcome.FROZEN)]
            if frozen else [])
        self.files = {"src/error.rs": skeleton_def + "\n"}
        # Backfill sources (parsed records over the scaffold): None = the
        # extractor is unavailable, so keyless AUTHORED rows must skip.
        self.skeleton_fns: list[FnRecord] | None = None
        self.skeleton_types: list[TypeRecord] | None = None
        self.committed: list[str] = []
        self.reverted_calls: list[list[FnLocator]] = []
        self.outcome_updates: list[ComponentResult] = []
        self.logs: list[str] = []
        self.prompts: list[str] = []

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

        def save_attempts(pd, c, a):
            self.saved_attempts = dict(a)
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
            enumerate_fns=lambda sd: self.fn_records,
            revert_fns=revert_fns,
            git_commit=git_commit,
            load_attempts=lambda pd, c: self.attempts,
            save_attempts=save_attempts,
            max_attempts=2,
        )


def test_fires_reauthors_and_reverts_only_referencing_fns():
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
    assert res.fired and res.succeeded
    assert res.reverted_count == 1
    assert f.reverted_calls == [[FnLocator("src/error.rs", "fmt")]]  # NOT chain.rs::next
    assert f.committed  # checkpointed before the attempt
    assert f.saved_attempts == {"ErrorImpl": 1}


def test_success_persists_authored_outcome_with_locate_keys():
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
    assert res.succeeded
    assert len(f.outcome_updates) == 1
    upd = f.outcome_updates[0]
    assert upd.authored and upd.item_names == ("ErrorImpl",)
    # the stored locate-key is the AUTHORED def — the text now in the tree
    assert dict(upd.authored_defs)["ErrorImpl"] == (
        "struct ErrorImpl { inner: Box<dyn StdError> }")


def test_reauthor_failure_does_not_touch_outcome_store():
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
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
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
    assert res.fired and res.succeeded
    assert "struct ErrorImpl { msg: String }" in f.files["src/error.rs"]


def test_authored_component_without_stored_keys_is_skipped_loudly():
    f = _Fakes()
    f._records = [LayoutComponentRecord(
        frozenset({"ErrorImpl"}), LayoutOutcome.AUTHORED)]
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
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
        file="src/error.rs", kind="struct", name="ErrorImpl", is_pub=False,
        definition=authored_def)]
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
    assert res.fired and res.succeeded
    assert "struct ErrorImpl { msg: String }" in f.files["src/error.rs"]
    # first update = the backfilled keys, second = the re-author's fresh keys
    assert dict(f.outcome_updates[0].authored_defs)["ErrorImpl"] == authored_def
    assert any("backfilled" in ln for ln in f.logs)


def test_gated_component_is_never_reauthored():
    f = _Fakes()
    f._records = [LayoutComponentRecord(
        frozenset({"ErrorImpl"}), LayoutOutcome.GATED)]
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
    assert not res.fired
    assert not f.committed


def test_no_component_recorded_does_not_fire():
    f = _Fakes(frozen=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
    assert not res.fired
    assert not f.committed


def test_error_not_naming_private_type_does_not_fire():
    f = _Fakes()
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"),
                           "error[E0308]: mismatched types", f.deps())
    assert not res.fired
    assert not f.committed


def test_cap_reached_does_not_fire():
    f = _Fakes(attempts={"ErrorImpl": 2})
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
    assert not res.fired
    assert not f.committed


def test_reauthor_failure_is_fired_but_not_succeeded():
    f = _Fakes(compiles=False)
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"), _ERR, f.deps())
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
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"),
                           "test result: FAILED. 14 passed; 55 failed",
                           f.deps(), crash_evidence=ev)
    assert res.fired and res.succeeded and res.cause == "RUNTIME_CRASH"
    assert any("RUNTIME-CRASH GUIDANCE" in p for p in f.prompts)
    # Cap still applies to the crash channel.
    f2 = _Fakes(attempts={"ErrorImpl": 2})
    res2 = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"),
                            "", f2.deps(), crash_evidence=ev)
    assert not res2.fired


def test_crash_evidence_confounded_by_component_stubs_defers():
    # Most of the component's own fns are still stubs (e.g. right after a
    # successful re-author's revert) — a stub panic aborts binaries exactly
    # like a bad scheme, so the attempt must NOT be spent yet.
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    f = _Fakes(fn_records=[
        FnRecord(file="src/error.rs", fn_name="fmt",
                 sig_text="fn fmt(&self) -> fmt::Result",
                 body_text="unimplemented!()", has_unimplemented_body=True)])
    f._records = [LayoutComponentRecord(
        frozenset({"ErrorImpl", "fmt"}), LayoutOutcome.FROZEN)]
    ev = CrashEvidence(
        vanished=frozenset({"test_boxed_str"}),
        suspect_fns=frozenset({FnLocator("src/error.rs", "fmt")}))
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"),
                           "", f.deps(), crash_evidence=ev)
    assert not res.fired
    assert not f.committed          # no attempt burned, no checkpoint
    assert any("confounded" in ln for ln in f.logs)


def test_crash_evidence_unrelated_suspects_do_not_fire():
    from pipeline_core.domain.layout_retrigger import CrashEvidence
    f = _Fakes()
    ev = CrashEvidence(vanished=frozenset({"test_x"}),
                       suspect_fns=frozenset({FnLocator("src/chain.rs", "next")}))
    res = retrigger_layout(Path("/x"), "anyhow", "stable", Path("/x/src"),
                           "", f.deps(), crash_evidence=ev)
    assert not res.fired
    assert not f.committed

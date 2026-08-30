"""The fix UNIT — one re-fillable target driven by its own inner cargo-check
loop (P3.C).

`_fix_single_fn` and `_fix_impl_block` were two ~80-line closures that shared a
shape — verify the unit compiles, else render a prompt, ask the model, splice the
returned bodies, re-check — and differed only in (a) the target they own, (b) the
prompt they build, and (c) how they splice the reply. This module makes the
shape ONE thing:

* :class:`FixUnit` — the verify/attempt protocol the loop driver consumes.
* :class:`_BaseFixUnit` — the shared inner-loop wiring (verify via
  :class:`~pipeline_core.domain.compile_probe.CompileProbe`, drive via
  :func:`run_callback_loop`, accumulate the changed unit-ids).
* :class:`FnFixUnit` / :class:`ImplFixUnit` — the two attempt strategies.

The clean/dirty verdict lives on the typed
:class:`~pipeline_core.domain.fix_target.FixTarget`; the loop policy lives in the
shared driver; only the prompt+splice differ between the two units. All seams are
ISP role ports (`ports.fix_engine`) — the unit is constructible with fakes.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from pipeline_core.application.reconstruct.pull_resolver import (
    PullSources,
    ask_with_pulls,
)
from pipeline_core.application.refine_loop import run_callback_loop
from pipeline_core.domain.behaviour_select import IdentityMustUnit
from pipeline_core.domain.compile_probe import CompileProbe
from pipeline_core.domain.fix_report import FixReport, UnitRunOutcome
from pipeline_core.domain.fix_session import FixSession
from pipeline_core.domain.fix_target import FixTarget, FnTarget, ImplTarget
from pipeline_core.domain.llm_output import (
    bind_headerless_names,
    extract_fn_bodies,
)
from pipeline_core.domain.models import FnLocator, Frontier
from pipeline_core.domain.rust_idents import signature_fixed_by_trait
from pipeline_core.ports.fix_engine import (
    CompileChecker,
    FixModel,
    FixPrompts,
    FixSplicer,
)
from pipeline_core.prompts.fix_prompts import (
    build_behavior_fix_prompt,
    build_fn_fix_prompt,
    build_impl_fix_prompt,
    build_tdd_stub_fill_prompt,
)
from pipeline_core.prompts.layout_authoring import identity_must_instruction
from pipeline_core.prompts.pull_menu import pull_menu_section

# The per-fn body-fix sig_rule (E0277 unsized-T relaxation) and the per-impl
# where_rule, verbatim from the originals so the prompts stay byte-identical.
_FN_SIG_RULE_RELAX = (
    "Signature is FIXED — except, if this fn fails because a type "
    "parameter needs a tighter bound (E0277 'size cannot be known'), "
    "you MAY add it by making the FIRST body line a `where` clause "
    "(e.g. `where T: Sized`). NEVER on a trait-impl method — the trait "
    "fixes the signature; an extra bound is rejected (E0276).\n"
)
_FN_SIG_RULE_FIXED = "Signature (do NOT change it):\n"
_IMPL_WHERE_RULE_RELAX = (
    "\nIf — and ONLY if — a PRIVATE (non-`pub`) fn fails because a type "
    "parameter needs a tighter bound (e.g. E0277 'size cannot be known': "
    "the fn returns `T` by value but `T` is `?Sized`), you MAY add a bound "
    "by making the FIRST line of that fn's body a `where` clause:\n"
    '<FN name="read" block="impl">\nwhere T: Sized\n... body ...\n</FN>\n'
    "Do not otherwise change any signature. Never add a bound to a `pub` fn, "
    "and NEVER to a trait-impl method — the trait fixes the signature; an "
    "extra bound is rejected (E0276).\n"
)
_IMPL_WHERE_RULE_FIXED = "Do NOT change any signature.\n"


def _retro_block(retro: str) -> str:
    return (f"\n<RETROSPECTIVE_GUIDANCE>\n{retro}\n</RETROSPECTIVE_GUIDANCE>\n"
            if retro.strip() else "")


def _skeleton_frozen_sig(project_dir: Path, file_rel: str, fn_name: str,
                         locate: Callable[[str, str], tuple[str, int, int, int] | None],
                         ) -> str | None:
    """The baked skeleton's signature for (file_rel, fn_name) — the frozen
    authority. Cluster copies live at `.skeleton/{cluster}/{file_rel}`; the sig
    is returned only when every cluster that has the fn agrees
    (whitespace-collapsed), else None. Reading `.skeleton/` post-strip is Hard
    Rule 11-compliant — it IS the baked artifact, not original source."""
    root = project_dir / ".skeleton"
    if not root.is_dir():
        return None
    first: str | None = None
    norms: set[str] = set()
    for cluster_dir in sorted(root.iterdir()):
        f = cluster_dir / file_rel
        if not cluster_dir.is_dir() or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        loc = locate(text, fn_name)
        if loc is None:
            continue
        sig = loc[0]
        norms.add(" ".join(sig.split()))
        if first is None:
            first = sig
    return first if len(norms) == 1 else None


@dataclass(frozen=True, slots=True)
class FixUnitDeps:
    """The ISP role ports + scalars one fix unit needs (no god-deps)."""

    checker: CompileChecker
    model: FixModel
    splicer: FixSplicer
    prompts: FixPrompts
    log: Callable[[str], None]
    do_retro: Callable[[str, list[dict[str, Any]]], str]
    thinking_directive: str
    err_cap: int
    per_fn_max_stalls: int
    # Pull protocol (None → plain ask, existing constructions unchanged).
    # `full_error` on the sources is a placeholder — each attempt rebinds it
    # to that attempt's untruncated cargo output.
    pull_sources: PullSources | None = None
    emit: Callable[..., None] | None = None
    max_pulls: int = 2


class FixUnit(Protocol):
    """What the loop driver consumes: verify the unit, attempt one repair."""

    target: FixTarget

    def verify(self) -> tuple[bool, str, int]: ...
    def attempt(self, output: str, history: list[dict[str, Any]], *,
                retro: str = ...) -> FixReport: ...
    def run(self) -> UnitRunOutcome: ...


class _BaseFixUnit:
    """Shared inner-loop wiring: probe → is_clean verdict → drive → changed ids."""

    _kind: str = ""

    def __init__(self, session: FixSession, deps: FixUnitDeps,
                 target: FixTarget) -> None:
        self.session = session
        self.deps = deps
        self.target = target
        self._changed: list[str] = []

    # --- verify (shared; the verdict is on the target) ----------------------
    def _probe(self) -> CompileProbe:
        s = self.session
        return self.deps.checker.probe(
            s.check_only_cmd, s.project_dir, dict(s.env), s.timeout)

    def verify(self) -> tuple[bool, str, int]:
        """(clean?, ANSI-stripped render, this-file error count). The render is
        the `output` the next attempt sees; the count is the floor metric."""
        probe = self._probe()
        ok = self.target.is_clean(probe.rc, probe.frontier)
        return ok, probe.rendered, probe.errors_in_file(self.target.file_rel)

    # --- attempt (each subclass builds its prompt + splices its reply) ------
    def attempt(self, output: str, history: list[dict[str, Any]], *,
                retro: str = "") -> FixReport:  # pragma: no cover - overridden
        raise NotImplementedError

    def _err(self, output: str) -> str:
        cap = self.deps.err_cap
        return output[-cap:] if len(output) > cap else output

    def _ask(self, prompt: str, *, full_error: str) -> str:
        """The unit's ask seam (P2): bounded pull round-trips when sources
        are bound and CONTEXT_PULLS is on; plain model ask otherwise.
        `full_error` is THIS attempt's untruncated check output — the
        `full_error` pull kind serves it verbatim."""
        d = self.deps
        if d.pull_sources is None or os.getenv("CONTEXT_PULLS", "1") == "0":
            return d.model.ask(prompt)
        sources = replace(d.pull_sources, full_error=lambda: full_error)
        return ask_with_pulls(
            d.model.ask,
            prompt + pull_menu_section(sources.available_kinds(), d.max_pulls),
            sources=sources,
            has_deliverable=lambda r: bool(extract_fn_bodies(r)),
            log=d.log, emit=d.emit or (lambda *_a, **_k: None),
            max_pulls=d.max_pulls)

    def _fixing_desc(self) -> str:  # pragma: no cover - overridden
        return self.target.label()

    def run(self) -> UnitRunOutcome:
        """Drive the inner cargo-check loop; return the changed unit-ids plus
        the termination verdict (the escalation policy's input)."""
        path = self.session.project_dir / self.target.file_rel
        if not path.exists():
            return UnitRunOutcome()
        self.deps.log(
            f"  [fix loop/{self._kind}] fixing {self._fixing_desc()} "
            "(inner cargo check loop)")
        ok, _out, history = run_callback_loop(
            name=self.target.label(),
            run_check=self.verify,
            do_fix=lambda o, h, *, retro="": self.attempt(o, h, retro=retro).as_record(),
            do_retro=self.deps.do_retro,
            max_stalls=self.deps.per_fn_max_stalls,
        )
        aborted = any(bool(r.get("abort")) for r in history)
        opening = int(history[0].get("metric", 0)) if history else 0
        best_post = min((int(r.get("post_fix_metric", opening)) for r in history),
                        default=opening)
        return UnitRunOutcome(
            changed=tuple(self._changed), clean=ok,
            floor_stalled=not ok and not aborted,
            progressed=bool(history) and best_post < opening)


class FnFixUnit(_BaseFixUnit):
    """Re-fill ONE free (or unambiguous) function by body splice."""

    _kind = "fn"

    def _fixing_desc(self) -> str:
        t = self.target
        assert isinstance(t, FnTarget)
        return f"{t.file_rel}::{t.fn_name}"

    def attempt(self, output: str, history: list[dict[str, Any]], *,
                retro: str = "") -> FixReport:
        t = self.target
        assert isinstance(t, FnTarget)
        path = self.session.project_dir / t.file_rel
        trait_owned_sig = signature_fixed_by_trait(t.container or "")
        # Frozen-signature authority (E0276 ratchet recovery): a where-clause
        # once spliced into a trait-owned method sticks — splice_fn_with_where
        # only inserts, and this prompt would re-freeze the corrupted sig as
        # "verbatim, do not change". Restore the baked-skeleton sig first.
        if trait_owned_sig:
            frozen = _skeleton_frozen_sig(
                self.session.project_dir, t.file_rel, t.fn_name,
                self.deps.splicer.locate_fn)
            if frozen and self.deps.splicer.restore_signature(
                    path, t.fn_name, frozen):
                self.deps.log("  [fix loop/fn] restored frozen signature "
                              f"{t.file_rel}::{t.fn_name}")
        file_text = path.read_text(encoding="utf-8", errors="replace")
        loc = self.deps.splicer.locate_fn(file_text, t.fn_name)
        if loc is None:
            return FixReport.aborted(reason="fn_not_located")
        sig_text, _ss, bs, be = loc
        cur_body = file_text[bs:be]
        # After a body-only attempt stalls, a PRIVATE fn may tighten its bounds
        # (unsized-T: returns `T` by value but `T` is `?Sized` → `where T: Sized`)
        # — never a trait-owned member (impl member or the trait's own default),
        # whose signature the trait fixes for every implementor (E0276).
        allow_where = (len(history) >= 1 and not trait_owned_sig
                       and not self.deps.splicer.is_pub_fn(file_text, t.fn_name))
        sig_rule = _FN_SIG_RULE_RELAX if allow_where else _FN_SIG_RULE_FIXED
        prompt = build_fn_fix_prompt(
            thinking_directive=self.deps.thinking_directive, file_rel=t.file_rel,
            fn_name=t.fn_name, sig_rule=sig_rule, sig_text=sig_text,
            cur_body=cur_body, errs=self._err(output),
            repo_block=self.deps.prompts.repo_spec_block(
                file_rel=t.file_rel, fn_name=t.fn_name, container=t.container or ""),
            retro_block=_retro_block(retro),
            gotchas_block=self.deps.prompts.gotchas_block(output),
            tests_block=self.deps.prompts.tests_block(frozenset({t.fn_name})),
            container=t.container or "")
        resp = self._ask(prompt, full_error=output)
        bodies = extract_fn_bodies(resp)
        blk = next((b for b in bodies if b.get("fn_name") == t.fn_name),
                   bodies[0] if bodies else None)
        body = blk.get("body") if blk else None
        if not body:
            return FixReport.unchanged(summary="no_body",
                                       error_preview=self._err(output))
        wc = blk.get("where_clause") if blk else None
        use_wc = wc if (allow_where and wc) else None
        ok_s, reason, delta = self.deps.splicer.splice_fn(
            path, t.container, t.fn_name, body,
            where_clause=use_wc, allow_nonstub=True)
        if ok_s:
            self._changed.append(f"{t.file_rel}::{t.fn_name}")
            self.deps.log(f"  [fix loop/fn] spliced {t.file_rel}::{t.fn_name}"
                          f"{' +where' if use_wc else ''} ({delta:+d} bytes)")
        else:
            self.deps.log(
                f"  [fix loop/fn] splice rejected {t.file_rel}::{t.fn_name} ({reason})")
        return FixReport.changed(
            summary=f"fn {t.file_rel}::{t.fn_name}",
            files_changed_str=f"{t.file_rel}::{t.fn_name}",
            error_preview=self._err(output))


class ImplFixUnit(_BaseFixUnit):
    """Re-fill a WHOLE impl block in one prompt (the model sees the header,
    every member body, and sibling impls of the same trait); each returned body
    is spliced into the CORRECT container."""

    _kind = "impl"

    def _fixing_desc(self) -> str:
        t = self.target
        assert isinstance(t, ImplTarget)
        return f"{t.file_rel}::{t.container[:50]}"

    def attempt(self, output: str, history: list[dict[str, Any]], *,
                retro: str = "") -> FixReport:
        t = self.target
        assert isinstance(t, ImplTarget)
        path = self.session.project_dir / t.file_rel
        file_text = path.read_text(encoding="utf-8", errors="replace")
        members = t.block.members
        block_src = self.deps.splicer.render_impl_block(file_text, members)
        siblings = t.siblings()
        sib_block = (
            "\nSibling impl blocks of the same trait in this file — keep them "
            "mutually consistent (e.g. matching return types / bounds):\n"
            + "\n".join(f"- {s}" for s in siblings) + "\n"
        ) if siblings else ""
        allow_where = len(history) >= 1
        where_rule = _IMPL_WHERE_RULE_RELAX if allow_where else _IMPL_WHERE_RULE_FIXED
        prompt = build_impl_fix_prompt(
            thinking_directive=self.deps.thinking_directive, file_rel=t.file_rel,
            where_rule=where_rule, block_src=block_src, sib_block=sib_block,
            errs=self._err(output),
            repo_block=self.deps.prompts.repo_spec_block(
                file_rel=t.file_rel, container=t.container),
            retro_block=_retro_block(retro),
            gotchas_block=self.deps.prompts.gotchas_block(output),
            tests_block=self.deps.prompts.tests_block(t.member_names()))
        resp = self._ask(prompt, full_error=output)
        spliced = 0
        # A reply that answers with the CONTAINER header names no fn; the
        # per-fn route above already falls back positionally, so this is the
        # last route where that shape is still dropped.
        for fb in bind_headerless_names(
                extract_fn_bodies(resp), sorted(t.member_names())):
            nm = fb.get("fn_name")
            body = fb.get("body")
            if not nm or not body:
                continue
            # Honour a where-clause only for PRIVATE fns (API-surface guard)
            # whose signature the trait does not own (E0276).
            wc = fb.get("where_clause")
            use_wc = wc if (allow_where and wc
                            and not signature_fixed_by_trait(t.container)
                            and not self.deps.splicer.is_pub_fn(
                                file_text, nm)) else None
            ok_s, reason, delta = self.deps.splicer.splice_fn(
                path, t.container, nm, body,
                where_clause=use_wc, allow_nonstub=True)
            if ok_s:
                spliced += 1
                self._changed.append(f"{t.file_rel}::{nm}")
                self.deps.log(f"  [fix loop/impl] spliced {t.file_rel}::{nm}"
                              f"{' +where' if use_wc else ''} ({delta:+d} bytes)")
            else:
                self.deps.log(
                    f"  [fix loop/impl] splice rejected {t.file_rel}::{nm} ({reason})")
        if spliced == 0:
            return FixReport.unchanged(summary="no_splice",
                                       error_preview=self._err(output))
        return FixReport.changed(
            summary=f"impl {t.file_rel} ({spliced} fn)",
            files_changed_str=f"{t.file_rel}::{t.container[:40]}",
            error_preview=self._err(output))


def make_fix_unit(session: FixSession, deps: FixUnitDeps,
                  target: FixTarget) -> FixUnit:
    """Type-driven dispatch: an :class:`ImplTarget` builds an
    :class:`ImplFixUnit`, anything else a :class:`FnFixUnit`."""
    if isinstance(target, ImplTarget):
        return ImplFixUnit(session, deps, target)
    assert isinstance(target, FnTarget)
    return FnFixUnit(session, deps, target)


def _container_for(fn: str, sig_text: str, recs: Sequence[Any]) -> str:
    """Container of the definition `locate_fn` showed — first sig match, else
    first same-name row. Empty when the extractor has no row (tests)."""
    fallback = ""
    for r in recs:
        if isinstance(r, dict):
            name = str(r.get("fn_name") or "")
            cont = str(r.get("container") or "")
            sig = str(r.get("sig_text") or "")
        else:
            name = str(getattr(r, "fn_name", "") or "")
            cont = str(getattr(r, "container", "") or "")
            sig = str(getattr(r, "sig_text", "") or "")
        if name != fn:
            continue
        if sig == sig_text:
            return cont
        if not fallback:
            fallback = cont
    return fallback


def one_shot_fill(
    targets: Frontier, *, kind: str,
    behavior_srcs: dict[str, tuple[str, str]],
    session: FixSession, model: FixModel, splicer: FixSplicer,
    log: Callable[[str], None], thinking_directive: str,
    err_cap: int, src_snip_cap: int, output: str,
    behaviour_specs: Sequence[Any] = (),
    fn_records: Sequence[Any] = (),
) -> list[str]:
    """Fill fns that COMPILE but fail tests — one body per fn, no inner compile
    loop (the iterative :class:`FixUnit` is wrong here: a stub has no compile
    error so its loop would exit immediately). `kind` is ``"behavior"`` (the
    failing tests' sources guide the body) or ``"tdd-stub"`` (an unimplemented
    stub the tests reach). The whole-file rewrite stays the LAST resort.

    `behaviour_specs` / `fn_records` feed the identity instruction on the
    compiles-but-wrong path. Empty (the defaults) leave that prompt
    byte-identical to a call that never named them.
    """
    changed: list[str] = []
    for (rel, fn) in sorted(targets):
        if not session.is_writable(rel):
            continue
        path = session.project_dir / rel
        if not path.exists():
            continue
        file_text = path.read_text(encoding="utf-8", errors="replace")
        loc = splicer.locate_fn(file_text, fn)
        if loc is None:
            continue
        sig_text, _ss, bs, be = loc
        cur_body = file_text[bs:be]
        errs = output[-err_cap:] if len(output) > err_cap else output
        container = _container_for(fn, sig_text, fn_records)
        if kind == "behavior":
            tests_block = ""
            for tn in sorted(targets[FnLocator(rel, fn)]):
                trel, tsrc = behavior_srcs.get(tn, ("", ""))
                if tsrc and len(tests_block) + len(tsrc) <= src_snip_cap:
                    tests_block += f"// {trel} :: {tn}\n{tsrc}\n\n"
            must = identity_must_instruction(
                behaviour_specs,
                units=(IdentityMustUnit(fn, container, sig_text),))
            prompt = build_behavior_fix_prompt(
                thinking_directive=thinking_directive, file_rel=rel, fn_name=fn,
                sig_text=sig_text, cur_body=cur_body, tests_block=tests_block,
                errs=errs, identity_must=must, container=container)
        else:
            prompt = build_tdd_stub_fill_prompt(
                thinking_directive=thinking_directive, file_rel=rel, fn_name=fn,
                sig_text=sig_text, cur_body=cur_body, errs=errs,
                container=container)
        bodies = extract_fn_bodies(model.ask(prompt))
        blk = next((b for b in bodies if b.get("fn_name") == fn),
                   bodies[0] if bodies else None)
        body = blk.get("body") if blk else None
        if not body:
            continue
        # Write through the record locator, not the bare-name path. A file that
        # holds two fns of a name makes `splice_body` refuse `ambiguous_name`,
        # and on this crate that is the common case: `src/error.rs::from` occurs
        # 4x and was refused here on 2026-08-05 after a full model call, with the
        # log blaming the splice rather than the caller. `sig_text` comes from
        # `locate_fn` above, which returns the FIRST occurrence — the same
        # definition the prompt's `cur_body` was cut from — so the body lands
        # where the model was asked to change it.
        ok_s, reason_s, delta = splicer.splice_body_scoped(
            path, fn, "", sig_text, body, allow_nonstub=True)
        if ok_s:
            changed.append(f"{rel}::{fn}")
            log(f"  [fix loop/{kind}] spliced {rel}::{fn} ({delta:+d} bytes)")
        else:
            log(f"  [fix loop/{kind}] splice refused {rel}::{fn}: {reason_s}")
    return changed

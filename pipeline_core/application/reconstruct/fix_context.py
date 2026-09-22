"""FixContextAssembler — THE single derivation point for every injectable
per-fn fix-prompt section (impl where-clause, TYPES_IN_SCOPE, bound-provided
methods, relevance-filtered model notes). Extracted verbatim from pipeline.py's
`_gather_fn_fix_context` family (god-file shrink): the implicit "one place
derives every section so a site can't silently omit one" rule is now an
explicit use-case. Pure domain/prompts logic plus two injected edges — the
model-notes store (I/O) and the truncation guard (telemetry).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_core.domain.blueprint import (
    Blueprint,
    blueprint_path,
    parse_blueprint,
    slice_identifiers,
)
from pipeline_core.domain.bound_methods import (
    format_bound_methods_section,
    select_bound_provider_traits,
)
from pipeline_core.domain.impl_block_ranges import impl_where_clause
from pipeline_core.domain.models import FnFixContext, TypeRecord
from pipeline_core.domain.prompt_budget import PromptBudgets
from pipeline_core.domain.type_refs import (
    hidden_symbols_from_types,
    types_referenced_in_text,
)
from pipeline_core.prompts.sections import render_types_section


@dataclass(frozen=True, slots=True)
class FixContextAssembler:
    """Assembles the domain `FnFixContext` VO the fill/fix use-cases consume."""

    budgets: PromptBudgets
    filter_notes: Callable[..., str]    # model-notes store (fr I/O)
    guard: Callable[[str, str], None]   # truncation telemetry (`GUARD:` log)
    # Blueprint cache: path → (mtime, parsed). Frozen dataclass, mutable
    # CONTENTS — reparse only when the file changes.
    _bp_cache: dict[Path, tuple[float, Blueprint]] = field(default_factory=dict)

    def _blueprint_for(self, fr_path: Path | None) -> Blueprint:
        if fr_path is None:
            return Blueprint()
        path = blueprint_path(fr_path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return Blueprint()
        hit = self._bp_cache.get(path)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        bp = parse_blueprint(path.read_text(encoding="utf-8", errors="replace"))
        self._bp_cache[path] = (mtime, bp)
        return bp

    def types_section_for_fix(
        self,
        sig_text: str,
        body_text: str,
        error_excerpt: str,
        container: str,
        impl_where: str,
        type_records: list[TypeRecord],
        file_rel: str = "",
        observables: frozenset[str] = frozenset(),
    ) -> str:
        """TYPES_IN_SCOPE for retry/fix prompts (parity with the initial-fill
        path). Selects from
        everything that can name a type at fix time — the signature, the
        enclosing impl header (+ where clause), the CURRENT body (names the
        types the previous attempt actually manipulated), and the error
        text — then formats and caps like the initial-fill path.

        `file_rel` filters to types (and methods) nameable from that file:
        a `pub trait` behind `mod ext` in another file is not callable here,
        however `is_pub` it reads. It is also the viewer the rendering is
        for: a struct's fields private to another file render as NOT
        accessible, each with its accessor, instead of under the
        `access with .name` header that had `src/fmt.rs` reading
        `ErrorImpl.backtrace` directly (E0616, run `3003-93e9a9bf`).

        `observables` are the literal texts the target's attributed failing
        tests assert appear in some rendering — a documented type that STATES
        one of them carries that doc region past the leading-lines cap
        (`_type_doc_lines`). Empty for the retry/fix routes, which have a
        compiler diagnostic rather than an attributed test."""
        search = "\n".join([
            sig_text or "", container or "", impl_where or "",
            body_text or "", error_excerpt or "",
        ])
        # Ranked: the types the SIGNATURE, header and where-clause name come
        # first, then everything the body and the diagnostic mention. The
        # selection used to be in record order and the section is cut at a
        # char cap, so a record appended late — the external std records —
        # fell off the end whenever the body named many crate types: the
        # session prompt built on run `3003-93e9a9bf`'s slice (2026-09-02)
        # had `Backtrace` in a signature and `BacktraceStatus` in the
        # records, and rendered neither.
        # Within each tier, records the prompt cannot show any other way —
        # external (standard-library) records and types declared in ANOTHER
        # file — rank before the file's own declarations, which the model
        # already reads in the file text. Under the char cap that is the
        # difference between `Backtrace`/`BacktraceStatus` reaching a
        # `src/error.rs` prompt and being cut behind `ErrorVTable`.
        # A type declared in this file whose trait impls live in ANOTHER file
        # is not fully shown by the file text either: the impl headers are
        # the part the model cannot see, and they decide which bounds the
        # type satisfies (run `450977-5f48b68a`, 2026-09-21: the fill could
        # not see `impl<C> StdError for ContextError<C, Error>` in another
        # file and bet it was "omitted").
        def _not_shown_elsewhere(rec: TypeRecord) -> int:
            if getattr(rec, "external_path", ""):
                return 0
            if not (file_rel and rec.file and rec.file == file_rel):
                return 0
            impls = getattr(rec, "trait_impls", None) or ()
            return 0 if any(t.file and t.file != file_rel for t in impls) else 1

        head = sorted(types_referenced_in_text(
            "\n".join([sig_text or "", container or "", impl_where or ""]),
            type_records, visible_from=file_rel), key=_not_shown_elsewhere)
        head_names = {r.name for r in head}
        rest = sorted(
            (r for r in types_referenced_in_text(
                search, type_records, visible_from=file_rel)
             if r.name not in head_names),
            key=_not_shown_elsewhere)
        recs = (head + rest)[:self.budgets.types_count]
        # The blanket list is printed once, ahead of the records. A record
        # that does not fit is cut before a list that says it is complete:
        # slicing after that header would make the sentence a lie.
        full = render_types_section(
            recs, observables, viewer_file=file_rel)
        if len(full) > self.budgets.types_section:  # surface starvation
            self.guard("phase-c/types_truncated",
                       f"{(container or sig_text)[:50]}: type context {len(full):,} > "
                       f"cap {self.budgets.types_section:,} ({len(recs)} types)")
        return render_types_section(
            recs, observables, viewer_file=file_rel,
            cap=self.budgets.types_section)

    def bound_methods_section_for(
        self, impl_where: str, type_records: list[TypeRecord],
        file_rel: str = "",
    ) -> str:
        """Name the methods the fn's FROZEN where-bounds make callable, so
        the body uses a bound-provided trait method instead of a mis-bound
        free constructor. "" when none."""
        if not impl_where.strip() or not type_records:
            return ""
        referenced = types_referenced_in_text(
            impl_where, type_records, visible_from=file_rel)
        providers = select_bound_provider_traits(referenced)
        return format_bound_methods_section(impl_where, providers)

    def gather(
        self,
        *,
        file_text: str,
        file_rel: str,
        fn_name: str,
        sig_text: str,
        body_text: str,
        error_excerpt: str,
        container: str,
        type_records: list[TypeRecord],
        fr_path: Path | None = None,
    ) -> FnFixContext:
        """Assemble the fix-prompt context for one fn: impl where-clause from
        the file, TYPES_IN_SCOPE from everything that names a type at fix time,
        and the relevance-filtered model notes (empty when `fr_path` is None)."""
        impl_where = impl_where_clause(file_text, container)
        types_section = self.types_section_for_fix(
            sig_text, body_text, error_excerpt, container, impl_where,
            type_records, file_rel=file_rel)
        bound_methods_section = self.bound_methods_section_for(
            impl_where, type_records, file_rel=file_rel)
        notes_section = ""
        if fr_path is not None:
            notes_section = self.filter_notes(
                fr_path, file_rel=file_rel, fn_name=fn_name,
                file_text=file_text, current_error=error_excerpt,
                hidden_symbols=hidden_symbols_from_types(
                    type_records, file_rel),
            )
        # This fn's blueprint slice — ranked by overlap with the fn name,
        # file stem, container tokens and signature tokens.
        bp = self._blueprint_for(fr_path)
        blueprint_section = "" if bp.empty else bp.slice_for(
            slice_identifiers(fn_name, Path(file_rel).stem, container,
                              sig_text),
            cap=int(os.getenv("BLUEPRINT_SLICE_CAP", "2500")))
        return FnFixContext(
            container=container, impl_where=impl_where,
            types_section=types_section, notes_section=notes_section,
            bound_methods_section=bound_methods_section,
            blueprint_section=blueprint_section,
        )

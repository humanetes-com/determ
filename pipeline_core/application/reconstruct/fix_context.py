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
from pipeline_core.domain.type_refs import types_referenced_in_text
from pipeline_core.prompts.sections import format_type_for_prompt


@dataclass(frozen=True, slots=True)
class FixContextAssembler:
    """Assembles the domain `FnFixContext` VO the fill/fix use-cases consume."""

    budgets: PromptBudgets
    filter_notes: Callable[..., str]    # model-notes store (fr I/O)
    guard: Callable[[str, str], None]   # truncation telemetry (`GUARD:` log)
    # P3 blueprint cache: path → (mtime, parsed). Frozen dataclass, mutable
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
    ) -> str:
        """TYPES_IN_SCOPE for retry/fix prompts (P5.1 parity). Selects from
        everything that can name a type at fix time — the signature, the
        enclosing impl header (+ where clause), the CURRENT body (names the
        types the previous attempt actually manipulated), and the error
        text — then formats and caps like the initial-fill path."""
        search = "\n".join([
            sig_text or "", container or "", impl_where or "",
            body_text or "", error_excerpt or "",
        ])
        recs = types_referenced_in_text(search, type_records)[:self.budgets.types_count]
        full = "\n\n".join(format_type_for_prompt(r) for r in recs)
        if len(full) > self.budgets.types_section:  # Plan 9 F5: surface starvation
            self.guard("phase-c/types_truncated",
                       f"{(container or sig_text)[:50]}: type context {len(full):,} > "
                       f"cap {self.budgets.types_section:,} ({len(recs)} types)")
        return full[:self.budgets.types_section]

    def bound_methods_section_for(
        self, impl_where: str, type_records: list[TypeRecord],
    ) -> str:
        """Corrected R2/R3 (debt #28): name the methods the fn's FROZEN
        where-bounds make callable, so the body uses a bound-provided trait
        method instead of a mis-bound free constructor. "" when none."""
        if not impl_where.strip() or not type_records:
            return ""
        referenced = types_referenced_in_text(impl_where, type_records)
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
            type_records)
        bound_methods_section = self.bound_methods_section_for(
            impl_where, type_records)
        notes_section = ""
        if fr_path is not None:
            notes_section = self.filter_notes(
                fr_path, file_rel=file_rel, fn_name=fn_name,
                file_text=file_text, current_error=error_excerpt,
            )
        # P3: this fn's blueprint slice — ranked by overlap with the fn name,
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

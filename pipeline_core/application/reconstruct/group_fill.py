"""Phase-C group fill (P4) — relocated from pipeline.py.

Fills the skeleton in leaf-first GROUPS (whole-file-per-group LLM calls), the
non-TDD Phase-C path. Collaborators injected via `GroupFillDeps` (composition root
binds the concrete pipeline.py helpers, late-bound for seams); pure text helpers
imported. The two nested closures keep their bodies byte-identical via the
rebind-prelude (task0c pattern).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline_core.domain.models import BuildCluster
from pipeline_core.domain.rust_idents import fn_names_with_body
from pipeline_core.domain.stub_inventory import StubInventory
from pipeline_core.domain.text import smart_truncate, trim_to_line


@dataclass(frozen=True, slots=True)
class GroupFillDeps:
    """Collaborators for the group-fill path, injected by the composition root."""

    log: Callable[[str], None]
    ask: Callable[..., str]
    extract_fn_blocks: Callable[..., dict[str, str]]
    format_skeleton_index_for_cluster: Callable[..., str]
    leaf_first_group_order: Callable[..., list[dict[str, Any]]]
    load_skeleton_files: Callable[..., list[tuple[str, str]]]
    merge_fn_blocks_into_disk: Callable[..., str]
    scan_unimplemented_functions: Callable[..., StubInventory]
    single_call_char_limit: int
    genesis_cap: int
    task2_model: str | None


def task2_fill_groups(
    deps: GroupFillDeps,
    *,
    skeleton_dir: Path,
    target_src_dir: Path,
    groups: list[dict[str, Any]],
    project_dir: Path,
    cluster_name: str,
    module_name: str,
    functional_req: str,
    genesis: str,
    cluster: BuildCluster | None = None,
) -> dict[str, Any]:
    """Phase C: per-group fill calls.

    - Seeds target_src_dir from the skeleton on first call (idempotent).
    - For each group (leaf-first), feeds the LLM the current skeleton state +
      the group's test files + an instruction to replace `unimplemented!()`
      bodies that these tests need.
    - Merges model output with on-disk source, preserving already-filled bodies.

    Returns {'groups_processed': int, 'bytes_filled': int, 'errors': list}.
    """
    # Rebind-prelude (task0c pattern): collaborators localised from deps so the
    # nested closures capture them unchanged — body relocated byte-for-byte.
    _log = deps.log
    ask = deps.ask
    _extract_fn_blocks = deps.extract_fn_blocks
    _format_skeleton_index_for_cluster = deps.format_skeleton_index_for_cluster
    _leaf_first_group_order = deps.leaf_first_group_order
    _load_skeleton_files = deps.load_skeleton_files
    _merge_fn_blocks_into_disk = deps.merge_fn_blocks_into_disk
    _scan_unimplemented_functions = deps.scan_unimplemented_functions
    SINGLE_CALL_CHAR_LIMIT = deps.single_call_char_limit
    _GENESIS_CAP = deps.genesis_cap
    _TASK2_MODEL = deps.task2_model
    target_src_dir.mkdir(parents=True, exist_ok=True)
    # Seed: copy any skeleton file that doesn't yet exist in target_src_dir.
    for src in skeleton_dir.rglob("*.rs"):
        rel = src.relative_to(skeleton_dir)
        dst = target_src_dir / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    ordered = _leaf_first_group_order(groups)
    bytes_before = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    errors: list[str] = []
    skipped_already_filled = 0

    # Cache the smallest successful fills from earlier groups so we can
    # offer them as few-shot examples on retry.
    successful_examples: list[tuple[str, str]] = []  # [(sig, body), ...]

    # Stash directory for failure triples (prompt, response, outcome).
    failures_dir = project_dir / ".skeleton" / "_failures"
    failures_dir.mkdir(parents=True, exist_ok=True)

    def _stub_count_for_group(group: dict[str, Any]) -> int:
        """Decide whether to skip the group's LLM call because all work is done.

        Returns 0 ONLY when we are confident no work remains:
          - the group has target_symbols
          - at least one of them resolves to a function name in target_src_dir
          - and EVERY matched function has a non-stub body
        Otherwise returns 1 (treat as needing work) — including the ambiguous
        case where the target_symbols are type names that don't map to any fn.
        That ambiguity caused F7's over-firing and is the safer default.
        """
        target_names: set[str] = set()
        for sym in group.get("target_symbols", []):
            bare = sym.rsplit("::", 1)[-1]
            target_names.add(bare)
        if not target_names:
            return 1  # no signal → run
        # Build set of ALL fn names in target_src_dir (filled + stubs).
        all_fn_names: set[str] = set()
        stub_fn_names: set[str] = set()
        for p in target_src_dir.rglob("*.rs"):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            all_fn_names |= fn_names_with_body(text)
        stub_fn_names |= _scan_unimplemented_functions(target_src_dir).unit_names()
        # How many target names map to ANY function (filled or unfilled)?
        matched_to_fn = target_names & all_fn_names
        if not matched_to_fn:
            # Targets are type names (or strings the skeleton has no fn for).
            # Can't infer from this signal. Run the group.
            return 1
        # Targets DID resolve to fns. Skip only when none of those fns are
        # currently unimplemented stubs.
        remaining_stub_targets = matched_to_fn & stub_fn_names
        return len(remaining_stub_targets)

    for g_idx, g in enumerate(ordered, 1):
        gname = g.get("name", f"group_{g_idx}")
        gtests = g.get("test_files", [])
        _log(f"  [phase-c] group {g_idx}/{len(ordered)}: {gname} "
             f"({len(gtests)} test file(s))")

        # Skip the group entirely if its target_symbols all already
        # have bodies. Saves an LLM round-trip and prevents the model from
        # being asked to "fill" things that are already done.
        remaining = _stub_count_for_group(g)
        if remaining == 0:
            _log(f"  [phase-c] group {gname}: all target symbols already "
                 f"filled — skipping LLM call.")
            skipped_already_filled += 1
            continue

        # Collect this group's test contents.
        test_blocks: list[str] = []
        for trel in gtests:
            tp = project_dir / trel
            if tp.exists():
                test_blocks.append(f'<TESTFILE path="{trel}">\n{tp.read_text(encoding="utf-8")}\n</TESTFILE>')
        if not test_blocks:
            _log(f"  [phase-c] group {gname}: no readable tests; skipping.")
            continue

        # Signature-only skeleton index, additionally
        # cfg-filtered for the active cluster so the model sees only
        # items active in this cluster (reduces cognitive load).
        cur_files = _load_skeleton_files(target_src_dir)
        skeleton_index = _format_skeleton_index_for_cluster(cur_files, cluster)

        # Genesis and FR capped against the per-prompt budget.
        _genesis_capped = trim_to_line(genesis, _GENESIS_CAP,
                                       "\n... (genesis truncated)") if genesis else ""
        _fr_cap = SINGLE_CALL_CHAR_LIMIT // 4
        _fr_capped = smart_truncate(functional_req or "", total_limit=_fr_cap,
                                    head=_fr_cap // 3, tail=2 * _fr_cap // 3)

        # Few-shot examples included from attempt 2 onward.
        # B023: per-group loop vars bound at def time.
        def _build_prompt(include_examples: bool, *, gname: str = gname, g: Any = g,
                          skeleton_index: str = skeleton_index, _fr_capped: str = _fr_capped,
                          test_blocks: Any = test_blocks,
                          _genesis_capped: str = _genesis_capped) -> str:
            examples_block = ""
            if include_examples and successful_examples:
                ex_parts = ["Example successful fills from earlier groups in this run:"]
                # Sort by body length, take 2 smallest.
                for sig, body in sorted(successful_examples, key=lambda x: len(x[1]))[:2]:
                    ex_parts.append(f'<FN sig="{sig}">\n{body}\n</FN>')
                examples_block = "\n\n" + "\n".join(ex_parts) + "\n"
            return f"""You are filling `unimplemented!()` bodies in a Rust crate so that a specific
group of tests passes.

Crate: `{module_name}` (cluster: {cluster_name})
Group: {gname}
Group rationale: {g.get('rationale', '(none)')}

Implementation skeleton (signature-only index of every file on disk; bodies
that are `unimplemented!()` need filling, bodies that are already implemented
must be preserved verbatim):
{skeleton_index}

Functional requirements (the spec):
{_fr_capped}

Tests this group must pass:
{chr(10).join(test_blocks)}

Methodology (genesis.md):
{_genesis_capped}
{examples_block}
Output format (mandatory — anything else is silently discarded):

  <FN sig="EXACT_SIGNATURE_FROM_SKELETON">
  body code goes here, WITHOUT the surrounding braces
  </FN>

One block per function whose body you fill. The `sig` attribute must be the
function declaration text exactly as it appears in the skeleton up to (but
excluding) the opening `{{`. Do NOT include the braces. Do NOT include other
content (no markdown, no prose, no full-file replacements).

CRITICAL — omit, don't stub:
  - If you cannot produce a real implementation for a function, OMIT it
    entirely from your output. Do not emit a `<FN>` block whose body is
    `unimplemented!()`, `todo!()`, or just panics.
  - Emitting nothing for a function leaves its current `unimplemented!()`
    body intact, which is correct — a later pass will handle it.
  - Re-emitting a stub wastes our LLM budget and yields no progress.

Only emit blocks for functions you ACTUALLY fill with real implementations
that satisfy the tests above.
"""

        # Three-attempt strategy:
        #   1. Default (thinking on, no few-shot).
        #   2. think=False (no few-shot) — recovers groups thinking-mode chokes on.
        #   3. think=False with few-shot examples (if any from prior groups).
        attempt_specs: list[tuple[bool, bool, str]] = [
            (False, False, "default"),  # include_examples=False, think_off=False
            (False, True, "think=False"),
            (True, True, "think=False+few-shot"),
        ]
        fn_blocks: dict[str, str] = {}
        last_response = ""
        for ex_flag, think_off, label in attempt_specs:
            this_prompt = _build_prompt(include_examples=ex_flag)
            kwargs: dict[str, Any] = {"model": _TASK2_MODEL}
            if think_off:
                kwargs["think"] = False
            last_response = ask(this_prompt, **kwargs)
            fn_blocks = _extract_fn_blocks(last_response)
            if fn_blocks:
                if label != "default":
                    _log(f"  [phase-c] group {gname}: recovered via {label}.")
                break
            _log(f"  [phase-c] group {gname}: no FN blocks from {label} attempt.")

        if not fn_blocks:
            _log(f"  [phase-c] group {gname}: all attempts failed.")
            errors.append(f"{gname}: empty/unparseable response")
            # Persist the failure for inspection / future training corpus.
            try:
                ts = int(time.time())
                fpath = failures_dir / f"{gname}-{ts}.txt"
                fpath.write_text(
                    f"# group: {gname}\n# response_chars: {len(last_response)}\n"
                    f"# response_preview: {last_response[:200].replace(chr(10),' ')}\n"
                    f"{'='*60}\n## RESPONSE\n{'='*60}\n{last_response}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            continue

        # Merge: walk each file in target_src_dir, splice in any of our fn_blocks
        # whose signature matches an unimplemented body in that file.
        for src_path in sorted(target_src_dir.rglob("*.rs")):
            rel_str: str = str(src_path.relative_to(target_src_dir))
            if ".gen_tests_" in rel_str:
                continue  # protected
            disk_text = src_path.read_text(encoding="utf-8")
            merged = _merge_fn_blocks_into_disk(disk_text, fn_blocks)
            if merged != disk_text:
                src_path.write_text(merged, encoding="utf-8")
                _log(f"  [phase-c] group {gname}: updated {rel_str} "
                     f"({len(disk_text)} → {len(merged)} bytes)")
                # Cache small successful fills as future few-shot examples.
                for sig, body in fn_blocks.items():
                    if len(body) < 800 and len(successful_examples) < 20:
                        successful_examples.append((sig, body))

    bytes_after = sum(p.stat().st_size for p in target_src_dir.rglob("*.rs"))
    return {
        "groups_processed": len(ordered),
        "groups_skipped_already_filled": skipped_already_filled,
        "bytes_filled": bytes_after - bytes_before,
        "errors": errors,
    }

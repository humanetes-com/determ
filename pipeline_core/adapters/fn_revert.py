"""Revert specific fns to ``unimplemented!()`` in a live (already-filled)
source tree — the F11 M3 layout-retrigger action's surgical revert step.

Same mechanism as `task2_generator.py`'s corruption-recovery revert (every
same-name occurrence, not just the first — `fmt` lives in each Debug/Display
impl), extracted standalone so it needs no class/heal-deps context: this
caller already knows exactly which fns to revert (they reference a just
re-authored component's types), it isn't diagnosing corruption.
"""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.reconstruct.fill_rules import is_unimplemented_body
from pipeline_core.domain.models import FnLocator
from pipeline_core.domain.rust_fn_headers import locate_all_fns


def revert_fns_to_stub(fns: list[FnLocator], src_dir: Path) -> int:
    """Revert every occurrence of each `(file, fn_name)` in `fns` to
    `unimplemented!()`. Returns how many bodies were actually changed
    (already-stub occurrences don't count). Rewrites last-to-first per file
    so earlier byte offsets stay valid across multiple reverts in one file."""
    by_file: dict[str, set[str]] = {}
    for file, fn_name in fns:
        by_file.setdefault(file, set()).add(fn_name)

    reverted = 0
    for rel_file, fn_names in by_file.items():
        fp = Path(src_dir) / rel_file
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        changed = False
        for fn_name in fn_names:
            locs = locate_all_fns(text, fn_name)
            for _sig, sig_start, body_start, body_end in reversed(locs):
                cur_body = text[body_start:body_end]
                if is_unimplemented_body(cur_body):
                    continue
                line_start = text.rfind("\n", 0, sig_start) + 1
                indent = ""
                for ch in text[line_start:sig_start]:
                    if ch in (" ", "\t"):
                        indent += ch
                    else:
                        break
                inner = indent + "    "
                new_body = "\n" + inner + "unimplemented!()\n" + indent
                text = text[:body_start] + new_body + text[body_end:]
                changed = True
                reverted += 1
        if changed:
            fp.write_text(text, encoding="utf-8")
    return reverted

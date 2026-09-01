#!/usr/bin/env python
"""WHY. Run `537093-ffcfd139` (2026-09-01, cluster `stable`, 135/137): the
pair route's one dispatch of `src/error.rs::fmt + src/fmt.rs::debug` sent
prompt `f226f03a7097` carrying three name-matched test files — none holding
its two convicting tests — and no evidence block, because the route
reimplemented one of the per-impl route's seven evidence-assembly calls
inline. The fix is the shared assembly (`application/reconstruct/
block_evidence.py`), and its plan named a computable fixture: rebuild the
pair's evidence channel from the RECORDED inputs under the new code and
compare against the recorded prompt. This script computes that fixture — it
is the "commands, not descriptions" half of the validation, run before the
word "validated" is written anywhere.

USAGE.
    .venv/bin/python scripts/rebuild_pair_prompt_fixture.py
No GPU, no vLLM, no writes. Preconditions: the live workspace tree
`tmp/anyhow` (tests + `.skeleton-filled/stable`) and the prompt store entry
`logs/prompts/f226f03a7097….txt` must still exist; the recorded inputs
(attributed tests, their failure text, the joint revert's newly-red list)
are inlined below VERBATIM from the run's events and prompt store, so the
script keeps working after the live frontier moves on.

HOW TO INTERPRET THE OUTPUT.
- `OLD (recorded prompt f226f03a7097)` — the negative control: the test
  files the blind prompt actually carried and `evidence quotes: 0`. This is
  the pre-fix behaviour, read from the store, not re-simulated.
- `NEW test files` — what the shared assembly hands the same dispatch from
  the same inputs. GOOD iff `tests/fmt_gen_3.rs` (the file holding both
  convicting tests) is present.
- `NEW evidence` — GOOD iff both recorded panic lines appear and the quoted
  source line under each panic (the `assert!` the test pins) is rendered.
- `REGRESSED-CHANNEL files (post-cap)` — the joint revert's ten newly-red
  tests live in five files; the channel admits them under the same
  distinct-file cap as the attributed channel (4), so this list is what a
  second dispatch actually carries. The decision rule: if the list is empty
  or the attributed file is missing, the assembly is mis-wired — do not
  ship; if both GOOD lines print, the mechanism fires on the live records.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline_core.application.reconstruct.block_evidence import (  # noqa: E402
    assemble_block_evidence,
)
from pipeline_core.application.reconstruct.pull_resolver import (  # noqa: E402
    reconstruction_file_reader,
)
from pipeline_core.application.reconstruct.test_locator import (  # noqa: E402
    find_tests_referencing_fn,
)
from pipeline_core.domain.attribution import FailureFrontier  # noqa: E402
from pipeline_core.domain.models import FnLocator, FnRecord, ImplBlock  # noqa: E402

RUN = "537093-ffcfd139"
OLD_PROMPT = ROOT / ("logs/prompts/f226f03a7097d9e86aef9b06ff9c5afa8fd4a4fed2"
                     "036d6edd8b6c9f67541708.txt")
PROJECT = ROOT / "tmp" / "anyhow"
TREE = PROJECT / ".skeleton-filled" / "stable"
TESTS_DIR = PROJECT / "tests"

# The recorded attribution of the two leftovers at the pair's dispatch
# (events `phase_c.attribution.measured_owners` / the hop's log lines), and
# the tests' own recorded words (`FailureFrontier.evidence`, quoted verbatim
# in the run's later `src/error.rs` prompt `bd0622aa3c9f`).
DISP = FnLocator("src/error.rs", "fmt")
IMPL = FnLocator("src/fmt.rs", "debug")
TESTS = ("test_debug_with_captured_backtrace_simple",
         "test_debug_with_captured_backtrace_and_cause")
EVIDENCE = {
    "test_debug_with_captured_backtrace_simple": (
        "thread 'test_debug_with_captured_backtrace_simple' (574016) "
        "panicked at tests/fmt_gen_3.rs:22:5:\n"
        'assertion failed: s.contains("\\n\\n")'),
    "test_debug_with_captured_backtrace_and_cause": (
        "thread 'test_debug_with_captured_backtrace_and_cause' (574015) "
        "panicked at tests/fmt_gen_3.rs:38:5:\n"
        'assertion failed: s.contains("Stack backtrace") || '
        's.contains("stack backtrace")'),
}
# The joint revert's newly-red set, verbatim from the run's
# `phase_c.pair.fill.reverted` event.
NEWLY_RED = (
    "debug_for_anyhow_literal",
    "display_error_debug_uses_display_via_anyhow_literal",
    "message_error_debug_with_string",
    "message_error_display_via_error_msg",
    "test_anyhow_literal_hits_construct_from_display",
    "test_debug",
    "test_debug_format_of_msg_error_is_message",
    "test_option_with_context_none_string_payload",
    "wrapper_test_anyhow_literal_debug_delegates",
    "wrapper_test_anyhow_literal_empty",
)


def _blocks() -> tuple[ImplBlock, ImplBlock]:
    d = FnRecord(file=DISP.file, fn_name=DISP.fn_name,
                 container="impl Debug for Error",
                 sig_text="fn fmt (& self , formatter : & mut fmt :: "
                          "Formatter) -> fmt :: Result")
    i = FnRecord(file=IMPL.file, fn_name=IMPL.fn_name,
                 container="impl ErrorImpl",
                 sig_text="pub (crate) unsafe fn debug (this : Ref < Self > "
                          ", f : & mut fmt :: Formatter) -> fmt :: Result")
    return (
        ImplBlock(file=DISP.file, container=d.container,
                  members=(d,), refill_members=(d,)),
        ImplBlock(file=IMPL.file, container=i.container,
                  members=(i,), refill_members=(i,)),
    )


def main() -> int:
    old = OLD_PROMPT.read_text(encoding="utf-8")
    old_files = [ln.split('"')[1].rsplit("/", 1)[-1]
                 for ln in old.splitlines() if ln.startswith("<TEST path=")]
    print(f"OLD (recorded prompt {OLD_PROMPT.name[:12]}):")
    print(f"  test files: {old_files}")
    print(f"  evidence quotes ('said:'): {old.count('said:')}")

    disp_block, impl_block = _blocks()
    frontier = FailureFrontier.build(
        {DISP: set(TESTS), IMPL: set(TESTS)},
        {},  # the recorded dispatch carried `0 convicting test(s)`
        evidence=EVIDENCE)
    tests_dir_files = sorted(TESTS_DIR.glob("*.rs"))
    read_source = reconstruction_file_reader(TREE / "src", PROJECT)

    def _find(leaf: str, mx: int):
        return find_tests_referencing_fn(tests_dir_files, leaf, max_blocks=mx)

    base = assemble_block_evidence(
        (disp_block, impl_block), [disp_block, impl_block], frontier,
        find_tests=_find, read_source=read_source)
    base_files = [Path(p).name for p, _ in base.test_blocks]
    print("\nNEW test files (same recorded inputs, shared assembly):")
    print(f"  {base_files}")
    ok_file = "fmt_gen_3.rs" in base_files
    both_panics = all(e.splitlines()[0] in base.refill_evidence
                      for e in EVIDENCE.values())
    quoted = "the line that panicked" in base.refill_evidence
    print(f"  fmt_gen_3.rs present: {'GOOD' if ok_file else 'BAD'}")
    print("NEW evidence: "
          + ("GOOD" if (both_panics and quoted) else "BAD")
          + f" (both panic lines={both_panics}, "
            f"panicking source line quoted={quoted})")

    with_prior = assemble_block_evidence(
        (disp_block, impl_block), [disp_block, impl_block], frontier,
        find_tests=_find, read_source=read_source,
        prior_regressed=NEWLY_RED)
    added = [Path(p).name for p, _ in with_prior.test_blocks
             if (p, _) not in set(base.test_blocks)]
    print(f"\nREGRESSED-CHANNEL files (post-cap, of {len(NEWLY_RED)} "
          f"newly-red tests): {added}")
    print("regressed section present: "
          + ("GOOD" if "reddened these previously-passing"
             in with_prior.refill_evidence else "BAD"))
    return 0 if (ok_file and both_panics and quoted and added) else 1


if __name__ == "__main__":
    raise SystemExit(main())

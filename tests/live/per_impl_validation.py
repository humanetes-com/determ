"""Live validation for the per-impl Phase C path on the anyhow workspace.

Builds the per-impl prompt (with DA where-clause preservation + DB crate-
idioms section) for three historically-stuck cases — `Error::context`,
`Mut::read`, `ErrorImpl::error_mut` — sends each to vLLM, and reports
what the model produced.

REQUIREMENTS at run time:
  - vLLM up at the URL configured in .env (or via VLLM_BASE_URL).
  - The anyhow workspace populated at
    `tmp/anyhow/.skeleton-filled/stable/` (any cluster works; stable is
    what current runs use). This is the state after Phase A of the
    stable cluster — the per-cluster source tree the per-impl path
    operates on.

NOT part of `bash check.sh`. Run as:
    .venv/bin/python tests/live/per_impl_validation.py

Pass criterion: at least 2 of 3 cases produce a parseable <FN> body
the splicer would accept. Use the printed `where_clause` and `body`
fields to assess structural correctness.
"""
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
import pipeline as P

_WORKSPACE_DEFAULT = _REPO_ROOT / "tmp" / "anyhow" / ".skeleton-filled" / "stable"
src = Path(os.getenv("PER_IMPL_VAL_WORKSPACE", str(_WORKSPACE_DEFAULT)))
if not src.exists():
    sys.exit(f"workspace not found: {src}\n"
             "Set PER_IMPL_VAL_WORKSPACE or run after Phase A produces "
             "the per-cluster tree.")

def find_block(blocks, file_rel, fn_name):
    for b in blocks:
        if b['file'] != file_rel:
            continue
        for m in b.get('stub_members', []):
            if m['fn_name'] == fn_name:
                return b
        for m in b.get('members', []):
            if m['fn_name'] == fn_name and m.get('has_unimplemented_body'):
                return b
    return None

blocks = P._collect_impl_blocks(src)
print(f"Total impl blocks: {len(blocks)}")

# Need to also re-mark stub_members because the on-disk file may have
# been partially filled. We'll just construct prompts for the three cases.

cases = [
    ('src/error.rs', 'context'),
    ('src/ptr.rs',   'read'),
    ('src/error.rs', 'error_mut'),
]

# Build crate type records once (used by types_section).
crate_types = P._enumerate_types_via_extractor(src) or []

for file_rel, fn_name in cases:
    block = find_block(blocks, file_rel, fn_name)
    if block is None:
        print(f"\n--- {file_rel}::{fn_name} ---  block not found (may be already filled)")
        continue
    print(f"\n=== {file_rel}::{fn_name} ===")
    print(f"   container: {block['container'][:80]}")
    print(f"   stub members: {[m['fn_name'] for m in block['stub_members']]}")

    # Build the same context the live code would.
    sig_concat = " ".join((m.get('sig_text') or '') for m in block['stub_members'])
    idioms = P._extract_crate_idioms(blocks, file_rel,
                                      target_sig_text=sig_concat, max_idioms=5)
    types_records = P._types_referenced_in_text(
        " ".join((m.get('sig_text') or '') for m in block['members']),
        crate_types)
    types_section = "\n\n".join(P._format_type_for_prompt(t) for t in types_records[:12])
    related = []  # keep minimal — no other impls for these cases that aren't
                  # in `related_containers` of the block already

    prompt = P._build_per_impl_prompt(
        block=block,
        related_blocks=related,
        test_blocks=[],
        module_name='anyhow',
        cluster_name='stable',
        types_section=types_section,
        invariants_section='',
        notes_section='',
        fr_path=None,
        retrospective_guidance='',
        crate_idioms_section=idioms,
    )
    print(f"   prompt: {len(prompt)} chars")

    # Show the rendered TARGET_IMPL_BLOCK so we can see the where-clause now.
    import re
    tib = re.search(r'<TARGET_IMPL_BLOCK[^>]*>(.*?)</TARGET_IMPL_BLOCK>', prompt, re.DOTALL)
    if tib:
        print(f"   TARGET preview:")
        for ln in tib.group(1).strip().splitlines()[:10]:
            print(f"     {ln}")

    # Ship to vLLM.
    print("   sending to vLLM...")
    try:
        resp = P.ask(prompt)
    except Exception as e:
        print(f"   ERROR: {e}")
        continue
    parsed = P.extract_fn_bodies(resp)
    print(f"   response: {len(resp)} chars, {len(parsed)} FN block(s)")
    for fn in parsed:
        body = fn['body'][:200]
        where = fn.get('where_clause', '') or '(none)'
        print(f"     FN name={fn['fn_name']}")
        print(f"       where_clause: {where}")
        print(f"       body: {body}")

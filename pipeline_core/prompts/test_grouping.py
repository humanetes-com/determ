"""Phase-B test-grouping prompt assembly, relocated out of pipeline.py.

Pure string assembly: renders the implementation skeleton as `<FILE>` blocks and
builds the LLM prompt that asks the model to partition the cluster's test files
into cohesive reconstruction units. No I/O, no model call.
"""
from __future__ import annotations


def format_skeleton_for_prompt(skeleton_files: list[tuple[str, str]]) -> str:
    """Render the skeleton as a sequence of <FILE> blocks for prompt embedding."""
    parts: list[str] = []
    for rel, content in skeleton_files:
        parts.append(f'<FILE path="{rel}">\n{content}\n</FILE>')
    return "\n\n".join(parts)


def build_grouping_prompt(
    module_name: str,
    cluster_name: str,
    skeleton_block: str,
    tests_block: str,
    test_paths: list[str],
) -> str:
    """The Phase-B grouping prompt (byte-identical to the inline original)."""
    return f"""You are planning a test-driven reconstruction of the `{module_name}` crate (cluster: {cluster_name}).

The implementation skeleton below has all type definitions, signatures, and macros, but
every function body is `unimplemented!()`. Your job is to group the test files into
cohesive units. Each group should exercise a coherent piece of functionality so that one
LLM call per group can fill the necessary `unimplemented!()` bodies and make that group's
tests pass.

Rules:
  - Aim for 6-15 groups. Each group should contain 1-3 test files (smaller
    groups → smaller per-call output → fewer empty responses from the model).
  - Every test file appears in EXACTLY one group. Include compile-fail tests
    (e.g. tests/compiletest.rs, trybuild harness files) — those are real
    tests and must be assigned to a group too.
  - target_symbols must be function/method names that exist in the skeleton.
  - rationale: one sentence describing what the group tests.

Test files you MUST cover (every entry here appears in exactly one group):
{chr(10).join(f"  - {p}" for p in test_paths)}

Implementation skeleton:
{skeleton_block}

Test files:
{tests_block}

Output a JSON object with this exact shape (no prose, no markdown fences):

{{
  "groups": [
    {{
      "name": "short_kebab_case_name",
      "rationale": "one sentence",
      "test_files": ["tests/foo.rs", "tests/bar.rs"],
      "target_symbols": ["func_a", "Method::name", "Type::method"]
    }}
  ]
}}
"""

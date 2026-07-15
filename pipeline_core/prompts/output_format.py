"""Shared output-format directive for every prompt that expects <FILE> output.

Explicitly forbids code fences — LLMs default to wrapping content in
```lang``` blocks, which breaks the extractor. Stating consequences
("will be ignored") improves compliance."""

FILE_FORMAT_INSTRUCTION = """
CRITICAL OUTPUT FORMAT:
- Use ONLY these markers to output files — nothing else:

<FILE path="path/to/file">
file contents go here
</FILE>

- Do NOT wrap contents in code fences (no ```rust, ```python, ```json, or any other ```).
- Do NOT add any text, explanation, or commentary outside the FILE markers.
- Any response that does not use these exact markers will be silently discarded.
"""

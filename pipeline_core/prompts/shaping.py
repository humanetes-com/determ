"""Test-content prompt shaping — pure text helpers for assembling the test
material that TASK 1/2 prompts inject.

Relocated verbatim from pipeline.py. These satisfy the `ports.synthesizer`
`PromptShaping` role (`prompt_budget`/`format_test_files`/`chunk_test_files_by_file`)
and the chunked-synthesis splitter. `prompt_budget` takes the single-call char
limit as an argument (the composition root binds the configured value) so this
module stays free of pipeline globals. The semantic-boundary splitter's regex
matches TEST-CONTENT boundaries, not Rust source structure.
"""

from __future__ import annotations

import re

# Test-function boundary markers across languages — used to split an oversized
# test file at coherent points rather than mid-function.
_BOUNDARY_RE = re.compile(
    r'^(\s*#\[(?:test|cfg\(test\))\]'   # Rust: #[test], #[cfg(test)]
    r'|\s*def test_'                      # Python
    r'|\s*fn test_'                       # Rust (without attribute)
    r'|\s*(?:it|test|describe)\s*\('      # JS/TS
    r'|\s*func Test'                      # Go
    r'|\s*@Test'                          # Java/Kotlin
    r')',
    re.MULTILINE,
)


def prompt_budget(single_call_limit: int, *fixed_parts: str) -> int:
    """Chars remaining for dynamic content after the fixed parts are accounted
    for. Never below 2,000 so there is always room for something useful."""
    used = sum(len(p) for p in fixed_parts)
    return max(2_000, single_call_limit - used)


def format_test_files(test_files: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"=== {rel_path} ===\n{content}" for rel_path, content in test_files)


def split_at_semantic_boundaries(content: str, part_size: int) -> list[str]:
    """Split content into parts of at most ``part_size`` chars, preferring to break
    at semantic boundaries: test-function markers, then blank lines, then a hard
    split. Each part holds coherent, self-contained test logic."""
    if len(content) <= part_size:
        return [content]

    lines = content.splitlines(keepends=True)
    parts: list[str] = []
    current_chars = 0
    current_lines: list[str] = []
    i = 0

    def emit() -> None:
        if current_lines:
            parts.append("".join(current_lines))
            current_lines.clear()

    while i < len(lines):
        line = lines[i]
        if current_lines and current_chars + len(line) > part_size:
            split_at = None
            for j in range(len(current_lines) - 1, 0, -1):
                if _BOUNDARY_RE.match(current_lines[j]):
                    split_at = j
                    break
            if split_at is None:
                for j in range(len(current_lines) - 1, 0, -1):
                    if current_lines[j].strip() == "":
                        split_at = j + 1
                        break
            if split_at is not None and split_at > 0:
                overflow = current_lines[split_at:]
                del current_lines[split_at:]
                emit()
                current_lines.extend(overflow)
                current_chars = sum(len(ln) for ln in current_lines)
            else:
                emit()
                current_chars = 0

        current_lines.append(line)
        current_chars += len(line)
        i += 1

    emit()
    return parts


def chunk_test_files(test_files: list[tuple[str, str]], char_limit: int) -> list[list[tuple[str, str]]]:
    """Group test files so each group fits within ``char_limit``; oversized files
    are split at semantic boundaries into labelled sub-chunks."""
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_size = 0

    def flush() -> None:
        if current:
            chunks.append(list(current))
            current.clear()

    for rel_path, content in test_files:
        header_size = len(rel_path) + 10   # "=== path ===" wrapper
        if len(content) + header_size > char_limit:
            flush()
            part_size = char_limit - header_size
            parts = split_at_semantic_boundaries(content, part_size)
            total_parts = len(parts)
            for idx, part in enumerate(parts, 1):
                chunks.append([(f"{rel_path} (part {idx}/{total_parts})", part)])
        else:
            entry_size = header_size + len(content)
            if current and current_size + entry_size > char_limit:
                flush()
                current_size = 0
            current.append((rel_path, content))
            current_size += entry_size

    flush()
    return chunks


def chunk_test_files_by_file(
    test_files: list[tuple[str, str]], file_char_limit: int,
) -> list[list[tuple[str, str]]]:
    """One chunk per test file; files exceeding ``file_char_limit`` are
    split at semantic boundaries so the model always processes one complete,
    coherent, named file per extraction call."""
    chunks: list[list[tuple[str, str]]] = []
    for rel_path, content in test_files:
        header_size = len(rel_path) + 10
        if len(content) + header_size > file_char_limit:
            part_size = file_char_limit - header_size
            parts = split_at_semantic_boundaries(content, part_size)
            total_parts = len(parts)
            for idx, part in enumerate(parts, 1):
                chunks.append([(f"{rel_path} (part {idx}/{total_parts})", part)])
        else:
            chunks.append([(rel_path, content)])
    return chunks

"""LLM-output extraction — parse <FILE> blocks + genesis content from model responses.

Relocated verbatim from pipeline.py (slice 5b foundation). The regex here operates on
model *response text* (the <FILE path="…"> protocol, markdown artefacts, genesis
delimiters), not Rust source — source parsing stays on the syn extractor. Logging goes
through the :mod:`pipeline_core.log` facade; the genesis-format text helper
`extract_code_content` is imported from :mod:`pipeline_core.domain.text`.
"""

from __future__ import annotations

import re

from .. import log as _log
from .text import extract_code_content

FILE_PATTERN = re.compile(r'<FILE path="([^"]+)">(.*?)</FILE>', re.DOTALL)
# Single-quote variant: <FILE path='foo'>
_FILE_PATTERN_SQ = re.compile(r"<FILE path='([^']+)'>(.*?)</FILE>", re.DOTALL)
# Fallback: models sometimes use <path/to/file> ... or ```lang blocks. We only support the
# canonical FILE format; this pattern detects the wrong format so we can warn usefully.
_BAD_FILE_PATTERN = re.compile(r'<([^>]+\.[a-zA-Z0-9]+)>')

GENESIS_DELIM_START = "===GENESIS_START==="
GENESIS_DELIM_END = "===GENESIS_END==="


def _normalize_file_markers(text: str) -> str:
    """
    Small models frequently prefix <FILE ...> tags with markdown artefacts:
      ### <FILE path="x">   ->  <FILE path="x">
      ### FILE path="x">    ->  <FILE path="x">
      <FILE path='x'>       ->  <FILE path="x">
    Normalise these so the primary regex can match them.
    """
    # Strip leading `###` (or `##`, `#`) before a <FILE ... > tag on the same line.
    text = re.sub(r'#{1,6}\s*(<FILE\s)', r'\1', text)
    # Handle `### FILE path=` (missing opening `<`)
    text = re.sub(r'#{1,6}\s*FILE\s+path=', '<FILE path=', text)
    # Single-quote to double-quote: <FILE path='x'> -> <FILE path="x">
    text = _FILE_PATTERN_SQ.sub(lambda m: f'<FILE path="{m.group(1)}">{m.group(2)}</FILE>', text)
    return text


def _looks_like_prose(content: str, path: str) -> bool:
    """Return True if file content appears to be prose description rather than source code.

    Guards against the model writing 'This snippet outlines...' style descriptions
    instead of actual code inside <FILE> blocks.
    """
    # Only apply to source code extensions
    if not any(path.endswith(ext) for ext in ('.rs', '.py', '.js', '.ts', '.go', '.java', '.c', '.cpp', '.h')):
        return False
    first_line = content.lstrip().split('\n')[0] if content.strip() else ''
    prose_starters = (
        'this ', 'the ', 'here ', 'below ', 'note:', 'note ', 'file ', 'module ',
        'this file', 'this module', 'this snippet', 'this code', 'this implementation',
        'the following', 'below is', 'here is', 'as outlined', 'per the',
    )
    return any(first_line.lower().startswith(p) for p in prose_starters)


def extract_files(text: str) -> dict[str, str]:
    """Parse all <FILE path="...">...</FILE> blocks from model output."""
    from .text import strip_markdown_fences
    text = _normalize_file_markers(text)
    files = {}
    for path, content in FILE_PATTERN.findall(text):
        content = strip_markdown_fences(content)
        if _looks_like_prose(content, path):
            _log.log(f"  WARNING: model wrote prose instead of code for `{path}` — skipping.")
            continue
        files[path] = content
    if not files and _BAD_FILE_PATTERN.search(text):
        _log.log("  WARNING: model used wrong file format (e.g. <path/to/file> instead of "
                 '<FILE path="path/to/file">). Retrying so it uses the correct format.')
    return files


def _extract_genesis(text: str) -> "str | None":
    """
    Extract genesis.md content from Task 4 response.
    1. Primary: content between GENESIS_DELIM_START / GENESIS_DELIM_END.
    2. Fallback: <FILE path="genesis.md">...</FILE> block.
    3. Last resort: content inside any markdown/text code fence (model wraps in ```markdown).
    Returns the stripped content string, or None if not found.
    """
    start = text.find(GENESIS_DELIM_START)
    end = text.find(GENESIS_DELIM_END)
    if start != -1 and end != -1 and end > start:
        return text[start + len(GENESIS_DELIM_START):end].strip()
    # Model output start delimiter but forgot the closing one — use rest of response.
    if start != -1:
        content = text[start + len(GENESIS_DELIM_START):].strip()
        if len(content) > 100:
            return content
    # Fallback: FILE tag format
    files = extract_files(text)
    if "genesis.md" in files:
        return files["genesis.md"]
    # Last resort: strip thinking + any code fence and take the bare content.
    content = extract_code_content(text)
    if content and len(content) > 100:
        return content
    return None

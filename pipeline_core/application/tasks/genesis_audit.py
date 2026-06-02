"""Genesis audit task — review genesis.md for internal contradictions (LLM use case).

First task extracted from pipeline.py (slice 5b). Body is verbatim; the former module-level
collaborators (``ask``/``read``/``write``/``_log``/``with_retry``/``_extract_genesis`` and
the genesis-format constants) are injected via :class:`GenesisAuditDeps` from the
composition root, so this module imports only stdlib — never pipeline.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...log import log
from ...retry import with_retry


@dataclass(frozen=True, slots=True)
class GenesisAuditDeps:
    """Collaborators the genesis audit needs, injected by the composition root.

    Logging + retry are cross-cutting infra (pipeline_core.log / .retry), imported directly
    rather than injected — that is the de-ceremony payoff of the 5b foundation step.
    """

    ask: Callable[..., str]
    read: Callable[[Any], str]
    write: Callable[[Any, str], None]
    extract_genesis: Callable[[str], "str | None"]
    model: str
    verbosity_guard: str
    delim_start: str
    delim_end: str


def genesis_audit(genesis_path: Path, deps: GenesisAuditDeps) -> bool:
    """
    Ask the LLM to review genesis.md for internal contradictions or redundant
    rules and return a cleaned version if issues are found.

    Returns True if genesis.md was updated, False if it was clean or the call
    failed. Called periodically (every GENESIS_AUDIT_INTERVAL iterations) or
    immediately after a regression is detected.
    """
    genesis = deps.read(genesis_path)
    if len(genesis.strip()) < 200:
        log("  Genesis audit: document too short to audit.")
        return False

    prompt = f"""Review this methodology document for internal contradictions, redundant rules,
or rules that directly conflict with each other.

genesis.md:
{genesis}

If no issues found, respond with exactly: AUDIT_CLEAN

If issues are found, output the corrected document with contradictions resolved
and redundant rules removed. Do not add new rules — only clean up existing ones.
{deps.verbosity_guard}
{deps.delim_start}
corrected content here
{deps.delim_end}
"""

    def attempt():
        response = deps.ask(prompt, model=deps.model)
        if 'AUDIT_CLEAN' in response:
            log("  Genesis audit: no issues found.")
            return 'clean'
        content = deps.extract_genesis(response)
        if content:
            deps.write(genesis_path, content)
            log("  Genesis audit: contradictions resolved — genesis.md updated.")
            return 'updated'
        return False

    result = with_retry(attempt)
    return bool(result == 'updated')

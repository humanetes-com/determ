"""TASK 4 prompt budget clamp: the additive `max(5_000, …)`
section floors could sum past the model input limit (seen live on anyhow:
296,417/293,376 → prompt rejected). task4 now renders the REAL prompt and, on
overflow, re-truncates the test-output section by the exact overage and
re-renders — the c05904e "size against the actual fixed prompt" pattern."""
from __future__ import annotations

from pathlib import Path

from pipeline_core.application.tasks.task4 import Task4Deps, task4
from pipeline_core.domain.llm_output import GENESIS_DELIM_END, GENESIS_DELIM_START

_RESP = f"{GENESIS_DELIM_START}\ngenesis\n{GENESIS_DELIM_END}"


def _deps(prompts: list[str], limit: int) -> Task4Deps:
    return Task4Deps(
        ask=lambda prompt, model=None: (prompts.append(prompt), _RESP)[1],
        read=lambda p: "g" * 20_000,           # genesis + functional_req content
        write=lambda p, c: None,
        extract_and_store_rules=lambda *a, **k: None,
        model="m", task4_model="m4", verbosity_guard="",
        intermediate_char_limit=50_000, single_call_char_limit=limit,
    )


def test_task4_prompt_clamped_to_input_limit() -> None:
    prompts: list[str] = []
    limit = 60_000
    out = ("test result: FAILED. 3 passed; 1 failed; 0 ignored\n" + "e" * 400_000)
    ok, cur = task4(Path("g"), Path("r"), out, 2, ".", _deps(prompts, limit),
                    failure_type="test",
                    failure_log="f" * 200_000, source_context="s" * 200_000)
    assert ok is True and cur == 3
    # smart_truncate's "[... N chars omitted ...]" marker shifts a few bytes
    # between the measuring render and the clamped one — allow that slack only.
    assert len(prompts[0]) <= limit + 100


def test_task4_prompt_under_limit_not_reclamped() -> None:
    prompts: list[str] = []
    out = "test result: ok. 3 passed; 0 failed; 0 ignored"
    ok, _ = task4(Path("g"), Path("r"), out, 2, ".", _deps(prompts, 500_000),
                  failure_type="build")
    assert ok is True
    assert out in prompts[0]          # test output kept verbatim — no clamp

"""commit.sh refuses a message that names the editor or model that drafted it.

The pattern is read from the script so this asserts the PLAN's sentence
(those names cannot reach git log), not a second copy of the regex. The
negative control is the pre-extension list: `cursor` / `grok` / `fable`
were not in the grep, so a message carrying only those would have committed.
"""
from __future__ import annotations

import re
from pathlib import Path

_COMMIT_SH = Path(__file__).resolve().parents[1] / "commit.sh"


def _attribution_pattern() -> re.Pattern[str]:
    text = _COMMIT_SH.read_text()
    m = re.search(
        r"grep -n -i -E '([^']+)' \"\$NORMALIZED\"", text)
    assert m is not None, "commit.sh attribution grep not found"
    return re.compile(m.group(1), re.IGNORECASE)


def test_commit_sh_refuses_cursor_grok_fable_and_the_prior_list() -> None:
    pat = _attribution_pattern()
    for s in (
        "cursor", "Grok", "Fable",
        "Claude", "Anthropic",
        "Co-Authored-By:", "Generated with", "🤖",
    ):
        assert pat.search(s), s


def test_commit_sh_does_not_refuse_a_message_that_names_none_of_them() -> None:
    pat = _attribution_pattern()
    assert not pat.search(
        "Build the extractor in the pre-run gate, and match hidden "
        "symbols as calls not words")

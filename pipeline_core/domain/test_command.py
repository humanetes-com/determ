"""Test-command discovery — pure domain (P6 engine-relocation series).

The vocabulary and parsing rules for turning a model's free-text answer into the
single shell command that runs a project's test suite. No I/O, no LLM, no logging
— given a response string this module decides what the command is and how to
normalize it. The orchestration (collecting project files, asking the model,
retrying, logging) lives in the use-case layer.

R7 win: the old ``re.finditer(r'`([^`]+)`', …)`` backtick scan is replaced by a
structured character scan (:func:`backtick_spans`) with byte-equivalent semantics
— a backtick-delimited run of one-or-more non-backtick chars, non-overlapping.
"""

from __future__ import annotations

# Known command prefixes across common project types.
KNOWN_PREFIXES: tuple[str, ...] = (
    "cargo", "pytest", "python", "npm", "npx", "yarn", "pnpm",
    "go ", "mvn", "gradle", "gradlew", "make", "rake", "mix",
    "dotnet", "swift", "stack", "cabal", "mix ", "jest", "mocha",
)

# Bare tool names that require a subcommand to be a valid test invocation.
BARE_TOOL_DEFAULTS: dict[str, str] = {
    "cargo": "cargo test",
    "npm": "npm test",
    "npx": "npx jest",
    "yarn": "yarn test",
    "pnpm": "pnpm test",
}


def backtick_spans(text: str) -> list[str]:
    """Backtick-delimited runs of one-or-more non-backtick chars, left-to-right,
    non-overlapping — the structured equivalent of ``re.finditer('`([^`]+)`', text)``.
    An empty pair ```` `` ```` yields nothing and does not consume the second
    backtick, so the regex's backtracking behaviour is preserved."""
    spans: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "`":
            i += 1
            continue
        j = i + 1
        while j < n and text[j] != "`":
            j += 1
        if j < n and j > i + 1:  # closing backtick found AND non-empty content
            spans.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return spans


def _first_word(cmd: str) -> str:
    parts = cmd.split()
    return parts[0].lower() if parts else ""


def looks_like_command(cmd: str) -> bool:
    """True if `cmd`'s first word is a known tool prefix, a path, or a `.sh` script."""
    first = _first_word(cmd)
    return (
        any(first.startswith(p.strip()) for p in KNOWN_PREFIXES)
        or "/" in first
        or first.endswith(".sh")
    )


def extract_command_from_response(response: str) -> str:
    """Pull the shell command out of a model response that may contain prose.

    1. A backtick-quoted span whose first word is a known prefix (or a path) wins.
    2. Else the first non-fence line, with any prose prefix up to ``': '`` stripped.
    3. Leading ``$`` shell-prompt markers are dropped throughout.
    """
    for span in backtick_spans(response):
        candidate = span.lstrip("$ ./")
        first = _first_word(candidate)
        if any(first.startswith(p.strip()) for p in KNOWN_PREFIXES) or "/" in first:
            return span.lstrip("$ ")
    lines = [ln.strip() for ln in response.splitlines()
             if ln.strip() and not ln.strip().startswith("```")]
    if not lines:
        return ""
    line = lines[0]
    if ": " in line:
        # "The command is: cargo test" -> "cargo test"
        after_colon = line.split(": ", 1)[1].strip().lstrip("`").rstrip("`")
        if after_colon:
            line = after_colon
    return line.lstrip("$ ")


def normalize_bare_tool(cmd: str) -> str | None:
    """The full test invocation for a bare tool name (`cargo` -> `cargo test`),
    or None if `cmd` is not a bare tool."""
    return BARE_TOOL_DEFAULTS.get(cmd.strip().lower())


# Files (besides README.md) whose contents are useful signals for the model.
EXTRA_SIGNAL_FILES: tuple[tuple[str, ...], ...] = (
    ("Cargo.toml",),
    (".cargo", "config.toml"),
    ("Makefile",),
    ("package.json",),
)


def build_discovery_prompt(readme_text: str, extra_sections: list[str]) -> str:
    """Prompt used when project files are present."""
    context_block = ("\n\n" + "\n\n".join(extra_sections)) if extra_sections else ""
    readme_block = (f"README.md:\n{readme_text}\n" if readme_text.strip()
                    else "README.md: (not present or empty)\n")
    return f"""Determine the single primary shell command used to run the full test suite for this project.

Check all provided files — README.md may not mention testing; if so, use Cargo.toml, package.json, Makefile, or project-type conventions to infer the correct command.

Rules:
- Output only the command on one line. No explanation, no markdown fences, no alternatives.
- If multiple test commands exist, pick the one that runs all tests.
- If Cargo.toml is present, default is `cargo test` unless the README or Makefile says otherwise.
- If a rust-toolchain.toml / rust-toolchain file or the README pins a channel (e.g. nightly),
  prefix it: `cargo +nightly test`. Likewise include any global flag the docs say is REQUIRED
  to compile the tests at all (e.g. `--all-features`). Do NOT add per-feature/per-cfg variants —
  those are handled separately per build configuration.
- If package.json is present with a "test" script, use `npm test`.

{readme_block}{context_block}
"""


# Prompt used when no project files are available — infer from conventions.
INFERENCE_PROMPT = """No project files are available. Output the single most common shell command used to run
the full test suite for a typical software project (e.g. `cargo test`, `pytest`, `npm test`).
Output only the command on one line. No explanation, no markdown fences.
"""

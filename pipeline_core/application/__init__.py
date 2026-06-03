"""application — the use-case layer: the pipeline's extracted tasks.

Depends inward on `domain` + `ports` only; never on `adapters` or `pipeline.py` (enforced by
the import-linter contract — see setup.cfg). Tasks receive their collaborators by plain
constructor/argument injection (frozen-dataclass deps records — the Pythonic SOLID idiom, no
framework). During the strangler-fig migration `pipeline.py` keeps thin shims that build the
deps and call these use cases. (No driver/orchestrator object is built yet — the earlier
empty `Orchestrator` scaffold was removed as dead + DIP-violating; see CODE_ARCHITECTURE.md
§8 R2.1. A real driver is R2.2/R3 work, to be designed ports-first.)
"""

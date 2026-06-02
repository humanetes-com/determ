"""application — the use-case layer: the pipeline's tasks + the orchestrator.

Depends inward on `domain` + `ports` only; never on `adapters` or `pipeline.py`. Tasks
receive their collaborators by plain constructor/argument injection (frozen-dataclass deps
records — the Pythonic SOLID idiom, no framework). During the strangler-fig migration
`pipeline.py` keeps thin shims that build the deps and call these use cases.
"""

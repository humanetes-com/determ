"""adapters — concrete port implementations. The ONLY layer that does I/O.

Each adapter wraps a real primitive directly (model_interact, the skeleton-extractor
binary, the cargo subprocess) and never imports the application layer (`pipeline.py`) —
that keeps the dependency rule one-directional and avoids an import cycle.

``build_adapters`` is the composition-root factory: `pipeline.py` (the entrypoint /
composition root) calls it once with the resolved :class:`Settings` and injects the
results downward. During the strangler-fig migration the adapters are constructed and
available; call sites move onto them one task at a time (slice 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..settings import Settings
from .cargo import CargoCli
from .extractor import CliExtractor
from .llm import ModelInteractLLM


@dataclass(frozen=True, slots=True)
class Adapters:
    """The constructed adapter set handed down from the composition root."""

    llm: ModelInteractLLM
    extractor: CliExtractor
    cargo: CargoCli


def build_adapters(settings: Settings, *, extractor_bin: Path, model: str | None = None) -> Adapters:
    """Construct every adapter from settings + resolved paths. Pure wiring, no I/O."""
    return Adapters(
        llm=ModelInteractLLM(default_model=model),
        extractor=CliExtractor(extractor_bin),
        cargo=CargoCli(),
    )


__all__ = ["Adapters", "build_adapters", "CargoCli", "CliExtractor", "ModelInteractLLM"]

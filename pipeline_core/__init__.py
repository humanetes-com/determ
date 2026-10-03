"""pipeline_core — layered package for the determ reconstruction pipeline.

Strangler-fig migration target. `pipeline.py` stays the entrypoint and progressively
delegates here. See docs/ARCHITECTURE.md for the layer rules (enforced by import-linter).

Public API (the stable, light surface — re-exported here so consumers don't reach into
submodules): the domain record types, the `Settings` config object, and the port Protocols.
The adapters (which pull in the heavy `model_interact` LLM client) and the application tasks
are intentionally NOT re-exported here — import them from their submodules at the composition
root, so `import pipeline_core` stays side-effect-free.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .domain.models import (
    BuildCluster,
    Diagnostic,
    DispatchFamily,
    FieldRecord,
    FnRecord,
    Frontier,
    MethodRecord,
    ModRecord,
    ModuleAugmentState,
    TypeRecord,
)
from .ports import LLM, Cargo, Coverage, Extractor, Workspace
from .settings import Settings

__all__ = [
    # domain record types (the Python↔Rust seam + cluster/module state)
    "FnRecord", "TypeRecord", "FieldRecord", "MethodRecord", "ModRecord",
    "DispatchFamily", "Diagnostic", "BuildCluster", "ModuleAugmentState", "Frontier",
    # config
    "Settings",
    # port interfaces (Protocols)
    "LLM", "Extractor", "Cargo", "Coverage", "Workspace",
    "__version__",
]

"""orchestrator — the composition context the use cases hang off.

For now a small immutable record bundling the resolved :class:`Settings` and the
constructed :class:`Adapters`. `pipeline.py` builds one at the composition root
(``ORCHESTRATOR``) and, slice by slice, the task dispatch in ``main()`` migrates onto it.
Kept deliberately thin: it is the seam, not yet the full driver.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters import Adapters
from ..settings import Settings


@dataclass(frozen=True, slots=True)
class Orchestrator:
    settings: Settings
    adapters: Adapters

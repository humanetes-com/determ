"""Typed settings — the target home for the scattered module-level globals + os.getenv.

Built once at the composition root via ``Settings.from_env`` and injected downward, so the
application layer never reads ``os.environ`` directly. Not yet wired into `pipeline.py`;
introduced as the seam that the getenv calls migrate behind, knob by knob.

Note: only the *LLM-serving* ``BACKEND`` maps to :class:`Backend`. The unrelated
generator ``backend`` (static/llm/skeleton) is a different concept and stays separate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .domain.enums import (
    Backend,
    CoverageBackend,
    FillUnit,
    FixStrategy,
    PhaseCDriver,
    PhaseCMode,
)


@dataclass(frozen=True, slots=True)
class Settings:
    backend: Backend
    coverage_backend: CoverageBackend
    phase_c_mode: PhaseCMode
    phase_c_driver: PhaseCDriver
    phase_c_fill_unit: FillUnit
    fix_loop_strategy: FixStrategy
    fix_loop_unit: FillUnit
    backend_max_len_tokens: int | None

    @classmethod
    def from_env(cls) -> "Settings":
        backend = Backend.coerce(os.getenv("BACKEND"), Backend.OLLAMA)
        var = backend.max_len_env_var
        raw = os.getenv(var) if var else None
        max_len = int(raw) if raw and raw.strip().isdigit() else None
        return cls(
            backend=backend,
            coverage_backend=CoverageBackend.coerce(
                os.getenv("COVERAGE_BACKEND"), CoverageBackend.TARPAULIN
            ),
            phase_c_mode=PhaseCMode.coerce(os.getenv("PHASE_C_MODE"), PhaseCMode.PER_GROUP),
            phase_c_driver=PhaseCDriver.coerce(os.getenv("PHASE_C_DRIVER"), PhaseCDriver.FN_WALK),
            phase_c_fill_unit=FillUnit.coerce(os.getenv("PHASE_C_FILL_UNIT"), FillUnit.IMPL),
            fix_loop_strategy=FixStrategy.coerce(os.getenv("FIX_LOOP_STRATEGY"), FixStrategy.PER_FN),
            fix_loop_unit=FillUnit.coerce(os.getenv("FIX_LOOP_UNIT"), FillUnit.IMPL),
            backend_max_len_tokens=max_len,
        )

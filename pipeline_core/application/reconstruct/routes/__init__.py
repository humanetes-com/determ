"""The fill routes as adapters of the `fill_routes.FillRoute` port.

Each route knows only its own runs: what it would open this fill (`plan`)
and how to execute one of them under the run clock (`run`). Everything
else — the fill context's counters, the splice, the gates, the prompt
builders — reaches it as an injected callable from `impl_fill.prepare_impl_fill`
(the per-fn route is the engine's own, `phase_c_engine._PerFnRoute`). The
adapters used to be module-level classes at the bottom of `impl_fill.py`,
typed against that module's private snapshot; they now live behind the
port, and `impl_fill` composes them.
"""
from __future__ import annotations

from pipeline_core.application.reconstruct.routes.file_first import FileRoute
from pipeline_core.application.reconstruct.routes.impl import ImplRoute
from pipeline_core.application.reconstruct.routes.pair import PairRoute
from pipeline_core.application.reconstruct.routes.session import SessionRoute

__all__ = ["FileRoute", "ImplRoute", "PairRoute", "SessionRoute"]

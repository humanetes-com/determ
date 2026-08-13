"""Per-model operating defaults: precedence is env > model default > global.

The table only carries measured values; these tests pin the resolution
order and the fallback so an unknown model behaves byte-identically to the
pre-table code (global DEFAULT), and so an explicit env var can always
force any combination for an experiment.
"""

from pipeline_core.domain.enums import RouteOrder
from pipeline_core.domain.model_defaults import (
    default_route_order,
    resolve_route_order,
)

MUSE = "RedHatAI/Muse-Glimmer-30B-FP8-block"
QWEN = "Qwen/Qwen3.6-27B-FP8"


def test_validated_model_defaults() -> None:
    assert default_route_order(MUSE) is RouteOrder.FILE_PRIMARY
    assert default_route_order(QWEN) is RouteOrder.DEFAULT
    # substring match is case-insensitive and prefix/suffix-tolerant
    assert default_route_order("muse-glimmer-30b") is RouteOrder.FILE_PRIMARY


def test_unknown_model_falls_back_to_global_default() -> None:
    assert default_route_order("meta-llama/Llama-4-70B") is RouteOrder.DEFAULT
    assert default_route_order("") is RouteOrder.DEFAULT


def test_env_always_wins_over_model_default() -> None:
    assert resolve_route_order("default", MUSE) is RouteOrder.DEFAULT
    assert resolve_route_order("file_primary", QWEN) is RouteOrder.FILE_PRIMARY


def test_unset_env_resolves_to_model_default() -> None:
    assert resolve_route_order(None, MUSE) is RouteOrder.FILE_PRIMARY
    assert resolve_route_order(None, QWEN) is RouteOrder.DEFAULT
    assert resolve_route_order("", MUSE) is RouteOrder.FILE_PRIMARY
    assert resolve_route_order("  ", MUSE) is RouteOrder.FILE_PRIMARY


def test_garbled_env_degrades_to_global_default_not_model_default() -> None:
    # An explicit-but-invalid value must not silently become the model
    # default: the operator wrote SOMETHING, and coerce's contract is to
    # fall back to the global DEFAULT for values it cannot read.
    assert resolve_route_order("file_primry", MUSE) is RouteOrder.DEFAULT

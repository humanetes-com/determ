"""The TDD driver's fill call, the engine that answers it, and the gate that
keeps an untyped port from drifting.

Run `486681-a304b30a` (2026-10-10): the driver passed `due_rungs=` to
`PhaseCFillEngine.run`, which did not take it. The port was
`Callable[..., ClusterFillResult]` bound through `lambda *a, **k`, so neither
mypy nor any test saw it; both clusters ended at their first fill, pass 31,
before a model call. These tests read the real driver and engine sources.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

from pipeline_core.application.reconstruct import phase_c_engine, phase_c_tdd
from pipeline_core.application.reconstruct.fill_scheduler import FillExecutionDeps
from pipeline_core.application.reconstruct.phase_c_engine import PhaseCFillEngine
from pipeline_core.ports.fill_engine import FillPassPort

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import check_untyped_port_keywords as gate  # noqa: E402

_PORT = "task2_fill_fns_one_by_one"


def _driver_fill_keywords() -> set[str]:
    tree = ast.parse(inspect.getsource(phase_c_tdd))
    out: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == _PORT):
            out |= {k.arg for k in node.keywords if k.arg is not None}
    return out


def _missing(keywords: set[str], fn: object) -> set[str]:
    params = inspect.signature(fn).parameters  # type: ignore[arg-type]
    return {k for k in keywords if k not in params}


def test_every_keyword_the_driver_passes_is_a_parameter_of_the_engine() -> None:
    keywords = _driver_fill_keywords()
    assert len(keywords) > 40, "the driver's fill call was not found"
    assert _missing(keywords, PhaseCFillEngine.run) == set()
    assert _missing(keywords, FillPassPort.__call__) == set()


def test_control_a_signature_without_the_keyword_is_reported() -> None:
    def run(project_dir: object, due: object = ()) -> None: ...
    assert _missing({"project_dir", "due", "due_rungs"}, run) == {"due_rungs"}


def test_the_engine_hands_every_schedule_input_the_driver_supplies_on() -> None:
    """A keyword the engine takes but drops is the same lost input one layer
    down: every driver keyword that names a `FillExecutionDeps` field is set
    on the deps the engine builds."""
    fields = set(FillExecutionDeps.__dataclass_fields__)
    supplied = _driver_fill_keywords() & fields
    assert {"due", "due_rungs"} <= supplied
    tree = ast.parse(inspect.getsource(phase_c_engine))
    forwarded: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "FillExecutionDeps"):
            forwarded |= {k.arg for k in node.keywords if k.arg is not None}
    assert supplied - forwarded == set()


# ── the untyped-port keyword ratchet ─────────────────────────────────────────

_PORTS = '''
from collections.abc import Callable
from typing import Protocol

class Deps:
    fill: Callable[..., int]
    maybe: Callable[..., int] | None = None

class Typed(Protocol):
    def __call__(self, *, a: int) -> int: ...

class Holder:
    typed: Typed
'''


def _scan(call_site: str) -> dict[str, set[str]]:
    return gate.port_keywords({"ports.py": _PORTS, "driver.py": call_site})


def test_a_keyword_new_at_an_untyped_ports_call_site_fails() -> None:
    current = _scan("def f(deps):\n    deps.fill(a=1, b=2)\n    deps.maybe(c=3)\n")
    failures, _ = gate.compare(current, {"fill": ["a"], "maybe": ["c"]})
    assert len(failures) == 1 and "['b']" in failures[0] and ".fill(" in failures[0]


def test_a_typed_port_and_a_stdlib_module_call_are_not_read() -> None:
    current = _scan("import subprocess\n"
                    "def f(h, deps):\n"
                    "    h.typed(a=1)\n"
                    "    subprocess.run(['x'], errors='replace')\n")
    assert current == {}
    ports = gate.untyped_ports([ast.parse(_PORTS)])
    assert ports == {"fill", "maybe"}


def test_the_baseline_shrinks_when_a_port_is_typed_or_a_keyword_goes() -> None:
    current = _scan("def f(deps):\n    deps.fill(a=1)\n")
    failures, shrunk = gate.compare(current, {"fill": ["a", "b"], "maybe": ["c"]})
    assert failures == [] and shrunk == {"fill": ["a"]}


def test_the_fill_pass_port_is_no_longer_an_untyped_port() -> None:
    sources = gate._read_sources()
    assert _PORT not in gate.port_keywords(sources)

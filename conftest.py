"""Pytest root config: put the repo root on sys.path so `import pipeline` / `pipeline_core`
resolve from any test file (the tree has no installed package yet — see backlog R1.2).

Test ISOLATION of prod observability (debt #21): tests exercise real pipeline paths
(generate_fix_loop, _ask_per_fn_body, …) that emit to `logs/events.jsonl` and write the
content-addressed prompts store. Left unscoped, those land in the SHARED prod artifacts —
and under pytest-xdist every worker is its own process named `MainThread`, so a test run
overlapping a real pipeline run poisons the C10 parallel-streams audit (timestamp-only
run scoping in `audit.load_run`) with phantom same-thread stream collisions.

We point both env vars at a per-process temp dir BEFORE `pipeline` is imported (this
module is imported by pytest ahead of any test module, and each xdist worker imports it
in its own process, so `pipeline._EVENTS_LOG`/`_PROMPTS_DIR` bind to the temp paths at
import). Tests therefore never touch prod artifacts; concurrent test/pipeline runs can't
contaminate each other's audits.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Must be set before `import pipeline` binds _EVENTS_LOG / _PROMPTS_DIR at module load.
_TEST_OBS_DIR = Path(tempfile.mkdtemp(prefix="pipeline-test-obs-"))
os.environ.setdefault("PIPELINE_EVENTS_LOG", str(_TEST_OBS_DIR / "events.jsonl"))
os.environ.setdefault("PIPELINE_PROMPTS_DIR", str(_TEST_OBS_DIR / "prompts"))

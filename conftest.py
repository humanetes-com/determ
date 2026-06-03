"""Pytest root config: put the repo root on sys.path so `import pipeline` / `pipeline_core`
resolve from any test file (the tree has no installed package yet — see backlog R1.2)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

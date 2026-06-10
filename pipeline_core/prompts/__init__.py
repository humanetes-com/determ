"""Prompt construction (slice 8): pure builders + section renderers.

pipeline.py keeps `_`-prefixed alias shims for all names so existing call
sites and test pins are unchanged.
"""

from pipeline_core.prompts.per_fn import build_per_fn_fix_prompt, build_per_fn_prompt
from pipeline_core.prompts.per_impl import build_per_impl_prompt

__all__ = [
    "build_per_fn_fix_prompt",
    "build_per_fn_prompt",
    "build_per_impl_prompt",
]

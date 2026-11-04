"""What a served model is, in one table, and the knobs it implies.

Before this module four tables answered "what is this model", each matching the
model string its own way: the request dialect by exact key, the prompt tier by
prefix, the route-order default by substring, and the server image/parsers by
a shell glob in `docker_vllm.sh`. Today's two models agreed across all four; a
Muse repo spelled differently did not — it matched the tier, route and launcher
tables and fell through the dialect table to `enable_thinking`, which the Muse
template drops without an error, so every think-off call reasoned at `high`.

Now one `ModelProfile` per family carries every model fact, and one matching
rule (`family_key`) applies to every reader. `resolve_model_knobs` is the one
env → profile → global-default resolution of the knobs a model's calibration
owns; the transport, the settings budget math, `RunConfig` and the launcher all
call it, so a commented `.env` line can no longer select a different value in
each reader (the transport's 65536 context against the launcher's 131072).

Pure: callers pass the env mapping in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .enums import ModelTier, RouteOrder


class ReasoningDialect(StrEnum):
    """Which chat-template variable expresses reasoning on/off.

    ENABLE_THINKING — Qwen3's template branches on a boolean, so False is a
    real off. STRENGTH — Muse Glimmer's template has no boolean; it renders a
    level (low / medium / high / xhigh, default high) into the system prompt,
    so "off" is the lowest rung, which still reasons."""

    ENABLE_THINKING = "enable_thinking"
    STRENGTH = "reasoning_strength"


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Every fact the pipeline and the launcher key on a model family.

    `calibration` names the runs or measurements each calibrated value came
    from, so a reader can tell a measured value from an inherited one."""

    family: str
    prefixes: tuple[str, ...]
    dialect: ReasoningDialect
    tier: ModelTier
    route_order: RouteOrder
    # Server facts (docker_vllm.sh).
    image: str
    reasoning_parser: str
    tool_call_parser: str
    max_model_len: int            # context ceiling measured on this box (2x RTX 3090, TP=2)
    # Reasoning calibration.
    reasoning_budget: str         # "per_call" | "fixed"
    thinking_budget: int          # planner reserve, tokens
    # The per_call runaway cap in reasoning chars; None = no cap. Meaningful
    # only when `runaway_calibrated`: an uncalibrated profile carries no cap
    # and says so at startup instead of inventing one.
    runaway_think_chars: int | None
    runaway_calibrated: bool
    strength_on: str | None       # STRENGTH dialect only
    strength_off: str | None
    calibration: str
    # The sampling repetition penalty every call sends. vLLM's generation
    # config supplies temperature, top_p and top_k; this one the transport
    # always overrode with a single value for every model.
    repetition_penalty: float = 1.05
    # ENABLE_THINKING dialect only: the template's reasoning depth, sent with
    # `enable_thinking: true` (`reasoning_effort` in `chat_template_kwargs`).
    # None: not sent, the template's own default applies.
    reasoning_effort: str | None = None


QWEN3 = ModelProfile(
    family="qwen3",
    prefixes=("qwen3",),
    dialect=ReasoningDialect.ENABLE_THINKING,
    tier=ModelTier.MID,
    # File-primary regressed Qwen's hardcrate (55.0 vs 32.2 min; runs
    # 182955-0c302bf2 vs 4182269-1ad665b5, 2026-08-12).
    route_order=RouteOrder.DEFAULT,
    image="vllm/vllm-openai:latest",
    reasoning_parser="qwen3",
    tool_call_parser="",
    # Qwen3.6-27B-FP8 reports a 136,533-token KV pool at TP=2 (2026-08-11).
    max_model_len=131072,
    # Inherited from the Muse measurement of 2026-09-21; Qwen's last run was
    # 2026-08-12, before per_call existed. No Qwen reading backs these two.
    reasoning_budget="per_call",
    thinking_budget=32768,
    runaway_think_chars=None,
    runaway_calibrated=False,
    strength_on=None,
    strength_off=None,
    calibration=("route order 2026-08-12; context 2026-08-11; reasoning shape "
                 "inherited from Muse 2026-09-21, runaway cap uncalibrated; "
                 "sampling and reasoning effort from the Qwen3.8-27B-FP8 card "
                 "2026-11-04, not measured here"),
    # The Qwen3.8 card recommends 1.0 in thinking and non-thinking mode, and
    # names reasoning_effort xhigh (default), medium, low. Sent explicitly so
    # the effort a call ran under is in the request, not a template default.
    # A Qwen3.6 template has no such variable and ignores it.
    repetition_penalty=1.0,
    reasoning_effort="xhigh",
)

MUSE_GLIMMER = ModelProfile(
    family="muse-glimmer",
    prefixes=("muse-glimmer",),
    dialect=ReasoningDialect.STRENGTH,
    tier=ModelTier.MID,
    # Halved hardcrate calls (30 vs 59; runs 143454-1d3cf2ee vs 63595-67b8afb1).
    route_order=RouteOrder.FILE_PRIMARY,
    # vLLM 0.27+ for the architecture and the muse_glimmer parsers.
    image="vllm/vllm-openai:muse-glimmer",
    reasoning_parser="muse_glimmer",
    tool_call_parser="muse_glimmer",
    # 189,405-token KV pool at TP=2 (2026-08-11).
    max_model_len=131072,
    # 2026-09-21: healthy file-route reasoning p50 73,962 / max 119,973 chars
    # under a fixed cap; re-asked uncapped, the cut prompts finished naturally
    # at up to 159,457 (scripts/measure_capped_uncapped.py). No runaway cap.
    reasoning_budget="per_call",
    thinking_budget=32768,
    runaway_think_chars=None,
    runaway_calibrated=True,
    strength_on="high",
    strength_off="low",
    calibration=("route order 2026-08-12; context 2026-08-11; per_call, "
                 "thinking reserve and no runaway cap 2026-09-21"),
    # The model card recommends no sampling values (it serves with
    # `--generation-config auto`); 1.05 is what every Muse run here sent.
    repetition_penalty=1.05,
    reasoning_effort=None,
)

PROFILES: tuple[ModelProfile, ...] = (QWEN3, MUSE_GLIMMER)


def family_key(model: str) -> str:
    """The one matching rule: lowercase, the segment after the last `/`, cut
    at `:`. `RedHatAI/Muse-Glimmer-30B-FP8-block` → `muse-glimmer-30b-fp8-block`,
    `qwen3:32b` → `qwen3`."""
    return model.strip().lower().split("/")[-1].split(":")[0]


def profile_by_family(family: str) -> ModelProfile | None:
    want = family.strip().lower()
    return next((p for p in PROFILES if p.family == want), None)


def profile_for(model: str, family: str | None = None) -> ModelProfile | None:
    """The profile `model` belongs to, or None. `family` (`MODEL_FAMILY`)
    names it outright for a repo spelled some other way."""
    if family and family.strip():
        return profile_by_family(family)
    key = family_key(model)
    if not key:
        return None
    return next((p for p in PROFILES
                 if any(key.startswith(pre) for pre in p.prefixes)), None)


class UnknownModelError(ValueError):
    """A served id no profile matches. Refused rather than defaulted: the
    default dialect is silently wrong for any model that is not Qwen-shaped."""


def known_families() -> str:
    return ", ".join(f"{p.family} (ids starting {'/'.join(p.prefixes)})" for p in PROFILES)


def require_profile(model: str, family: str | None = None) -> ModelProfile:
    prof = profile_for(model, family)
    if prof is not None:
        return prof
    if family and family.strip():
        raise UnknownModelError(
            f"MODEL_FAMILY={family!r} names no profile. Known families: {known_families()}.")
    raise UnknownModelError(
        f"model {model!r} matches no profile (domain/model_profile.py). Known "
        f"families: {known_families()}. Add a profile, or set MODEL_FAMILY=<family> "
        "for a repo of a known family spelled differently.")


# ── knob resolution: env → profile → global default ─────────────────────────

# The global defaults, one per knob. Before this module the transport, the
# settings budget math and RunConfig each carried their own (context 65536 /
# 65536 / none, thinking reserve 16384 / 0 / none, think chars 120000 / — /
# 60000); these are the transport's, the values a call actually ran under.
KnobValue = int | float | str | None

GLOBAL_DEFAULTS: Mapping[str, KnobValue] = {
    "VLLM_MAX_MODEL_LEN": 65536,
    "VLLM_REASONING_BUDGET": "fixed",
    "VLLM_THINKING_BUDGET": 16384,
    "VLLM_RUNAWAY_THINK_CHARS": None,
    "VLLM_REASONING_STRENGTH": "high",
    "VLLM_REASONING_STRENGTH_OFF": "low",
    "VLLM_MAX_THINK_CHARS": 120000,
    "VLLM_REPETITION_PENALTY": 1.05,
    "VLLM_REASONING_EFFORT": None,
}


@dataclass(frozen=True, slots=True)
class ResolvedKnob:
    value: KnobValue
    source: str   # "env" | "profile" | "default" | "uncalibrated"


@dataclass(frozen=True, slots=True)
class ModelKnobs:
    """The resolved model-calibrated knobs for one served id."""

    model: str
    profile: ModelProfile | None
    knobs: tuple[tuple[str, ResolvedKnob], ...]

    def get(self, name: str) -> ResolvedKnob:
        return dict(self.knobs)[name]

    def value(self, name: str) -> KnobValue:
        return self.get(name).value

    def _int(self, name: str) -> int:
        v = self.value(name)
        if not isinstance(v, int):
            raise TypeError(f"{name} resolved to {v!r}, not an integer")
        return v

    @property
    def family(self) -> str | None:
        return self.profile.family if self.profile else None

    @property
    def max_model_len(self) -> int:
        return self._int("VLLM_MAX_MODEL_LEN")

    @property
    def thinking_budget(self) -> int:
        return self._int("VLLM_THINKING_BUDGET")

    @property
    def reasoning_budget(self) -> str:
        return str(self.value("VLLM_REASONING_BUDGET"))

    @property
    def runaway_think_chars(self) -> int | None:
        return (None if self.value("VLLM_RUNAWAY_THINK_CHARS") is None
                else self._int("VLLM_RUNAWAY_THINK_CHARS"))

    @property
    def max_think_chars(self) -> int:
        return self._int("VLLM_MAX_THINK_CHARS")

    @property
    def strength_on(self) -> str:
        return str(self.value("VLLM_REASONING_STRENGTH"))

    @property
    def strength_off(self) -> str:
        return str(self.value("VLLM_REASONING_STRENGTH_OFF"))

    @property
    def repetition_penalty(self) -> float:
        v = self.value("VLLM_REPETITION_PENALTY")
        if not isinstance(v, (int, float)):
            raise TypeError(f"VLLM_REPETITION_PENALTY resolved to {v!r}, not a number")
        return float(v)

    @property
    def reasoning_effort(self) -> str | None:
        """The reasoning depth sent with thinking on, or None (not sent).
        `none` in `.env` is an explicit "do not send" for a profile that
        sets one."""
        v = self.value("VLLM_REASONING_EFFORT")
        if v is None or str(v).strip().lower() in ("", "none"):
            return None
        return str(v).strip()

    def sources(self) -> dict[str, str]:
        return {k: r.source for k, r in self.knobs}

    def summary(self) -> str:
        """The startup line: family, then each knob with where it came from."""
        fam = self.family or "none"
        parts = [f"{k}={r.value}({r.source})" for k, r in self.knobs]
        return f"model profile: {self.model} family={fam} " + " ".join(parts)


def served_model(env: Mapping[str, str]) -> str:
    """The id the server serves: `VLLM_MODEL_NAME`, else `MODEL`."""
    return ((env.get("VLLM_MODEL_NAME") or "").strip()
            or (env.get("MODEL") or "").strip())


def _present(env: Mapping[str, str], name: str) -> str | None:
    """An env value, None when unset or blank — a commented or emptied line
    is absent, never the empty string."""
    raw = env.get(name)
    return raw.strip() if raw is not None and raw.strip() else None


def _int_or(raw: str, name: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer") from exc


def _float_or(raw: str, name: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a number") from exc


def resolve_model_knobs(env: Mapping[str, str], model: str | None = None) -> ModelKnobs:
    """env → profile → global default for every model-calibrated knob.

    `model` defaults to the served id read from `env`. An unknown model
    resolves with no profile (env, then the global default); refusing it is
    the launch's and the transport factory's job, not this function's, so
    `Settings.from_env("")` and test settings still build."""
    served = served_model(env) if model is None else model
    prof = profile_for(served, env.get("MODEL_FAMILY"))
    from_profile: dict[str, KnobValue] = {}
    if prof is not None:
        from_profile = {
            "VLLM_MAX_MODEL_LEN": prof.max_model_len,
            "VLLM_REASONING_BUDGET": prof.reasoning_budget,
            "VLLM_THINKING_BUDGET": prof.thinking_budget,
        }
        if prof.runaway_calibrated:
            from_profile["VLLM_RUNAWAY_THINK_CHARS"] = prof.runaway_think_chars
        if prof.strength_on is not None:
            from_profile["VLLM_REASONING_STRENGTH"] = prof.strength_on
        if prof.strength_off is not None:
            from_profile["VLLM_REASONING_STRENGTH_OFF"] = prof.strength_off
        from_profile["VLLM_REPETITION_PENALTY"] = prof.repetition_penalty
        if prof.reasoning_effort is not None:
            from_profile["VLLM_REASONING_EFFORT"] = prof.reasoning_effort
    ints = {"VLLM_MAX_MODEL_LEN", "VLLM_THINKING_BUDGET",
            "VLLM_RUNAWAY_THINK_CHARS", "VLLM_MAX_THINK_CHARS"}
    floats = {"VLLM_REPETITION_PENALTY"}
    out: list[tuple[str, ResolvedKnob]] = []
    for name, default in GLOBAL_DEFAULTS.items():
        raw = _present(env, name)
        if raw is not None:
            val: KnobValue = (_int_or(raw, name) if name in ints
                              else _float_or(raw, name) if name in floats else raw)
            out.append((name, ResolvedKnob(val, "env")))
        elif name in from_profile:
            out.append((name, ResolvedKnob(from_profile[name], "profile")))
        elif (name == "VLLM_RUNAWAY_THINK_CHARS" and prof is not None
              and not prof.runaway_calibrated):
            out.append((name, ResolvedKnob(None, "uncalibrated")))
        else:
            out.append((name, ResolvedKnob(default, "default")))
    return ModelKnobs(model=served, profile=prof, knobs=tuple(out))


def reasoning_payload(dialect: ReasoningDialect, enable_thinking: bool, *,
                      strength_on: str, strength_off: str,
                      effort: str | None = None) -> dict[str, str | bool]:
    """The `chat_template_kwargs` that asks for (no) reasoning in `dialect`.
    In the boolean dialect a request with thinking on also names its depth
    when `effort` is set; a request with thinking off never does."""
    if dialect is ReasoningDialect.STRENGTH:
        return {dialect.value: strength_on if enable_thinking else strength_off}
    if enable_thinking and effort:
        return {dialect.value: True, "reasoning_effort": effort}
    return {dialect.value: enable_thinking}

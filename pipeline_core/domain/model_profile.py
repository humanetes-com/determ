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


class EffortLevel(StrEnum):
    """The reasoning level one model call is sent at, named for the pipeline,
    not for a chat template.

    Two levels. MAX is what every call ran at until 2026-11-07 (Qwen3.8
    `reasoning_effort=xhigh`; Muse `reasoning_strength=high`). MEDIUM is the
    default: on run `336559-eb0222a5` the behaviour book's prediction calls
    used 2.7x the completion tokens per call of Muse's at the same decode
    rate, so the wall of t0d was reasoning length, which this level sets.
    Each profile maps a level to its template's word (`effort_words`); the
    same name on two templates is not the same amount of reasoning, and the
    A/B that compares them is what says how far apart they are."""

    MEDIUM = "medium"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class LevelCalibration:
    """A level's own reasoning calibration, when one was measured at it. A
    level without one inherits the profile's base values (measured at MAX)."""

    thinking_budget: int
    runaway_think_chars: int | None


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
    #: The word each level is sent as with thinking on: `reasoning_effort`
    #: beside `enable_thinking: true` in the boolean dialect, the
    #: `reasoning_strength` rung in the strength dialect. A level absent here
    #: sends no word (the template's own default applies).
    effort_words: tuple[tuple[EffortLevel, str], ...]
    #: STRENGTH dialect only: the rung a request with thinking OFF is sent at
    #: (the dialect has no off). Thinking off is below every level.
    strength_off: str | None
    calibration: str
    # The sampling repetition penalty every call sends. vLLM's generation
    # config supplies temperature, top_p and top_k; this one the transport
    # always overrode with a single value for every model.
    repetition_penalty: float = 1.05
    #: The word a level is meant to send once it is calibrated on this box,
    #: when that differs from `effort_words` (which holds what is sent now).
    #: Printed on the startup line so a level running below its card word
    #: says so.
    effort_pending: tuple[tuple[EffortLevel, str], ...] = ()
    #: Per-level calibration measured at that level; a level not listed
    #: inherits `thinking_budget` / `runaway_think_chars` above.
    level_calibration: tuple[tuple[EffortLevel, LevelCalibration], ...] = ()

    def word_for(self, level: EffortLevel) -> str | None:
        return dict(self.effort_words).get(level)

    def pending_for(self, level: EffortLevel) -> str | None:
        return dict(self.effort_pending).get(level)


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
    # Reasoning shape inherited from the Muse measurement of 2026-09-21.
    # Thinking reserve measured with the runaway cap below: healthy p90
    # reasoning 174,137 chars over the 20 calls, at 3.53 completion chars per
    # token (39 completions over 2,000 tokens on hardcrate run
    # 2871410-20138abd, same model and effort) = 49,331 tokens, rounded up to
    # 1,024 (`measure_runaway_cap.py report --tag qwen3.8-2026-11-06 --cpt
    # 3.53`). The inherited 32,768 was a third of the healthy max: the input
    # cap it set (244,224 chars, ~70k tokens) admitted prompts leaving ~60k
    # tokens, under the measured file-route max of 267,255 chars (~76k). At
    # 50,176 the single-call cap is 192,000 chars and its largest prompt
    # still leaves ~74k tokens. The sample's largest prompt was 43,644
    # tokens, so the region near the cap is still unmeasured.
    reasoning_budget="per_call",
    thinking_budget=50176,
    # Measured 2026-11-06 on Qwen3.8-27B-FP8 at reasoning effort xhigh,
    # penalty 1.0 (scripts/measure_runaway_cap.py, tag qwen3.8-2026-11-06,
    # through the pipeline's own transport): 20 recorded prompts re-asked
    # uncapped, 5 each of book / file / impl / session, all 20 finished with an
    # answer and none filled the context. Healthy reasoning max per route
    # 75,978 / 267,255 / 201,381 / 141,978 chars; the cap is the overall max
    # (a file-route call, 34 min) + 25%. Qwen3.8 reasons well past Muse's
    # 159,457 tail, so Muse's "no cap" was not transferable either way.
    runaway_think_chars=335_000,
    runaway_calibrated=True,
    # The card names reasoning_effort xhigh (its default), medium and low.
    # MAX is xhigh, what every Qwen3.8 call ran at before 2026-11-07, and
    # the level the runaway cap and the reserve above were measured at.
    # MEDIUM inherits both until a reading at medium sets its own.
    effort_words=((EffortLevel.MEDIUM, "medium"), (EffortLevel.MAX, "xhigh")),
    strength_off=None,
    calibration=("route order 2026-08-12; context 2026-08-11; reasoning shape "
                 "inherited from Muse 2026-09-21; runaway cap 335,000 and thinking "
                 "reserve 50,176 measured on Qwen3.8-27B-FP8 2026-11-06 "
                 "(qwen3.8-2026-11-06); sampling and "
                 "reasoning effort from the Qwen3.8-27B-FP8 card 2026-11-04"),
    # The Qwen3.8 card recommends 1.0 in thinking and non-thinking mode. The
    # effort word is sent explicitly so the level a call ran under is in the
    # request, not a template default. A Qwen3.6 template has no such
    # variable and ignores it; the launch conformance check's level probe
    # (`model_conformance.check_levels`) is what says so.
    repetition_penalty=1.0,
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
    # Levels low / medium / high / xhigh. Every Muse calibration on record
    # was taken at `high` (per_call, no runaway cap, healthy uncapped
    # maximum 159,457 chars, 2026-09-21). `xhigh` is unmeasured on this box,
    # so MAX is sent as `high` until `scripts/measure_runaway_cap.py
    # --effort max` with `VLLM_EFFORT_WORD_MAX=xhigh` has set a cap and a
    # reserve for it; the startup line says so (`effort_pending`).
    effort_words=((EffortLevel.MEDIUM, "medium"), (EffortLevel.MAX, "high")),
    strength_off="low",
    calibration=("route order 2026-08-12; context 2026-08-11; per_call, "
                 "thinking reserve and no runaway cap 2026-09-21 (at high)"),
    # The model card recommends no sampling values (it serves with
    # `--generation-config auto`); 1.05 is what every Muse run here sent.
    repetition_penalty=1.05,
    effort_pending=((EffortLevel.MAX, "xhigh"),),
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
    "VLLM_REASONING_STRENGTH_OFF": "low",
    "VLLM_MAX_THINK_CHARS": 120000,
    "VLLM_REPETITION_PENALTY": 1.05,
    # The template word each level is sent as (`ModelProfile.effort_words`).
    # Set in env only to calibrate a word the profile does not send yet
    # (`scripts/measure_runaway_cap.py`); `none` sends no word.
    "VLLM_EFFORT_WORD_MEDIUM": None,
    "VLLM_EFFORT_WORD_MAX": None,
    # Not a model calibration: the answer's share of a call's room. Resolved
    # here so the transport that plans with it and the health line that
    # reports it read one default.
    "VLLM_CONTENT_BUDGET": 16384,
}

#: Knobs a level replaced. One word cannot name two levels: a leftover line
#: would pin both to it. Each is ignored and named at startup.
RETIRED_KNOBS: Mapping[str, str] = {
    "VLLM_REASONING_EFFORT": "the level's word is VLLM_EFFORT_WORD_<LEVEL>; "
                             "the level is LLM_EFFORT / LLM_EFFORT_PHASES",
    "VLLM_REASONING_STRENGTH": "the level's word is VLLM_EFFORT_WORD_<LEVEL>; "
                               "the level is LLM_EFFORT / LLM_EFFORT_PHASES",
}


def retired_knob_lines(env: Mapping[str, str]) -> list[str]:
    """One line per retired knob that `env` still sets."""
    return [f"{name}={_present(env, name)} is ignored: {why}"
            for name, why in RETIRED_KNOBS.items()
            if _present(env, name) is not None]


def _word_knob(level: EffortLevel) -> str:
    return f"VLLM_EFFORT_WORD_{level.name}"


def reasoning_budget_is_per_call(value: str) -> bool:
    """`VLLM_REASONING_BUDGET` → True for `per_call`, False for `fixed`.

    Anything else raises: a typo silently falling back to `fixed` would put the
    run back under the character cap the operator meant to remove, and the
    only symptom is the cut rate the knob was set to end."""
    v = value.strip().lower()
    if v in ("per_call", "per-call"):
        return True
    if v in ("fixed", ""):
        return False
    raise ValueError(
        f"VLLM_REASONING_BUDGET={value!r}: expected 'fixed' or 'per_call'")


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
    def per_call(self) -> bool:
        return reasoning_budget_is_per_call(self.reasoning_budget)

    @property
    def content_budget(self) -> int:
        return self._int("VLLM_CONTENT_BUDGET")

    def budget_line(self) -> str:
        """The call budget as the vLLM transport plans with it, each value
        with where it came from, for the per-iteration health line.

        Only the knobs the resolved shape consults: per_call has no
        character cap but the runaway cap, fixed has no runaway cap but the
        character cap. Why: the health line used to print the raw env with
        its own `fixed` default, so run `2871410-20138abd` (2026-11-06) read
        `VLLM_REASONING_BUDGET=fixed VLLM_MAX_THINK_CHARS=200000` on every
        iteration while every call ran per_call with no character cap
        (request max 129,912) — the one cap reading on screen described a
        budget no call used."""
        def one(label: str, name: str) -> str:
            r = self.get(name)
            shown = "none" if r.value is None else r.value
            return f"{label}={shown}({r.source})"
        parts = [one("reasoning-budget", "VLLM_REASONING_BUDGET"),
                 one("ctx", "VLLM_MAX_MODEL_LEN"),
                 one("thinking-reserve", "VLLM_THINKING_BUDGET")]
        parts.append(one("runaway-cap", "VLLM_RUNAWAY_THINK_CHARS") if self.per_call
                     else one("think-char-cap", "VLLM_MAX_THINK_CHARS"))
        parts.append(one("content-budget", "VLLM_CONTENT_BUDGET"))
        return " ".join(parts)

    @property
    def runaway_think_chars(self) -> int | None:
        return (None if self.value("VLLM_RUNAWAY_THINK_CHARS") is None
                else self._int("VLLM_RUNAWAY_THINK_CHARS"))

    @property
    def max_think_chars(self) -> int:
        return self._int("VLLM_MAX_THINK_CHARS")

    def effort_word(self, level: EffortLevel) -> str | None:
        """The template word `level` is sent as with thinking on, or None
        (no word: the template's own default). `none` in env is an explicit
        "send no word"."""
        v = self.value(_word_knob(level))
        if v is None or str(v).strip().lower() in ("", "none"):
            return None
        return str(v).strip()

    def _level_cal(self, level: EffortLevel) -> LevelCalibration | None:
        if self.profile is None:
            return None
        return dict(self.profile.level_calibration).get(level)

    def thinking_budget_at(self, level: EffortLevel) -> int:
        """The planner reserve for a call at `level`: an env value applies
        to every level; else the level's own calibration; else the
        profile's base value (measured at MAX)."""
        cal = self._level_cal(level)
        if self.get("VLLM_THINKING_BUDGET").source == "env" or cal is None:
            return self.thinking_budget
        return cal.thinking_budget

    def runaway_think_chars_at(self, level: EffortLevel) -> int | None:
        """The per_call runaway cap for a call at `level`, resolved as
        :meth:`thinking_budget_at`."""
        cal = self._level_cal(level)
        if self.get("VLLM_RUNAWAY_THINK_CHARS").source == "env" or cal is None:
            return self.runaway_think_chars
        return cal.runaway_think_chars

    def effort_line(self) -> str:
        """Each level, the word it is sent as and where that came from,
        and a level running below its card word (`effort_pending`)."""
        parts: list[str] = []
        for level in EffortLevel:
            r = self.get(_word_knob(level))
            word = self.effort_word(level) or "none"
            part = f"{level.value}→{word}({r.source})"
            pend = self.profile.pending_for(level) if self.profile else None
            if pend and r.source != "env":
                part += (f" [{pend} uncalibrated on {self.family}: "
                         f"{level.value} → {word}]")
            parts.append(part)
        return "effort " + " ".join(parts)

    @property
    def strength_off(self) -> str:
        return str(self.value("VLLM_REASONING_STRENGTH_OFF"))

    @property
    def repetition_penalty(self) -> float:
        v = self.value("VLLM_REPETITION_PENALTY")
        if not isinstance(v, (int, float)):
            raise TypeError(f"VLLM_REPETITION_PENALTY resolved to {v!r}, not a number")
        return float(v)

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
        if prof.strength_off is not None:
            from_profile["VLLM_REASONING_STRENGTH_OFF"] = prof.strength_off
        from_profile["VLLM_REPETITION_PENALTY"] = prof.repetition_penalty
        for level, word in prof.effort_words:
            from_profile[_word_knob(level)] = word
    ints = {"VLLM_MAX_MODEL_LEN", "VLLM_THINKING_BUDGET",
            "VLLM_RUNAWAY_THINK_CHARS", "VLLM_MAX_THINK_CHARS",
            "VLLM_CONTENT_BUDGET"}
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
                      effort_word: str | None,
                      strength_off: str) -> dict[str, str | bool]:
    """The `chat_template_kwargs` that asks for (no) reasoning in `dialect`
    at the level whose word is `effort_word`.

    Strength dialect: thinking on is the level's rung (no word: no key, the
    template's default); thinking off is `strength_off`, the lowest rung,
    below every level. Boolean dialect: thinking on names its depth when the
    level has a word; thinking off never does. `effort_word` is required so
    no caller can build a payload without deciding the level."""
    if dialect is ReasoningDialect.STRENGTH:
        if not enable_thinking:
            return {dialect.value: strength_off}
        return {dialect.value: effort_word} if effort_word else {}
    if enable_thinking and effort_word:
        return {dialect.value: True, "reasoning_effort": effort_word}
    return {dialect.value: enable_thinking}

"""One model profile per family, one matching rule, one knob resolver.

Before: the dialect (exact key), tier (prefix), route order (substring) and
launcher (shell glob) tables each matched the model string their own way, and
a Muse id missing from the exact-key table was sent `enable_thinking`, which
the Muse template drops (recorded live below: `enable_thinking: false` renders
the identical prompt as `reasoning_strength: high`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline_core.adapters import llm_transports as t
from pipeline_core.adapters import model_conformance as mc
from pipeline_core.domain.enums import ModelTier, RouteOrder
from pipeline_core.domain.model_caps import model_tier
from pipeline_core.domain.model_defaults import default_route_order
from pipeline_core.domain.model_profile import (
    MUSE_GLIMMER,
    PROFILES,
    QWEN3,
    EffortLevel,
    ReasoningDialect,
    UnknownModelError,
    profile_for,
    require_profile,
    resolve_model_knobs,
    retired_knob_lines,
)
from pipeline_core.domain.phase_cost import RunConfig
from scripts.model_profile_shell import shell_lines

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "model_conformance"
MUSE = "RedHatAI/Muse-Glimmer-30B-FP8-block"
QWEN = "Qwen/Qwen3.6-27B-FP8"

# Every repo in the local HF cache, plus the spellings the old tables keyed.
CACHED_IDS = [
    ("Qwen/Qwen3.6-27B", QWEN3), ("Qwen/Qwen3.6-27B-FP8", QWEN3),
    ("mattbucci/Qwen3.6-27B-AWQ", QWEN3), (MUSE, MUSE_GLIMMER),
    ("meta-models/Muse-Glimmer-30B", MUSE_GLIMMER), ("muse-glimmer-30b", MUSE_GLIMMER),
]

_MODEL_ENV = ("MODEL", "VLLM_MODEL_NAME", "MODEL_FAMILY", "VLLM_MAX_MODEL_LEN",
              "VLLM_REASONING_BUDGET", "VLLM_THINKING_BUDGET", "VLLM_RUNAWAY_THINK_CHARS",
              "VLLM_REASONING_STRENGTH", "VLLM_REASONING_STRENGTH_OFF", "VLLM_MAX_THINK_CHARS",
              "VLLM_REPETITION_PENALTY", "VLLM_REASONING_EFFORT",
              "VLLM_EFFORT_WORD_MEDIUM", "VLLM_EFFORT_WORD_MAX")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for v in _MODEL_ENV:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


@pytest.mark.parametrize(("model", "profile"), CACHED_IDS)
def test_every_cached_repo_resolves_to_its_family(model, profile):
    assert profile_for(model) is profile


@pytest.mark.parametrize(("model", "profile"), CACHED_IDS)
def test_tier_and_route_order_read_the_profile(model, profile):
    # The values computed for today's two models before the move.
    assert model_tier(model) is ModelTier.MID
    assert default_route_order(model) is (
        RouteOrder.FILE_PRIMARY if profile is MUSE_GLIMMER else RouteOrder.DEFAULT)


@pytest.mark.parametrize("profile", PROFILES)
def test_payloads_per_family_and_level(clean_env, profile):
    """Each level renders the profile's own word; thinking off is the off
    form at both levels (below every level)."""
    model = MUSE if profile is MUSE_GLIMMER else QWEN
    med = t.reasoning_template_kwargs(model, True, effort=EffortLevel.MEDIUM)
    top = t.reasoning_template_kwargs(model, True, effort=EffortLevel.MAX)
    offs = {str(t.reasoning_template_kwargs(model, False, effort=lv))
            for lv in EffortLevel}
    if profile.dialect is ReasoningDialect.STRENGTH:
        # Muse's xhigh is uncalibrated on this box: MAX is sent as high.
        assert (med, top) == ({"reasoning_strength": "medium"},
                              {"reasoning_strength": "high"})
        assert offs == {str({"reasoning_strength": "low"})}
    else:
        assert (med, top) == ({"enable_thinking": True, "reasoning_effort": "medium"},
                              {"enable_thinking": True, "reasoning_effort": "xhigh"})
        assert offs == {str({"enable_thinking": False})}


def test_negative_control_one_word_cannot_name_two_levels(clean_env):
    """The retired single knob pinned both levels to its word; it is now
    ignored and named, and the levels keep their own words."""
    env = {"MODEL": "Qwen/Qwen3.8-27B-FP8", "VLLM_REASONING_EFFORT": "xhigh",
           "VLLM_REASONING_STRENGTH": "high"}
    k = resolve_model_knobs(env)
    assert (k.effort_word(EffortLevel.MEDIUM), k.effort_word(EffortLevel.MAX)) == (
        "medium", "xhigh")
    lines = retired_knob_lines(env)
    assert len(lines) == 2 and all("is ignored" in x for x in lines)


def test_each_model_gets_its_own_suggested_sampling(clean_env):
    """Switching MODEL switches the penalty and the level words with it:
    the Qwen3.8 card's 1.0 and medium/xhigh, and Muse's 1.05 with
    medium/high (its xhigh named pending on the startup line)."""
    qwen = resolve_model_knobs({"MODEL": "Qwen/Qwen3.8-27B-FP8"})
    muse = resolve_model_knobs({"MODEL": MUSE})
    assert (qwen.repetition_penalty, qwen.effort_word(EffortLevel.MAX)) == (1.0, "xhigh")
    assert (muse.repetition_penalty, muse.effort_word(EffortLevel.MAX)) == (1.05, "high")
    assert qwen.sources()["VLLM_REPETITION_PENALTY"] == "profile"
    assert qwen.sources()["VLLM_EFFORT_WORD_MAX"] == "profile"
    assert "VLLM_REPETITION_PENALTY=1.0(profile)" in qwen.summary()
    assert "max→xhigh(profile)" in qwen.effort_line()
    assert "xhigh uncalibrated on muse-glimmer: max → high" in muse.effort_line()


def test_an_env_line_overrides_whichever_model_is_loaded(clean_env):
    env = {"MODEL": "Qwen/Qwen3.8-27B-FP8", "VLLM_REPETITION_PENALTY": "1.1",
           "VLLM_EFFORT_WORD_MAX": "medium"}
    k = resolve_model_knobs(env)
    assert (k.repetition_penalty, k.effort_word(EffortLevel.MAX)) == (1.1, "medium")
    assert k.sources()["VLLM_REPETITION_PENALTY"] == "env"
    # `none` is an explicit "send no word".
    off = resolve_model_knobs({**env, "VLLM_EFFORT_WORD_MAX": "none"})
    assert off.effort_word(EffortLevel.MAX) is None
    with pytest.raises(ValueError):
        resolve_model_knobs({**env, "VLLM_REPETITION_PENALTY": "high"})


def test_a_level_without_its_own_calibration_inherits_the_measured_one(clean_env):
    """The reserve and the runaway cap reach the budget per level; medium
    inherits max's measured values until a reading at medium sets its own."""
    k = resolve_model_knobs({"MODEL": "Qwen/Qwen3.8-27B-FP8"})
    for lv in EffortLevel:
        assert k.thinking_budget_at(lv) == QWEN3.thinking_budget
        assert k.runaway_think_chars_at(lv) == QWEN3.runaway_think_chars


def test_the_transport_sends_the_resolved_penalty(clean_env, monkeypatch):
    monkeypatch.setenv("MODEL", QWEN)
    monkeypatch.setenv("VLLM_MODEL_NAME", QWEN)
    assert t.VLLMTransport()._rep_penalty == 1.0
    monkeypatch.setenv("VLLM_MODEL_NAME", MUSE)
    assert t.VLLMTransport()._rep_penalty == 1.05


def test_negative_control_a_muse_id_the_old_exact_table_lacked(clean_env):
    # Not a key of the pre-profile MODEL_CAPS, so the old lookup fell through
    # to the boolean dialect — the payload the Muse fixture renders as `high`.
    model = "SomeOrg/Muse-Glimmer-30B-AWQ"
    assert t.reasoning_template_kwargs(model, False, effort=EffortLevel.MAX) == {
        "reasoning_strength": "low"}


@pytest.mark.parametrize(("model", "dialect"), [(MUSE, "strength"), (QWEN, "boolean")])
def test_reasoning_delta_per_dialect(clean_env, model, dialect):
    clean_env.setenv("VLLM_MODEL_NAME", model)
    tr = t.VLLMTransport()
    d = tr._reasoning_delta("text", False)
    if dialect == "strength":
        assert d.reasoning == "text"       # the low rung's reasoning stays reasoning
    else:
        assert d.content == "text"         # Qwen's think-off misroute is rerouted
    assert tr._reasoning_delta("text", True).reasoning == "text"


@pytest.mark.parametrize("profile", PROFILES)
def test_launcher_shell_lines_equal_the_profile(clean_env, profile):
    model = MUSE if profile is MUSE_GLIMMER else QWEN
    got = dict(line.split("=", 1) for line in shell_lines({"MODEL": model}))
    unq = {k: v.strip("'") for k, v in got.items()}
    assert unq["VLLM_FAMILY"] == profile.family
    assert unq["_def_image"] == profile.image
    assert unq["_def_reasoning"] == profile.reasoning_parser
    assert unq["_def_tool"] == profile.tool_call_parser
    assert int(unq["_max_len_ceiling"]) == profile.max_model_len
    assert (unq["VLLM_MAX_LEN"], unq["VLLM_MAX_LEN_SOURCE"]) == (str(profile.max_model_len), "profile")


def test_unknown_id_is_refused_and_model_family_admits_it(clean_env):
    with pytest.raises(UnknownModelError, match="qwen3.*muse-glimmer"):
        require_profile("meta-llama/Llama-4-70B")
    assert require_profile("my-org/glimmer-finetune", "muse-glimmer") is MUSE_GLIMMER
    with pytest.raises(UnknownModelError, match="MODEL_FAMILY"):
        require_profile(MUSE, "nope")
    # The live transport build refuses; the launcher printer exits 2.
    clean_env.setenv("BACKEND", "vllm")
    clean_env.setenv("VLLM_MODEL_NAME", "meta-llama/Llama-4-70B")
    with pytest.raises(UnknownModelError):
        t._backend_factory()
    clean_env.setenv("MODEL_FAMILY", "qwen3")
    assert type(t._backend_factory()).__name__ == "VLLMTransport"
    rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "model_profile_shell.py"),
                         "--model", "meta-llama/Llama-4-70B"],
                        capture_output=True, text=True, env={"PATH": "/usr/bin"}).returncode
    assert rc == 2


def test_settings_still_build_with_no_model(clean_env):
    from pipeline_core.settings import Settings
    Settings.from_env("")


# ── the resolver ─────────────────────────────────────────────────────────────


def test_resolver_precedence_env_profile_default():
    k = resolve_model_knobs({"MODEL": MUSE})
    assert (k.max_model_len, k.reasoning_budget, k.thinking_budget) == (131072, "per_call", 32768)
    assert k.get("VLLM_MAX_MODEL_LEN").source == "profile"
    k = resolve_model_knobs({"MODEL": MUSE, "VLLM_MAX_MODEL_LEN": "65536"})
    assert (k.max_model_len, k.get("VLLM_MAX_MODEL_LEN").source) == (65536, "env")
    # Blank is absent, never the empty string (a commented or emptied line).
    k = resolve_model_knobs({"MODEL": MUSE, "VLLM_REASONING_BUDGET": "  "})
    assert (k.reasoning_budget, k.get("VLLM_REASONING_BUDGET").source) == ("per_call", "profile")
    # No profile: env, then the global default — the transport's own.
    k = resolve_model_knobs({"MODEL": "unknown-model"})
    assert (k.max_model_len, k.reasoning_budget, k.thinking_budget, k.max_think_chars) == (
        65536, "fixed", 16384, 120000)
    assert k.family is None


def _uncalibrated_qwen(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry with Qwen's runaway cap unmeasured: the state every
    profile starts in, and the one the run-start warning exists for."""
    import dataclasses

    import pipeline_core.domain.model_profile as mp
    monkeypatch.setattr(mp, "PROFILES", tuple(
        dataclasses.replace(p, runaway_think_chars=None, runaway_calibrated=False)
        if p is QWEN3 else p for p in mp.PROFILES))


def test_qwen_runaway_cap_is_the_measured_value():
    # Measured 2026-11-06 on Qwen3.8-27B-FP8: healthy max 267,255 chars + 25%.
    k = resolve_model_knobs({"MODEL": QWEN})
    assert k.runaway_think_chars == 335_000
    assert k.get("VLLM_RUNAWAY_THINK_CHARS").source == "profile"
    muse = resolve_model_knobs({"MODEL": MUSE})
    assert (muse.runaway_think_chars, muse.get("VLLM_RUNAWAY_THINK_CHARS").source) == (
        None, "profile")


def test_an_uncalibrated_runaway_cap_is_none_not_invented(monkeypatch):
    _uncalibrated_qwen(monkeypatch)
    k = resolve_model_knobs({"MODEL": QWEN})
    assert k.runaway_think_chars is None
    assert k.get("VLLM_RUNAWAY_THINK_CHARS").source == "uncalibrated"
    assert resolve_model_knobs({"MODEL": MUSE}).get("VLLM_RUNAWAY_THINK_CHARS").source == "profile"


def test_transport_settings_and_launcher_agree_on_the_context(clean_env):
    from pipeline_core.domain.enums import Backend
    from pipeline_core.settings import _resolve_ctx_thinking
    clean_env.setenv("MODEL", QWEN)
    ctx, thinking = _resolve_ctx_thinking(Backend.VLLM)
    tr = t.VLLMTransport()
    launcher = dict(line.split("=", 1) for line in shell_lines({"MODEL": QWEN}))
    assert ctx == tr._ctx == int(launcher["VLLM_MAX_LEN"]) == QWEN3.max_model_len
    assert thinking == tr._thinking_budget == QWEN3.thinking_budget
    assert tr._content_budget == resolve_model_knobs({"MODEL": QWEN}).content_budget


# ── the thinking reserve the input caps are sized against ────────────────────

# The 20 healthy re-asks of `measure_runaway_cap.py ask --tag
# qwen3.8-2026-11-06` (logs/reasoning_econ/runaway/calls.jsonl, Qwen3.8-27B-FP8,
# effort xhigh): (reasoning_chars, content_chars), all finish=stop.
QWEN38_HEALTHY = (
    (6447, 728), (7794, 477), (3899, 611), (75978, 27291), (19096, 2247),
    (11401, 855), (28526, 1754), (267255, 6084), (34282, 266), (114383, 2296),
    (174137, 3090), (134177, 96), (69155, 393), (8229, 75), (201381, 52),
    (110828, 1832), (63736, 1332), (76951, 1787), (141978, 43), (128261, 3730),
)
# Completion chars per token over hardcrate run 2871410-20138abd's 39
# completions above 2,000 tokens (same model, same effort, 2026-11-06).
QWEN38_CPT = 3.53


def _qwen38_rows() -> list[dict]:
    return [{"route": "x", "finish": "stop", "reasoning_chars": r, "content_chars": c}
            for r, c in QWEN38_HEALTHY]


def test_qwen_reserve_is_the_calibration_p90():
    from scripts.measure_runaway_cap import reserve_proposal
    reserve, _why = reserve_proposal(_qwen38_rows(), QWEN38_CPT)
    assert reserve == QWEN3.thinking_budget == resolve_model_knobs({"MODEL": QWEN}).thinking_budget
    # Rows with no completion tokens and no --cpt propose nothing.
    assert reserve_proposal(_qwen38_rows(), None)[0] is None


@pytest.mark.parametrize("reserve,fits", [(QWEN3.thinking_budget, True), (32768, False)])
def test_the_largest_admitted_prompt_leaves_the_healthy_p90(reserve, fits):
    """At the planner's own chars-per-token floor, a prompt at the single-call
    cap still leaves room for the calibration's healthy p90 reasoning. The
    inherited 32,768 (the negative control) did not."""
    from pipeline_core.domain.model_caps import safe_input_char_limit
    from scripts.measure_runaway_cap import reasoning_tokens
    toks = sorted(reasoning_tokens(r, QWEN38_CPT) or 0.0 for r in _qwen38_rows())
    p90 = toks[17]   # ceil(0.9 * 20) - 1, the script's percentile
    ctx = QWEN3.max_model_len
    admitted = safe_input_char_limit(ctx, reserve, 16384) / 3.0
    assert (ctx - admitted - 512 >= p90) is fits


# ── the budget line on every iteration's health reading ──────────────────────


def test_budget_line_renders_the_resolved_shape_not_the_raw_env():
    """Run 2871410-20138abd printed `VLLM_REASONING_BUDGET=fixed
    VLLM_MAX_THINK_CHARS=200000` from the raw env while every call ran
    per_call with no character cap."""
    env = {"MODEL": "Qwen/Qwen3.8-27B-FP8", "VLLM_MAX_THINK_CHARS": "200000"}
    line = resolve_model_knobs(env).budget_line()
    assert "reasoning-budget=per_call(profile)" in line
    assert "runaway-cap=335000(profile)" in line
    assert f"thinking-reserve={QWEN3.thinking_budget}(profile)" in line
    assert "content-budget=16384(default)" in line
    assert "think-char-cap" not in line   # per_call never consults it
    # The fixed shape consults the character cap and not the runaway cap.
    fixed = resolve_model_knobs({**env, "VLLM_REASONING_BUDGET": "fixed"}).budget_line()
    assert "reasoning-budget=fixed(env)" in fixed
    assert "think-char-cap=200000(env)" in fixed and "runaway-cap" not in fixed
    # Negative control: the old rendering's default disagrees with the resolver.
    assert env.get("VLLM_REASONING_BUDGET", "fixed") != resolve_model_knobs(env).reasoning_budget


def test_no_pipeline_module_reads_a_resolved_knob_from_the_raw_env():
    """Every reader of a resolver knob goes through `resolve_model_knobs`; a
    raw read carries its own default, which is how the health line came to
    report `fixed` for a per_call run. The one exception tests presence only
    and then resolves (`llm_transports._default_max_tokens`)."""
    import re

    from pipeline_core.domain.model_profile import GLOBAL_DEFAULTS
    names = "|".join(GLOBAL_DEFAULTS)
    pat = re.compile(r"(getenv|environ\.get|environ\[)\(?\s*['\"](" + names + r")['\"]")
    allowed = {("pipeline_core/adapters/llm_transports.py", 'vllm_ctx = os.getenv("VLLM_MAX_MODEL_LEN")')}
    hits = []
    for path in [ROOT / "pipeline.py", *sorted((ROOT / "pipeline_core").rglob("*.py"))]:
        rel = path.relative_to(ROOT).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if pat.search(line) and (rel, line.strip()) not in allowed:
                hits.append(f"{rel}: {line.strip()}")
    assert hits == []


# ── RunConfig: same resolver, today's fingerprint kept ───────────────────────

# `.env` on 2026-10-06 with the four model lines in place. Its fingerprint,
# computed with the pre-profile RunConfig, was c093e8cd8cfb.
TODAY_ENV = {
    "MODEL": MUSE, "PHASE_C_DRIVER": "tdd", "PIPELINE_PARALLELISM": "4",
    "VLLM_REASONING_STRENGTH": "high", "VLLM_MAX_MODEL_LEN": "131072",
    "VLLM_MAX_THINK_CHARS": "200000", "VLLM_REASONING_BUDGET": "per_call",
    "VLLM_THINKING_BUDGET": "32768",
    # Every call at the maximum, nothing escalated: what "today" ran under
    # before the reasoning level existed.
    "LLM_EFFORT": "max", "TDD_ESCALATE_AFTER": "0",
}


def test_run_config_keeps_todays_fingerprint_with_and_without_the_lines():
    assert RunConfig.from_env(TODAY_ENV).fingerprint == "c093e8cd8cfb"
    trimmed = {k: v for k, v in TODAY_ENV.items()
               if k not in ("VLLM_REASONING_STRENGTH", "VLLM_MAX_MODEL_LEN",
                            "VLLM_REASONING_BUDGET", "VLLM_THINKING_BUDGET")}
    rc = RunConfig.from_env(trimmed)
    assert rc.fingerprint == "c093e8cd8cfb"
    assert rc.model_family == "muse-glimmer"
    assert dict(rc.knob_sources)["VLLM_MAX_MODEL_LEN"] == "profile"


def test_a_run_at_the_medium_default_does_not_share_the_max_baseline():
    base = RunConfig.from_env(TODAY_ENV)
    medium = RunConfig.from_env({k: v for k, v in TODAY_ENV.items()
                                 if k not in ("LLM_EFFORT", "TDD_ESCALATE_AFTER")})
    assert medium.fingerprint != base.fingerprint
    assert dict(medium.values)["LLM_EFFORT"] == "medium"
    assert "LLM_EFFORT" not in dict(base.values)


def test_run_config_hashes_a_later_knob_only_when_it_is_set():
    base = RunConfig.from_env(TODAY_ENV)
    capped = RunConfig.from_env({**TODAY_ENV, "VLLM_RUNAWAY_THINK_CHARS": "90000"})
    assert capped.fingerprint != base.fingerprint
    assert dict(capped.values)["VLLM_RUNAWAY_THINK_CHARS"] == 90000
    assert "VLLM_RUNAWAY_THINK_CHARS" not in dict(base.values)


# ── conformance verdicts on recorded server responses ────────────────────────


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def test_muse_live_fixture_passes_all_three_checks():
    d = _load("muse_glimmer_live.json")
    tok = d["tokenize"]
    assert mc.check_served(MUSE, d["models"]).ok
    assert mc.check_dialect(tok["strength_high"]["tokens"], tok["strength_low"]["tokens"],
                            {"reasoning_strength": "high"}, {"reasoning_strength": "low"}).ok
    split = mc.check_parser_split(ReasoningDialect.STRENGTH, d["generate_on"], d["generate_off"])
    assert split.ok, split.detail


def test_negative_control_the_boolean_payload_on_the_muse_server_fails_the_dialect_check():
    tok = _load("muse_glimmer_live.json")["tokenize"]
    verdict = mc.check_dialect(tok["enable_thinking_true"]["tokens"],
                               tok["enable_thinking_false"]["tokens"],
                               {"enable_thinking": True}, {"enable_thinking": False})
    assert not verdict.ok and "ignores the variable" in verdict.detail
    # The dropped variable renders exactly the template default, `high`.
    assert tok["enable_thinking_false"]["tokens"] == tok["strength_high"]["tokens"]


def test_qwen_tokenize_fixture_dialect_and_its_negative_control():
    tok = _load("qwen3_offline_tokenize.json")["tokenize"]
    assert mc.check_dialect(tok["enable_thinking_true"]["tokens"],
                            tok["enable_thinking_false"]["tokens"],
                            {"enable_thinking": True}, {"enable_thinking": False}).ok
    assert not mc.check_dialect(tok["strength_high"]["tokens"], tok["strength_low"]["tokens"],
                                {"reasoning_strength": "high"},
                                {"reasoning_strength": "low"}).ok


def test_served_id_mismatch_is_refused():
    d = _load("muse_glimmer_live.json")
    verdict = mc.check_served(QWEN, d["models"])
    assert not verdict.ok and MUSE in verdict.detail


def _chat(content: str, reasoning: str, finish: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content, "reasoning_content": reasoning},
                         "finish_reason": finish}]}


def test_parser_split_boolean_dialect_off_cases():
    on = _chat("ready", "thinking it over")
    ok = ReasoningDialect.ENABLE_THINKING
    assert mc.check_parser_split(ok, on, _chat("ready", "")).ok
    assert mc.check_parser_split(ok, on, _chat("", "ready")).ok          # the rerouted misroute
    assert not mc.check_parser_split(ok, on, _chat("ready", "reasoned anyway")).ok
    assert not mc.check_parser_split(ok, _chat("<think>x</think>ready", ""), _chat("ready", "")).ok
    assert not mc.check_parser_split(ok, _chat("ready", "r", "length"), _chat("ready", "")).ok


def test_report_at_run_start_emits_the_saved_verdict(tmp_path):
    lines: list[str] = []
    events: list[tuple[str, dict]] = []
    report = tmp_path / "model_conformance.json"
    report.write_text(json.dumps({"model": QWEN, "family": "qwen3", "ok": True,
                                  "checks": [], "payload_on": {}, "payload_off": {},
                                  "checked_at": "t"}))
    mc.report_at_run_start({"MODEL": QWEN}, log=lines.append,
                           emit=lambda e, **f: events.append((e, f)), report_path=report)
    assert "family=qwen3" in lines[0] and "VLLM_MAX_MODEL_LEN=131072(profile)" in lines[0]
    assert not any("no measured runaway cap" in ln for ln in lines)   # calibrated
    assert events == [("model.conformance", {"status": "pass", "model": QWEN, "family": "qwen3",
                                             "payload_on": {}, "payload_off": {},
                                             "payload_levels": None,
                                             "checks": [], "checked_at": "t"})]
    events.clear()
    mc.report_at_run_start({"MODEL": MUSE}, log=lines.append,
                           emit=lambda e, **f: events.append((e, f)), report_path=report)
    assert events[0][1]["status"] == "absent"    # the saved report names another model


def test_report_at_run_start_warns_on_an_uncalibrated_runaway_cap(tmp_path, monkeypatch):
    _uncalibrated_qwen(monkeypatch)
    lines: list[str] = []
    mc.report_at_run_start({"MODEL": QWEN}, log=lines.append, emit=lambda e, **f: None,
                           report_path=tmp_path / "absent.json")
    assert any("qwen3 has no measured runaway cap" in ln for ln in lines)

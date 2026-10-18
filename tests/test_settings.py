"""Pins FixLoopCaps.from_env — the env→value mapping the pipeline.py fix/stall
globals are now single-sourced from."""

from pipeline_core.domain.enums import LanguageDriverKind
from pipeline_core.settings import FixLoopCaps, Settings


def test_fixloopcaps_defaults(monkeypatch):
    for k in (
        "UNIFIED_FILL_LOOP", "U2_TEST_TIMEOUT", "PHASE_C_FN_HARD_CAP",
        "PHASE_C_FIX_CAP_PER_FN", "PHASE_C_FN_MAX_STALLS", "PHASE_C_TEST_FIX_HARD_CAP",
        "PHASE_C_FIX_CAP_PER_PASS", "PHASE_C_TEST_FIX_MAX_STALLS",
        "PIPELINE_PASS_THROUGH", "PIPELINE_PASS_THROUGH_STALLS", "PIPELINE_CONVERGE_MODULE",
    ):
        monkeypatch.delenv(k, raising=False)
    c = FixLoopCaps.from_env(single_call_char_limit=100_000)
    assert c.fix_err_cap == 10_000           # single_call // 10
    assert c.unified_fill_loop is True       # default "1"
    assert c.u2_test_timeout == 120
    assert c.per_fn_fix_hard_cap == 10_000
    assert c.per_fn_fix_max_stalls == 3
    assert c.per_pass_test_fix_hard_cap == 10_000
    assert c.per_pass_test_fix_max_stalls == 3
    assert c.pass_through is False
    assert c.pass_through_stalls == 1
    assert c.converge_module == ""


def test_fix_err_cap_floor(monkeypatch):
    # tiny budget → 2_000 floor, not single_call // 10
    assert FixLoopCaps.from_env(single_call_char_limit=5_000).fix_err_cap == 2_000


def test_fixloopcaps_env_overrides(monkeypatch):
    monkeypatch.setenv("UNIFIED_FILL_LOOP", "0")
    monkeypatch.setenv("U2_TEST_TIMEOUT", "300")
    monkeypatch.setenv("PHASE_C_FN_MAX_STALLS", "7")
    monkeypatch.setenv("PIPELINE_PASS_THROUGH", "1")
    monkeypatch.setenv("PIPELINE_PASS_THROUGH_STALLS", "2")
    monkeypatch.setenv("PIPELINE_CONVERGE_MODULE", "  context  ")
    c = FixLoopCaps.from_env(single_call_char_limit=100_000)
    assert c.unified_fill_loop is False
    assert c.u2_test_timeout == 300
    assert c.per_fn_fix_max_stalls == 7
    assert c.pass_through is True
    assert c.pass_through_stalls == 2
    assert c.converge_module == "context"   # stripped


def test_legacy_hard_cap_aliases(monkeypatch):
    # legacy PHASE_C_FIX_CAP_PER_FN / _PER_PASS fall back when the new key is absent
    monkeypatch.delenv("PHASE_C_FN_HARD_CAP", raising=False)
    monkeypatch.delenv("PHASE_C_TEST_FIX_HARD_CAP", raising=False)
    monkeypatch.setenv("PHASE_C_FIX_CAP_PER_FN", "42")
    monkeypatch.setenv("PHASE_C_FIX_CAP_PER_PASS", "99")
    c = FixLoopCaps.from_env(single_call_char_limit=100_000)
    assert c.per_fn_fix_hard_cap == 42
    assert c.per_pass_test_fix_hard_cap == 99


def test_language_driver_defaults_generic(monkeypatch):
    monkeypatch.delenv("LANGUAGE_DRIVER", raising=False)
    assert Settings.from_env().language_driver is LanguageDriverKind.GENERIC
    monkeypatch.setenv("LANGUAGE_DRIVER", "0")
    assert Settings.from_env().language_driver is LanguageDriverKind.GENERIC


def test_behaviour_pin_env_debug_defaults_on(monkeypatch):
    monkeypatch.delenv("BEHAVIOUR_PIN_ENV_DEBUG", raising=False)
    assert Settings.from_env().behaviour_pin_env_debug is True
    monkeypatch.setenv("BEHAVIOUR_PIN_ENV_DEBUG", "0")
    assert Settings.from_env().behaviour_pin_env_debug is False
    monkeypatch.setenv("BEHAVIOUR_PIN_ENV_DEBUG", "1")
    assert Settings.from_env().behaviour_pin_env_debug is True


def test_task0c_pass_stall_limit_reads_env_with_a_floor_of_one(monkeypatch):
    monkeypatch.delenv("TASK0C_PASS_STALL_LIMIT", raising=False)
    assert Settings.from_env().task0c_pass_stall_limit == 10
    monkeypatch.setenv("TASK0C_PASS_STALL_LIMIT", "2")
    assert Settings.from_env().task0c_pass_stall_limit == 2
    # 0 would hard-block every module on its first pass below target.
    monkeypatch.setenv("TASK0C_PASS_STALL_LIMIT", "0")
    assert Settings.from_env().task0c_pass_stall_limit == 1

"""std_api_def — built-in signature cards for well-known unstable std APIs
(the type_def pull fallback that unblocks nightly `std::error::Request` fns)."""
from pipeline_core.domain.std_api_sigs import std_api_def


def test_request_card_has_provide_ref_and_feature_gate():
    card = std_api_def("Request")
    assert "provide_ref" in card and "error_generic_member_access" in card
    assert "WRONG" in card and "RIGHT" in card   # concrete-example style


def test_request_free_fns_card():
    assert "request_ref<'a, T>" in std_api_def("request_ref")
    assert std_api_def("request_value") == std_api_def("request_ref")


def test_unknown_name_returns_empty():
    assert std_api_def("Error") == ""
    assert std_api_def("MyLocalType") == ""
    assert std_api_def("") == ""

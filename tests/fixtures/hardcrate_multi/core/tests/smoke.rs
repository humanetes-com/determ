//! Integration smoke pin — every family's public API, end to end.
use hcm_core::*;

#[test]
fn core_public_api_smoke() {
    assert_eq!(wrap_sum_0(&[200, 100]), 44);
    assert_eq!(clamp_add_6(1000, 2000, -50, 50), 50);
    assert_eq!(decode_id_12(&encode_id_12(42)), Some(42));
    assert_eq!(count_set_bits_18(255), 8);
    assert_eq!(Weighted(2).score(5), 10);
}

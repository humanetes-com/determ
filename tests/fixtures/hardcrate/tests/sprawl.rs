//! Integration pin for TRAP T7 (sprawl.rs) — the level-3 augmentation fan-out
//! reproducer. ONE input per fn: enough to pin behaviour (so reconstruction is
//! constrained) but NOT enough to cover every branch/arm, so TASK 0c augmentation
//! has a wide uncovered surface to fill. Keep it sparse on purpose.
use hardcrate::{
    abs_diff, clamp_u8, classify, count_set_bits, is_ascii_upper, max3, min3,
    nth_byte, parity, reverse_nibbles, saturating_sub_u8, sign, to_lower, Category,
};

#[test]
fn each_fn_pinned_on_one_path() {
    assert_eq!(clamp_u8(-5), 0); // the low branch (unit test pins the in-range one)
    assert_eq!(sign(7), 1);
    assert_eq!(abs_diff(3, 10), 7);
    assert_eq!(nth_byte(&[9, 8, 7], 1), Some(8));
    assert_eq!(is_ascii_upper(b'Q'), true);
    assert_eq!(to_lower(b'Z'), b'z');
    assert_eq!(count_set_bits(0b1011), 3);
    assert_eq!(parity(0b1011), true);
    assert_eq!(saturating_sub_u8(3, 10), 0);
    assert_eq!(min3(5, 2, 8), 2);
    assert_eq!(max3(5, 2, 8), 8);
    assert_eq!(reverse_nibbles(0x4A), 0xA4);
    assert_eq!(classify(42), Category::Large);
}

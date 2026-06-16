//! Integration pin for TRAP T1 (round.rs). complexity 1.
use hardcrate::divide_round;

#[test]
fn divide_round_is_half_to_even() {
    assert_eq!(divide_round(1, 2), 0);
    assert_eq!(divide_round(5, 2), 2);
    assert_eq!(divide_round(7, 2), 4);
    assert_eq!(divide_round(8, 3), 3);
}

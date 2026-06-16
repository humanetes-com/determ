//! Integration pin for TRAP T2 (merge.rs). complexity 1.
use hardcrate::merge_first_wins;

#[test]
fn merge_keeps_first_on_conflict() {
    let out = merge_first_wins(&[(2, 20), (1, 10)], &[(2, 99), (3, 30)]);
    assert_eq!(out, vec![(1, 10), (2, 20), (3, 30)]);
}

//! Integration pin for TRAP T3 (textsum.rs). complexity 1.
use hardcrate::{sum_csv, SumError};

#[test]
fn sum_csv_distinguishes_empty() {
    assert_eq!(sum_csv("4,5,6"), Ok(15));
    assert_eq!(sum_csv(""), Err(SumError::Empty));
    assert_eq!(sum_csv("1,bad"), Err(SumError::BadToken));
}

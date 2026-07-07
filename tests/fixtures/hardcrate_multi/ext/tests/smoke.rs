//! Integration smoke pin for the cross-crate consumer API.
use hcm_core::{Biased, Weighted};
use hcm_ext::*;

#[test]
fn ext_public_api_smoke() {
    let items = vec![1, 2, 3];
    assert_eq!(total_weighted_0(&items, &Weighted(2)), 12);
    assert_eq!(total_biased_0(&items, &Biased(1)), 9);
}

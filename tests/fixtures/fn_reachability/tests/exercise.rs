//! Integration tests of the fixture's public API. A downstream crate cannot even
//! use a value whose type is private (rustc's type-privacy check rejects
//! `leaked().clone()` and `shapes()`), so the cases that rest on such a value
//! are kept by the oracle without a test that runs them. These tests execute
//! the rest: the soundness reading of
//! `scripts/measure_fn_reachability.py --measure-executed` (a proved function
//! must not be among them).
use fn_reachability::*;

#[test]
fn public_surface_runs_the_kept_functions() {
    assert_eq!(bump(1), 2);
    assert_eq!(dup(3), 3);
    let e = fail();
    assert_eq!(format!("{} {:?}", e, e), "inner Inner");
    assert_eq!(uses_live(), 9);
    assert_eq!(hidden_api(), 1);
    assert_eq!(render(5), "5");
    assert_eq!(use_generic(4), 4);
    guarded();
    assert_eq!(Api(6).value(), 6);
}

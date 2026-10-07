//! One test per route `expected/routes.json` states, written from the route
//! line alone: the integration test runs these under llvm-cov and requires
//! each routed target to execute, which is what makes a route a fact.
use fn_routes::{entry, lazy, Context};

#[test]
fn display_error_debug_through_option_context_and_alternate() {
    let err = None::<u8>.context("boom").unwrap_err();
    assert_eq!(format!("{:#?}", err), "boom");
}

#[test]
fn error_impl_debug_through_the_box_conversion() {
    let err = None::<u8>.context("boom").unwrap_err();
    let boxed: Box<dyn std::error::Error + Send + Sync> = err.into();
    assert_eq!(format!("{:?}", boxed), "boom");
}

#[test]
fn leaf_through_entry() {
    assert_eq!(entry(), 4);
}

#[test]
fn in_closure_through_lazy() {
    assert_eq!(lazy(None), 5);
}

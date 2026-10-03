//! Integration pin for TRAP T7 (label.rs). complexity 4 — frozen-bound mismatch.
//! Pins `tagged` to render through the FROZEN `T: Describe` bound's method, and
//! `describe` to its per-variant text — the unique rule a human reading only the
//! tests derives.
use hardcrate::{Describe, Reason, Tagged};

#[test]
fn tagged_renders_via_describe() {
    assert_eq!(Tagged(Reason::Timeout).tagged("io"), "io: timed out");
    assert_eq!(Tagged(Reason::Refused).tagged("rpc"), "rpc: refused");
    assert_eq!(Reason::Refused.describe(), "refused");
}

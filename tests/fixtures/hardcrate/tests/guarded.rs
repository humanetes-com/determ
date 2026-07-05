//! Level-6 oracle — pins that `seal` and `verify` AGREE on a private tag scheme,
//! never the absolute scheme. Correctness is a property of the PAIR, so a single-fn
//! fill is behaviorally under-constrained and only the cross round-trip catches a
//! seal/verify disagreement.
use hardcrate::{seal, verify, Guarded};

#[test]
fn addr_round_trips_through_seal() {
    assert_eq!(seal(0).addr(), 0);
    assert_eq!(seal(42).addr(), 42);
    assert_eq!(seal(0xDEAD_BEEF).addr(), 0xDEAD_BEEF);
}

#[test]
fn seal_output_verifies() {
    for x in [0u32, 1, 2, 255, 256, 65_535, 1_000_000, 0xFFFF_FFFF] {
        assert!(verify(&seal(x)), "seal({x}) must verify");
    }
}

#[test]
fn tag_depends_on_address() {
    // Distinct addresses must not all collide to one tag — pins that the tag is
    // actually derived FROM the address (a constant tag would pass round-trip).
    assert_ne!(seal(1).tag(), seal(2).tag());
    assert_ne!(seal(0).tag(), seal(0x0100_0000).tag());
}

#[test]
fn tampered_tag_is_rejected() {
    // Forge a value whose tag is one the address does not produce.
    let good = seal(5);
    let wrong_tag = seal(6).tag();
    assert_ne!(good.tag(), wrong_tag, "test precondition: tags differ");
    assert!(!verify(&Guarded(5, wrong_tag)), "a mismatched tag must fail verify");
    assert!(!verify(&Guarded(5, good.tag().wrapping_add(1))));
}

#[test]
fn accessors_read_the_fields() {
    let g = Guarded(7, 9);
    assert_eq!(g.addr(), 7);
    assert_eq!(g.tag(), 9);
}

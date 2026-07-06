//! Level-7 oracle — pins that `link_a::stamp` and `link_b::unstamp` AGREE on a
//! private mix scheme split across two files, never the absolute scheme. A greedy
//! per-file fix loop that fills each file in its own pass/context cannot co-design
//! them in one call the way `guarded.rs`'s single-file `seal`/`verify` can — only
//! this cross-file round-trip test catches a mismatch.
use hardcrate::{stamp, unstamp};

#[test]
fn stamp_then_unstamp_round_trips() {
    for p in [0u16, 1, 42, 255, 256, 4096, 65_535] {
        assert_eq!(unstamp(stamp(p)), Some(p), "payload {p} must round-trip");
    }
}

#[test]
fn mix_depends_on_payload() {
    // Distinct payloads must not all collide to one mix value — pins that the mix
    // is actually derived FROM the payload (a constant mix would pass round-trip).
    assert_ne!(stamp(1) >> 16, stamp(2) >> 16);
    assert_ne!(stamp(0) >> 16, stamp(4096) >> 16);
}

#[test]
fn tampered_code_is_rejected() {
    // Forge a code whose high bits are a mix value that does NOT belong to its
    // own low-bits payload.
    let good = stamp(5);
    let wrong_mix = stamp(6) & 0xFFFF_0000;
    assert_ne!(good & 0xFFFF_0000, wrong_mix, "test precondition: mixes differ");
    let tampered = wrong_mix | 5u32;
    assert_eq!(unstamp(tampered), None, "a mismatched mix must fail unstamp");
}

#[test]
fn low_bits_carry_the_payload_verbatim() {
    // Even before any mix logic is trusted, the low 16 bits of `stamp`'s output
    // must be the payload unchanged.
    assert_eq!(stamp(1234) & 0xFFFF, 1234);
}

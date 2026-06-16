//! Integration pin for TRAP T5 (wire.rs) — the cross-fn codec. complexity 1.
//! The EXACT wire format lives ONLY here; the unit tests in src/wire.rs check
//! round-trip alone, so a per-fn fill can't see the format without this holistic
//! pin (the lever meant to push recovery to task4).
use hardcrate::{decode_id, encode_id};

#[test]
fn wire_format_is_pinned() {
    assert_eq!(encode_id(0), "0000");
    assert_eq!(encode_id(42), "002a");
    assert_eq!(encode_id(255), "00ff");
    assert_eq!(decode_id("0010"), Some(16));
    assert_eq!(decode_id("zz"), None);
}

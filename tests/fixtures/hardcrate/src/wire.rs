//! TRAP T5 (HARD, cross-fn) — `encode_id`/`decode_id` are codependent (one wire
//! scheme, two fns) and the EXACT format is pinned ONLY by the integration test.
//! The unit tests check ROUND-TRIP alone, so a per-fn fill satisfies them with ANY
//! consistent scheme (decimal, unpadded hex, …); the integration pin then fails
//! BOTH fns at once — the cross-fn consistency a single-fn fix loop oscillates on
//! (fix encode → decode no longer inverts it → fix decode → …), which is what
//! should push the repair past the phase-c loop into task4/genesis. The wire
//! format (4-char zero-padded lowercase hex) is deliberately NOT documented here.

/// Encode an id to its wire string.
pub fn encode_id(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if it is not a valid wire form.
pub fn decode_id(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_roundtrip() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535] {
            assert_eq!(decode_id(&encode_id(id)), Some(id));
        }
    }

    #[test]
    fn decode_rejects_garbage() {
        assert_eq!(decode_id(""), None);
        assert_eq!(decode_id("zz"), None);
    }
}

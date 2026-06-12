//! Tag codec — the ITERATION trap: `encode_tag`/`decode_tag` are codependent
//! (one wire scheme, two fns) and the exact format is pinned ONLY by the
//! integration tests. The unit tests below check round-trip alone, so a
//! per-fn fill can satisfy them with ANY invented scheme; the integration
//! pin then fails both fns at once — cross-fn consistency that single-fn
//! attribution struggles with, driving the repair loop (audit C1–C4
//! evidence). The format itself is deliberately NOT documented here.

/// Encode a tag id into its compact wire form.
///
/// The wire format is an implementation detail of the codec pair:
/// [`decode_tag`] must accept exactly what `encode_tag` emits.
pub fn encode_tag(id: u32) -> String {
    format!("t{id}")
}

/// Decode a wire-form tag produced by [`encode_tag`].
///
/// Returns `None` for anything `encode_tag` could not have emitted.
pub fn decode_tag(s: &str) -> Option<u32> {
    s.strip_prefix('t')?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tag_roundtrip() {
        for id in [0, 1, 9, 10, 4242] {
            assert_eq!(decode_tag(&encode_tag(id)), Some(id));
        }
    }

    #[test]
    fn decode_rejects_non_tags() {
        assert_eq!(decode_tag(""), None);
        assert_eq!(decode_tag("zzz"), None);
    }
}

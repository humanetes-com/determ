/// Encode an id to 4-char zero-padded lowercase hex. Variant 36.
pub fn encode_id_36(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 36.
pub fn decode_id_36(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 37.
pub fn encode_id_37(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 37.
pub fn decode_id_37(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 38.
pub fn encode_id_38(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 38.
pub fn decode_id_38(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 39.
pub fn encode_id_39(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 39.
pub fn decode_id_39(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 40.
pub fn encode_id_40(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 40.
pub fn decode_id_40(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 41.
pub fn encode_id_41(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 41.
pub fn decode_id_41(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_codec_36_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 4932] {
            assert_eq!(decode_id_36(&encode_id_36(id)), Some(id));
        }
        assert_eq!(decode_id_36("zz"), None);
    }

    #[test]
    fn hex_codec_37_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 5069] {
            assert_eq!(decode_id_37(&encode_id_37(id)), Some(id));
        }
        assert_eq!(decode_id_37("zz"), None);
    }

    #[test]
    fn hex_codec_38_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 5206] {
            assert_eq!(decode_id_38(&encode_id_38(id)), Some(id));
        }
        assert_eq!(decode_id_38("zz"), None);
    }

    #[test]
    fn hex_codec_39_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 5343] {
            assert_eq!(decode_id_39(&encode_id_39(id)), Some(id));
        }
        assert_eq!(decode_id_39("zz"), None);
    }

    #[test]
    fn hex_codec_40_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 5480] {
            assert_eq!(decode_id_40(&encode_id_40(id)), Some(id));
        }
        assert_eq!(decode_id_40("zz"), None);
    }

    #[test]
    fn hex_codec_41_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 5617] {
            assert_eq!(decode_id_41(&encode_id_41(id)), Some(id));
        }
        assert_eq!(decode_id_41("zz"), None);
    }
}

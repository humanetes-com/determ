/// Encode an id to 4-char zero-padded lowercase hex. Variant 60.
pub fn encode_id_60(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 60.
pub fn decode_id_60(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 61.
pub fn encode_id_61(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 61.
pub fn decode_id_61(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 62.
pub fn encode_id_62(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 62.
pub fn decode_id_62(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 63.
pub fn encode_id_63(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 63.
pub fn decode_id_63(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 64.
pub fn encode_id_64(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 64.
pub fn decode_id_64(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 65.
pub fn encode_id_65(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 65.
pub fn decode_id_65(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_codec_60_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 8220] {
            assert_eq!(decode_id_60(&encode_id_60(id)), Some(id));
        }
        assert_eq!(decode_id_60("zz"), None);
    }

    #[test]
    fn hex_codec_61_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 8357] {
            assert_eq!(decode_id_61(&encode_id_61(id)), Some(id));
        }
        assert_eq!(decode_id_61("zz"), None);
    }

    #[test]
    fn hex_codec_62_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 8494] {
            assert_eq!(decode_id_62(&encode_id_62(id)), Some(id));
        }
        assert_eq!(decode_id_62("zz"), None);
    }

    #[test]
    fn hex_codec_63_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 8631] {
            assert_eq!(decode_id_63(&encode_id_63(id)), Some(id));
        }
        assert_eq!(decode_id_63("zz"), None);
    }

    #[test]
    fn hex_codec_64_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 8768] {
            assert_eq!(decode_id_64(&encode_id_64(id)), Some(id));
        }
        assert_eq!(decode_id_64("zz"), None);
    }

    #[test]
    fn hex_codec_65_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 8905] {
            assert_eq!(decode_id_65(&encode_id_65(id)), Some(id));
        }
        assert_eq!(decode_id_65("zz"), None);
    }
}

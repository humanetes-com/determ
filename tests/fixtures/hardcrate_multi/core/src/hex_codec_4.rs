/// Encode an id to 4-char zero-padded lowercase hex. Variant 84.
pub fn encode_id_84(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 84.
pub fn decode_id_84(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 85.
pub fn encode_id_85(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 85.
pub fn decode_id_85(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 86.
pub fn encode_id_86(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 86.
pub fn decode_id_86(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 87.
pub fn encode_id_87(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 87.
pub fn decode_id_87(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 88.
pub fn encode_id_88(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 88.
pub fn decode_id_88(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 89.
pub fn encode_id_89(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 89.
pub fn decode_id_89(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_codec_84_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 11508] {
            assert_eq!(decode_id_84(&encode_id_84(id)), Some(id));
        }
        assert_eq!(decode_id_84("zz"), None);
    }

    #[test]
    fn hex_codec_85_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 11645] {
            assert_eq!(decode_id_85(&encode_id_85(id)), Some(id));
        }
        assert_eq!(decode_id_85("zz"), None);
    }

    #[test]
    fn hex_codec_86_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 11782] {
            assert_eq!(decode_id_86(&encode_id_86(id)), Some(id));
        }
        assert_eq!(decode_id_86("zz"), None);
    }

    #[test]
    fn hex_codec_87_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 11919] {
            assert_eq!(decode_id_87(&encode_id_87(id)), Some(id));
        }
        assert_eq!(decode_id_87("zz"), None);
    }

    #[test]
    fn hex_codec_88_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 12056] {
            assert_eq!(decode_id_88(&encode_id_88(id)), Some(id));
        }
        assert_eq!(decode_id_88("zz"), None);
    }

    #[test]
    fn hex_codec_89_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 12193] {
            assert_eq!(decode_id_89(&encode_id_89(id)), Some(id));
        }
        assert_eq!(decode_id_89("zz"), None);
    }
}

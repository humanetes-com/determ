/// Encode an id to 4-char zero-padded lowercase hex. Variant 12.
pub fn encode_id_12(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 12.
pub fn decode_id_12(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 13.
pub fn encode_id_13(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 13.
pub fn decode_id_13(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 14.
pub fn encode_id_14(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 14.
pub fn decode_id_14(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 15.
pub fn encode_id_15(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 15.
pub fn decode_id_15(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 16.
pub fn encode_id_16(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 16.
pub fn decode_id_16(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

/// Encode an id to 4-char zero-padded lowercase hex. Variant 17.
pub fn encode_id_17(id: u16) -> String {
    format!("{id:04x}")
}

/// Decode a wire string back to its id; `None` if not valid hex. Variant 17.
pub fn decode_id_17(s: &str) -> Option<u16> {
    u16::from_str_radix(s, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_codec_12_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 1644] {
            assert_eq!(decode_id_12(&encode_id_12(id)), Some(id));
        }
        assert_eq!(decode_id_12("zz"), None);
    }

    #[test]
    fn hex_codec_13_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 1781] {
            assert_eq!(decode_id_13(&encode_id_13(id)), Some(id));
        }
        assert_eq!(decode_id_13("zz"), None);
    }

    #[test]
    fn hex_codec_14_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 1918] {
            assert_eq!(decode_id_14(&encode_id_14(id)), Some(id));
        }
        assert_eq!(decode_id_14("zz"), None);
    }

    #[test]
    fn hex_codec_15_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 2055] {
            assert_eq!(decode_id_15(&encode_id_15(id)), Some(id));
        }
        assert_eq!(decode_id_15("zz"), None);
    }

    #[test]
    fn hex_codec_16_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 2192] {
            assert_eq!(decode_id_16(&encode_id_16(id)), Some(id));
        }
        assert_eq!(decode_id_16("zz"), None);
    }

    #[test]
    fn hex_codec_17_roundtrips() {
        for id in [0u16, 1, 16, 42, 255, 4096, 65535, 2329] {
            assert_eq!(decode_id_17(&encode_id_17(id)), Some(id));
        }
        assert_eq!(decode_id_17("zz"), None);
    }
}

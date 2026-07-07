/// Add then clamp to [lo, hi]. Variant 54.
pub fn clamp_add_54(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 55.
pub fn clamp_add_55(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 56.
pub fn clamp_add_56(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 57.
pub fn clamp_add_57(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 58.
pub fn clamp_add_58(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 59.
pub fn clamp_add_59(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_add_54_clamps(){
        assert_eq!(clamp_add_54(10, 5, -104, 104), 15);
        assert_eq!(clamp_add_54(1000, 2000, -104, 104), 104);
        assert_eq!(clamp_add_54(-1000, -2000, -104, 104), -104);
        assert_eq!(clamp_add_54(0, 0, -104, 104), 0);
        assert_eq!(clamp_add_54(54, -54, -104, 104), 0);
    }

    #[test]
    fn clamp_add_55_clamps(){
        assert_eq!(clamp_add_55(10, 5, -105, 105), 15);
        assert_eq!(clamp_add_55(1000, 2000, -105, 105), 105);
        assert_eq!(clamp_add_55(-1000, -2000, -105, 105), -105);
        assert_eq!(clamp_add_55(0, 0, -105, 105), 0);
        assert_eq!(clamp_add_55(55, -55, -105, 105), 0);
    }

    #[test]
    fn clamp_add_56_clamps(){
        assert_eq!(clamp_add_56(10, 5, -106, 106), 15);
        assert_eq!(clamp_add_56(1000, 2000, -106, 106), 106);
        assert_eq!(clamp_add_56(-1000, -2000, -106, 106), -106);
        assert_eq!(clamp_add_56(0, 0, -106, 106), 0);
        assert_eq!(clamp_add_56(56, -56, -106, 106), 0);
    }

    #[test]
    fn clamp_add_57_clamps(){
        assert_eq!(clamp_add_57(10, 5, -107, 107), 15);
        assert_eq!(clamp_add_57(1000, 2000, -107, 107), 107);
        assert_eq!(clamp_add_57(-1000, -2000, -107, 107), -107);
        assert_eq!(clamp_add_57(0, 0, -107, 107), 0);
        assert_eq!(clamp_add_57(57, -57, -107, 107), 0);
    }

    #[test]
    fn clamp_add_58_clamps(){
        assert_eq!(clamp_add_58(10, 5, -108, 108), 15);
        assert_eq!(clamp_add_58(1000, 2000, -108, 108), 108);
        assert_eq!(clamp_add_58(-1000, -2000, -108, 108), -108);
        assert_eq!(clamp_add_58(0, 0, -108, 108), 0);
        assert_eq!(clamp_add_58(58, -58, -108, 108), 0);
    }

    #[test]
    fn clamp_add_59_clamps(){
        assert_eq!(clamp_add_59(10, 5, -109, 109), 15);
        assert_eq!(clamp_add_59(1000, 2000, -109, 109), 109);
        assert_eq!(clamp_add_59(-1000, -2000, -109, 109), -109);
        assert_eq!(clamp_add_59(0, 0, -109, 109), 0);
        assert_eq!(clamp_add_59(59, -59, -109, 109), 0);
    }
}

/// Add then clamp to [lo, hi]. Variant 102.
pub fn clamp_add_102(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 103.
pub fn clamp_add_103(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 104.
pub fn clamp_add_104(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 105.
pub fn clamp_add_105(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 106.
pub fn clamp_add_106(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

/// Add then clamp to [lo, hi]. Variant 107.
pub fn clamp_add_107(a: i32, b: i32, lo: i32, hi: i32) -> i32 {
    (a + b).clamp(lo, hi)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamp_add_102_clamps(){
        assert_eq!(clamp_add_102(10, 5, -152, 152), 15);
        assert_eq!(clamp_add_102(1000, 2000, -152, 152), 152);
        assert_eq!(clamp_add_102(-1000, -2000, -152, 152), -152);
        assert_eq!(clamp_add_102(0, 0, -152, 152), 0);
        assert_eq!(clamp_add_102(102, -102, -152, 152), 0);
    }

    #[test]
    fn clamp_add_103_clamps(){
        assert_eq!(clamp_add_103(10, 5, -153, 153), 15);
        assert_eq!(clamp_add_103(1000, 2000, -153, 153), 153);
        assert_eq!(clamp_add_103(-1000, -2000, -153, 153), -153);
        assert_eq!(clamp_add_103(0, 0, -153, 153), 0);
        assert_eq!(clamp_add_103(103, -103, -153, 153), 0);
    }

    #[test]
    fn clamp_add_104_clamps(){
        assert_eq!(clamp_add_104(10, 5, -154, 154), 15);
        assert_eq!(clamp_add_104(1000, 2000, -154, 154), 154);
        assert_eq!(clamp_add_104(-1000, -2000, -154, 154), -154);
        assert_eq!(clamp_add_104(0, 0, -154, 154), 0);
        assert_eq!(clamp_add_104(104, -104, -154, 154), 0);
    }

    #[test]
    fn clamp_add_105_clamps(){
        assert_eq!(clamp_add_105(10, 5, -155, 155), 15);
        assert_eq!(clamp_add_105(1000, 2000, -155, 155), 155);
        assert_eq!(clamp_add_105(-1000, -2000, -155, 155), -155);
        assert_eq!(clamp_add_105(0, 0, -155, 155), 0);
        assert_eq!(clamp_add_105(105, -105, -155, 155), 0);
    }

    #[test]
    fn clamp_add_106_clamps(){
        assert_eq!(clamp_add_106(10, 5, -156, 156), 15);
        assert_eq!(clamp_add_106(1000, 2000, -156, 156), 156);
        assert_eq!(clamp_add_106(-1000, -2000, -156, 156), -156);
        assert_eq!(clamp_add_106(0, 0, -156, 156), 0);
        assert_eq!(clamp_add_106(106, -106, -156, 156), 0);
    }

    #[test]
    fn clamp_add_107_clamps(){
        assert_eq!(clamp_add_107(10, 5, -157, 157), 15);
        assert_eq!(clamp_add_107(1000, 2000, -157, 157), 157);
        assert_eq!(clamp_add_107(-1000, -2000, -157, 157), -157);
        assert_eq!(clamp_add_107(0, 0, -157, 157), 0);
        assert_eq!(clamp_add_107(107, -107, -157, 157), 0);
    }
}

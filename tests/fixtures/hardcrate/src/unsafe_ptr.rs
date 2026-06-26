//! TRAP T8 (level 5) — unsafe pointer arithmetic.
//! Tests raw *const/*mut offset/read/write round-trips.

/// Read the value at `p.offset(i)`.
pub unsafe fn read_at<T: Copy>(p: *const T, i: isize) -> T {
    *p.offset(i)
}

/// Write `val` to `p.offset(i)`.
pub unsafe fn write_at<T>(p: *mut T, i: isize, val: T) {
    *p.offset(i) = val;
}

/// Number of elements between two raw pointers of the same type.
pub unsafe fn ptr_distance<T>(start: *const T, end: *const T) -> usize {
    end.offset_from(start) as usize
}

/// Copy all elements from `src` via raw pointer reads.
pub fn copy_via_raw<T: Copy>(src: &[T]) -> Vec<T> {
    let mut dst = Vec::with_capacity(src.len());
    unsafe {
        for i in 0..src.len() as isize {
            dst.push(read_at(src.as_ptr(), i));
        }
    }
    dst
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_at_first() {
        let arr = [10u32, 20, 30];
        unsafe { assert_eq!(read_at(arr.as_ptr(), 0), 10); }
    }

    #[test]
    fn read_at_offset() {
        let arr = [1i64, 2, 3];
        unsafe { assert_eq!(read_at(arr.as_ptr(), 2), 3); }
    }

    #[test]
    fn write_at_round_trip() {
        let mut arr = [0u8; 4];
        unsafe { write_at(arr.as_mut_ptr(), 2, 99u8); }
        assert_eq!(arr[2], 99);
    }

    #[test]
    fn ptr_distance_zero() {
        let arr = [0u32; 1];
        let p = arr.as_ptr();
        unsafe { assert_eq!(ptr_distance(p, p), 0); }
    }

    #[test]
    fn ptr_distance_n() {
        let arr = [0u64; 5];
        unsafe {
            assert_eq!(ptr_distance(arr.as_ptr(), arr.as_ptr().offset(5)), 5);
        }
    }

    #[test]
    fn copy_via_raw_empty() {
        let v: Vec<i32> = copy_via_raw(&[]);
        assert!(v.is_empty());
    }

    #[test]
    fn copy_via_raw_values() {
        let src = vec![7u32, 8, 9];
        assert_eq!(copy_via_raw(&src), src);
    }
}

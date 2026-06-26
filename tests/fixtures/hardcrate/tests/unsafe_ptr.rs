//! Integration pins for the unsafe_ptr trap (level 5).

use hardcrate::{copy_via_raw, ptr_distance, read_at, write_at};

#[test]
fn read_at_matches_index() {
    let data = [100u32, 200, 300, 400];
    for i in 0..data.len() {
        unsafe {
            assert_eq!(read_at(data.as_ptr(), i as isize), data[i]);
        }
    }
}

#[test]
fn write_at_mutates_correct_slot() {
    let mut buf = [0i32; 8];
    unsafe {
        write_at(buf.as_mut_ptr(), 3, 42);
        write_at(buf.as_mut_ptr(), 7, -1);
    }
    assert_eq!(buf[3], 42);
    assert_eq!(buf[7], -1);
    assert_eq!(buf[0], 0);
}

#[test]
fn ptr_distance_consecutive() {
    let arr = [0u8; 10];
    unsafe {
        assert_eq!(ptr_distance(arr.as_ptr(), arr.as_ptr().offset(10)), 10);
    }
}

#[test]
fn ptr_distance_same_pointer() {
    let x = 42u64;
    let p = &x as *const u64;
    unsafe {
        assert_eq!(ptr_distance(p, p), 0);
    }
}

#[test]
fn copy_via_raw_produces_identical_slice() {
    let src: Vec<u64> = (0..16).collect();
    assert_eq!(copy_via_raw(&src), src);
}

#[test]
fn copy_via_raw_empty_slice() {
    let result: Vec<f64> = copy_via_raw(&[]);
    assert!(result.is_empty());
}

#[test]
fn copy_via_raw_single_element() {
    assert_eq!(copy_via_raw(&[99u32]), vec![99u32]);
}

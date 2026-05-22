// Tiny fixture crate for pipeline smoke tests.
//
// Coverage profile (with the bundled tests):
//   - add()           — fully covered
//   - divide()        — partially covered (only b==0 branch)
//   - never_called()  — fully uncovered
//
// Use this fixture to verify coverage measurement, gen_tests sibling-file
// handling, and other pipeline mechanics without running on a large crate.

pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

pub fn divide(a: i32, b: i32) -> Option<i32> {
    if b == 0 {
        None
    } else {
        Some(a / b)
    }
}

pub fn never_called() -> i32 {
    42
}

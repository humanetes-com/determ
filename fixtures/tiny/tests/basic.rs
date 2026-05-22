use tiny::*;

#[test]
fn test_add() {
    assert_eq!(add(2, 3), 5);
}

#[test]
fn test_divide_by_zero() {
    assert_eq!(divide(10, 0), None);
}

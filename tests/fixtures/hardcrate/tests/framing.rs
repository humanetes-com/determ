//! Integration pin for TRAP T6 (framing.rs) — the level-2 frame protocol. The
//! EXACT wire bytes AND the validation rules live ONLY here (the unit tests check
//! round-trip alone), so per-fn fills can't see the format without this holistic
//! pin, and the accept-valid vs reject-corrupt cases pull deframe in tension.
use hardcrate::{deframe, frame};

#[test]
fn frame_wire_format_is_pinned() {
    // layout = [len, payload.., xor_of_payload]
    assert_eq!(frame(&[1, 2, 3]), vec![3, 1, 2, 3, 0]); // 1^2^3 == 0
    assert_eq!(frame(&[]), vec![0, 0]);
    assert_eq!(frame(&[9, 9]), vec![2, 9, 9, 0]);

    // deframe accepts a well-formed frame …
    assert_eq!(deframe(&[1, 5, 5]), Some(vec![5]));
    // … rejects a bad CHECKSUM (payload [5] → xor 5, not 6) …
    assert_eq!(deframe(&[1, 5, 6]), None);
    // … and rejects a bad LENGTH (claims 2 payload bytes, buffer too short).
    assert_eq!(deframe(&[2, 5, 5]), None);
}

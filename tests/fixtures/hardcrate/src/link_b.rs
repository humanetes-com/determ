//! Level-7 trap, HALF B — see `link_a.rs` for the full rationale.
//!
//! `unstamp` must re-derive the SAME mix value `link_a::stamp` computed, via its own
//! independently-named private helper (`mix_b`). Correctness is a property of the
//! PAIR across two files, not of either file alone.

/// Fold a payload into a 16-bit mix value. Private to this file — must match
/// `link_a::mix_a`'s scheme exactly, but is named/written independently so a fill
/// of this file has no syntactic reference to the other file's private scheme.
fn mix_b(payload: u16) -> u16 {
    let mut acc: u16 = 0x1234;
    acc ^= payload.rotate_left(3);
    acc = acc.wrapping_mul(0x9E37) ^ (payload >> 8);
    acc
}

/// Unstamp a code produced by `link_a::stamp`: split it into its mix value (high 16
/// bits) and payload (low 16 bits), and return the payload only if the mix value
/// matches what this file's scheme would produce for that payload.
pub fn unstamp(code: u32) -> Option<u16> {
    let payload = (code & 0xFFFF) as u16;
    let mix = (code >> 16) as u16;
    if mix == mix_b(payload) {
        Some(payload)
    } else {
        None
    }
}

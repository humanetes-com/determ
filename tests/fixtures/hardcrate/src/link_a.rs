//! Level-7 trap, HALF A — REGRESSION-CYCLE (two files, one shared scheme).
//!
//! `stamp` (here) and `unstamp` (`link_b.rs`) must agree on a private "mix" scheme,
//! exactly like `guarded.rs`'s `seal`/`verify` — but this time the two fns live in
//! SEPARATE FILES with INDEPENDENTLY NAMED private helpers (`mix_a` here, `mix_b` in
//! `link_b.rs`), so a greedy PER-FILE fix loop (fill `link_a.rs`, then `link_b.rs`,
//! each its own LLM call/context) cannot co-design them in one pass the way a single
//! per-impl call can for `guarded.rs`. Only the cross-file round-trip test
//! (`tests/link_pair.rs`) catches a mismatch, and fixing one side to match the other's
//! CURRENT scheme, then later re-fixing that side again, is what forces ≥2 outer TDD
//! iterations with no single-pass fixed point (C3/C4/C6).

/// Fold a payload into a 16-bit mix value. Private to this file — the scheme is free
/// to choose, but it MUST match `link_b::mix_b`'s scheme (tests only pin agreement,
/// never the absolute value).
fn mix_a(payload: u16) -> u16 {
    let mut acc: u16 = 0x1234;
    acc ^= payload.rotate_left(3);
    acc = acc.wrapping_mul(0x9E37) ^ (payload >> 8);
    acc
}

/// Stamp a payload: pack its mix value into the high 16 bits and the payload itself
/// into the low 16 bits.
pub fn stamp(payload: u16) -> u32 {
    ((mix_a(payload) as u32) << 16) | payload as u32
}

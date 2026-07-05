//! Level-6 trap — CO-DESIGNED PRIVATE SCHEME (the anyhow ContextError reproducer).
//!
//! `seal` CONSTRUCTS a `Guarded` (a tuple struct — the constructor is `Guarded(a, t)`,
//! never a `::new`, so the naive `Guarded::new(..)` fill is an E0599) and stores a tag
//! derived from the address. `verify` RE-DERIVES the tag and compares. The tests pin
//! ONLY that seal and verify AGREE (round-trip + rejection + distinctness) — they do
//! NOT pin the absolute tag scheme. So any single-fn fill is behaviorally under-
//! constrained; correctness is a property of the PAIR. A model that picks one scheme
//! for `seal` and a different one for `verify` compiles clean but fails the cross
//! round-trip test — the exact "compiles but tests FAIL" class the TDD re-fill loop
//! must recover across iterations (C3/C4), and a wrong re-fill of one side reddens the
//! round-trip that the other side passed (C6 ratchet).

/// An addressed payload: the address and a guard tag derived from it.
/// Fields are public so a test can forge a tampered value; construction is still
/// the tuple form `Guarded(addr, tag)` (there is no `::new`).
pub struct Guarded(pub u32, pub u16);

impl Guarded {
    /// The stored address.
    pub fn addr(&self) -> u32 {
        self.0
    }

    /// The stored guard tag.
    pub fn tag(&self) -> u16 {
        self.1
    }
}

/// Fold an address into a 16-bit guard tag. Private — the scheme is free to choose,
/// but `seal` and `verify` MUST use the same one.
fn derive_tag(addr: u32) -> u16 {
    let mut acc: u16 = 0xABCD;
    for i in 0..4 {
        let b = ((addr >> (i * 8)) & 0xFF) as u16;
        acc = acc.rotate_left(5) ^ b.wrapping_mul(0x9E37);
    }
    acc
}

/// Seal an address into a guarded value, computing its tag from the address.
pub fn seal(addr: u32) -> Guarded {
    Guarded(addr, derive_tag(addr))
}

/// True iff the guarded value's tag matches the one its address would produce
/// (i.e. it was produced by [`seal`] and not tampered with).
pub fn verify(g: &Guarded) -> bool {
    g.tag() == derive_tag(g.addr())
}

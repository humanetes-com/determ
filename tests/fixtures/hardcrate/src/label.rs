//! TRAP T7 (HARD, level 4) — frozen-bound mismatch.
//!
//! `Tagged::tagged` is a generic method whose impl carries the FROZEN bound
//! `where T: Describe` (skeletonise stubs only fn BODIES — the trait def, the
//! `impl … where` clause and the struct stay verbatim). The body MUST reach the
//! payload's text through that bound's method, `T::describe`.
//!
//! Obvious-wrong attractor: a stubbed body naturally writes
//! `format!("{}: {}", ctx, self.0)` (needs `T: Display`) or `{:?}` (needs
//! `T: Debug`) — NEITHER bound is carried by the impl, so both E0277. The fill
//! only compiles by USING the bound-provided `describe`. This is the case the
//! BOUND_METHODS prompt block exists for: once the model sees that the frozen
//! `T: Describe` bound offers a callable `describe(&self) -> String`, it stops
//! reaching for Display/Debug and fills via the bound method on the first try
//! (GOOD = no E0277 thrash, no GENUINELY_HARD skip of a reconstructable fn).

/// A value that can describe itself for a labelled context.
pub trait Describe {
    /// Short human description of this value.
    fn describe(&self) -> String;
}

/// A reason code that implements [`Describe`] but deliberately NOT `Display` or
/// `Debug`-for-text, so the wrong `format!("{}", x)` / `{:?}` attractors fail to
/// compile and the only in-scope text path is the bound's `describe`.
#[derive(PartialEq, Eq, Clone, Copy)]
pub enum Reason {
    /// The operation ran out of time.
    Timeout,
    /// The peer rejected the operation.
    Refused,
}

impl Describe for Reason {
    fn describe(&self) -> String {
        match self {
            Reason::Timeout => "timed out".to_string(),
            Reason::Refused => "refused".to_string(),
        }
    }
}

/// Pairs a payload with a context label for rendering.
pub struct Tagged<T>(pub T);

impl<T> Tagged<T>
where
    T: Describe,
{
    /// Render the payload as `"<ctx>: <description>"`.
    pub fn tagged(&self, ctx: &str) -> String {
        format!("{}: {}", ctx, self.0.describe())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reason_describes() {
        assert_eq!(Reason::Timeout.describe(), "timed out");
        assert_eq!(Reason::Refused.describe(), "refused");
    }

    #[test]
    fn tagged_uses_bound_method() {
        assert_eq!(Tagged(Reason::Timeout).tagged("conn"), "conn: timed out");
        assert_eq!(Tagged(Reason::Refused).tagged("auth"), "auth: refused");
    }
}

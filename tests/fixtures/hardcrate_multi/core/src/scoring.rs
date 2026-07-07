//! Hand-written anchor: the PUBLIC API `hcm_ext` depends on across the crate
//! boundary. Reconstructing `hcm_ext` correctly requires the pipeline to see
//! THIS module's public signatures (via the workspace dependency graph) without
//! ever seeing `hcm_core`'s private internals.

/// Something that assigns a score to an integer.
pub trait Scorer {
    fn score(&self, x: i32) -> i32;
}

/// Multiplies by a fixed weight.
pub struct Weighted(pub i32);

impl Scorer for Weighted {
    fn score(&self, x: i32) -> i32 {
        x * self.0
    }
}

/// Adds a fixed bias.
pub struct Biased(pub i32);

impl Scorer for Biased {
    fn score(&self, x: i32) -> i32 {
        x + self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn weighted_scores() {
        assert_eq!(Weighted(3).score(4), 12);
        assert_eq!(Weighted(-2).score(5), -10);
    }

    #[test]
    fn biased_scores() {
        assert_eq!(Biased(3).score(4), 7);
        assert_eq!(Biased(-2).score(5), 3);
    }
}

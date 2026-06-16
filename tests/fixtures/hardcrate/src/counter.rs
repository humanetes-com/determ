//! Baseline impl-block fill path (like the microcrate's `Counter`) — fills first
//! try, so it contributes to the rising pass-count series the ratchet/plateau
//! audits read, and exercises the per-impl fill route alongside the per-fn traps.

/// A monotonically increasing counter.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Counter {
    value: u32,
}

impl Counter {
    /// A counter starting at `start`.
    pub fn new(start: u32) -> Self {
        Counter { value: start }
    }

    /// Current value.
    pub fn get(&self) -> u32 {
        self.value
    }

    /// Increment and return the new value.
    pub fn bump(&mut self) -> u32 {
        self.value += 1;
        self.value
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counter_bumps() {
        let mut c = Counter::new(0);
        assert_eq!(c.get(), 0);
        assert_eq!(c.bump(), 1);
        assert_eq!(c.bump(), 2);
        assert_eq!(c.get(), 2);
    }
}

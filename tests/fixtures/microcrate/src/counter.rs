//! A monotonically increasing counter — the impl-block fill path, plus a
//! capability-gated extension impl (same file changes API surface per
//! cluster, like anyhow's context.rs under `error_generic_member_access`).

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

/// Capability-gated extension: bump that saturates at a cap.
#[cfg(any(extra_cap, feature = "extra_cap"))]
impl Counter {
    /// Increment, saturating at `max`; returns the new value.
    pub fn bump_capped(&mut self, max: u32) -> u32 {
        self.value = self.value.saturating_add(1).min(max);
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

    #[cfg(any(extra_cap, feature = "extra_cap"))]
    #[test]
    fn bump_saturates_at_cap() {
        let mut c = Counter::new(2);
        assert_eq!(c.bump_capped(3), 3);
        assert_eq!(c.bump_capped(3), 3);
    }
}

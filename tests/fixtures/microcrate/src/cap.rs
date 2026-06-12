//! Capability extension module — only compiled under `--cfg extra_cap` /
//! `--features extra_cap` (the second compiler cluster). The whole file is
//! gated at its `mod` declaration in lib.rs, mirroring anyhow's nightly.rs:
//! under the default cluster this file is skipped entirely.

use crate::capacity_hint;

/// Add, clamped to the capability buffer limit.
pub fn clamp_add(a: usize, b: usize) -> usize {
    (a + b).min(capacity_hint())
}

/// Outcome of a clamped addition.
#[derive(Debug, PartialEq, Eq)]
pub struct CapReport {
    pub value: usize,
    pub clamped: bool,
}

impl core::fmt::Display for CapReport {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(f, "{}{}", self.value, if self.clamped { " (clamped)" } else { "" })
    }
}

/// [`clamp_add`] with a report of whether clamping occurred.
pub fn clamp_add_report(a: usize, b: usize) -> CapReport {
    let raw = a + b;
    let value = clamp_add(a, b);
    CapReport { value, clamped: raw > value }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamps_to_hint() {
        assert_eq!(clamp_add(60, 10), 64);
        assert_eq!(clamp_add(1, 2), 3);
    }

    #[test]
    fn report_marks_clamp() {
        let r = clamp_add_report(1, 2);
        assert_eq!(r.value, 3);
        assert!(!r.clamped);
        assert_eq!(r.to_string(), "3");
        let c = clamp_add_report(60, 10);
        assert!(c.clamped);
        assert_eq!(c.to_string(), "64 (clamped)");
    }
}

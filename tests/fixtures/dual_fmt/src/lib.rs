use std::fmt;

pub struct Widget {
    pub label: String,
}

impl fmt::Display for Widget {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.label)
    }
}

impl fmt::Debug for Widget {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Widget({:?})", self.label)
    }
}

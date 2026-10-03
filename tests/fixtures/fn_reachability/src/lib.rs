//! Fixture for `domain.fn_reachability`: one case per claim of the oracle, each
//! with the negative control that keeps the claim honest. `expected.json`
//! beside this crate states which functions the oracle must prove unreachable
//! (`flagged`) and which it must leave testable (`kept`), and why.
#![allow(dead_code, private_interfaces, private_bounds)]

use core::fmt;
use std::error::Error;

// --- A. a private Copy type whose Clone nothing calls: PROVED ---------------
struct Token(u32);
impl Copy for Token {}
impl Clone for Token {
    fn clone(&self) -> Self {
        *self
    }
}
impl Token {
    // A. an inherent method nothing calls, on an unreachable type: PROVED.
    pub fn never_called(&self) -> u32 {
        self.0
    }
}
pub fn bump(x: u32) -> u32 {
    let t = Token(x);
    t.0 + 1
}

// --- B. the same shape with ONE `.clone()` call: KEPT (call edge) -----------
struct Called(u32);
impl Copy for Called {}
impl Clone for Called {
    fn clone(&self) -> Self {
        *self
    }
}
pub fn dup(x: u32) -> u32 {
    let c = Called(x);
    let d = c.clone();
    d.0
}

// --- C. Debug/Display/Error reached only through `Box<dyn Error>`: KEPT -----
struct Inner;
impl fmt::Debug for Inner {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Inner")
    }
}
impl fmt::Display for Inner {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("inner")
    }
}
impl Error for Inner {}
pub fn fail() -> Box<dyn Error> {
    Box::new(Inner)
}

// --- D. a dead private fn: PROVED; a private fn with a caller: KEPT ---------
fn dead_helper() -> u32 {
    7
}
fn live_helper() -> u32 {
    9
}
pub fn uses_live() -> u32 {
    live_helper()
}

// --- E. a #[doc(hidden)] pub fn: in the public index, KEPT ------------------
#[doc(hidden)]
pub fn hidden_api() -> u32 {
    1
}

// --- F. a private fn named only in an exported macro body: KEPT -------------
fn macro_named() -> u32 {
    2
}
#[macro_export]
macro_rules! through_macro {
    () => {
        macro_named()
    };
}

// --- G. a supertrait vtable: Debug for Sq is run through `dyn Shape`: KEPT --
trait Shape: fmt::Debug {
    fn area(&self) -> u32;
}
struct Sq(u32);
impl fmt::Debug for Sq {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Sq({})", self.0)
    }
}
impl Shape for Sq {
    fn area(&self) -> u32 {
        self.0 * self.0
    }
}
pub fn shapes() -> Vec<Box<dyn Shape>> {
    vec![Box::new(Sq(2))]
}
pub trait Named {
    fn name(&self) -> String;
}
impl Named for Box<dyn Shape> {
    fn name(&self) -> String {
        format!("{:?}", self)
    }
}

// --- H. a provided method runs the required one: write_str is KEPT ----------
struct Sink(String);
impl fmt::Write for Sink {
    fn write_str(&mut self, s: &str) -> fmt::Result {
        self.0.push_str(s);
        Ok(())
    }
}
pub fn render(x: u32) -> String {
    use fmt::Write;
    let mut s = Sink(String::new());
    write!(s, "{}", x).unwrap();
    s.0
}

// --- I. the type flows into generic code that clones it: KEPT ---------------
struct Boxed(u32);
impl Clone for Boxed {
    fn clone(&self) -> Self {
        Boxed(self.0)
    }
}
fn dup_generic<T: Clone>(t: &T) -> T {
    t.clone()
}
pub fn use_generic(x: u32) -> u32 {
    let b = Boxed(x);
    dup_generic(&b).0
}

// --- J. a private type a PUBLIC signature hands out: Clone is KEPT -----------
// (a test cannot name `Leaked`, but `leaked().clone()` compiles)
struct Leaked(u32);
impl Clone for Leaked {
    fn clone(&self) -> Self {
        Leaked(self.0)
    }
}
pub fn leaked() -> Leaked {
    Leaked(1)
}

// --- K. Drop is never proved: the call is the compiler's, at scope exit -----
struct Guard;
impl Drop for Guard {
    fn drop(&mut self) {}
}
pub fn guarded() {
    let _g = Guard;
}

// --- L. a public method on a public type: in the index, KEPT ----------------
pub struct Api(pub u32);
impl Api {
    pub fn value(&self) -> u32 {
        self.0
    }
    // L. a private method nothing calls on a public type: PROVED.
    fn private_unused(&self) -> u32 {
        self.0
    }
}

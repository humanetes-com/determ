"""Built-in signature cards for well-known UNSTABLE std APIs.

The `type_def` pull channel answers from the crate's own baked type manifest,
so a request for a *standard-library* type (e.g. `std::error::Request` behind
the nightly `error_generic_member_access` feature) came back "unavailable" —
the model then guessed at the API and burned its whole fix loop on E0433/E0599
errors it could never resolve. These are public, stable-in-shape std-lib
signatures (not project source), served as a fallback when no crate record
matches. Concrete usage examples included: the model acts on examples, not
prose.
"""

from __future__ import annotations

_REQUEST_CARD = """// std::error::Request — nightly-only, feature(error_generic_member_access).
// The lib.rs (or the fn's file) must have: #![feature(error_generic_member_access)]
pub struct Request<'a>(/* private */);

impl<'a> Request<'a> {
    pub fn provide_value<T: 'static>(&mut self, value: T) -> &mut Self;
    pub fn provide_ref<T: ?Sized + 'static>(&mut self, value: &'a T) -> &mut Self;
    pub fn would_be_satisfied_by_value_of<T: 'static>(&self) -> bool;
    pub fn would_be_satisfied_by_ref_of<T: ?Sized + 'static>(&self) -> bool;
}

// The Error-trait hook that receives it:
//   fn provide<'a>(&'a self, request: &mut Request<'a>);
// EXAMPLE — providing a backtrace from an error type:
//   impl std::error::Error for MyError {
//       fn provide<'a>(&'a self, request: &mut std::error::Request<'a>) {
//           request.provide_ref::<std::backtrace::Backtrace>(&self.backtrace);
//       }
//   }
// WRONG: request.provide(&self.backtrace)          // no such method
// WRONG: Request::new(...)                          // not constructible by user code
// RIGHT: request.provide_ref::<Backtrace>(&self.backtrace)
"""

_REQUEST_FREE_FNS_CARD = """// std::error — free fns for CONSUMING provided data (nightly,
// feature(error_generic_member_access)):
pub fn request_ref<'a, T>(err: &'a (impl std::error::Error + ?Sized)) -> Option<&'a T>
where T: 'static + ?Sized;
pub fn request_value<'a, T>(err: &'a (impl std::error::Error + ?Sized)) -> Option<T>
where T: 'static;

// EXAMPLE — asking an error for its backtrace:
//   let bt: Option<&std::backtrace::Backtrace> =
//       std::error::request_ref::<std::backtrace::Backtrace>(&*err);
// WRONG: err.request_ref::<Backtrace>()   // free fn, not a method on the error
// RIGHT: std::error::request_ref::<Backtrace>(&err)
"""

# Keyed by the identifier a model plausibly asks for. Multiple keys may map
# to one card.
_STD_API_CARDS: dict[str, str] = {
    "Request": _REQUEST_CARD,
    "request_ref": _REQUEST_FREE_FNS_CARD,
    "request_value": _REQUEST_FREE_FNS_CARD,
    "provide": _REQUEST_CARD,
}


def std_api_def(name: str) -> str:
    """Signature card for a well-known unstable std API, or "" when the name
    isn't one. Callers use this as the LAST fallback after the crate's own
    type manifest — a crate-local type of the same name always wins."""
    return _STD_API_CARDS.get(name.strip(), "")

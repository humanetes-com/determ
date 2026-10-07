//! Fixture for `domain.fn_routes`: which public calls reach a function no test
//! can name, read from the crate's MIR. It mirrors the three shapes anyhow
//! hides its error objects behind, each with a control. `expected/routes.json`
//! states the route each target must get (or that it gets none).
#![allow(dead_code, private_interfaces, private_bounds, clippy::all)]

use core::fmt;
use std::error::Error as StdError;

// --- A. a pub type in a PRIVATE module, built only inside a crate fn that one
// public trait impl on a foreign type calls. Its Debug is dispatched by the
// crate's own `dyn Error` Debug call, which runs only under `{:#?}`.
mod wrapper {
    use core::fmt;

    pub struct DisplayError<M>(pub M);

    impl<M: fmt::Display> fmt::Debug for DisplayError<M> {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            fmt::Display::fmt(&self.0, f)
        }
    }

    impl<M: fmt::Display> fmt::Display for DisplayError<M> {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            fmt::Display::fmt(&self.0, f)
        }
    }

    impl<M: fmt::Display> std::error::Error for DisplayError<M> {}

    // A. control: pub in the same private module, with a Debug impl, and
    // never built anywhere. No route.
    pub struct Unbuilt(pub u8);

    impl fmt::Debug for Unbuilt {
        fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
            write!(f, "Unbuilt({})", self.0)
        }
    }
}

use wrapper::DisplayError;

// --- B. a private object behind a fn-pointer vtable. `ErrorImpl<E>` is
// coerced to `Box<dyn Error>` only inside `object_boxed`, which nothing calls
// by name: the vtable's field is read and called in `Error::into_boxed`,
// which the public `From<Error> for Box<dyn Error + Send + Sync>` calls.
struct VTable {
    object_ref: unsafe fn(*const ()) -> *const (dyn StdError + Send + Sync),
    object_boxed: unsafe fn(*mut ()) -> Box<dyn StdError + Send + Sync>,
}

struct ErrorImpl<E> {
    vtable: &'static VTable,
    object: E,
}

impl<E: fmt::Debug> fmt::Debug for ErrorImpl<E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(&self.object, f)
    }
}

impl<E: fmt::Display> fmt::Display for ErrorImpl<E> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.object, f)
    }
}

impl<E: StdError> StdError for ErrorImpl<E> {}

unsafe fn object_ref<E: StdError + Send + Sync + 'static>(p: *const ()) -> *const (dyn StdError + Send + Sync) {
    let unerased = p as *const ErrorImpl<E>;
    let inner: *const E = core::ptr::addr_of!((*unerased).object);
    inner
}

unsafe fn object_boxed<E: StdError + Send + Sync + 'static>(p: *mut ()) -> Box<dyn StdError + Send + Sync> {
    let unerased: Box<ErrorImpl<E>> = Box::from_raw(p as *mut ErrorImpl<E>);
    unerased
}

pub struct Error {
    ptr: *mut (),
    vtable: &'static VTable,
}

impl Error {
    fn construct<E: StdError + Send + Sync + 'static>(object: E) -> Error {
        let vtable: &'static VTable = &VTable {
            object_ref: object_ref::<E>,
            object_boxed: object_boxed::<E>,
        };
        let inner = Box::new(ErrorImpl { vtable, object });
        Error { ptr: Box::into_raw(inner) as *mut (), vtable }
    }

    pub(crate) fn from_display<M: fmt::Display + Send + Sync + 'static>(message: M) -> Error {
        let error = DisplayError(message);
        Error::construct(error)
    }

    fn object(&self) -> &(dyn StdError + Send + Sync) {
        unsafe { &*(self.vtable.object_ref)(self.ptr as *const ()) }
    }

    fn into_boxed(self) -> Box<dyn StdError + Send + Sync> {
        unsafe { (self.vtable.object_boxed)(self.ptr) }
    }

    // C. control: a private inherent method nothing calls. No route.
    fn never_called(&self) -> u8 {
        0
    }
}

// The crate's own `dyn Error` Debug dispatch, on the alternate branch only.
impl fmt::Debug for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let object = self.object();
        if f.alternate() {
            return fmt::Debug::fmt(object, f);
        }
        write!(f, "{}", object)
    }
}

impl From<Error> for Box<dyn StdError + Send + Sync> {
    fn from(error: Error) -> Self {
        error.into_boxed()
    }
}

pub trait Context<T> {
    fn context<C: fmt::Display + Send + Sync + 'static>(self, context: C) -> Result<T, Error>;
}

impl<T> Context<T> for Option<T> {
    fn context<C: fmt::Display + Send + Sync + 'static>(self, context: C) -> Result<T, Error> {
        match self {
            Some(ok) => Ok(ok),
            None => Err(Error::from_display(context)),
        }
    }
}

// --- C. a private free fn reached through a private one from a public fn.
fn leaf() -> u32 {
    3
}

fn middle() -> u32 {
    leaf() + 1
}

pub fn entry() -> u32 {
    middle()
}

// --- D. a private fn called only inside a closure a public fn creates: the
// closure is its own MIR item, and its calls count as its creator's.
fn in_closure() -> u32 {
    5
}

pub fn lazy(value: Option<u32>) -> u32 {
    value.unwrap_or_else(|| in_closure())
}

"""Run `95878-aaf5bf1d` iter 1's escalated cohort, the records the producer
facts are read from (shared fixture, not a test module)."""
from __future__ import annotations

# The stable cluster's escalated cohort as run `95878-aaf5bf1d` iter 1 ordered
# it (`order_frontier` on the baked leaf counts; the `attempted` batch on the
# floor was the first twelve). Bodies are the live tree's, abbreviated to the
# construction and call facts the ranking reads.
_COHORT_ORDER = [
    ("src/context.rs", "context"), ("src/error.rs", "context"),
    ("src/error.rs", "msg"), ("src/error.rs", "new"), ("src/kind.rs", "new"),
    ("src/error.rs", "downcast"), ("src/error.rs", "from"),
    ("src/lib.rs", "must_use"), ("src/error.rs", "from_boxed"),
    ("src/context.rs", "with_context"), ("src/ensure.rs", "__dispatch_ensure"),
    ("src/error.rs", "deref"), ("src/lib.rs", "format_err"),
    ("src/context.rs", "ext_context"), ("src/error.rs", "deref_mut"),
    ("src/ensure.rs", "render"), ("src/error.rs", "construct"),
    ("src/error.rs", "construct_from_adhoc"), ("src/error.rs", "construct_from_boxed"),
    ("src/error.rs", "construct_from_context"), ("src/error.rs", "construct_from_display"),
    ("src/error.rs", "construct_from_std"), ("src/lib.rs", "Ok"),
    ("src/error.rs", "error"), ("src/error.rs", "is"),
    ("src/error.rs", "downcast_ref"), ("src/error.rs", "erase"),
]
_NOT_ESCALATED = {("src/lib.rs", "Ok"), ("src/error.rs", "error"),
                  ("src/error.rs", "is"), ("src/error.rs", "downcast_ref")}
def _table(t: str) -> str:
    return ("let vtable = Box::leak(Box::new(ErrorVTable { object_drop: object_drop::<T>, "
            "object_downcast: object_downcast::<T> }));").replace("T>", t + ">")
_COHORT_RECS = [
    ("src/context.rs", "context", "impl<T, E> Context<T, E> for Result<T, E>",
     "match self { Ok(v) => Ok(v), Err(e) => Err(e.ext_context(context)) }"),
    ("src/context.rs", "context", "impl<T> Context<T, Infallible> for Option<T>",
     "match self { Some(v) => Ok(v), None => Err(crate::Error::construct_from_display(context, backtrace!())) }"),
    ("src/error.rs", "context", "impl Error",
     _table("ContextError<C, Error>") + " unsafe { Self::construct(ContextError { context, error: self }, vtable, backtrace) }"),
    ("src/error.rs", "msg", "impl Error", "Self::construct_from_adhoc(message, backtrace!())"),
    ("src/error.rs", "new", "impl Error", "let bt = backtrace_if_absent!(&error); Self::construct_from_std(error, bt)"),
    ("src/kind.rs", "new", "impl Adhoc", "crate::Error::msg(message)"),
    ("src/kind.rs", "new", "impl Trait", "error.into()"),
    ("src/kind.rs", "new", "impl Boxed", "crate::Error::from_boxed(error)"),
    ("src/error.rs", "downcast", "impl Error", "let target = TypeId::of::<E>(); unsafe { let inner = self.inner.by_mut(); (vtable(inner.ptr).object_downcast)(inner.by_ref(), target) }"),
    ("src/error.rs", "from", "impl<E> From<E> for Error", "let bt = backtrace_if_absent!(&error); Self::construct_from_std(error, bt)"),
    ("src/lib.rs", "must_use", "", "error"),
    ("src/error.rs", "from_boxed", "impl Error", "let bt = backtrace_if_absent!(&*boxed_error); Self::construct_from_boxed(boxed_error, bt)"),
    ("src/context.rs", "with_context", "impl<T, E> Context<T, E> for Result<T, E>",
     "match self { Ok(v) => Ok(v), Err(e) => Err(e.ext_context(context())) }"),
    ("src/ensure.rs", "__dispatch_ensure", "impl<A, B> BothDebug for (A, B)",
     "let mut sb = SmallBuf { buf: [0; 128], pos: 0 }; let owned = alloc::string::String::from(s); crate::Error::msg(owned)"),
    ("src/ensure.rs", "__dispatch_ensure", "impl<A, B> NotBothDebug for &(A, B)", "crate::Error::msg(msg)"),
    ("src/error.rs", "deref", "impl Deref for Error", "unsafe { ErrorImpl::error(self.inner.by_ref()) }"),
    ("src/lib.rs", "format_err", "", "if let Some(s) = args.as_str() { crate::Error::msg(s) } else { crate::Error::msg(s) }"),
    ("src/context.rs", "ext_context", "impl<E> StdError for E", "let bt = backtrace_if_absent!(self); crate::Error::construct_from_context(context, self, bt)"),
    ("src/context.rs", "ext_context", "impl StdError for Error", "self.context(context)"),
    ("src/error.rs", "deref_mut", "impl DerefMut for Error", "unsafe { ErrorImpl::error_mut(self.inner.by_mut()) }"),
    ("src/ensure.rs", "render", "", "let mut buf = Buf::new(); let owned = alloc::string::String::from(s); crate::Error::msg(owned)"),
    ("src/error.rs", "construct", "impl Error",
     "let err_impl = ErrorImpl { vtable, backtrace, value: error }; let boxed = Box::new(err_impl); let inner = Own::new(boxed).cast::<ErrorImpl>(); Self { inner, ptr: PhantomData }"),
    ("src/error.rs", "construct_from_adhoc", "impl Error", "Self::construct_from_std(crate::wrapper::MessageError(message), backtrace)"),
    ("src/error.rs", "construct_from_boxed", "impl Error",
     _table("BoxedError") + " unsafe { Self::construct(crate::error::BoxedError(error), vtable, backtrace) }"),
    ("src/error.rs", "construct_from_context", "impl Error",
     _table("ContextError<C, E>") + " unsafe { Self::construct(ContextError { context, error }, vtable, backtrace) }"),
    ("src/error.rs", "construct_from_display", "impl Error", "Self::construct_from_std(crate::wrapper::DisplayError(message), backtrace)"),
    ("src/error.rs", "construct_from_std", "impl Error", _table("E") + " unsafe { Self::construct(error, vtable, backtrace) }"),
    ("src/lib.rs", "Ok", "", "Result::Ok(value)"),
    ("src/error.rs", "error", "impl ErrorImpl", "unsafe { &*(vtable(this.ptr).object_ref)(this).ptr }"),
    ("src/error.rs", "is", "impl Error", "self.downcast_ref::<E>().is_some()"),
    ("src/error.rs", "downcast_ref", "impl Error", "unsafe { let addr = (vtable(self.inner.ptr).object_downcast)(self.inner.by_ref(), target)?; Some(&*addr.cast::<E>().ptr.as_ptr()) }"),
    ("src/error.rs", "erase", "impl ErrorImpl", "Ref::new(self).cast::<ErrorImpl>()"),
]
_SIX = {("src/error.rs", n) for n in (
    "construct", "construct_from_adhoc", "construct_from_boxed",
    "construct_from_context", "construct_from_display", "construct_from_std")}



#![allow(clippy::all, dead_code, non_snake_case, unreachable_patterns, unsafe_op_in_unsafe_fn, unused, unused_imports, unused_mut)]
#![allow(clippy::all, dead_code, non_snake_case, unreachable_patterns, unsafe_op_in_unsafe_fn, unused, unused_imports, unused_mut)]
#![feature(error_generic_member_access)]
use anyhow::*;
use std::fmt::{self, Display, Debug};
use std::string::{String, ToString};
use std::vec::Vec;
use std::boxed::Box;
use std::format;
use std::error::Error as StdError;

use anyhow::Context;

#[test]
fn context_error_c_error_provide_is_reached() {
    let base = anyhow::Error::msg("base error");
    let err = base.context("outer context");
    let bt = std::error::request_ref::<std::backtrace::Backtrace>(&*err);
    assert!(
        bt.is_some(),
        "ContextError<C, Error>::provide should forward the provide request to the inner error"
    );
}

#[test]
fn context_error_c_e_provide_is_reached() {
    let res: std::result::Result<(), std::io::Error> =
        Err(std::io::Error::other("inner io error"));
    let err = res.context("result context").unwrap_err();
    let bt = std::error::request_ref::<std::backtrace::Backtrace>(&*err);
    assert!(
        bt.is_some(),
        "ContextError<C, E>::provide should forward the provide request to the inner error"
    );
}

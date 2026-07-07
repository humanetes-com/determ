//! `hcm_core` — the SCALE + multi-crate fixture's dependency root.
//! See `tools/hardcrate_multi_gen.py` for how this file is generated.
#![cfg_attr(hcm_nightly, feature(error_generic_member_access))]

mod wrap_sum_1;
mod clamp_add_1;
mod hex_codec_1;
mod bit_ops_1;
mod wrap_sum_2;
mod clamp_add_2;
mod hex_codec_2;
mod bit_ops_2;
mod wrap_sum_3;
mod clamp_add_3;
mod hex_codec_3;
mod bit_ops_3;
mod wrap_sum_4;
mod clamp_add_4;
mod hex_codec_4;
mod bit_ops_4;
mod wrap_sum_5;
mod clamp_add_5;
mod scoring;
#[cfg(hcm_nightly)]
mod nightly_provide;

pub use wrap_sum_1::*;
pub use clamp_add_1::*;
pub use hex_codec_1::*;
pub use bit_ops_1::*;
pub use wrap_sum_2::*;
pub use clamp_add_2::*;
pub use hex_codec_2::*;
pub use bit_ops_2::*;
pub use wrap_sum_3::*;
pub use clamp_add_3::*;
pub use hex_codec_3::*;
pub use bit_ops_3::*;
pub use wrap_sum_4::*;
pub use clamp_add_4::*;
pub use hex_codec_4::*;
pub use bit_ops_4::*;
pub use wrap_sum_5::*;
pub use clamp_add_5::*;
pub use scoring::*;
#[cfg(hcm_nightly)]
pub use nightly_provide::*;

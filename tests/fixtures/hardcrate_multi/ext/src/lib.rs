//! `hcm_ext` — depends on `hcm_core` across a REAL crate boundary.
//! See `tools/hardcrate_multi_gen.py` for how this file is generated.

mod consumers_1;
mod consumers_2;
mod consumers_3;
mod consumers_4;
mod consumers_5;
mod consumers_6;
mod consumers_7;
mod consumers_8;
mod consumers_9;
mod consumers_10;

pub use consumers_1::*;
pub use consumers_2::*;
pub use consumers_3::*;
pub use consumers_4::*;
pub use consumers_5::*;
pub use consumers_6::*;
pub use consumers_7::*;
pub use consumers_8::*;
pub use consumers_9::*;
pub use consumers_10::*;

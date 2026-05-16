# Project Profile for Argh

## 1. Primary Language and Version
- **Language**: Rust  
- **Version**: Not specified in provided artifacts (inferred from Cargo-like structure and code examples)

## 2. Build System and Test Runner
- **Build System**: Cargo  
- **Test Runner**: `cargo test` (standard Rust testing)  
  - Includes integration tests in `<crate>/tests/` and UI-style compile-fail tests with `.stderr` expectations.

## 3. Project Layout
- **Type**: Monorepo workspace with three top-level Rust crates:  
  | Module | Path | Type | Notes |  
  |--------|------|------|-------|  
  | `argh` | `argh/` | Library + Binary | Core argument parser logic. Contains examples in `argh/examples/`. |  
  | `argh_complete` | `argh_complete/` | Library | Shell completion utilities. |  
  | `argh_derive` | `argh_derive/` | Library (proc-macro) | Derive macro implementation for argument parsing annotations. |  

## 4. Minimum Required Files per Crate
- **All Crates**:  
  - `Cargo.toml` with `[lib]` section (for `argh_derive`, `proc-macro = true` is required).  
  - `src/lib.rs` (main library code).  
- **argh**:  
  - `src/main.rs` (if binary is included, though not directly evident from file paths; examples are in `examples/`).  
- **argh_derive**:  
  - Must declare `proc-macro` crate type in `Cargo.toml`.  

## 5. Structural Conventions
- **Test Files**:  
  - Integration/regression tests in `<crate>/tests/` (e.g., `argh/tests/args_info_tests.rs`).  
  - UI/compile-fail tests in `<crate>/tests/ui/` (e.g., `argh/tests/ui/bad-long-names/bad-long-names.rs` with `.stderr` expected output).  
- **Source Files**:  
  - Core code in `<crate>/src/` (e.g., `argh/src/lib.rs`).  
- **Macro Derive**:  
  - `argh_derive` contains the `#[derive(FromArgs)]` implementation.  
- **Examples**:  
  - `argh/examples/` holds code samples used for documentation and macro expansion demonstrations (e.g., `simple_example.rs`).
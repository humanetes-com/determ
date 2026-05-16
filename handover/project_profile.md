# Project Profile

## Primary Language and Version
- **Primary Language:** Rust
- **Version Not Specified**

## Build System and Test Runner
- **Build System**: Cargo
- **Test Runner**: cargo-nextest || rust's built-in test runner (run `cargo test`)

## Project Layout
This project consists of a library (`argh`) and its dependencies. It maintains the typical monorepo structure for Rust projects:

### Modules/Crates/Packages

1. **Library Crate:**
   - **Path:** `argh`
   - **Type:** Library
   - **Required Files:** 
     - `Cargo.toml` with `[lib]` section
     - `src/lib.rs`

2. **Completed Macro for the Library:**
   - **Path:** `argh_complete`
   - **Type:** Library (likely not used on its own)
   - **Required Files:** 
     - `Cargo.toml`
     - `src/lib.rs`

3. **Derive Crate that Extends Functionality:**
   - **Path:** `argh_derive`
   - **Type:** Library
   - **Required Files:** 
     - `Cargo.toml` with `[lib]` section
     - `src/lib.rs`

## Minimum Required Files for Each Module/Crate

- For a Rust library, a minimal starting point would be:
  ```toml
  # Cargo.toml
  [package]
  name = "argh"
  version = "0.1.0"
  edition = "2018"

  [lib]

  # src/lib.rs with some basic derive syntax to get started
  ```

## Structural Conventions Visible from Test Paths

- **Test Files:** Live in `<crate>/tests/`
  - Example: All tests for `lib` crate would be inside `argh/tests/*.rs`.

- **Source Code (Library Code):** Typically found under `<crate>/src/`
  - Example: Library logic would be under `argh/src/lib.rs`

This detailed profile ensures that the project is structured correctly, with clear separation of concerns and adherence to Rust crate conventions.
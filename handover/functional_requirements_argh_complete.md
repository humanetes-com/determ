# Functional Requirements: argh_complete

This crate generates shell completion scripts for argh-based CLIs.

## Imports (all files use these)
- `use argh_shared::{CommandInfoWithArgs, FlagInfo, FlagInfoKind, Optionality, SubCommandInfo};`
- `use std::fmt::Write;`  ← required for `write!(&mut String, ...)` to compile

## lib.rs — crate root

```rust
pub mod bash;
pub mod fish;
pub mod nushell;
pub mod zsh;

use argh_shared::CommandInfoWithArgs;

pub trait Generator {
    fn generate(cmd_name: &str, cmd: &CommandInfoWithArgs<'_>) -> String;
}

#[cfg(test)]
mod tests;
```

## bash.rs — Bash completion generator

Struct `Bash` implements `Generator`. Output includes:
- `cmd="<cmd_name>_<subcmd>"` for subcommands
- `opts="--verbose -v"` for flags
- `cmds="help test"` for subcommand names

## zsh.rs — Zsh completion generator

Struct `Zsh` implements `Generator`. Output includes:
- `#compdef <cmd_name>`
- `'*::command:->subcmd'` for subcommand dispatch
- `(-v --verbose)` style flag completions

## fish.rs — Fish completion generator

Struct `Fish` implements `Generator`. Output includes:
- `function __fish_<cmd_name>_using_command`
- `complete -c <cmd_name> -n '__fish_...' -f -l <long> -s <short> -d '<desc>'`

## nushell.rs — Nushell completion generator

Struct `Nushell` implements `Generator`. Output includes:
- `export extern "<cmd_name>" [` for each command
- `--verbose(-v) # verbose output` for flag completions

## Critical rules
- Do NOT use type `Command` — the correct type is `CommandInfoWithArgs<'_>` from argh_shared.
- `write!` into `&mut String` requires `use std::fmt::Write;` at the top of each file.
- Helper functions are private (no `pub`).
- lib.rs MUST end with `#[cfg(test)] mod tests;` to include tests.rs.

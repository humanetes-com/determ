# Functional Requirements for argh_complete

## Bash Generator

`crate::bash::Bash::generate(cmd_name: &str, cmd: &Command) -> String`

Output must contain:
- `cmd="mycmd_subcmd"`
- `opts="--verbose -v"`
- `cmds="help test"`

## Zsh Generator

`crate::zsh::Zsh::generate(cmd_name: &str, cmd: &Command) -> String`

Output must contain:
- `#compdef mycmd`
- "'*::command:->subcmd'"`
- `(-v --verbose)` 

## Fish Generator

`crate::fish::Fish::generate(cmd_name: &str, cmd: &Command) -> String`

Output must contain:
- `function __fish_mycmd_using_command`
- `complete -c mycmd -n '__fish_mycmd_using_command' -f -l verbose -s v -d 'verbose output'`
- `complete -c mycmd -n '__fish_mycmd_using_command' -f -a 'subcmd' -d 'a sub command'`

## Nushell Generator

`crate::nushell::Nushell::generate(cmd_name: &str, cmd: &Command) -> String`

Output must contain:
- `export extern "mycmd" [`
- `--verbose(-v) # verbose output`
- `export extern "mycmd subcmd" [`
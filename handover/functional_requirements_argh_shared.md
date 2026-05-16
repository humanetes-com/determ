```markdown
# Functional Requirements

## Module: argh

### Function: `from_env`
```rust
fn from_env<T>() -> Result<T, env::VarError>
```
- **Module Path**: `argh::from_env`
- **Description**: Parses the environment variables to create an instance of type `T`.
- **Parameters**: None.
- **Return Type**: 
  - Success: An instance of `T`.
  - Failure: A `Result<T, env::VarError>` where `env::VarError` is an error from the standard library representing a failure to retrieve an environment variable.

### Struct: `CommandInfo`
```rust
struct CommandInfo<'a> {
    name: &'static str,
    short: char,
    description: &'a str,
}
```
- **Fields**:
  - `name`: A static string slice (`&'static str`) representing the command name.
  - `short`: A character indicating a shorthand for the command.
  - `description`: A string slice reference (`&'a str`) providing a description of the command.

### Function: `write_description`
```rust
fn write_description(out: &mut String, cmd: &CommandInfo<'_>) {
    let mut current_line = INDENT.to_string();
    current_line.push_str(cmd.name);

    if *cmd.short != '\0' {
        current_line.push_str(&format!("  {}", cmd.short));
    }

    if cmd.description.is_empty() {
        new_line(&mut current_line, out);
        return;
    }

    if !indent_description(&mut current_line) {
        // Start the description on a new line if the flag names already
        // add up to more than DESCRIPTION_INDENT.
        new_line(&mut current_line, out);
    }

    let mut words = cmd.description.split(' ').peekable();
    while let Some(first_word) = words.next() {
        indent_description(&mut current_line);
        current_line.push_str(first_word);

        'inner: while let Some(&word) = words.peek() {
            if (char_len(&current_line) + char_len(word) + 1) > WRAP_WIDTH {
                new_line(&mut current_line, out);
                break 'inner;
            } else {
                // Advance the iterator
                let _ = words.next();
                current_line.push(' ');
                current_line.push_str(word);
            }
        }
    }
    new_line(&mut current_line, out);
}
```
- **Module Path**: `argh::write_description`
- **Description**: Writes the command name and description to an output string.
- **Parameters**:
  - `out`: A mutable reference to a string that accumulates the output.
  - `cmd`: A reference to a `CommandInfo` instance containing the command's details.
- **Return Type**: None. The function modifies the provided `out` parameter directly by appending formatted strings.

## Module: argh_derive

### Trait: `ArghParser`
```rust
trait ArghParser {
    type Args;
    fn get_args_info(&self) -> Option<Self::Args>;
}
```
- **Description**: Defines a trait for types that can be parsed into arguments.
- **Associated Type**:
  - `Self::Args`: A generic associated type representing the argument structure of the implementing type.
- **Methods**:
  - `get_args_info()`: Returns an optional reference to the argument structure if parsing is possible, otherwise `None`.

### Struct: `SubCommandInfo`
```rust
#[derive(Default, PartialEq, Eq, Clone)]
pub struct SubCommandInfo<'a> {
    pub name: &'a str,
    pub command: CommandInfoWithArgs<'a>,
}
```
- **Fields**:
  - `name`: A string slice (`&'a str`) representing the subcommand name.
  - `command`: An instance of `CommandInfoWithArgs` associated with this subcommand.

### Enum: `Optionality`
```rust
#[derive(Default, PartialEq, Eq)]
pub enum Optionality {
    Required,
    Optional,
    Repeating,
    Greedy,
}
```
- **Variants**:
  - `Required`: Indicates the argument is required exactly once.
  - `Optional`: Indicates the argument may or may not be present.
  - `Repeating`: Indicates the argument may appear zero or more times.
  - `Greedy`: Used for positional arguments which capture all command line input up to the next flag or the end of the input.

### Enum: `FlagInfoKind`
```rust
#[derive(Default, PartialEq, Eq)]
pub enum FlagInfoKind<'a> {
    Switch,
    Option { arg_name: &'a str },
}
```
- **Variants**:
  - `Switch`: Represents a boolean flag.
  - `Option { arg_name }`: Indicates the flag is associated with an argument value named `arg_name`.

### Struct: `FlagInfo`
```rust
#[derive(Default, PartialEq, Eq)]
pub struct FlagInfo<'a> {
    pub kind: FlagInfoKind<'a>,
    pub optionality: Optionality,
    pub long: &'a str,
    pub short: Option<char>,
    pub description: &'a str,
    pub hidden: bool,
}
```
- **Fields**:
  - `kind`: The kind of flag (`FlagInfoKind`).
  - `optionality`: Defines the requirements for the argument’s presence on the command line.
  - `long`: The long form string representation of the flag.
  - `short`: An optional character representing a shorthand for the flag.
  - `description`: A description string for the flag.
  - `hidden`: A boolean indicating whether this flag should be hidden from help outputs.
```markdown
```
These functional requirements document the API contracts for the functions, structs, enums, and traits explicitly called or asserted in the provided test module.
## Implementation note: private helpers required by write_description

The `write_description` function depends on these private constants and functions
that MUST be defined in lib.rs:

```rust
const INDENT: &str = "  ";
const DESCRIPTION_INDENT: usize = 20;
const WRAP_WIDTH: usize = 80;

fn char_len(s: &str) -> usize {
    s.chars().count()
}

fn new_line(current_line: &mut String, out: &mut String) {
    out.push('\n');
    out.push_str(current_line);
    current_line.truncate(0);
}

fn indent_description(line: &mut String) -> bool {
    let cur_len = char_len(line);
    if cur_len < DESCRIPTION_INDENT {
        let num_spaces = DESCRIPTION_INDENT - cur_len;
        line.extend(std::iter::repeat(' ').take(num_spaces));
        true
    } else {
        false
    }
}
```

Do NOT use `env::` — there is no env module in argh_shared.
Do NOT use `std::env` — it is not needed.

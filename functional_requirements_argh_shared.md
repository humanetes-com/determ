# Functional Requirements for Argh

## Modules and Components

### Module `argh`

#### Functions
- `pub fn from_env<T: TopLevelCommand>() -> T`
  - Dependencies: `std::env::args_os`
  - Returns: `T` or exits via `EarlyExit`

- `pub fn cargo_from_env<T: TopLevelCommand>() -> T`
  - Dependencies: `std::env::args`

#### Traits
- `pub trait FromArgs: Sized`
  ```rust
  fn from_args(command_name: &[&str], args: &[&str]) -> Result<Self, EarlyExit>
  fn redact_arg_values(_command_name: &[&str], _args: &[&str]) -> Result<Vec<String>, EarlyExit>
  ```

- `pub trait FromArgValue: Sized`
  ```rust
  fn from_arg_value(value: &str) -> Result<Self, String>
  impl<T> FromArgValue for T where T: FromStr, T::Err: std::fmt::Display
  ```

- `pub trait Flag`
  ```rust
  fn default() -> Self where Self: Sized
  fn set_flag(&mut self)
  impl Flag for bool
  impl Flag for Option<bool>
  impl Flag for numeric types [u8, u128, i8, i128, ...]
  ```

- `pub trait ParseValueSlot`
  ```rust
  fn fill_slot(&mut self, arg: &str, value: &str) -> Result<(), String>
  impl<T> ParseValueSlot for ParseValueSlotTy<Option<T>, T>
  impl<T> ParseValueSlot for ParseValueSlotTy<Vec<T>, T>
  ```

- `pub trait DynamicSubCommand`
  ```rust
  fn commands() -> &'static [&'static CommandInfo]
  fn try_redact_arg_values(&self, args: &[&str]) -> Result<Vec<String>, EarlyExit>
  fn try_from_args(args: &[&str]) -> Option<Result<Self, EarlyExit>>
  ```

#### Structs
- `#[derive(Debug, Clone, PartialEq, Eq)] pub struct EarlyExit`
  ```rust
  pub output: String
  pub status: Result<(), ()>
  impl From<String> for EarlyExit
  ```

- `pub struct MissingRequirements`
  ```rust
  pub options: Vec<&'static str>
  pub subcommands: Option<Vec<&'static CommandInfo>>
  pub positional_args: Vec<&'static str>
  pub fn missing_option(&mut self, name: &'static str)
  pub fn missing_subcommands(&mut self, commands: impl Iterator<Item = &'static CommandInfo>)
  pub fn missing_positional_arg(&mut self, name: &'static str)
  pub fn err_on_any(&self) -> Result<(), String>
  ```

- `pub struct CommandInfoWithArgs`
  ```rust
  pub name: &str
  pub short: &&str
  pub description: &str
  pub examples: &[&str]
  pub flags: &[FlagInfo]
  pub notes: &[&str]
  pub positionals: &[PositionalInfo]
  pub commands: Vec<SubCommandInfo>
  pub error_codes: &[ErrorCodeInfo]
  ```

#### Enums
- `pub enum FlagInfoKind`
  ```rust
  Switch
  Option { arg_name: &'a str }
  ```

- `pub enum Optionality`
  ```rust
  Required
  Optional
  Repeating
  Greedy
  ```

#### Subcommand and Positional Types
- `pub struct FlagInfo<'a>`
  ```rust
  pub kind: FlagInfoKind
  pub optionality: Optionality
  pub long: &'a str
  pub short: Option<char>
  pub description: &'a str
  pub hidden: bool
  ```

- `pub struct PositionalInfo`
  ```rust
  pub name: &str
  pub description: &str
  pub optionality: Optionality
  pub hidden: bool
  ```

- `pub struct SubCommandInfo`
  ```rust
  pub name: &str
  pub command: CommandInfoWithArgs
  ```

- `pub struct ErrorCodeInfo`
  ```rust
  pub code: i32
  pub description: &str
  ```

#### Invariants
- `assert_eq!(GoUp::from_args(&["cmdname"], &["--height", "5"]).unwrap().height, 5)`
- `argh::from_env()` must exit with correct `EarlyExit` on help/error
- `Field::hidden` disables help output for fields marked `#[argh(hidden_help)]`
- Positional arguments must be declared in order of appearance
- Only the last positional argument may be `Option<T>`, `Vec<T>`, or have a default
- Compile-fail: duplicate `#[argh(subcommand)]` enum variant names
- Compile-fail: non-ASCII `#[argh(long = "...")]` field names
- Compile-fail: duplicate `short/long` names in the same command struct

---

### Module `argh_derive`

#### Procedural Macro Functions
- `#[proc_macro_derive(FromArgs, attributes(argh))] pub fn argh_derive(...) -> TokenStream`
- `#[proc_macro_derive(FromArgValue, attributes(argh))] pub fn argh_value_derive(...) -> TokenStream`
- `#[proc_macro_derive(ArgsInfo, attributes(argh))] pub fn args_info_derive(...) -> TokenStream`

#### Structs
- `pub struct FieldAttrs`
  ```rust
  pub default: Option<syn::LitStr>
  pub description: Option<Description>
  pub from_str_fn: Option<syn::ExprPath>
  pub field_type: Option<FieldType>
  pub long: Option<syn::LitStr>
  pub short: Option<syn::LitChar>
  pub hidden_help: bool
  ```

- `pub struct TypeAttrs`
  ```rust
  pub is_subcommand: Option<syn::Ident>
  pub name: Option<syn::LitStr>
  pub description: Option<Description>
  pub examples: Vec<syn::LitStr>
  pub error_codes: Vec<(syn::LitInt, syn::LitStr)>
  pub check_error_codes(&self, errors: &Errors)
  ```

#### Helper Functions
- `pub(crate) fn check_long_name(errors: &Errors, spanned: &impl Spanned, value: &str)`
- Compile-time rule: `long` names must be ASCII lowercase/numeric/dashes
- Compile-time rule: `short` names must be unique per struct
- Compile-time rule: `greedy` positional must be the last field

---

### Module `argh_complete`

#### Generator
- `pub struct Generator`
  ```rust
  fn generate(command_name: &str, cmd_info: &CommandInfoWithArgs) -> String
  ```
- Outputs bash/zsh/fish/nushell shell completion scripts
- Contains assertions:
  - Bash: `bash_out.contains("opts=\"--verbose -v\"")`
  - Fish: `fish_out.contains("complete -c mycmd -l verbose -s v")`

---

### Error Handling

#### `argh::EarlyExit`
- Represents parse failure or help display with:
  - Human-readable CLI output in `.output`
  - Exit status in `.status` (Ok(()) for help, Err(()) for errors)

#### Compile-time Validation Errors
- `The long name of "--xxx" was already used here.` (duplicate long names)
- `The short name of "-f" was already used here.` (duplicate short names)
- `Only the last positional argument may be Option<Vec<T>>` (positional ordering)
- `Long names must be ASCII` (invalid character checks)
- `Invalid capital letters in long name` (style validation)

---

### Attributes in `#[derive(FromArgs)]`

#### Struct Field Attributes
- `#[argh(option)]` for optional args with `--name`/`-s`
- `#[argh(switch)]` for boolean flags like `--flag`
- `#[argh(positional)]` for ordered non-flag args
- `#[argh(positional, greedy)]` for trailing positional varargs
- `#[argh(default = "expr")]` for fallback values (marks field as optional)
- `#[argh(from_str_fn = "custom_parser")]` for custom parsing logic

#### Enum and Subcommand Attributes
- `#[argh(subcommand)]` on enum to denote subcommands
- `#[argh(subcommand, name = "alias", short = 'c')]` for subcommand aliases
- `#[argh(description = "...")]` for custom command description
- `#[argh(notes = "...")]` for usage notes
- `#[argh(example = "...")]` for example CLI invocations

---

### Subcommand Rules

#### Enum-Based Subcommand
- Must be marked `#[derive(FromArgs)]` and `#[argh(subcommand)]`
- Variants wrap structs also marked `#[derive(FromArgs)]`
- Example:
  ```rust
  #[derive(FromArgs)]
  #[argh(subcommand)]
  enum SubCommand {
      Start(StartArgs),
      Stop(StopArgs)
  }
  ```

#### Dynamic Subcommands
- Must implement `DynamicSubCommand` trait
- `commands()` returns associated command metadata
- `try_from_args()` must parse subcommand variant from raw args

---

### Help System

- Triggered by `--help` or `help` arguments
- Generated with:
  - Usage syntax
  - Options with descriptions in alphabetical order
  - Subcommand list with descriptions
  - Examples from `#[argh(example = "...")]`
- Conditional help message validation in `argh::help` feature
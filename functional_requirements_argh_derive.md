# Functional Requirements for argh_derive

## 1. Derive Macros Provided

### `#[derive(FromArgs)]`
- **Implemented trait**: `argh::FromArgs`
- **Trait methods**:
  - `fn from_args(__cmd_name: &[&str], __args: &[&str]) -> Result<Self, argh::EarlyExit>`
  - `fn redact_arg_values(__cmd_name: &[&str], __args: &[&str]) -> Result<Vec<String>, argh::EarlyExit>`
- **Behavior**:
  - Parses command-line arguments into struct fields annotated with `argh(...)` attributes.
  - Supports positional arguments, options, switches, subcommands, and default values.
  - Validates argument naming rules (long names must be lowercase/digits/dashes).

### `#[derive(FromArgValue)]`
- **Implemented trait**: `argh::FromArgValue`
- **Trait method**:
  - `fn from_arg_value(value: &str) -> Result<Self, String>`
- **Behavior**:
  - Enum-only derive for choice enums that map string values to enum variants.
  - Uses PascalCase→snake_case conversion unless `#[argh(name = "...")]` overrides variant names.

### `#[derive(ArgsInfo)]`
- **Implemented trait**: `argh::ArgsInfo`
- **Behavior**:
  - Generates metadata for help text (command description, subcommand info, flag documentation).

---

## 2. Helper Attributes

### Field Attributes (`#[argh(...)]` on struct fields):
| Attribute | Applies To | Behavior |
|-----------|------------|----------|
| `arg_name = "NAME"` | All fields | Overrides the display name for positional arguments |
| `default = "EXPR"` | `option`/`positional` | Sets default value if argument is missing |
| `description = "TEXT"` | All fields | Adds field description to help output |
| `from_str_fn = PATH` | `option`/`positional` | Custom parsing function for field type |
| `long = "NAME"` | `option`/`switch` | Sets long name for flag/option (instead of kebab-case field name) |
| `option` | Struct fields | Marks field as optional argument with `--key VALUE` syntax |
| `positional` | Struct fields | Marks field as positional argument (parsed in declaration order) |
| `short = 'C'` | `option`/`switch` | Sets short name for flag/option |
| `switch` | Struct fields | Boolean flag parsed as `--flag` (supports `Option<bool>`) |
| `subcommand` | Single struct field | Marks enum field for subcommand dispatch |
| `greedy` | `positional` | Last positional argument consumes remaining CLI tokens |
| `hidden_help` | All fields | Excludes field from help output |
| `usage` | All fields | Marks field for inclusion in usage string |

### Type Attributes (`#[argh(...)]` on struct/enum):
| Attribute | Behavior |
|-----------|----------|
| `description = "TEXT"` | Sets command description for help |
| `error_code = "CODE, MSG"` | Defines exit code and message for parsing errors |
| `example = "TEXT"` | Adds usage example to help |
| `name = "NAME"` | Sets command name for subcommands |
| `note = "TEXT"` | Adds footnote to help documentation |
| `short = 'C'` | Sets short name for subcommand |
| `subcommand` | Activates enum-based subcommand dispatch |
| `help_triggers = ["--help", "help"]` | Customizes `--help`/`help` trigger strings |
| `usage = "TEXT"` | Overrides default usage string format |

### Variant Attributes (`#[argh(...)]` on enum variants):
| Attribute | Behavior |
|-----------|----------|
| `dynamic` | Marks enum variant as dynamic subcommand (parsed via `<T as DynamicSubCommand>::try_from_args()`) |
| `name = "NAME"` | `FromArgValue` enums: overrides variant's string representation |

---

## 3. Proc-Macro Entry Points

### `#[proc_macro_derive(FromArgs, attributes(argh))]`
- **Input**: `TokenStream` representing struct/enum definition
- **Output**: `TokenStream` with:
  - `impl argh::FromArgs for T`
  - `impl argh::TopLevelCommand/SubCommand for T` (depending on `#[argh(subcommand)]`)

### `#[proc_macro_derive(FromArgValue, attributes(argh))]`
- **Input**: `TokenStream` representing enum definition
- **Output**: `TokenStream` with:
  - `impl argh::FromArgValue for T`

### `#[proc_macro_derive(ArgsInfo, attributes(argh))]`
- **Input**: `TokenStream` representing struct/enum definition
- **Output**: `TokenStream` with:
  - Metadata generation for help/command info via `arhg::ArgsInfo`

---

## 4. Generated Trait Implementations

### `impl argh::FromArgs for MyStruct`
- **Required methods**:
  ```rust
  fn from_args(__cmd_name: &[&str], __args: &[&str]) -> Result<Self, argh::EarlyExit>
  fn redact_arg_values(__cmd_name: &[&str], __args: &[&str]) -> Result<Vec<String>, argh::EarlyExit>
  ```
- **Generated logic**:
  - Parses CLI args into struct fields (`switch`, `option`, `positional`, `subcommand`)
  - Validates argument presence and defaults
  - Handles subcommand dispatch for nested CLI commands

### `impl argh::FromArgValue for MyEnum`
- **Required method**:
  ```rust
  fn from_arg_value(value: &str) -> Result<Self, String>
  ```
- **Generated logic**:
  - Map string `value` to enum variant based on `#[argh(name = "...")]` or snake-case conventions
  - Returns error for unrecognized values

### `impl argh::ArgsInfo for MyStruct`
- **Generated associated items**:
  - `const COMMAND_INFO: &'static argh::CommandInfo`
  - `const ARGUMENTS: &'static [&'static str]`
  - Method to generate structured help output using `{command_name}` placeholders
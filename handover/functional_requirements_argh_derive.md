# Functional Requirements for the `yarp` crate

## Derive Macros Provided

### 1. **FromArgs**
- **Derive Name**: `FromArgs`
- **Implemented Traits**: The type annotated with `#[derive(FromArgs)]` must implement the following traits on the annotated type:
  - `FromArgs` itself, providing method signatures for parsing arguments.
- **Trait Methods**:
  - `from_args(&[&str], &[&str]) -> Result<Self, EarlyExit>`: Parses command line arguments into the annotated type.
  - `redact_arg_values(&[&str], &[&str]) -> Result<Vec<String>, EarlyExit>`: Redacts argument values for privacy or testing purposes.

### 2. **SubCommands**
- **Derive Name**: `SubCommands`
- **Implemented Traits**: The type annotated with `#[derive(SubCommands)]` must implement the following traits on the annotated type:
  - `SubCommands`: Provides a static list of command information for subcommands.
- **Trait Methods**:
  - `const COMMANDS: &'static [&'static CommandInfo]`: Static array of command infos provided by each subcommand implementing `SubCommand`.

### 3. **SubCommand**
- **Derive Name**: `SubCommand`
- **Implemented Traits**: The type annotated with `#[derive(SubCommand)]` must implement the following traits on the annotated type:
  - `SubCommand`: Provides a static command info for the subcommand.
- **Trait Methods**:
  - `const COMMAND: &'static CommandInfo`: Static command info provided by this subcommand.

### 4. **TopLevelCommand**
- **Derive Name**: Not applicable (implicit trait through implementations)
- **Implemented Traits**: Types that are top-level commands should implicitly implement the `TopLevelCommand` trait, which requires implementing `FromArgs`.

## Helper Attributes

### Attribute Path: `#[argh(option)]`
- **Applies to**: Fields within structs or enums.
- **Controls**: Indicates that the field is an option with a value following it in command line arguments.

### Attribute Path: `#[argh(switch)]`
- **Applies to**: Fields and variants within structs or enums.
- **Controls**: Indicates that the switch (boolean flag) can be toggled on/off without a specific value in command line arguments.

### Attribute Path: `#[argh(description = "...")]`
- **Applies to**: Any item where description is applicable, including fields, variants, items, etc.
- **Controls**: Provides a detailed description of the argument or field for user documentation.

## Proc-Macro Entry Points

### 1. `#[proc_macro_derive(FromArgs, attributes(...))]`
- **Input Type**: `TokenStream` (proc_macro::TokenStream)
- **Output Type**: `TokenStream`
- **Purpose**: Automatically derives the `FromArgs` trait for the specified type, implementing necessary methods to parse command line arguments.

### 2. `#[proc_macro_derive(SubCommands, attributes(...))]`
- **Input Type**: `TokenStream` (proc_macro::TokenStream)
- **Output Type**: `TokenStream`
- **Purpose**: Automatically derives the `SubCommands` trait for the specified type, providing static command information.

### 3. `#[proc_macro_derive(SubCommand, attributes(...))]`
- **Input Type**: `TokenStream` (proc_macro::TokenStream)
- **Output Type**: `TokenStream`
- **Purpose**: Automatically derives the `SubCommand` trait for the specified type, providing command information.

## Generated Trait Implementations

### Impl Block: `impl<T: FromArgs> FromArgs for Box<T>`
- **Methods**:
  - `from_args(&[&str], &[&str]) -> Result<Self>`: Parses arguments and wraps them in a box.
  - `redact_arg_values(&[&str], &[&str]) -> Result<Vec<String>>`: Redacts argument values for boxed types, ensuring privacy or testability.

### Impl Block: `impl FromArgs for T`
- **Methods**:
  - `from_args(&[&str], &[&str]) -> Result<Self>`: Parses command line arguments into the type implementing this trait.
  - `redact_arg_values(&[&str], &[&str]) -> Result<Vec<String>>`: Redacts argument values for testability or privacy.

### Impl Block: `impl SubCommands for T`
- **Methods**:
  - `const COMMANDS: &'static [&'static CommandInfo]`: Static array of command infos provided by each subcommand implementing `SubCommand`.

### Impl Block: `impl<T: SubCommand> SubCommand for Box<T>`
- **Methods**:
  - `const COMMAND: &'static CommandInfo`: Static command info provided by this boxed subcommand.

These specifications cover the functional requirements for the `yarp` crate, focusing on derive macros and attribute handling as well as macro entry points and generated trait implementations.
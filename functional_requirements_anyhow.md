anyhow::Error::backtrace -> Option<std::backtrace::Backtrace>
  Requires [feature = "std"]
  Returns Some(backtrace) when feature "std" is enabled
  Returns None when feature "std" is disabled

anyhow::Error::context(&self, msg: impl Into<Cow<'static, str>>) -> Error
  Creates new Error with added context message
  Preserves original error as source
  Message must be 'static

anyhow::Error::source(&self) -> Option<&(dyn std::error::Error + 'static)>
  Returns underlying source error
  Returns None for terminal errors in chain
  Implements std::error::Error trait contract

anyhow::Error::root_cause(&self) -> &(dyn std::error::Error + 'static)
  Returns first error in causal chain
  Always returns reference to Error in chain

anyhow::Chain::new(error: &Error) -> Chain
  Creates iterator over error chain
  Methods:
    .next() -> Option<&dyn std::error::Error>
    .next_back() -> Option<&dyn std::error::Error>
    .len() -> usize
    .size_hint() -> (usize, Option<usize>)
    .rev() -> Rev

anyhow::bail!(msg: impl Display) -> !
  Panics with Err(anyhow::Error) containing msg
  Consumes input via Display implementation
  Example output must contain: "oh no!"

anyhow::anyhow!(error: impl IntoError) -> Error
  Converts error into anyhow::Error
  Supports: &str, String, Box<dyn Error>, custom types implementing Display
  Example: anyhow!("message") creates Error with message "oh no!"

anyhow::ensure!(condition: bool, msg: impl Display)
  Returns Ok(()) if condition is true
  Returns Err(anyhow::Error) with msg if condition is false
  Example error message format: 
    "Condition failed: `expr` (actual vs expected)"
    "Condition failed: `v + v == 1` (2 vs 1)"

anyhow::Context::context<T, E>(&self, msg: impl FnOnce() -> impl Into<Cow<'static, str>>) -> Result<T, Error>
  Adds contextual message to error chain on Err
  Lazy evaluation of message function
  Preserves original error for Ok values

anyhow::Result<T> -> Result<T, anyhow::Error>
  Type alias for error type propagation
  Supports ? operator with Error conversion
  Example: fn() -> anyhow::Result<()>

anyhow::Error::is<U: ErrorKind>(&self) -> bool
  Checks if error is of specific type U
  Requires U to implement ErrorKind
  Returns true if downcast possible

anyhow::Error::downcast<T: ErrorKind>(self) -> Result<Box<T>, Self>
  Converts Error to Box<T> if type matches
  Returns Err(original) if type mismatch
  Drops other error chain elements

anyhow::Error::fmt_impl::display(ref this, &mut fmt::Formatter) -> fmt::Result
  Formats error with causal chain using :
  In -form  "error1: error2: ..."
  In -form includes full chain

anyhow::Error::fmt_impl::debug(ref this, &mut fmt::Formatter) -> fmt::Result
  Formats with "Caused by:" sections
  Includes backtrace if captured and feature "std" enabled
  Example output:
    "error1\n\nCaused by:\n  0: error2\n  1: error3"

anyhow::Backtrace
  Conditional type based on features
  [feature = "std"]: std::backtrace::Backtrace
  [not(feature = "std")]: Empty enum
  Implements Send + Sync

anyhow::Error formatting constraints:
  Display format must contain: "oh no!"
  Debug format must contain:
    - "Caused by:"
    - "Stack backtrace:"
    - Line numbers in " Caused by: N: " format
    - Backtrace frames if captured

anyhow::Chain string representations:
  FORWARD iteration format: "outer: inner: ..."
  REVERSE iteration format: "...: inner: outer"
  Length must match number of errors in chain

anyhow::Context error messages must:
  - Include original error message
  - Include contextual message as prefix
  - Show chain positions using numbers
  Example: "failed to start server: failed to load config: no such file..."

anyhow::ensure! error messages must satisfy:
  - "Condition failed: `expression`"
  - "actual value vs expected value" comparison
  - Example: "Condition failed: `v + v == 1` (2 vs 1)"
  - For string comparisons: include " vs " between values
  - For numeric comparisons: show exact numeric values

anyhow::Error downcasting must satisfy:
  - *downcast_ref* returns Some(T) if type matches
  - downcast_mut returns Some(&mut T) if type matches
  - downcast returns Ok(Box<T>) if type matches
  - Drops other elements in chain when specific type is extracted

anyhow::Backtrace visibility rules:
  - Requires RUST_LIB_BACKTRACE=1 environment variable
  - Only captured in debug builds
  - Must be None when feature "std" is disabled
  - Must include "stack backtrace:" prefix in output

anyhow::Error formatting must include:
  - Original error message first
  - "Caused by:" sections with numbered causes
  - Backtrace section if available
  - "S" capitalized prefix on backtrace output
  - No trailing colons or empty lines

anyhow::macros must satisfy:
  - anyhow!(...) must handle format strings
  - bail!(...) must format errors with context
  - ensure!(...) must capture expression text
  - Context messages must be lazily evaluated

anyhow::nightly features:
  - Requires nightly Rust for error source access
  - Uses generic_member_access cfg flag
  - Implements source/backtrace access via nightly APIs
  - Fallback to std backtrace when nightly features unavailable
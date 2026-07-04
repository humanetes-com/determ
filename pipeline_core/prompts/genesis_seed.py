"""Initial genesis.md content — the seed reconstruction methodology.

genesis.md is the project-INDEPENDENT methodology document: how to turn test
signal into a compiling, test-passing implementation. TASK 4 evolves it after
failures; before this seed existed the happy path (iteration-1 success) left
only a placeholder, so the per-fn prompts carried no methodology at all
(debt #38). The seed is deterministic (Rule 9) — a curated static artifact,
never LLM-generated — and written whenever genesis.md is absent.

Style contract (CLAUDE.md): concrete WRONG→RIGHT examples with generic
placeholder names, compact rules — never abstract prose.
"""

GENESIS_SEED = """# genesis.md — reconstruction methodology

How to derive an implementation from tests + functional requirements + API
surface. TASK 4 appends lessons after failed iterations; keep entries compact.

## Extracting test signal

1. Asserted literals are exact contract. Copy asserted strings byte-for-byte.
   WRONG: `write!(f, "Division by zero")` when the test asserts
          `e.to_string() == "division by zero"`.
   RIGHT: `write!(f, "division by zero")`.

2. Implement the general rule behind the assertions, never a lookup of test
   inputs.
   WRONG: `fn digit_sum(n: u32) -> u32 { if n == 123 { 6 } else { 0 } }`
   RIGHT: `fn digit_sum(mut n: u32) -> u32 { let mut s = 0; while n > 0 { s += n % 10; n /= 10; } s }`

3. Roundtrip tests define inverses — implement decode as the true inverse of
   encode, not as a separate guess.
   WRONG: encode pads to 4 hex chars but decode parses only unpadded input.
   RIGHT: `decode(&encode(x)) == Some(x)` holds by construction for every x.

4. Add no guard the tests do not assert. The simplest implementation
   consistent with ALL assertions is the target.
   WRONG: `if s.len() == 4 { s.parse().ok() } else { None }` when no test
          exercises other lengths.
   RIGHT: `s.parse().ok()`.

5. Distinct error variants come from distinct asserted inputs — map each
   asserted failing input class to the exact variant the test names.
   WRONG: returning `Err(ParseError::Bad)` for both `""` and `"x"` when tests
          assert `Err(ParseError::Empty)` for `""`.
   RIGHT: check the empty/blank case first, then per-token failures.

6. `#[should_panic(expected = "...")]` fixes the panic message — panic with
   exactly that substring.

## Cross-cutting

7. When cfg-gated tests assert DIFFERENT results for the same fn, branch on
   the cfg inside one body.
   WRONG: one unconditional `fn limit() -> usize { 255 }` failing the default
          cluster's `limit() == 64`.
   RIGHT: `if cfg!(any(extra_cap, feature = "extra_cap")) { 255 } else { 64 }`.

8. Frozen signatures and `where` clauses are ground truth — satisfy them,
   never edit them. If a bound looks insufficient, find the in-scope method
   that the bound DOES provide.

9. Sibling fns share invariants: if `push` maintains a length prefix, `pop`
   must maintain the same prefix. Re-read filled siblings before writing.
"""

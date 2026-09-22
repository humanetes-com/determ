# The Python↔Rust seam

The Rust extractor (`tools/skeleton_extractor`) and the Python pipeline communicate over a
**JSON CLI contract**. This boundary is deliberate and stable: the Rust side parses Rust with
`syn` (a real AST, never regex) and emits typed records; the Python side parses that JSON into
frozen dataclasses. Schema changes go through *both* sides together — `model.rs` and
`pipeline_core/domain/models.py` — never ad-hoc dict mutation on the Python side.

## Wire records

Each record type exists on both sides, field-for-field:

| Rust (`src/model.rs`) | Python (`pipeline_core/domain/models.py`) | emitted by |
|---|---|---|
| `FnRecord` | `FnRecord` | `--enumerate-fns` |
| `TypeEnumeration` (`types`: `TypeRecord` / `FieldRecord` / `MethodRecord` / `TraitImplRecord`, plus `blanket_impls`) | `type_records_from_enumeration` → `list[TypeRecord]`, the one `blanket_impls` tuple attached to every record | `--enumerate-types` (one object, not a list) |
| `ModRecord` | `ModRecord` | `--enumerate-mods` |
| `ConstRecord` | `ConstRecord` | `--enumerate-consts` |
| `DispatchFamily` | `DispatchFamily` | `--enumerate-dispatch-families` |
| `FileReport` | (consumed inline) | `--report` (skeletonise) |

`FnRecord`, for example, is exactly:

```json
{
  "file": "src/error.rs", "fn_name": "backtrace",
  "sig_text": "pub fn backtrace(&self) -> &Backtrace", "body_text": "unimplemented!()",
  "sig_byte_start": 10, "body_byte_start": 50, "body_byte_end": 70,
  "cfg_predicates": ["feature = \"std\""], "is_pub": true,
  "container": "impl Error", "has_unimplemented_body": true
}
```

On the Python side this is parsed by `FnRecord.from_dict` (tolerant: missing keys take the
field default, extra keys are ignored) and round-trips byte-identically via `to_dict`.

## CLI subcommands

The binary is invoked by `pipeline_core/adapters/extractor.py`. The flag names are part of the
contract — they are preserved byte-for-byte:

```
skeleton-extractor --enumerate-fns               --input <dir> [--cfg-true PRED]... [--cfg-false PRED]...
skeleton-extractor --enumerate-types             --input <dir> ...
skeleton-extractor --enumerate-mods              --input <dir> ...
skeleton-extractor --enumerate-consts            --input <dir> ...
skeleton-extractor --enumerate-dispatch-families --input <dir> ...
skeleton-extractor --enumerate-feature-gate-cfgs --input <dir> ...
skeleton-extractor --input <dir> --output <dir> --report   [--cfg-true PRED]... [--cfg-false PRED]...
skeleton-extractor --merge-clusters --merge-output <path> --cluster name:path:cfg ...
```

- `--cfg-true` / `--cfg-false` (repeatable) carry the active cluster's cfg predicates; the
  extractor evaluates `#[cfg(...)]` against them and filters items accordingly.
- Each `enumerate_*` returns a JSON array on stdout, or exits non-zero. The Python adapter maps
  a failure (missing binary, non-zero exit, bad JSON) to `None` = "no records".

## Why it's stable

The seam is the experiment's structural ground truth. Tests on both sides guard it: the crate's
unit tests assert the record shapes and merge behaviour; the Python `tests/test_domain.py`
asserts `from_dict`/`to_dict` round-trip the exact wire shape. A change that altered the JSON
silently would break one or the other. Keep field names and order as the contract.

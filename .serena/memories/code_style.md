# Code Style & Conventions

## Python (yt-dlp)

### General Style
- **Line length**: 120 characters (configured in pyproject.toml)
- **Quotes**: Single quotes for multiline, double for docstrings
- **Indentation**: 4 spaces

### Linting Rules (Ruff)
Key ignored rules:
- `E402` - module-import-not-at-top-of-file
- `E501` - line-too-long
- `E731` - lambda-assignment
- `E741` - ambiguous-variable-name
- `B006` - mutable-argument-default
- `B023` - function-uses-loop-variable
- `PLW0603` - global-statement

### Import Conventions
- Use `from __future__ import annotations`
- Relative imports ordered: closest-to-furthest
- Banned imports from: base64, datetime, functools, json, os, re, sys, etc.

### Naming Conventions
- Functions/variables: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_CASE`

### yt-dlp Specific Conventions
- Extractor classes: `PlatformIE` (e.g., `TikTokIE`)
- `_VALID_URL` regex pattern required
- `_extract_info()` method for metadata extraction
- Use `traverse_obj()` for safe dict access

## Rust (server implementations)

### General Style
- Follow standard Rust conventions (rustfmt)
- Use `cargo fmt` for formatting
- Use `cargo clippy` for linting

### Naming Conventions
- Functions/variables: `snake_case`
- Types/Structs: `PascalCase`
- Constants: `UPPER_CASE` or `SCREAMING_SNAKE_CASE`

### Async Patterns
- Use `tokio::spawn_blocking` for CPU-intensive tasks
- Use `tokio::time::timeout` for operation timeouts
- Leverage Tokio's auto-managed thread pool (no manual worker config)

### Error Handling
- Use `Result<T, E>` for fallible operations
- Use `?` operator for error propagation
- Map errors to appropriate HTTP status codes

### PyO3 Integration
```rust
Python::with_gil(|py| {
    let yt_dlp = py.import("yt_dlp")?;
    // ... extraction logic
})
```

## Project Structure Patterns

### Python Server (serverx/serverpy)
```
serverx/
├── main.py              # FastAPI app entry point
├── requirements.txt     # Dependencies
├── Dockerfile           # Container config
└── docker-compose.yml   # Orchestration
```

### Rust Server (serverx-rs/serverrs)
```
serverx-rs/
├── Cargo.toml           # Rust dependencies
├── src/
│   └── main.rs          # Axum server
├── Dockerfile
└── docker-compose.yml
```

## Testing Conventions

### Python
- Use `pytest` framework
- Test files: `test_*.py`
- Fixtures in `conftest.py`
- Use markers: `@pytest.mark.download` for download tests

### Rust
- Unit tests in `src/` files with `#[cfg(test)]`
- Integration tests in `tests/` directory
- Use `tokio::test` for async tests

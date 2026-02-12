# Suggested Commands for yt-dlp-tiktok

## yt-dlp Core Development

### Setup
```bash
# Install with all dev dependencies
pip install -e ".[default,dev]"

# Setup pre-commit hooks
hatch run setup
```

### Testing
```bash
# Run all tests
make test

# Run offline tests only
make offlinetest

# Run pytest directly
python -m pytest -Werror

# Run specific test
python -m pytest test/test_download.py -k "test_TikTok"
```

### Linting & Formatting
```bash
# Check code style
make codetest

# Run ruff linter
ruff check .

# Run autopep8 formatter check
autopep8 --diff .

# Fix formatting
autopep8 --in-place .
```

### Building
```bash
# Build everything
make all

# Build lazy extractors only
make lazy-extractors

# Build documentation
make doc

# Create tar.gz
make tar
```

### Cleaning
```bash
# Clean test artifacts
make clean-test

# Clean distribution files
make clean-dist

# Clean cache
make clean-cache

# Clean everything
make clean-all
```

## Serverx (Python/FastAPI - Simple)

```bash
cd serverx

# Install dependencies
pip install -r requirements.txt

# Run development server
python main.py

# Run with uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Run production
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

## Serverx-rs (Rust - Simple)

```bash
cd serverx-rs

# Build & run
cargo run

# Build release
cargo build --release

# Run with custom port
PORT=9000 cargo run

# Check code
cargo check

# Run tests
cargo test
```

## Serverrs (Rust - Full-featured)

```bash
cd serverrs

# Build & run
cargo run

# Build release
cargo build --release

# Docker build
docker compose up --build

# Docker production
docker compose up -d
```

## Serverpy (Python - Full-featured)

```bash
cd serverpy

# Install dependencies
pip install -r requirements.txt

# Run development
python main.py

# Run with gunicorn
gunicorn -w 5 -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:5001
```

## Testing API Endpoints

### Test serverx / serverx-rs
```bash
# Health check
curl http://localhost:8000/health
curl http://localhost:8025/health

# Extract video (X/Twitter)
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://x.com/username/status/123456789"}'

# Extract video (TikTok)
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@username/video/123456789"}' | jq
```

### Test serverrs
```bash
# Health check
curl http://localhost:3021/health

# Extract metadata
curl -X POST http://localhost:3021/tiktok \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@user/video/123456789"}'
```

## Git Commands

```bash
# Check status
git status

# View recent commits
git log --oneline -10

# Create new branch
git checkout -b feature/new-feature

# Sync with upstream
git fetch upstream
git merge upstream/master
```

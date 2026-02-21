#!/bin/bash
#
# Start script untuk yt-dlp-stream dengan Python 3.14 + Granian
# Supports: development, production, dan free-threading (nogil) modes
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
VENV_DIR="${VENV_DIR:-./venv}"
VENV_NOGIL_DIR="${VENV_NOGIL_DIR:-./venv-nogil}"
PYTHON314="${PYTHON314:-python3.14}"
PYTHON314T="${PYTHON314T:-python3.14t}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# Mode: dev, prod, nogil
MODE="${1:-dev}"

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check pyenv availability
check_pyenv() {
    if command -v pyenv &> /dev/null; then
        log_success "pyenv found"
        eval "$(pyenv init -)"
        return 0
    else
        log_warn "pyenv not found"
        return 1
    fi
}

# Install Python 3.14t via pyenv
install_python_nogil() {
    log_info "Installing Python 3.14 free-threading via pyenv..."

    # Check if already installed
    if pyenv versions --bare | grep -q "^3.14t$"; then
        log_success "Python 3.14t already installed"
        return 0
    fi

    # Install Python 3.14 with nogil
    log_info "This will take 10-30 minutes..."
    PYTHON_CONFIGURE_OPTS="--disable-gil" pyenv install 3.14t || {
        log_error "Failed to install Python 3.14t"
        log_info "Trying alternative: PYTHON_CONFIGURE_OPTS='--disable-gil' pyenv install 3.14-dev"
        PYTHON_CONFIGURE_OPTS="--disable-gil" pyenv install 3.14-dev
    }

    log_success "Python 3.14t installed"
}

# Check Python 3.14 availability
check_python() {
    log_info "Checking Python 3.14..."

    if command -v "$PYTHON314" &> /dev/null; then
        PYTHON_CMD="$PYTHON314"
    elif command -v "python3.14" &> /dev/null; then
        PYTHON_CMD="python3.14"
    elif command -v "/opt/homebrew/bin/python3.14" &> /dev/null; then
        PYTHON_CMD="/opt/homebrew/bin/python3.14"
    else
        log_error "Python 3.14 not found!"
        log_info "Install with: brew install python@3.14"
        exit 1
    fi

    PYTHON_VERSION=$($PYTHON_CMD --version 2>&1)
    log_success "Found: $PYTHON_VERSION at $PYTHON_CMD"

    # Check GIL support
    GIL_STATUS=$($PYTHON_CMD -c "import sys; print('disabled' if hasattr(sys.flags, 'nogil') and sys.flags.nogil else 'enabled')" 2>/dev/null || echo "enabled")
    log_info "GIL status: $GIL_STATUS"
}

# Check Python 3.14t (free-threading)
check_python_nogil() {
    log_info "Checking Python 3.14 free-threading..."

    # Try different ways to find python3.14t
    if command -v "$PYTHON314T" &> /dev/null; then
        PYTHON_CMD="$PYTHON314T"
    elif command -v "python3.14t" &> /dev/null; then
        PYTHON_CMD="python3.14t"
    elif pyenv versions --bare 2>/dev/null | grep -q "3.14t"; then
        PYTHON_CMD="$(pyenv root)/versions/3.14t/bin/python3"
    elif [ -f "$HOME/.pyenv/versions/3.14t/bin/python3" ]; then
        PYTHON_CMD="$HOME/.pyenv/versions/3.14t/bin/python3"
    else
        log_warn "Python 3.14t not found"
        return 1
    fi

    # Verify it's actually free-threading
    GIL_STATUS=$($PYTHON_CMD -c "import sys; print('disabled' if hasattr(sys.flags, 'nogil') and sys.flags.nogil else 'enabled')" 2>/dev/null || echo "enabled")

    if [ "$GIL_STATUS" != "disabled" ]; then
        log_warn "Found Python at $PYTHON_CMD but GIL is enabled"
        return 1
    fi

    PYTHON_VERSION=$($PYTHON_CMD --version 2>&1)
    log_success "Found: $PYTHON_VERSION (GIL disabled) at $PYTHON_CMD"
    return 0
}

# Setup virtual environment
setup_venv() {
    local venv_path="$1"
    log_info "Setting up virtual environment at $venv_path..."

    if [ ! -d "$venv_path" ]; then
        log_info "Creating new venv..."
        $PYTHON_CMD -m venv "$venv_path"
        log_success "Virtual environment created"
    else
        log_info "Virtual environment exists"
        # Check if venv uses correct Python
        VENV_PYTHON="$venv_path/bin/python"
        if [ -f "$VENV_PYTHON" ]; then
            VENV_GIL=$($VENV_PYTHON -c "import sys; print('disabled' if hasattr(sys.flags, 'nogil') and sys.flags.nogil else 'enabled')" 2>/dev/null || echo "enabled")
            if [ "$MODE" == "nogil" ] && [ "$VENV_GIL" != "disabled" ]; then
                log_warn "Existing venv has GIL enabled, recreating..."
                rm -rf "$venv_path"
                $PYTHON_CMD -m venv "$venv_path"
                log_success "Virtual environment recreated with free-threading"
            else
                log_success "Virtual environment OK (GIL: $VENV_GIL)"
            fi
        fi
    fi
}

# Install dependencies
install_deps() {
    local venv_path="$1"
    log_info "Installing dependencies..."

    VENV_PIP="$venv_path/bin/pip"
    VENV_PYTHON="$venv_path/bin/python"

    # Upgrade pip
    $VENV_PIP install --upgrade pip

    # Install requirements
    if [ -f "requirements.txt" ]; then
        $VENV_PIP install -r requirements.txt
        log_success "Dependencies installed"
    else
        log_error "requirements.txt not found!"
        exit 1
    fi

    # Verify granian installation
    if ! $VENV_PYTHON -c "import granian" 2>/dev/null; then
        log_error "Granian installation failed!"
        exit 1
    fi
    log_success "Granian verified"
}

# Run Granian with appropriate configuration
run_granian() {
    local venv_path="$1"
    VENV_GRANIAN="$venv_path/bin/granian"

    log_info "Starting yt-dlp-stream in ${MODE} mode..."
    log_info "Host: $HOST:$PORT"

    case "$MODE" in
        "dev"|"development")
            log_info "Mode: Development (with reload)"
            $VENV_GRANIAN \
                --interface asgi \
                --reload \
                --loop uvloop \
                --host "$HOST" \
                --port "$PORT" \
                main:app
            ;;

        "prod"|"production")
            log_info "Mode: Production (optimized)"
            $VENV_GRANIAN \
                --interface asgi \
                --workers 2 \
                --threads 16 \
                --loop uvloop \
                --http 1 \
                --backlog 2048 \
                --host "$HOST" \
                --port "$PORT" \
                --access-log \
                main:app
            ;;

        "nogil"|"free-threading")
            log_info "Mode: Free-Threading (NOGIL) - Experimental"
            log_warn "Free-threading mode may have compatibility issues with some C extensions"

            # Verify GIL is disabled
            GIL_CHECK=$($venv_path/bin/python -c "import sys; print('disabled' if hasattr(sys.flags, 'nogil') and sys.flags.nogil else 'enabled')")
            if [ "$GIL_CHECK" != "disabled" ]; then
                log_error "GIL is not disabled! This Python build does not support free-threading."
                log_info "Please install Python 3.14 with --disable-gil flag"
                exit 1
            fi

            log_success "GIL is disabled, starting with free-threading..."

            PYTHON_GIL=0 $VENV_GRANIAN \
                --interface asgi \
                --workers 2 \
                --threads 32 \
                --loop asyncio \
                --http 1 \
                --backlog 2048 \
                --host "$HOST" \
                --port "$PORT" \
                --access-log \
                main:app
            ;;

        *)
            log_error "Unknown mode: $MODE"
            log_info "Usage: $0 [dev|prod|nogil]"
            exit 1
            ;;
    esac
}

# Print usage
print_usage() {
    cat << EOF
Usage: $0 [MODE] [OPTIONS]

Modes:
  dev, development    Development mode with auto-reload
  prod, production    Production mode (optimized)
  nogil, free-threading  Free-threading mode (NOGIL) - Experimental

Environment Variables:
  VENV_DIR       Virtual environment directory (default: ./venv)
  VENV_NOGIL_DIR Virtual environment for nogil (default: ./venv-nogil)
  PYTHON314      Python 3.14 executable path (default: python3.14)
  PYTHON314T     Python 3.14t executable path (default: python3.14t)
  HOST           Host to bind (default: 0.0.0.0)
  PORT           Port to bind (default: 8000)

Examples:
  $0 dev                    # Development mode
  $0 prod                   # Production mode
  PORT=9000 $0 prod         # Production on port 9000
  $0 nogil                  # Free-threading mode

Free-Threading Setup:
  1. Install pyenv: brew install pyenv
  2. Install Python 3.14t: PYTHON_CONFIGURE_OPTS="--disable-gil" pyenv install 3.14t
  3. Run: $0 nogil

EOF
}

# Setup nogil environment
setup_nogil() {
    log_info "Setting up free-threading environment..."

    # Check if pyenv is available
    if check_pyenv; then
        # Try to install Python 3.14t if not found
        if ! check_python_nogil; then
            log_info "Python 3.14t not found, attempting to install via pyenv..."
            install_python_nogil
            if ! check_python_nogil; then
                log_error "Failed to setup Python 3.14t"
                exit 1
            fi
        fi
    else
        log_error "pyenv is required for free-threading setup"
        log_info "Install with: brew install pyenv"
        log_info "Then run: PYTHON_CONFIGURE_OPTS=\"--disable-gil\" pyenv install 3.14t"
        exit 1
    fi

    # Setup venv for nogil
    setup_venv "$VENV_NOGIL_DIR"
    install_deps "$VENV_NOGIL_DIR"
}

# Main
main() {
    if [ "$1" == "-h" ] || [ "$1" == "--help" ]; then
        print_usage
        exit 0
    fi

    echo -e "${GREEN}================================${NC}"
    echo -e "${GREEN}  yt-dlp-stream + Granian${NC}"
    echo -e "${GREEN}================================${NC}"
    echo

    case "$MODE" in
        "nogil"|"free-threading")
            setup_nogil
            echo
            run_granian "$VENV_NOGIL_DIR"
            ;;
        "dev"|"development"|"prod"|"production")
            check_python
            setup_venv "$VENV_DIR"
            install_deps "$VENV_DIR"
            echo
            run_granian "$VENV_DIR"
            ;;
        *)
            log_error "Unknown mode: $MODE"
            print_usage
            exit 1
            ;;
    esac
}

main "$@"

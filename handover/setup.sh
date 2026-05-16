#!/usr/bin/env bash
# Sets up .venv for the backend configured in .env.
# Run once after cloning, or again after changing BACKEND in .env.
#
# Usage:
#   ./setup.sh
#
# After running, activate with:
#   source .venv/bin/activate

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# --- Read BACKEND from .env (default: ollama) ---
BACKEND="ollama"
if [[ -f "$ENV_FILE" ]]; then
    _val=$(grep -E '^BACKEND=' "$ENV_FILE" | tail -1 | cut -d'=' -f2 | tr -d '[:space:]' || true)
    if [[ -n "$_val" ]]; then
        BACKEND="$_val"
    fi
fi

echo "Backend: $BACKEND"

# --- Pick requirements file ---
case "$BACKEND" in
    huggingface)
        REQUIREMENTS="$SCRIPT_DIR/requirements-huggingface.txt"
        ;;
    claude)
        REQUIREMENTS="$SCRIPT_DIR/requirements-base.txt"
        ;;
    ollama|*)
        REQUIREMENTS="$SCRIPT_DIR/requirements-ollama.txt"
        ;;
esac

echo "Requirements: $REQUIREMENTS"

# --- Create .venv if it doesn't exist ---
VENV="$SCRIPT_DIR/.venv"
if [[ ! -d "$VENV" ]]; then
    echo "Creating .venv ..."
    python3 -m venv "$VENV"
fi

# --- Install dependencies ---
echo "Installing dependencies ..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$REQUIREMENTS"

echo ""
echo "Done. Activate with:"
echo "  source .venv/bin/activate"

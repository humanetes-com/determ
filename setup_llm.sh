#!/usr/bin/env bash
set -euo pipefail

INI_FILE="llms.ini"
ENV_FILE=".env"
DOCKER_FILE="docker_vllm.sh"

# 1. Verification of File Prerequisites
if [ ! -f "$INI_FILE" ]; then
    echo "Error: Configuration registry file '$INI_FILE' not found."
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: Target configuration environment '$ENV_FILE' not found."
    exit 1
fi
if [ ! -f "$DOCKER_FILE" ]; then
    echo "Error: Execution blueprint '$DOCKER_FILE' not found."
    exit 1
fi

# Helper function to read from .env safely
_env() { grep -E "^[[:space:]]*$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]' | tr -d '"' | tr -d "'"; }

# Expose local environment authentication token to downloading thread context
HF_TOKEN_VAL=$(_env HF_TOKEN)
if [ -n "$HF_TOKEN_VAL" ]; then
    export HF_TOKEN="$HF_TOKEN_VAL"
fi

# 2. Virtual Environment (venv) Activation Sweep
VENV_DIRS=(".venv" "venv" "env")
for dir in "${VENV_DIRS[@]}"; do
    if [ -f "$dir/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$dir/bin/activate"
        break
    fi
done

# Ensure the active workspace has the unified hf binary toolchain available
python3 -m pip install -q -U "huggingface_hub"

# 3. Apply Modifiers to docker_vllm.sh to Support Variable Arguments
if grep -q "\-\-reasoning-parser qwen3" "$DOCKER_FILE"; then
    sed -i 's|--reasoning-parser qwen3|--reasoning-parser "$(_env VLLM_REASONING_PARSER)"|g' "$DOCKER_FILE"
fi

if ! grep -q "VLLM_REASONING_PARSER=" "$ENV_FILE"; then
    echo 'VLLM_REASONING_PARSER="qwen3"' >> "$ENV_FILE"
fi

# Helper function to extract fields from INI based on the section selection ID
read_ini() {
    local section=$1
    local key=$2
    sed -n "/^\[$section\]/,/^\[/p" "$INI_FILE" | grep -E "^[[:space:]]*$key[[:space:]]*=" | cut -d= -f2- | xargs
}

# 4. Generate the Interactive Dynamic Selection User Interface
echo "============================================="
echo "   vLLM Dynamic Model Switcher (INI Driven)  "
echo "============================================="
echo "Select which LLM setup to configure and download:"

# Read available numerical ID blocks sequentially from the INI file
sections=$(grep -E "^\[([0-9]+)\]" "$INI_FILE" | tr -d '[]')

for id in $sections; do
    lbl=$(read_ini "$id" "name")
    desc=$(read_ini "$id" "description")
    echo "$id) $lbl -- ($desc)"
done
echo "============================================="
read -rp "Enter choice: " choice

# Validate that the requested block index actually exists inside the configuration file
if ! echo "$sections" | grep -q -w "$choice"; then
    echo "[ERROR] Invalid choice. Selection matches no registration inside $INI_FILE."
    exit 1
fi

# 5. Extract Selection Metadata Block Parameters
TARGET_NAME=$(read_ini "$choice" "name")
TARGET_REPO=$(read_ini "$choice" "repo")
TARGET_CONTEXT=$(read_ini "$choice" "context")
TARGET_PARSER=$(read_ini "$choice" "parser")

# 6. Comprehensive search-and-replace sweep for your custom keys
echo -e "\n[!] Modifying .env targeting: $TARGET_NAME"

# Array of all target model keys to trace and adjust inside your environment configuration
TARGET_KEYS=(
    "VLLM_MODEL_NAME"
    "MODEL"
    "TASK0_MODEL"
    "TASK0C_MODEL"
    "TASK1_MODEL"
    "TASK2_MODEL"
    "TASK4_MODEL"
)

for key in "${TARGET_KEYS[@]}"; do
    if grep -q "^[[:space:]]*$key=" "$ENV_FILE"; then
        # Key exists: Replace its value inline securely using alternate delimiters
        sed -i "s|^[[:space:]]*$key=.*|$key=\"$TARGET_REPO\"|" "$ENV_FILE"
        echo " -> Updated existing parameter: $key"
    else
        # Key missing entirely: Safely append it to the end of the file
        echo "$key=\"$TARGET_REPO\"" >> "$ENV_FILE"
        echo " -> Appended missing parameter: $key"
    fi
done

# Synchronize standard context constraints and parser types
sed -i "s|^VLLM_MAX_MODEL_LEN=.*|VLLM_MAX_MODEL_LEN=$TARGET_CONTEXT|" "$ENV_FILE"
sed -i "s|^VLLM_REASONING_PARSER=.*|VLLM_REASONING_PARSER=\"$TARGET_PARSER\"|" "$ENV_FILE"

# 7. Fire the high-speed multi-threaded downloader
echo -e "\n[!] Syncing layers to local runtime storage via 'hf' tool..."
hf download "$TARGET_REPO" --cache-dir ~/.cache/huggingface

echo -e "\n[SUCCESS] Environment aligned and cache generation complete!"
echo "Fire up your container backend now using: ./docker_vllm.sh"
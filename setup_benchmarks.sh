#!/usr/bin/env bash
set -euo pipefail

# Define text styling colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CLEAR='\033[0;36m'
NC='\033[0m'

echo -e "${BLUE}=== LLM Evaluation & High-Speed Setup Script ===${NC}"

# 1. Ensure high-speed python dependency is met
echo -e "\n${YELLOW}[1/4] Installing high-speed Hugging Face multi-threaded CLI tool...${NC}"
if ! command -v pip &> /dev/null; then
    echo "pip could not be found. Please ensure Python and pip are installed on the host."
    exit 1
fi
pip install -q "huggingface_hub[cli]"
echo -e "${GREEN}✓ Hugging Face CLI ready.${NC}"

# 2. Handle HF Token Authentication (Required for gated repositories)
echo -e "\n${YELLOW}[2/4] Authenticating with Hugging Face...${NC}"
echo "We need your HF Token to download models. Press Enter if you've already run 'huggingface-cli login'."
echo -n "Enter Hugging Face Token (or skip): "
read -r -s HF_INPUT_TOKEN
echo ""

if [[ -n "$HF_INPUT_TOKEN" ]]; then
    export HF_TOKEN="$HF_INPUT_TOKEN"
    huggingface-cli login --token "$HF_TOKEN"
else
    echo "Using existing system authentication configurations..."
fi

# 3. Parallel downloading blocks directly to your shared vLLM docker cache cache
echo -e "\n${YELLOW}[3/4] Launching downloads to ~/.cache/huggingface...${NC}"
echo -e "${CLEAR}Downloading Model 1: DeepSeek-V4-Flash (MoE Layer)...${NC}"
huggingface-cli download deepseek-ai/DeepSeek-V4-Flash --cache-dir ~/.cache/huggingface

echo -e "\n${CLEAR}Downloading Model 2: Qwen3-Coder-32B-Instruct-FP8...${NC}"
huggingface-cli download Qwen/Qwen3-Coder-32B-Instruct-FP8 --cache-dir ~/.cache/huggingface

echo -e "\n${CLEAR}Downloading Model 3: Kimi-K2.6-Code...${NC}"
huggingface-cli download MoonshotAI/Kimi-K2.6-Code --cache-dir ~/.cache/huggingface

echo -e "${GREEN}✓ All 3 models downloaded and cached successfully!${NC}"

# 4. Inject a helpful terminal utility macro to switch environments on the fly
echo -e "\n${YELLOW}[4/4] Writing environment configuration helper...${NC}"

cat << 'EOF' > update_env.sh
#!/usr/bin/env bash
# Quick toggle script to change active local model configs
ENV_FILE=".env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Error: .env file not found in current directory."
    exit 1
fi

echo "Select which model to push live into your vLLM docker container:"
echo "1) DeepSeek V4-Flash (1M Context MoE)"
echo "2) Qwen3 Coder 32B FP8 (Strict Type Focus)"
echo "3) Kimi K2.6 Code (Complex Reasoning Spec)"
read -rp "Choice [1-3]: " choice

case $choice in
    1)
        sed -i 's/^VLLM_MODEL_NAME=.*/VLLM_MODEL_NAME="deepseek-ai\/DeepSeek-V4-Flash"/' "$ENV_FILE"
        sed -i 's/^VLLM_MAX_MODEL_LEN=.*/VLLM_MAX_MODEL_LEN=131072/' "$ENV_FILE"
        echo "Updated .env to DeepSeek V4-Flash"
        ;;
    2)
        sed -i 's/^VLLM_MODEL_NAME=.*/VLLM_MODEL_NAME="Qwen\/Qwen3-Coder-32B-Instruct-FP8"/' "$ENV_FILE"
        sed -i 's/^VLLM_MAX_MODEL_LEN=.*/VLLM_MAX_MODEL_LEN=65536/' "$ENV_FILE"
        echo "Updated .env to Qwen3 Coder 32B (Safe 64k context boundary)"
        ;;
    3)
        sed -i 's/^VLLM_MODEL_NAME=.*/VLLM_MODEL_NAME="MoonshotAI\/Kimi-K2.6-Code"/' "$ENV_FILE"
        sed -i 's/^VLLM_MAX_MODEL_LEN=.*/VLLM_MAX_MODEL_LEN=65536/' "$ENV_FILE"
        echo "Updated .env to Kimi K2.6 Code"
        ;;
    *)
        echo "Invalid selection."
        exit 1
        ;;
esac

echo "Done! You can now run your ./docker_vllm.sh script to boot the fresh container."
EOF

chmod +x update_env.sh
echo -e "${GREEN}✓ Generated './update_env.sh' script to help change your active model context instantly.${NC}"
echo -e "\n${BLUE}Setup Complete. Ready to test!${NC}"
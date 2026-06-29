# 1. Install the official high-speed downloader utility
pip install "huggingface_hub[cli]"

# 2. Download the 3 models (They will go straight to your Docker cache)
huggingface-cli download deepseek-ai/DeepSeek-V4-Flash --cache-dir ~/.cache/huggingface
huggingface-cli download Qwen/Qwen3-Coder-32B-Instruct-FP8 --cache-dir ~/.cache/huggingface
huggingface-cli download MoonshotAI/Kimi-K2.6-Code --cache-dir ~/.cache/huggingface
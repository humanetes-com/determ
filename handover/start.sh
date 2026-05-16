sudo systemctl start ollama  

source .venv/bin/activate && OLLAMA_CONTEXT_LENGTH=8192 python pipeline.py --project $1 2>&1
| tee /tmp/pipeline_live.log   

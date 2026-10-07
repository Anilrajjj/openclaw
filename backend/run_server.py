import sys
import os
from pathlib import Path

import uvicorn


log_path = Path(__file__).with_name("uvicorn-background.log")
log_file = log_path.open("a", encoding="utf-8", buffering=1)
sys.stdout = log_file
sys.stderr = log_file

host = os.getenv("BACKEND_HOST", "127.0.0.1")
port = int(os.getenv("BACKEND_PORT", "8001"))

uvicorn.run("main:app", host=host, port=port, log_level="info")

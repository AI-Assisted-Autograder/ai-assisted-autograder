import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = PROJECT_ROOT / "web"

os.chdir(WEB_DIR)

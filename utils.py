import os
import time
import json
import random
import logging
from typing import Any

log = logging.getLogger("memecoinsnipa")

def setup_logging():
    if not log.handlers:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO"),
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )

def env_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v).strip()

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default

def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default

def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def jitter_sleep(seconds: float, jitter: float = 0.2):
    if seconds <= 0:
        return
    delta = seconds * jitter
    time.sleep(max(0.0, seconds + random.uniform(-delta, delta)))

def safe_json_load(path: str, default: Any):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning(f"Failed to read json {path}: {e}")
        return default

def safe_json_write_atomic(path: str, obj: Any):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

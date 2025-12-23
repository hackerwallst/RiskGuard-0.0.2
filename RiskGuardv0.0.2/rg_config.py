from __future__ import annotations

import os
from typing import Dict, Optional


_ROOT = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PATH = os.path.join(_ROOT, "config.txt")

_CACHE: Optional[Dict[str, str]] = None


def _read_config(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                data[key] = value.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return data


def _get_config() -> Dict[str, str]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _read_config(_DEFAULT_PATH)
    return _CACHE


def get_str(key: str, default: str = "") -> str:
    val = _get_config().get(key)
    if val is None or val == "":
        return default
    return val


def get_int(key: str, default: int = 0) -> int:
    val = _get_config().get(key)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except Exception:
        return default


def get_optional_int(key: str, default: Optional[int] = None) -> Optional[int]:
    val = _get_config().get(key)
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("", "none", "null"):
        return None
    try:
        return int(v)
    except Exception:
        return default


def get_float(key: str, default: float = 0.0) -> float:
    val = _get_config().get(key)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except Exception:
        return default


def get_optional_float(key: str, default: Optional[float] = None) -> Optional[float]:
    val = _get_config().get(key)
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("", "none", "null"):
        return None
    try:
        return float(v)
    except Exception:
        return default


def get_bool(key: str, default: bool = False) -> bool:
    val = _get_config().get(key)
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default

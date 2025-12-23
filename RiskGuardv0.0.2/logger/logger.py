# logger.py — Função 6: registro de logs mensais (JSON Lines)
from __future__ import annotations
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from pathlib import Path
import json
import os

# Diretório base (padrão: ./logs ao lado dos scripts)
_BASE_DIR = Path(__file__).resolve().parent
_LOG_DIR = _BASE_DIR / "logs"

def set_log_dir(path: str | Path) -> None:
    """Opcional: defina outro diretório de logs."""
    global _LOG_DIR
    _LOG_DIR = Path(path).expanduser().resolve()
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

def _month_filename(dt: Optional[datetime] = None) -> Path:
    if dt is None:
        dt = datetime.now(timezone.utc)
    fname = f"{dt.year:04d}-{dt.month:02d}-riskguard.log"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR / fname

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def log_event(event_type: str,
              payload: Optional[Dict[str, Any]] = None,
              context: Optional[Dict[str, Any]] = None) -> None:
    """
    Escreve uma linha JSON no log mensal atual.
    - event_type: categoria do evento (ex.: 'CLOSE', 'LIMITS', 'DD_KILL', 'NEWS', 'ERROR')
    - payload: dados específicos do evento
    - context: infos padrão (ex.: {'account': login, 'server': 'XM...', 'module': 'limits'})
    """
    entry = {
        "ts": _utc_iso(),            # UTC ISO
        "type": str(event_type).upper(),
        "payload": payload or {},
        "context": context or {},
    }
    path = _month_filename()
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as e:
        # Evita quebrar o gestor por falha de log
        try:
            err_path = _LOG_DIR / "logger_errors.log"
            err = {
                "ts": _utc_iso(),
                "type": "LOGGER_ERROR",
                "error": repr(e),
                "entry": entry,
            }
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            with err_path.open("a", encoding="utf-8") as ef:
                ef.write(json.dumps(err, ensure_ascii=False) + "\n")
        except Exception:
            pass

def log_path_current() -> str:
    """Retorna o caminho do arquivo de log do mês atual (string)."""
    return str(_month_filename())

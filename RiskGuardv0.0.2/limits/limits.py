# limits.py — Função 3: risco agregado ≤ 5%; bloquear novas aberturas; 3 tentativas -> bloqueio lógico
from __future__ import annotations
from typing import Any, Dict, List, Set
from datetime import datetime
import os, json
import pytz
from rg_config import get_float, get_int
from logger import log_event
from notify import notify_limits

from mt5_reader import RiskGuardMT5Reader
from .guard import close_position_full

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".riskguard_limits.json")
DEFAULT_THRESHOLD_PCT = get_float("AGGREGATE_MAX_RISK", 5.0)
DEFAULT_MAX_ATTEMPTS = get_int("AGGREGATE_MAX_ATTEMPTS", 3)

# ---------------------------
# Estado persistente simples
# ---------------------------
def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(d: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def _now_utc():
    return datetime.utcnow().replace(tzinfo=pytz.UTC)

# ---------------------------
# API pública (Função 3)
# ---------------------------
def enforce_aggregate_risk(reader: RiskGuardMT5Reader,
                           threshold_pct: float = DEFAULT_THRESHOLD_PCT,
                           max_block_attempts: int = DEFAULT_MAX_ATTEMPTS) -> Dict[str, Any]:
    """
    Regras:
      - Se total_risk_pct <= threshold: atualiza baseline (tickets atuais), zera tentativas e remove bloqueio lógico.
      - Se total_risk_pct > threshold: qualquer NOVO ticket (não presente no baseline) é fechado.
        Cada novo ticket conta 1 tentativa. Ao atingir max_block_attempts -> risk_block_active=True.
      - Enquanto risk_block_active=True, o módulo continua fechando novos tickets; o bloqueio cai
        automaticamente assim que total_risk_pct <= threshold.

    Retorna um relatório com estado e ações tomadas.
    """
    snap = reader.snapshot()
    total = float(snap["exposure"]["total_risk_pct"])
    tickets_current: Set[int] = {int(p["ticket"]) for p in snap["positions"]}

    st = _load_state()
    baseline: List[int] = st.get("baseline_tickets") or []
    attempts: int = int(st.get("block_attempts", 0))
    risk_block_active: bool = bool(st.get("risk_block_active", False))

    report: Dict[str, Any] = {
        "now_utc": _now_utc().isoformat(),
        "threshold_pct": threshold_pct,
        "total_risk_pct": total,
        "positions": len(tickets_current),
        "baseline_tickets": baseline,
        "new_tickets_detected": [],
        "closed": [],
        "failed": [],
        "attempts_before": attempts,
        "attempts_after": attempts,
        "risk_block_active_before": risk_block_active,
        "risk_block_active_after": risk_block_active
    }

    # Primeira execução: cria baseline e não fecha nada
    if not baseline:
        st["baseline_tickets"] = sorted(list(tickets_current))
        st["block_attempts"] = 0
        st["risk_block_active"] = False
        _save_state(st)
        report["baseline_tickets"] = st["baseline_tickets"]
        report["attempts_after"] = 0
        report["risk_block_active_after"] = False
        return report

    # Se risco agregado está OK, atualiza baseline e limpa bloqueio/tentativas
    if total <= (threshold_pct + 1e-9):
        st["baseline_tickets"] = sorted(list(tickets_current))
        st["block_attempts"] = 0
        st["risk_block_active"] = False
        _save_state(st)
        report["baseline_tickets"] = st["baseline_tickets"]
        report["attempts_after"] = 0
        report["risk_block_active_after"] = False
        return report

    # Risco agregado excedido -> fechar apenas NOVOS tickets
    baseline_set = set(int(x) for x in baseline)
    new_tickets = [t for t in tickets_current if t not in baseline_set]
    report["new_tickets_detected"] = new_tickets

    # Fecha cada novo ticket detectado
    for pos in snap["positions"]:
        t = int(pos["ticket"])
        if t not in new_tickets:
            continue
        ok, res = close_position_full(
            ticket=t,
            symbol=pos["symbol"],
            side=pos["type"],
            volume=float(pos["volume"]),
            comment="RG aggblock"
        )
        if ok:
            report["closed"].append({"ticket": t, "symbol": pos["symbol"], "result": res})
        else:
            report["failed"].append({"ticket": t, "symbol": pos["symbol"], "result": res})

    # Contabiliza tentativas (1 por novo ticket detectado)
    attempts += len(new_tickets)
    st["block_attempts"] = attempts
    report["attempts_after"] = attempts

    # Ativa bloqueio lógico após X tentativas
    if attempts >= max_block_attempts:
        risk_block_active = True
    st["risk_block_active"] = risk_block_active
    report["risk_block_active_after"] = risk_block_active

    # baseline permanece como o conjunto original (não inclui os novos bloqueados)
    _save_state(st)
    return report

def risk_block_status() -> Dict[str, Any]:
    """Consulta estado do bloqueio lógico de risco agregado."""
    st = _load_state()
    return {
        "risk_block_active": bool(st.get("risk_block_active", False)),
        "block_attempts": int(st.get("block_attempts", 0)),
        "baseline_tickets": st.get("baseline_tickets", [])
    }

# ---------------------------
# Execução direta (teste)
# ---------------------------
if __name__ == "__main__":
    # Ajuste o caminho do terminal conforme necessário
    TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    reader = RiskGuardMT5Reader(path=TERMINAL_PATH)
    assert reader.connect(), "Falha ao conectar no MT5"
    try:
        from pprint import pprint
        rep = enforce_aggregate_risk(reader, threshold_pct=DEFAULT_THRESHOLD_PCT, max_block_attempts=DEFAULT_MAX_ATTEMPTS)
        should_log = bool(
            rep.get("new_tickets_detected") or
            rep.get("closed") or
            rep.get("failed") or
            (rep.get("attempts_before") != rep.get("attempts_after")) or
            (rep.get("risk_block_active_before") != rep.get("risk_block_active_after"))
        )
        if should_log:
            log_event("LIMITS", {
                "total_risk_pct": rep["total_risk_pct"],
                "new_tickets": rep["new_tickets_detected"],
                "closed": rep["closed"],
                "failed": rep["failed"],
                "attempts": rep["attempts_after"],
                "risk_block_active": rep["risk_block_active_after"]
            }, context={"module": "limits"})
        notify_limits(rep)
        pprint(rep)
        print("status:", risk_block_status())
    finally:
        reader.shutdown()

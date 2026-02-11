# limits.py — Função 3: risco agregado <= limite; tentativas + bloqueio temporário de AutoTrading
from __future__ import annotations
from typing import Any, Dict, List, Set
from datetime import datetime, timedelta
import os, json
import pytz
from rg_config import get_float, get_int
from logger import log_event
from notify import notify_limits

from mt5_reader import RiskGuardMT5Reader
from .guard import close_position_full
from .kill_switch import set_kill_until, kill_status

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".riskguard_limits.json")
DEFAULT_THRESHOLD_PCT = get_float("AGGREGATE_MAX_RISK", 5.0)
DEFAULT_MAX_ATTEMPTS = get_int("AGGREGATE_MAX_ATTEMPTS", 3)
DEFAULT_BLOCK_MINUTES = get_int("AGGREGATE_BLOCK_MINUTES", 60)

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

def _from_iso_any(v: Any):
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        if v.endswith("Z"):
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return datetime.fromisoformat(v)
    except Exception:
        return None

# ---------------------------
# API pública (Função 3)
# ---------------------------
def enforce_aggregate_risk(reader: RiskGuardMT5Reader,
                           threshold_pct: float = DEFAULT_THRESHOLD_PCT,
                           max_block_attempts: int = DEFAULT_MAX_ATTEMPTS,
                           block_minutes: int = DEFAULT_BLOCK_MINUTES) -> Dict[str, Any]:
    """
    Regras:
      - Se total_risk_pct <= threshold: atualiza baseline (tickets atuais), sem zerar tentativas imediatamente.
      - Se total_risk_pct > threshold: qualquer NOVO ticket (não presente no baseline) é fechado.
        Cada novo ticket conta 1 tentativa. Ao atingir max_block_attempts -> risk_block_active=True.
      - Ao atingir max_block_attempts, desativa AutoTrading por block_minutes via kill_switch.
      - Tentativas expiram após block_minutes sem novas violações.

    Retorna um relatório com estado e ações tomadas.
    """
    snap = reader.snapshot()
    total = float(snap["exposure"]["total_risk_pct"])
    tickets_current: Set[int] = {int(p["ticket"]) for p in snap["positions"]}

    st = _load_state()
    baseline: List[int] = st.get("baseline_tickets") or []
    attempts: int = int(st.get("block_attempts", 0))
    last_attempt_at = _from_iso_any(st.get("last_attempt_at"))

    ks_before = kill_status()
    kill_active_before = bool(ks_before.get("active"))
    kill_until_before = ks_before.get("until")
    risk_block_active: bool = bool(st.get("risk_block_active", False)) or kill_active_before

    # Evita carregar tentativa antiga por tempo indefinido:
    # se ficou sem novas violações por "block_minutes", zera o contador.
    if attempts > 0 and last_attempt_at is not None:
        idle_sec = (_now_utc() - last_attempt_at).total_seconds()
        if idle_sec >= max(1, int(block_minutes)) * 60:
            attempts = 0
            st["block_attempts"] = 0
            st["last_attempt_at"] = None

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
        "risk_block_active_after": risk_block_active,
        "kill_switch_active_before": kill_active_before,
        "kill_switch_active_after": kill_active_before,
        "kill_switch_until_before": kill_until_before,
        "kill_switch_until_after": kill_until_before,
        "kill_switch_armed_now": False,
        "block_minutes": int(block_minutes),
    }

    # Primeira execução: cria baseline e não fecha nada
    if not baseline:
        st["baseline_tickets"] = sorted(list(tickets_current))
        st["block_attempts"] = attempts
        st["risk_block_active"] = risk_block_active
        _save_state(st)
        report["baseline_tickets"] = st["baseline_tickets"]
        report["attempts_after"] = attempts
        report["risk_block_active_after"] = risk_block_active
        return report

    # Se risco agregado está OK, atualiza baseline.
    # Não zera tentativas aqui para evitar "Tentativas EA: 1" em loop de abre/fecha.
    if total <= (threshold_pct + 1e-9):
        # Se o kill-switch já expirou e havia bloqueio ativo, faz reset limpo do ciclo.
        if risk_block_active and (not kill_active_before):
            attempts = 0
            st["last_attempt_at"] = None
            risk_block_active = False
        st["baseline_tickets"] = sorted(list(tickets_current))
        st["block_attempts"] = attempts
        st["risk_block_active"] = risk_block_active
        _save_state(st)
        report["baseline_tickets"] = st["baseline_tickets"]
        report["attempts_after"] = attempts
        report["risk_block_active_after"] = risk_block_active
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
    if new_tickets:
        attempts += len(new_tickets)
        st["last_attempt_at"] = _now_utc().isoformat()
    st["block_attempts"] = attempts
    report["attempts_after"] = attempts

    # Ativa bloqueio lógico/físico (AutoTrading OFF) após X tentativas
    if attempts >= max_block_attempts:
        risk_block_active = True
        if not kill_active_before:
            until = _now_utc() + timedelta(minutes=max(1, int(block_minutes)))
            try:
                set_kill_until(until)
                ks_after = kill_status()
                report["kill_switch_armed_now"] = True
                report["kill_switch_active_after"] = bool(ks_after.get("active"))
                report["kill_switch_until_after"] = ks_after.get("until")
            except Exception:
                report["kill_switch_active_after"] = False
                report["kill_switch_until_after"] = None

    # Se não armou kill agora, mantém o status atual observado
    if not report["kill_switch_armed_now"]:
        ks_after = kill_status()
        report["kill_switch_active_after"] = bool(ks_after.get("active"))
        report["kill_switch_until_after"] = ks_after.get("until")

    # Enquanto kill_switch estiver ativo, bloqueio de risco é considerado ativo
    if report["kill_switch_active_after"]:
        risk_block_active = True

    st["risk_block_active"] = risk_block_active
    report["risk_block_active_after"] = risk_block_active

    # baseline permanece como o conjunto original (não inclui os novos bloqueados)
    _save_state(st)
    return report

def risk_block_status() -> Dict[str, Any]:
    """Consulta estado do bloqueio lógico de risco agregado."""
    st = _load_state()
    ks = kill_status()
    return {
        "risk_block_active": bool(st.get("risk_block_active", False)) or bool(ks.get("active")),
        "block_attempts": int(st.get("block_attempts", 0)),
        "baseline_tickets": st.get("baseline_tickets", []),
        "kill_switch_active": bool(ks.get("active")),
        "kill_switch_until": ks.get("until"),
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

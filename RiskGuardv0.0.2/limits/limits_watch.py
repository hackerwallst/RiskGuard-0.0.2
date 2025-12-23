# limits_watch.py — loop de observação para Função 3
from __future__ import annotations
import time, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path: sys.path.insert(0, HERE)

from mt5_reader import RiskGuardMT5Reader
from limits import enforce_aggregate_risk, risk_block_status
from logger import log_event
from notify import notify_limits
from rg_config import get_float, get_int

TERMINAL_PATH   = r"C:\Program Files\MetaTrader 5\terminal64.exe"
THRESHOLD_PCT   = get_float("AGGREGATE_MAX_RISK", 5.0)     # ↓ Para testar rápido, defina 0.5
MAX_ATTEMPTS    = get_int("AGGREGATE_MAX_ATTEMPTS", 3)
INTERVAL_SEC    = get_float("LIMITS_WATCH_INTERVAL_SEC", 0.7)     # intervalo de varredura

def main():
    r = RiskGuardMT5Reader(path=TERMINAL_PATH)
    assert r.connect(), "Falha ao conectar no MT5"
    prev_attempts = None
    prev_block = None
    try:
        print("Observando risco agregado... (Ctrl+C para parar)")
        while True:
            rep = enforce_aggregate_risk(r, threshold_pct=THRESHOLD_PCT, max_block_attempts=MAX_ATTEMPTS)

            total = rep["total_risk_pct"]
            lines = []
            changed = False

            if rep["new_tickets_detected"]:
                lines.append(f"NOVOS: {rep['new_tickets_detected']}")
                changed = True
            if rep["closed"]:
                lines.append(f"FECHADOS: {[c['ticket'] for c in rep['closed']]}")
                changed = True
            if rep["failed"]:
                lines.append(f"FALHOU: {[f['ticket'] for f in rep['failed']]} (ver payload)")
                changed = True

            st = risk_block_status()
            attempts = st["block_attempts"]
            block = st["risk_block_active"]
            if attempts != prev_attempts or block != prev_block:
                lines.append(f"Tentativas={attempts} | RiskBlock={block}")
                prev_attempts, prev_block = attempts, block
                changed = True

            if changed:
                print(f"[{rep['now_utc']}] total_risk={total:.2f}% | " + " | ".join(lines))
                log_event("LIMITS", {
                    "total_risk_pct": rep["total_risk_pct"],
                    "new_tickets": rep["new_tickets_detected"],
                    "closed": rep["closed"],
                    "failed": rep["failed"],
                    "attempts": rep["attempts_after"],
                    "risk_block_active": rep["risk_block_active_after"]
                }, context={"module": "limits"})
                notify_limits(rep)
            time.sleep(INTERVAL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        r.shutdown()

if __name__ == "__main__":
    main()

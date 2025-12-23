# guard.py — MODO DEUS / MODO TIRANO + detecção de burla
from __future__ import annotations
from typing import Any, Dict, Tuple, Optional, List
import os, sys, time, json
import MetaTrader5 as mt5

from logger.logger import log_event
from notify import send_alert
from notify import notify_per_trade  # manter compat
from limits.uia import ensure_autotrading_on, ensure_autotrading_off
from rg_config import get_float

# Notificações opcionais (não falhar se não existirem)
try:
    from notify import send_alert
except Exception:
    send_alert = None

# --- garantir reader no path ---
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mt5_reader import RiskGuardMT5Reader

# Bloqueios (opcionais; não quebra se não existir)
try:
    from kill_switch import kill_status
except Exception:
    def kill_status() -> Dict[str, Any]:
        return {"active": False}

try:
    from limits import risk_block_status
except Exception:
    def risk_block_status() -> Dict[str, Any]:
        return {"risk_block_active": False}

try:
    from dd_kill import dd_status as dd_status_func
except Exception:
    dd_status_func = None


# =========================
# Config / arquivos locais
# =========================
BREACH_CACHE_FILE = os.path.join(HERE, ".guard_breach_cache.json")  # tickets já notificados
DEFAULT_MAX_RISK_PCT = get_float("PERTRADE_MAX_RISK", 1.0)

def _load_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(BREACH_CACHE_FILE):
            with open(BREACH_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"tickets_reported": []}

def _save_cache(d: Dict[str, Any]) -> None:
    try:
        with open(BREACH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ==============
# Utilidades
# ==============
def _opposite_type(side: str) -> int:
    # side da posição: "buy"|"sell" → ordem oposta para fechar
    return mt5.ORDER_TYPE_SELL if str(side).lower() == "buy" else mt5.ORDER_TYPE_BUY

def _safe_comment(s: str, max_len: int = 31) -> str:
    s_ascii = s.encode("ascii", "ignore").decode("ascii")
    return s_ascii[:max_len]

def _get_market_price(symbol: str, order_type: int) -> float:
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        raise RuntimeError(f"Sem tick para {symbol}")
    return float(tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid)

def _symbol_ensure_visible(symbol: str):
    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    if not info:
        raise RuntimeError(f"Símbolo indisponível: {symbol}")
    if info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        # ainda tentaremos fechar (há brokers que permitem fechar mesmo com trade desativado)
        pass


# ==========================
# Envio de ordem — tirano
# ==========================
def _order_send_tirano(req: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    Estratégia:
      1) Até 3 tentativas com IOC + slippage moderado
      2) Forçar execução: FOK + slippage máximo
    """
    # Tentativas “normais”
    for attempt in range(1, 4):
        r = dict(req)
        r["type_filling"] = mt5.ORDER_FILLING_IOC
        r["deviation"] = 50

        result = mt5.order_send(r)
        payload = {
            "attempt": attempt,
            "request": r,
            "result": None if result is None else {
                "retcode": result.retcode,
                "comment": getattr(result, "comment", ""),
                "order": getattr(result, "order", 0),
                "deal": getattr(result, "deal", 0),
                "price": getattr(result, "price", 0.0),
                "volume": getattr(result, "volume", 0.0),
            },
            "last_error": mt5.last_error()
        }

        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True, payload

        time.sleep(0.15)

    # Forçar execução
    r = dict(req)
    r["type_filling"] = mt5.ORDER_FILLING_FOK
    r["deviation"] = 9999
    result = mt5.order_send(r)

    payload = {
        "attempt": "FORCED",
        "request": r,
        "result": None if result is None else {
            "retcode": result.retcode,
            "comment": getattr(result, "comment", ""),
            "order": getattr(result, "order", 0),
            "deal": getattr(result, "deal", 0),
            "price": getattr(result, "price", 0.0),
            "volume": getattr(result, "volume", 0.0),
        },
        "last_error": mt5.last_error()
    }
    ok = bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)
    return ok, payload


def _is_autotrading_disabled(payload: Dict[str, Any]) -> bool:
    """
    Detecta 'AutoTrading disabled' pelos caminhos comuns:
    - retcode 10027 / 10028
    - comment retornando essa string
    - last_error com esses códigos
    """
    try:
        rc = ((payload or {}).get("result") or {}).get("retcode")
        cm = (((payload or {}).get("result") or {}).get("comment") or "").lower()
        le = (payload or {}).get("last_error", (None, ""))[0]
        return (rc in (10027, 10028)) or ("autotrading disabled" in cm) or (le in (10027, 10028))
    except Exception:
        return False

# ==================================
# Fechamento de posição — MODO TIRANO
# ==================================
def close_position_full(ticket: int, symbol: str, side: str, volume: float,
                        comment: str = "RG MODO DEUS") -> Tuple[bool, Dict[str, Any]]:
    """
    Fluxo correto (MODO TIRANO):
      1) Tenta fechar direto via API (se AT já estiver ON).
      2) Se falhar por AutoTrading OFF → LIGA AT → FECHA → aguarda → DESLIGA AT.
    """
    try:
        _symbol_ensure_visible(symbol)
        order_type = _opposite_type(side)

        def _req() -> Dict[str, Any]:
            price_now = _get_market_price(symbol, order_type)  # reprecifica SEMPRE antes de enviar
            return {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(volume),
                "type": order_type,
                "position": int(ticket),
                "price": float(price_now),
                "magic": 0,
                "type_time": mt5.ORDER_TIME_GTC,
                "comment": _safe_comment(comment),
            }

        # 1) Primeira tentativa (sem toggle)
        ok, payload = _order_send_tirano(_req())
        if ok:
            return True, {"mode": "api", **payload}

        # 2) Detecta se falhou porque o AutoTrading está OFF
        if not _is_autotrading_disabled(payload):
            # Não é caso de AT OFF → retorna erro original
            return False, {"mode": "api", **payload}

        # 3) LIGA AutoTrading → FECHA → espera → DESLIGA
        toggled_on = ensure_autotrading_on()
        # pequeno delay para o terminal aplicar o estado
        time.sleep(0.50)

        ok2, payload2 = _order_send_tirano(_req())  # FECHAR AGORA (AT ON)
        # garante processamento do DEAL antes de desligar
        time.sleep(0.50)

        ensure_autotrading_off()
        time.sleep(0.40)

        return (True, {"mode": "api+toggle_hotkey", "uia_on": toggled_on, **payload2}) if ok2 \
               else (False, {"mode": "api+toggle_hotkey", "uia_on": toggled_on, **payload2})

    except Exception as e:
        return False, {"error": repr(e), "ticket": ticket, "symbol": symbol}


# ==================================
# Detectar estado de BLOQUEIO ativo
# ==================================
def _block_active() -> Dict[str, Any]:
    """
    Retorna o estado consolidado de bloqueio:
    - kill_switch.active
    - dd_kill: cooldown/awaiting_unlock
    - limits: risk_block_active
    """
    st = {
        "kill_active": False,
        "dd_cooldown": False,
        "dd_await": False,
        "risk_block": False
    }
    try:
        ks = kill_status()
        st["kill_active"] = bool(ks.get("active"))
    except Exception:
        pass
    try:
        rb = risk_block_status()
        st["risk_block"] = bool(rb.get("risk_block_active"))
    except Exception:
        pass
    if dd_status_func:
        try:
            ds = dd_status_func()
            st["dd_cooldown"] = bool(ds.get("cooldown_until"))
            st["dd_await"] = bool(ds.get("awaiting_unlock"))
        except Exception:
            pass
    st["any_block"] = st["kill_active"] or st["dd_cooldown"] or st["dd_await"] or st["risk_block"]
    return st


def _notify_breach(lines: List[str]):
    # usa send_alert se existir; caso contrário, passa
    try:
        if send_alert:
            send_alert("BLOCK_BREACH", lines)
    except Exception:
        pass


# ==================================
# Modo Estrito (Per-Trade) — MODO DEUS
# ==================================
def enforce_per_trade_risk(reader: RiskGuardMT5Reader, max_risk_pct: float = DEFAULT_MAX_RISK_PCT) -> Dict[str, Any]:
    """
    Regras (independe de AutoTrading):
      - SEM SL  -> fecha
      - RISCO > max_risk_pct -> fecha
    Extra:
      - Se houver BLOQUEIO ATIVO e detectarmos posições, registramos "tentativa de burla"
        (cache local por ticket para não spammar).
    """
    snap = reader.snapshot()
    equity = float((snap["account"] or {}).get("equity") or 0.0)
    positions = list(snap.get("positions") or [])

    report: Dict[str, Any] = {
        "equity": equity,
        "limit_pct": max_risk_pct,
        "checked": 0,
        "violations": [],
        "closed": [],
        "failed": [],
        "skipped": [],
        "breach_logged": []
    }

    # 1) Se existe bloqueio ativo, considerar qualquer posição como potencial burla
    blk = _block_active()
    if blk.get("any_block") and positions:
        cache = _load_cache()
        already = set(cache.get("tickets_reported", []))
        new_breaches = []

        for p in positions:
            tk = int(p.get("ticket", 0))
            if tk and tk not in already:
                new_breaches.append(tk)

        if new_breaches:
            lines = ["🚫 Tentativa de operar durante BLOQUEIO detectada:"]
            for p in positions:
                if int(p.get("ticket", 0)) in new_breaches:
                    lines.append(f"• {p.get('symbol')} #{p.get('ticket')} vol={p.get('volume')} side={p.get('type')}")

            # Logar + notificar uma única vez por ticket
            log_event("BLOCK_BREACH", {
                "tickets": new_breaches,
                "block_state": blk
            }, context={"module": "guard"})

            _notify_breach(lines)

            cache["tickets_reported"] = list(already.union(new_breaches))
            _save_cache(cache)
            report["breach_logged"] = new_breaches

    # 2) Per-trade — fechar violações
    for pos in positions:
        report["checked"] += 1
        ticket = int(pos.get("ticket", 0))
        symbol = pos.get("symbol")
        side = pos.get("type")
        vol = float(pos.get("volume") or 0.0)

        # SL real (manual ou EA)
        sl = pos.get("sl") or pos.get("sl_price") or 0.0
        missing_sl = (sl is None or sl == 0 or sl == 0.0)

        # risco (se o reader não calcular, apenas o sem SL já fecha)
        risk_pct = pos.get("risk_pct")
        violates = missing_sl or (isinstance(risk_pct, (int, float)) and risk_pct > max_risk_pct + 1e-9)

        if not violates:
            report["skipped"].append({"ticket": ticket, "symbol": symbol})
            continue

        report["violations"].append({
            "ticket": ticket,
            "symbol": symbol,
            "side": side,
            "volume": vol,
            "missing_sl": missing_sl,
            "risk_pct": risk_pct
        })

        ok, res = close_position_full(ticket, symbol, side, vol)
        if ok:
            report["closed"].append({"ticket": ticket, "symbol": symbol, "result": res})
        else:
            report["failed"].append({"ticket": ticket, "symbol": symbol, "result": res})

    return report


# ==================================
# Execução direta (teste)
# ==================================
if __name__ == "__main__":
    TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    reader = RiskGuardMT5Reader(path=TERMINAL_PATH)
    if not reader.connect():
        print("mt5.last_error():", mt5.last_error())
        raise SystemExit("Falha ao iniciar MT5 (path/terminal/credenciais).")

    try:
        rep = enforce_per_trade_risk(reader, max_risk_pct=DEFAULT_MAX_RISK_PCT)
        # Logs resumidos
        log_event("PER_TRADE", {
            "equity": rep.get("equity"),
            "violations": rep.get("violations"),
            "closed": rep.get("closed"),
            "failed": rep.get("failed"),
            "breach_logged": rep.get("breach_logged")
        }, context={"module": "guard"})

        # Notificação tradicional
        try:
            notify_per_trade(rep)
        except Exception:
            pass

        from pprint import pprint
        pprint(rep)
    finally:
        reader.shutdown()

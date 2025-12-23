# kill_switch.py — com reativação automática do AutoTrading
from __future__ import annotations
from typing import Optional, Dict, Any
from datetime import datetime
import os, json, time
import pytz

import MetaTrader5 as mt5
import win32gui
import win32api
import win32con


HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".riskguard_state.json")

# ------------------ STATE FILE ------------------
def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def _save_state(data: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ------------------ WINDOW TOGGLE ------------------
def _toggle_autotrade(new_state: bool):
    """new_state=True ativa / new_state=False desativa"""
    if not mt5.initialize():
        return False

    account = mt5.account_info()
    info = mt5.terminal_info()
    if not account:
        mt5.shutdown()
        return False

    # se já está no estado desejado → nada a fazer
    if info.trade_allowed == new_state:
        mt5.shutdown()
        return True

    login_str = str(account.login)
    server = account.server

    def enum_callback(hwnd, results):
        title = win32gui.GetWindowText(hwnd)
        if title and login_str in title and server in title:
            results.append(hwnd)

    hwnds = []
    win32gui.EnumWindows(enum_callback, hwnds)

    if not hwnds:
        mt5.shutdown()
        return False

    hwnd = hwnds[0]

    # Comando padrão real do MT5 para toggle AutoTrading
    win32api.PostMessage(hwnd, win32con.WM_COMMAND, 32851, 0)
    time.sleep(0.6)

    mt5.shutdown()
    mt5.initialize()
    info2 = mt5.terminal_info()

    mt5.shutdown()
    return info2.trade_allowed == new_state

# ------------------ PUBLIC API ------------------
def set_kill_until(dt_utc):
    state = _load_state()
    state["autotrade_disabled_until"] = dt_utc.astimezone(pytz.UTC).isoformat()
    _save_state(state)
    print(f"[KILL SWITCH] Bloqueando AutoTrading até {state['autotrade_disabled_until']}")
    _toggle_autotrade(False)  # DESATIVAR AGORA

def kill_status(now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    if now_utc is None:
        now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    state = _load_state()
    until_iso = state.get("autotrade_disabled_until")
    if not until_iso:
        return {"active": False, "until": None, "remaining_sec": None}
    try:
        # Formato robusto (aceita Z ou +00:00)
        if until_iso.endswith("Z"):
            until = datetime.fromisoformat(until_iso.replace("Z", "+00:00"))
        else:
            until = datetime.fromisoformat(until_iso)
    except:
        return {"active": False, "until": None, "remaining_sec": None}

    remaining = (until - now_utc).total_seconds()
    return {
        "active": remaining > 0,
        "until": until_iso,
        "remaining_sec": max(0.0, remaining)
    }

def maybe_reenable_autotrade():
    status = kill_status()
    if (not status["active"]) and status["until"] is not None:
        if _toggle_autotrade(True):
            print("[KILL SWITCH] ✅ AutoTrading REATIVADO automaticamente.")
            state = _load_state()
            state["autotrade_disabled_until"] = None
            _save_state(state)
            return True
    return False

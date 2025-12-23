# uia.py — toggle do AutoTrading via atalho (Ctrl+E) com foco no MT5
from __future__ import annotations
import time

def _focus_mt5():
    from pywinauto import Application
    app = Application(backend="uia").connect(title_re=".*MetaTrader 5.*")
    win = app.top_window()
    win.set_focus()
    return app, win

def _press_ctrl_e(times: int = 1, delay: float = 0.25):
    from pywinauto import keyboard
    for _ in range(times):
        keyboard.send_keys("^e")
        time.sleep(delay)

def ensure_autotrading_on(max_tries: int = 4, delay: float = 0.3) -> bool:
    """
    Tenta ligar o AutoTrading por hotkey (Ctrl+E), sem depender de ler o estado UI.
    Estratégia: focar MT5 -> ^E -> aguarda -> retorna True se conseguiu focar/enviar hotkey.
    O estado final será validado do lado do guard pela ausência do retcode 10027/10028.
    """
    try:
        _focus_mt5()
    except Exception:
        return False
    ok = False
    for _ in range(max_tries):
        _press_ctrl_e()
        time.sleep(delay)
        ok = True
        # Não checamos UI aqui; quem valida é a tentativa de order_send no guard
        break
    return ok

def ensure_autotrading_off(max_tries: int = 2, delay: float = 0.3) -> bool:
    """Desliga com ^E (1–2 toques), sem ler estado UI."""
    try:
        _focus_mt5()
    except Exception:
        return False
    _press_ctrl_e(times=1, delay=delay)
    return True

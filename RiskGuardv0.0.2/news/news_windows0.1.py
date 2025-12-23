from __future__ import annotations
from typing import Any, Dict, List, Set
from datetime import datetime, timedelta
import os, sys, time, json, argparse
import pandas as pd
import pytz


# ---------- Ajuste de PATH ----------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# ------------------------------------

from rg_config import get_int, get_optional_int
from mt5_reader import RiskGuardMT5Reader
from limits.guard import close_position_full
from limits.kill_switch import set_kill_until, maybe_reenable_autotrade, kill_status
from notify import notify_news
from logger.logger import log_event
from limits.uia import ensure_autotrading_on, ensure_autotrading_off


CACHE_FILE = os.path.join(HERE, "ff_cache.json")
DEBUG_MODE = True
LAST_AUTOTRADE_REENABLE = 0.0
LAST_CALENDAR_UPDATE_DAY = None
DEFAULT_NEWS_WINDOW_MINUTES = get_int("NEWS_WINDOW_MINUTES", 60)
DEFAULT_NEWS_RECENT_SECONDS = get_optional_int("NEWS_RECENT_SECONDS", None)

BROKER_UTC_OFFSET_HOURS = 2  # Ajuste após verificar MT5


def debug(msg: str):
    if DEBUG_MODE:
        now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
        server_time = now_utc + timedelta(hours=BROKER_UTC_OFFSET_HOURS)
        ts = server_time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[NEWS_DEBUG] {ts} | {msg}", flush=True)


# ============================================================
# CARREGA CALENDÁRIO
# ============================================================
def load_cached_calendar(max_age_days: int = 7) -> pd.DataFrame | None:
    if not os.path.exists(CACHE_FILE):
        debug("Cache inexistente. Rode update_news.py primeiro.")
        return None

    try:
        data = json.load(open(CACHE_FILE, "r", encoding="utf-8"))
        rows = data.get("events", [])
        for r in rows:
            if r.get("ts_utc"):
                r["ts_utc"] = datetime.fromisoformat(r["ts_utc"])

        df = pd.DataFrame(rows)
        if df.empty:
            debug("Cache vazio.")
            return None

        df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
        debug(f"Cache carregado: {len(df)} eventos.")

        return df.sort_values("ts_utc")

    except Exception as e:
        debug(f"Erro lendo cache: {e!r}")
        return None


# ============================================================
# LÓGICA DAS MOEDAS
# ============================================================
def map_symbol_currencies(symbol: str) -> Set[str]:
    s = (symbol or "").upper()
    if len(s) >= 6 and s[:3].isalpha() and s[3:6].isalpha():
        return {s[:3], s[3:6]}
    return {s[-3:]}


def find_events(df: pd.DataFrame, currencies: Set[str], now_utc: datetime, window_min: int = DEFAULT_NEWS_WINDOW_MINUTES):
    if df is None or df.empty:
        return []
    lo = now_utc - timedelta(minutes=window_min)
    hi = now_utc + timedelta(minutes=window_min)
    return [
        r for _, r in df.iterrows()
        if r["currency"] in currencies and lo <= r["ts_utc"] <= hi
    ]


# ============================================================
# FUNÇÃO PRINCIPAL: FECHAR ORDENS
# ============================================================
def enforce_news_window(
    reader: RiskGuardMT5Reader,
    events_df: pd.DataFrame,
    window_min: int = DEFAULT_NEWS_WINDOW_MINUTES,
    recent_s: int | None = DEFAULT_NEWS_RECENT_SECONDS
):
    """
    Fecha ordens durante notícia, MAS NÃO DESLIGA AUTOTRADE AQUI.
    Isso é feito pelo run_daemon após confirmar que tudo fechou.
    """

    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    if recent_s is None:
        recent_s = window_min * 60

    try:
        snap = reader.snapshot()
    except Exception:
        debug("MT5 indisponível — aguardando…")
        time.sleep(2)
        return {"affected": [], "closed": [], "failed": [], "kill_switch_until": None}

    positions = snap.get("positions", [])
    report = {"affected": [], "closed": [], "failed": [], "kill_switch_until": None}

    for pos in positions:
        try:
            ticket = int(pos.get("ticket", -1))
            symbol = str(pos.get("symbol", ""))
            side = pos.get("type")
            volume = float(pos.get("volume", 0.0))
            open_time_raw = pos.get("open_time")

            if not symbol or not open_time_raw:
                continue

            open_time = pd.to_datetime(open_time_raw, utc=True)
            if (now_utc - open_time).total_seconds() > recent_s:
                continue

            # moedas
            ccy = map_symbol_currencies(symbol)
            matches = find_events(events_df, ccy, now_utc, window_min)
            if not matches:
                continue

            debug(f"[NEWS] Ordem afetada: {symbol} ticket {ticket}")

            # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
            # NOVO: GARANTIR AUTOTRADE ON ANTES DE FECHAR
            # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
            debug("Garantindo AutoTrading ON para permitir fechamento…")
            ensure_autotrading_on()
            time.sleep(0.4)

            ok, res = close_position_full(ticket, symbol, side, volume, comment="RG NewsBlock")

            if ok:
                report["closed"].append({"ticket": ticket, "symbol": symbol})
            else:
                report["failed"].append({"ticket": ticket, "symbol": symbol, "res": res})

            until = max(m["ts_utc"] for m in matches) + timedelta(minutes=window_min)
            report["kill_switch_until"] = until.isoformat()
            set_kill_until(until)

            report["affected"].append({
                "ticket": ticket,
                "symbol": symbol,
                "matches": [dict(m) for m in matches]
            })

        except Exception as e:
            debug(f"Erro enforce: {e}")
            continue

    return report


# ============================================================
# AUTO UPDATE DO CALENDÁRIO
# ============================================================
def auto_update_calendar():
    import subprocess
    global LAST_CALENDAR_UPDATE_DAY

    now_utc = datetime.utcnow()
    today = now_utc.date()
    weekday = today.weekday()  # 6 = domingo

    if weekday == 6 and LAST_CALENDAR_UPDATE_DAY != today:
        updater = os.path.join(HERE, "update_news.py")
        if os.path.exists(updater):
            subprocess.run([sys.executable, updater], check=False)
            LAST_CALENDAR_UPDATE_DAY = today


# ============================================================
# DAEMON PRINCIPAL
# ============================================================
def run_daemon(mt5_path: str, poll_s: int = 3, cal_refresh_min: int = 10):

    global LAST_AUTOTRADE_REENABLE
    debug(f"Iniciando monitor — MT5={mt5_path}")

    reader = RiskGuardMT5Reader(path=mt5_path)
    if not reader.connect():
        debug("Falha ao conectar MT5.")
        sys.exit(2)

    last_load = 0.0
    events_df = None

    while True:
        debug("Loop vivo…")

        # 1 — Regate automático quando KillSwitch expira
        state = kill_status()
        if state["until"] is not None:
            if maybe_reenable_autotrade():
                LAST_AUTOTRADE_REENABLE = time.time()

        if LAST_AUTOTRADE_REENABLE and time.time() - LAST_AUTOTRADE_REENABLE < 3:
            time.sleep(1)
            continue

        try:
            now = time.time()

            auto_update_calendar()

            # Recarregar calendário
            if now - last_load > cal_refresh_min * 60:
                events_df = load_cached_calendar()
                last_load = now

            if events_df is not None:
                report = enforce_news_window(reader, events_df)

                # Se houve ordens afetadas, aguardar até todas sumirem
                if report.get("affected"):
                    debug("Notícia detectada — aguardando fechamento total…")

                    affected_tickets = {item["ticket"] for item in report["affected"]}

                    while True:
                        snap2 = reader.snapshot()
                        alive = {
                            int(p["ticket"])
                            for p in snap2.get("positions", [])
                        }

                        remaining = affected_tickets.intersection(alive)

                        if not remaining:
                            break

                        debug(f"Aguardando… ordens restantes: {remaining}")
                        time.sleep(1)

                    debug("TODAS AS ORDENS FORAM ENCERRADAS.")

                    # DESLIGAR AUTOTRADE APÓS FECHAR TUDO
                    debug("Desligando AutoTrading…")
                    ensure_autotrading_off()

                    debug("Notificando Telegram…")
                    notify_news(report)

        except Exception as e:
            debug(f"Erro principal: {e}")
            time.sleep(2)

        time.sleep(poll_s)


# ============================================================
# MAIN
# ============================================================
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mt5-path", default=r"C:\\Program Files\\MetaTrader 5\\terminal64.exe")
    p.add_argument("--poll", type=int, default=3)
    p.add_argument("--refresh", type=int, default=10)
    args = p.parse_args()
    run_daemon(args.mt5_path, poll_s=args.poll, cal_refresh_min=args.refresh)


if __name__ == "__main__":
    auto_update_calendar()
    main()

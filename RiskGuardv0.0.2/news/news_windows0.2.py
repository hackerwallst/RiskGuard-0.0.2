from __future__ import annotations
from typing import Any, Dict, List, Set
from datetime import datetime, timedelta
import os, sys, time, json, argparse
import pandas as pd
import pytz
import MetaTrader5 as mt5  # só para debug de ambiente


# ---------- Ajuste de PATH para enxergar a raiz do projeto ----------
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # pasta acima de /news

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# -------------------------------------------------------------------

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
LAST_CALENDAR_UPDATE_DAY = None  # <- novo
DEFAULT_NEWS_WINDOW_MINUTES = get_int("NEWS_WINDOW_MINUTES", 60)
DEFAULT_NEWS_RECENT_SECONDS = get_optional_int("NEWS_RECENT_SECONDS", None)

# Offset fixo entre UTC e horário do servidor da corretora.
# Exemplo: se no MT5 aparecer:
#   TimeTradeServer = 19:17
#   TimeGMT        = 17:17
# então o servidor está em UTC+2 -> use 2 aqui.
BROKER_UTC_OFFSET_HOURS = 2  # <-- AJUSTA AQUI depois de ver no MT5


def debug(msg: str):
    if DEBUG_MODE:
        # agora o timestamp do LOG vai sair no horário da corretora (server),
        # usando um offset fixo em relação ao UTC
        now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
        server_time = now_utc + timedelta(hours=BROKER_UTC_OFFSET_HOURS)
        ts = server_time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[NEWS_DEBUG] {ts} | {msg}", flush=True)


# ============================================================
# Ler o cache local do ForexFactory
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
        for _, r in df.iterrows():
            ts_utc = r["ts_utc"]
            ts_server = ts_utc + timedelta(hours=BROKER_UTC_OFFSET_HOURS)
            debug(
                f"Evento: {r['currency']} | {r['event']} | "
                f"{ts_utc} (UTC) -> {ts_server} (SERVER)"
            )
        return df.sort_values("ts_utc")
    except Exception as e:
        debug(f"Erro lendo cache: {e!r}")
        return None


# ============================================================
# Lógica principal de bloqueio
# ============================================================
def map_symbol_currencies(symbol: str) -> Set[str]:
    s = (symbol or "").upper()
    if len(s) >= 6 and s[:3].isalpha() and s[3:6].isalpha():
        return {s[:3], s[3:6]}
    return {s[-3:]}


def find_events(df: pd.DataFrame, currencies: Set[str], now_utc: datetime,
                window_min: int = DEFAULT_NEWS_WINDOW_MINUTES):
    if df is None or df.empty:
        return []
    lo = now_utc - timedelta(minutes=window_min)
    hi = now_utc + timedelta(minutes=window_min)
    return [
        r for _, r in df.iterrows()
        if r["currency"] in currencies and lo <= r["ts_utc"] <= hi
    ]


def enforce_news_window(reader: RiskGuardMT5Reader,
                        events_df: pd.DataFrame,
                        window_min: int = DEFAULT_NEWS_WINDOW_MINUTES,
                        recent_s: int | None = DEFAULT_NEWS_RECENT_SECONDS):
    """
    Fecha todas as ordens abertas em janela de notícia e,
    SÓ DEPOIS, aciona o kill-switch + notifica no Telegram.
    """

    now_utc = datetime.utcnow().replace(tzinfo=pytz.UTC)
    if recent_s is None:
        recent_s = window_min * 60

    try:
        snap = reader.snapshot()
    except Exception:
        debug("MT5 indisponível (AutoTrade OFF ou terminal travado). Aguardando…")
        time.sleep(2)
        return {"affected": [], "closed": [], "failed": [], "kill_switch_until": None}

    positions = snap.get("positions", []) or []
    report: Dict[str, Any] = {
        "affected": [],
        "closed": [],
        "failed": [],
        "kill_switch_until": None,
    }

    max_until: datetime | None = None

    for pos in positions:
        debug(f"Analisando posição recebida: {pos}")
        try:
            # --- Primeiro, validação básica ---
            if not isinstance(pos, dict):
                debug("Ignorada — posição não é dict.")
                continue

            # --- Obtém campos essenciais ---
            ticket = int(pos.get("ticket", -1))
            symbol = str(pos.get("symbol", ""))
            side = pos.get("type")
            volume = float(pos.get("volume", 0.0))

            if not symbol:
                debug("Ignorada — símbolo vazio.")
                continue

            # --- CONVERSÃO DO HORÁRIO DA ORDEM (100% UTC) ---
            open_time_raw = pos.get("open_time")
            if not open_time_raw:
                debug("Ignorada — open_time ausente.")
                continue

            # Tratamos sempre como UTC, alinhado com MT5/ticks e calendário
            open_time = pd.to_datetime(open_time_raw, utc=True)

            debug(f"Horário bruto da ordem: {open_time_raw}")
            debug(f"Horário UTC convertido: {open_time}")

            # posição só válida se aberta recentemente
            if (now_utc - open_time).total_seconds() > recent_s:
                debug(f"Ignorada — ordem antiga demais (>{recent_s}s).")
                continue

            # --- DETECÇÃO DE NOTÍCIA ---
            ccy = map_symbol_currencies(symbol)
            debug(f"Moedas do ativo {symbol}: {ccy}")
            matches = find_events(events_df, ccy, now_utc, window_min)
            if not matches:
                continue

            debug(f"[NEWS] Ordem afetada detectada: {symbol} ticket {ticket}")
            debug(f"[NEWS] Fechando ordem via close_position_full()…")

            # IMPORTANTE: aqui NÃO desligamos AutoTrading.
            # Apenas tentamos fechar a posição.
            ok, res = close_position_full(
                ticket,
                symbol,
                side,
                volume,
                comment="RG NewsBlock",
            )

            debug(f"[NEWS] Resultado do fechamento: ok={ok}, res={res}")

            if ok:
                report["closed"].append({"ticket": ticket, "symbol": symbol})
            else:
                report["failed"].append(
                    {"ticket": ticket, "symbol": symbol, "res": res}
                )

            this_until = max(m["ts_utc"] for m in matches) + timedelta(
                minutes=window_min
            )
            if (max_until is None) or (this_until > max_until):
                max_until = this_until

            report["affected"].append(
                {
                    "ticket": ticket,
                    "symbol": symbol,
                    "matches": [dict(m) for m in matches],
                }
            )

        except Exception as e:
            debug(f"Erro enforce: {repr(e)}")
            continue

    # Só depois de tentar fechar TODAS as ordens afetadas,
    # ativamos o kill-switch e notificamos no Telegram.
    if report["affected"] and max_until is not None:
        set_kill_until(max_until)
        debug(f"AutoTrade pausado até {max_until} (kill-switch).")
        report["kill_switch_until"] = max_until.isoformat()

        # Notificação única consolidada
        debug("[NEWS] Enviando notificação Telegram (notify_news)…")
        try:
            notify_news(report)
        except Exception as e:
            debug(f"Erro ao notificar Telegram (notify_news): {repr(e)}")

    return report


def auto_update_calendar():
    """
    Roda update_news.py automaticamente 1x por domingo (horário UTC).
    """
    import subprocess  # importa local para não mexer nos imports globais
    global LAST_CALENDAR_UPDATE_DAY

    now_utc = datetime.utcnow()
    today = now_utc.date()
    weekday = today.weekday()  # 0=segunda, 6=domingo

    # Só roda aos domingos e apenas uma vez por dia
    if weekday == 6 and LAST_CALENDAR_UPDATE_DAY != today:
        updater = os.path.join(HERE, "update_news.py")

        if os.path.exists(updater):
            print("[AUTOUPDATE] Domingo detectado — atualizando calendário ForexFactory...")
            try:
                subprocess.run([sys.executable, updater], check=False)
                LAST_CALENDAR_UPDATE_DAY = today
                print("[AUTOUPDATE] Atualização concluída.")
            except Exception as e:
                print(f"[AUTOUPDATE] Erro ao rodar update_news.py: {e!r}")
        else:
            print("[AUTOUPDATE] update_news.py não encontrado.")


# ============================================================
# Daemon: monitora continuamente
# ============================================================
def run_daemon(mt5_path: str, poll_s: int = 3, cal_refresh_min: int = 10):
    global LAST_AUTOTRADE_REENABLE
    debug(f"Iniciando monitor de notícias (MT5={mt5_path})")
    reader = RiskGuardMT5Reader(path=mt5_path)
    if not reader.connect():
        debug("Falha ao conectar MT5.")
        sys.exit(2)

    # DEBUG inicial do ambiente MT5
    debug("========== DEBUG MT5 ENV INICIAL ==========")
    debug(f"MT5 path (reader.path): {reader.path}")
    try:
        snap0 = reader.snapshot()
        debug(f"Snapshot keys: {list(snap0.keys())}")
        acc0 = snap0.get("account", {}) or {}
        debug(
            f"Conta MT5 (snapshot): login={acc0.get('login')} | "
            f"name={acc0.get('name')} | balance={acc0.get('balance')} | "
            f"equity={acc0.get('equity')}"
        )
        pos0 = snap0.get("positions", []) or []
        debug(f"Total de posições abertas no snapshot: {len(pos0)}")
        syms0 = {p.get("symbol") for p in pos0 if isinstance(p, dict)}
        debug(f"Símbolos com posição aberta: {syms0}")
    except Exception as e:
        debug(f"Erro snapshot inicial: {repr(e)}")

    try:
        ti = mt5.terminal_info()
        debug(f"mt5.terminal_info(): {ti}")
    except Exception as e:
        debug(f"Erro terminal_info(): {repr(e)}")

    try:
        ai = mt5.account_info()
        debug(f"mt5.account_info(): {ai}")
    except Exception as e:
        debug(f"Erro account_info(): {repr(e)}")

    try:
        si = mt5.symbol_info("EURUSD")
        debug(f"mt5.symbol_info('EURUSD'): {si}")
    except Exception as e:
        debug(f"Erro symbol_info('EURUSD'): {repr(e)}")

    debug("======== FIM DEBUG MT5 ENV INICIAL ========")

    last_load = 0.0
    events_df = None
    while True:
        debug("Loop vivo… verificando notícias e ordens.")

        # 1 — REENGATE DO AUTOTRADE (usar o kill_switch oficial)
        state = kill_status()

        # Só tenta religar SE existe kill configurado no JSON
        if state["until"] is not None:
            reenabled = maybe_reenable_autotrade()
            if reenabled:
                LAST_AUTOTRADE_REENABLE = time.time()

        # 2 — Anti-flood: aguardar MT5 voltar ao normal após religar AutoTrade
        if LAST_AUTOTRADE_REENABLE and time.time() - LAST_AUTOTRADE_REENABLE < 3:
            debug("Aguardando MT5 estabilizar após religar AutoTrade…")
            time.sleep(1)
            continue

        try:
            t0 = time.time()

            # 0 — Atualização automática do calendário aos domingos
            auto_update_calendar()

            # 1 — Recarrega o cache a cada X minutos
            if (t0 - last_load) > cal_refresh_min * 60:
                events_df = load_cached_calendar()
                last_load = t0

            # 2 — Aplica a lógica de bloqueio de notícias
            if events_df is not None:
                enforce_news_window(reader, events_df)

        except Exception as e:
            debug(f"Erro no loop principal: {e!r}")
            time.sleep(2)

        time.sleep(poll_s)


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

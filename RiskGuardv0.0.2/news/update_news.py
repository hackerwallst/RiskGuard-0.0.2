# update_news.py — Atualiza o cache semanal de eventos ForexFactory (ff_cache.json)
# ==============================================================================
# Este script NÃO usa o MetaTrader.
# Deve ser executado 1x por semana (ex: domingo à noite)
# Ele baixa todos os eventos da semana e salva em formato JSON compatível
# com o RiskGuard / news_windows.py.
# ==============================================================================
from datetime import datetime, timedelta
import os, json, time, random
import pandas as pd
import pytz, requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import List, Dict


HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(HERE, "ff_cache.json")

def _df_from_raw_ff(raw) -> pd.DataFrame:

    rows = []
    for ev in raw:
        try:
            raw_date = str(ev.get("date", ""))  # ex: "2025-11-19T17:00:00+00:00"

            # Parse normal (vem como +00, mas é horário de New York disfarçado)
            # Lê o horário exatamente como o JSON entrega
            local_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))

            # Converte para UTC corretamente
            ts_utc = local_dt.astimezone(pytz.UTC)

            rows.append({
                "id": ev.get("id"),
                "currency": str(ev.get("country", "")).upper(),
                "importance": str(ev.get("impact", "")).lower(),
                "event": ev.get("title", ""),
                "ts_utc": ts_utc,
            })
        except Exception:
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[df["importance"].isin({"medium", "high"})].copy()
    return df.sort_values("ts_utc").reset_index(drop=True)

def fetch_ff_calendar(max_retries: int = 3, timeout_s: int = 8) -> pd.DataFrame:
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    s = requests.Session()
    retry = Retry(total=max_retries, backoff_factor=1.0,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(["GET"]))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    headers = {
        "User-Agent": "RiskGuard/1.0 (+update_news)",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    for i in range(max_retries):
        try:
            print(f"[update_news] Baixando calendário ForexFactory (tentativa {i+1})...")
            r = s.get(url, headers=headers, timeout=timeout_s)
            r.raise_for_status()
            raw = r.json()
            df = _df_from_raw_ff(raw)
            print(f"[update_news] OK: {len(df)} eventos medium/high.")
            return df
        except Exception as e:
            wait = (2 ** i) + random.random()
            print(f"[update_news] Falha {e!r}. Repetindo em {wait:.1f}s...")
            time.sleep(wait)
    raise RuntimeError("Falha ao baixar calendário ForexFactory após retries.")

def save_cache(df: pd.DataFrame):
    if df.empty:
        print("[update_news] Nenhum evento para salvar.")
        return False
    payload = []
    for _, r in df.iterrows():
        payload.append({
            "id": r.get("id"),
            "currency": r.get("currency"),
            "importance": r.get("importance"),
            "event": r.get("event"),
            "ts_utc": r.get("ts_utc").isoformat() if pd.notna(r.get("ts_utc")) else None
        })
    out = {
        "saved_at": datetime.utcnow().isoformat(),
        "events": payload
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[update_news] Cache salvo em {CACHE_FILE} ({len(payload)} eventos).")
    return True

def main():
    print("[update_news] Iniciando atualização semanal de calendário ForexFactory...")
    try:
        df = fetch_ff_calendar()
        save_cache(df)
        print("[update_news] Finalizado com sucesso.")
    except Exception as e:
        print(f"[update_news] ERRO FATAL: {e!r}")

if __name__ == "__main__":
    main()

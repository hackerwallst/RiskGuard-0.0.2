# reports.py — Função 7: Relatório de Performance (MT5 + Logs RiskGuard)
from __future__ import annotations

# --- imports base ---
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
import argparse
import json
import csv
import os
from collections import defaultdict
import calendar

# --- sys.path para importar módulos da pasta raiz (Notify.py, mt5_reader.py, logger.py) ---
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- imports do projeto ---
from notify import set_ident_from_snapshot, notify_report, send_document  # usa Notify.py (N maiúsculo)
from rg_config import get_float
import MetaTrader5 as mt5
from mt5_reader import RiskGuardMT5Reader
from logger import log_path_current
from mc import simulate_paths, summarize_paths, mc_fig_fanchart, mc_fig_dd_hist, mc_table
import numpy as np

# === RG: helpers de fluxos (depósitos/retiradas) — versão completa ===
def _rg_extract_flows_from_deals(deals):
    """
    Detecta depósitos e retiradas com heurísticas:
    - type == BALANCE (dep/saque)
    - type == CREDIT (depósito)
    - volume == 0 e symbol vazio (balance manual)
    - comentário com 'deposit', 'withdraw' etc.
    """
    from collections import defaultdict
    import MetaTrader5 as mt5

    dep = defaultdict(float)
    wdr = defaultdict(float)
    KW_DEP = ("deposit", "credit", "bonus", "add", "fund")
    KW_WDR = ("withdraw", "payout", "remove", "sub", "out")

    for d in deals:
        typ = int(d.get("type", -999))
        sym = (d.get("symbol") or "").strip().lower()
        cmt = (d.get("comment") or "").lower()
        vol = float(d.get("volume", 0.0))
        amt = float(d.get("profit", 0.0))
        day = (d.get("time") or "")[:10]
        if not day:
            continue

        # 1. Tipos diretos
        if typ in (mt5.DEAL_TYPE_BALANCE, mt5.DEAL_TYPE_CREDIT):
            if amt > 0:
                dep[day] += amt
            elif amt < 0:
                wdr[day] += abs(amt)
            continue

        # 2. Volume zero e símbolo vazio
        if vol == 0 and not sym:
            if amt > 0:
                dep[day] += amt
            elif amt < 0:
                wdr[day] += abs(amt)
            continue

        # 3. Comentário detectado
        if any(k in cmt for k in KW_DEP) and amt > 0:
            dep[day] += amt
        elif any(k in cmt for k in KW_WDR) and amt < 0:
            wdr[day] += abs(amt)

    return sorted(dep.items()), sorted(wdr.items())


# === RG: helpers para curva de equity ===
def _rg_make_equity_series(trades, equity_now, net_pnl):
    """
    trades: lista de dicts com pelo menos {"end": "...", "pnl": float}
    equity_now: equity atual
    net_pnl: pnl líquido do período (para inferir equity inicial)
    Retorna: [(label, equity), ...]
    """
    try:
        start_equity = float(equity_now or 0.0) - float(net_pnl or 0.0)
    except:
        start_equity = 0.0

    # usa a data/hora de fechamento do trade: campo 'end'
    rows = sorted(trades, key=lambda t: t.get("end",""))
    eq = []
    acc = start_equity
    last_label = None
    for t in rows:
        acc += float(t.get("pnl", 0.0))
        ct = str(t.get("end",""))
        label = ct.split("T")[0] if "T" in ct else ct.split(" ")[0]
        if not label:
            label = last_label or "?"
        eq.append((label, round(acc, 2)))
        last_label = label

    if not eq:
        eq = [("—", float(equity_now or 0.0))]
    return eq

# === RG: detectores de fluxos (depósitos/retiradas) no histórico completo ===
def _rg_fetch_all_flows(until_dt: datetime):
    """
    Lê TODO o histórico de deals desde 2000-01-01 até 'until_dt' e retorna
    duas listas [(YYYY-MM-DD, +valor)], uma para depósitos e outra para saques.
    """
    import MetaTrader5 as mt5
    from collections import defaultdict

    since_dt = datetime(2000, 1, 1, tzinfo=timezone.utc)  # início bem amplo
    deals = mt5.history_deals_get(since_dt, until_dt)
    dep = defaultdict(float)
    wdr = defaultdict(float)
    if not deals:
        return [], []

    # palavras-chave de fallback (casos onde o broker usa comment)
    KW_DEP = ("deposit", "credit", "bonus", "add", "fund", "topup", "refill", "in")
    KW_WDR = ("withdraw", "payout", "remove", "sub", "out", "wd")

    for d in deals:
        typ = int(getattr(d, "type", -999))
        amt = float(getattr(d, "profit", 0.0))
        vol = float(getattr(d, "volume", 0.0))
        sym = (getattr(d, "symbol", "") or "").strip().lower()
        cmt = (getattr(d, "comment", "") or "").strip().lower()
        ts  = datetime.fromtimestamp(getattr(d, "time", 0), tz=timezone.utc)
        day = ts.date().isoformat()

        # 1) tipos oficiais
        # BALANCE: depósito/saque; CREDIT: tratamos como depósito
        if typ in (mt5.DEAL_TYPE_BALANCE, mt5.DEAL_TYPE_CREDIT):
            if amt >= 0:
                dep[day] += amt
            else:
                wdr[day] += abs(amt)
            continue

        # 2) heurística: volume 0 e símbolo vazio costumam ser operações de saldo
        if vol == 0 and not sym and amt != 0:
            if amt > 0:
                dep[day] += amt
            else:
                wdr[day] += abs(amt)
            continue

        # 3) fallback por comentário
        if any(k in cmt for k in KW_DEP) and amt > 0:
            dep[day] += amt
        elif any(k in cmt for k in KW_WDR) and amt < 0:
            wdr[day] += abs(amt)

    return sorted(dep.items()), sorted(wdr.items())


def _rg_filter_flows_to_period(deposits, withdrawals, since_dt: datetime, until_dt: datetime):
    """
    Filtra listas [(YYYY-MM-DD, valor+)] para o intervalo [since_dt, until_dt)
    """
    def _keep(day_str):
        try:
            d = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return since_dt <= d < until_dt
        except:
            return False
    deps = [(d, v) for (d, v) in deposits if _keep(d)]
    wdrs = [(d, v) for (d, v) in withdrawals if _keep(d)]
    return deps, wdrs


# === RG: alinhar equity com dias do período e com fluxos ===
def _rg_daily_equity(eq_points, since_dt, until_dt):
    """
    Recebe [(YYYY-MM-DD, equity)] (em dias com trade) e preenche TODOS os dias do período.
    Forward-fill do último equity conhecido.
    """
    from datetime import timedelta, datetime
    # normaliza pontos (somente datas)
    pts = [(p[0].split("T")[0] if isinstance(p[0], str) else str(p[0]), float(p[1])) for p in (eq_points or [])]
    pts.sort(key=lambda x: x[0])
    last_val = pts[0][1] if pts else 0.0
    eq_by_day = {d: v for d, v in pts}

    out = []
    d = since_dt.date()
    end = (until_dt - timedelta(days=1)).date()  # until é exclusivo
    while d <= end:
        key = d.isoformat()
        if key in eq_by_day:
            last_val = eq_by_day[key]
        out.append((key, last_val))
        d += timedelta(days=1)
    return out


def _max_drawdown_stats(eq_points):
    """
    Calcula DD máximo pela curva de equity.
    Retorna (dd_abs, dd_pct, peak_equity, trough_equity, peak_label, trough_label)
    """
    peak = None
    dd_abs = 0.0
    peak_label = trough_label = ""
    peak_equity = trough_equity = 0.0
    for lab, val in eq_points:
        if peak is None or val > peak:
            peak = val
            peak_label = lab
            trough_equity = val
            trough_label = lab
        if peak - val > dd_abs:
            dd_abs = peak - val
            trough_equity = val
            trough_label = lab
    dd_pct = (dd_abs / (peak or 1.0)) * 100.0
    return round(dd_abs, 2), round(dd_pct, 2), peak or 0.0, trough_equity, peak_label, trough_label

# === RG: fluxos (depósitos/retiradas) olhando janela ampla ===
def _rg_extract_flows_wide_window(since: datetime, until: datetime):
    """
    Varre uma janela mais ampla (since-400d .. until) para achar depósitos/saques,
    e depois filtra os eventos para [since, until).
    Retorna: (deposits[ (YYYY-MM-DD, +valor) ], withdrawals[ (YYYY-MM-DD, +valor) ])
    """
    import MetaTrader5 as mt5
    from collections import defaultdict, Counter

    wide_since = since - timedelta(days=400)
    deals = mt5.history_deals_get(wide_since, until)
    dep = defaultdict(float)
    wdr = defaultdict(float)

    if not deals:
        return [], []

    # Palavras-chave para fallback em comentários (ajustaremos se necessário)
    KW_DEP = ("deposit", "credit", "bonus", "add", "fund", "topup", "refill", "in")
    KW_WDR = ("withdraw", "payout", "remove", "sub", "out", "wd")

    # DEBUG opcional: mapear comentários mais comuns (sem volume)
    # (se quiser, descomente estas 3 linhas)
    # cmt_counter = Counter((getattr(d, "comment", "") or "").strip().lower() for d in deals if getattr(d, "volume", 0.0) == 0)
    # top_cmts = cmt_counter.most_common(8)
    # print("[DEBUG] top balance comments:", top_cmts)

    for d in deals:
        typ = int(getattr(d, "type", -999))
        amt = float(getattr(d, "profit", 0.0))
        vol = float(getattr(d, "volume", 0.0))
        sym = (getattr(d, "symbol", "") or "").strip().lower()
        cmt = (getattr(d, "comment", "") or "").strip().lower()
        ts  = datetime.fromtimestamp(getattr(d, "time", 0), tz=timezone.utc)
        day = ts.date().isoformat()

        # Detectores:
        if typ in (mt5.DEAL_TYPE_BALANCE, mt5.DEAL_TYPE_CREDIT):
            # CREDIT tratamos como "depósito"
            if since <= ts < until:
                if amt >= 0:
                    dep[day] += amt
                else:
                    wdr[day] += abs(amt)
            continue

        # volume zero e símbolo vazio costumam ser operações de saldo
        if vol == 0 and not sym:
            if since <= ts < until:
                if amt > 0:
                    dep[day] += amt
                elif amt < 0:
                    wdr[day] += abs(amt)
            continue

        # fallback por comentário
        if since <= ts < until:
            if any(k in cmt for k in KW_DEP) and amt > 0:
                dep[day] += amt
            elif any(k in cmt for k in KW_WDR) and amt < 0:
                wdr[day] += abs(amt)

    return sorted(dep.items()), sorted(wdr.items())


# --- HTML/PDF (funciona rodando como módulo ou script)
try:
    # se "reports" for um pacote (tem __init__.py)
    from .render_html import render_html
    from .render_pdf import html_to_pdf
except ImportError:
    # se rodar direto: python reports/reports.py ...
    from render_html import render_html
    from render_pdf import html_to_pdf

# --- pastas de saída ---
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "reports"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# =========================
# Utilidades de data / fmt
# =========================
def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _dt(date_str: str) -> datetime:
    # "YYYY-MM-DD" -> datetime UTC 00:00
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return d.replace(tzinfo=timezone.utc)

def _fmt_usd(x: Optional[float]) -> str:
    if x is None:
        return "N/D"
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return str(x)

def _seconds_to_hms(sec: float) -> str:
    sec = int(abs(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

# =====================================
# 1) Histórico de trades (deals) do MT5
# =====================================
def fetch_deals(reader: RiskGuardMT5Reader, since: datetime, until: datetime) -> List[Dict[str, Any]]:
    """Busca deals de histórico no período [since, until)."""
    deals = mt5.history_deals_get(since, until)
    out: List[Dict[str, Any]] = []
    if deals is None:
        return out
    for d in deals:
        if int(getattr(d, "type", -999)) in (mt5.DEAL_TYPE_BALANCE, mt5.DEAL_TYPE_CREDIT):
            continue
        entry = getattr(d, "entry", None)
        out.append({
            "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
            "ticket": int(d.ticket),
            "position_id": int(getattr(d, "position_id", 0)),
            "symbol": d.symbol,
            "type": int(d.type),
            "entry": int(entry) if entry is not None else None,
            "price": float(d.price),
            "volume": float(d.volume),
            "profit": float(d.profit),
            "commission": float(getattr(d, "commission", 0.0)),
            "swap": float(getattr(d, "swap", 0.0)),
            "magic": int(getattr(d, "magic", 0)),
            "comment": getattr(d, "comment", ""),
        })
    out.sort(key=lambda x: x["time"])
    return out

def group_trades(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Agrupa deals por position_id (trade) e calcula PnL e duração.
    """
    by_pos: Dict[int, List[Dict[str, Any]]] = {}
    for d in deals:
        pid = d.get("position_id", 0) or 0
        by_pos.setdefault(pid, []).append(d)

    trades: List[Dict[str, Any]] = []
    for pid, lst in by_pos.items():
        lst = sorted(lst, key=lambda x: x["time"])
        symbol = lst[0]["symbol"]
        vol = sum(x["volume"] for x in lst if x["entry"] == 1 or x["entry"] is None)  # aproximado
        profit = sum(x["profit"] for x in lst) + sum(x["commission"] for x in lst) + sum(x["swap"] for x in lst)
        t0 = _utc(lst[0]["time"])
        t1 = _utc(lst[-1]["time"])
        holding_sec = (t1 - t0).total_seconds()
        trade_type = "buy" if any(x["type"] == mt5.DEAL_TYPE_BUY for x in lst) else ("sell" if any(x["type"] == mt5.DEAL_TYPE_SELL for x in lst) else "n/a")
        trades.append({
            "position_id": pid,
            "symbol": symbol,
            "volume": vol,
            "pnl": profit,
            "start": t0.isoformat(),
            "end": t1.isoformat(),
            "holding_time_sec": holding_sec,
            "type": trade_type,
        })
    return [t for t in trades if t["position_id"] != 0 and (t.get("symbol") or "").strip() != ""]

# ==========================================
# 1.1) Filtros por origem (magic/manual-only)
# ==========================================
def _parse_magic_list(s: Optional[str]) -> Optional[List[int]]:
    if not s:
        return None
    out: List[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except:
            pass
    return out or None

def filter_deals(deals: List[Dict[str, Any]], magic_list: Optional[List[int]], manual_only: bool) -> List[Dict[str, Any]]:
    if manual_only:
        return [d for d in deals if d.get("magic", 0) == 0]
    if magic_list:
        return [d for d in deals if d.get("magic", 0) in magic_list]
    return deals

# =============================
# 2) Métricas e qualidade (fase A)
# =============================
def compute_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "gross_profit": 0.0, "gross_loss": 0.0, "net_pnl": 0.0,
            "profit_factor": None,
            "avg_holding": None,
            "best_trade": None, "worst_trade": None,
            "pnl_by_symbol": {},
            "max_dd_abs": 0.0, "max_dd_pct": None
        }
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net = sum(pnls)
    wr = (len(wins) / len(pnls)) * 100.0 if pnls else 0.0
    pf = (gross_profit / gross_loss) if gross_loss > 1e-12 else None
    avg_hold = sum(t["holding_time_sec"] for t in trades) / len(trades)
    best = max(trades, key=lambda x: x["pnl"])
    worst = min(trades, key=lambda x: x["pnl"])

    # PnL por símbolo
    by_sym: Dict[str, float] = {}
    for t in trades:
        by_sym[t["symbol"]] = by_sym.get(t["symbol"], 0.0) + t["pnl"]

    # Curva cumulativa -> MaxDD (pelo resultado realizado)
    cum = 0.0
    cum_curve: List[float] = []
    for t in trades:
        cum += t["pnl"]
        cum_curve.append(cum)
    max_dd_abs = 0.0
    peak = -1e30
    for v in cum_curve:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd_abs:
            max_dd_abs = dd
    max_dd_pct = (max_dd_abs / peak * 100.0) if peak > 1e-9 else None

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": wr,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_pnl": net,
        "profit_factor": pf,
        "avg_holding": avg_hold,
        "best_trade": best,
        "worst_trade": worst,
        "pnl_by_symbol": {k: v for k, v in sorted(by_sym.items(), key=lambda kv: -abs(kv[1]))},
        "max_dd_abs": max_dd_abs,
        "max_dd_pct": max_dd_pct
    }

def compute_streaks(pnls: List[float]) -> Dict[str, Any]:
    best_win, best_loss = 0, 0
    cur_win, cur_loss = 0, 0
    for p in pnls:
        if p > 0:
            cur_win += 1
            cur_loss = 0
        elif p < 0:
            cur_loss += 1
            cur_win = 0
        else:
            cur_win = 0
            cur_loss = 0
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return {"win_streak": best_win, "loss_streak": best_loss}

def compute_expectancy_payoff(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"expectancy": 0.0, "payoff": None, "avg_win": 0.0, "avg_loss": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    p_win = len(wins) / len(pnls) if pnls else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (abs(sum(losses)) / len(losses)) if losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 1e-12 else None
    expectancy = p_win * avg_win - (1 - p_win) * avg_loss
    return {
        "expectancy": expectancy,
        "payoff": payoff,
        "avg_win": avg_win,
        "avg_loss": avg_loss
    }

# =========================================
# 3) Mensal / Semanal / Distribuições (Fase A)
# =========================================
def group_by_month(trades: List[Dict[str, Any]]) -> Dict[str, float]:
    out = defaultdict(float)
    for t in trades:
        d = datetime.fromisoformat(t["end"]).date()
        key = f"{d.year}-{d.month:02d}"
        out[key] += t["pnl"]
    return dict(sorted(out.items()))

def group_by_week(trades: List[Dict[str, Any]]) -> Dict[str, float]:
    out = defaultdict(float)
    for t in trades:
        d = datetime.fromisoformat(t["end"]).date()
        year, week, _ = d.isocalendar()
        key = f"{year}-W{week:02d}"
        out[key] += t["pnl"]
    return dict(sorted(out.items()))

def distro_weekday_hour(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    wd = defaultdict(float)   # 0=Mon ... 6=Sun
    hr = defaultdict(float)   # 0..23
    for t in trades:
        d1 = datetime.fromisoformat(t["end"])
        wd[d1.weekday()] += t["pnl"]
        hr[d1.hour] += t["pnl"]
    wd_named = {calendar.day_abbr[k]: v for k, v in sorted(wd.items())}
    hr_named = {f"{k:02d}h": v for k, v in sorted(hr.items())}
    return {"by_weekday": wd_named, "by_hour": hr_named}

# =======================================
# 4) Leitura de logs RiskGuard (Função 6)
# =======================================
def load_logs_in_range(since: datetime, until: datetime) -> List[Dict[str, Any]]:
    # Por simplicidade, lê apenas o log do mês atual (extensível para múltiplos meses)
    logf = Path(log_path_current())
    if not logf.exists():
        return []
    out: List[Dict[str, Any]] = []
    with logf.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
                ts = _utc(j.get("ts"))
                if since <= ts < until:
                    out.append(j)
            except Exception:
                continue
    return out

def summarize_riskguard_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_type = {"PER_TRADE": 0, "NEWS": 0, "LIMITS": 0, "DD_KILL": 0}
    closed_total = 0
    for e in events:
        et = (e.get("type") or "").upper()
        by_type[et] = by_type.get(et, 0) + 1
        payload = e.get("payload", {})
        if isinstance(payload.get("closed"), list):
            closed_total += len(payload["closed"])
    return {"events_total": len(events), "by_type": by_type, "closed_total": closed_total}

# ====================================
# 5) Consolidação do relatório (núcleo)
# ====================================
def build_report(
    reader: RiskGuardMT5Reader,
    since: datetime,
    until: datetime,
    notify: bool = False,
    magic_list: Optional[List[int]] = None,
    manual_only: bool = False
) -> Dict[str, Any]:

    snap = reader.snapshot()
    # Definir IDENT para o Telegram (cabeçalho com conta/servidor/login)
    try:
        set_ident_from_snapshot(snap, label="RiskGuard")
    except Exception:
        pass

    # Histórico bruto
    deals = fetch_deals(reader, since, until)

    # Filtros por origem
    deals = filter_deals(deals, magic_list, manual_only)
    # Trades agrupados e métricas
    trades = group_trades(deals)
    met = compute_metrics(trades)

    # Qualidade: streaks, expectancy, payoff
    streaks = compute_streaks([t["pnl"] for t in trades])
    qual = compute_expectancy_payoff(trades)

    # Período: mensal / semanal / distribuições
    monthly = group_by_month(trades)
    weekly = group_by_week(trades)
    distro = distro_weekday_hour(trades)

    # Eventos RiskGuard do log
    evs = load_logs_in_range(since, until)
    ev_summary = summarize_riskguard_events(evs)

    account = snap["account"]
    summary = {
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "account": {"login": account["login"], "server": account["server"], "currency": account["currency"]},
        "equity_now": account["equity"],
        "metrics": met,
        "quality": {
            "expectancy": qual["expectancy"],
            "payoff": qual["payoff"],
            "avg_win": qual["avg_win"],
            "avg_loss": qual["avg_loss"],
            "win_streak": streaks["win_streak"],
            "loss_streak": streaks["loss_streak"],
        },
        "period_tables": {"monthly": monthly, "weekly": weekly},
        "distribution": distro,
        "riskguard": ev_summary
    }

    # Saídas (arquivos)
    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{account['login']}_{ts_tag}"

    csv_path = OUT_DIR / f"trades_{base}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["position_id","symbol","volume","pnl","start","end","holding_sec","type"])
        for t in trades:
            w.writerow([t["position_id"], t["symbol"], t["volume"], f"{t['pnl']:.2f}", t["start"], t["end"], int(t["holding_time_sec"]), t["type"]])

    json_path = OUT_DIR / f"summary_{base}.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    # Console
    print("\n=== RiskGuard Performance Report ===")
    print(f"Conta: {account['login']} | Servidor: {account['server']} | Moeda: {account['currency']}")
    print(f"Período: {since.date()} até {until.date()}")
    print(f"Equity agora: {_fmt_usd(account['equity'])}")
    pf_console = met['profit_factor'] if met['profit_factor'] is not None else 'N/D'
    print(f"Trades: {met['trades']} | Win%: {met['win_rate']:.2f}% | PF: {pf_console}")
    print(f"PnL Bruto: {_fmt_usd(met['gross_profit'])} / {_fmt_usd(-met['gross_loss'])} | Net: {_fmt_usd(met['net_pnl'])}")
    if met["max_dd_pct"] is not None:
        print(f"Max DD (período): {_fmt_usd(met['max_dd_abs'])} ({met['max_dd_pct']:.2f}% )")
    else:
        print(f"Max DD (período): {_fmt_usd(met['max_dd_abs'])}")
    if met["best_trade"]:
        print(f"Melhor: {_fmt_usd(met['best_trade']['pnl'])} | Pior: {_fmt_usd(met['worst_trade']['pnl'])}")
    print(f"Tempo médio por trade: {_seconds_to_hms(met['avg_holding'] or 0)}")
    print(f"Symbols com maior impacto: {list(met['pnl_by_symbol'].items())[:5]}")
    print(f"Expectancy: {_fmt_usd(qual['expectancy'])} | Payoff: {qual['payoff']:.2f}" if qual['payoff'] is not None else f"Expectancy: {_fmt_usd(qual['expectancy'])} | Payoff: N/D")
    print(f"Streaks -> Win: {streaks['win_streak']} | Loss: {streaks['loss_streak']}")
    print(f"Mensal ($): {monthly}")
    print(f"Semanal ($): {weekly}")
    print(f"Distro por dia: {distro['by_weekday']}")
    print(f"Distro por hora: {distro['by_hour']}")
    print(f"Eventos RiskGuard: {ev_summary}")

    print(f"\nArquivos salvos:")
    print(f" - Trades CSV: {csv_path}")
    print(f" - Summary JSON: {json_path}\n")

    # === 2.1) Série de equity e DD por curva ===
    summary.setdefault("timeseries", {})
    print("[DEBUG] trades_para_equity", len(trades))
    eq_points = _rg_make_equity_series(
        trades=trades,
        equity_now=summary.get("equity_now"),
        net_pnl=met.get("net_pnl", 0.0),
    )
    summary["timeseries"]["equity"] = eq_points
    print("[DEBUG] equity_points", len(eq_points))

    dd_abs, dd_pct, dd_peak, dd_trough, dd_peak_lab, dd_trough_lab = _max_drawdown_stats(eq_points)
    q = summary.setdefault("quality", {})
    q["max_dd_abs_curve"] = dd_abs
    q["max_dd_pct_curve"] = dd_pct
    q["max_dd_window"] = {"from": dd_peak_lab, "to": dd_trough_lab}

    # === 2.2) Monthly Gain % (uma única vez) ===
    if summary["timeseries"].get("equity"):
        eq_daily = _rg_daily_equity(summary["timeseries"]["equity"], since, until)
        summary["timeseries"]["equity_daily"] = eq_daily

        first_by_month, last_by_month = {}, {}
        for day, eq in eq_daily:
            key = day[:7]  # YYYY-MM
            first_by_month.setdefault(key, float(eq))
            last_by_month[key] = float(eq)

        monthly_gain_pct = {}
        for key in sorted(last_by_month):
            first = first_by_month.get(key, 0.0)
            last = last_by_month.get(key, 0.0)
            if first > 0:
                monthly_gain_pct[key] = round((last / first - 1.0) * 100.0, 2)

        summary.setdefault("period_tables", {})["monthly_gain_pct"] = monthly_gain_pct
    else:
        print("[WARN] timeseries/equity ausentes; pulando monthly_gain_pct.")
        summary.setdefault("period_tables", {})["monthly_gain_pct"] = {}

    # === 2.3) FLUXOS (histórico completo + filtro do período) ===
    deps_all, wdr_all = _rg_fetch_all_flows(until)
    total_dep = sum(v for _, v in deps_all)
    total_wdr = sum(v for _, v in wdr_all)
    saldo_fluxos = total_dep - total_wdr
    summary["flows_summary"] = {
        "total_deposits": round(total_dep, 2),
        "total_withdrawals": round(total_wdr, 2),
        "net_flows": round(saldo_fluxos, 2),
    }
    print(f"Depósitos totais: ${total_dep:,.2f}")
    print(f"Retiradas totais: ${total_wdr:,.2f}")
    print(f"Fluxo líquido: ${saldo_fluxos:,.2f}")

    deposits, withdrawals = _rg_filter_flows_to_period(deps_all, wdr_all, since, until)
    summary["timeseries"]["flows"] = {"deposit": deposits, "withdrawal": withdrawals}
    print("[DEBUG] flows (all): dep_total:", len(deps_all), "wdr_total:", len(wdr_all))
    print("[DEBUG] flows (period): dep:", len(deposits), "wdr:", len(withdrawals))

    # DEBUG: tipos de deals (pra investigação)
    from collections import Counter
    type_counts = Counter(int(d.get("type", -999)) for d in deals)
    print("[DEBUG] deal.type counts:", dict(type_counts))

    
    # === NEW: monthly gain % baseado na curva de equity diária
    if "timeseries" not in summary or "equity" not in summary["timeseries"]:
        print("[WARN] timeseries ou equity ausentes; pulando cálculo de ganho mensal.")
        summary.setdefault("period_tables", {})["monthly_gain_pct"] = {}
    else:
        eq_daily = _rg_daily_equity(summary["timeseries"]["equity"], since, until)
        summary.setdefault("timeseries", {})["equity_daily"] = eq_daily

        first_by_month = {}
        last_by_month = {}
        for day, eq in eq_daily:        # day = "YYYY-MM-DD", eq = float
            key = day[:7]               # "YYYY-MM"
            first_by_month.setdefault(key, float(eq))
            last_by_month[key] = float(eq)

        monthly_gain_pct = {}
        for key in sorted(last_by_month):
            first = first_by_month.get(key, 0.0)
            last = last_by_month.get(key, 0.0)
            if first > 0:
                monthly_gain_pct[key] = round((last/first - 1.0) * 100.0, 2)

        summary.setdefault("period_tables", {})["monthly_gain_pct"] = monthly_gain_pct


    eq_daily = _rg_daily_equity(summary["timeseries"]["equity"], since, until)
    summary.setdefault("timeseries", {})["equity_daily"] = eq_daily

    first_by_month = {}
    last_by_month = {}
    for day, eq in eq_daily:        # day = "YYYY-MM-DD", eq = float
        key = day[:7]               # "YYYY-MM"
        first_by_month.setdefault(key, float(eq))
        last_by_month[key] = float(eq)

    monthly_gain_pct = {}
    for key in sorted(last_by_month):
        first = first_by_month.get(key, 0.0)
        last = last_by_month.get(key, 0.0)
        if first > 0:
            monthly_gain_pct[key] = round((last/first - 1.0) * 100.0, 2)

    summary.setdefault("period_tables", {})["monthly_gain_pct"] = monthly_gain_pct


    # === RG: construir curva de equity a partir dos trades ===
    # 1) Descobrir de onde vêm os trades:
    #    - se você já tem a variável `trades`, ótimo
    #    - senão tentamos pegar de dentro do summary (caso você tenha guardado)
    trades_src = None
    try:
        # caso 1: variável `trades` já existe na função
        trades_src = trades
    except NameError:
        # caso 2: tente dentro do summary
        trades_src = summary.get("trades") or summary.get("closed_trades") or []

    # 2) Log de diagnóstico
    print("[DEBUG] trades_para_equity", len(trades_src))

    # 3) Monta a série e injeta no summary
    eq_points = _rg_make_equity_series(
        trades=trades_src,
        equity_now=summary.get("equity_now"),
        net_pnl=summary.get("metrics", {}).get("net_pnl", 0.0),
    )
    summary.setdefault("timeseries", {})["equity"] = eq_points

    # 4) (opcional) DD por curva — se quiser já usar no gráfico
    #    (pode deixar por enquanto; não atrapalha)
    try:
        peak = None
        dd_abs = 0.0
        peak_lab = trough_lab = ""
        for lab, val in eq_points:
            if peak is None or val > peak:
                peak = val
                peak_lab = lab
            if peak - val > dd_abs:
                dd_abs = peak - val
                trough_lab = lab
        dd_pct = (dd_abs / (peak or 1.0)) * 100.0
        q = summary.setdefault("quality", {})
        q["max_dd_abs_curve"] = round(dd_abs, 2)
        q["max_dd_pct_curve"] = round(dd_pct, 2)
        q["max_dd_window"] = {"from": peak_lab, "to": trough_lab}
    except Exception as e:
        print("[DEBUG] dd_by_curve erro:", repr(e))

    # 5) Log: quantos pontos gerados
    print("[DEBUG] equity_points", len(summary.get("timeseries", {}).get("equity", [])))

    # === RG: FLUXOS (puxa histórico completo e filtra para o período do relatório) ===
    deps_all, wdr_all = _rg_fetch_all_flows(until)
    deposits, withdrawals = _rg_filter_flows_to_period(deps_all, wdr_all, since, until)
    summary.setdefault("timeseries", {})["flows"] = {
        "deposit": deposits,
        "withdrawal": withdrawals,
    }
    print("[DEBUG] flows (all): dep_total:", len(deps_all), "wdr_total:", len(wdr_all))
    print("[DEBUG] flows (period): dep:", len(deposits), "wdr:", len(withdrawals))



    # DEBUG: estatística de tipos de deals no período
    from collections import Counter
    type_counts = Counter(int(d.get("type", -999)) for d in deals)
    print("[DEBUG] deal.type counts:", dict(type_counts))
    flows = summary.get("timeseries", {}).get("flows", {})
    print("[DEBUG] deposits:", len(flows.get("deposit", [])), "withdrawals:", len(flows.get("withdrawal", [])))

    # === MONTE CARLO ======================================
    print("\n=== Monte Carlo Simulation ===")

    # 2.0) Parâmetros de simulação
    MC_ITER = 5000               # nº de caminhos
    MC_TRADES = max(100, len(trades))  # horizonte (>=100)
    MC_METHOD = "block"          # "bootstrap" | "block" | "permute"
    MC_BLOCK = 5                 # tamanho do bloco se method="block"
    MC_RISK_PCT = get_float("MC_RISK_PCT", 0.01)  # 1% por trade
    MC_DD_LIMIT = get_float("MC_DD_LIMIT", 0.30)  # 30% (para prob. de ruína)

    # 2.1) Equity inicial do período (a mesma lógica que você já usa)
    try:
        mc_start_equity = float(summary.get("equity_now") or 0.0) - float(met.get("net_pnl") or 0.0)
        if mc_start_equity <= 1e-6:
            # fallback seguro: evita start_equity ≈ 0
            mc_start_equity = float(summary.get("equity_now") or 1000.0)
    except Exception:
        mc_start_equity = float(summary.get("equity_now") or 0.0)

    # 2.2) Construir R-histórico realista a partir dos trades (método robusto)
    # === Monte Carlo: preparar amostra de trades REALISTA ===
    # Filtra:
    # - remove position_id == 0 (BALANCE/CREDIT)
    # - remove símbolo vazio
    # - remove ruído muito pequeno (|pnl| < 0.05)
    trades_for_mc = [
        t for t in trades
        if int(t.get("position_id", 0)) != 0
        and str(t.get("symbol") or "").strip() != ""
        and abs(float(t.get("pnl", 0.0))) >= 0.05
    ]

    from mc import compute_R_from_trades

    try:
        R_hist, est_risk_pct = compute_R_from_trades(
            trades_for_mc if len(trades_for_mc) >= 5 else trades,
            equity_start=mc_start_equity,
            fallback_risk_pct=MC_RISK_PCT,
        )
        print(f"[DEBUG] MonteCarlo: trades_for_mc={len(trades_for_mc)} | R_hist={len(R_hist)} | risk_pct≈{est_risk_pct:.3%}")
    except Exception as e:
        print("[WARN] Falha ao calcular R_hist:", e)
        R_hist = np.array([-1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0])
        est_risk_pct = MC_RISK_PCT


    # 2.3) Simulação dos caminhos
    paths = simulate_paths(
        returns_R=R_hist,
        start_equity=mc_start_equity if mc_start_equity > 0 else (summary.get("equity_now") or 1000.0),
        risk_pct=est_risk_pct,
        n_trades=MC_TRADES,
        iterations=MC_ITER,
        method=MC_METHOD,
        block_size=MC_BLOCK,
        fee_per_trade=0.0,
        seed=42
    )

    # 2.4) Resumo (ruína, VaR/ES, DD, percentis, etc.)
    mc_summary = summarize_paths(
        paths,
        start_equity=mc_start_equity if mc_start_equity > 0 else (summary.get("equity_now") or 1000.0),
        dd_limit_pct=MC_DD_LIMIT
    )

    # 2.5) Salvar gráficos no diretório de saída com o mesmo 'base'
    fan_path = dd_path = None
    try:
        fig_fan = mc_fig_fanchart(paths, title=f"Monte Carlo – {MC_TRADES} trades ({MC_ITER} iterações)")
        fan_path = OUT_DIR / f"mc_fanchart_{base}.png"
        fig_fan.savefig(fan_path, dpi=120, bbox_inches="tight")

        fig_dd = mc_fig_dd_hist(paths, bins=30, title="Distribuição do Máx. Drawdown")
        dd_path = OUT_DIR / f"mc_dd_hist_{base}.png"
        fig_dd.savefig(dd_path, dpi=120, bbox_inches="tight")
    except Exception as e:
        print("[MC] Aviso: não foi possível gerar figuras (ok):", repr(e))
        fan_path = dd_path = None

    # 2.6) Tabela amigável
    tabela_mc = mc_table(mc_summary)

    # 2.7) Injeta no summary (para o HTML/PDF)
    summary["monte_carlo"] = {
        "config": {
            "iterations": MC_ITER,
            "n_trades": MC_TRADES,
            "method": MC_METHOD,
            "block_size": MC_BLOCK,
            "risk_pct": float(est_risk_pct),
            "dd_limit_pct": MC_DD_LIMIT,
            "start_equity": mc_start_equity,
        },
        "final_equity": mc_summary["final_equity"],
        "final_pnl": mc_summary["final_pnl"],
        "final_return_pct": mc_summary["final_return_pct"],
        "max_drawdown": mc_summary["max_drawdown"],
        "prob_ruin_peak": mc_summary.get("prob_ruin_peak"),
        "prob_ruin_floor": mc_summary.get("prob_ruin_floor"),
        "table": tabela_mc,
        "plots": {
            "fan_chart": str(fan_path) if fan_path else None,
            "dd_hist":   str(dd_path)  if dd_path  else None,
        }
    }


    print("Monte Carlo concluído.")
    # === FIM MONTE CARLO =======================================================

    # === HTML e PDF ===
    # pré-requisito: variáveis summary, account, OUT_DIR e ts_tag já existem
    html_path = OUT_DIR / f"report_{account['login']}_{ts_tag}.html"
    render_html(summary, html_path)

    pdf_path = OUT_DIR / f"report_{account['login']}_{ts_tag}.pdf"
    ok_pdf = html_to_pdf(html_path, pdf_path)  # tenta gerar via wkhtmltopdf


    # Telegram (resumo bonito + PDF, se existir)
    if notify:
        notify_report(
            period_from=str(since.date()),
            period_to=str(until.date()),
            account={
                "login": account["login"],
                "server": account["server"],
                "currency": account["currency"],
                "equity": account["equity"]
            },
            met=met,
            ev_summary=ev_summary
        )
        # Enviar PDF se gerou; caso não, avisa e deixa o HTML pronto
        pdf_path = OUT_DIR / f"report_{account['login']}_{ts_tag}.pdf"
        if ok_pdf and pdf_path.exists():
            send_document(str(pdf_path), caption="📊 Relatório de Performance")
        else:
            print(f"Aviso: PDF não gerado. HTML disponível em '{html_path}'.")

    return summary

# =========
# 6) CLI
# =========
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="date_from", type=str, help="YYYY-MM-DD (inclusivo)")
    parser.add_argument("--to", dest="date_to", type=str, help="YYYY-MM-DD (exclusivo; default=amanhã)")
    parser.add_argument("--notify", action="store_true", help="Enviar resumo para o Telegram")
    parser.add_argument("--terminal", type=str, default=r"C:\Program Files\MetaTrader 5\terminal64.exe", help="Caminho do terminal MT5")
    parser.add_argument("--magic", type=str, help="Lista de magic numbers separados por vírgula (ex: 123,9999)")
    parser.add_argument("--manual-only", action="store_true", help="Somente operações manuais (magic=0)")
    args = parser.parse_args()

    date_to = _dt(args.date_to) if args.date_to else (_now_utc().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    if args.date_from:
        date_from = _dt(args.date_from)
    else:
        date_from = date_to - timedelta(days=30)  # padrão: últimos 30 dias

    reader = RiskGuardMT5Reader(path=args.terminal)
    assert reader.connect(), "Falha ao conectar no MT5"
    try:
        summary = build_report(
            reader,
            since=date_from,
            until=date_to,
            notify=args.notify,
            magic_list=_parse_magic_list(args.magic),
            manual_only=args.manual_only
        )
    finally:
        reader.shutdown()

if __name__ == "__main__":
    main()

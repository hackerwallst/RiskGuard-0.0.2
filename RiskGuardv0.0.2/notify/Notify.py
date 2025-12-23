# notify.py — Função 5: notificações Telegram (com IDENT + estilização)
from __future__ import annotations
from typing import Dict, Any, Optional
import os, time, json, requests
from pathlib import Path
from datetime import datetime, timezone
from rg_config import get_str

# ==========================================================
# CONFIGURAÇÃO DO SEU BOT TELEGRAM (seu token/ID)
# ==========================================================
BOT_TOKEN = get_str("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = get_str("TELEGRAM_CHAT_ID", "")
# ==========================================================

_LAST_SENT_FILE = Path(__file__).resolve().parent / ".notify_last.txt"
_MIN_INTERVAL_S = 1.2  # anti-flood básico
_TELEGRAM_CONFIGURED = bool(BOT_TOKEN and CHAT_ID)
_TELEGRAM_WARNED = False


def _ensure_telegram_configured() -> bool:
    global _TELEGRAM_WARNED
    if _TELEGRAM_CONFIGURED:
        return True
    if not _TELEGRAM_WARNED:
        print("[notify] Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config.txt")
        _TELEGRAM_WARNED = True
    return False

# ---------- IDENTIFICADOR GLOBAL ----------
_IDENT = ""  # será mostrado no topo de cada mensagem

def set_ident(label: Optional[str] = None, login: Optional[int] = None, server: Optional[str] = None) -> None:
    """
    Define o identificador mostrado nas mensagens.
    Exemplos:
      set_ident(label="Conta Demo 01", login=315397198, server="XMGlobal-MT5 7")
      set_ident(label="VPS-LON-1")    # só com nome
    """
    parts = []
    if label:
        parts.append(str(label))
    if server:
        parts.append(str(server))
    if login:
        parts.append(str(login))
    global _IDENT
    _IDENT = " | ".join(parts) if parts else ""

def set_ident_from_snapshot(snap: Dict[str, Any], label: Optional[str] = None) -> None:
    """
    Extrai login/server do snapshot do mt5_reader e monta o IDENT.
    """
    acc = snap.get("account", {}) if isinstance(snap, dict) else {}
    login = acc.get("login")
    server = acc.get("server")
    set_ident(label=label, login=login, server=server)

# ---------- Utils ----------
def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _sleep_antiflood():
    try:
        last = 0.0
        if _LAST_SENT_FILE.exists():
            last = float(_LAST_SENT_FILE.read_text().strip() or "0")
        now = time.time()
        wait = (last + _MIN_INTERVAL_S) - now
        if wait > 0:
            time.sleep(wait)
        _LAST_SENT_FILE.write_text(str(time.time()))
    except Exception:
        pass

def _send_text(text: str) -> bool:
    if not _ensure_telegram_configured():
        return False
    _sleep_antiflood()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    text = text if len(text) <= 3900 else (text[:3900] + "\n…(truncado)…")
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})
        if r.status_code != 200:
            print("[notify] falha:", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        print("[notify] exceção:", repr(e))
        return False

def _ident_header() -> str:
    return (f"🏷️ <b>{_IDENT}</b>\n" if _IDENT else "")

def send_alert(title: str, lines: list[str]) -> bool:
    header = _ident_header()
    msg = header + f"📣 <b>{title}</b>\n" + "\n".join(lines)
    return _send_text(msg)

# ---------- Notificações por tipo ----------
def notify_per_trade(rep: Dict[str, Any]) -> None:
    for v in rep.get("violations", []):
        risk_pct = v.get("risk_pct")
        lines = [
            f"⚠️ <b>Per-Trade Violation</b>",
            f"💰 <b>Equity:</b> ${rep.get('equity', 0):,.2f}",
            f"🔹 <b>Symbol:</b> {v.get('symbol')}",
            f"🎫 <b>Ticket:</b> {v.get('ticket')}",
            f"📊 <b>Risco:</b> {risk_pct:.2f}% " if isinstance(risk_pct, (int,float)) else "📊 <b>Risco:</b> N/D",
            f"⏰ <b>Hora:</b> {_utc_iso()}",
        ]
        send_alert("PER-TRADE (1% / SL)", lines)

def notify_news(rep: Dict[str, Any]) -> None:
    if rep.get("affected"):
        for a in rep["affected"]:
            m0 = a.get("matches", [{}])[0] if a.get("matches") else {}
            lines = [
                f"📰 <b>Notícia detectada</b>",
                f"💱 <b>Ativo:</b> {a.get('symbol')}",
                f"🌍 <b>Moeda:</b> {m0.get('currency')}",
                f"⚡ <b>Evento:</b> {m0.get('event')}",
                f"🚫 <b>AutoTrade pausado até:</b> {rep.get('kill_switch_until')}",
                f"⏰ {_utc_iso()}",
            ]
            send_alert("NEWS WINDOW (±1h)", lines)

def notify_limits(rep: Dict[str, Any]) -> None:
    changed = bool(rep.get("closed") or rep.get("failed") or rep.get("new_tickets_detected") or
                   rep.get("attempts_before") != rep.get("attempts_after") or
                   rep.get("risk_block_active_before") != rep.get("risk_block_active_after"))
    if changed:
        lines = [
            f"📈 <b>Controle de Risco Global</b>",
            f"💰 <b>Total Risco:</b> {rep.get('total_risk_pct', 0):.2f}%",
            f"🚧 <b>Bloqueio Ativo:</b> {rep.get('risk_block_active_after')}",
            f"🧾 <b>Ordens Fechadas:</b> {len(rep.get('closed', []))}",
            f"⏰ {_utc_iso()}",
        ]
        send_alert("LIMITS (5%)", lines)

def notify_dd(rep: Dict[str, Any]) -> None:
    if rep.get("tripped") or rep.get("in_cooldown") or rep.get("awaiting_unlock"):
        lines = [
            f"💀 <b>Drawdown Detectado</b>",
            f"📉 <b>DD Atual:</b> {rep.get('dd_pct', 0):.2f}%",
            f"💰 <b>Equity:</b> ${rep.get('equity', 0):,.2f}",
            f"⏳ <b>Cooldown até:</b> {rep.get('cooldown_until')}",
            f"🔒 <b>2FA Ativo:</b> {rep.get('awaiting_unlock')}",
            f"⏰ {_utc_iso()}",
        ]
        send_alert("DD KILL (Cooldown + 2FA)", lines)

def _fmt_money(x):
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return str(x)

def _fmt_pct(x):
    try:
        return f"{float(x):.2f}%"
    except Exception:
        return "N/D"

def _clean_top_symbols(pnl_by_symbol: dict, limit: int = 5):
    # Remove símbolos vazios/None e ordena por |PnL| desc
    items = [(s, v) for s, v in (pnl_by_symbol or {}).items() if s]
    items.sort(key=lambda kv: -abs(kv[1]))
    return items[:limit]

def notify_report(period_from, period_to, account: dict, met: dict, ev_summary: dict) -> None:
    """
    Mensagem bonita do relatório de performance (card compacto).
    """
    # Cabeçalho
    acc_line = f"{account.get('login')} @ {account.get('server')}"
    equity   = _fmt_money(account.get("equity"))

    # Métricas principais
    trades = met.get("trades", 0)
    winr   = _fmt_pct(met.get("win_rate", 0))
    pf     = "N/D" if met.get("profit_factor") is None else f"{met['profit_factor']:.2f}"
    net    = _fmt_money(met.get("net_pnl", 0))
    dd_abs = _fmt_money(met.get("max_dd_abs", 0))
    dd_pct = "N/D" if met.get("max_dd_pct") is None else _fmt_pct(met["max_dd_pct"])
    best   = _fmt_money(met.get("best_trade", {}).get("pnl") if met.get("best_trade") else 0)
    worst  = _fmt_money(met.get("worst_trade", {}).get("pnl") if met.get("worst_trade") else 0)

    # Top símbolos (lista)
    top_syms = _clean_top_symbols(met.get("pnl_by_symbol", {}), limit=5)
    if top_syms:
        sym_lines = [f"• <b>{s}</b>: {_fmt_money(v)}" for s, v in top_syms]
        syms_block = "\n".join(sym_lines)
    else:
        syms_block = "—"

    # Eventos RiskGuard (legível)
    by_type = ev_summary.get("by_type", {})
    rg_total = ev_summary.get("events_total", 0)
    rg_lines = [
        f"• PER_TRADE: {by_type.get('PER_TRADE', 0)}",
        f"• NEWS: {by_type.get('NEWS', 0)}",
        f"• LIMITS: {by_type.get('LIMITS', 0)}",
        f"• DD_KILL: {by_type.get('DD_KILL', 0)}",
        f"• FECHADOS: {ev_summary.get('closed_total', 0)}"
    ]
    rg_block = "\n".join(rg_lines)

    # Blocos formatados (usando <pre> para alinhamento pontual)
    lines = [
        f"📅 <b>Período:</b> {period_from} → {period_to}",
        f"👤 <b>Conta:</b> {acc_line}",
        f"💰 <b>Equity:</b> {equity}",
        "",
        "<b>Resumo</b>",
        f"<pre>Trades: {trades:<5}   Win%: {winr:<8}   PF: {pf:<6}</pre>",
        f"<pre>Net PnL: {net:<12}  MaxDD: {dd_abs} ({dd_pct})</pre>",
        f"<pre>Best:    {best:<12}  Worst: {worst}</pre>",
        "",
        "<b>Top Símbolos</b>",
        syms_block,
        "",
        "<b>Eventos RiskGuard</b>",
        rg_block,
        f"\n⏰ {_utc_iso()}",
    ]

    send_alert("📊 Relatório de Performance", lines)


def send_document(file_path: str, caption: str = "") -> bool:
    """Envia um arquivo (ex.: PDF) para o Telegram."""
    if not _ensure_telegram_configured():
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            files = {"document": (Path(file_path).name, f)}
            data = {"chat_id": CHAT_ID, "caption": caption}
            r = requests.post(url, data=data, files=files)
        if r.status_code != 200:
            print("[notify] falha sendDocument:", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        print("[notify] exceção sendDocument:", repr(e))
        return False

# --- Compat: send_event(title, payload) -> usa send_alert com formatação JSON ---
def send_event(title: str, payload: Optional[Dict[str, Any]] = None) -> bool:
    """
    Wrapper de compatibilidade para módulos que chamam send_event(title, payload).
    Renderiza o payload como JSON (linhas) e delega para send_alert.
    """
    try:
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            lines = body.splitlines()
        else:
            lines = []
    except Exception:
        lines = [str(payload) if payload is not None else ""]

    # opcional: carimbar horário no final
    lines.append(f"⏰ {_utc_iso()}")
    return send_alert(title, lines)


# ---------- Teste rápido ----------
if __name__ == "__main__":
    # Exemplo: defina o IDENT manualmente para o teste
    set_ident(label="Conta Demo 01", login=315397198, server="XMGlobal-MT5 7")
    ok = send_alert("✅ Self-test", [
        f"🧠 <b>RiskGuard Notify OK</b>",
        f"⏰ {_utc_iso()}",
        f"💬 Teste concluído com sucesso!"
    ])
    print("notify:", ok)

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from pathlib import Path
import html
import time
import unicodedata
import json

from mt5_reader import RiskGuardMT5Reader
from notify import send_alert, send_document
from limits.limits import risk_block_status
from logger import log_event

try:
    from limits.dd_kill import dd_status
except Exception:
    dd_status = None


MAX_POSITIONS = 20
MAX_HISTORY = 15
REPORT_STATE_FILE = Path(__file__).resolve().parent / ".rg_report_cmd.json"
PERTRADE_STATE_FILE = Path(__file__).resolve().parent / ".rg_pertrade_interactive.json"
REPORT_PENDING_TTL_SEC = 15 * 60

REPORT_CHOICES = {
    "1": {"label": "Completo", "days": None},
    "2": {"label": "Anual (últimos 12 meses)", "days": 365},
    "3": {"label": "Trimestral (últimos 90 dias)", "days": 90},
    "4": {"label": "Mensal (últimos 30 dias)", "days": 30},
    "5": {"label": "Quinzenal (últimos 15 dias)", "days": 15},
}

REPORT_ALIAS = {
    "completo": "1",
    "total": "1",
    "anual": "2",
    "ano": "2",
    "trimestral": "3",
    "trimestre": "3",
    "mensal": "4",
    "mes": "4",
    "quinzenal": "5",
    "quinzena": "5",
}


def handle_telegram_commands(reader: RiskGuardMT5Reader, messages: List[Dict[str, Any]]) -> int:
    handled = 0
    for msg in messages or []:
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if text in ("1", "2", "3", "4", "5"):
            # Se há escolha pendente de relatório e não conflita com per-trade, consome.
            if _is_report_pending() and not _pertrade_has_pending():
                _send_report(reader, [text])
                handled += 1
                continue
            # Caso contrário, mantém reservado para per-trade.
            if text in ("1", "2"):
                continue
        cmd, args = _parse_command(text)
        if not cmd:
            continue
        if cmd == "status":
            _send_status(reader)
            handled += 1
        elif cmd == "positions":
            _send_positions(reader)
            handled += 1
        elif cmd == "history":
            _send_history(reader)
            handled += 1
        elif cmd == "report":
            _send_report(reader, args)
            handled += 1
        elif cmd == "help":
            _send_help()
            handled += 1
    return handled


def _strip_accents(text: str) -> str:
    try:
        return "".join(
            ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
        )
    except Exception:
        return text


def _parse_command(text: str) -> Tuple[Optional[str], List[str]]:
    raw = text.strip()
    if not raw:
        return None, []

    parts = raw.split()
    if not parts:
        return None, []

    if raw.startswith("/"):
        first = parts[0][1:]
        args = parts[1:]
    else:
        first = parts[0]
        args = parts[1:]

    if not first:
        return None, []

    # Remove bot mention in group commands, e.g. /status@MyBot
    first = first.split("@")[0]
    first = _strip_accents(first).lower()

    aliases = {
        "status": "status",
        "positions": "positions",
        "posicoes": "positions",
        "history": "history",
        "historico": "history",
        "report": "report",
        "relatorio": "report",
        "help": "help",
        "ajuda": "help",
    }

    cmd = aliases.get(first)
    if not cmd:
        return None, []
    return cmd, args


def _fmt_money(x: Any) -> str:
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "N/D"


def _fmt_pct(x: Any) -> str:
    try:
        return f"{float(x):.2f}%"
    except Exception:
        return "N/D"


def _fmt_price(x: Any, digits: int) -> str:
    try:
        if x is None:
            return "-"
        v = float(x)
        if v == 0.0:
            return "-"
        return f"{v:.{int(digits)}f}"
    except Exception:
        return "-"


def _h(v: Any) -> str:
    try:
        return html.escape(str(v), quote=False)
    except Exception:
        return str(v)


def _period_last_30_days() -> Tuple[datetime, datetime]:
    return _period_last_days(30)

def _period_last_days(days: int) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    until = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    since = until - timedelta(days=int(days))
    return since, until

def _report_menu_lines() -> List[str]:
    return [
        "Escolha o período do relatório:",
        "1 - Completo",
        "2 - Anual",
        "3 - Trimestral",
        "4 - Mensal",
        "5 - Quinzenal",
        "",
        "Responda com /relatorio 1..5",
    ]

def _load_report_state() -> Dict[str, Any]:
    try:
        if REPORT_STATE_FILE.exists():
            data = json.loads(REPORT_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"pending": False, "requested_at": 0.0}

def _save_report_state(state: Dict[str, Any]) -> None:
    try:
        REPORT_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass

def _set_report_pending() -> None:
    _save_report_state({"pending": True, "requested_at": time.time()})

def _clear_report_pending() -> None:
    _save_report_state({"pending": False, "requested_at": 0.0})

def _is_report_pending() -> bool:
    st = _load_report_state()
    if not st.get("pending"):
        return False
    requested_at = float(st.get("requested_at") or 0.0)
    if requested_at and (time.time() - requested_at) > REPORT_PENDING_TTL_SEC:
        _clear_report_pending()
        return False
    return True

def _pertrade_has_pending() -> bool:
    try:
        if not PERTRADE_STATE_FILE.exists():
            return False
        data = json.loads(PERTRADE_STATE_FILE.read_text(encoding="utf-8"))
        tickets = data.get("tickets")
        if isinstance(tickets, dict):
            for st in tickets.values():
                if isinstance(st, dict) and st.get("status") == "pending":
                    return True
    except Exception:
        return False
    return False

def _parse_report_choice(args: Optional[List[str]]) -> Optional[str]:
    if not args:
        return None
    token = _strip_accents(str(args[0]).strip().lower())
    if token in REPORT_CHOICES:
        return token
    return REPORT_ALIAS.get(token)


def _send_status(reader: RiskGuardMT5Reader) -> None:
    try:
        snap = reader.snapshot()
    except Exception as exc:
        send_alert("STATUS", [
            "❌ MT5 desconectado ou indisponível no momento.",
            f"Detalhe: {repr(exc)}",
        ])
        return
    acc = snap.get("account") or {}
    exp = snap.get("exposure") or {}
    positions = snap.get("positions") or []

    risk_state = risk_block_status()
    dd = dd_status() if dd_status else {}

    lines = [
        f"💼 <b>Conta:</b> {_h(acc.get('login'))} @ {_h(acc.get('server'))}",
        f"💱 <b>Moeda:</b> {_h(acc.get('currency'))} | <b>Alav:</b> 1:{_h(acc.get('leverage'))}",
        f"💰 <b>Saldo:</b> {_h(_fmt_money(acc.get('balance')))} | <b>Equity:</b> {_h(_fmt_money(acc.get('equity')))} | <b>Profit:</b> {_h(_fmt_money(acc.get('profit')))}",
        f"🧱 <b>Margem:</b> usada {_h(_fmt_money(acc.get('margin')))} | livre {_h(_fmt_money(acc.get('margin_free')))} | nível {_h(_fmt_pct(acc.get('margin_level')))}",
        f"✅ <b>Trade permitido:</b> {_h('SIM' if acc.get('trade_allowed') else 'NAO')}",
        f"📊 <b>Posições:</b> {len(positions)} | <b>Risco total (SL):</b> {_h(_fmt_pct(exp.get('total_risk_pct')))} ({_h(_fmt_money(exp.get('total_risk_money')))})",
        f"🛡 <b>Bloqueio 5%:</b> {'ATIVO' if risk_state.get('risk_block_active') else 'INATIVO'} | tentativas {risk_state.get('block_attempts', 0)}",
    ]

    if dd:
        cooldown = dd.get("cooldown_until") or "-"
        lines.append(
            f"💀 <b>DD:</b> limite {_h(_fmt_pct(dd.get('dd_limit_pct')))} | cooldown {_h(cooldown)} | 2FA {_h('SIM' if dd.get('twofa_configured') else 'NAO')}"
        )

    lines.append(f"🕒 <b>Atualizado:</b> {_h(snap.get('timestamp'))}")

    send_alert("STATUS", lines)


def _send_positions(reader: RiskGuardMT5Reader) -> None:
    try:
        snap = reader.snapshot()
    except Exception as exc:
        send_alert("POSITIONS", [
            "❌ MT5 desconectado ou indisponível no momento.",
            f"Detalhe: {repr(exc)}",
        ])
        return
    positions = snap.get("positions") or []

    if not positions:
        send_alert("POSITIONS", ["Nenhuma posição aberta."])
        return

    lines: List[str] = [f"📊 <b>Posições abertas:</b> {len(positions)}", ""]
    for idx, pos in enumerate(positions[:MAX_POSITIONS]):
        sym = pos.get("symbol") or "?"
        ticket = pos.get("ticket") or "?"
        side = str(pos.get("type") or "").upper() or "N/D"
        vol = pos.get("volume")
        pnl = pos.get("floating_pnl")
        risk = pos.get("risk_pct")
        digits = int((pos.get("symbol_info") or {}).get("digits") or 5)
        entry = _fmt_price(pos.get("open_price"), digits)
        sl = _fmt_price(pos.get("sl"), digits)
        tp = _fmt_price(pos.get("tp"), digits)
        vol_str = f"{float(vol):.2f}" if isinstance(vol, (int, float)) else vol

        lines.append(f"• {_h(sym)} #{_h(ticket)} | {_h(side)} {_h(vol_str)}")
        lines.append(f"  Entrada {_h(entry)} | SL {_h(sl)} | TP {_h(tp)}")
        lines.append(f"  PnL {_h(_fmt_money(pnl))} | Risco {_h(_fmt_pct(risk))}")
        if idx < len(positions[:MAX_POSITIONS]) - 1:
            lines.append("")

    if len(positions) > MAX_POSITIONS:
        lines.append(f"... e mais {len(positions) - MAX_POSITIONS} posições.")

    send_alert("POSITIONS", lines)


def _send_history(reader: RiskGuardMT5Reader) -> None:
    since, until = _period_last_30_days()
    try:
        from reports import reports as reports_mod
    except Exception as exc:
        err = repr(exc)
        lines = ["❌ Módulo de relatórios indisponível."]
        if isinstance(exc, ModuleNotFoundError):
            missing = getattr(exc, "name", None) or "dependência"
            lines.append(
                f"Dependência ausente: {missing}. Rode setup_riskguard.ps1 para reinstalar."
            )
        else:
            lines.append(f"Detalhe: {err}")
        send_alert("HISTORY", lines)
        log_event("ERROR", {"err": err}, {"module": "reports_import"})
        return

    try:
        reader.ensure_connection()
    except Exception as exc:
        send_alert("HISTORY", [
            "❌ MT5 desconectado ou indisponível no momento.",
            f"Detalhe: {repr(exc)}",
        ])
        return
    deals = reports_mod.fetch_deals(reader, since, until)
    trades = reports_mod.group_trades(deals)
    trades.sort(key=lambda t: t.get("end", ""))
    met = reports_mod.compute_metrics(trades)

    period_label = f"{since.date()} -> {until.date()}"
    lines: List[str] = [f"🗓 <b>Período:</b> {period_label} (últimos 30 dias)"]

    if not trades:
        lines.append("Sem trades no período.")
        send_alert("HISTORY", lines)
        return

    pf = met.get("profit_factor")
    pf_str = f"{pf:.2f}" if isinstance(pf, (int, float)) else "N/D"
    lines.append(
        f"Trades: {met.get('trades', 0)} | Win%: {_fmt_pct(met.get('win_rate'))} | PF: {pf_str}"
    )
    lines.append(
        f"Net: {_fmt_money(met.get('net_pnl'))} | Best: {_fmt_money((met.get('best_trade') or {}).get('pnl'))} | Worst: {_fmt_money((met.get('worst_trade') or {}).get('pnl'))}"
    )
    lines.append("")
    lines.append("<b>Últimos:</b>")

    recent_lines: List[str] = []
    for t in trades[-MAX_HISTORY:]:
        dt = (t.get("end") or "").split("T")[0]
        sym = t.get("symbol") or "?"
        side = str(t.get("type") or "").upper() or "N/D"
        pnl = _fmt_money(t.get("pnl"))
        recent_lines.append(f"{dt}  {sym}  {side}  {pnl}")

    if recent_lines:
        recent_block = "\n".join(recent_lines)
        lines.append(f"<pre>{_h(recent_block)}</pre>")

    if len(trades) > MAX_HISTORY:
        lines.append(f"... e mais {len(trades) - MAX_HISTORY} trades.")

    send_alert("HISTORY", lines)


def _find_new_report_pdf(out_dir: Path, since_ts: float) -> Optional[Path]:
    try:
        if not out_dir.exists():
            return None
    except Exception:
        return None

    pdfs = list(out_dir.glob("report_*.pdf"))
    if not pdfs:
        return None
    pdfs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in pdfs:
        try:
            if p.stat().st_mtime >= since_ts - 2:
                return p
        except Exception:
            continue
    return None


def _send_report(reader: RiskGuardMT5Reader, args: Optional[List[str]] = None) -> None:
    choice = _parse_report_choice(args)
    if not choice:
        _set_report_pending()
        send_alert("RELATORIO", _report_menu_lines())
        return

    _clear_report_pending()
    opt = REPORT_CHOICES.get(choice) or REPORT_CHOICES["4"]
    days = opt.get("days")
    if days is None:
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
        _, until = _period_last_days(1)
    else:
        since, until = _period_last_days(int(days))

    send_alert("RELATORIO", [f"⏳ Gerando relatório ({opt['label']})... aguarde."])
    try:
        from reports import reports as reports_mod
    except Exception as exc:
        err = repr(exc)
        lines = ["❌ Módulo de relatórios indisponível."]
        if isinstance(exc, ModuleNotFoundError):
            missing = getattr(exc, "name", None) or "dependência"
            lines.append(
                f"Dependência ausente: {missing}. Rode setup_riskguard.ps1 para reinstalar."
            )
        else:
            lines.append(f"Detalhe: {err}")
        send_alert("RELATORIO", lines)
        log_event("ERROR", {"err": err}, {"module": "reports_import"})
        return

    start_ts = time.time()
    try:
        reader.ensure_connection()
    except Exception as exc:
        send_alert("RELATORIO", [
            "❌ MT5 desconectado ou indisponível no momento.",
            f"Detalhe: {repr(exc)}",
        ])
        return
    try:
        reports_mod.build_report(reader, since=since, until=until, notify=False)
    except Exception as exc:
        send_alert("RELATORIO", [f"❌ Falha ao gerar relatório: {repr(exc)}"])
        return

    pdf_path = _find_new_report_pdf(reports_mod.OUT_DIR, start_ts)
    if not pdf_path:
        send_alert("RELATORIO", ["❌ Não consegui gerar o PDF. Verifique o Playwright/Chromium."])
        return

    ok = send_document(
        str(pdf_path),
        caption=f"Relatorio {opt['label']} ({since.date()} -> {until.date()})",
    )
    if not ok:
        send_alert("RELATORIO", ["❌ Falha ao enviar o PDF pelo Telegram."])


def _send_help() -> None:
    lines = [
        "Comandos disponíveis:",
        "• /status — status completo da conta",
        "• /positions — posições abertas",
        "• /history — histórico do último mês",
        "• /relatorio — escolher período do relatório (1..5)",
    ]
    send_alert("AJUDA", lines)

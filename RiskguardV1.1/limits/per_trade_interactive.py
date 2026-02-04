from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
import html
import json
import os
import time

from logger import log_event
from mt5_reader import RiskGuardMT5Reader
from notify import send_alert, telegram_poll_chat_messages

from .guard import modify_position_sltp


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_FILE = os.path.join(ROOT, ".rg_pertrade_interactive.json")


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _h(v: Any) -> str:
    try:
        return html.escape(str(v), quote=False)
    except Exception:
        return str(v)


def _fmt_side(side: Any) -> str:
    s = str(side or "").lower()
    if s == "buy":
        return "BUY"
    if s == "sell":
        return "SELL"
    return str(side or "").upper() or "N/D"


def _fmt_volume(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except Exception:
        return "N/D"


def _fmt_price(v: Any, digits: int) -> str:
    try:
        if v is None:
            return "—"
        x = float(v)
        if x == 0.0:
            return "—"
        return f"{x:.{int(digits)}f}"
    except Exception:
        return "—"


def _pre(lines: List[str]) -> str:
    return "<pre>" + "\n".join(lines) + "</pre>"


def _risk_line(risk_pct: Any, limit_pct: float) -> str:
    if isinstance(risk_pct, (int, float)):
        return f"{float(risk_pct):.2f}% (limite {float(limit_pct):.2f}%)"
    return f"N/D (limite {float(limit_pct):.2f}%)"


def _sl_adjust_card(
    symbol: str,
    ticket: Any,
    side: Any,
    volume: Any,
    risk_pct: Any,
    limit_pct: float,
    sl_original: Any,
    sl_adjusted: Any,
    digits: int,
) -> List[str]:
    pre_lines = [
        f"Ativo:     {_h(symbol)}",
        f"Ticket:    {_h(ticket)}",
        f"Lado:      {_h(_fmt_side(side))}",
        f"Volume:    {_h(_fmt_volume(volume))}",
        f"Risco:     {_h(_risk_line(risk_pct, limit_pct))}",
        f"SL antigo: {_h(_fmt_price(sl_original, digits))}",
        f"SL novo:   {_h(_fmt_price(sl_adjusted, digits))}",
    ]
    return [_pre(pre_lines)]


def _load_state(path: str) -> Dict[str, Any]:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {"telegram_offset": None, "tickets": {}}


def _save_state(path: str, data: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass


def _coerce_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x
    except Exception:
        return None


def _round_price(price: float, digits: int) -> float:
    try:
        return float(round(float(price), int(digits)))
    except Exception:
        return float(price)


def _is_no_changes_result(res: Any) -> bool:
    """
    Detecta retorno "No changes" do MT5 para tratar como sucesso.
    """
    try:
        result = (res or {}).get("result") or {}
        rc = result.get("retcode")
        comment = str(result.get("comment") or "").lower()
        return rc == 10025 or "no changes" in comment
    except Exception:
        return False


def _compute_sl_for_risk(pos: Dict[str, Any], equity: float, max_risk_pct: float) -> Tuple[Optional[float], str]:
    """
    Calcula um SL alvo (preço) para manter o risco ≤ max_risk_pct.
    Retorna (sl_price|None, reason_if_none).
    """
    try:
        symbol_info = pos.get("symbol_info") or {}
        side = str(pos.get("type") or "").lower()
        open_price = float(pos.get("open_price") or 0.0)
        volume = float(pos.get("volume") or 0.0)

        if equity <= 0:
            return None, "equity inválida"
        if open_price <= 0:
            return None, "open_price inválido"
        if volume <= 0:
            return None, "volume inválido"
        if side not in ("buy", "sell"):
            return None, "side inválido"

        risk_money_allowed = float(equity) * float(max_risk_pct) / 100.0
        if risk_money_allowed <= 0:
            return None, "risco permitido inválido"

        tick_size = float(symbol_info.get("tick_size") or 0.0)
        tick_value = float(symbol_info.get("tick_value") or 0.0)
        if tick_size > 0 and tick_value > 0:
            price_diff = (risk_money_allowed / (tick_value * volume)) * tick_size
        else:
            # fallback FX/CFD: pontos * valor do ponto
            point = float(symbol_info.get("point") or 0.0)
            contract_size = float(symbol_info.get("contract_size") or 0.0)
            price_ref = float(pos.get("current_price") or open_price)
            if point <= 0 or contract_size <= 0 or price_ref <= 0:
                return None, "dados do símbolo insuficientes (tick/point)"
            point_value = (contract_size * point) / price_ref
            if point_value <= 0:
                return None, "point_value inválido"
            price_diff = (risk_money_allowed / (point_value * volume)) * point

        if price_diff <= 0:
            return None, "price_diff inválido"

        if side == "buy":
            sl = open_price - price_diff
        else:
            sl = open_price + price_diff

        if sl <= 0:
            return None, "SL calculado inválido"
        return float(sl), ""
    except Exception as e:
        return None, repr(e)


def _select_pending_ticket(tickets_state: Dict[str, Any]) -> Optional[str]:
    # Seleciona o mais recente (maior prompted_at) dentre os "pending".
    candidates: list[tuple[float, str]] = []
    for tk, st in (tickets_state or {}).items():
        if not isinstance(st, dict):
            continue
        if st.get("status") != "pending":
            continue
        candidates.append((float(st.get("prompted_at") or 0.0), str(tk)))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _extract_decision(text: Any) -> Optional[str]:
    """
    Extrai decisão "1" ou "2" de mensagens que não vêm exatamente como "1"/"2".
    Aceita: "1", "2", "1.", "2 - manter", "opcao 1" etc.
    """
    try:
        s = str(text or "").strip()
    except Exception:
        return None
    if not s:
        return None
    if s in ("1", "2"):
        return s
    if s[0] in ("1", "2"):
        return s[0]
    for ch in s:
        if ch in ("1", "2"):
            return ch
    return None


def enforce_per_trade_interactive_sl(
    reader: RiskGuardMT5Reader,
    max_risk_pct: float,
    timeout_minutes: int = 15,
    state_path: str = STATE_FILE,
    snapshot: Optional[Dict[str, Any]] = None,
    incoming_messages: Optional[List[Dict[str, Any]]] = None,
    incoming_next_offset: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Modo interativo (Telegram) para violações per-trade:
      - Se risco > limite: ajusta SL imediatamente para dentro do risco permitido.
      - Envia Telegram com 2 opções:
          1) Manter SL original (override até a posição fechar)
          2) Manter SL ajustado (padrão)
      - Aguarda timeout_minutes; sem resposta -> mantém SL ajustado e notifica.

    Se não conseguir ajustar SL, apenas notifica e solicita ajuste manual.
    """
    snap = snapshot or reader.snapshot()
    equity = float((snap.get("account") or {}).get("equity") or 0.0)
    positions: list[dict[str, Any]] = list(snap.get("positions") or [])

    state = _load_state(state_path)
    tickets_state: Dict[str, Any] = state.get("tickets") if isinstance(state.get("tickets"), dict) else {}

    now = time.time()
    open_tickets = {str(int(p.get("ticket", 0))) for p in positions if p.get("ticket") is not None}

    # Limpar tickets encerrados
    for tk in list(tickets_state.keys()):
        if str(tk) not in open_tickets:
            tickets_state.pop(tk, None)

    report: Dict[str, Any] = {
        "now_utc": _now_utc_iso(),
        "equity": equity,
        "max_risk_pct": max_risk_pct,
        "timeout_minutes": timeout_minutes,
        "adjusted": [],
        "override": [],
        "kept": [],
        "timed_out": [],
        "adjust_failed": [],
        "pending": [],
    }

    # Poll de decisões (somente se houver pending)
    pending_ticket_id = _select_pending_ticket(tickets_state)
    if pending_ticket_id is not None:
        tk = pending_ticket_id
        st = tickets_state.get(tk) or {}
        symbol = st.get("symbol")
        digits = int(st.get("digits") or 5)
        side = st.get("side")
        volume = st.get("volume")
        risk_pct = st.get("risk_pct_detected")

        if incoming_messages is None:
            msgs, next_offset = telegram_poll_chat_messages(state.get("telegram_offset"))
        else:
            msgs = list(incoming_messages)
            next_offset = incoming_next_offset
            if not msgs:
                extra_msgs, extra_offset = telegram_poll_chat_messages(state.get("telegram_offset"))
                if extra_msgs:
                    msgs = extra_msgs
                    next_offset = extra_offset
        if next_offset is not None:
            state["telegram_offset"] = next_offset

        # Processa somente mensagens humanas (ignora bot) e prioriza a última decisão.
        decision: Optional[str] = None
        min_ts = float(st.get("prompted_at") or 0.0)
        for m in msgs:
            if not isinstance(m, dict):
                continue
            if m.get("from_is_bot"):
                continue
            msg_date = m.get("date")
            if isinstance(msg_date, int) and min_ts:
                # Tolerância a skew de relógio (Telegram usa epoch UTC).
                # Só descarta se for MUITO mais antigo que o prompt.
                if msg_date < int(min_ts) - (6 * 3600):
                    continue
            s = m.get("text")
            d = _extract_decision(s)
            if d in ("1", "2"):
                decision = d

        if decision == "1":
            if _coerce_float(st.get("sl_original")) is None:
                # Não havia SL original; não há o que restaurar.
                st["status"] = "keep"
                st["decided_at"] = now
                tickets_state[tk] = st
                report["kept"].append({"ticket": int(tk), "symbol": symbol})
                lines = _sl_adjust_card(
                    symbol=str(symbol or "?"),
                    ticket=tk,
                    side=side,
                    volume=volume,
                    risk_pct=risk_pct,
                    limit_pct=max_risk_pct,
                    sl_original=st.get("sl_original"),
                    sl_adjusted=st.get("sl_adjusted"),
                    digits=digits,
                )
                lines.append("ℹ️ Esta posição não tinha SL antigo para restaurar.")
                lines.append(f"✅ Mantendo SL novo (dentro do risco). ⏰ {_h(_now_utc_iso())}")
                send_alert("ℹ️ RiskGuard — opção 1 indisponível", lines)
            else:
                ok, res = modify_position_sltp(
                    ticket=int(tk),
                    symbol=str(symbol),
                    sl=_coerce_float(st.get("sl_original")),
                    tp=_coerce_float(st.get("tp_original")),
                    comment="RG override SL original",
                )
                if ok:
                    st["status"] = "override"
                    st["decided_at"] = now
                    tickets_state[tk] = st
                    report["override"].append({"ticket": int(tk), "symbol": symbol})
                    lines = _sl_adjust_card(
                        symbol=str(symbol or "?"),
                        ticket=tk,
                        side=side,
                        volume=volume,
                        risk_pct=risk_pct,
                        limit_pct=max_risk_pct,
                        sl_original=st.get("sl_original"),
                        sl_adjusted=st.get("sl_adjusted"),
                        digits=digits,
                    )
                    lines.append("🟡 Override ativo até a posição fechar.")
                    lines.append(f"↩️ SL antigo restaurado. ⏰ {_h(_now_utc_iso())}")
                    send_alert("🟡 RiskGuard — SL antigo restaurado", lines)
                    log_event("PER_TRADE_OVERRIDE", {
                        "ticket": int(tk),
                        "symbol": symbol,
                        "sl_original": st.get("sl_original"),
                        "result": res,
                    }, {"module": "per_trade_interactive"})
                else:
                    lines = _sl_adjust_card(
                        symbol=str(symbol or "?"),
                        ticket=tk,
                        side=side,
                        volume=volume,
                        risk_pct=risk_pct,
                        limit_pct=max_risk_pct,
                        sl_original=st.get("sl_original"),
                        sl_adjusted=st.get("sl_adjusted"),
                        digits=digits,
                    )
                    lines.append(f"❌ Falha ao restaurar SL antigo. 📎 Motivo: {_h(str(res)[:500])}")
                    lines.append(f"🛠️ Ajuste manualmente no MT5. ⏰ {_h(_now_utc_iso())}")
                    send_alert("❌ RiskGuard — falha no override", lines)
        elif decision == "2":
            st["status"] = "keep"
            st["decided_at"] = now
            tickets_state[tk] = st
            report["kept"].append({"ticket": int(tk), "symbol": symbol})
            lines = _sl_adjust_card(
                symbol=str(symbol or "?"),
                ticket=tk,
                side=side,
                volume=volume,
                risk_pct=risk_pct,
                limit_pct=max_risk_pct,
                sl_original=st.get("sl_original"),
                sl_adjusted=st.get("sl_adjusted"),
                digits=digits,
            )
            lines.append("✅ Confirmado: mantendo SL novo (dentro do risco).")
            lines.append(f"⏰ {_h(_now_utc_iso())}")
            send_alert("✅ RiskGuard — SL novo confirmado", lines)
            log_event("PER_TRADE_KEEP_ADJUSTED", {
                "ticket": int(tk),
                "symbol": symbol,
                "sl_adjusted": st.get("sl_adjusted"),
            }, {"module": "per_trade_interactive"})

    # Timeout dos pendentes
    for tk, st in list(tickets_state.items()):
        if not isinstance(st, dict) or st.get("status") != "pending":
            continue
        deadline = float(st.get("deadline_at") or 0.0)
        if deadline and now > deadline:
            st["status"] = "timeout"
            tickets_state[tk] = st
            report["timed_out"].append({"ticket": int(tk), "symbol": st.get("symbol")})
            digits = int((st or {}).get("digits") or 5)
            lines = _sl_adjust_card(
                symbol=str(st.get("symbol") or "?"),
                ticket=tk,
                side=st.get("side"),
                volume=st.get("volume"),
                risk_pct=st.get("risk_pct_detected"),
                limit_pct=max_risk_pct,
                sl_original=st.get("sl_original"),
                sl_adjusted=st.get("sl_adjusted"),
                digits=digits,
            )
            lines.append(f"⏳ Sem resposta em {timeout_minutes} min → mantendo SL novo.")
            lines.append(f"⏰ {_h(_now_utc_iso())}")
            send_alert("⏳ RiskGuard — sem resposta", lines)
            log_event("PER_TRADE_TIMEOUT", {
                "ticket": int(tk),
                "symbol": st.get("symbol"),
                "sl_adjusted": st.get("sl_adjusted"),
            }, {"module": "per_trade_interactive"})

    # Ajuste automático de SL para violações
    for pos in positions:
        ticket = int(pos.get("ticket", 0))
        if not ticket:
            continue
        tk = str(ticket)
        symbol = str(pos.get("symbol") or "")
        side = str(pos.get("type") or "")
        volume = float(pos.get("volume") or 0.0)
        risk_pct = pos.get("risk_pct")
        sl_now = _coerce_float(pos.get("sl"))
        tp_now = _coerce_float(pos.get("tp"))
        missing_sl = bool(pos.get("missing_sl")) or (sl_now in (None, 0.0))

        st = tickets_state.get(tk) if isinstance(tickets_state.get(tk), dict) else None
        if st and st.get("status") == "override":
            continue

        violates = missing_sl or (
            isinstance(risk_pct, (int, float)) and float(risk_pct) > float(max_risk_pct) + 1e-9
        )
        if not violates:
            continue

        # Primeira vez: guardar original + criar janela
        if not st:
            digits = int((pos.get("symbol_info") or {}).get("digits") or 5)
            st = {
                "ticket": ticket,
                "symbol": symbol,
                "side": side,
                "volume": volume,
                "risk_pct_detected": float(risk_pct) if isinstance(risk_pct, (int, float)) else None,
                "limit_pct": float(max_risk_pct),
                "digits": digits,
                "sl_original": None if missing_sl else sl_now,
                "tp_original": tp_now,
                "status": "pending",
                "prompted_at": now,
                "deadline_at": now + float(timeout_minutes) * 60.0,
            }
            tickets_state[tk] = st

        # Se for a primeira violação neste setup, sincroniza offset para não
        # aplicar "1/2" antigos do histórico do bot.
        if not st.get("prompt_sent") and state.get("telegram_offset") is None:
            if incoming_messages is not None:
                if incoming_next_offset is not None:
                    state["telegram_offset"] = incoming_next_offset
            else:
                _, next_offset = telegram_poll_chat_messages(None)
                if next_offset is not None:
                    state["telegram_offset"] = next_offset

        # Calcular SL alvo e aplicar
        sl_target, reason = _compute_sl_for_risk(pos, equity=equity, max_risk_pct=max_risk_pct)
        digits = int((pos.get("symbol_info") or {}).get("digits") or 5)
        point = float((pos.get("symbol_info") or {}).get("point") or 0.0)
        sl_target_rounded = _round_price(sl_target, digits) if sl_target is not None else None

        # Já está no SL calculado? evita spam de order_send
        if sl_target_rounded is not None and sl_now is not None:
            eps = (point / 2.0) if point > 0 else 1e-9
            if abs(float(sl_now) - float(sl_target_rounded)) <= eps:
                report["pending"].append({"ticket": ticket, "symbol": symbol})
                continue

        if sl_target_rounded is None:
            if st.get("status") != "adjust_failed":
                st["status"] = "adjust_failed"
                st["adjust_failed_reason"] = reason
                tickets_state[tk] = st
                report["adjust_failed"].append({"ticket": ticket, "symbol": symbol, "reason": reason})
                digits = int(st.get("digits") or 5)
                lines = _sl_adjust_card(
                    symbol=symbol,
                    ticket=ticket,
                    side=side,
                    volume=volume,
                    risk_pct=risk_pct,
                    limit_pct=max_risk_pct,
                    sl_original=st.get("sl_original"),
                    sl_adjusted=None,
                    digits=digits,
                )
                lines.append(f"❌ Não consegui calcular um SL válido. 📎 Motivo: {_h(reason)}")
                lines.append(f"🛠️ Ajuste o SL manualmente no MT5. ⏰ {_h(_now_utc_iso())}")
                send_alert("❌ RiskGuard — ajuste de SL falhou", lines)
                log_event("PER_TRADE_ADJUST_FAILED", {
                    "ticket": ticket,
                    "symbol": symbol,
                    "risk_pct": float(risk_pct) if isinstance(risk_pct, (int, float)) else None,
                    "limit_pct": float(max_risk_pct),
                    "reason": reason,
                }, {"module": "per_trade_interactive"})
            continue

        ok, res = modify_position_sltp(ticket=ticket, symbol=symbol, sl=sl_target_rounded, tp=tp_now, comment="RG ajusta SL")
        if not ok and _is_no_changes_result(res):
            ok = True
        if ok:
            # Se antes falhou (ex.: AutoTrading OFF) e agora conseguiu, volta para pending.
            if st.get("status") == "adjust_failed":
                st.pop("adjust_failed_reason", None)
            st["status"] = "pending"
            st["sl_adjusted"] = float(sl_target_rounded)
            st["adjusted_at"] = now
            tickets_state[tk] = st
            report["adjusted"].append({"ticket": ticket, "symbol": symbol, "sl": float(sl_target_rounded)})

            # Mensagem inicial (1x por ticket)
            if not st.get("prompt_sent"):
                st["prompt_sent"] = True
                tickets_state[tk] = st
                lines = _sl_adjust_card(
                    symbol=symbol,
                    ticket=ticket,
                    side=side,
                    volume=volume,
                    risk_pct=risk_pct,
                    limit_pct=max_risk_pct,
                    sl_original=st.get("sl_original"),
                    sl_adjusted=st.get("sl_adjusted"),
                    digits=digits,
                )
                lines.append("O que deseja fazer?")
                if st.get("sl_original") is not None:
                    lines.append("<code>1</code> 🟡 Manter SL antigo (override até fechar)")
                else:
                    lines.append("<code>1</code> ⚪ Indisponível (não havia SL antigo)")
                lines.append("<code>2</code> ✅ Manter SL novo (recomendado)")
                lines.append(f"⏳ <i>Responda apenas com 1 ou 2 ({timeout_minutes} min).</i>")
                send_alert("🛡️ RiskGuard — SL ajustado", lines)
                log_event("PER_TRADE_ADJUSTED", {
                    "ticket": ticket,
                    "symbol": symbol,
                    "risk_pct": float(risk_pct) if isinstance(risk_pct, (int, float)) else None,
                    "limit_pct": float(max_risk_pct),
                    "sl_original": st.get("sl_original"),
                    "sl_adjusted": st.get("sl_adjusted"),
                    "result": res,
                }, {"module": "per_trade_interactive"})
            else:
                log_event("PER_TRADE_REAPPLY_SL", {
                    "ticket": ticket,
                    "symbol": symbol,
                    "sl_adjusted": st.get("sl_adjusted"),
                    "result": res,
                }, {"module": "per_trade_interactive"})

        else:
            if st.get("status") != "adjust_failed":
                st["status"] = "adjust_failed"
                st["adjust_failed_reason"] = str(res)
                tickets_state[tk] = st
                report["adjust_failed"].append({"ticket": ticket, "symbol": symbol, "reason": str(res)})
                digits = int(st.get("digits") or 5)
                lines = _sl_adjust_card(
                    symbol=symbol,
                    ticket=ticket,
                    side=side,
                    volume=volume,
                    risk_pct=risk_pct,
                    limit_pct=max_risk_pct,
                    sl_original=st.get("sl_original"),
                    sl_adjusted=st.get("sl_adjusted"),
                    digits=digits,
                )
                lines.append(f"❌ Não consegui ajustar o SL automaticamente. 📎 Motivo: {_h(str(res)[:500])}")
                lines.append(f"🛠️ Ajuste o SL manualmente no MT5. ⏰ {_h(_now_utc_iso())}")
                send_alert("❌ RiskGuard — ajuste de SL falhou", lines)
                log_event("PER_TRADE_ADJUST_FAILED", {
                    "ticket": ticket,
                    "symbol": symbol,
                    "risk_pct": float(risk_pct) if isinstance(risk_pct, (int, float)) else None,
                    "limit_pct": float(max_risk_pct),
                    "reason": str(res),
                }, {"module": "per_trade_interactive"})

    # Persistir
    state["tickets"] = tickets_state
    _save_state(state_path, state)

    # Reportar pendências atuais
    for tk, st in tickets_state.items():
        if isinstance(st, dict) and st.get("status") == "pending":
            report["pending"].append({"ticket": int(tk), "symbol": st.get("symbol")})

    return report

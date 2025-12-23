# main.py — RiskGuard (Modo Deus Neural) — bloqueio lógico, sem tocar no botão
from __future__ import annotations
import os, sys, time, json, random
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List, Dict, Any

# ====== STDOUT sem buffer ======
os.environ["PYTHONUNBUFFERED"] = "1"
# Força UTF-8 nos prints (evita UnicodeEncodeError em consoles CP1252)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ====== Módulos do projeto ======
try:
    from mt5_reader import RiskGuardMT5Reader
except ModuleNotFoundError as exc:
    if exc.name == "MetaTrader5":
        print("ERRO: pacote MetaTrader5 nao encontrado.", flush=True)
        print("Instale o MetaTrader 5 e execute o RiskGuard usando o venv:", flush=True)
        print("  1) powershell -NoProfile -ExecutionPolicy Bypass -File .\\setup_riskguard.ps1", flush=True)
        print("  2) .\\venv\\Scripts\\python.exe .\\main.py", flush=True)
        sys.exit(1)
    raise
from limits.limits import enforce_aggregate_risk, risk_block_status
from logger import log_event
from notify import send_alert, set_ident_from_snapshot
from rg_config import get_bool, get_float, get_int, get_optional_float, get_optional_int


# Opcionais (carregar sem quebrar)
try:
    from limits.guard import close_position_full  # fará o toggle neural internamente
except Exception:
    close_position_full = None

try:
    from limits.dd_kill import enforce_drawdown
except Exception:
    enforce_drawdown = None

try:
    import reports as reports_mod
except Exception:
    reports_mod = None

# ====== CONFIG ======
CANDIDATE_TERMINALS = [
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\XM Global MT5\terminal64.exe",
    r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
    r"C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\terminal64.exe",
]
def _detect_terminal() -> Optional[str]:
    for p in CANDIDATE_TERMINALS:
        if os.path.exists(p):
            return p
    return None

DEFAULT_TERMINAL_PATH = _detect_terminal() or r"C:\Program Files\MetaTrader 5\terminal64.exe"

LOOP_SECONDS = get_float("LOOP_SECONDS", 2.0)  # intervalo do loop principal
SNAPSHOT_MAX_FAILS_BEFORE_RECONNECT = 6   # após X falhas seguidas, tenta reconectar
CONNECT_BACKOFF_MAX = 60                  # backoff máx em segs

PERTRADE_MAX_RISK = get_float("PERTRADE_MAX_RISK", 1.0)          # %
AGGREGATE_MAX_RISK = get_float("AGGREGATE_MAX_RISK", 5.0)         # %
AGGREGATE_MAX_ATTEMPTS = get_int("AGGREGATE_MAX_ATTEMPTS", 3)     # segue monitorando tentativas (não toca no botão)

DD_LIMIT_PCT = get_optional_float("DD_LIMIT_PCT", 20.0)           # % (None para desativar)
DD_COOLDOWN_DAYS = get_int("DD_COOLDOWN_DAYS", 30)

NEWS_WINDOW_MINUTES = get_int("NEWS_WINDOW_MINUTES", 60)
NEWS_RECENT_SECONDS = get_optional_int("NEWS_RECENT_SECONDS", 180)
CALENDAR_TZ = "America/Sao_Paulo"
NEWS_WINDOW_ENABLED = get_bool("NEWS_WINDOW_ENABLED", False)  # deixe False para isolar o módulo de notícias enquanto ajusta

STATE_FILE = os.path.join(ROOT, ".rg_state.json")
MONTHLY_FLAG = os.path.join(ROOT, "reports", ".monthly.flag")
TERMINAL_CFG_FILE = os.path.join(ROOT, ".rg_terminal.json")

# Rate-limit e debounce (telegram e afins)
ALERT_MIN_INTERVALS = {
    "PER-TRADE": 5,          # segs entre alertas deste tipo
    "LIMITS (5%)": 10,
    "DD KILL": 10,
    "NEWS WINDOW": 10,
    "ERRO": 20,
    "STATUS": 5
}
_last_alert_ts: Dict[str, float] = {}     # nome -> epoch

# ====== Helpers ======
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _fmt_money(x) -> str:
    try: return f"${float(x):,.2f}"
    except Exception: return "N/D"

def _fmt_pct(x) -> str:
    try: return f"{float(x):.2f}%"
    except Exception: return "N/D"

def _load_json(path: str, default: Any):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _save_json(path: str, data: Any):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _load_terminal_cfg() -> Dict[str, Any]:
    return _load_json(TERMINAL_CFG_FILE, {})

def _save_terminal_cfg(path: str):
    _save_json(TERMINAL_CFG_FILE, {"terminal_path": path})

def _select_terminal_path(default_path: Optional[str]) -> str:
    """
    Lista candidatos e permite o usuário escolher qual terminal64.exe usar.
    Se nada for selecionado, volta a pedir até receber um caminho válido.
    """
    cfg = _load_terminal_cfg()
    saved = cfg.get("terminal_path")

    candidates = []
    if saved and os.path.exists(saved):
        candidates.append(("config", saved))
    for p in CANDIDATE_TERMINALS:
        if os.path.exists(p) and (p not in [c[1] for c in candidates]):
            candidates.append(("detectado", p))
    if default_path and os.path.exists(default_path) and (default_path not in [c[1] for c in candidates]):
        candidates.append(("default", default_path))

    if not candidates:
        print("ERRO: terminal64.exe do MetaTrader 5 nao encontrado.", flush=True)
        print("Instale o MetaTrader 5 e tente novamente.", flush=True)
        print("Opcional: salve o caminho em .rg_terminal.json (campo terminal_path).", flush=True)
        sys.exit(1)

    while True:
        print("\nSelecione o terminal MT5 para o RiskGuard:", flush=True)
        for idx, (_, path) in enumerate(candidates, 1):
            print(f"  [{idx}] {path}", flush=True)
        print("  [M] Digitar caminho manual (ex.: C:\\Program Files\\MetaTrader 5\\terminal64.exe)", flush=True)
        if candidates:
            print("  (ENTER para usar a primeira opção listada)", flush=True)

        choice = input("Opção: ").strip()
        if choice == "" and not candidates:
            choice = "m"
        if choice == "" and candidates:
            selected = candidates[0][1]
        elif choice.lower() == "m":
            entered = input("Caminho completo para terminal64.exe: ").strip().strip('"')
            selected = entered
        else:
            try:
                idx = int(choice)
                if 1 <= idx <= len(candidates):
                    selected = candidates[idx - 1][1]
                else:
                    print("Opção inválida.", flush=True)
                    continue
            except Exception:
                print("Opção inválida.", flush=True)
                continue

        if os.path.exists(selected):
            _save_terminal_cfg(selected)
            return selected
        else:
            print("Caminho não encontrado. Tente novamente.", flush=True)
            continue

def _rate_limited_alert(kind: str, lines: List[str]):
    now = time.time()
    gap = ALERT_MIN_INTERVALS.get(kind, 5)
    last = _last_alert_ts.get(kind, 0.0)
    if now - last >= gap:
        try:
            send_alert(kind, lines)
        finally:
            _last_alert_ts[kind] = now

# ====== Calendário opcional (investpy) ======
def _fetch_calendar_df():
    try:
        import investpy
        today = date.today()
        d0 = (today - timedelta(days=1)).strftime("%d/%m/%Y")
        d1 = (today + timedelta(days=1)).strftime("%d/%m/%Y")
        return investpy.economic_calendar(from_date=d0, to_date=d1)
    except Exception:
        return None

# ====== Per-trade inline (1% + SL) ======
def enforce_per_trade_inline(reader: RiskGuardMT5Reader, max_risk_pct: float = 1.0):
    """
    Fecha qualquer posição SEM SL ou com risco > limite.
    Encerramento é feito via guard.close_position_full (que usa toggle neural interno).
    """
    actions = []
    snap = reader.snapshot()
    equity = float((snap.get("account") or {}).get("equity") or 0.0)

    for p in snap.get("positions", []):
        ticket = int(p.get("ticket", 0))
        sym = p.get("symbol")
        side = p.get("type")
        vol = float(p.get("volume") or 0.0)

        # SL real (manual ou EA)
        sl = p.get("sl") or p.get("sl_price") or 0.0
        missing_sl = (sl is None or sl == 0 or sl == 0.0)

        # Risco aproximado se houver SL
        r_pct = p.get("risk_pct")
        if r_pct is None and not missing_sl:
            try:
                entry = float(p.get("price_open") or 0.0)
                sl_price = float(sl)
                point = reader.symbol_point(sym)
                distance = abs(entry - sl_price)
                risk_value = (distance / point) * reader.symbol_tick_value(sym) * vol
                if equity > 0:
                    r_pct = (risk_value / equity) * 100.0
            except Exception:
                r_pct = None

        # Regras de fechamento
        violou = missing_sl or (isinstance(r_pct, (int, float)) and r_pct > max_risk_pct + 1e-9)

        if violou and close_position_full and ticket:
            try:
                ok, res = close_position_full(ticket, sym, side, vol, comment="RG per-trade")
            except Exception as e:
                ok, res = False, repr(e)

            print(f"[⚠️] MODE GOD → FECHANDO {sym} #{ticket} — "
                  f"{'SEM SL' if missing_sl else f'risco {_fmt_pct(r_pct)}'} → {'✅ OK' if ok else '❌ FALHA'}",
                  flush=True)

            evt = {
                "equity": equity, "ticket": ticket, "symbol": sym,
                "risk_pct": float(r_pct) if isinstance(r_pct, (int,float)) else None,
                "missing_sl": missing_sl, "ok": ok, "res": res
            }
            actions.append(evt)
            try:
                log_event("PER_TRADE", evt, {"module": "per-trade"})
            except Exception:
                pass

    return actions

# ====== Relatório mensal (dia 1, 1x) ======
def _once_monthly_generate_and_send(reader: RiskGuardMT5Reader):
    today = date.today()
    if today.day != 1:
        if os.path.exists(MONTHLY_FLAG):
            try: os.remove(MONTHLY_FLAG)
            except Exception: pass
        return
    if os.path.exists(MONTHLY_FLAG):
        return

    ok = False
    if reports_mod is not None:
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        since = datetime(last_month_start.year, last_month_start.month, 1, tzinfo=timezone.utc)
        until = datetime(first_this.year, first_this.month, 1, tzinfo=timezone.utc)

        for fname in ("generate_and_send_report","generate_full_report","generate_monthly_report",
                      "build_and_send_report","run_monthly"):
            try:
                fn = getattr(reports_mod, fname, None)
                if callable(fn):
                    try:
                        fn(reader=reader, since=since, until=until)
                    except TypeError:
                        fn(reader, since, until)  # fallback posicional
                    ok = True
                    break
            except Exception:
                pass
        if ok:
            log_event("REPORT", {"since": since.isoformat(), "until": until.isoformat()}, {"module":"reports"})
    _save_json(MONTHLY_FLAG, {"ts": _now_utc().isoformat(), "status": "ok" if ok else "skip"})

# ====== Backoff helper ======
def _sleep_backoff(base: float, cap: float):
    # base * 2^n com jitter [0.5..1.0], limitado a cap
    n = 0
    while True:
        import random
        jitter = random.uniform(0.5, 1.0)
        yield min(cap, base * (2 ** n) * jitter)
        n += 1

# ====== MAIN ======
def main():
    print("🔌 Iniciando RiskGuard...", flush=True)

    terminal_path = _select_terminal_path(DEFAULT_TERMINAL_PATH)
    reader = RiskGuardMT5Reader(path=terminal_path)

    # Conexão com backoff
    attempts = 0
    backoff = _sleep_backoff(base=3, cap=CONNECT_BACKOFF_MAX)
    while True:
        try:
            attempts += 1
            ok = reader.connect()
            if ok:
                print("✅ Conectado ao MT5.", flush=True)
                break
            else:
                print(f"❌ connect() retornou False (tentativa {attempts}). Verifique login e caminho:\n{terminal_path}",
                      flush=True)
        except Exception as e:
            print(f"❌ Erro conectando ao MT5 (tentativa {attempts}): {e}", flush=True)
        time.sleep(next(backoff))

    # Ident para notificações
    try:
        set_ident_from_snapshot(reader.snapshot(), label="RiskGuard")
    except Exception:
        pass

    state = _load_json(STATE_FILE, {"tickets": []})
    last_calendar_df, last_calendar_ts = None, 0.0
    snapshot_fails = 0

    print(f"🚀 RiskGuard iniciado. Loop a cada {LOOP_SECONDS}s.\n", flush=True)

    while True:
        loop_start = time.time()
        try:
            # === NEW: não interfere enquanto o guard estiver fechando ordens ===
            if os.path.exists(os.path.join(ROOT, ".guard_lock")):
                time.sleep(1)
                continue
            try:
                snap = reader.snapshot()
                snapshot_fails = 0
            except Exception as e:
                snapshot_fails += 1
                print(f"❌ snapshot() falhou ({snapshot_fails}/{SNAPSHOT_MAX_FAILS_BEFORE_RECONNECT}): {e}", flush=True)
                log_event("ERROR", {"err": repr(e), "stage": "snapshot"}, {"module": "reader"})
                if snapshot_fails >= SNAPSHOT_MAX_FAILS_BEFORE_RECONNECT:
                    rb = _sleep_backoff(base=2, cap=20)
                    re_ok = False
                    for _ in range(4):
                        try:
                            if reader.connect():
                                re_ok = True
                                print("🔄 Reconectado ao MT5 após falhas de snapshot.", flush=True)
                                break
                        except Exception:
                            pass
                        time.sleep(next(rb))
                    snapshot_fails = 0 if re_ok else snapshot_fails
                time.sleep(5)
                continue

            acct = snap.get("account") or {}
            eq = _fmt_money(acct.get("equity"))
            positions = snap.get("positions") or []
            tickets = [int(p.get("ticket", 0)) for p in positions if p.get("ticket") is not None]
            prev = set(state.get("tickets", []))
            curr = set(tickets)
            new_tickets = [t for t in curr if t not in prev]

            # Heartbeat
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Equity={eq} | Posições={len(tickets)}", flush=True)
            log_event("HEARTBEAT", {"equity": eq, "positions": len(tickets)}, {"module":"main"})

            # 1) Per-trade (1% + SL)
            try:
                pt_actions = enforce_per_trade_inline(reader, PERTRADE_MAX_RISK)
                if pt_actions:
                    lines = []
                    for a in pt_actions:
                        motivo = "SEM SL" if a.get("missing_sl") else f"risco {_fmt_pct(a.get('risk_pct'))}"
                        status = "✅" if a.get("ok") else "❌"
                        lines.append(f"{status} {a.get('symbol')} #{a.get('ticket')} — {motivo}")
                    _rate_limited_alert("PER-TRADE", lines)
            except Exception as e:
                log_event("ERROR", {"err": repr(e)}, {"module":"per-trade"})

            # 2) Agregado (5%) + tentativas (bloqueio lógico, sem botão)
            try:
                rep = enforce_aggregate_risk(reader,
                                             threshold_pct=AGGREGATE_MAX_RISK,
                                             max_block_attempts=AGGREGATE_MAX_ATTEMPTS)

                closed = rep.get("closed") or rep.get("tickets_closed") or rep.get("just_closed") or []
                if isinstance(closed, dict): closed = [closed]
                qty = len(closed) if hasattr(closed, "__len__") else (1 if closed else 0)

                if qty or rep.get("risk_block_active_after") or rep.get("attempts_after") != rep.get("attempts_before"):
                    lines = []
                    if qty:
                        shown = 0
                        for c in closed:
                            if shown >= 5: break
                            if isinstance(c, dict):
                                sym = c.get("symbol") or "?"
                                tk  = c.get("ticket") or c.get("id") or "?"
                                rsn = c.get("reason") or "limite 5%"
                                lines.append(f"✅ {sym} #{tk} — {rsn}")
                            else:
                                lines.append(f"✅ ticket {c}")
                            shown += 1
                        if qty > shown:
                            lines.append(f"... e +{qty - shown} tickets")
                    lines.append(f"Risco total ≤5%? {'SIM' if not rep.get('risk_block_active_after') else 'NÃO'}")
                    lines.append(f"Tentativas EA: {rep.get('attempts_after', 0)}")
                    if rep.get("risk_block_active_after"):
                        lines.append("🚫 BLOQUEIO DE RISCO ATIVO")
                    _rate_limited_alert("LIMITS (5%)", lines)

                if rep.get("risk_block_active_after"):
                    print("🚫 Bloqueio de risco ativo (>5%).", flush=True)
                    log_event("LIMITS", {"status": "bloqueio ativo"}, {"module":"limits"})
            except Exception as e:
                log_event("ERROR", {"err": repr(e)}, {"module":"limits"})

            # 3) Drawdown (bloqueio lógico; quem fecha ordens é o guard)
            if enforce_drawdown is not None and DD_LIMIT_PCT is not None:
                try:
                    dd_rep = enforce_drawdown(reader, dd_limit_pct=DD_LIMIT_PCT, cooldown_days=DD_COOLDOWN_DAYS)
                    if dd_rep.get("tripped"):
                        _rate_limited_alert("DD KILL", ["💀 DD atingido: conta em cooldown (fechamentos executados)."])
                        log_event("DD_KILL", {"rep": dd_rep}, {"module":"dd"})
                except Exception as e:
                    log_event("ERROR", {"err": repr(e)}, {"module":"dd"})

            # 4) Notícias (apenas ordens recém-abertas) — sem tocar no botão
            if NEWS_WINDOW_ENABLED and new_tickets:
                try:
                    from news_window import enforce_news_window  # mantenha o nome real do seu módulo
                except Exception:
                    enforce_news_window = None

                if enforce_news_window:
                    if (time.time() - last_calendar_ts) > 300:
                        last_calendar_df = _fetch_calendar_df()
                        last_calendar_ts = time.time()

                    for t in new_tickets:
                        try:
                            rep_news = enforce_news_window(
                                reader,
                                investing_calendar_df=last_calendar_df,
                                calendar_tz=CALENDAR_TZ,
                                window_minutes=NEWS_WINDOW_MINUTES,
                                just_opened_ticket=int(t),
                                recent_seconds=NEWS_RECENT_SECONDS
                            )
                            try:
                                closed = (rep_news.get("closed") or rep_news.get("just_closed")
                                          or rep_news.get("tickets_closed") or [])
                                if isinstance(closed, dict): closed = [closed]
                                if closed:
                                    lines = []
                                    for c in closed:
                                        sym = (c or {}).get("symbol") or "?"
                                        tk  = (c or {}).get("ticket") or (c or {}).get("id") or "?"
                                        rsn = (c or {}).get("reason") or "janela de notícia"
                                        lines.append(f"✅ {sym} #{tk} — {rsn}")
                                    _rate_limited_alert("NEWS WINDOW", lines)
                            except Exception as e:
                                log_event("ERROR", {"err": repr(e)}, {"module": "news_alert"})

                        except Exception as e:
                            log_event("ERROR", {"err": repr(e)}, {"module": "news"})

            # 5) Relatório mensal (dia 1)
            _once_monthly_generate_and_send(reader)

            # Persistir estado
            prev = set(state.get("tickets", []))
            curr = set(tickets)
            new_tickets = [t for t in curr if t not in prev]

            # ✅ ATUALIZA AQUI (antes de usar em news_window)
            state["tickets"] = tickets
            _save_json(STATE_FILE, state)
            

            # Dormir respeitando o período do loop
            elapsed = time.time() - loop_start
            to_sleep = max(0.0, LOOP_SECONDS - elapsed)
            time.sleep(to_sleep)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print("❌ ERRO no loop:", e, flush=True)
            log_event("ERROR", {"err": repr(e)}, {"module":"main"})
            _rate_limited_alert("ERRO", [repr(e)])
            time.sleep(10)

    # Encerramento
    try:
        reader.shutdown()
    except Exception:
        pass
    print("🛑 RiskGuard finalizado.", flush=True)

if __name__ == "__main__":
    main()

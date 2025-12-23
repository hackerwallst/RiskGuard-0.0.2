# dd_kill.py — Função 4: DD kill + cooldown + 2FA local (PIN) — versão robusta
from __future__ import annotations
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timedelta, timezone
import os, json, hashlib, tempfile, io, sys
import pytz

from rg_config import get_float, get_int
from logger import log_event
from notify import notify_limits

from mt5_reader import RiskGuardMT5Reader
from limits.guard import close_position_full  # deve aceitar: ticket, symbol, side, volume, comment
from limits.kill_switch import set_kill_until, kill_status

# --------------------------------------------------------------------------------------
# Config/estado
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".riskguard_dd.json")
LOCK_FILE  = os.path.join(HERE, ".riskguard_dd.lock")
DEFAULT_DD_LIMIT_PCT = get_float("DD_LIMIT_PCT", 20.0)
DEFAULT_COOLDOWN_DAYS = get_int("DD_COOLDOWN_DAYS", 30)

# --------------------------------------------------------------------------------------
# Utilitários de tempo/ISO
def _now_utc() -> datetime:
    # Mantém tz-aware
    return datetime.now(timezone.utc)

def _iso_z(dt: datetime) -> str:
    # ISO sempre com Z
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _from_iso_any(iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None

# --------------------------------------------------------------------------------------
# File lock (cross-platform simples)
class _FileLock:
    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def __enter__(self):
        # Garante existência do arquivo de lock
        self._fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._fh.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    try:
                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
            finally:
                self._fh.close()
        return False

# --------------------------------------------------------------------------------------
# Persistência atômica do estado
def _load() -> Dict[str, Any]:
    # Sem lock para leitura rápida; gravações usarão lock
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save(d: Dict[str, Any]) -> None:
    # Escrita atômica + lock
    with _FileLock(LOCK_FILE):
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".rgdd.", dir=HERE)
        try:
            with io.open(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, STATE_FILE)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

# --------------------------------------------------------------------------------------
# Cripto simples do PIN
def _sha(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()

# --------------------------------------------------------------------------------------
# 2FA (PIN local)
def set_2fa_pin(pin: str) -> None:
    """Define/atualiza o PIN 2FA (hash). Requer >= 4 caracteres."""
    if not pin or len(pin) < 4:
        raise ValueError("PIN muito curto (mínimo 4 caracteres).")
    st = _load()
    st["twofa_sha256"] = _sha(pin)
    _save(st)

def unlock_with_pin(pin: str) -> Dict[str, Any]:
    """Tenta desbloquear após cooldown usando o PIN 2FA."""
    st = _load()
    want = st.get("twofa_sha256")
    ok = (want is not None and _sha(pin) == want)
    rep = {"ok": ok, "reason": None}
    if not ok:
        rep["reason"] = "PIN inválido ou 2FA não configurado."
        return rep

    # Desbloqueia: desativa bloqueio pendente pós-cooldown
    st["awaiting_unlock"] = False
    # Não mexe em peak_equity nem dd_limit_pct
    # Limpa cooldown se por algum motivo ainda existir
    if "cooldown_until" in st:
        try:
            cu = _from_iso_any(st["cooldown_until"])
            if not cu or _now_utc() >= cu.astimezone(timezone.utc):
                del st["cooldown_until"]
        except Exception:
            del st["cooldown_until"]
    _save(st)
    rep["reason"] = "Desbloqueado."
    return rep

# --------------------------------------------------------------------------------------
# Suporte a simulação (para testes)
def _set_peak_for_simulated_dd(target_dd_pct: float, current_equity: float) -> float:
    """
    Define peak_equity no estado para simular um drawdown alvo (em %) dado a equity atual.
    Ex.: target_dd_pct=20 e equity=1000 -> peak=1250 (porque 20% abaixo de 1250 é 1000).
    Retorna o peak definido.
    """
    if target_dd_pct <= 0 or current_equity <= 0:
        raise ValueError("Parâmetros inválidos para simulação de DD.")
    peak = current_equity / (1.0 - target_dd_pct / 100.0)
    st = _load()
    st["peak_equity"] = float(peak)
    _save(st)
    return float(peak)

# --------------------------------------------------------------------------------------
# Status público
def dd_status() -> Dict[str, Any]:
    st = _load()
    cooldown_iso = st.get("cooldown_until")
    if cooldown_iso:
        cu = _from_iso_any(cooldown_iso)
        if not cu or _now_utc() >= cu.astimezone(timezone.utc):
            cooldown_iso = None  # expirada ou inválida → não mostrar
    return {
        "peak_equity": st.get("peak_equity"),
        "dd_limit_pct": st.get("dd_limit_pct"),
        "cooldown_until": cooldown_iso,
        "awaiting_unlock": bool(st.get("awaiting_unlock", False)),
        "twofa_configured": bool(st.get("twofa_sha256")),
    }

# --------------------------------------------------------------------------------------
# Helpers
def _coerce_side_for_close(side_val: Any) -> Any:
    """
    Adapte aqui se close_position_full exigir um formato específico.
    Ex.: se snapshot traz 0/1 e o close exige 'BUY'/'SELL':
        return "BUY" if side_val in (0, "BUY", "buy") else "SELL"
    Mantém pass-through por padrão.
    """
    return side_val

# --------------------------------------------------------------------------------------
# Lógica principal
def enforce_drawdown(reader: RiskGuardMT5Reader,
                     dd_limit_pct: float = DEFAULT_DD_LIMIT_PCT,
                     cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
                     mode: str = "logical") -> Dict[str, Any]:
    """
    - Mantém/atualiza pico de equity automaticamente (fora de cooldown).
    - Se DD >= limite: fecha TODAS as posições e ativa kill lógico por 'cooldown_days'.
      Após o cooldown, permanece bloqueado até 'unlock_with_pin' (2FA).
    - mode:
        - "logical": apenas kill_switch lógico.
        - "physical": idem + ganchos para desligar AutoTrading (se integrados externamente).
    """
    snap = reader.snapshot()
    equity = float(snap["account"]["equity"])
    positions = list(snap.get("positions", []))

    st = _load()
    peak = st.get("peak_equity")
    if not isinstance(peak, (int, float)) or peak <= 0:
        peak = equity

    # Cooldown atual
    cooldown_iso = st.get("cooldown_until")
    in_cooldown = False
    cu_dt = None
    if cooldown_iso:
        cu_dt = _from_iso_any(cooldown_iso)
        if cu_dt:
            in_cooldown = (_now_utc() < cu_dt.astimezone(timezone.utc))
        else:
            in_cooldown = False

    # Atualiza pico somente fora do cooldown e quando equity faz nova máxima
    if not in_cooldown and equity > peak:
        peak = equity

    dd_pct = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak * 100.0)
    awaiting_unlock = bool(st.get("awaiting_unlock", False))

    rep: Dict[str, Any] = {
        "now_utc": _iso_z(_now_utc()),
        "equity": equity,
        "peak_equity": peak,
        "dd_pct": dd_pct,
        "dd_limit_pct": dd_limit_pct,
        "closed": [],
        "failed": [],
        "cooldown_until": cooldown_iso,
        "in_cooldown": in_cooldown,
        "awaiting_unlock": awaiting_unlock,
        "tripped": False,
        "mode": mode,
    }

    # Trip do DD
    if dd_pct >= dd_limit_pct - 1e-9:
        rep["tripped"] = True

        # Fecha TODAS as posições — resiliente
        for pos in positions:
            try:
                ticket = int(pos.get("ticket"))
                symbol = pos.get("symbol")
                volume = float(pos.get("volume", 0.0))
                side = _coerce_side_for_close(pos.get("type"))
                if volume <= 0:
                    raise ValueError("volume inválido para fechamento total")
                ok, res = close_position_full(
                    ticket=ticket,
                    symbol=symbol,
                    side=side,
                    volume=volume,
                    comment="RG DD kill"
                )
                (rep["closed"] if ok else rep["failed"]).append(
                    {"ticket": ticket, "symbol": symbol, "result": res}
                )
            except Exception as e:
                rep["failed"].append({
                    "ticket": int(pos.get("ticket", -1)) if str(pos.get("ticket", "")).isdigit() else pos.get("ticket"),
                    "symbol": pos.get("symbol"),
                    "error": str(e)
                })

        # Ativa kill switch lógico até fim do cooldown
        until = _now_utc() + timedelta(days=cooldown_days)
        set_kill_until(until)
        cu_iso = _iso_z(until)
        rep["cooldown_until"] = cu_iso

        # Após cooldown, manter bloqueado até 2FA
        st["awaiting_unlock"] = True
        st["cooldown_until"] = cu_iso

        # Modo físico (gancho): aqui você pode acionar integração para desligar AutoTrading
        if mode.lower() == "physical":
            # Ex.: enviar sinal para um watcher que desativa AutoTrading no terminal alvo
            # (mantenho como log para não criar dependência direta)
            log_event("AUTOTRADING_OFF_REQUEST", {"reason": "DD_TRIPPED"}, context={"module": "dd"})
    else:
        # Cooldown terminou, porém ainda aguardando 2FA → manter kill e limpar cooldown do estado
        if not in_cooldown and awaiting_unlock:
            ks = kill_status()
            if not ks.get("active"):
                # reativa por segurança por mais 24h (renova continuamente até desbloquear)
                set_kill_until(_now_utc() + timedelta(hours=24))
            rep["cooldown_until"] = None
            if "cooldown_until" in st:
                del st["cooldown_until"]
        # Se não está aguardando unlock e cooldown presente mas vencido → limpar
        elif cooldown_iso:
            cu_dt = _from_iso_any(cooldown_iso)
            if (not cu_dt) or (_now_utc() >= cu_dt.astimezone(timezone.utc)):
                rep["cooldown_until"] = None
                if "cooldown_until" in st:
                    del st["cooldown_until"]

    # Persiste estado consolidado (com lock/atômico)
    st["peak_equity"] = float(peak)
    st["dd_limit_pct"] = float(dd_limit_pct)
    _save(st)

    return rep

# --------------------------------------------------------------------------------------
# Execução direta mínima
if __name__ == "__main__":
    import argparse
    from pprint import pprint

    parser = argparse.ArgumentParser()
    parser.add_argument("--set-pin", type=str, help="Define/atualiza o PIN 2FA (mín. 4 chars)")
    parser.add_argument("--unlock", type=str, help="Desbloqueia após cooldown usando PIN")
    parser.add_argument("--dd", type=float, default=DEFAULT_DD_LIMIT_PCT, help="Limite de DD em % (default 20)")
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_DAYS, help="Dias de cooldown (default 30)")
    parser.add_argument("--mode", type=str, default="logical", choices=["logical", "physical"], help="Tipo de kill")
    parser.add_argument("--simulate-dd", type=float, help="Simular DD alvo (em %): ajusta peak_equity e roda a checagem")
    parser.add_argument("--mt5-path", type=str, default=r"C:\Program Files\MetaTrader 5\terminal64.exe", help="Caminho do terminal MT5")
    args = parser.parse_args()

    if args.set_pin:
        set_2fa_pin(args.set_pin)
        print("2FA PIN configurado.")
        raise SystemExit(0)

    if args.unlock:
        print(unlock_with_pin(args.unlock))
        raise SystemExit(0)

    # Conecta MT5
    r = RiskGuardMT5Reader(path=args.mt5_path)
    assert r.connect(), "Falha ao conectar no MT5"

    try:
        if args.simulate_dd:
            snap = r.snapshot()
            eq = float(snap["account"]["equity"])
            peak = _set_peak_for_simulated_dd(args.simulate_dd, eq)
            print(f"[simulate] equity_atual={eq:.2f} | peak_definido={peak:.2f} -> DD alvo {args.simulate_dd:.2f}%")

        rep = enforce_drawdown(
            r,
            dd_limit_pct=args.dd,
            cooldown_days=args.cooldown,
            mode=args.mode
        )

        # Logar/Notificar somente quando relevante
        should_log = bool(
            rep.get("tripped")
            or rep.get("closed")
            or rep.get("failed")
            or rep.get("awaiting_unlock")
            or rep.get("in_cooldown")
        )
        if should_log:
            log_event("DD_KILL", {
                "dd_pct": rep["dd_pct"],
                "closed": rep["closed"],
                "failed": rep["failed"],
                "cooldown_until": rep["cooldown_until"],
                "awaiting_unlock": rep["awaiting_unlock"],
                "in_cooldown": rep["in_cooldown"],
                "mode": rep.get("mode"),
            }, context={"module": "dd"})
            notify_limits(rep)

        pprint(rep)
        print("status:", dd_status())
    finally:
        r.shutdown()

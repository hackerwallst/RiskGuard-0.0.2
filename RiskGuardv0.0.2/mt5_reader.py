# riskguard_reader_mt5.py
# Função 1 — Leitura de conta/ordens/símbolos + cálculos básicos (MT5)
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple
import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone

# ---------------------------
# Utilidades
# ---------------------------
def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def _side_name(pos_type: int) -> str:
    # 0=buy, 1=sell no MT5
    return "buy" if pos_type == mt5.POSITION_TYPE_BUY else "sell"

def _price_for_risk(side: str, open_price: float, sl: Optional[float]) -> Optional[float]:
    if sl is None or sl == 0.0:
        return None
    return abs(open_price - sl)

def _point_value(contract_size: float, point: float, price_ref: float) -> float:
    # valor do ponto em moeda da conta (aprox. para FX/CFD)
    if price_ref <= 0:
        return 0.0
    return (contract_size * point) / price_ref

def _risk_money_per_lot(price_diff: float, sym: Dict[str, float], price_ref: float) -> float:
    """
    Converte diferença de preço (price_diff) em dinheiro por LOTE usando
    preferencialmente tick_value/tick_size (MT5). Fallback para fórmula FX.
    """
    tick_size = float(sym.get("tick_size", 0.0))
    tick_value = float(sym.get("tick_value", 0.0))
    if tick_size > 0 and tick_value > 0:
        return (price_diff / tick_size) * tick_value
    # fallback (FX): pontos * valor do ponto por lote
    point = float(sym.get("point", 0.0))
    if point > 0:
        pv = _point_value(float(sym.get("contract_size", 0.0)), point, price_ref)
        return (price_diff / point) * pv
    return 0.0

def _current_price_for_side(symbol: str, side: str) -> Optional[float]:
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None
    # Para BUY, saída é via Bid; para SELL, saída é via Ask
    return tick.bid if side == "buy" else tick.ask

# ---------------------------
# Leitor principal
# ---------------------------
class RiskGuardMT5Reader:
    def __init__(self, login: Optional[int] = None, password: Optional[str] = None,
                 server: Optional[str] = None, path: Optional[str] = None, timeout: int = 10):
        """
        Se login/password/server forem None, usa sessão já autenticada no terminal.
        """
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self.timeout = timeout
        self._connected = False

    # ---- conexão
    def connect(self) -> bool:
        if not mt5.initialize(path=self.path):
            return False
        if self.login and self.password and self.server:
            if not mt5.login(self.login, password=self.password, server=self.server):
                mt5.shutdown()
                return False
        self._connected = True
        return True

    def ensure_connection(self) -> None:
        if self._connected:
            return
        if not self.connect():
            raise RuntimeError("Falha ao conectar no MetaTrader 5 (initialize/login).")

    def shutdown(self) -> None:
        try:
            mt5.shutdown()
        finally:
            self._connected = False

    # ---- conta
    def read_account(self) -> Dict[str, Any]:
        self.ensure_connection()
        acc = mt5.account_info()
        if not acc:
            raise RuntimeError("Não foi possível ler account_info().")
        return {
            "balance": float(acc.balance),
            "equity": float(acc.equity),
            "profit": float(acc.profit),
            "margin": float(acc.margin),
            "margin_free": float(acc.margin_free),
            "margin_level": float(acc.margin_level),
            "leverage": int(acc.leverage),
            "currency": acc.currency,
            "trade_allowed": bool(acc.trade_allowed),
            "login": int(acc.login),
            "server": acc.server
        }

    # ---- símbolo
    def _read_symbol_info(self, symbol: str) -> Dict[str, Any]:
        info = mt5.symbol_info(symbol)
        if not info or not info.visible:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
        if not info:
            raise RuntimeError(f"Símbolo indisponível: {symbol}")
        return {
            "symbol": symbol,
            "digits": int(info.digits),
            "point": float(info.point),
            "tick_size": float(info.trade_tick_size),
            "tick_value": float(info.trade_tick_value),
            "contract_size": float(info.trade_contract_size),
            "vol_min": float(info.volume_min)
        }

    # ---- slippage (melhor esforço via deals próximos da abertura)
    def _estimate_slippage(self, position_ticket: int, symbol: str,
                           side: str, open_time: datetime,
                           open_price: float, point: float,
                           contract_size: float) -> Dict[str, float]:
        """
        Heurística: busca deals do símbolo próximos ao horário de abertura (+/- 60s) e
        infere o preço de execução. Se não encontrar, retorna zeros.
        """
        try:
            time_from = open_time - timedelta(seconds=60)
            time_to   = open_time + timedelta(seconds=60)
            deals = mt5.history_deals_get(time_from, time_to, group=symbol)
        except Exception:
            deals = None

        if not deals:
            return {"points": 0.0, "money": 0.0}

        # Seleciona o deal mais próximo do horário de abertura
        best = None
        best_dt = None
        for d in deals:
            if d.symbol != symbol:
                continue
            # filtro por posição (quando disponível)
            if d.position_id and d.position_id != position_ticket:
                continue
            dt = datetime.fromtimestamp(d.time, tz=timezone.utc)
            if (best is None) or (abs((dt - open_time).total_seconds()) < abs((best_dt - open_time).total_seconds())):
                best = d
                best_dt = dt

        if not best:
            return {"points": 0.0, "money": 0.0}

        fill_price = float(best.price)
        # slippage em pontos: diferença entre preço desejado (open_price) e preço executado (fill_price)
        # Observação: open_price aqui já é o preço de execução salvo na posição para muitas corretoras.
        # Ainda assim preservamos a métrica como  (fill - open) para BUY e (open - fill) para SELL.
        if side == "buy":
            slip_points = (fill_price - open_price) / point
        else:
            slip_points = (open_price - fill_price) / point

        # Aproximação de valor monetário do slippage por 1 lote — multiplicaremos por volume na posição ao consolidar
        # (nesta função retornamos valor por lote; aplicaremos volume fora).
        # NOTA: como não temos preço_ref confiável aqui, usamos fill_price.
        point_val_per_lot = _point_value(contract_size, point, fill_price)
        slip_money_per_lot = slip_points * point * point_val_per_lot

        return {"points": float(slip_points), "money": float(slip_money_per_lot)}

    # ---- posições ativas
    def read_positions(self) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
        self.ensure_connection()
        raw_positions = mt5.positions_get()
        if raw_positions is None:
            raise RuntimeError("Não foi possível ler positions_get().")

        positions: List[Dict[str, Any]] = []
        exposure_by_symbol: Dict[str, float] = {}

        for p in raw_positions:
            symbol = p.symbol
            side = _side_name(p.type)
            sym = self._read_symbol_info(symbol)

            open_time = datetime.fromtimestamp(p.time, tz=timezone.utc)
            open_price = float(p.price_open)
            volume = float(p.volume)
            sl = float(p.sl) if p.sl not in (None, 0.0) else None
            tp = float(p.tp) if p.tp not in (None, 0.0) else None

            current_price = _current_price_for_side(symbol, side)
            floating_pnl = float(p.profit)

            # risco estimado
            price_diff = _price_for_risk(side, open_price, sl)
            if price_diff is None:
                risk_money = None
                risk_pct = None
                missing_sl = True
            else:
                price_ref = current_price or open_price
                risk_money_per_lot = _risk_money_per_lot(price_diff, sym, price_ref)
                risk_money = risk_money_per_lot * volume
                # equity será aplicada no snapshot (precisamos da conta); aqui apenas guardamos valor monetário
                risk_pct = None
                missing_sl = False

            # slippage estimado por lote -> multiplica pelo volume
            slp = self._estimate_slippage(
                position_ticket=p.ticket,
                symbol=symbol,
                side=side,
                open_time=open_time,
                open_price=open_price,
                point=sym["point"],
                contract_size=sym["contract_size"]
            )
            slippage_money = slp["money"] * volume

            pos_obj = {
                "ticket": int(p.ticket),
                "symbol": symbol,
                "type": side,
                "volume": volume,
                "open_time": _to_iso(open_time),
                "open_price": open_price,
                "sl": sl,
                "tp": tp,
                "current_price": current_price,
                "floating_pnl": floating_pnl,
                "symbol_info": sym,
                "risk_money": risk_money,     # pode ser None se sem SL
                "risk_pct": risk_pct,         # será preenchido no snapshot com equity
                "missing_sl": missing_sl,
                "slippage": {
                    "points": float(slp["points"]),
                    "money": float(slippage_money)
                }
            }
            positions.append(pos_obj)

            # exposição por símbolo (somente posições com SL)
            if risk_money is not None:
                exposure_by_symbol[symbol] = exposure_by_symbol.get(symbol, 0.0) + float(risk_money)

        return positions, exposure_by_symbol

    # ---- snapshot completo (saída única para as próximas funções)
    def snapshot(self) -> Dict[str, Any]:
        acc = self.read_account()
        positions, exposure_by_symbol = self.read_positions()

        # completar risk_pct com equity
        equity = float(acc["equity"]) if acc and "equity" in acc else 0.0
        total_risk_money = 0.0
        for pos in positions:
            if pos["risk_money"] is not None and equity > 0:
                pos["risk_pct"] = (pos["risk_money"] / equity) * 100.0
                total_risk_money += float(pos["risk_money"])

        total_risk_pct = (total_risk_money / equity) * 100.0 if equity > 0 else 0.0

        out = {
            "account": {
                "equity": acc["equity"],
                "margin": acc["margin"],
                "margin_free": acc["margin_free"],
                "margin_level": acc["margin_level"],
                "leverage": acc["leverage"],
                "currency": acc["currency"],
                "trade_allowed": acc["trade_allowed"],
                "login": acc["login"],
                "server": acc["server"],
            },
            "positions": positions,
            "exposure": {
                "by_symbol": exposure_by_symbol,
                "total_risk_money": float(total_risk_money),
                "total_risk_pct": float(total_risk_pct)
            },
            "timestamp": _to_iso(datetime.now(timezone.utc))
        }
        return out


# ---------------------------
# Exemplo mínimo de uso (manual)
# ---------------------------
if __name__ == "__main__":
    reader = RiskGuardMT5Reader(path=r"C:\Program Files\MetaTrader 5\terminal64.exe")
    if not reader.connect():
        raise SystemExit("Falha ao iniciar MT5. Verifique terminal aberto / credenciais.")
    try:
        snap = reader.snapshot()
        from pprint import pprint
        pprint(snap)
    finally:
        reader.shutdown()

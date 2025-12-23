from __future__ import annotations

# ==============================================================
# mc.py — Monte Carlo para curvas de equity (robusto e headless)
# ==============================================================

import os
import sys
os.environ["MPLBACKEND"] = "Agg"  # backend sem GUI, funciona em servidor/VPS

from typing import Any, Dict, List, Optional, Tuple, Iterable, Union
from pathlib import Path
import math
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rg_config import get_float

DEFAULT_MC_RISK_PCT = get_float("MC_RISK_PCT", 0.01)
DEFAULT_MC_DD_LIMIT = get_float("MC_DD_LIMIT", 0.30)

# --- matplotlib: carregado sob demanda, com backend Agg ---
def _get_plt():
    """
    Retorna matplotlib.pyplot pronto para uso com backend Agg (headless).
    Se indisponível, retorna None (e funções de gráfico devem lidar com isso).
    """
    try:
        import matplotlib
        try:
            matplotlib.use("Agg", force=True)
        except Exception:
            pass
        import matplotlib.pyplot as plt
        return plt
    except Exception as e:
        print("[WARN] matplotlib indisponível no mc.py:", repr(e))
        return None


# ===========================
# Helpers internos
# ===========================
def _as_numpy_1d(x: Iterable[float]) -> np.ndarray:
    arr = np.asarray(list(x), dtype=float).ravel()
    if arr.size == 0:
        raise ValueError("A lista de R-multiples não pode ser vazia.")
    return arr

def _validate_method(method: str) -> str:
    method = (method or "bootstrap").lower()
    if method not in {"bootstrap", "block", "permute", "tilted"}:
        raise ValueError("method deve ser 'bootstrap', 'block', 'permute' ou 'tilted'.")
    return method

def _prepare_risk_vector(risk_pct: Optional[Iterable[float]], n_trades: int) -> np.ndarray:
    """
    risk_pct pode ser:
      - None  -> assume 1% por trade (0.01)
      - float -> aplica em todos os trades
      - Iterable[float] com len == n_trades
    """
    if risk_pct is None:
        r = np.full(n_trades, DEFAULT_MC_RISK_PCT, dtype=float)
    elif isinstance(risk_pct, (int, float)):
        if not (0 < float(risk_pct) < 1):
            raise ValueError("risk_pct (float) deve estar entre 0 e 1 (ex.: 0.01 para 1%).")
        r = np.full(n_trades, float(risk_pct), dtype=float)
    else:
        r = np.asarray(list(risk_pct), dtype=float).ravel()
        if r.size != n_trades:
            raise ValueError("risk_pct como sequência deve ter comprimento n_trades.")
        if np.any((r <= 0) | (r >= 1)):
            raise ValueError("Todos os valores de risk_pct devem estar entre 0 e 1.")
    return r

def _winsorize(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    qlo, qhi = np.quantile(x, [lo, hi])
    return np.clip(x, qlo, qhi)

def _sample_sequence_R(
    R: np.ndarray,
    n_trades: int,
    rng: np.random.Generator,
    method: str,
    block_size: int
) -> np.ndarray:
    """Retorna uma sequência de R-multiples de comprimento n_trades, conforme método."""
    N = R.size
    if N == 0:
        raise ValueError("R não pode ser vazio.")
    if method == "bootstrap":
        idx = rng.integers(0, N, size=n_trades)
        return R[idx]
    elif method == "permute":
        # permutação sem reposição; se precisar de mais, concatena novas permutações
        seq = []
        while len(seq) < n_trades:
            perm = rng.permutation(R)
            seq.append(perm)
        return np.concatenate(seq)[:n_trades]
    elif method == "block":
        if block_size <= 0:
            raise ValueError("block_size deve ser >= 1.")
        blocks_needed = math.ceil(n_trades / block_size)
        out = []
        for _ in range(blocks_needed):
            start = rng.integers(0, N)
            # extrai bloco circular de tamanho block_size
            if start + block_size <= N:
                out.append(R[start:start + block_size])
            else:
                tail = R[start:]
                head = R[: (start + block_size) % N]
                out.append(np.concatenate([tail, head]))
        return np.concatenate(out)[:n_trades]
    else:
        raise ValueError("Use _sample_tilted para 'tilted'.")

def _sample_tilted(R: np.ndarray, size: int, rng: np.random.Generator, alpha: float) -> np.ndarray:
    """
    Amostragem 'tilted' (pessimista): combina uniforme com peso extra para cauda inferior.
    alpha=0 -> puro uniforme (bootstrap); alpha=1 -> só peso pela cauda.
    """
    ranks = np.argsort(R)            # menores valores primeiro
    w = np.linspace(1.0, 0.0, R.size)  # mais peso para perdas
    w = (1 - alpha) * (np.ones_like(w) / w.size) + alpha * (w / w.sum())
    idx = rng.choice(ranks, size=size, replace=True, p=w)
    return R[idx]

def _equity_path_from_R(
    R_seq: np.ndarray,
    start_equity: float,
    risk_vec: np.ndarray,
    fee_vec: np.ndarray
) -> np.ndarray:
    """
    equity_{t+1} = equity_t + R_t * (equity_t * risk_t) - fee_t
    Não permite equity negativa (trava em zero).
    Retorna vetor de tamanho n_trades + 1 com equity inicial.
    """
    n = R_seq.size
    eq = np.empty(n + 1, dtype=float)
    eq[0] = float(start_equity)
    for t in range(n):
        if eq[t] <= 0:
            eq[t + 1] = 0.0
            continue
        pnl = R_seq[t] * (eq[t] * risk_vec[t])
        next_eq = eq[t] + pnl - float(fee_vec[t])
        eq[t + 1] = max(0.0, next_eq)
    return eq

def _max_drawdown(path: np.ndarray) -> float:
    """
    Máx. drawdown em termos percentuais relativos ao pico: max(1 - equity/peak).
    Retorna valor em [0,1].
    """
    peaks = np.maximum.accumulate(path)
    dd = 1.0 - np.divide(path, peaks, out=np.ones_like(path), where=peaks > 0)
    return float(np.max(dd))

def _percentiles_over_time(paths: np.ndarray, qs=(5, 25, 50, 75, 95)) -> Dict[str, np.ndarray]:
    """Percentis ao longo do tempo (colunas)."""
    out = {}
    for q in qs:
        out[f"p{q}"] = np.percentile(paths, q, axis=0)
    return out


# ===========================
# Robustez extra (realismo)
# ===========================
def compute_R_from_trades(
    trades: Iterable[dict],
    equity_start: Optional[float] = None,
    fallback_risk_pct: float = DEFAULT_MC_RISK_PCT,
) -> Tuple[np.ndarray, float]:
    """
    Constrói R a partir de trades (cada item com 'pnl' e opcional 'risk_amount').
    Se não houver risk_amount por trade, define 1R = mediana(|perdas|) e R = pnl/med_loss.
    Retorna: (R_hist, risk_pct_estimado)
    """
    pnls = np.array([float(t.get("pnl", 0.0)) for t in trades], dtype=float)
    if pnls.size == 0:
        return np.array([-1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0], dtype=float), fallback_risk_pct

    risk_amt = np.array([t.get("risk_amount", None) for t in trades], dtype=object)
    if np.any(risk_amt != None):
        ra = np.array([float(x) if x is not None else np.nan for x in risk_amt], dtype=float)
        ok = ~np.isnan(ra) & (ra > 0)
        if ok.sum() >= 5:
            R = pnls[ok] / ra[ok]
            est_risk_pct = (np.nanmedian(ra[ok]) / float(equity_start)) if equity_start else fallback_risk_pct
            return R, float(np.clip(est_risk_pct, 0.001, 0.5))

    losses = np.abs(pnls[pnls < 0])
    if losses.size >= 3:
        med_loss = float(np.median(losses))
        R = pnls / (med_loss if med_loss > 0 else 1.0)
        est_risk_pct = (med_loss / float(equity_start)) if equity_start else fallback_risk_pct
        return R, float(np.clip(est_risk_pct, 0.001, 0.5))

    # fallback sintético
    return np.array([-1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0], dtype=float), fallback_risk_pct

def suggest_block_size(R: np.ndarray, max_b: int = 10) -> int:
    """
    Sugere block_size via maior autocorrelação (em valor absoluto) nos 1..max_b lags.
    Se nada forte, retorna 1 (bootstrap simples).
    """
    r = np.asarray(R, dtype=float).ravel()
    if r.size < 3:
        return 1
    r = r - r.mean()
    best = 1
    best_ac = 0.0
    for lag in range(1, min(max_b, r.size - 1) + 1):
        num = float(np.dot(r[:-lag], r[lag:]))
        den = float(np.dot(r, r) + 1e-12)
        ac = num / den
        if abs(ac) > abs(best_ac):
            best_ac, best = ac, lag
    return max(1, best)


# ===========================
# API pública
# ===========================
def simulate_paths(
    returns_R: Iterable[float],
    start_equity: float,
    n_trades: int,
    iterations: int = 10000,
    method: str = "bootstrap",                 # 'bootstrap' | 'block' | 'permute' | 'tilted'
    block_size: Union[int, str] = 5,           # ou "auto"
    risk_pct: Optional[Iterable[float]] = None,# float ou vetor len=n_trades
    fee_per_trade: Union[float, Iterable[float]] = 0.0,  # float ou vetor
    seed: Optional[int] = None,
    winsor: Optional[Tuple[float, float]] = (0.01, 0.99),# cap 1%-99% nos R
    tilt_alpha: float = 0.2,                   # usado no método 'tilted'
) -> np.ndarray:
    """
    Gera 'iterations' trajetórias de equity com horizonte de 'n_trades' a partir de R-multiples.
    Adicionado:
      - Limite automático para não travar VPS.
      - Ajuste do block_size.
      - Winsorização com cap em ±10R.
    """
    if start_equity <= 0:
        raise ValueError("start_equity deve ser > 0.")
    if n_trades <= 0:
        raise ValueError("n_trades deve ser > 0.")
    if iterations <= 0:
        raise ValueError("iterations deve ser > 0.")

    method = _validate_method(method)
    R_hist = _as_numpy_1d(returns_R)

    # Winsorize (cap de cauda)
    if winsor is not None:
        lo, hi = winsor
        R_hist = _winsorize(R_hist, lo, hi)
    # Cap adicional em ±10R para estabilidade extrema
    R_hist = np.clip(R_hist, -10.0, 10.0)

    # Ajuste de block_size (auto ou excesso)
    if isinstance(block_size, str) and block_size.lower() == "auto":
        block_size = suggest_block_size(R_hist)
    if isinstance(block_size, (int, float)):
        block_size = int(min(block_size, max(1, R_hist.size)))

    rng = np.random.default_rng(seed)
    risk_vec = _prepare_risk_vector(risk_pct, n_trades)

    # normaliza fee em vetor
    if isinstance(fee_per_trade, (list, tuple, np.ndarray)):
        fee_vec = np.asarray(fee_per_trade, dtype=float).ravel()
        if fee_vec.size != n_trades:
            raise ValueError("fee_per_trade (vetor) deve ter len == n_trades.")
    else:
        fee_vec = np.full(n_trades, float(fee_per_trade), dtype=float)

    # Guardrail para evitar travamento em VPS fraco
    if iterations * n_trades > 2e6:
        print(f"[WARN] Reduzindo iterações por segurança (de {iterations} para {int(2e6 / n_trades)})")
        iterations = int(2e6 / n_trades)

    paths = np.empty((iterations, n_trades + 1), dtype=float)
    for i in range(iterations):
        if method in ("bootstrap", "block", "permute"):
            R_seq = _sample_sequence_R(
                R_hist, n_trades, rng,
                method if method != "permute" else "permute",
                int(block_size)
            )
        elif method == "tilted":
            R_seq = _sample_tilted(R_hist, n_trades, rng, tilt_alpha)
        else:
            raise ValueError("method inválido.")
        paths[i] = _equity_path_from_R(R_seq, start_equity, risk_vec, fee_vec)

    return paths



def summarize_paths(
    paths: np.ndarray,
    start_equity: float,
    dd_limit_pct: Optional[float] = None,
    alphas: Tuple[float, float] = (0.05, 0.01),
) -> Dict[str, Any]:
    """
    Resume estatísticas dos caminhos:
      - percentis ao longo do tempo (p5/p25/p50/p75/p95)
      - distribuição do equity final, PnL e retorno
      - máx. drawdown (média/mediana/p95)
      - prob. de ruína (pico e piso)
      - VaR/ES no final (absoluto e em % do start_equity)
      - prob. de dobrar e tempos medianos para 1.5x/2x
    """
    if paths.ndim != 2:
        raise ValueError("paths deve ser 2D: (iterations, n_steps).")
    iters, n_steps = paths.shape
    if n_steps < 2:
        raise ValueError("paths precisa ter ao menos 2 colunas (t=0 e t=1...).")

    # Percentis ao longo do tempo (para fan chart)
    fan = _percentiles_over_time(paths, qs=(5, 25, 50, 75, 95))

    final_eq = paths[:, -1]
    pnl = final_eq - start_equity
    ret = np.divide(pnl, start_equity, out=np.zeros_like(pnl), where=start_equity != 0)

    # Máx. drawdown por caminho
    max_dd = np.apply_along_axis(_max_drawdown, 1, paths)

    # Probabilidade de ruína
    ruin_peak = None
    ruin_floor = None
    if dd_limit_pct is not None and dd_limit_pct > 0:
        ruin_peak = float(np.mean(max_dd >= dd_limit_pct))
        floor_level = start_equity * (1.0 - dd_limit_pct)
        ruin_floor = float(np.mean(np.min(paths, axis=1) <= floor_level))

    # VaR / ES (Expected Shortfall) no final
    a1, a2 = alphas
    def _var_es(x: np.ndarray, alpha: float) -> Tuple[float, float]:
        q = np.quantile(x, alpha)
        es = float(x[x <= q].mean()) if np.any(x <= q) else float(q)
        return float(q), es

    var5, es5 = _var_es(pnl, a1)
    var1, es1 = _var_es(pnl, a2)

    # Prob. de dobrar e tempos medianos para metas
    prob_double = float(np.mean(final_eq >= 2.0 * start_equity))

    def _median_hitting_time(target_mult: float) -> Optional[float]:
        target = start_equity * target_mult
        hits = []
        for row in paths:
            idx = np.argmax(row >= target)
            if row[idx] >= target:
                hits.append(idx)  # em número de trades
        if len(hits) == 0:
            return None
        return float(np.median(hits))

    t_med_1_5x = _median_hitting_time(1.5)
    t_med_2x  = _median_hitting_time(2.0)

    summary = {
        "n_iterations": iters,
        "n_steps": n_steps,
        "start_equity": float(start_equity),

        # Fan chart percentiles ao longo do tempo
        "fan": fan,  # dict: 'p5','p25','p50','p75','p95' -> np.ndarray (len n_steps)

        # Equity final / PnL / Retorno
        "final_equity": {
            "mean": float(np.mean(final_eq)),
            "median": float(np.median(final_eq)),
            "p5": float(np.percentile(final_eq, 5)),
            "p95": float(np.percentile(final_eq, 95)),
        },
        "final_pnl": {
            "mean": float(np.mean(pnl)),
            "median": float(np.median(pnl)),
            "p5": float(np.percentile(pnl, 5)),
            "p95": float(np.percentile(pnl, 95)),
            "var@5%": float(var5),
            "es@5%": float(es5),
            "var@1%": float(var1),
            "es@1%": float(es1),
        },
        "final_pnl_pct": {
            "var@5%": float(var5 / start_equity) if start_equity else None,
            "es@5%":  float(es5  / start_equity) if start_equity else None,
            "var@1%": float(var1 / start_equity) if start_equity else None,
            "es@1%":  float(es1  / start_equity) if start_equity else None,
        },
        "final_return_pct": {
            "mean": float(np.mean(ret)),
            "median": float(np.median(ret)),
            "p5": float(np.percentile(ret, 5)),
            "p95": float(np.percentile(ret, 95)),
        },

        # Drawdown
        "max_drawdown": {
            "mean": float(np.mean(max_dd)),
            "median": float(np.median(max_dd)),
            "p95": float(np.percentile(max_dd, 95)),
        },

        # Ruína
        "prob_ruin_peak": ruin_peak,   # MDD >= dd_limit_pct
        "prob_ruin_floor": ruin_floor, # equity <= start*(1-dd_limit_pct)

        # Alvos
        "prob_double": prob_double,
        "median_time_to_1_5x": t_med_1_5x,
        "median_time_to_2x": t_med_2x,
    }
    return summary


# ===========================
# Gráficos
# ===========================
def mc_fig_fanchart(paths: np.ndarray, title: str = "Monte Carlo – Fan Chart"):
    plt = _get_plt()
    if plt is None:
        raise RuntimeError("matplotlib não está disponível no ambiente.")
    iters, n_steps = paths.shape
    x = np.arange(n_steps)
    fan = _percentiles_over_time(paths)

    fig, ax = plt.subplots()
    ax.plot(x, fan["p50"], label="Mediana")
    ax.fill_between(x, fan["p25"], fan["p75"], alpha=0.3, label="IQR (p25–p75)")
    ax.fill_between(x, fan["p5"], fan["p95"], alpha=0.15, label="Faixa (p5–p95)")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Equity")
    ax.set_title(title)
    ax.legend()
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.6)
    return fig

def mc_fig_dd_hist(paths: np.ndarray, bins: int = 30, title: str = "Distribuição do Máx. Drawdown"):
    plt = _get_plt()
    if plt is None:
        raise RuntimeError("matplotlib não está disponível no ambiente.")
    max_dd = np.apply_along_axis(_max_drawdown, 1, paths)

    fig, ax = plt.subplots()
    ax.hist(max_dd * 100.0, bins=bins)
    ax.set_xlabel("Máx. Drawdown (%)")
    ax.set_ylabel("Frequência")
    ax.set_title(title)
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.6)
    return fig


# ===========================
# Tabela-resumo
# ===========================
def mc_table(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    fe = summary["final_equity"]
    fp = summary["final_pnl"]
    fr = summary["final_return_pct"]
    dd = summary["max_drawdown"]

    rows = [
        {"Métrica": "Iterations", "Valor": summary["n_iterations"]},
        {"Métrica": "Passos (t)", "Valor": summary["n_steps"]},
        {"Métrica": "Equity inicial", "Valor": f"{summary['start_equity']:.2f}"},

        {"Métrica": "Equity final (média)", "Valor": f"{fe['mean']:.2f}"},
        {"Métrica": "Equity final (mediana)", "Valor": f"{fe['median']:.2f}"},
        {"Métrica": "Equity final (p5–p95)", "Valor": f"{fe['p5']:.2f} → {fe['p95']:.2f}"},

        {"Métrica": "PnL final (média)", "Valor": f"{fp['mean']:.2f}"},
        {"Métrica": "PnL final (mediana)", "Valor": f"{fp['median']:.2f}"},
        {"Métrica": "PnL VaR@5% / ES@5%", "Valor": f"{fp['var@5%']:.2f} / {fp['es@5%']:.2f}"},
        {"Métrica": "PnL VaR@1% / ES@1%", "Valor": f"{fp['var@1%']:.2f} / {fp['es@1%']:.2f}"},

        {"Métrica": "Retorno final (média)", "Valor": f"{fr['mean']*100:.2f}%"},
        {"Métrica": "Retorno final (mediana)", "Valor": f"{fr['median']*100:.2f}%"},
        {"Métrica": "Retorno final (p5–p95)", "Valor": f"{fr['p5']*100:.2f}% → {fr['p95']*100:.2f}%"},

        {"Métrica": "Máx. DD (média)", "Valor": f"{dd['mean']*100:.2f}%"},
        {"Métrica": "Máx. DD (mediana)", "Valor": f"{dd['median']*100:.2f}%"},
        {"Métrica": "Máx. DD (p95)", "Valor": f"{dd['p95']*100:.2f}%"},
    ]

    if summary.get("prob_ruin_peak") is not None:
        rows.append({"Métrica": "Prob. ruína (por pico)", "Valor": f"{(summary['prob_ruin_peak'] or 0)*100:.2f}%"})
    if summary.get("prob_ruin_floor") is not None:
        rows.append({"Métrica": "Prob. ruína (por piso)", "Valor": f"{(summary['prob_ruin_floor'] or 0)*100:.2f}%"})
    if summary.get("prob_double") is not None:
        rows.append({"Métrica": "Chance de dobrar", "Valor": f"{summary['prob_double']*100:.2f}%"})

    return rows


# ===========================
# Helpers p/ salvar PNG (opcional)
# ===========================
def mc_save_fanchart(paths: np.ndarray, out_path: str, title: str = "Monte Carlo – Fan Chart") -> bool:
    try:
        plt = _get_plt()
        if plt is None:
            return False
        fig = mc_fig_fanchart(paths, title=title)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception as e:
        print("[WARN] falha ao salvar fan chart:", repr(e))
        return False

def mc_save_dd_hist(paths: np.ndarray, out_path: str, bins: int = 30, title: str = "Distribuição do Máx. Drawdown") -> bool:
    try:
        plt = _get_plt()
        if plt is None:
            return False
        fig = mc_fig_dd_hist(paths, bins=bins, title=title)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return True
    except Exception as e:
        print("[WARN] falha ao salvar dd hist:", repr(e))
        return False


# ===========================
# Exemplo CLI rápido (isolado)
# ===========================
if __name__ == "__main__":
    import csv, json, argparse
    from datetime import datetime

    HERE = Path(__file__).resolve().parent
    REPORTS_DIR = HERE / "reports"  # onde o reports.py salva trades_*.csv e summary_*.json
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # --- argumentos opcionais (você pode rodar sem nenhum) ---
    parser = argparse.ArgumentParser(description="Monte Carlo (auto) - usa o último trades_*.csv em reports/")
    parser.add_argument("--iterations", type=int, default=5000)
    parser.add_argument("--method", type=str, default="block", choices=["bootstrap","block","permute","tilted"])
    parser.add_argument("--block-size", type=str, default="auto")  # "auto" ou inteiro
    parser.add_argument("--dd-limit", type=float, default=DEFAULT_MC_DD_LIMIT)    # 30% por padrão
    parser.add_argument("--tilt", type=float, default=0.0)         # 0..1 (só se method="tilted")
    parser.add_argument("--winsor", type=str, default="0.01,0.99") # cap dos R
    parser.add_argument("--risk-pct", type=float, default=None)    # override manual (0..1)
    parser.add_argument("--fee", type=float, default=0.0)          # taxa fixa por trade
    args = parser.parse_args()

    # --- localizar o último trades_*.csv em reports/ ---
    def _latest_file(pattern: str) -> Optional[Path]:
        files = list(REPORTS_DIR.glob(pattern))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    trades_csv = _latest_file("trades_*.csv")
    if not trades_csv:
        print("[MC] Nenhum trades_*.csv encontrado em", REPORTS_DIR)
        print("[MC] Usando R sintético apenas para teste.")
        # Fallback sintético
        R_hist = np.array([-1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0])
        start_eq = 1000.0
        N_TRADES = 200
        risk_pct = DEFAULT_MC_RISK_PCT
        fee_vec = float(args.fee)
    else:
        print(f"[MC] Usando CSV: {trades_csv.name}")

        # --- tentar achar o summary_*.json "par" (mesmo sufixo) ---
        # ex.: trades_88665044_20251010-125718.csv -> summary_88665044_20251010-125718.json
        suffix = trades_csv.name.replace("trades_", "")
        summary_json = REPORTS_DIR / ("summary_" + suffix.replace(".csv", ".json"))

        # --- equity inicial ---
        start_eq = 1000.0  # padrão
        if summary_json.exists():
            try:
                j = json.loads(summary_json.read_text(encoding="utf-8"))
                equity_now = float(j.get("equity_now") or 0.0)
                net_pnl = float(j.get("metrics", {}).get("net_pnl") or 0.0)
                start_eq = equity_now - net_pnl if (equity_now or net_pnl) else start_eq
                print(f"[MC] equity_now={equity_now:.2f}, net_pnl={net_pnl:.2f} -> start_equity≈{start_eq:.2f}")
            except Exception as e:
                print("[WARN] Falha ao ler summary JSON:", e)

        # --- ler PnL dos trades ---
        trades = []
        with trades_csv.open("r", encoding="utf-8", newline="") as f:
            rd = csv.DictReader(f)
            for row in rd:
                try:
                    trades.append({"pnl": float(row.get("pnl", 0.0))})
                except Exception:
                    pass
        if not trades:
            print("[WARN] CSV sem PnLs válidos; usando R sintético.")
            R_hist = np.array([-1.0, -0.5, -0.25, 0.25, 0.5, 1.0, 2.0])
            risk_pct = DEFAULT_MC_RISK_PCT if args.risk_pct is None else float(args.risk_pct)
            N_TRADES = 200
        else:
            # construir R reais e estimar risk_pct
            R_hist, est_risk_pct = compute_R_from_trades(trades, equity_start=start_eq, fallback_risk_pct=DEFAULT_MC_RISK_PCT)
            risk_pct = est_risk_pct if args.risk_pct is None else float(args.risk_pct)
            N_TRADES = max(50, len(trades) * 3)  # horizonte padrão: 3x nº trades (mín 50)
            print(f"[MC] trades lidos: {len(trades)} | risk_pct usado: {risk_pct:.4f}")

        fee_vec = float(args.fee)

    # --- winsor (cap) dos R ---
    try:
        wlo, whi = (float(x.strip()) for x in args.winsor.split(","))
        winsor = (wlo, whi)
    except Exception:
        winsor = (0.01, 0.99)

    # --- block size ---
    blk = args.block_size
    if blk != "auto":
        try:
            blk = int(blk)
        except Exception:
            blk = 5

    # --- rodar simulação ---
    paths = simulate_paths(
        returns_R=R_hist,
        start_equity=start_eq,
        n_trades=N_TRADES,
        iterations=int(args.iterations),
        method=args.method,
        block_size=blk,
        risk_pct=risk_pct,
        fee_per_trade=fee_vec,
        seed=42,
        winsor=winsor,
        tilt_alpha=float(args.tilt),
    )

    summary = summarize_paths(paths, start_equity=start_eq, dd_limit_pct=float(args.dd_limit))
    print("Resumo:", {k: v for k, v in summary.items() if k not in ("fan",)})

    fe = summary["final_equity"]; dd = summary["max_drawdown"]
    print(f"[CHECK] Mediana equity final: {fe['median']:.2f}")
    print(f"[CHECK] DD p95: {dd['p95']*100:.1f}%")
    dd_floor_pct = float(args.dd_limit) * 100.0
    print(f"[CHECK] Prob. ruína (piso {dd_floor_pct:.0f}%): { (summary.get('prob_ruin_floor') or 0)*100:.1f}%")
    print(f"[CHECK] risk_pct usado: {risk_pct:.3f} | R calculado a partir de: 1R = mediana(|losses|)")

    # --- salvar figuras em reports/ ---
    base = trades_csv.stem if trades_csv else f"manual_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    fan_ok = mc_save_fanchart(paths, str(REPORTS_DIR / f"mc_fanchart_{base}.png"),
                              title=f"Monte Carlo – {N_TRADES} trades ({args.iterations} it)")
    dd_ok  = mc_save_dd_hist(paths, str(REPORTS_DIR / f"mc_dd_hist_{base}.png"),
                             bins=30, title="Distribuição do Máx. Drawdown")
    if fan_ok:
        print("Imagem gerada:", REPORTS_DIR / f"mc_fanchart_{base}.png")
    if dd_ok:
        print("Imagem gerada:", REPORTS_DIR / f"mc_dd_hist_{base}.png")

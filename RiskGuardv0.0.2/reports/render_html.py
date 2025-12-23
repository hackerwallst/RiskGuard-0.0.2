# reports/render_html.py
from __future__ import annotations
from pathlib import Path
import json, html, calendar
from datetime import datetime

# =========================
# CSS
# =========================
CSS = """
body{font-family:Segoe UI,Arial,sans-serif;background:#0b0f14;color:#e8eef4;margin:0;padding:0;}
.wrap{max-width:1080px;margin:0 auto;padding:24px;}
.card{background:#121821;border:1px solid #1f2933;border-radius:14px;box-shadow:0 8px 24px rgba(0,0,0,.25);padding:20px;margin-bottom:18px;}
.h1{font-size:22px;font-weight:700;margin:0 0 10px}
.h2{font-size:18px;font-weight:700;margin:0 0 10px;color:#9ed0ff}
.kv{display:grid;grid-template-columns:220px 1fr;gap:8px 16px;}
.kv div{padding:4px 0;border-bottom:1px dashed #213040}
.kv b{color:#c5e1ff}
.tbl{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
.tbl th,.tbl td{border-bottom:1px solid #223041;padding:8px;text-align:left}
.tbl th{color:#9ed0ff;font-weight:700;background:#0f141b}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#1b2633;color:#a3cfff;margin-left:8px;font-size:12px}
.small{opacity:.8;font-size:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
pre{background:#0f141b;border:1px solid #1d2a38;border-radius:8px;padding:10px;overflow:auto}
.card{page-break-inside: avoid; break-inside: avoid;}
svg{page-break-inside: avoid; break-inside: avoid;}
"""
SVG_CSS = """
.svg{width:100%;height:auto;display:block}
.grid{stroke:#213040;stroke-width:1;shape-rendering:crispEdges}
.bar{fill:#87bfff}
.line{stroke:#87bfff;stroke-width:2}
.lbl{font-size:11px;fill:#9db9d6}
.tick{font-size:11px;fill:#6f8aa3}
.eq-line{stroke:#87bfff;stroke-width:2;fill:none}
.hwm-line{stroke:#5f9ad6;stroke-width:1.2;fill:none;stroke-dasharray:4 3}
.eq-dot{fill:#87bfff}
.bad{fill:#ff8a8a;opacity:.15}
.annot{font-size:12px;fill:#c5e1ff}
.val{font-size:11px;fill:#c5e1ff}
"""
CSS = CSS + SVG_CSS

# =========================
# Helpers numéricos
# =========================
def _fmoney(x):
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return str(x)

def _fpct(x):
    try:
        return f"{float(x):.2f}%"
    except Exception:
        return "N/D"

# =========================
# SVG helpers
# =========================
def _svg_bar_chart(data, width=960, height=180, pad=24, show_values=False, pct_map=None):
    if not data:
        return "<div class='small'>Sem dados para plotar.</div>"
    labels, vals = zip(*data)
    n = len(vals)
    minv, maxv = min(0, min(vals)), max(0, max(vals))
    span = (maxv - minv) or 1.0
    inner_w = width - 2*pad
    inner_h = height - 2*pad
    bar_w = inner_w / max(n,1)
    zero_y = pad + inner_h * (maxv / span)
    lines = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"]
    lines.append(f"<line x1='{pad}' y1='{zero_y:.1f}' x2='{width-pad}' y2='{zero_y:.1f}' class='grid'/>")
    for i, v in enumerate(vals):
        x = pad + i * bar_w + 2
        h = inner_h * (abs(v) / span)
        y = zero_y - h if v >= 0 else zero_y
        lines.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bar_w-4:.1f}' height='{h:.1f}' class='bar'/>")
        if show_values:
            lab = str(labels[i])
            val_txt = _fmoney(v)
            if pct_map and lab in pct_map and pct_map[lab] is not None:
                val_txt = f"{val_txt} ({pct_map[lab]:.2f}%)"
            ty = (y - 4) if v >= 0 else (y + h + 12)
            tx = x + (bar_w-4)/2
            lines.append(f"<text x='{tx:.1f}' y='{ty:.1f}' class='val' text-anchor='middle'>{val_txt}</text>")
    step = max(1, n // 12)
    for i, lab in enumerate(labels):
        if i % step == 0 or i == n-1:
            tx = pad + i * bar_w + bar_w/2
            lines.append(f"<text x='{tx:.1f}' y='{height-6}' class='lbl' text-anchor='middle'>{lab}</text>")
    lines.append("</svg>")
    return "".join(lines)

def _svg_line_chart(points, width=960, height=220, pad=28):
    if not points:
        return "<div class='small'>Sem dados para plotar.</div>"
    labels, vals = zip(*points)
    n = len(vals)
    minv, maxv = min(vals), max(vals)
    span = (maxv - minv) or 1.0
    inner_w = width - 2*pad
    inner_h = height - 2*pad
    def sx(i): return pad + inner_w * (i/(max(n-1,1)))
    def sy(v): return pad + inner_h * (1 - (v - minv)/span)
    d = []
    for i, v in enumerate(vals):
        d.append(f"{'M' if i==0 else 'L'}{sx(i):.1f},{sy(v):.1f}")
    lines = [f"<svg viewBox='0 0 {width} {height}' class='svg'>"]
    for val in (maxv, (minv+maxv)/2, minv):
        y = sy(val)
        lines.append(f"<line x1='{pad}' y1='{y:.1f}' x2='{width-pad}' y2='{y:.1f}' class='grid'/>")
        lines.append(f"<text x='{pad-6}' y='{y+4:.1f}' class='tick' text-anchor='end'>{_fmoney(val)}</text>")
    lines.append(f"<path d='{' '.join(d)}' class='line' fill='none'/>")
    step = max(1, n // 10)
    for i, lab in enumerate(labels):
        if i % step == 0 or i == n-1:
            lines.append(f"<text x='{sx(i):.1f}' y='{height-6}' class='lbl' text-anchor='middle'>{lab}</text>")
    lines.append("</svg>")
    return "".join(lines)

def _svg_equity_chart(points, width=960, height=260, pad=32, annotate=None):
    if not points:
        return "<div class='small'>Sem dados de equity.</div>"
    labels, vals = zip(*points)
    n = len(vals)
    minv, maxv = min(vals), max(vals)
    span = (maxv - minv) or 1.0
    inner_w = width - 2*pad
    inner_h = height - 2*pad
    def sx(i): return pad + inner_w * (i/(max(n-1,1)))
    def sy(v): return pad + inner_h * (1 - (v - minv)/span)
    path = []
    for i, v in enumerate(vals):
        path.append(f"{'M' if i==0 else 'L'}{sx(i):.1f},{sy(v):.1f}")
    hwm = []
    peak = None
    for i, v in enumerate(vals):
        peak = v if (peak is None or v > peak) else peak
        hwm.append(f"{'M' if i==0 else 'L'}{sx(i):.1f},{sy(peak):.1f}")
    dd_poly = ""
    if annotate and annotate.get("from") and annotate.get("to"):
        try:
            i0 = next(i for i,(lab,_) in enumerate(points) if lab == annotate["from"])
            i1 = next(i for i,(lab,_) in enumerate(points) if lab == annotate["to"])
            if i1 < i0: i0, i1 = i1, i0
            up, down = [], []
            peak = None
            for i in range(n):
                peak = vals[i] if (peak is None or vals[i] > peak) else peak
                if i0 <= i <= i1:
                    up.append(f"{sx(i):.1f},{sy(peak):.1f}")
                    down.append(f"{sx(i):.1f},{sy(vals[i]):.1f}")
            pts = " ".join(up + down[::-1])
            dd_poly = f"<polygon points='{pts}' class='bad'/>"
        except StopIteration:
            pass
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"]
    for val in (maxv, (minv+maxv)/2, minv):
        y = sy(val)
        out.append(f"<line x1='{pad}' y1='{y:.1f}' x2='{width-pad}' y2='{y:.1f}' class='grid'/>")
    if dd_poly: out.append(dd_poly)
    out.append(f"<path d='{' '.join(path)}' class='eq-line'/>")
    out.append(f"<path d='{' '.join(hwm)}' class='hwm-line'/>")
    step = max(1, n // 10)
    for i, lab in enumerate(labels):
        if i % step == 0 or i == n-1:
            out.append(f"<text x='{sx(i):.1f}' y='{height-6}' class='lbl' text-anchor='middle'>{lab}</text>")
    if annotate:
        txt = f"Max DD: {_fmoney(annotate.get('dd_abs',0))} ({_fpct(annotate.get('dd_pct',0))})"
        out.append(f"<text x='{width-pad}' y='{pad+14}' class='annot' text-anchor='end'>{txt}</text>")
    out.append("</svg>")
    return "".join(out)

def _svg_equity_with_flows(points, deposits, withdrawals, width=960, height=260, pad=32):
    if not points:
        return "<div class='small'>Sem dados de equity.</div>"
    def _lab(s):
        s = str(s)
        return s.split("T")[0] if "T" in s else s.split(" ")[0]
    eq_by_day = {}
    for (t, v) in points:
        eq_by_day[_lab(t)] = float(v)
    dep_by_day, wdr_by_day = {}, {}
    for (d, v) in (deposits or []):
        dep_by_day[_lab(d)] = dep_by_day.get(_lab(d), 0.0) + float(v)
    for (d, v) in (withdrawals or []):
        wdr_by_day[_lab(d)] = wdr_by_day.get(_lab(d), 0.0) + float(v)
    all_days = sorted(set(eq_by_day) | set(dep_by_day) | set(wdr_by_day))
    if not all_days:
        return "<div class='small'>Sem dados de equity.</div>"
    labels, vals, last = [], [], None
    for day in all_days:
        if day in eq_by_day:
            last = eq_by_day[day]
        elif last is None:
            try:
                first_val = next(iter(eq_by_day.values()))
            except StopIteration:
                first_val = 0.0
            last = first_val
        labels.append(day); vals.append(float(last))
    n = len(labels)
    minv, maxv = min(vals), max(vals)
    span = (maxv - minv) or 1.0
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    def sx(i): return pad + inner_w * (i / max(n - 1, 1))
    def sy(v): return pad + inner_h * (1 - (v - minv) / span)
    idx = {lab: i for i, lab in enumerate(labels)}
    max_flow = max([v for v in dep_by_day.values()] + [v for v in wdr_by_day.values()] + [1.0])
    bars_h = inner_h * 0.30
    def fh(v): return bars_h * (float(v) / max_flow)
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"]
    for val in (maxv, (minv + maxv) / 2, minv):
        y = sy(val)
        out.append(f"<line x1='{pad}' y1='{y:.1f}' x2='{width - pad}' y2='{y:.1f}' style='stroke:#213040;stroke-width:1'/>")
    base_y = pad + inner_h
    col_w = inner_w / max(n, 1)
    bar_w = col_w * 0.6
    for day, v in dep_by_day.items():
        i = idx.get(day)
        if i is None: 
            continue
        x = sx(i) - bar_w / 2
        h = fh(v)
        out.append(f"<rect x='{x:.1f}' y='{base_y - h:.1f}' width='{bar_w:.1f}' height='{h:.1f}' style='fill:#7ddc82;opacity:.5'/>")
    for day, v in wdr_by_day.items():
        i = idx.get(day)
        if i is None: 
            continue
        x = sx(i) - bar_w / 2
        h = fh(v)
        out.append(f"<rect x='{x:.1f}' y='{base_y - h:.1f}' width='{bar_w:.1f}' height='{h:.1f}' style='fill:#ff8a8a;opacity:.5'/>")
    path = []
    for i, v in enumerate(vals):
        path.append(f"{'M' if i == 0 else 'L'}{sx(i):.1f},{sy(v):.1f}")
    out.append(f"<path d='{' '.join(path)}' style='stroke:#87bfff;stroke-width:2;fill:none'/>")
    step = max(1, n // 10)
    for i, lab in enumerate(labels):
        if i % step == 0 or i == n - 1:
            out.append(f"<text x='{sx(i):.1f}' y='{height - 6}' style='font-size:11px;fill:#9db9d6' text-anchor='middle'>{lab}</text>")
    out.append("</svg>")
    return "".join(out)

# =========================
# Monte Carlo (HTML)
# =========================
def _file_url(p: str | None) -> str | None:
    if not p:
        return None
    try:
        ap = Path(p).resolve()
        return "file:///" + str(ap).replace("\\", "/")
    except Exception:
        return None

def _fmt_month(key:str) -> str:
    try:
        y, m = key.split("-")
        mname = calendar.month_abbr[int(m)].capitalize()
        return f"{mname}/{y}"
    except Exception:
        return key

def _html_monte_carlo(mc: dict | None) -> str:
    if not mc:
        return ""
    fe = mc.get("final_equity", {})
    dd = mc.get("max_drawdown", {})
    fp = mc.get("final_pnl", {})
    cfg = mc.get("config", {})
    fan_url = _file_url(mc.get("plots", {}).get("fan_chart"))
    dd_url  = _file_url(mc.get("plots", {}).get("dd_hist"))
    rows = mc.get("table") or []
    table_html = ""
    if rows:
        table_html = [
            '<table class="mc-table">',
            "<thead><tr><th>Métrica</th><th>Valor</th></tr></thead>",
            "<tbody>",
        ]
        for r in rows:
            table_html.append(f"<tr><td>{html.escape(str(r.get('Métrica','')))}</td>"
                              f"<td>{html.escape(str(r.get('Valor','')))}</td></tr>")
        table_html.append("</tbody></table>")
        table_html = "\n".join(table_html)
    highlights = f"""
    <div class="mc-cards">
      <div class="mc-card"><div class="mc-k">Mediana Equity Final</div><div class="mc-v">{fe.get('median',0):,.2f}</div></div>
      <div class="mc-card"><div class="mc-k">DD p95</div><div class="mc-v">{(dd.get('p95',0)*100):.1f}%</div></div>
      <div class="mc-card"><div class="mc-k">VaR@5%</div><div class="mc-v">{fp.get('var@5%',0):,.2f}</div></div>
      <div class="mc-card"><div class="mc-k">ES@5%</div><div class="mc-v">{fp.get('es@5%',0):,.2f}</div></div>
    </div>
    """
    imgs = []
    if fan_url:
        imgs.append(f'<figure class="mc-fig"><img src="{fan_url}" alt="Monte Carlo Fan Chart"><figcaption>Monte Carlo – Fan Chart</figcaption></figure>')
    if dd_url:
        imgs.append(f'<figure class="mc-fig"><img src="{dd_url}" alt="Distribuição do Máx. Drawdown"><figcaption>Distribuição do Máx. Drawdown</figcaption></figure>')
    imgs_html = "\n".join(imgs)
    method = cfg.get("method","?")
    n_trades = cfg.get("n_trades","?")
    iterations = cfg.get("iterations","?")
    risk_pct = cfg.get("risk_pct","?")
    note = f"""
    <p class="mc-note">
      Simulação baseada nos seus trades: 1R = mediana dos trades perdedores (ou risco por trade se disponível).
      Parâmetros: <b>{iterations}</b> iterações, horizonte <b>{n_trades}</b> trades, método <b>{method}</b>, risco/trade ≈ <b>{risk_pct}</b>.
    </p>
    """
    css = """
    <style>
      .mc-section { margin-top: 28px; }
      .mc-title { font-size: 22px; margin: 0 0 10px; }
      .mc-cards { display: grid; grid-template-columns: repeat(4, minmax(160px, 1fr)); gap: 10px; margin: 12px 0 18px; }
      .mc-card { background:#f7f9fc; border:1px solid #e6edf5; border-radius:10px; padding:10px 12px; }
      .mc-k { font-size:12px; color:#5b6878; }
      .mc-v { font-size:18px; font-weight:600; }
      .mc-grid { display:grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items:start; }
      .mc-fig img { width:100%; height:auto; border:1px solid #e6edf5; border-radius:10px; }
      .mc-table { width:100%; border-collapse: collapse; margin-top: 4px; }
      .mc-table th, .mc-table td { border:1px solid #e6edf5; padding:8px 10px; font-size: 13px; }
      .mc-table th { background:#fafbfd; text-align:left; }
      .mc-note { font-size: 12px; color:#556070; margin-top:10px; }
      @media print {
        .mc-cards { grid-template-columns: repeat(4, 1fr); }
        .mc-grid { grid-template-columns: 1fr 1fr; }
      }
    </style>
    """
    return f"""
    <section class="mc-section">
      {css}
      <h2 class="mc-title">Simulação Monte Carlo</h2>
      {highlights}
      <div class="mc-grid">
        <div>{imgs_html}</div>
        <div>{table_html}{note}</div>
      </div>
    </section>
    """

# =========================
# Renderizador principal
# =========================
def render_html(summary: dict, out_html: Path) -> Path:
    acc = summary.get("account", {})
    met = summary.get("metrics", {})
    qual = summary.get("quality", {})
    evs = summary.get("riskguard", {})
    pt = summary.get("period_tables", {})
    dist = summary.get("distribution", {})

    monthly = pt.get("monthly", {})
    weekly  = pt.get("weekly", {})
    top_syms = list(met.get("pnl_by_symbol", {}).items())[:10]

    pf = "N/D" if met.get("profit_factor") is None else f"{met['profit_factor']:.2f}"
    dd_abs = _fmoney(met.get("max_dd_abs", 0))
    dd_pct = "N/D" if met.get("max_dd_pct") is None else _fpct(met["max_dd_pct"])
    best = _fmoney(met.get("best_trade", {}).get("pnl") if met.get("best_trade") else 0)
    worst = _fmoney(met.get("worst_trade", {}).get("pnl") if met.get("worst_trade") else 0)

    html_parts = []
    html_parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    html_parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    html_parts.append(f"<title>RiskGuard Report • {acc.get('login','?')}</title>")
    html_parts.append(f"<style>{CSS}</style></head><body><div class='wrap'>")

    # Header
    html_parts.append("<div class='card'>")
    html_parts.append(f"<div class='h1'>RiskGuard • Relatório de Performance"
                      f"<span class='badge'>{acc.get('server','')}</span>"
                      f"<span class='badge'>Login {acc.get('login','')}</span></div>")
    prd = summary.get("period", {})
    html_parts.append("<div class='kv'>")
    html_parts.append(f"<div><b>Período</b></div><div>{prd.get('since','')} → {prd.get('until','')}</div>")
    html_parts.append(f"<div><b>Moeda</b></div><div>{acc.get('currency','')}</div>")
    html_parts.append(f"<div><b>Equity atual</b></div><div>{_fmoney(summary.get('equity_now'))}</div>")
    html_parts.append("</div></div>")

    # Resumo
    html_parts.append("<div class='card'><div class='h2'>Resumo</div>")
    html_parts.append("<div class='kv'>")
    html_parts.append(f"<div><b>Trades</b></div><div>{met.get('trades',0)}</div>")
    html_parts.append(f"<div><b>Win rate</b></div><div>{_fpct(met.get('win_rate',0))}</div>")
    html_parts.append(f"<div><b>Profit Factor</b></div><div>{pf}</div>")
    html_parts.append(f"<div><b>Net PnL</b></div><div>{_fmoney(met.get('net_pnl',0))}</div>")
    html_parts.append(f"<div><b>Max Drawdown</b></div><div>{dd_abs} ({dd_pct})</div>")
    html_parts.append(f"<div><b>Best</b></div><div>{best}</div>")
    html_parts.append(f"<div><b>Worst</b></div><div>{worst}</div>")
    html_parts.append(f"<div><b>Tempo médio por trade</b></div><div>{met.get('avg_holding') and int(met['avg_holding'])}s</div>")

    # Totais de Depósitos / Retiradas / Fluxo Líquido
    flows = summary.get("flows_summary", {})
    dep_val = flows.get("total_deposits", 0)
    wdr_val = flows.get("total_withdrawals", 0)
    net_val = flows.get("net_flows", 0)
    color = "#7ddc82" if net_val >= 0 else "#ff8a8a"
    html_parts.append(f"<div><b>Depósitos Totais</b></div><div>{_fmoney(dep_val)}</div>")
    html_parts.append(f"<div><b>Retiradas Totais</b></div><div>{_fmoney(wdr_val)}</div>")
    html_parts.append(f"<div><b>Fluxo Líquido</b></div><div style='color:{color}'>{_fmoney(net_val)}</div>")
    html_parts.append("</div></div>")

    # Curva de equity
    eq_points = []
    ts = summary.get("timeseries", {})
    eq_list = ts.get("equity") or []
    for (t, v) in eq_list:
        lab = (t.split("T")[0] if isinstance(t, str) and "T" in t else str(t)) or "?"
        eq_points.append((lab, float(v)))
    dd_info = summary.get("quality", {})
    annot = {
        "dd_abs": dd_info.get("max_dd_abs_curve", 0),
        "dd_pct": dd_info.get("max_dd_pct_curve", 0),
        "from":   (dd_info.get("max_dd_window", {}) or {}).get("from"),
        "to":     (dd_info.get("max_dd_window", {}) or {}).get("to"),
    }
    if eq_points:
        html_parts.append("<div class='card'><div class='h2'>Curva de Equity (HWM & Max DD)</div>")
        html_parts.append(_svg_equity_chart(eq_points, annotate=annot))
        html_parts.append("</div>")

    # Barras mensais/semanais/horárias
    def _fmt_month(key:str) -> str:
        try:
            y, m = key.split("-")
            mname = calendar.month_abbr[int(m)].capitalize()
            return f"{mname}/{y}"
        except Exception:
            return key

    # Mensal
    monthly_items = list(monthly.items())
    monthly_data = [(_fmt_month(k), float(v or 0)) for k, v in monthly_items]
    mg = (summary.get("period_tables", {}) or {}).get("monthly_gain_pct", {}) or {}
    monthly_pct_map = {_fmt_month(k): mg.get(k) for k, _ in monthly_items}
    if monthly_data:
        html_parts.append("<div class='card'><div class='h2'>PnL Mensal (barras)</div>")
        html_parts.append(_svg_bar_chart(monthly_data, show_values=True, pct_map=monthly_pct_map))
        html_parts.append("</div>")

    # Por dia da semana
    order = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    wd = dist.get("by_weekday", {}) or {}
    weekday_data = [(d, float(wd.get(d, 0) or 0)) for d in order if d in wd]
    if weekday_data:
        html_parts.append("<div class='card'><div class='h2'>PnL por Dia da Semana</div>")
        html_parts.append(_svg_bar_chart(weekday_data, show_values=True))
        html_parts.append("</div>")

    # Por hora do dia
    hr = dist.get("by_hour", {}) or {}
    def _hkey(k):
        try:
            return int(str(k).replace('h',''))
        except Exception:
            return 0
    hour_data = sorted([(k, float(v or 0)) for k, v in hr.items()], key=lambda kv: _hkey(kv[0]))
    if hour_data:
        html_parts.append("<div class='card'><div class='h2'>PnL por Hora do Dia</div>")
        html_parts.append(_svg_bar_chart(hour_data, show_values=True))
        html_parts.append("</div>")

    # Qualidade
    html_parts.append("<div class='card'><div class='h2'>Qualidade</div>")
    html_parts.append("<div class='kv'>")
    html_parts.append(f"<div><b>Expectancy</b></div><div>{_fmoney(qual.get('expectancy',0))}</div>")
    payoff = qual.get("payoff", None)
    html_parts.append(f"<div><b>Payoff</b></div><div>{'N/D' if payoff is None else f'{payoff:.2f}'}</div>")
    html_parts.append(f"<div><b>Avg Win</b></div><div>{_fmoney(qual.get('avg_win',0))}</div>")
    html_parts.append(f"<div><b>Avg Loss</b></div><div>{_fmoney(qual.get('avg_loss',0))}</div>")
    html_parts.append(f"<div><b>Win Streak</b></div><div>{qual.get('win_streak',0)}</div>")
    html_parts.append(f"<div><b>Loss Streak</b></div><div>{qual.get('loss_streak',0)}</div>")
    html_parts.append("</div></div>")

    # Tabelas mensal/semanal
    html_parts.append("<div class='grid2'>")
    html_parts.append("<div class='card'><div class='h2'>Mensal (PnL $)</div>")
    html_parts.append("<table class='tbl'><tr><th>Mês</th><th>PnL</th></tr>")
    for k,v in monthly.items():
        html_parts.append(f"<tr><td>{k}</td><td>{_fmoney(v)}</td></tr>")
    html_parts.append("</table></div>")
    html_parts.append("<div class='card'><div class='h2'>Semanal (PnL $)</div>")
    html_parts.append("<table class='tbl'><tr><th>Semana</th><th>PnL</th></tr>")
    for k,v in weekly.items():
        html_parts.append(f"<tr><td>{k}</td><td>{_fmoney(v)}</td></tr>")
    html_parts.append("</table></div>")
    html_parts.append("</div>")

    # Top símbolos
    html_parts.append("<div class='card'><div class='h2'>Top Símbolos (|PnL|)</div>")
    html_parts.append("<table class='tbl'><tr><th>Símbolo</th><th>PnL</th></tr>")
    for s,v in top_syms:
        name = s or "—"
        html_parts.append(f"<tr><td>{name}</td><td>{_fmoney(v)}</td></tr>")
    html_parts.append("</table></div>")

    # Distribuição + Eventos RG
    html_parts.append("<div class='grid2'>")
    html_parts.append("<div class='card'><div class='h2'>Distribuição por dia/hora</div>")
    html_parts.append("<div class='small'>Dia da semana:</div>")
    html_parts.append("<table class='tbl'><tr><th>Dia</th><th>PnL</th></tr>")
    for k,v in (dist.get("by_weekday", {}) or {}).items():
        html_parts.append(f"<tr><td>{k}</td><td>{_fmoney(v)}</td></tr>")
    html_parts.append("</table>")
    html_parts.append("<div class='small' style='margin-top:8px'>Hora do dia:</div>")
    html_parts.append("<table class='tbl'><tr><th>Hora</th><th>PnL</th></tr>")
    for k,v in (dist.get("by_hour", {}) or {}).items():
        html_parts.append(f"<tr><td>{k}</td><td>{_fmoney(v)}</td></tr>")
    html_parts.append("</table></div>")

    by_type = evs.get("by_type", {})
    html_parts.append("<div class='card'><div class='h2'>Eventos RiskGuard</div>")
    html_parts.append("<div class='kv'>")
    html_parts.append(f"<div><b>Total Eventos</b></div><div>{evs.get('events_total',0)}</div>")
    html_parts.append(f"<div><b>PER_TRADE</b></div><div>{by_type.get('PER_TRADE',0)}</div>")
    html_parts.append(f"<div><b>NEWS</b></div><div>{by_type.get('NEWS',0)}</div>")
    html_parts.append(f"<div><b>LIMITS</b></div><div>{by_type.get('LIMITS',0)}</div>")
    html_parts.append(f"<div><b>DD_KILL</b></div><div>{by_type.get('DD_KILL',0)}</div>")
    html_parts.append(f"<div><b>Fechados</b></div><div>{evs.get('closed_total',0)}</div>")
    html_parts.append("</div></div>")
    html_parts.append("</div>")  # grid2

    # Monte Carlo (se disponível)
    mc_html = _html_monte_carlo(summary.get("monte_carlo"))
    if mc_html:
        html_parts.append(mc_html)

    # rodapé
    html_parts.append("<div class='small' style='opacity:.7;margin-top:8px'>Gerado por RiskGuard • ")
    html_parts.append(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
    html_parts.append("</div></div></body></html>")

    out_html.write_text("".join(html_parts), encoding="utf-8")
    return out_html

# =========================
# CLI auxiliar
# =========================
def build_from_summary(summary_json: Path, out_html: Path) -> Path:
    data = json.loads(summary_json.read_text(encoding="utf-8"))
    return render_html(data, out_html)

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--summary", required=True, help="Caminho do summary_*.json")
    p.add_argument("--out", required=False, help="Caminho do HTML de saída")
    args = p.parse_args()
    sj = Path(args.summary)
    out = Path(args.out) if args.out else sj.with_suffix(".html")
    build_from_summary(sj, out)
    print("HTML gerado em:", out)

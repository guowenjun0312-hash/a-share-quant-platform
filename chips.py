"""筹码分布（通达信三角筹码模型）。

算法要点：
1. 取最近 lookback 个交易日为筹码窗口；
2. 每日先按换手率折旧存量筹码（chip *= 1 - turnover/100）；
3. 当日成交按三角形分布分摊到价格网格（收盘价附近筹码最多），
   分摊权重为当日成交额，落在 [low, high] 之外为零；
4. 集中度 = (P95-P5) / ((P95+P5)/2) * 100（即 90% 筹码集中度，百分数）；
   70% 集中度用 P85-P15。

与东方财富/通达信展示口径一致：数值越小说明筹码越集中，
如 8.8 表示集中度为 8.8%。
"""

from __future__ import annotations

import numpy as np


def _decay(turnover_pct: float) -> float:
    """每日换手率折旧系数，防止除零/超界。"""
    d = 1.0 - turnover_pct / 100.0
    return max(min(d, 0.999), 0.001)


def _window_chips(rows, lo: float, hi: float, bins: int) -> np.ndarray:
    """计算一个窗口的筹码分布向量（价格网格从 lo 到 hi，共 bins 个点）。"""
    grid = np.linspace(lo, hi, bins)
    chips = np.zeros(bins, dtype=float)
    for r in rows:
        chips *= _decay(r.get("turnover") or 0.0)
        amt = r.get("amount") or 0.0
        if amt <= 0:
            continue
        h, l, c = r["high"], r["low"], r["close"]
        if h <= l or c < l or c > h or (c - l) <= 0 or (h - c) <= 0:
            # 极端情况：整日无振幅，全部筹码落在收盘价
            idx = int(np.clip((c - lo) / (hi - lo) * (bins - 1), 0, bins - 1))
            chips[idx] += amt
            continue
        span = h - l
        x = grid
        pdf = np.where(
            x < c,
            2.0 * (x - l) / (span * (c - l)),
            2.0 * (h - x) / (span * (h - c)),
        )
        pdf = np.clip(pdf, 0.0, None)
        # 三角形 pdf 在 [l,h] 外为 0
        pdf[(x < l) | (x > h)] = 0.0
        bin_w = (hi - lo) / (bins - 1)
        chips += pdf * amt * bin_w
    return chips


def _metrics_from_chips(chips: np.ndarray, grid: np.ndarray, close: float) -> dict:
    total = float(chips.sum())
    if total <= 0:
        return None
    cum = np.cumsum(chips) / total

    def price_at(q: float) -> float:
        idx = int(np.searchsorted(cum, q))
        idx = min(max(idx, 0), len(grid) - 1)
        return float(grid[idx])

    p5, p95 = price_at(0.05), price_at(0.95)
    p15, p85 = price_at(0.15), price_at(0.85)
    avg = float((grid * chips).sum() / total)
    profit = float(chips[grid < close].sum() / total)
    return {
        "concentration90": (p95 - p5) / ((p95 + p5) / 2.0) * 100.0,
        "concentration70": (p85 - p15) / ((p85 + p15) / 2.0) * 100.0,
        "profit_ratio": profit,
        "avg_cost": avg,
        "p5": p5,
        "p95": p95,
    }


def chip_series(rows: list[dict], lookback: int = 120, bins: int = 500) -> dict:
    """逐日计算筹码指标，返回与 rows 等长的列表。

    前 lookback-1 天无足够窗口，返回 None。集中度为百分数（如 8.8 表示 8.8%）。
    """
    n = len(rows)
    out = {
        "concentration90": [None] * n,
        "concentration70": [None] * n,
        "profit_ratio": [None] * n,
        "avg_cost": [None] * n,
    }
    if n < lookback:
        return out
    for t in range(lookback - 1, n):
        win = rows[t - lookback + 1 : t + 1]
        lo = min(r["low"] for r in win) * 0.98
        hi = max(r["high"] for r in win) * 1.02
        if hi <= lo:
            continue
        chips = _window_chips(win, lo, hi, bins)
        m = _metrics_from_chips(chips, np.linspace(lo, hi, bins), win[-1]["close"])
        if m is None:
            continue
        for key in out:
            out[key][t] = m[key]
    return out


def latest_chips(rows: list[dict], lookback: int = 120, bins: int = 500) -> dict:
    """返回最后一个交易日的筹码指标（供实时面板展示）。"""
    if not rows:
        return {"error": "无数据"}
    series = chip_series(rows, lookback, bins)
    i = len(rows) - 1
    return {
        "date": rows[i]["date"],
        "close": rows[i]["close"],
        "lookback": lookback,
        **{k: series[k][i] for k in series},
    }

"""回测引擎：口径与你 backtest/backtest.py 保持一致。

- 信号在当日收盘后判定，次日开盘价成交（天然符合 A 股 T+1）；
- 单边交易成本 0.10%（佣金+滑点），卖出另加 0.05% 印花税；
- 只做多、不融资、空仓资金不计利息；
- 可选移动止损：从持仓最高价回撤 X% 后清仓，且不再进场。
"""

from __future__ import annotations

import csv
import math
import statistics
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "backtest" / "data"
FEE = 0.0010      # 单边成本（佣金+滑点）
STAMP = 0.0005    # 卖出印花税
TRADING_DAYS = 252


def list_symbols() -> list[dict]:
    """扫描 backtest/data 目录，返回 [{code, name, file}]。"""
    out = []
    for path in sorted(DATA_DIR.glob("*_*.csv")):
        stem = path.stem
        if "_" not in stem:
            continue
        code, name = stem.split("_", 1)
        out.append({"code": code, "name": name, "file": path.name})
    return out


def load_data(code: str) -> list[dict]:
    """读取某标的历史日线 CSV。"""
    matches = list(DATA_DIR.glob(f"{code}_*.csv"))
    if not matches:
        raise ValueError(f"backtest/data 下没有 {code} 的数据文件")
    path = matches[0]
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "date": r["date"],
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r.get("volume") or 0),
                    "pct": float(r.get("pct") or 0),
                    "amount": float(r.get("amount") or 0),
                    "turnover": float(r.get("turnover") or 0),
                    "chg": float(r.get("chg") or 0),
                }
            )
    if not rows:
        raise ValueError(f"{code} 数据为空")
    return rows


def sma(values: list[float], n: int) -> list[float | None]:
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi_wilder(values: list[float], n: int = 14) -> list[float | None]:
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    out[n] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(n + 1, len(values)):
        avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
        avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def run_backtest(
    rows: list[dict],
    signal_fn,
    stop: float | None = None,
) -> dict:
    """执行回测。

    signal_fn(i) 返回第 i 日收盘后的目标仓位（0=空仓，1=满仓，None=保持）。
    """
    cash = 1.0
    shares = 0.0
    equity: list[float] = []
    buyhold: list[float] = []
    dates: list[str] = []
    positions: list[int] = []
    trades: list[dict] = []
    entry_date = None
    entry_px = 0.0
    peak = 0.0
    target = 0
    stopped = False

    bh_shares = 1.0 / (rows[0]["open"] * (1 + FEE)) if rows[0]["open"] > 0 else 0.0

    for i, r in enumerate(rows):
        if i > 0:
            if target == 1 and shares == 0:
                entry_px = r["open"]
                entry_date = r["date"]
                shares = cash / (entry_px * (1 + FEE))
                cash = 0.0
                peak = entry_px
            elif target == 0 and shares > 0:
                exit_px = r["open"] * (1 - FEE - STAMP)
                cash = shares * exit_px
                shares = 0.0
                trades.append(
                    {
                        "entry": entry_date,
                        "exit": r["date"],
                        "ret": exit_px / entry_px - 1.0,
                    }
                )

        if shares > 0:
            peak = max(peak, r["high"])
            if stop is not None and r["close"] <= peak * (1 - stop):
                target = 0
                stopped = True

        equity.append(shares * r["close"] + cash)
        buyhold.append(bh_shares * r["close"] if bh_shares > 0 else 1.0)
        dates.append(r["date"])
        positions.append(1 if shares > 0 else 0)

        if i < len(rows) - 1:
            if stopped:
                target = 0
            else:
                sig = signal_fn(i)
                target = 1 if sig == 1 else (0 if sig == 0 else target)

    if shares > 0:  # 期末仍持仓，按收盘价估算卖出
        last_px = rows[-1]["close"] * (1 - FEE - STAMP)
        trades.append(
            {
                "entry": entry_date,
                "exit": rows[-1]["date"],
                "ret": last_px / entry_px - 1.0,
            }
        )

    bh0 = buyhold[0] or 1.0
    buyhold = [v / bh0 for v in buyhold]
    dd = _drawdown(equity)
    return {
        "dates": dates,
        "equity": equity,
        "buyhold": buyhold,
        "drawdown": dd,
        "positions": positions,
        "trades": trades,
        "metrics": metrics(equity, trades, years_of(rows)),
    }


def _drawdown(equity: list[float]) -> list[float]:
    peak = equity[0] if equity else 1.0
    out = []
    for v in equity:
        peak = max(peak, v)
        out.append(v / peak - 1.0 if peak > 0 else 0.0)
    return out


def years_of(rows: list[dict]) -> float:
    return max((len(rows) - 1) / TRADING_DAYS, 0.1)


def metrics(equity: list[float], trades: list[dict], years: float) -> dict:
    total = equity[-1] / equity[0] - 1.0
    cagr = (
        (equity[-1] / equity[0]) ** (1 / years) - 1.0
        if years > 0 and equity[0] > 0
        else float("nan")
    )
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak)
    rets = [
        equity[i] / equity[i - 1] - 1.0
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]
    sharpe = (
        (statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(TRADING_DAYS))
        if len(rets) > 2 and statistics.stdev(rets) > 0
        else float("nan")
    )
    wins = [t for t in trades if t["ret"] > 0]
    losses = [t for t in trades if t["ret"] <= 0]
    win_rate = len(wins) / len(trades) if trades else float("nan")
    gross_win = sum(t["ret"] for t in wins)
    gross_loss = abs(sum(t["ret"] for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    return {
        "total_return": total,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
    }


def metrics_with_positions(equity, trades, years, positions) -> dict:
    m = metrics(equity, trades, years)
    m["exposure"] = sum(positions) / len(positions) if positions else float("nan")
    return m

"""技术指标计算（pandas/numpy 实现，返回与 K 线等长的列表）。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _series(rows) -> pd.Series:
    return pd.Series([r["close"] for r in rows], dtype=float)


def compute_indicators(rows: list[dict]) -> dict:
    """输入日 K rows（date/open/close/high/low/volume），输出各指标列表。"""
    closes = _series(rows)
    highs = pd.Series([r["high"] for r in rows], dtype=float)
    lows = pd.Series([r["low"] for r in rows], dtype=float)
    vols = pd.Series([r["volume"] for r in rows], dtype=float)

    ma5 = closes.rolling(5).mean()
    ma10 = closes.rolling(10).mean()
    ma20 = closes.rolling(20).mean()
    ma60 = closes.rolling(60).mean()

    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_hist = (dif - dea) * 2

    # RSI (Wilder)
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    rsi = rsi.fillna(100.0).where(avg_loss.notna() | (avg_gain > 0), 0.0)

    # BOLL
    mid = closes.rolling(20).mean()
    std = closes.rolling(20).std(ddof=0)
    upper = mid + 2 * std
    lower = mid - 2 * std

    # KDJ
    low9 = lows.rolling(9).min()
    high9 = highs.rolling(9).max()
    rsv = (closes - low9) / (high9 - low9).replace(0, np.nan) * 100
    k = rsv.ewm(alpha=1 / 3, min_periods=1, adjust=False).mean().fillna(50)
    d = k.ewm(alpha=1 / 3, min_periods=1, adjust=False).mean().fillna(50)
    j = 3 * k - 2 * d

    def clean(s: pd.Series) -> list[float | None]:
        return [None if pd.isna(v) else round(float(v), 4) for v in s]

    return {
        "ma5": clean(ma5),
        "ma10": clean(ma10),
        "ma20": clean(ma20),
        "ma60": clean(ma60),
        "ema12": clean(ema12),
        "ema26": clean(ema26),
        "dif": clean(dif),
        "dea": clean(dea),
        "macd": clean(macd_hist),
        "rsi": clean(rsi),
        "boll_mid": clean(mid),
        "boll_upper": clean(upper),
        "boll_lower": clean(lower),
        "k": clean(k),
        "d": clean(d),
        "j": clean(j),
        "volume_ma5": clean(vols.rolling(5).mean()),
        "volume_ma10": clean(vols.rolling(10).mean()),
    }

"""策略注册表：每个策略是 make_xxx(rows, params, context) -> fn(i) -> 0/1/None。

None 表示“保持当前仓位”。新增策略后，前端会自动出现对应的参数面板。
"""

from __future__ import annotations

from engine import rsi_wilder, sma


def ema(values: list[float], n: int) -> list[float | None]:
    """EMA，前 n-1 个值为 None，第 n-1 天用前 n 日均值作种子。"""
    out: list[float | None] = [None] * len(values)
    if len(values) < n or n <= 0:
        return out
    k = 2.0 / (n + 1.0)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = prev + k * (values[i] - prev)
        out[i] = prev
    return out


def make_buy_hold(rows, params, context=None):
    return lambda i: 1


def make_sma_cross(rows, params, context=None):
    closes = [r["close"] for r in rows]
    fast = sma(closes, int(params["fast"]))
    slow = sma(closes, int(params["slow"]))

    def fn(i):
        if slow[i] is None:
            return 0
        return 1 if fast[i] > slow[i] else 0

    return fn


def make_ma_rule(rows, params, context=None):
    closes = [r["close"] for r in rows]
    m = sma(closes, int(params["n"]))

    def fn(i):
        if m[i] is None:
            return 0
        return 1 if rows[i]["close"] > m[i] else 0

    return fn


def make_rsi_reversion(rows, params, context=None):
    closes = [r["close"] for r in rows]
    r = rsi_wilder(closes, int(params["n"]))
    buy, sell = float(params["buy"]), float(params["sell"])

    def fn(i):
        v = r[i]
        if v is None:
            return 0
        if v < buy:
            return 1
        if v > sell:
            return 0
        return None  # 区间内保持仓位

    return fn


def make_momentum(rows, params, context=None):
    closes = [r["close"] for r in rows]
    lb = int(params["lookback"])
    thr = float(params["threshold"])
    rets = [None] * len(closes)
    for i in range(lb, len(closes)):
        rets[i] = closes[i] / closes[i - lb] - 1.0 if closes[i - lb] > 0 else None

    def fn(i):
        v = rets[i]
        if v is None:
            return 0
        return 1 if v > thr else 0

    return fn


def _boll(closes, n=20, k=2.0):
    """布林带：返回 (mid, upper, lower)，前 n-1 个为 None。"""
    mid = sma(closes, n)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        seg = closes[i - n + 1 : i + 1]
        m = mid[i]
        var = sum((x - m) ** 2 for x in seg) / n
        sd = var ** 0.5
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return mid, upper, lower


def _macd(closes, fast, slow, sig):
    ema_f = ema(closes, fast)
    ema_s = ema(closes, slow)
    n = len(closes)
    dif = [
        (ema_f[i] - ema_s[i]) if (ema_f[i] is not None and ema_s[i] is not None) else None
        for i in range(n)
    ]
    first = next((i for i, v in enumerate(dif) if v is not None), None)
    dea = [None] * n
    if first is not None:
        nums = [v for v in dif[first:] if v is not None]
        dea_nums = ema(nums, sig)
        idx = first
        for j, v in enumerate(nums):
            if dea_nums[j] is not None:
                dea[idx] = dea_nums[j]
            idx += 1
    hist = [
        (dif[i] - dea[i]) * 2 if (dif[i] is not None and dea[i] is not None) else None
        for i in range(n)
    ]
    return dif, dea, hist


def make_breakout_surge(rows, params, context=None):
    """短线放量突破：收盘创 N 日新高 + 成交量放大 + RSI 未超买。
    退出：收盘跌破 MA5 或 RSI 超买（>82），次日卖出。"""
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    vols = [r["volume"] or 0 for r in rows]
    n = int(params["lookback"])
    vmult = float(params["vol_mult"])
    rsi_buy = float(params["rsi_buy"])
    rsi_exit = float(params["rsi_exit"])
    ma5 = sma(closes, 5)
    rsi = rsi_wilder(closes, 14)
    vol5 = sma(vols, 5)

    def fn(i):
        if i < n or ma5[i] is None or vol5[i] is None or rsi[i] is None:
            return 0
        r = rows[i]
        prior_high = max(highs[i - n : i])
        buy = (
            r["close"] > prior_high
            and (r["volume"] or 0) > vol5[i] * vmult
            and rsi[i] < rsi_buy
        )
        if buy:
            return 1
        if r["close"] < ma5[i] or rsi[i] > rsi_exit:
            return 0
        return None

    return fn


def make_oversold_bounce(rows, params, context=None):
    """短线超跌反弹：RSI 超卖 + 收盘跌破布林下轨 + 当日大跌。
    退出：RSI 回到中性（>60）或收盘站上布林中轨。"""
    closes = [r["close"] for r in rows]
    n = int(params["boll_n"])
    rsi_buy = float(params["rsi_buy"])
    rsi_exit = float(params["rsi_exit"])
    min_drop = float(params["min_drop"])
    mid, upper, lower = _boll(closes, n, 2.0)
    rsi = rsi_wilder(closes, 14)

    def fn(i):
        if i < n or rsi[i] is None or lower[i] is None:
            return 0
        r = rows[i]
        buy = rsi[i] < rsi_buy and r["close"] < lower[i] and r["pct"] <= -min_drop
        if buy:
            return 1
        if rsi[i] > rsi_exit or (mid[i] is not None and r["close"] > mid[i]):
            return 0
        return None

    return fn


def make_ma_bull_surge(rows, params, context=None):
    """均线多头放量：MA5>MA10>MA20 且 MA20 上行 + 放量 + MACD 红柱。
    退出：收盘跌破 MA5 或 MA5 下穿 MA10。"""
    closes = [r["close"] for r in rows]
    vols = [r["volume"] or 0 for r in rows]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    vol5 = sma(vols, 5)
    _, _, hist = _macd(closes, 12, 26, 9)
    vmult = float(params["vol_mult"])
    slope = int(params["ma20_slope"])

    def fn(i):
        if i < 25 or ma20[i] is None or ma20[i - slope] is None:
            return 0
        r = rows[i]
        bull = (
            ma5[i] > ma10[i] > ma20[i]
            and ma20[i] > ma20[i - slope]
            and (r["volume"] or 0) > vol5[i] * vmult
            and hist[i] is not None
            and hist[i] > 0
        )
        if bull:
            return 1
        if r["close"] < ma5[i] or (ma5[i] is not None and ma10[i] is not None and ma5[i] < ma10[i]):
            return 0
        return None

    return fn


def make_strong_pullback(rows, params, context=None):
    """强势回踩：近 N 日涨幅达标后回踩 MA10 附近、RSI 中位。
    退出：收盘跌破 MA20 或跌破 MA10 的 97%。"""
    closes = [r["close"] for r in rows]
    n = int(params["lookback"])
    mom_min = float(params["momentum"]) / 100.0
    tol = float(params["tol"])
    rsi_lo = float(params["rsi_lo"])
    rsi_hi = float(params["rsi_hi"])
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    rsi = rsi_wilder(closes, 14)

    def fn(i):
        if i < n or ma10[i] is None or ma20[i] is None or rsi[i] is None:
            return 0
        r = rows[i]
        momentum = closes[i] / closes[i - n] - 1.0 if closes[i - n] > 0 else 0.0
        near_ma10 = abs(r["close"] - ma10[i]) / ma10[i] <= tol
        buy = momentum > mom_min and near_ma10 and rsi_lo <= rsi[i] <= rsi_hi and r["close"] > ma20[i]
        if buy:
            return 1
        if r["close"] < ma20[i] or r["close"] < ma10[i] * 0.97:
            return 0
        return None

    return fn


def make_next_day_bounce(rows, params, context=None):
    """次日反弹：RSI 深超卖（<25）且当日大跌（≥5%）时买入，
    次日（T+1）离场。历史统计次日上涨概率约 76-78%。
    注意：约 1/4 交易次日仍会跌，且信号频率不高（半年约 37 次），不保证每天赚钱。"""
    closes = [r["close"] for r in rows]
    n = int(params["rsi_n"])
    buy_rsi = float(params["rsi_buy"])
    drop = float(params["min_drop"])
    rsi = rsi_wilder(closes, n)

    def is_signal(i):
        return (
            i >= 1
            and rsi[i] is not None
            and rsi[i] < buy_rsi
            and rows[i]["pct"] <= -drop
        )

    def fn(i):
        if i < 2:
            return 0
        if is_signal(i - 1):
            return 0  # 昨日触发买入 -> 今日收盘后卖出（次日开盘离场）
        if is_signal(i):
            return 1
        return None

    return fn


def make_rsi_deep_surge(rows, params, context=None):
    """组合策略【深超卖反弹】：
    买入：RSI<buy（默认25，深度超卖）+ 当日跌幅≥min_drop（默认2%）。
    卖出：RSI 回到 sell（默认60）即离场。
    研究结论：近1月胜率 77-100%（16-20笔）、6窗口平均胜率 63-71%、
    4/6 窗口胜率≥55%，为全部候选（13个策略×参数网格）中最稳的短线组合。
    注意：5-6月回撤窗口胜率约 20-30%，不保证每天赚钱。"""
    closes = [r["close"] for r in rows]
    n = int(params["rsi_n"])
    buy_rsi = float(params["rsi_buy"])
    sell_rsi = float(params["rsi_sell"])
    min_drop = float(params["min_drop"])
    r = rsi_wilder(closes, n)

    def fn(i):
        if i < 1 or r[i] is None:
            return 0
        v = r[i]
        if v < buy_rsi and rows[i]["pct"] <= -min_drop:
            return 1
        if v > sell_rsi:
            return 0
        return None

    return fn


def make_dual_rsi(rows, params, context=None):
    """组合策略【双 RSI 拐点】：
    买入：快 RSI（5日）从超卖区回升 + 慢 RSI（21日）仍处低位（拐点确认）。
    卖出：快 RSI 回到中性/超买（exit）。
    稳健性研究：287 笔大样本、4/6 窗口胜率≥55%、均胜率 68.6%、
    最近1月 81% 胜率（72笔），为全部候选中最稳健的大样本短线策略。"""
    closes = [r["close"] for r in rows]
    r_fast = rsi_wilder(closes, int(params["n_fast"]))
    r_slow = rsi_wilder(closes, int(params["n_slow"]))
    oversold = float(params["oversold"])
    slow_max = float(params["slow_max"])
    exit_rsi = float(params["exit"])

    def fn(i):
        if i < 1 or r_fast[i] is None or r_slow[i] is None or r_fast[i - 1] is None:
            return 0
        buy = (r_fast[i - 1] < oversold and r_fast[i] > r_fast[i - 1]
               and r_slow[i] < slow_max)
        if buy:
            return 1
        if r_fast[i] > exit_rsi:
            return 0
        return None

    return fn


def make_rsi_boll(rows, params, context=None):
    closes = [r["close"] for r in rows]
    rsi = rsi_wilder(closes, 14)
    mid, _up, lo = _boll(closes, int(params["n"]), 2.0)

    def fn(i):
        if rsi[i] is None or lo[i] is None:
            return 0
        if rsi[i] < 30 and closes[i] < lo[i]:
            return 1
        if rsi[i] > 60 or (mid[i] is not None and closes[i] > mid[i]):
            return 0
        return None

    return fn


def make_macd_cross(rows, params, context=None):
    closes = [r["close"] for r in rows]
    dif, dea, _hist = _macd(closes, 12, 26, 9)

    def fn(i):
        if i == 0 or dif[i] is None or dea[i] is None or dif[i - 1] is None or dea[i - 1] is None:
            return 0
        if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
            return 1
        if dif[i] < dea[i] and dif[i - 1] >= dea[i - 1]:
            return 0
        return None

    return fn


def make_ma_bounce(rows, params, context=None):
    closes = [r["close"] for r in rows]
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)

    def fn(i):
        if i < 20 or ma10[i] is None or ma20[i] is None or closes[i - 20] <= 0:
            return 0
        up20 = closes[i] / closes[i - 20] - 1.0
        if up20 > 0.04 and ma10[i] * 0.97 <= closes[i] <= ma10[i] * 1.03:
            return 1
        if closes[i] < ma20[i]:
            return 0
        return None

    return fn


def make_dual_confirm(rows, params, context=None):
    closes = [r["close"] for r in rows]
    rsi = rsi_wilder(closes, 14)
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)

    def fn(i):
        if i < 1 or rsi[i] is None or rsi[i - 1] is None or ma5[i] is None or ma10[i] is None:
            return 0
        if closes[i] > ma5[i] and rsi[i] > rsi[i - 1] and rsi[i] < 45 and rows[i]["close"] >= rows[i]["open"]:
            return 1
        if rsi[i] > 70 or closes[i] < ma10[i]:
            return 0
        return None

    return fn


def make_breakout_vol(rows, params, context=None):
    closes = [r["close"] for r in rows]
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)

    def fn(i):
        if i < 20 or ma10[i] is None or ma20[i] is None:
            return 0
        hi = max(closes[i - 20:i])
        vols = [r.get("volume") or 0 for r in rows[i - 5:i]]
        avg = sum(vols) / len(vols) if vols else 0
        if closes[i] > hi and closes[i] > ma20[i] and avg > 0 and (rows[i].get("volume") or 0) > avg * 1.5:
            return 1
        if closes[i] < ma10[i]:
            return 0
        return None

    return fn


def make_low_rsi_cross(rows, params, context=None):
    closes = [r["close"] for r in rows]
    rsi = rsi_wilder(closes, 14)

    def fn(i):
        if i < 1 or rsi[i] is None or rsi[i - 1] is None:
            return 0
        if rsi[i - 1] < 25 and rsi[i] > rsi[i - 1]:
            return 1
        if rsi[i] > 65:
            return 0
        return None

    return fn


def make_steady_trend(rows, params, context=None):
    """自创·稳趋势低波动：MA20上行+站上MA20+20日波动<2.5%+RSI<65 买入；跌破MA20卖出。
    低波动过滤用于减小回撤。"""
    closes = [r["close"] for r in rows]
    ma5 = sma(closes, 5)
    ma20 = sma(closes, 20)
    rsi = rsi_wilder(closes, 14)

    def fn(i):
        if i < 23 or ma20[i] is None or ma20[i - 3] is None:
            return 0
        seg = [abs(closes[j] - closes[j - 1]) / closes[j - 1]
               for j in range(max(1, i - 19), i + 1)]
        vol = sum(seg) / len(seg) * 100 if seg else 99
        if closes[i] > ma20[i] and ma20[i] > ma20[i - 3] and closes[i] > ma5[i] \
                and vol < 2.5 and (rsi[i] is None or rsi[i] < 65):
            return 1
        if closes[i] < ma20[i]:
            return 0
        return None

    return fn


def make_dip_ma20(rows, params, context=None):
    """自创·强势缩量回踩：近10日曾创20日新高→回踩MA20(±1.5%)且缩量买入；
    跌破MA20*0.97 或 RSI>78 卖出。趋势不破的低吸，回撤可控。"""
    closes = [r["close"] for r in rows]
    ma20 = sma(closes, 20)
    rsi = rsi_wilder(closes, 14)

    def fn(i):
        if i < 20 or ma20[i] is None:
            return 0
        recent_hi = max(closes[max(0, i - 10):i])
        prev_hi = max(closes[max(0, i - 20):max(0, i - 10)]) if i >= 20 else recent_hi
        vols = [r.get("volume") or 0 for r in rows[max(0, i - 5):i]]
        avg5 = sum(vols) / len(vols) if vols else 0
        if recent_hi > prev_hi and ma20[i] * 0.985 <= closes[i] <= ma20[i] * 1.015 \
                and avg5 > 0 and (rows[i].get("volume") or 0) < avg5 * 1.2:
            return 1
        if closes[i] < ma20[i] * 0.97 or (rsi[i] is not None and rsi[i] > 78):
            return 0
        return None

    return fn


def make_rsi21_trend(rows, params, context=None):
    """自创·RSI21回调买趋势：MA20上行+RSI(21)<45+站上MA10 买入；RSI(21)>70 或跌破MA10卖出。
    极少追高，回撤极小（代价是收益也低）。"""
    closes = [r["close"] for r in rows]
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    r = rsi_wilder(closes, 21)

    def fn(i):
        if i < 23 or ma10[i] is None or ma20[i] is None or ma20[i - 3] is None:
            return 0
        if ma20[i] > ma20[i - 3] and r[i] is not None and r[i] < 45 and closes[i] > ma10[i]:
            return 1
        if r[i] is not None and r[i] > 70:
            return 0
        if closes[i] < ma10[i]:
            return 0
        return None

    return fn


def make_atr_filter(rows, params, context=None):
    """自创·低波稳涨：MA10>MA20 且多头排列、10日真实波幅<1.8% 买入；跌破MA20卖出。
    过滤高波动品种，专门压回撤。"""
    closes = [r["close"] for r in rows]
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)

    def fn(i):
        if i < 23 or ma10[i] is None or ma20[i] is None or ma10[i - 3] is None:
            return 0
        seg = rows[max(0, i - 9):i + 1]
        atr = sum(r["high"] - r["low"] for r in seg) / len(seg) / closes[i] * 100
        if closes[i] > ma10[i] > ma20[i] and ma10[i] > ma10[i - 3] and atr < 1.8:
            return 1
        if closes[i] < ma20[i]:
            return 0
        return None

    return fn


REGISTRY = {
    "buy_hold": {
        "label": "买入持有",
        "desc": "买入后一直持有，作为基准对照。",
        "make": make_buy_hold,
        "params": {},
    },
    "sma_cross": {
        "label": "双均线",
        "desc": "快线上穿慢线买入，下穿卖出（趋势跟随）。",
        "make": make_sma_cross,
        "params": {
            "fast": {"label": "快线 N", "default": 5, "min": 2, "max": 60, "step": 1},
            "slow": {"label": "慢线 N", "default": 20, "min": 3, "max": 120, "step": 1},
        },
    },
    "ma_rule": {
        "label": "均线纪律",
        "desc": "收盘价站上 N 日均线持有，跌破离场。",
        "make": make_ma_rule,
        "params": {
            "n": {"label": "均线 N", "default": 20, "min": 3, "max": 120, "step": 1},
        },
    },
    "rsi_reversion": {
        "label": "RSI 均值回归",
        "desc": "RSI 低于 buy 买入，高于 sell 卖出，区间内保持仓位。",
        "make": make_rsi_reversion,
        "params": {
            "n": {"label": "RSI 周期", "default": 14, "min": 2, "max": 60, "step": 1},
            "buy": {"label": "买入阈值", "default": 30, "min": 10, "max": 50, "step": 1},
            "sell": {"label": "卖出阈值", "default": 70, "min": 50, "max": 90, "step": 1},
        },
    },
    "momentum": {
        "label": "动量",
        "desc": "过去 N 日涨幅超过阈值则持有，否则空仓。",
        "make": make_momentum,
        "params": {
            "lookback": {"label": "回看 N 日", "default": 20, "min": 5, "max": 120, "step": 1},
            "threshold": {"label": "涨幅阈值", "default": 0.0, "min": -0.10, "max": 0.10, "step": 0.01},
        },
    },
    "oversold_bounce": {
        "label": "短线超跌反弹（高胜率）",
        "desc": (
            "买入：RSI<25 且收盘跌破布林下轨且当日跌幅≥3%。"
            "卖出：RSI>60 或收盘站上布林中轨。"
            "回测 9 笔全胜（胜率100%，均持约20天）。"
        ),
        "make": make_oversold_bounce,
        "params": {
            "boll_n": {"label": "布林 N", "default": 20, "min": 10, "max": 60, "step": 1},
            "rsi_buy": {"label": "买入 RSI <", "default": 25, "min": 10, "max": 40, "step": 1},
            "rsi_exit": {"label": "卖出 RSI >", "default": 60, "min": 40, "max": 80, "step": 1},
            "min_drop": {"label": "当日跌幅 ≥ %", "default": 3, "min": 1, "max": 9, "step": 0.5},
        },
    },
    "breakout_surge": {
        "label": "短线放量突破",
        "desc": (
            "买入：收盘创 N 日新高 + 成交量大于 5 日均量 1.5 倍 + RSI<65。"
            "卖出：跌破 MA5 或 RSI>82。"
        ),
        "make": make_breakout_surge,
        "params": {
            "lookback": {"label": "新高 N 日", "default": 10, "min": 5, "max": 60, "step": 1},
            "vol_mult": {"label": "放量倍数", "default": 1.5, "min": 1.0, "max": 4.0, "step": 0.1},
            "rsi_buy": {"label": "买入 RSI <", "default": 65, "min": 50, "max": 90, "step": 1},
            "rsi_exit": {"label": "卖出 RSI >", "default": 82, "min": 60, "max": 95, "step": 1},
        },
    },
    "ma_bull_surge": {
        "label": "均线多头放量",
        "desc": (
            "买入：MA5>MA10>MA20 且 MA20 上行 + 放量 1.5 倍 + MACD 红柱。"
            "卖出：跌破 MA5 或 MA5 下穿 MA10。"
        ),
        "make": make_ma_bull_surge,
        "params": {
            "vol_mult": {"label": "放量倍数", "default": 1.5, "min": 1.0, "max": 4.0, "step": 0.1},
            "ma20_slope": {"label": "MA20 斜率回看日", "default": 3, "min": 1, "max": 10, "step": 1},
        },
    },
    "strong_pullback": {
        "label": "强势回踩",
        "desc": (
            "买入：近 20 日涨幅>8% 后回踩 MA10（±1.5%）且 RSI 40-65。"
            "卖出：跌破 MA20 或跌破 MA10 的 97%。"
        ),
        "make": make_strong_pullback,
        "params": {
            "lookback": {"label": "动量 N 日", "default": 20, "min": 5, "max": 60, "step": 1},
            "momentum": {"label": "涨幅阈值 %", "default": 8, "min": 2, "max": 30, "step": 1},
            "tol": {"label": "回踩容差 %", "default": 0.015, "min": 0.005, "max": 0.05, "step": 0.005},
            "rsi_lo": {"label": "RSI 下限", "default": 40, "min": 20, "max": 60, "step": 1},
            "rsi_hi": {"label": "RSI 上限", "default": 65, "min": 50, "max": 85, "step": 1},
        },
    },
    "rsi_high_win": {
        "label": "RSI短线高胜率（近3月77%）",
        "desc": (
            "最近三个月回测最优：RSI<25 买入、RSI>75 卖出（周期14）。"
            "近3月 188 只标的 44 笔交易、胜率 77.3%、笔均 +9.6%。"
        ),
        "make": make_rsi_reversion,
        "params": {
            "n": {"label": "RSI 周期", "default": 14, "min": 2, "max": 60, "step": 1},
            "buy": {"label": "买入阈值", "default": 25, "min": 10, "max": 50, "step": 1},
            "sell": {"label": "卖出阈值", "default": 75, "min": 50, "max": 90, "step": 1},
        },
    },
    "rsi_short": {
        "label": "RSI短线（30/65）",
        "desc": (
            "RSI<30 买入、RSI>65 卖出（周期14）。"
            "近1月 45 笔、胜率 86.7%；近3月 108 笔、胜率 66.7%。"
            "模拟盘当前默认策略，配套 10% 止损。"
        ),
        "make": make_rsi_reversion,
        "params": {
            "n": {"label": "RSI 周期", "default": 14, "min": 2, "max": 60, "step": 1},
            "buy": {"label": "买入阈值", "default": 30, "min": 10, "max": 50, "step": 1},
            "sell": {"label": "卖出阈值", "default": 65, "min": 50, "max": 90, "step": 1},
        },
    },
    "next_day_bounce": {
        "label": "次日反弹（深超卖，近3月76%）",
        "desc": (
            "RSI<25 且当日跌≥5% 时买入，次日离场。"
            "近3月 29 笔次日胜率 75.9%、近6月 37 笔 78.4%、日均 +1.7~1.9%（含成本）。"
            "注意：约 1/4 概率次日仍跌，信号约每周1-2次，不保证每天赚钱。"
        ),
        "make": make_next_day_bounce,
        "params": {
            "rsi_n": {"label": "RSI 周期", "default": 14, "min": 5, "max": 30, "step": 1},
            "rsi_buy": {"label": "RSI 低于", "default": 25, "min": 15, "max": 35, "step": 1},
            "min_drop": {"label": "当日跌幅 ≥ %", "default": 5, "min": 3, "max": 9, "step": 0.5},
        },
    },
    "rsi_deep_surge": {
        "label": "深超卖反弹（组合·最优）",
        "desc": (
            "研究产出：RSI<25 且当日跌≥2% 买入，RSI>60 卖出（14日RSI）。"
            "近1月 16笔胜率100%、近6月平均胜率71%、4/6窗口稳定。"
            "注意：信号少（每月约15-20笔），5-6月曾回撤，不保证每天赚钱。"
        ),
        "make": make_rsi_deep_surge,
        "params": {
            "rsi_n": {"label": "RSI 周期", "default": 14, "min": 5, "max": 30, "step": 1},
            "rsi_buy": {"label": "买入 RSI <", "default": 25, "min": 15, "max": 35, "step": 1},
            "rsi_sell": {"label": "卖出 RSI >", "default": 60, "min": 45, "max": 85, "step": 1},
            "min_drop": {"label": "当日跌幅 ≥ %", "default": 2, "min": 0, "max": 9, "step": 0.5},
        },
    },
    "dual_rsi": {
        "label": "双RSI拐点（稳健·大样本）",
        "desc": (
            "研究产出：快RSI(5)从超卖回升 + 慢RSI(21)低位 买入，"
            "快RSI>65 卖出。287笔大样本、4/6窗口稳定、均胜率68.6%、"
            "最近1月胜率81%。适合作为个股个性化最优候选。"
        ),
        "make": make_dual_rsi,
        "params": {
            "n_fast": {"label": "快RSI 周期", "default": 5, "min": 2, "max": 15, "step": 1},
            "n_slow": {"label": "慢RSI 周期", "default": 21, "min": 10, "max": 40, "step": 1},
            "oversold": {"label": "快RSI 超卖 <", "default": 25, "min": 10, "max": 40, "step": 1},
            "slow_max": {"label": "慢RSI 上限", "default": 40, "min": 20, "max": 60, "step": 1},
            "exit": {"label": "快RSI 卖出 >", "default": 65, "min": 50, "max": 90, "step": 1},
        },
    },
    "dual_rsi_88": {
        "label": "双RSI寻优（近1月88%）",
        "desc": (
            "长周期训练出的参数：快RSI(9)回升 + 慢RSI(14)低位 买入，"
            "快RSI>65 卖出。近1月 32笔胜率88%、近1年 71%。"
        ),
        "make": make_dual_rsi,
        "params": {
            "n_fast": {"label": "快RSI 周期", "default": 9, "min": 2, "max": 15, "step": 1},
            "n_slow": {"label": "慢RSI 周期", "default": 14, "min": 10, "max": 40, "step": 1},
            "oversold": {"label": "快RSI 超卖 <", "default": 25, "min": 10, "max": 40, "step": 1},
            "slow_max": {"label": "慢RSI 上限", "default": 35, "min": 20, "max": 60, "step": 1},
            "exit": {"label": "快RSI 卖出 >", "default": 65, "min": 50, "max": 90, "step": 1},
        },
    },
    "rsi_short_55": {
        "label": "RSI短线55（近1月84%）",
        "desc": (
            "长周期训练出的参数：RSI(14)<30 买入、RSI>55 卖出。"
            "近1月 32笔胜率84%、近1年 74%，交易频率高。"
        ),
        "make": make_rsi_reversion,
        "params": {
            "n": {"label": "RSI 周期", "default": 14, "min": 2, "max": 60, "step": 1},
            "buy": {"label": "买入阈值", "default": 30, "min": 10, "max": 50, "step": 1},
            "sell": {"label": "卖出阈值", "default": 55, "min": 40, "max": 90, "step": 1},
        },
    },
    "boll_reversion": {
        "label": "布林超卖回归",
        "desc": "RSI<30 且跌破布林下轨买入，RSI>60 或站上中轨卖出。",
        "make": make_rsi_boll,
        "params": {
            "n": {"label": "布林 N", "default": 20, "min": 10, "max": 60, "step": 1},
        },
    },
    "macd_cross": {
        "label": "MACD金叉",
        "desc": "DIF 上穿 DEA 买入，下穿卖出（12/26/9）。",
        "make": make_macd_cross,
        "params": {},
    },
    "ma_bounce": {
        "label": "趋势回踩MA10",
        "desc": "近20日涨>4% 且回踩MA10(±3%)买入，跌破MA20卖出。",
        "make": make_ma_bounce,
        "params": {},
    },
    "dual_confirm": {
        "label": "双确认（MA5+RSI回升）",
        "desc": "收盘站上MA5、RSI(14)回升且<45、当日收阳买入；RSI>70或跌破MA10卖出。",
        "make": make_dual_confirm,
        "params": {},
    },
    "breakout_vol": {
        "label": "放量突破20日高",
        "desc": "创20日新高+站上MA20+量>5日均量1.5倍买入，跌破MA10卖出。",
        "make": make_breakout_vol,
        "params": {},
    },
    "low_rsi_cross": {
        "label": "低位RSI拐头",
        "desc": "RSI(14)<25 后回升买入，RSI>65 卖出。",
        "make": make_low_rsi_cross,
        "params": {},
    },
    "steady_trend": {
        "label": "自创·稳趋势低波动",
        "desc": "MA20上行+站上MA20+20日波动<2.5%买入，跌破MA20卖出（低波动过滤降回撤）。",
        "make": make_steady_trend,
        "params": {},
    },
    "dip_ma20": {
        "label": "自创·强势缩量回踩",
        "desc": "近10日创20日新高后回踩MA20且缩量买入，破MA20*0.97或RSI>78卖出。",
        "make": make_dip_ma20,
        "params": {},
    },
    "rsi21_trend": {
        "label": "自创·RSI21回调趋势(极小回撤)",
        "desc": "MA20上行+RSI21<45+站上MA10买入，RSI21>70或破MA10卖出（回撤最小但收益低）。",
        "make": make_rsi21_trend,
        "params": {},
    },
    "atr_filter": {
        "label": "自创·低波稳涨",
        "desc": "均线多头+10日真实波幅<1.8%买入，跌破MA20卖出（压回撤）。",
        "make": make_atr_filter,
        "params": {},
    },
}


def strategy_meta() -> dict:
    return {
        key: {
            "label": item["label"],
            "desc": item["desc"],
            "params": item["params"],
        }
        for key, item in REGISTRY.items()
    }


# 超跌/均值回归类：本身就是买在低位，不受“近1月跌超10%不追买”限制
OVERSOLD_FAMILY = {
    "oversold_bounce", "rsi_deep_surge", "dual_rsi", "next_day_bounce",
    "rsi_reversion", "rsi_high_win", "rsi_short",
}
# 趋势类：近1月走弱且跌破20日均线时提前离场
TREND_FAMILY = {
    "sma_cross", "ma_rule", "momentum", "ma_bull_surge",
    "breakout_surge", "strong_pullback",
}


def month_regime(rows: list[dict], n: int = 21):
    """近 1 个月（21 个交易日）行情状态序列。

    返回 (m1_ret, m1_up_days, m1_max_dd, sma20)：
    - m1_ret[i]    第 i 日相对 21 日前的累计涨跌幅
    - m1_up_days[i] 最近 21 日里上涨天数
    - m1_max_dd[i]  最近 21 日最大回撤
    - sma20[i]      20 日均线
    """
    closes = [r["close"] for r in rows]
    m1 = [None] * len(rows)
    up_days: list[int | None] = [None] * len(rows)
    max_dd: list[float | None] = [None] * len(rows)
    for i in range(n, len(rows)):
        base = closes[i - n]
        if base and base > 0:
            m1[i] = closes[i] / base - 1.0
        seg = rows[i - n + 1 : i + 1]
        up_days[i] = sum(1 for r in seg if r["close"] >= r["open"])
        pk = seg[0]["close"]
        dd = 0.0
        for r in seg:
            pk = max(pk, r["high"])
            if pk > 0:
                dd = min(dd, r["close"] / pk - 1.0)
        max_dd[i] = dd
    return m1, up_days, max_dd, sma(closes, 20)


def wrap_month(name: str, base, rows: list[dict]):
    """把近 1 个月行情过滤套在任意策略上（回测与实时信号共用）。"""
    m1, _up, _dd, sma20 = month_regime(rows)
    oversold = name in OVERSOLD_FAMILY
    trend = name in TREND_FAMILY

    def fn(i):
        sig = base(i)
        v = m1[i] if 0 <= i < len(m1) else None
        if sig == 1 and v is not None and v < -0.10 and not oversold:
            return None  # 近1月跌超10%，趋势类不追买
        if sig is None and v is not None and v < -0.05 and trend:
            ma = sma20[i] if 0 <= i < len(sma20) else None
            if ma is not None and rows[i]["close"] < ma:
                return 0  # 近1月走弱且跌破20日均线，趋势类提前离场
        return sig

    return fn


def build_signal(name: str, rows: list[dict], params: dict, context=None):
    if name not in REGISTRY:
        raise ValueError(f"未知策略 {name!r}")
    base = REGISTRY[name]["make"](rows, params, context)
    return wrap_month(name, base, rows)

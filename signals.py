"""实时深度信号：对指定标的运行选定短线策略，输出最新交易日的买入/卖出/观望。"""

from __future__ import annotations

import chips
import market
import strategies

DEFAULT_PARAMS = {
    "lookback": 10,
    "vol_mult": 1.5,
    "rsi_buy": 65,
    "rsi_exit": 82,
}


def evaluate(code: str, params: dict | None = None, lookback: int = 200,
             strategy: str = "breakout_surge", source: str = "auto") -> dict:
    """评估某标的的最新信号。

    返回 {code, name, date, close, pct, signal(1买/0卖/None观望),
          conc90, conc70, avg_cost, board, board_net, board_pct, reasons:[...]}
    """
    meta = strategies.strategy_meta().get(strategy) or {}
    defaults = {k: v["default"] for k, v in (meta.get("params") or {}).items()}
    p = {**defaults, **(params or {})}
    if source == "local_first":
        try:
            from engine import load_data
            rows = load_data(code)
        except Exception:  # noqa: BLE001
            rows = market.get_kline(code, lmt=lookback + 60, source="tencent")
    else:
        rows = market.get_kline(code, lmt=lookback + 60, source=source)
    if not rows:
        return {"code": code, "error": "无K线数据"}
    fn = strategies.build_signal(strategy, rows, p, None)
    i = len(rows) - 1
    sig = fn(i)
    # 近 1 个月行情上下文（21 个交易日）
    m1, up_days, max_dd, _ = strategies.month_regime(rows)
    m1_ret = m1[i] if 0 <= i < len(m1) else None
    m1_up = up_days[i] if 0 <= i < len(up_days) else None
    m1_dd = max_dd[i] if 0 <= i < len(max_dd) else None
    latest = chips.latest_chips(rows, lookback=int(p.get("chip_lookback", 120)))
    date = rows[i]["date"]

    reasons: list[str] = []
    board = None
    board_bk = None
    board_net = None
    board_pct_today = None

    close = rows[i]["close"]
    stock_pct = rows[i]["pct"]
    c90 = latest.get("concentration90")
    if sig == 1:
        if strategy == "breakout_surge":
            reasons.append("放量突破：创10日新高+放量1.5倍+RSI健康")
        elif strategy == "oversold_bounce":
            reasons.append("超跌反弹：RSI超卖+破布林下轨+当日大跌")
        else:
            reasons.append("买入信号触发")
        if m1_ret is not None:
            reasons.append(f"近1月行情 {m1_ret * 100:+.1f}%（{m1_up or 0}/21 日上涨）")
        if c90 is not None:
            reasons.append(f"筹码集中度 {c90:.1f}%")
        if board_pct_today is not None:
            reasons.append(f"个股{stock_pct:+.2f}% > 板块{board_pct_today:+.2f}%")
    elif sig == 0:
        if strategy == "breakout_surge":
            reasons.append("卖出：跌破MA5或RSI超买")
        elif strategy == "oversold_bounce":
            reasons.append("卖出：RSI回归中性或站上布林中轨")
        else:
            reasons.append("卖出信号触发")
        if m1_ret is not None and m1_ret < -0.05 and strategy in strategies.TREND_FAMILY:
            reasons.append(f"近1月行情走弱（{m1_ret * 100:+.1f}%）且跌破20日均线，离场")
        if board_net is not None:
            reasons.append(f"板块主力净流出 {board_net / 1e8:.1f} 亿")
    else:
        if m1_ret is not None and m1_ret < -0.10 and strategy not in strategies.OVERSOLD_FAMILY:
            reasons.append(f"近1月跌超10%（{m1_ret * 100:+.1f}%），1月行情过滤不追买")
        reasons.append("观望（条件未同时满足）")

    return {
        "code": code,
        "name": market.get_name(code),
        "strategy": strategy,
        "date": date,
        "close": close,
        "pct": stock_pct,
        "signal": sig,
        "signal_label": "买入" if sig == 1 else ("卖出" if sig == 0 else "观望"),
        "m1_ret": m1_ret,
        "m1_up_days": m1_up,
        "m1_max_dd": m1_dd,
        "conc90": c90,
        "conc70": latest.get("concentration70"),
        "avg_cost": latest.get("avg_cost"),
        "board": board,
        "board_bk": board_bk,
        "board_net": board_net,
        "board_pct": board_pct_today,
        "reasons": reasons,
    }


def evaluate_many(codes: list[str], params: dict | None = None,
                  strategy: str = "breakout_surge") -> list[dict]:
    out = []
    for code in codes:
        try:
            out.append(evaluate(code, params, strategy=strategy))
        except Exception as exc:  # noqa: BLE001
            out.append({"code": code, "error": str(exc)})
    return out

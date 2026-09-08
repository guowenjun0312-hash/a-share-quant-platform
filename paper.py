"""模拟盘（纸面交易）：用真实行情 + 策略信号虚拟成交，不涉及真实资金。

状态持久化到 paper_state.json，服务重启后自动恢复。
规则：
- 从买入信号中按胜率/短期收益择优，最多同时持 max_positions 只，资金等分；
- 卖出信号出现且持仓 -> 按最新价全部卖出（另扣 0.05% 印花税）；
- 单笔从买入价回撤超过 stop_loss（默认10%）触发止损卖出；
- 每日/每次刷新记录净资产曲线。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "paper_state.json"
TRADE_CODES: list[str] = []
MAX_POSITIONS = 5
DEFAULT_STOP_LOSS = 0.10
FEE = 0.0010
STAMP = 0.0005
ACCOUNTS = ("main", "small", "research")  # main=100万主账户；small=8000小账户；research=投研团队7000账户

# 由 server.paper_auto_tick 在每次撮合前注入的个股最优策略映射
# code -> {win, r1y, rec_win, rec_avg1, adj_win1, strategy}
_monitor_map_ref: dict = {}


def _default_state() -> dict:
    return {
        "running": False,
        "started": None,
        "capital": 100000.0,
        "cash": 100000.0,
        "strategy": "breakout_surge",
        "params": {},
        "codes": TRADE_CODES,
        "max_positions": MAX_POSITIONS,
        "stop_loss": DEFAULT_STOP_LOSS,
        "stop_overrides": {},  # 个股止损覆盖：code -> 止损价（优先于统一止损）
        "recent_sells": {},    # 卖出记录：code -> 卖出日期（冷却期内禁止买回）
        "sell_cooldown_days": 3,  # 卖出后冷却天数，防止“止损后又原价买回”的恶性循环
        "mode": "watchlist",   # watchlist=自选股；market=全市场（非ST、<50元）
        "force_codes": [],     # 强制持仓：这些代码优先买入并保留（不参与换仓卖出）
        "min_hold_days": 0,    # 最短持有天数（0=不限）：短线进攻参数
        "rotation_gap": 5.0,   # 换仓门槛：最优候选比最差持仓高出的最小综合分差
        "mirror_until": None,  # 镜像期截止日（YYYY-MM-DD）：此前只刷新价格不交易
        "auto_buy": True,      # 自动买入：False 时只自动卖出/止损，买入全部手动
        "positions": {},   # code -> {shares, entry_px, entry_date, name}
        "trades": [],      # {date, code, name, side, price, shares, amount, ret}
        "equity": [],      # {time, equity, cash, pos_value}
        "last_tick": None,
        "selection": None,  # 最近一次择优入选记录
    }


_lock = threading.Lock()


def _state_path(account: str = "main") -> Path:
    if account == "main":
        return STATE_FILE
    return ROOT / f"paper_state_{account}.json"


def load_state(account: str = "main") -> dict:
    path = _state_path(account)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            base = _default_state()
            base.update({k: v for k, v in data.items() if k in base})
            return base
        except Exception:  # noqa: BLE001
            pass
    return _default_state()


def save_state(state: dict, account: str = "main") -> None:
    try:
        _state_path(account).write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def get_state(account: str = "main") -> dict:
    with _lock:
        state = load_state(account)
        return _public_state(state, account)


def update_prices(quotes: dict, account: str = "main") -> dict:
    """仅更新持仓最新价（不触发买卖），供高频估值使用。"""
    with _lock:
        state = load_state(account)
        pos = state.get("positions") or {}
        for code, p in pos.items():
            q = quotes.get(code) or {}
            px = q.get("price")
            if px and px > 0:
                p["last_px"] = px
                if px > (p.get("peak_px") or p.get("entry_px") or 0):
                    p["peak_px"] = px  # 移动止损峰值跟踪
        save_state(state, account)
        return _public_state(state, account)


def _public_state(state: dict, account: str = "main") -> dict:
    pos_value = sum(
        p["shares"] * p.get("last_px", p["entry_px"]) for p in state["positions"].values()
    )
    equity = state["cash"] + pos_value
    returns = equity / state["capital"] - 1.0 if state["capital"] > 0 else 0.0
    wins = [t for t in state["trades"] if t.get("ret") is not None and t["ret"] > 0]
    sells = [t for t in state["trades"] if t["side"] == "sell"]
    closed = _pair_trades(state["trades"])
    return {
        **state,
        "equity_now": equity,
        "pos_value": pos_value,
        "return_total": returns,
        "trades_count": len(state["trades"]),
        "win_rate": len(wins) / len(sells) if sells else None,
        "closed_trades": closed,
    }


def _pair_trades(trades: list[dict]) -> list[dict]:
    """买卖配对：同一只股票的买入按时间先进先出，配对其后发生的卖出。
    返回 [{code,name,strategy,buy_date,buy_px,sell_date,sell_px,shares,
           ret,hold_days,reason}]，按卖出时间倒序。"""
    from collections import deque

    open_buys: dict[str, deque] = {}
    closed: list[dict] = []
    for t in trades:
        code = t.get("code")
        if t.get("side") == "buy":
            open_buys.setdefault(code, deque()).append(t)
        elif t.get("side") == "sell" and code in open_buys and open_buys[code]:
            buy = open_buys[code].popleft()
            buy_px = buy.get("price") or 0
            sell_px = t.get("price") or 0
            shares = t.get("shares") or buy.get("shares") or 0
            ret = t.get("ret")  # 与交易记录同口径：扣卖出费用后的净收益
            if ret is None:
                ret = (sell_px / buy_px - 1.0) if buy_px else None
            hold = 0
            try:
                from datetime import datetime
                bd = datetime.strptime(str(buy.get("date")), "%Y-%m-%d")
                sd = datetime.strptime(str(t.get("date")), "%Y-%m-%d")
                hold = max((sd - bd).days, 1)
            except Exception:  # noqa: BLE001
                pass
            closed.append({
                "code": code,
                "name": t.get("name") or buy.get("name") or code,
                "strategy": t.get("strategy") or buy.get("strategy"),
                "buy_date": buy.get("date"),
                "buy_px": buy_px,
                "sell_date": t.get("date"),
                "sell_px": sell_px,
                "shares": shares,
                "ret": ret,
                "hold_days": hold,
                "reason": t.get("reason") or "卖出信号",
            })
    closed.sort(key=lambda x: x.get("sell_date") or "", reverse=True)
    return closed


def start(capital: float, strategy: str, params: dict, codes: list[str],
          max_positions: int = MAX_POSITIONS,
          stop_loss: float = DEFAULT_STOP_LOSS, mode: str = "watchlist",
          force_codes: list[str] | None = None, reset: bool = True,
          account: str = "main", min_hold_days: int = 0,
          auto_buy: bool | None = None) -> dict:
    with _lock:
        state = load_state(account)
        state.update(
            {
                "running": True,
                "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "strategy": strategy,
                "params": params or {},
                "codes": codes or TRADE_CODES,
                "max_positions": int(max_positions),
                "stop_loss": float(stop_loss),
                "mode": mode or "watchlist",
                "min_hold_days": int(min_hold_days),
                "auto_buy": auto_buy if auto_buy is not None else state.get("auto_buy", True),
                "last_tick": None,
            }
        )
        if force_codes is not None:
            state["force_codes"] = list(force_codes)
        if reset:
            state["capital"] = float(capital)
            state["cash"] = float(capital)
            state["positions"] = {}
            state["trades"] = []
            state["selection"] = None
            state["equity"] = [
                {
                    "time": state["started"],
                    "equity": state["capital"],
                    "cash": state["capital"],
                    "pos_value": 0.0,
                }
            ]
        save_state(state, account)
        return _public_state(state, account)


def stop(account: str = "main") -> dict:
    with _lock:
        state = load_state(account)
        state["running"] = False
        save_state(state, account)
        return _public_state(state, account)


def reset(account: str = "main") -> dict:
    with _lock:
        state = _default_state()
        save_state(state, account)
        return _public_state(state, account)


def manual_order(account: str, code: str, action: str, shares: float,
                 price: float, name: str = "") -> dict:
    """手动调仓（与实盘同步用）：按最新价成交，含手续费，记录到 trades。"""
    with _lock:
        state = load_state(account)
        if not state["running"]:
            raise ValueError("账户未运行")
        if not price or price <= 0:
            raise ValueError("最新价无效")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = now[:10]
        if action == "sell":
            pos = state["positions"].get(code)
            if not pos:
                raise ValueError(f"未持有 {code}")
            sell_shares = pos["shares"] if (shares is None or shares <= 0) else float(shares)
            if sell_shares > pos["shares"] + 1e-9:
                raise ValueError("卖出数量超过持仓")
            exit_px = price * (1 - FEE - STAMP)
            amount = sell_shares * exit_px
            ret = exit_px / pos["entry_px"] - 1.0
            state["cash"] += amount
            pos["shares"] = round(pos["shares"] - sell_shares, 4)
            state["trades"].append(
                {"date": today, "code": code, "name": pos["name"], "side": "sell",
                 "price": price, "shares": round(sell_shares, 4), "amount": amount,
                 "ret": ret, "strategy": pos.get("strategy"), "reason": "手动调仓"}
            )
            if pos["shares"] <= 0.001:
                del state["positions"][code]
                fc = [c for c in (state.get("force_codes") or []) if c != code]
                state["force_codes"] = fc
        elif action == "buy":
            if code in state["positions"]:
                raise ValueError(f"已持有 {code}，请先卖出")
            shares = float(shares)
            if shares < 100:
                raise ValueError("A股买入至少 100 股（一手）")
            cost = shares * price * (1 + FEE)
            if cost > state["cash"] + 1e-6:
                raise ValueError(f"现金不足：需要 {cost:.2f}，可用 {state['cash']:.2f}")
            state["cash"] -= cost
            state["positions"][code] = {
                "shares": shares, "entry_px": price, "entry_date": today,
                "name": name or code, "strategy": "手动", "force": False,
                "last_px": price, "peak_px": price, "rec_win": None, "rec_avg1": None,
            }
            state["trades"].append(
                {"date": today, "code": code, "name": name or code, "side": "buy",
                 "price": price, "shares": shares, "amount": cost,
                 "ret": None, "strategy": "手动", "reason": "手动调仓"}
            )
        else:
            raise ValueError("action 必须为 buy 或 sell")
        save_state(state, account)
        return _public_state(state, account)


def _combo_score(win_rate, r1y, pnl_pct=None, hold_days=0, entry_date=None,
                 rec_win=None, rec_avg1=None):
    """综合评分：近1月胜率（优先）→ 近1月笔均收益 → 近1年收益 + 当前盈亏 + 持仓天数微调。
    用于动态换仓：分数越高代表该标的综合越优。"""
    score = 0.0
    wr = rec_win if rec_win is not None else win_rate
    score += max(wr or 0.0, 0.0) * 50.0                # 近1月胜率 0-1 → 0-50 分
    if rec_avg1 is not None:
        score += min(max(rec_avg1, -0.2), 0.3) * 40.0  # 近1月笔均收益 → ±12 分
    score += min(max(r1y if r1y is not None else -9.0, -1.0), 3.0) * 4.0   # 近1年收益 → ±12 分
    if pnl_pct is not None:
        score += min(max(pnl_pct, -0.2), 0.2) * 30.0   # 当前盈亏 -20%~+20% → ±6 分
    if hold_days is not None:
        score -= min(hold_days, 10) * 0.3              # 持有越久略降分（避免长期不动）
    return round(score, 2)


def _limit_px(code: str, prev_close, name: str = ""):
    """按板块返回 (涨停价, 跌停价)；prev_close 缺失时返回 (None, None)。"""
    if not prev_close or prev_close <= 0:
        return None, None
    if name and "ST" in name.upper():
        r = 0.05
    elif code.startswith(("300", "301", "688")):
        r = 0.20
    elif code.startswith(("8", "4", "92")):
        r = 0.30
    else:
        r = 0.10
    return round(prev_close * (1 + r), 2), round(prev_close * (1 - r), 2)


def _prev_close(q: dict, info: dict) -> float | None:
    pc = q.get("prev_close")
    if pc and pc > 0:
        return float(pc)
    pct = q.get("pct") or info.get("pct")
    price = q.get("price") or info.get("close")
    if pct is not None and price:
        try:
            return float(price) / (1 + float(pct) / 100.0)
        except Exception:  # noqa: BLE001
            return None
    return None


def _record_sell(state: dict, code: str, date_str: str) -> None:
    """记录卖出（冷却期内禁止买回同一只，避免止损/卖出信号被同轮买回抵消）。"""
    rs = state.setdefault("recent_sells", {})
    rs[code] = date_str
    cd = int(state.get("sell_cooldown_days") or 3)
    try:
        cutoff = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=cd)).strftime("%Y-%m-%d")
        for c, d in list(rs.items()):
            if d < cutoff:
                rs.pop(c, None)
    except Exception:  # noqa: BLE001
        pass


def tick(signals: dict, quotes: dict, account: str = "main") -> dict:
    """signals: code -> {signal(1买/0卖), name}; quotes: code -> {price}。"""
    with _lock:
        state = load_state(account)
        if not state["running"]:
            return _public_state(state, account)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 镜像期：只刷新持仓价格，不做任何买卖（等待 mirror_until 当天开始交易）
        mirror_until = state.get("mirror_until")
        if mirror_until and now[:10] < str(mirror_until):
            for code, pos in (state.get("positions") or {}).items():
                q = quotes.get(code) or {}
                px = q.get("price")
                if px and px > 0:
                    pos["last_px"] = px
            pos_value = sum(
                p["shares"] * p.get("last_px", p["entry_px"])
                for p in state["positions"].values()
            )
            state["equity"].append(
                {"time": now, "equity": round(state["cash"] + pos_value, 2),
                 "cash": round(state["cash"], 2), "pos_value": round(pos_value, 2)}
            )
            if len(state["equity"]) > 2000:
                state["equity"] = state["equity"][-2000:]
            state["last_tick"] = now
            save_state(state, account)
            return _public_state(state, account)
        # ---- 卖出：持仓股出现对应策略卖出信号则清仓 ----
        for code in state["codes"]:
            info = signals.get(code) or {}
            sig = info.get("signal")
            q = quotes.get(code) or {}
            price = q.get("price") or info.get("close")
            if price is None or price <= 0:
                continue
            name = info.get("name") or q.get("name") or code
            pos = state["positions"].get(code)
            if pos is not None and price > 0:
                if price > (pos.get("peak_px") or pos.get("last_px") or pos.get("entry_px") or 0):
                    pos["peak_px"] = price
            if sig == 0 and pos is not None:
                # 最短持有期保护：未满 min_hold_days 不因卖出信号离场（止损除外）
                min_hold = int(state.get("min_hold_days") or 0)
                # 跌停卖不出：真实盘跌停价难以成交，跳过本次卖出
                _lu, ld = _limit_px(code, _prev_close(q, info), name)
                if ld is not None and price <= ld + 0.005:
                    pos["last_px"] = price
                    continue
                # A股 T+1：当天买入的持仓不可卖出
                if min_hold > 0 and pos.get("entry_date"):
                    try:
                        held = (datetime.strptime(now[:10], "%Y-%m-%d")
                                - datetime.strptime(pos["entry_date"], "%Y-%m-%d")).days
                        if held < max(min_hold, 1):
                            pos["last_px"] = price
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                elif pos.get("entry_date") == now[:10]:
                    # T+1：无论是否设置最短持有，当天买入不可卖
                    pos["last_px"] = price
                    continue
                # 卖出确认（回测胜出）：卖出信号需“次日/后续再次出现”才执行，
                # 避免单日假信号误杀；止损不在此列，仍无条件执行。
                # 投研评级"回避"的票：不确认，出现信号直接卖出。
                if info.get("rating") == "回避":
                    pos.pop("sell_pending_date", None)
                else:
                    pending_date = pos.get("sell_pending_date")
                    if pending_date == now[:10]:
                        pos["last_px"] = price
                        continue
                    if pending_date is None:
                        pos["sell_pending_date"] = now[:10]
                        pos["last_px"] = price
                        continue
                    pos.pop("sell_pending_date", None)
                exit_px = price * (1 - FEE - STAMP)
                amount = pos["shares"] * exit_px
                ret = exit_px / pos["entry_px"] - 1.0
                state["cash"] += amount
                state["trades"].append(
                    {"date": now[:10], "code": code, "name": pos["name"], "side": "sell",
                     "price": price, "shares": pos["shares"], "amount": amount, "ret": ret,
                     "strategy": pos.get("strategy"), "reason": "卖出信号"}
                )
                if pos.get("force"):
                    fc = [c for c in (state.get("force_codes") or []) if c != code]
                    state["force_codes"] = fc  # 卖出后解除强制，后续按最优策略
                del state["positions"][code]
                _record_sell(state, code, now[:10])
            elif pos is not None and state.get("stop_loss"):
                stop_px = (state.get("stop_overrides") or {}).get(code)
                if stop_px is None:
                    # 移动止损：从持仓最高价回撤 stop_loss 才离场
                    peak = pos.get("peak_px") or pos.get("last_px") or pos["entry_px"]
                    stop_px = peak * (1 - state["stop_loss"])
                if price <= stop_px:
                    _lu, ld = _limit_px(code, _prev_close(q, info), name)
                    if ld is not None and price <= ld + 0.005:
                        pos["last_px"] = price  # 跌停止损也卖不出，继续持有
                        continue
                    exit_px = price * (1 - FEE - STAMP)
                    amount = pos["shares"] * exit_px
                    ret = exit_px / pos["entry_px"] - 1.0
                    state["cash"] += amount
                    state["trades"].append(
                        {"date": now[:10], "code": code, "name": pos["name"], "side": "sell",
                         "price": price, "shares": pos["shares"], "amount": amount, "ret": ret,
                         "strategy": pos.get("strategy"), "reason": "止损"}
                    )
                    if pos.get("force"):
                        fc = [c for c in (state.get("force_codes") or []) if c != code]
                        state["force_codes"] = fc
                    del state["positions"][code]
                    _record_sell(state, code, now[:10])
                else:
                    pos["last_px"] = price
                    pos.pop("sell_pending_date", None)  # 未破止损且无卖出信号：取消待确认
            elif pos is not None and price > 0:
                pos["last_px"] = price
                pos.pop("sell_pending_date", None)  # 信号消失（非卖出）则取消待确认

        # ---- 买入候选：从买入信号中按胜率/短期收益择优 ----
        buy_candidates = []
        for code in state["codes"]:
            if code in state["positions"]:
                continue
            info = signals.get(code) or {}
            if info.get("signal") != 1:
                continue
            if code.startswith(("300", "301")):
                continue  # 按用户偏好排除创业板
            if info.get("rating") == "回避":
                continue  # 投研评级回避：不进入买入候选
            rs = state.get("recent_sells") or {}
            if code in rs:
                cd = int(state.get("sell_cooldown_days") or 3)
                try:
                    sold_d = datetime.strptime(rs[code], "%Y-%m-%d")
                    if (datetime.strptime(now[:10], "%Y-%m-%d") - sold_d).days < cd:
                        continue  # 卖出冷却期内不买回同一只
                except Exception:  # noqa: BLE001
                    pass
            q = quotes.get(code) or {}
            price = q.get("price") or info.get("close")
            if price is None or price <= 0:
                continue
            lu, _ld = _limit_px(code, _prev_close(q, info),
                                info.get("name") or q.get("name") or code)
            if lu is not None and price >= lu - 0.005:
                continue  # 涨停封板买不进，跳过该候选
            buy_candidates.append(
                {
                    "code": code,
                    "name": info.get("name") or q.get("name") or code,
                    "price": price,
                    "strategy": info.get("strategy"),
                    "win_rate": info.get("win_rate"),
                    "r1y": info.get("r1y"),
                    "rec_win": info.get("rec_win"),      # 近1月胜率（优先）
                    "rec_avg1": info.get("rec_avg1"),    # 近1月笔均收益
                    "adj_win": info.get("adj_win"),      # 收缩后胜率（小样本修正）
                    "mon_score": info.get("mon_score"),  # 多周期综合分（优先）
                    "pct": info.get("pct"),              # 当日涨幅（趋势强度）
                    "amount": (q.get("amount") or info.get("amount") or 0),  # 成交额（活跃度）
                }
            )
        # 择优：多周期综合分 → 近1月收缩胜率 → 笔均收益 → 当日趋势 → 成交活跃度 → 近1年收益
        import math

        buy_candidates.sort(
            key=lambda x: (
                -(x["mon_score"] if x["mon_score"] is not None else -9.0),
                -((x["adj_win"] if x["adj_win"] is not None else
                   x["rec_win"] if x["rec_win"] is not None else 0.0)),
                -(x["rec_avg1"] if x["rec_avg1"] is not None else 0.0),
                -(min(max(x["pct"] if x["pct"] is not None else 0.0, 0.0), 10.0) / 10.0),
                -(min(math.log10(max(float(x["amount"] or 0), 1e6)) / 7.0, 1.0)),
                -(x["r1y"] or -9.0),
                x["code"],
            )
        )

        # ---- 强制持仓：先确保 force_codes 已买入 ----
        force_codes = [c for c in (state.get("force_codes") or []) if c not in state["positions"]]
        for code in force_codes:
            info = signals.get(code) or {}
            q = quotes.get(code) or {}
            price = q.get("price") or info.get("close")
            if not price or price <= 0:
                continue
            if code.startswith(("300", "301")):
                continue  # 按用户偏好排除创业板
            lu, _ld = _limit_px(code, _prev_close(q, info),
                                info.get("name") or q.get("name") or code)
            if lu is not None and price >= lu - 0.005:
                continue  # 涨停封板买不进，强制持仓也等下次
            rs = state.get("recent_sells") or {}
            if code in rs:
                cd = int(state.get("sell_cooldown_days") or 3)
                try:
                    sold_d = datetime.strptime(rs[code], "%Y-%m-%d")
                    if (datetime.strptime(now[:10], "%Y-%m-%d") - sold_d).days < cd:
                        continue  # 卖出冷却期内不强制买回
                except Exception:  # noqa: BLE001
                    pass
            slots_now = state["max_positions"] - len(state["positions"])
            # 现金不足时，卖出评分最低的非强制持仓腾出资金
            if state["cash"] <= 0 and slots_now <= 0:
                candidates = [
                    (code2, p2) for code2, p2 in state["positions"].items()
                    if not p2.get("force")
                ]
                if not candidates:
                    break
                candidates.sort(key=lambda x: _combo_score(
                    x[1].get("win_rate"), x[1].get("r1y"),
                    rec_win=x[1].get("rec_win"), rec_avg1=x[1].get("rec_avg1")))
                sell_code, sell_pos = candidates[0]
                q2 = quotes.get(sell_code) or {}
                exit_px = (q2.get("price") or sell_pos.get("last_px") or sell_pos["entry_px"]) * (1 - FEE - STAMP)
                amount = sell_pos["shares"] * exit_px
                ret = exit_px / sell_pos["entry_px"] - 1.0
                state["cash"] += amount
                state["trades"].append(
                    {"date": now[:10], "code": sell_code, "name": sell_pos["name"], "side": "sell",
                     "price": exit_px / (1 - FEE - STAMP), "shares": sell_pos["shares"],
                     "amount": amount, "ret": ret,
                     "strategy": sell_pos.get("strategy"), "reason": "为强制持仓腾位"}
                )
                del state["positions"][sell_code]
                _record_sell(state, sell_code, now[:10])
            if state["cash"] <= 0:
                break
            alloc = state["cash"] / slots_now if slots_now > 0 else state["cash"]
            shares = alloc / (price * (1 + FEE))
            if shares < 100:
                continue
            cost = shares * price * (1 + FEE)
            state["cash"] -= cost
            state["positions"][code] = {
                "shares": shares, "entry_px": price, "entry_date": now[:10],
                "name": info.get("name") or q.get("name") or code,
                "strategy": info.get("strategy"), "force": True,
                "win_rate": info.get("win_rate"), "r1y": info.get("r1y"),
                "rec_win": info.get("rec_win"), "rec_avg1": info.get("rec_avg1"),
                "last_px": price, "peak_px": price,
            }
            state["trades"].append(
                {"date": now[:10], "code": code, "name": state["positions"][code]["name"],
                 "side": "buy", "price": price, "shares": shares, "amount": cost,
                 "ret": None, "strategy": info.get("strategy"), "reason": "强制持仓"}
            )

        # ---- 动态换仓：有持仓且有明显更优标的时，卖出综合最差的一只换入 ----
        rotation: dict | None = None
        if state.get("auto_buy", True) and state["positions"] and buy_candidates:
            max_pos = state["max_positions"]
            today = now[:10]
            # 持仓评分（只考虑今日之前买入的，遵守 T+1）
            scored_pos = []
            for code, pos in state["positions"].items():
                if pos.get("force") and code in (state.get("force_codes") or []):
                    continue  # 强制持仓不参与换仓卖出（以 force_codes 名单为准）
                if pos.get("entry_date") == today:
                    continue
                q = quotes.get(code) or {}
                cur = q.get("price") or pos.get("last_px") or pos["entry_px"]
                pnl = cur / pos["entry_px"] - 1.0 if pos["entry_px"] else 0.0
                try:
                    hd = (datetime.strptime(now[:10], "%Y-%m-%d")
                          - datetime.strptime(pos.get("entry_date") or today, "%Y-%m-%d")).days
                except ValueError:
                    hd = 0
                # 旧持仓缺评分字段时，用信号信息补齐
                info = signals.get(code) or {}
                mm = _monitor_map_ref.get(code) or {}
                pos.setdefault("win_rate", info.get("win_rate") or mm.get("win"))
                pos.setdefault("r1y", info.get("r1y") or mm.get("r1y"))
                pos.setdefault("rec_win", info.get("rec_win") or mm.get("rec_win"))
                pos.setdefault("rec_avg1", info.get("rec_avg1") or mm.get("rec_avg1"))
                ms = info.get("mon_score")
                if ms is not None:
                    sc = ms  # 优先用多周期综合分（与信号监控同口径）
                else:
                    sc = _combo_score(pos.get("win_rate"), pos.get("r1y"), pnl, hd,
                                      rec_win=pos.get("rec_win"), rec_avg1=pos.get("rec_avg1"))
                scored_pos.append((sc, code, pos))
            if len(scored_pos) < len(state["positions"]):
                # 有今日买入的持仓（T+1 限制不能卖），本次跳过换仓
                scored_pos = []
            # 候选评分
            scored_cand = [
                (c.get("mon_score") if c.get("mon_score") is not None
                 else _combo_score(c.get("win_rate"), c.get("r1y"),
                                   rec_win=c.get("rec_win"), rec_avg1=c.get("rec_avg1")), c)
                for c in buy_candidates
            ]
            if scored_pos and scored_cand:
                scored_pos.sort()
                scored_cand.sort(key=lambda x: x[0], reverse=True)
                worst_score, worst_code, worst_pos = scored_pos[0]
                best_score, best_cand = scored_cand[0]
                # 换仓门槛：最优候选比最差持仓高出 rotation_gap 分才换
                gap = float(state.get("rotation_gap") or 5.0)
                if best_score - worst_score >= gap and best_cand["code"] != worst_code:
                    q = quotes.get(worst_code) or {}
                    p_worst = q.get("price") or worst_pos.get("last_px") or worst_pos["entry_px"]
                    _lu3, ld3 = _limit_px(worst_code, _prev_close(q, {}), worst_pos.get("name") or worst_code)
                    if ld3 is not None and p_worst <= ld3 + 0.005:
                        rotation = {"time": now, "skip": "跌停卖不出"}
                    else:
                        exit_px = p_worst * (1 - FEE - STAMP)
                        amount = worst_pos["shares"] * exit_px
                        ret = exit_px / worst_pos["entry_px"] - 1.0
                        state["cash"] += amount
                        state["trades"].append(
                            {"date": now[:10], "code": worst_code, "name": worst_pos["name"], "side": "sell",
                             "price": exit_px / (1 - FEE - STAMP), "shares": worst_pos["shares"],
                             "amount": amount, "ret": ret,
                             "strategy": worst_pos.get("strategy"), "reason": "换仓卖出（综合较差）"}
                        )
                        del state["positions"][worst_code]
                        _record_sell(state, worst_code, now[:10])
                        rotation = {
                            "time": now,
                            "sell_code": worst_code,
                            "sell_name": worst_pos["name"],
                            "sell_score": worst_score,
                            "buy_code": best_cand["code"],
                            "buy_name": best_cand["name"],
                            "buy_score": best_score,
                        }

        # ---- 买入：补足最多 max_positions 只 ----
        slots = state["max_positions"] - len(state["positions"])
        unlimited = int(state.get("max_positions") or 0) >= 100
        if state.get("auto_buy", True):
            if unlimited:
                # 不限制持仓数量：按综合分顺序，现金够买 100 股就全仓买入最优可买标的
                selected = []
                for cand in buy_candidates:
                    if state["cash"] >= cand["price"] * 100 * (1 + FEE):
                        selected = [cand]
                        break
            else:
                selected = buy_candidates[: max(slots, 0)]
        else:
            selected = []
        state["selection"] = {
            "time": now,
            "buy_total": len(buy_candidates),
            "rotation": rotation,
            "codes": [
                {
                    "code": c["code"],
                    "name": c["name"],
                    "strategy": c["strategy"],
                    "win_rate": c["win_rate"],
                    "r1y": c["r1y"],
                    "rec_win": c.get("rec_win"),
                    "rec_avg1": c.get("rec_avg1"),
                }
                for c in selected
            ],
        }
        remaining = len(selected)
        for cand in selected:
            if remaining <= 0:
                break
            alloc = state["cash"] / remaining
            shares = alloc / (cand["price"] * (1 + FEE))
            if shares >= 100:
                cost = shares * cand["price"] * (1 + FEE)
                state["cash"] -= cost
                state["positions"][cand["code"]] = {
                    "shares": shares,
                    "entry_px": cand["price"],
                    "entry_date": now[:10],
                    "name": cand["name"],
                    "strategy": cand["strategy"],
                    "win_rate": cand.get("win_rate"),
                    "r1y": cand.get("r1y"),
                    "rec_win": cand.get("rec_win"),
                    "rec_avg1": cand.get("rec_avg1"),
                    "last_px": cand["price"], "peak_px": cand["price"],
                }
                state["trades"].append(
                    {
                        "date": now[:10], "code": cand["code"], "name": cand["name"],
                        "side": "buy", "price": cand["price"], "shares": shares,
                        "amount": cost, "ret": None, "strategy": cand["strategy"],
                    }
                )
            remaining -= 1
        pos_value = sum(p["shares"] * p.get("last_px", p["entry_px"]) for p in state["positions"].values())
        state["equity"].append(
            {"time": now, "equity": round(state["cash"] + pos_value, 2),
             "cash": round(state["cash"], 2), "pos_value": round(pos_value, 2)}
        )
        if len(state["equity"]) > 2000:
            state["equity"] = state["equity"][-2000:]
        state["last_tick"] = now
        save_state(state, account)
        return _public_state(state, account)

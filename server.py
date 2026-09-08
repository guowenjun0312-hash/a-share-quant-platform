#!/usr/bin/env python3
"""大A 量化交易窗口 —— 本地服务。

启动：
  python3 server.py            # 默认 http://127.0.0.1:8765
  python3 server.py --port 9000

接口：
  GET  /api/symbols            标的列表（来自 backtest/data）
  GET  /api/strategies         策略列表与参数 schema
  POST /api/backtest           执行回测
  GET  /api/quotes?codes=..    批量实时行情（东方财富）
  GET  /api/trend?code=..      分时走势
  GET  /api/notes              策略说明
  POST /api/notes              追加策略说明
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
NOTES_FILE = ROOT / "notes.md"
MONITOR_MAP_FILE = ROOT / "monitor_map.json"
SIGNAL_HISTORY_FILE = ROOT / "signal_history.json"
MONITOR_MAP_TTL = 6 * 3600
MONITOR_CANDIDATES = [
    "sma_cross", "ma_rule", "rsi_reversion", "momentum",
    "oversold_bounce", "breakout_surge", "ma_bull_surge", "strong_pullback",
    "rsi_high_win", "rsi_deep_surge", "dual_rsi", "next_day_bounce",
    "dual_rsi_88", "rsi_short_55",
    "boll_reversion", "macd_cross", "ma_bounce", "dual_confirm",
    "breakout_vol", "low_rsi_cross",
    "steady_trend", "dip_ma20", "rsi21_trend", "atr_filter",
]
MONITOR_DEFAULT_STRATEGY = "breakout_surge"
SIGNALS_LOCK = threading.Lock()
LAST_SIGNALS: dict = {"t": 0.0, "data": []}  # 最近一次成功扫描结果（忙时兜底）
MARKET_BOARD_CACHE: dict = {"t": 0.0, "data": None}  # 全市场信号榜缓存（120 秒）

sys.path.insert(0, str(ROOT))
from engine import FEE, STAMP, list_symbols, load_data, metrics_with_positions  # noqa: E402
from engine import run_backtest as engine_run_backtest  # noqa: E402
import market  # noqa: E402
import strategies  # noqa: E402
import paper  # noqa: E402


def _json_safe(obj):
    """把 NaN/Infinity 转成 null，保证输出是合法 JSON（浏览器可解析）。"""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def json_response(handler, obj, status=200):
    body = json.dumps(_json_safe(obj), ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):  # noqa: BLE001
        pass


def read_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        # 页面/脚本一律不缓存，保证快捷方式打开的一定是最新版
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        print(f"[{datetime.now():%H:%M:%S}] {self.address_string()} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.path = "/index.html"
            return super().do_GET()
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/api/symbols":
            return json_response(self, {"symbols": list_symbols()})
        if path == "/api/strategies":
            return json_response(self, strategies.strategy_meta())
        if path == "/api/health":
            return json_response(self, {
                "ok": True,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "version": "2026-08-12",
            })
        if path == "/api/notes":
            text = NOTES_FILE.read_text(encoding="utf-8") if NOTES_FILE.exists() else ""
            return json_response(self, {"notes": text})
        if path == "/api/quotes":
            secids = (query.get("secids") or [""])[0].split(",")
            secids = [s for s in secids if s]
            if secids:
                return json_response(self, {"quotes": market.get_quotes_secids(secids)})
            codes = (query.get("codes") or [""])[0].split(",")
            codes = [c for c in codes if c]
            return json_response(self, {"quotes": market.get_quotes(codes)})
        if path == "/api/trend":
            code = (query.get("code") or [""])[0]
            secid = query.get("secid") or [None]
            secid = secid[0] if secid and secid[0] else None
            try:
                return json_response(self, market.get_trend(code, secid=secid))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=502)
        if path == "/api/board":
            code = (query.get("code") or [""])[0]
            try:
                return json_response(self, market.get_board_context(code))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=502)
        if path == "/api/chips":
            code = (query.get("code") or [""])[0]
            lookback = int((query.get("lookback") or ["120"])[0])
            try:
                return json_response(self, market.get_chips(code, lookback=lookback))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=502)
        if path == "/api/kline":
            code = (query.get("code") or [""])[0]
            days = int((query.get("days") or ["250"])[0])
            try:
                rows = market.get_kline(code, lmt=days + 10)
                import indicators

                ind = indicators.compute_indicators(rows)
                return json_response(
                    self,
                    {
                        "code": code,
                        "name": market.get_name(code),
                        "rows": rows,
                        "indicators": ind,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=502)
        if path == "/api/boards":
            try:
                return json_response(self, {"boards": market.get_board_rank()})
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=502)
        if path == "/api/board_stocks":
            bk = (query.get("bk") or [""])[0]
            try:
                return json_response(self, {"stocks": market.get_board_stocks(bk)})
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=502)
        if path == "/api/board/detail":
            bk = (query.get("bk") or [""])[0]
            try:
                kline = market.get_board_kline(bk)
                fflow = market.get_board_fflow(bk)
                return json_response(self, {"bk": bk, "kline": kline, "fflow": fflow})
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=502)
        if path == "/api/screener":
            try:
                return json_response(self, run_screener(query))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=502)
        if path == "/api/data":
            return json_response(self, {"symbols": list_symbols_with_dates()})
        if path == "/api/monitor_map":
            return json_response(
                self,
                {"map": monitor_strategy_map(), "default": MONITOR_DEFAULT_STRATEGY},
            )
        if path == "/api/research":
            import research

            return json_response(
                self,
                {
                    "opinions": research.all_opinions(),
                    "summary": research.summary(),
                    "stats": research.verify_all(),
                    "ingest": research.last_ingest(),
                },
            )
        if path == "/api/tushare/status":
            import tushare_provider

            return json_response(self, tushare_provider.status())
        if path == "/api/stock_best":
            code = (query.get("code") or [""])[0]
            try:
                return json_response(self, stock_month_best(code))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/market_board":
            try:
                limit = int((query.get("limit") or ["80"])[0])
                return json_response(self, market_signal_board(limit=min(max(limit, 10), 200)))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=500)
        if path == "/api/paper":
            try:
                account = str((query.get("account") or ["main"])[0])
                paper_refresh_quotes(account)
            except Exception:  # noqa: BLE001
                pass
            account = str((query.get("account") or ["main"])[0])
            return json_response(self, paper.get_state(account))
        if path == "/api/backtest":
            return json_response(
                self, {"error": "回测请使用 POST 请求"}, status=405
            )
        return super().do_GET()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/backtest":
            try:
                body = read_body(self)
                return self.run_backtest(body)
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/notes":
            body = read_body(self)
            text = (body.get("text") or "").strip()
            if not text:
                return json_response(self, {"error": "说明不能为空"}, status=400)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(NOTES_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n## {stamp}\n\n{text}\n\n")
            return json_response(self, {"ok": True, "file": str(NOTES_FILE)})
        if path == "/api/signals":
            body = read_body(self)
            codes = body.get("codes") or []
            if isinstance(codes, str):
                codes = [c.strip() for c in codes.split(",") if c.strip()]
            selected = str(body.get("strategy") or "best_per_stock")
            import signals

            if not SIGNALS_LOCK.acquire(blocking=False):
                if time.time() - LAST_SIGNALS["t"] < 600:
                    return json_response(self, {"signals": LAST_SIGNALS["data"], "stale": True})
                return json_response(self, {"signals": [], "busy": True})
            try:
                cache_key = selected + "|" + ",".join(codes)

                def scan() -> list[dict]:
                    m = monitor_strategy_map()
                    from concurrent.futures import ThreadPoolExecutor

                    def work(code):
                        if selected == "best_per_stock":
                            strat = (m.get(code) or {}).get("strategy") or MONITOR_DEFAULT_STRATEGY
                        else:
                            strat = selected
                        try:
                            res = signals.evaluate(code, None, strategy=strat, source="tencent")
                            import research

                            res["rating"] = research.get_rating(code)
                            mm = m.get(code) or {}
                            if selected == "best_per_stock":
                                res["rec_win"] = mm.get("rec_win")
                                res["rec_trades"] = mm.get("rec_trades")
                                res["rec_avg1"] = mm.get("rec_avg1")
                                res["mon_score"] = mm.get("score")
                                res["win3"] = mm.get("win3")
                                res["win6"] = mm.get("win6")
                                res["winY"] = mm.get("winY")
                            else:
                                rec = (mm.get("rec") or {}).get(strat)
                                if rec:
                                    res["rec_win"] = rec[0]
                                    res["rec_trades"] = rec[1]
                                    if len(rec) > 2:
                                        res["rec_avg1"] = rec[2]
                                res["mon_score"] = None
                            res.update(prev_buy_info(code))
                            return res
                        except Exception as exc:  # noqa: BLE001
                            return {"code": code, "error": str(exc), **prev_buy_info(code)}

                    with ThreadPoolExecutor(max_workers=20) as ex:
                        results = list(ex.map(work, codes))
                    record_signal_history(results)
                    # 卖出区“此前有买入信号”的排最前；随后按综合分降序、近1月胜率、近1年收益
                    results.sort(
                        key=lambda s: (
                            -(1 if (s.get("signal") == 0 and s.get("prev_buy")) else 0),
                            -(s.get("mon_score") if s.get("mon_score") is not None else -9.0),
                            -(s.get("rec_win") if s.get("rec_win") is not None else -1.0),
                            -(s.get("r1y") or -9.0),
                            s.get("code") or "",
                        )
                    )
                    return results

                results = cached_signals(cache_key, scan)
                LAST_SIGNALS["t"] = time.time()
                LAST_SIGNALS["data"] = results
                return json_response(self, {"signals": results})
            finally:
                SIGNALS_LOCK.release()
        if path == "/api/market_signals":
            try:
                body = read_body(self)
                limit = int(body.get("limit") or 100)
                import signals as sigmod

                mkt = market_analysis_map()
                if not mkt:
                    return json_response(self, {"signals": [], "error": "全市场分析尚未完成"})
                # 只评估近1月胜率最高、且有足够样本的前 200 只，避免全量实时扫描
                items = [
                    v for v in mkt.values()
                    if not v.get("error") and v.get("m1_trades")
                    and v["m1_trades"] >= 1 and v.get("m1_win") is not None
                ]
                items.sort(
                    key=lambda v: (-(v.get("m1_win") or 0),
                                   -(v.get("m1_avg") or -9),
                                   -(v.get("m1_trades") or 0),
                                   v.get("code") or "")
                )
                items = items[:200]
                results = []

                def work(item):
                    code = str(item.get("code") or "")
                    strat = item.get("strategy")
                    if not code or not strat:
                        return None
                    try:
                        res = sigmod.evaluate(code, None, strategy=strat,
                                              source="local_first")
                        res["rec_win"] = item.get("m1_win")
                        res["rec_trades"] = item.get("m1_trades")
                        res["rec_avg1"] = item.get("m1_avg")
                        res["market"] = True
                        res["price"] = item.get("price")
                        res["industry"] = item.get("industry") or ""
                        return res
                    except Exception:  # noqa: BLE001
                        return None

                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=12) as ex:
                    for r in ex.map(work, items):
                        if r:
                            results.append(r)
                results.sort(
                    key=lambda s: (
                        -(1 if s.get("signal") == 0 and s.get("prev_buy") else 0),
                        -(s.get("rec_win") if s.get("rec_win") is not None else -1.0),
                        -(s.get("rec_avg1") if s.get("rec_avg1") is not None else -9.0),
                        s.get("code") or "",
                    )
                )
                buys = [s for s in results if s.get("signal") == 1]
                sells = [s for s in results if s.get("signal") == 0]
                return json_response(
                    self,
                    {
                        "total": len(results),
                        "buys": buys[:limit],
                        "sells": sells[:limit],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/backtest_all":
            body = read_body(self)
            return json_response(self, run_backtest_all(body))
        if path == "/api/data/refresh":
            body = read_body(self)
            code = str(body.get("code") or "")
            name = body.get("name")
            try:
                return json_response(self, market.refresh_data(code, name))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/paper/start":
            body = read_body(self)
            try:
                account = str(body.get("account") or "main")
                mode = str(body.get("mode") or "watchlist")
                codes = body.get("codes") or []
                force_codes = body.get("force_codes")
                reset = bool(body.get("reset", True))
                if mode == "market":
                    universe_file = ROOT / "market_universe.json"
                    try:
                        universe = json.loads(universe_file.read_text(encoding="utf-8"))
                        codes = [u["code"] for u in universe]
                    except Exception:  # noqa: BLE001
                        codes = []
                return json_response(
                    self,
                    paper.start(
                        float(body.get("capital") or 100000),
                        str(body.get("strategy") or "breakout_surge"),
                        body.get("params") or {},
                        codes or paper.TRADE_CODES,
                        int(body.get("max_positions") or paper.MAX_POSITIONS),
                        float(body.get("stop_loss") or paper.DEFAULT_STOP_LOSS),
                        mode,
                        force_codes=force_codes,
                        reset=reset,
                        account=account,
                        min_hold_days=int(body.get("min_hold_days") or 0),
                        auto_buy=body.get("auto_buy"),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/paper/stop":
            body = read_body(self)
            return json_response(self, paper.stop(str(body.get("account") or "main")))
        if path == "/api/paper/reset":
            body = read_body(self)
            return json_response(self, paper.reset(str(body.get("account") or "main")))
        if path == "/api/paper/tick":
            body = read_body(self)
            return json_response(self, paper_auto_tick(str(body.get("account") or "main")))
        if path == "/api/paper/manual":
            body = read_body(self)
            try:
                code = str(body.get("code") or "")
                action = str(body.get("action") or "")
                shares = float(body.get("shares") or 0)
                q = {}
                try:
                    qs = market.get_quotes([code], fast=True)
                    q = qs[0] if qs else {}
                except Exception:  # noqa: BLE001
                    q = market.get_quote(code, retries=2) or {}
                price = q.get("price")
                if not price:
                    return json_response(self, {"error": "取不到最新价"}, status=400)
                r = paper.manual_order(
                    str(body.get("account") or "main"), code, action,
                    shares, float(price), str(q.get("name") or ""),
                )
                return json_response(self, r)
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/research/import":
            body = read_body(self)
            import research

            try:
                return json_response(self, research.import_text(str(body.get("text") or "")))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/research/set":
            body = read_body(self)
            import research

            try:
                return json_response(
                    self,
                    research.set_opinion(
                        str(body.get("code") or ""),
                        str(body.get("rating") or "中性"),
                        str(body.get("levels") or ""),
                        str(body.get("reason") or ""),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/tushare/config":
            body = read_body(self)
            import tushare_provider

            try:
                return json_response(self, tushare_provider.save_token(str(body.get("token") or "")))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/tushare/sync":
            body = read_body(self)
            import tushare_provider

            try:
                days = min(max(int(body.get("days") or 150), 30), 400)
                return json_response(self, tushare_provider.start_sync(days))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        if path == "/api/tushare/daily":
            body = read_body(self)
            import tushare_provider

            try:
                day = (str(body.get("day") or "")).strip() or None
                r = tushare_provider.daily_update(day)
                if not r.get("skipped") and r.get("updated_files"):
                    try:
                        monitor_strategy_map(force=True)
                    except Exception:  # noqa: BLE001
                        pass
                return json_resp(self, r)
            except Exception as exc:  # noqa: BLE001
                return json_resp(self, {"error": str(exc)}, status=400)
        if path == "/api/bounce/run":
            body = read_body(self)
            try:
                return json_response(self, bounce_run(force=bool(body.get("force"))))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": str(exc)}, status=400)
        return json_response(self, {"error": "not found"}, status=404)

    def run_backtest(self, body: dict) -> None:
            code = str(body.get("code") or "")
            name = str(body.get("name") or code)
            if str(code).startswith("688"):
                return json_response(self, {"error": "科创板已排除，不在可选范围内"}, status=400)
            strat = str(body.get("strategy") or "buy_hold")
            params = _merged_params(strat, body.get("params"))
            stop_raw = body.get("stop")
            stop = float(stop_raw) if stop_raw not in (None, "", 0, "0") else None

            rows = load_data(code)
            signal = strategies.build_signal(strat, rows, params, None)
            result = engine_run_backtest(rows, signal, stop=stop)
            result["metrics"] = metrics_with_positions(
                result["equity"], result["trades"], engine_years(rows), result["positions"]
            )
            result["metrics"]["m1"] = window_stats(result["dates"], result["trades"], 21)
            result["metrics"]["m3"] = window_stats(result["dates"], result["trades"], 63)
            result["symbol"] = {"code": code, "name": name}
            result["strategy"] = strat
            mm = monitor_strategy_map().get(code) or {}
            result["best"] = {
                "strategy": mm.get("strategy"),
                "rec_win": mm.get("rec_win"),
                "rec_trades": mm.get("rec_trades"),
                "rec_avg1": mm.get("rec_avg1"),
                "score": mm.get("score"),
                "is_best": mm.get("strategy") == strat,
            }
            result["board"] = None
            result["assumptions"] = {
                "execute": "T+1 开盘价成交",
                "fee": FEE,
                "stamp": STAMP,
                "note": "短线超跌反弹：RSI超卖+跌破布林下轨+当日大跌买入，RSI回归卖出。",
            }
            return json_response(self, result)


def engine_years(rows) -> float:
    from engine import years_of

    return years_of(rows)


def window_stats(dates: list[str], trades: list[dict], days: int = 21) -> dict:
    """最近 days 个交易日内的交易统计：{win, trades, avg}。"""
    if not dates or not trades:
        return {"win": None, "trades": 0, "avg": None}
    start = dates[-days] if len(dates) >= days else dates[0]
    ws = [t for t in trades if t["entry"] >= start]
    if not ws:
        return {"win": None, "trades": 0, "avg": None}
    wins = sum(1 for t in ws if t["ret"] > 0)
    return {
        "win": wins / len(ws),
        "trades": len(ws),
        "avg": sum(t["ret"] for t in ws) / len(ws),
    }


def stock_month_best(code: str) -> dict:
    """个股研究：对该股回测全部候选策略，找出“近1月”表现最优的策略。
    排序：近1月胜率 → 近1月笔均 → 近1月笔数；同时给出近3月/近1年做参照。
    """
    rows = load_data(code)
    ranked = []
    for strat in MONITOR_CANDIDATES:
        meta = strategies.strategy_meta()[strat]
        params = {k: v["default"] for k, v in meta["params"].items()}
        try:
            signal = strategies.build_signal(strat, rows, params, None)
            res = engine_run_backtest(rows, signal)
        except Exception:  # noqa: BLE001
            continue
        ranked.append(
            {
                "strategy": strat,
                "m1": window_stats(res["dates"], res["trades"], 21),
                "m3": window_stats(res["dates"], res["trades"], 63),
                "mY": window_stats(res["dates"], res["trades"], 252),
            }
        )
    ranked.sort(
        key=lambda x: (
            -(x["m1"]["win"] if x["m1"]["win"] is not None else -1.0),
            -(x["m1"]["avg"] if x["m1"]["avg"] is not None else -9.0),
            -(x["m1"]["trades"] or 0),
            x["strategy"],
        )
    )
    return {"code": code, "name": market.get_name(code), "best": ranked[0] if ranked else None,
            "ranked": ranked}


def market_signal_board(limit: int = 80) -> dict:
    """全市场信号榜：1200 只主板标的按各自最优策略算本地信号，按近1月胜率/综合分排序。"""
    now = time.time()
    if MARKET_BOARD_CACHE["data"] and now - MARKET_BOARD_CACHE["t"] < 120:
        data = MARKET_BOARD_CACHE["data"]
    else:
        mm = monitor_strategy_map()
        name_map = {s["code"]: s["name"] for s in list_symbols()}
        rows = []
        for code, v in mm.items():
            strat = v.get("strategy")
            if not strat:
                continue
            try:
                rows_l = load_data(code)
                meta = strategies.strategy_meta()[strat]
                params = {k: x["default"] for k, x in meta["params"].items()}
                fn = strategies.build_signal(strat, rows_l, params, None)
                sig = fn(len(rows_l) - 1)
                rows.append({
                    "code": code, "name": name_map.get(code, code),
                    "strategy": strat, "label": meta["label"],
                    "signal": 1 if sig == 1 else (0 if sig == 0 else 2),  # 2=观望
                    "rec_win": v.get("rec_win"), "win3": v.get("win3"),
                    "score": v.get("score"),
                    "date": rows_l[-1]["date"],
                })
            except Exception:  # noqa: BLE001
                continue
        rows.sort(key=lambda x: (
            -(x["rec_win"] if x["rec_win"] is not None else -1.0),
            -(x["score"] if x["score"] is not None else -9.0),
            x["code"],
        ))
        data = rows
        MARKET_BOARD_CACHE["t"] = time.time()
        MARKET_BOARD_CACHE["data"] = data
    top = data[:limit]
    quotes = {}
    try:
        for q in market.get_quotes([r["code"] for r in top], fast=True):
            quotes[q["code"]] = q
    except Exception:  # noqa: BLE001
        pass
    for r in top:
        q = quotes.get(r["code"]) or {}
        r["price"] = q.get("price")
        r["pct"] = q.get("pct")
    buys = [r for r in top if r["signal"] == 1]
    sells = [r for r in top if r["signal"] == 0]
    return {"buys": buys, "sells": sells, "total": len(data),
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


BOUNCE_ACCOUNT = "bounce"
BOUNCE_LOTS = 100      # 每只买一手
BOUNCE_CAP = 7000.0    # 超跌精选账户初始资金（7000）
BOUNCE_HOLD_DAYS = 3   # 持有 3 个交易日后卖出
BOUNCE_MAX_PER_DAY = 10
BOUNCE_TP = 0.01    # 止盈 +1%（回测最优：胜率65.5%）
BOUNCE_HOLD_MIN = 3  # 最早时间离场窗口起点（附近离场）
BOUNCE_HOLD_MAX = 4  # 最迟持有天数
BOUNCE_SL = 0.008   # 小止损 -0.8%（锁回撤，平均亏损约-0.79%）


def _bounce_ensure():
    """确保超跌精选账户存在（7000 初始资金）。"""
    st = paper.get_state(BOUNCE_ACCOUNT)
    if not st.get("running"):
        paper.start(capital=BOUNCE_CAP, strategy="best_per_stock", params={},
                    codes=list_symbols_codes(), max_positions=999, stop_loss=0.10,
                    mode="watchlist", reset=True, account=BOUNCE_ACCOUNT,
                    min_hold_days=0, auto_buy=False)
    st = paper.load_state(BOUNCE_ACCOUNT)
    st.setdefault("bounce_last_day", None)
    paper.save_state(st, BOUNCE_ACCOUNT)


def list_symbols_codes():
    return [s["code"] for s in list_symbols()]


def _trading_days() -> list[str]:
    """本地交易日（升序，从有数据的 CSV 里取）。"""
    try:
        import csv
        from engine import DATA_DIR
        fs = sorted(DATA_DIR.glob("*_*.csv"))
        if not fs:
            return []
        with open(fs[len(fs) // 2], encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return sorted({r["date"] for r in rows if r.get("date")})
    except Exception:  # noqa: BLE001
        return []


def _bounce_candidates():
    """近1月高胜率分层规则（统一回测口径：次日开盘买 → 持3日收盘卖）。

    精选档 RSI7<25 + 收盘低于MA20≥5% + 当日跌≥2%（弱收盘+缩量确认）
      -> 近1月 18笔 / 3日胜率 88.9%；近3月 76笔 / 61.8%（信号少而精）
    主档   RSI7<30 + 收盘低于MA20≥5%（同确认条件）
      -> 近1月 91笔 / 3日胜率 75.8%；近3月 249笔 / 65.9%（保证交易量）
    买入顺序：先精选档，再主档；同时符合只计精选档。
    """
    days = _trading_days()
    if not days:
        return []
    last = days[-1]
    tier1, tier2 = [], []
    for sym in list_symbols():
        code = sym["code"]
        if code.startswith(("688", "300", "301")):
            continue
        try:
            rows = load_data(code)
        except Exception:  # noqa: BLE001
            continue
        if not rows or rows[-1]["date"] != last:
            continue
        closes = [r["close"] for r in rows]
        if len(closes) < 25:
            continue
        try:
            from engine import rsi_wilder
            r7 = rsi_wilder(closes, 7)
        except Exception:  # noqa: BLE001
            continue
        rsi = r7[-1]
        ma20v = sum(closes[-20:]) / 20.0
        r = rows[-1]
        if rsi is None or ma20v <= 0 or r["close"] > ma20v * 0.95:
            continue
        hi, lo = r["high"], r["low"]
        if hi > lo and (r["close"] - lo) / (hi - lo) > 0.30:
            continue  # 弱收盘：收在当日区间下沿 30% 以内
        v = [r2.get("volume") or 0 for r2 in rows]
        if len(v) >= 7 and sum(v[-7:-2]) > 0:
            shrink = v[-2] / (sum(v[-7:-2]) / 5.0)
        else:
            shrink = None
        if shrink is not None and shrink > 0.85:
            continue  # 缩量确认：当日量能 ≤ 前5日均量 85%
        prev_c = closes[-2] if len(closes) >= 2 and closes[-2] > 0 else 0
        pct = (r["close"] / prev_c - 1.0) * 100 if prev_c else 0.0
        ma20d = r["close"] / ma20v - 1.0
        item = {"code": code, "name": sym["name"], "price": r["close"],
                "date": last, "rsi": round(rsi, 1), "ma20d": ma20d,
                "pct": round(pct, 2), "weak": True, "tier": "t2"}
        if rsi < 25 and pct <= -2.0:
            item["tier"] = "t1"
            tier1.append(item)
        else:
            tier2.append(item)
    tier1.sort(key=lambda x: x["pct"])
    tier2.sort(key=lambda x: x["ma20d"])
    return tier1 + tier2


def bounce_run(force: bool = False) -> dict:
    """超跌精选账户每日撮合：买全部符合条件的（一手），持有3个交易日后卖。"""
    _bounce_ensure()
    days = _trading_days()
    if not days:
        return {"error": "无本地交易日"}
    today = days[-1]
    if not force and today < datetime.now().strftime("%Y-%m-%d"):
        # 数据还停在旧交易日（行情未更新/休市日），先等新数据，避免拿旧信号重复买入
        return {"waiting": f"最新数据日 {today} 未更新，等待行情刷新"}
    st = paper.load_state(BOUNCE_ACCOUNT)
    if not force and st.get("bounce_last_day") == today:
        return {"skipped": today, "positions": len(st.get("positions") or {})}
    # 1) 卖出：弹性“3天附近”离场（止盈/时间/小止损谁先到谁执行）
    idx = {d: i for i, d in enumerate(days)}
    sells = []
    for code in list(st.get("positions", {}).keys()):
        pos = st["positions"][code]
        try:
            held = idx.get(today, 0) - idx.get(pos["entry_date"], 0)
        except Exception:  # noqa: BLE001
            held = 99
        if held < 1:
            continue
        try:
            q = market.get_quote(code, retries=1) or {}
            px = q.get("price") or pos["last_px"]
            if not px or px <= 0:
                continue
            ret = px / pos["entry_px"] - 1.0 if pos["entry_px"] else 0
            sell = False
            if held >= 2 and ret >= BOUNCE_TP:
                sell = True
            elif held >= BOUNCE_HOLD_MAX:
                sell = True
            elif held >= BOUNCE_HOLD_MIN and ret <= -BOUNCE_SL:
                sell = True
            elif held >= BOUNCE_HOLD_MIN and ret >= 0.005:
                sell = True  # 3天后有赚就落袋，不必等第4天
            if sell:
                paper.manual_order(BOUNCE_ACCOUNT, code, "sell", pos["shares"], px,
                                   pos["name"])
                sells.append(code)
        except Exception:  # noqa: BLE001
            pass
    # 2) 买入：全部符合条件的买一手
    buys = []
    cands = _bounce_candidates()
    for c in cands[:BOUNCE_MAX_PER_DAY]:
        code = c["code"]
        st = paper.load_state(BOUNCE_ACCOUNT)
        if code in st.get("positions", {}):
            continue
        cash = st.get("cash") or 0
        cost = BOUNCE_LOTS * c["price"] * (1 + paper.FEE)
        if cost > cash:
            break
        try:
            paper.manual_order(BOUNCE_ACCOUNT, code, "buy", BOUNCE_LOTS, c["price"],
                               c["name"])
            st2 = paper.load_state(BOUNCE_ACCOUNT)
            if code in st2.get("positions", {}):
                st2["positions"][code]["entry_date"] = c["date"]  # 以数据交易日为准
                paper.save_state(st2, BOUNCE_ACCOUNT)
            buys.append(code)
        except Exception:  # noqa: BLE001
            continue
    st = paper.load_state(BOUNCE_ACCOUNT)
    st["bounce_last_day"] = today
    paper.save_state(st, BOUNCE_ACCOUNT)
    return {"day": today, "candidates": len(cands), "buys": buys, "sells": sells,
            "positions": len(st.get("positions") or {}),
            "cash": round(st.get("cash") or 0, 2)}


def _q(query, key, default=None):
    vals = query.get(key) or []
    return vals[0] if vals else default


def _merged_params(strat: str, params: dict | None) -> dict:
    """合并策略默认参数：前端漏传的参数一律用默认值补全，避免回测/信号直接报错。"""
    meta = strategies.strategy_meta().get(strat) or {}
    defaults = {k: v["default"] for k, v in (meta.get("params") or {}).items()}
    out = dict(defaults)
    out.update(params or {})
    return out


MARKET_ANALYSIS_FILE = ROOT / "market_analysis.json"
MARKET_ANALYSIS_TTL = 12 * 3600


def market_analysis_map(force: bool = False) -> dict:
    """全市场分析结果（每只股票近1月最优策略与胜率），12 小时缓存。"""
    if MARKET_ANALYSIS_FILE.exists() and not force:
        age = time.time() - MARKET_ANALYSIS_FILE.stat().st_mtime
        if age < MARKET_ANALYSIS_TTL:
            try:
                return json.loads(MARKET_ANALYSIS_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    return {}


SIGNAL_CACHE: dict[str, tuple[float, list[dict]]] = {}
SIGNAL_CACHE_TTL = 45  # 45 秒内相同标的+策略直接返回上次结果，避免重复全量扫描
SIGNAL_CACHE_LOCK = threading.Lock()


def cached_signals(codes_key: str, scan_fn) -> list[dict]:
    """带缓存的信号扫描：并发请求时合并到同一次扫描，45 秒内重复请求秒回。"""
    now = time.time()
    with SIGNAL_CACHE_LOCK:
        hit = SIGNAL_CACHE.get(codes_key)
        if hit and now - hit[0] < SIGNAL_CACHE_TTL:
            return hit[1]
    results = scan_fn()
    with SIGNAL_CACHE_LOCK:
        SIGNAL_CACHE[codes_key] = (time.time(), results)
        if len(SIGNAL_CACHE) > 32:
            for k in list(SIGNAL_CACHE.keys())[:16]:
                SIGNAL_CACHE.pop(k, None)
    return results


def load_signal_history() -> dict:
    """读取信号历史：{code: [{date, signal}]}，只保留最近 90 天。"""
    if SIGNAL_HISTORY_FILE.exists():
        try:
            data = json.loads(SIGNAL_HISTORY_FILE.read_text(encoding="utf-8"))
            cutoff = (datetime.now().date().toordinal() - 90)  # 近 90 天
            for code, entries in list(data.items()):
                fresh = []
                for e in entries:
                    try:
                        d = datetime.strptime(str(e.get("date") or ""), "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if d.toordinal() >= cutoff:
                        fresh.append({"date": e["date"], "signal": e.get("signal")})
                fresh.sort(key=lambda x: x["date"])
                if fresh:
                    data[code] = fresh
                else:
                    data.pop(code, None)
            return data
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_signal_history(history: dict) -> None:
    try:
        SIGNAL_HISTORY_FILE.write_text(
            json.dumps(history, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


def record_signal_history(results: list[dict]) -> None:
    """把每次扫描触发的买入/卖出记入历史，供“此前买入 → 现在卖出”重点标注。"""
    history = load_signal_history()
    today = datetime.now().strftime("%Y-%m-%d")
    changed = False
    for r in results:
        code = str(r.get("code") or "")
        sig = r.get("signal")
        if sig not in (1, 0):
            continue
        entries = history.get(code) or []
        latest = entries[-1] if entries else None
        if sig == 1:
            # 买入：最新一条不是买入（或日期不同）才记录，避免重复
            if not (latest and latest.get("signal") == 1 and latest.get("date") == today):
                entries.append({"date": today, "signal": 1})
                changed = True
        else:
            # 卖出：记录卖出动作，同时清掉该标的此前买入标记（避免重复提示）
            if not (latest and latest.get("signal") == 0 and latest.get("date") == today):
                entries.append({"date": today, "signal": 0})
                changed = True
        if len(entries) > 60:
            entries = entries[-60:]
        history[code] = entries
    if changed:
        save_signal_history(history)


def prev_buy_info(code: str, history: dict | None = None) -> dict:
    """判断该标的是否“此前有买入信号且尚未出现卖出信号”，供卖出重点标注。"""
    hist = history if history is not None else load_signal_history()
    entries = hist.get(str(code)) or []
    for e in reversed(entries):
        if e.get("signal") == 1:
            return {"prev_buy": True, "prev_buy_date": e.get("date")}
        if e.get("signal") == 0:
            return {"prev_buy": False, "prev_buy_date": None}
    # 模拟盘持仓兜底：实际买入过的股票出现卖出信号同样重点提示
    try:
        paper_pos = paper.get_state().get("positions") or {}
        if str(code) in paper_pos:
            return {"prev_buy": True, "prev_buy_date": paper_pos[str(code)].get("entry_date")}
    except Exception:  # noqa: BLE001
        pass
    return {"prev_buy": False, "prev_buy_date": None}


def monitor_strategy_map(force: bool = False) -> dict:
    """个股最优监控策略：对每个本地标的回测全部候选策略，
    综合判断：近1月/近3月/近6月/近1年胜率加权（35/25/15/10）
    + 近1月/近3月笔均收益 + 成交量权重 + 多周期稳定性加成。
    结果缓存 6 小时。"""
    if MONITOR_MAP_FILE.exists() and not force:
        age = time.time() - MONITOR_MAP_FILE.stat().st_mtime
        if age < MONITOR_MAP_TTL:
            try:
                return json.loads(MONITOR_MAP_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    mapping = {}
    for sym in list_symbols():
        code = sym["code"]
        if code.startswith("688"):
            continue
        try:
            rows = load_data(code)
        except Exception:  # noqa: BLE001
            continue
        best = None
        rec_by_strat = {}
        m1_by_strat = {}
        for strat in MONITOR_CANDIDATES:
            meta = strategies.strategy_meta()[strat]
            params = {k: v["default"] for k, v in meta["params"].items()}
            signal = strategies.build_signal(strat, rows, params, None)
            res = engine_run_backtest(rows, signal)
            eq = res["equity"]
            r1 = eq[-1] / eq[-252] - 1.0 if len(eq) > 252 and eq[-252] > 0 else -9.0
            r6 = eq[-1] / eq[-126] - 1.0 if len(eq) > 126 and eq[-126] > 0 else -9.0
            m = res["metrics"]
            win = m["win_rate"] if m["win_rate"] == m["win_rate"] else -1.0
            # 多周期统计：近1月(21)/近2月(42)/近3月(63)/近6月(126)/近1年(252)
            N = [21, 42, 63, 126, 252]
            dates = res["dates"]
            starts = [dates[-n] if len(dates) >= n else dates[0] for n in N]
            cnt = [0, 0, 0, 0, 0]
            wcnt = [0, 0, 0, 0, 0]
            rets = [0.0, 0.0, 0.0, 0.0, 0.0]
            vr_list: list[float] = []
            # 日期 → 量比（当日成交量 / 5日均量），用于信号放量质量
            vols = [r.get("volume") or 0 for r in rows]
            vol5 = [None] * len(vols)
            for i in range(4, len(vols)):
                s = sum(vols[i - 4:i + 1])
                if s > 0:
                    vol5[i] = vols[i] / (s / 5.0)
            date_vr = {}
            for i, r in enumerate(rows):
                if vol5[i]:
                    date_vr[r["date"]] = vol5[i]
            for t in res["trades"]:
                vr = date_vr.get(t["entry"], 1.0)
                for k, start in enumerate(starts):
                    if t["entry"] >= start:
                        cnt[k] += 1
                        wcnt[k] += 1 if t["ret"] > 0 else 0
                        rets[k] += t["ret"]
                if t["entry"] >= starts[0]:
                    vr_list.append(vr)
            win1 = (wcnt[0] / cnt[0]) if cnt[0] else -1.0
            avg1 = (rets[0] / cnt[0]) if cnt[0] else -9.0
            win42 = (wcnt[1] / cnt[1]) if cnt[1] else -1.0
            avg42 = (rets[1] / cnt[1]) if cnt[1] else -9.0
            win3 = (wcnt[2] / cnt[2]) if cnt[2] else -1.0
            avg3 = (rets[2] / cnt[2]) if cnt[2] else -9.0
            win6 = (wcnt[3] / cnt[3]) if cnt[3] else -1.0
            winY = (wcnt[4] / cnt[4]) if cnt[4] else -1.0
            avg_vr = (sum(vr_list) / len(vr_list)) if vr_list else 1.0
            # 贝叶斯收缩：笔数越少，胜率越向 50% 靠拢，避免 1 笔样本决定排序
            adj = [
                (wcnt[k] + 1.0) / (cnt[k] + 2.0) if cnt[k] else None
                for k in range(5)
            ]
            # 综合分 = 多周期胜率加权(近2月为主)60 + 近1月/近2月笔均 + 量比 + 稳定性 - 回撤惩罚
            weights = [30.0, 25.0, 15.0, 12.0, 8.0]
            avail = [(adj[k], weights[k]) for k in range(5) if adj[k] is not None]
            wsum = sum(w for _, w in avail)
            win_part = (sum(v * w for v, w in avail) / wsum) * 60.0 if avail else 0.0
            ret_part = 0.0
            if cnt[0]:
                ret_part += min(max(avg1, 0.0), 0.30) * 30.0 * (cnt[0] / (cnt[0] + 3.0))
            if cnt[1]:
                ret_part += min(max(avg42, 0.0), 0.30) * 20.0 * (cnt[1] / (cnt[1] + 3.0))
            vol_part = min(max(avg_vr - 1.0, 0.0), 2.0) * 5.0
            # 稳定性：至少 3 个周期有样本且各周期收缩胜率都 ≥60%，加 5 分
            stab = 5.0 if len(avail) >= 3 and min(v for v, _ in avail) >= 0.60 else 0.0
            # 回撤惩罚：回撤越大扣分越多（每 50% 回撤最多扣 7.5 分），引导选低回撤策略
            dd_pen = min(max(res["metrics"]["max_drawdown"], 0.0), 0.5) * 15.0
            score = win_part + ret_part + vol_part + stab - dd_pen
            if m["trades"] < 2:
                score = -1.0  # 历史样本太少，几乎不入选
            cand = (score, adj[0] or -1.0, avg1 if cnt[0] else -9.0, cnt[0], m["trades"], strat)
            if best is None or cand[:4] > best[:4]:
                best = cand
            rec_by_strat[strat] = [(win1 if cnt[0] else None), cnt[0], (avg1 if cnt[0] else None)]
            m1_by_strat[strat] = {
                "win1": win1 if cnt[0] else None,
                "trades": cnt[0],
                "avg1": avg1 if cnt[0] else None,
                "win42": win42 if cnt[1] else None,
                "win3": win3 if cnt[2] else None,
                "win6": win6 if cnt[3] else None,
                "winY": winY if cnt[4] else None,
                "r1": r1, "r6": r6,
                "adj_win1": adj[0],
                "adj_win3": adj[2],
                "avg_vr": avg_vr if cnt[0] else None,
                "stability": min(v for v, _ in avail) if avail else None,
            }
        if best is not None:
            bwin, btrades, bavg1 = rec_by_strat.get(best[5], (None, 0, None))
            bm = m1_by_strat.get(best[5], {})
            b_adj = bm.get("adj_win1")
            b_vr = bm.get("avg_vr")
            mapping[code] = {
                "strategy": best[5],
                "score": best[0],          # 综合分（多周期加权）
                "r1y": bm.get("r1", -9.0),       # 近1年收益
                "r6m": bm.get("r6", -9.0),       # 近6月收益
                "total": best[2],          # 近1月笔均收益
                "win": best[3],            # 近1月胜率
                "trades": best[4],         # 总交易笔数
                "rec_win": bwin,           # 近1月胜率（展示用）
                "rec_trades": btrades,     # 近1月交易笔数
                "rec_avg1": bavg1,         # 近1月笔均收益
                "win3": bm.get("win3"),    # 近3月胜率
                "win6": bm.get("win6"),    # 近6月胜率
                "winY": bm.get("winY"),    # 近1年胜率
                "stability": bm.get("stability"),
                "adj_win1": b_adj,         # 收缩后近1月胜率
                "avg_vr": b_vr,            # 信号日平均量比（成交量权重）
                "rec": rec_by_strat,
                "m1": m1_by_strat,
            }
    try:
        MONITOR_MAP_FILE.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass
    return mapping


def run_screener(query) -> dict:
    """全市场快照选股：可叠加涨幅/换手/量比/成交额/主力净流入/跑赢板块条件。"""
    snap = market.get_all_stocks()
    stocks = snap["stocks"]
    board_pct = market.board_pct_map()

    min_pct = float(_q(query, "min_pct", -99) or -99)
    max_pct = float(_q(query, "max_pct", 99) or 99)
    min_turnover = float(_q(query, "min_turnover", 0) or 0)
    min_volratio = float(_q(query, "min_volratio", 0) or 0)
    min_amount = float(_q(query, "min_amount", 0) or 0) * 1e8
    min_net = float(_q(query, "min_net", 0) or 0) * 1e8
    beat_board = _q(query, "beat_board", "0") == "1"
    sort_by = _q(query, "sort", "pct")
    limit = int(_q(query, "limit", 50) or 50)

    out = []
    for s in stocks:
        pct = s["pct"]
        if pct is None or pct < min_pct or pct > max_pct:
            continue
        if (s["turnover"] or 0) < min_turnover:
            continue
        if (s["vol_ratio"] or 0) < min_volratio:
            continue
        if (s["amount"] or 0) < min_amount:
            continue
        if (s["main_net"] or 0) < min_net:
            continue
        bp = board_pct.get(s["industry"] or "")
        if beat_board and (bp is None or pct <= bp):
            continue
        s["board_pct"] = bp
        s["beat_board"] = bp is not None and pct > bp
        out.append(s)

    key_map = {
        "pct": "pct",
        "net": "main_net",
        "amount": "amount",
        "turnover": "turnover",
        "volratio": "vol_ratio",
        "cap": "float_cap",
    }
    key = key_map.get(sort_by, "pct")
    out.sort(key=lambda x: (x.get(key) is not None, -(x.get(key) or 0)))
    return {"total": len(out), "stocks": out[:limit]}


def list_symbols_with_dates() -> list[dict]:
    from engine import DATA_DIR
    from pathlib import Path

    rows_out = []
    for path in sorted(DATA_DIR.glob("*_*.csv")):
        stem = path.stem
        if "_" not in stem:
            continue
        code, name = stem.split("_", 1)
        lines = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("date"):
                    lines.append(line.split(",")[0])
        rows_out.append(
            {
                "code": code,
                "name": name,
                "file": path.name,
                "rows": len(lines),
                "first": lines[0] if lines else None,
                "last": lines[-1] if lines else None,
            }
        )
    return rows_out


def run_backtest_all(body: dict) -> dict:
    """组合回测：对所有本地标的运行同一策略，输出逐标的指标表 + 汇总。"""
    strat = str(body.get("strategy") or "oversold_bounce")
    params = _merged_params(strat, body.get("params"))
    stop_raw = body.get("stop")
    stop = float(stop_raw) if stop_raw not in (None, "", 0, "0") else None

    symbols = list_symbols()
    table = []
    for sym in symbols:
        code, name = sym["code"], sym["name"]
        if code.startswith("688"):
            continue
        try:
            rows = load_data(code)
            signal = strategies.build_signal(strat, rows, params, None)
            result = engine_run_backtest(rows, signal, stop=stop)
            m = metrics_with_positions(
                result["equity"], result["trades"], engine_years(rows), result["positions"]
            )
            m1 = window_stats(result["dates"], result["trades"], 21)
            m3 = window_stats(result["dates"], result["trades"], 63)
            table.append(
                {
                    "code": code,
                    "name": name,
                    "total_return": m["total_return"],
                    "cagr": m["cagr"],
                    "max_drawdown": m["max_drawdown"],
                    "sharpe": m["sharpe"],
                    "trades": m["trades"],
                    "win_rate": m["win_rate"],
                    "exposure": m["exposure"],
                    "m1": m1,
                    "m3": m3,
                    "board": None,
                }
            )
        except Exception as exc:  # noqa: BLE001
            table.append({"code": code, "name": name, "error": str(exc)})

    ok = [r for r in table if "error" not in r]
    summary = {}
    if ok:
        avg = lambda key: sum(r.get(key) or 0 for r in ok) / len(ok)  # noqa: E731
        summary = {
            "symbols": len(table),
            "ok": len(ok),
            "avg_return": avg("total_return"),
            "avg_drawdown": avg("max_drawdown"),
            "avg_sharpe": avg("sharpe"),
            "total_trades": sum(r.get("trades") or 0 for r in ok),
            "win_all": sum(1 for r in ok if (r.get("win_rate") or 0) == 1),
            "m1_avg_win": (
                sum(r["m1"]["win"] for r in ok if r.get("m1", {}).get("win") is not None)
                / sum(1 for r in ok if r.get("m1", {}).get("win") is not None)
                if any(r.get("m1", {}).get("win") is not None for r in ok) else None
            ),
            "m1_trades": sum(r.get("m1", {}).get("trades") or 0 for r in ok),
        }
    return {"table": table, "summary": summary, "strategy": strat}


def paper_auto_tick(account: str = "main") -> dict:
    """模拟盘撮合：拉取模拟盘标的的深度信号 + 实时行情，按信号成交。"""
    state = paper.get_state(account)
    if not state["running"]:
        return state
    codes = state["codes"]
    if not codes:
        return state
    import signals

    try:
        from concurrent.futures import ThreadPoolExecutor

        m = monitor_strategy_map() if state.get("mode") != "market" else {}
        mkt = market_analysis_map() if state.get("mode") == "market" else {}

        # 全市场模式：只评估“候选池+当前持仓”，候选=近1月胜率≥55%且≥2笔交易的标的
        work_codes = list(codes)
        mkt_info: dict[str, dict] = {}
        if state.get("mode") == "market":
            if not mkt:
                # 全市场分析未完成：只评估当前持仓，避免全量扫描
                work_codes = list(state["positions"].keys())
            else:
                work_codes = list(state["positions"].keys())
                cand = [
                    v for v in mkt.values()
                    if v.get("m1_trades") and v["m1_trades"] >= 1
                    and v.get("m1_win") is not None
                ]
                for v in cand:
                    t = v.get("m1_trades") or 0
                    w = v.get("m1_win") or 0
                    v["adj_win1"] = (w * t + 1.0) / (t + 2.0)
                cand.sort(
                    key=lambda v: (-(v.get("adj_win1") or 0), -(v.get("m1_avg") or -9), v.get("code") or "")
                )
                cand = cand[:200]
                for v in cand:
                    work_codes.append(v["code"])
                    mkt_info[v["code"]] = v
                work_codes = list(dict.fromkeys(work_codes))

        def work(code):
            if state.get("mode") == "market" and code in mkt_info:
                strat = mkt_info[code].get("strategy") or MONITOR_DEFAULT_STRATEGY
            elif state["strategy"] == "best_per_stock":
                strat = (m.get(code) or {}).get("strategy") or MONITOR_DEFAULT_STRATEGY
            else:
                strat = state["strategy"]
            try:
                res = signals.evaluate(code, state["params"], strategy=strat,
                                       source="local_first" if state.get("mode") == "market" else "tencent")
                if code in mkt_info:
                    res["rec_win"] = mkt_info[code].get("m1_win")
                    res["rec_trades"] = mkt_info[code].get("m1_trades")
                    res["rec_avg1"] = mkt_info[code].get("m1_avg")
                    res["adj_win"] = mkt_info[code].get("adj_win1")
                    res["win_rate"] = mkt_info[code].get("m1_win")
                    res["r1y"] = mkt_info[code].get("m1_avg")
                return res
            except Exception as exc:  # noqa: BLE001
                return {"code": code, "error": str(exc)}

        with ThreadPoolExecutor(max_workers=8) as ex:
            sigs = {s["code"]: s for s in ex.map(work, work_codes)}
        if state.get("mode") != "market":
            for code in sigs:
                mm = m.get(code) or {}
                sigs[code]["win_rate"] = mm.get("win")
                sigs[code]["r1y"] = mm.get("r1y")
                sigs[code]["rec_win"] = mm.get("rec_win")
                sigs[code]["rec_avg1"] = mm.get("rec_avg1")
                sigs[code]["adj_win"] = mm.get("adj_win1")
                sigs[code]["mon_score"] = mm.get("score")
                import research

                sigs[code]["rating"] = research.get_rating(code)
        # 只请求本次评估的标的行情（market 模式 200 只；自选股模式全部）
        quotes = {q["code"]: q for q in market.get_quotes(work_codes, fast=True)}
        # 注入个股最优策略映射，供 paper.tick 换仓评分兜底
        paper._monitor_map_ref = mkt if state.get("mode") == "market" else m
        return paper.tick(sigs, quotes, account=account)
    except Exception:  # noqa: BLE001
        return paper.get_state(account)


def paper_refresh_quotes(account: str = "main") -> dict:
    """高频估值：只刷新持仓股最新价（更新盈亏展示），不做买卖决策。"""
    try:
        state = paper.get_state(account)
        codes = list((state.get("positions") or {}).keys())
        if not codes:
            return state
        quotes = {q["code"]: q for q in market.get_quotes(codes, fast=True)}
        return paper.update_prices(quotes, account=account)
    except Exception:  # noqa: BLE001
        return paper.get_state(account)


def _paper_loop():
    while True:
        # 高频行情刷新（盈亏实时），低频撮合（买卖信号）
        for acc in paper.ACCOUNTS:
            try:
                paper_auto_tick(acc)
            except Exception:  # noqa: BLE001
                pass
        for _ in range(29):
            time.sleep(5)
            for acc in paper.ACCOUNTS:
                try:
                    paper_refresh_quotes(acc)
                except Exception:  # noqa: BLE001
                    pass


def _paper_autostart():
    """服务启动时确保模拟盘在运行：用保存的最优配置（100万/个股最优/8只/强制股）。"""
    def _watch_codes():
        try:
            import re as _re
            src = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
            m = _re.search(r'const WATCH_CODES = "([^"]*)"', src)
            if m:
                return m.group(1).split(",")
        except Exception:  # noqa: BLE001
            pass
        return paper.TRADE_CODES

    # 主账户：100万/个股最优/8只（模板默认无强制持仓，可自行填写）
    try:
        state = paper.get_state("main")
        if not state.get("running"):
            paper.start(
                capital=1000000.0, strategy="best_per_stock", params={},
                codes=_watch_codes(), max_positions=8, stop_loss=0.08,
                mode="watchlist", force_codes=[],
                reset=False, account="main",
            )
            print("[auto] 主账户已自动启动：100万/个股最优/8只")
    except Exception as exc:  # noqa: BLE001
        print(f"[auto] 主账户自动启动失败: {exc}")

    # 小账户：8000元，已有持仓/记录则只确保参数，否则空仓启动
    try:
        state = paper.get_state("small")
        if state.get("positions") or state.get("trades"):
            st = paper.load_state("small")
            st["min_hold_days"] = 1
            st["max_positions"] = 999  # 不限制持仓数量，追求利益最大化
            st["stop_loss"] = 0.08  # 移动止损 -8%（从持仓最高价回撤）
            st["strategy"] = "best_per_stock"
            st["rotation_gap"] = 5.0
            paper.save_state(st, "small")
            print("[auto] 小账户参数已确保")
        else:
            paper.start(
                capital=8000.0, strategy="best_per_stock", params={},
                codes=_watch_codes(), max_positions=999, stop_loss=0.10,
                mode="watchlist", reset=True, account="small",
                min_hold_days=1, auto_buy=True,
            )
            print("[auto] 小账户已启动：8000元")
    except Exception as exc:  # noqa: BLE001
        print(f"[auto] 小账户自动启动失败: {exc}")

    # 投研团队账户：7000元，手动操作（auto_buy=False），与主/小账户独立
    try:
        state = paper.get_state("research")
        if not state.get("running"):
            paper.start(
                capital=7000.0, strategy="best_per_stock", params={},
                codes=_watch_codes(), max_positions=999, stop_loss=0.10,
                mode="watchlist", reset=True, account="research",
                min_hold_days=1, auto_buy=False,
            )
            print("[auto] 投研团队账户已创建：7000元/手动操作")
        else:
            st = paper.load_state("research")
            st["auto_buy"] = False
            st["max_positions"] = 999
            paper.save_state(st, "research")
    except Exception as exc:  # noqa: BLE001
        print(f"[auto] 投研账户自动启动失败: {exc}")


def main() -> int:
    p = argparse.ArgumentParser(description="大A 量化交易窗口")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--share", action="store_true",
                   help="局域网分享模式：绑定 0.0.0.0，同一网络下他人可访问")
    args = p.parse_args()
    host = "0.0.0.0" if args.share else args.host

    server = ThreadingHTTPServer((host, args.port), Handler)
    # 投研意见自动接入：扫描“投研输入”文件夹，每分钟一次
    import research

    research.IN_DIR.mkdir(exist_ok=True)
    research.DONE_DIR.mkdir(exist_ok=True)

    def _research_loop():
        while True:
            try:
                research.ingest_folder()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(60)

    threading.Thread(target=_research_loop, daemon=True).start()
    _paper_autostart()
    threading.Thread(target=_paper_loop, daemon=True).start()
    # 每日 15:35 后自动日更（Tushare 单次调用），随后重算策略表
    def _daily_update_loop():
        import tushare_provider

        while True:
            try:
                r = tushare_provider.daily_update()
                if r.get("updated_files"):
                    print(f"[daily] {r.get('day')} 已更新 {r['updated_files']} 个文件，重算策略表…")
                    try:
                        monitor_strategy_map(force=True)
                        print("[daily] 策略表已重算")
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
            time.sleep(300)

    threading.Thread(target=_daily_update_loop, daemon=True).start()
    # 超跌精选每日撮合（交易日 15:35 后自动跑：买全部符合的，持有3个交易日卖）
    def _bounce_loop():
        while True:
            try:
                now = datetime.now()
                if now.weekday() < 5 and now.strftime("%H%M") >= "1535":
                    bounce_run()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(300)

    threading.Thread(target=_bounce_loop, daemon=True).start()
    try:
        _bounce_ensure()
    except Exception:  # noqa: BLE001
        pass
    url = f"http://{args.host}:{args.port}"
    if args.share:
        print(f"大A 量化交易窗口已启动（局域网分享模式）")
        print(f"  本机访问:   {url}")
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            print(f"  他人访问:   http://{ip}:{args.port}")
        except Exception:  # noqa: BLE001
            pass
    else:
        print(f"大A 量化交易窗口已启动: {url}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Tushare 全市场数据同步：按交易日批量拉全 A 日线，写入 backtest/data 供回测/信号使用。

- 股票范围：主板（沪 60x / 深 00x，排除创业 300/301、科创 688、北交所 8/4/92）非 ST；
- 拉取最近 N 个交易日（默认 150），每次调 daily + daily_basic；
- 进度与 token 保存在 tushare_config.json。
"""

from __future__ import annotations

import csv
import json
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "tushare_config.json"
DATES_FILE = ROOT / "tushare_dates.json"
CSV_HEADER = ["date", "open", "high", "low", "close", "volume", "pct", "amount", "turnover", "chg"]


def _load() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"token": None, "permissions": {}, "last_sync": None, "sync": {}}


def _save(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _pro():
    import tushare as ts

    cfg = _load()
    if not cfg.get("token"):
        raise ValueError("尚未配置 Tushare token")
    return ts.pro_api(cfg["token"])


def mask(token):
    return (token[:6] + "****" + token[-4:]) if token and len(token) > 10 else ""


def status() -> dict:
    cfg = _load()
    return {
        "configured": bool(cfg.get("token")),
        "token_masked": mask(cfg.get("token")),
        "permissions": cfg.get("permissions") or {},
        "last_sync": cfg.get("last_sync"),
        "last_daily": cfg.get("last_daily"),
        "sync": cfg.get("sync") or {},
    }


def save_token(token: str) -> dict:
    token = (token or "").strip()
    if len(token) < 20:
        raise ValueError("token 格式不正确")
    cfg = _load()
    cfg["token"] = token
    cfg["permissions"] = {}
    _save(cfg)
    return status()


def check_permissions() -> dict:
    cfg = _load()
    perms = {}
    if cfg.get("token"):
        try:
            pro = _pro()
            probes = {
                "stock_basic": lambda: pro.stock_basic(list_status="L", fields="ts_code,symbol,name"),
                "trade_cal": lambda: pro.trade_cal(exchange="SSE", start_date="20260901", end_date="20260904"),
                "daily": lambda: pro.daily(trade_date="20260904"),
                "daily_basic": lambda: pro.daily_basic(trade_date="20260904"),
            }
            for k, fn in probes.items():
                try:
                    df = fn()
                    perms[k] = {"ok": True, "rows": int(len(df))}
                except Exception as exc:  # noqa: BLE001
                    perms[k] = {"ok": False, "error": str(exc)[:80]}
        except Exception as exc:  # noqa: BLE001
            perms["_error"] = str(exc)[:120]
    cfg["permissions"] = perms
    cfg["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save(cfg)
    return perms


def _include(symbol: str, name: str) -> bool:
    if len(symbol) != 6:
        return False
    if symbol.startswith(("688", "300", "301", "4", "8", "92")):
        return False
    if "ST" in name.upper():
        return False
    return symbol.startswith(("60", "00"))


def _local_dates(days: int) -> list[str]:
    """用本地已有 CSV 的日期做交易日历（绕开 trade_cal 的 1 次/分钟限制）。"""
    from engine import DATA_DIR

    files = sorted(DATA_DIR.glob("*.csv"))
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            ds = [r["date"] for r in rows if r.get("date")]
            if len(ds) >= days + 5:
                return ds
        except Exception:  # noqa: BLE001
            continue
    return []


def _sync(days: int, progress: dict) -> None:
    from engine import DATA_DIR

    pro = _pro()

    def up(**kw):
        progress.update(kw)
        progress["updated"] = time.strftime("%H:%M:%S")
        cfg = _load()
        cfg["sync"] = dict(progress)
        _save(cfg)

    up(running=True, done=0, total=0, current="准备中", errors=0, message="")
    try:
        today = datetime.now().strftime("%Y%m%d")
        dates = _local_dates(days)  # 优先本地日历，避免 trade_cal 限流
        if DATES_FILE.exists():
            try:
                cache = json.loads(DATES_FILE.read_text(encoding="utf-8"))
                if cache.get("end") == today:
                    if len(dates) < days:
                        dates = cache["dates"]
            except Exception:  # noqa: BLE001
                pass
        if not dates:
            wait_n = 0
            while True:
                try:
                    cal = pro.trade_cal(exchange="SSE", is_open="1",
                                        start_date="20000101", end_date=today)
                    dates = sorted(cal["cal_date"].tolist())
                    DATES_FILE.write_text(json.dumps({"end": today, "dates": dates},
                                                     ensure_ascii=False), encoding="utf-8")
                    break
                except Exception:  # noqa: BLE001（120积分低频：每分钟自动重试，恢复即续跑）
                    wait_n += 1
                    up(current=f"等待日历接口限流恢复（已等 {wait_n} 分钟）…")
                    time.sleep(61)
        dates = dates[-days:]
        dates = [str(d).replace("-", "") for d in dates]  # Tushare 需要 YYYYMMDD
        up(total=len(dates), current="读取交易日历完成")

        # 股票名单：本地 market_analysis.json + 已有 CSV 文件名（不调 stock_basic，省额度）
        name_map: dict[str, str] = {}
        ma_file = ROOT / "market_analysis.json"
        if ma_file.exists():
            try:
                ma = json.loads(ma_file.read_text(encoding="utf-8"))
                for code, v in ma.items():
                    if _include(str(code), str(v.get("name") or "")):
                        name_map[str(code)] = str(v.get("name") or code)
            except Exception:  # noqa: BLE001
                pass
        from engine import DATA_DIR as _DD

        for f in _DD.glob("*_*.csv"):
            stem = f.stem
            if "_" in stem:
                code, name = stem.split("_", 1)
                if _include(code, name):
                    name_map[code] = name
        keep = name_map
        up(current=f"候选股票 {len(keep)} 只（本地名单，未调 stock_basic）")

        bars: dict[str, list[dict]] = {}
        turn: dict[str, dict] = {}
        for i, day in enumerate(dates, 1):
            up(done=i - 1, current=f"拉取 {day} ({i}/{len(dates)})")
            for attempt in range(6):
                try:
                    d = pro.daily(trade_date=day)
                    break
                except Exception as exc:  # noqa: BLE001
                    if "频率超限" in str(exc):
                        time.sleep(61)  # 120积分低频：限流时等满 1 分钟
                    else:
                        time.sleep(8)
                    d = None
            if d is not None and len(d):
                for _, r in d.iterrows():
                    sym = str(r["ts_code"]).split(".")[0]
                    if sym not in keep:
                        continue
                    td = str(r["trade_date"])
                    date_dash = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
                    bars.setdefault(sym, []).append({
                        "date": date_dash,
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r["vol"]) * 100.0,
                        "pct": float(r["pct_chg"]) if r["pct_chg"] == r["pct_chg"] else 0.0,
                        "amount": float(r["amount"]) * 1000.0,
                        "chg": float(r["change"]) if r["change"] == r["change"] else 0.0,
                    })
            time.sleep(0.5)
        up(done=len(dates), current="写入本地文件")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        written = skipped = errors = 0
        for sym, rows in bars.items():
            if len(rows) < 30:
                skipped += 1
                continue
            rows.sort(key=lambda x: x["date"])
            name = keep.get(sym, sym)
            safe = "".join(c for c in name if c not in '/\\:*?"<>|') or sym
            path = DATA_DIR / f"{sym}_{safe}.csv"
            try:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_HEADER)
                    w.writeheader()
                    for row in rows:
                        row["turnover"] = turn.get(sym, 0.0)
                        w.writerow({k: row.get(k, 0.0) for k in CSV_HEADER})
                written += 1
            except Exception:  # noqa: BLE001
                errors += 1
        cfg = _load()
        cfg["last_sync"] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "days": days, "start": dates[0], "end": dates[-1],
                            "stocks": written}
        cfg["sync"] = {"running": False, "done": len(dates), "total": len(dates),
                       "current": "同步完成", "message": f"写入 {written} 只，跳过 {skipped}，失败 {errors}",
                       "updated": time.strftime("%H:%M:%S")}
        _save(cfg)
    except Exception as exc:  # noqa: BLE001
        up(running=False, message=f"同步失败：{exc}")


def start_sync(days: int = 150) -> dict:
    cfg = _load()
    if not cfg.get("token"):
        raise ValueError("尚未配置 token")
    if (cfg.get("sync") or {}).get("running"):
        raise ValueError("已有同步任务在运行")
    progress: dict = {}
    threading.Thread(target=_sync, args=(days, progress), daemon=True).start()
    return {"started": True, "days": days}


def daily_update(day: str | None = None) -> dict:
    """每日增量：拉取指定/最新交易日全市场日线并更新本地 CSV（单次调用）。"""
    cfg = _load()
    if not cfg.get("token"):
        return {"error": "未配置 token"}
    if day is None:
        today = datetime.now()
        if today.weekday() >= 5:
            return {"skipped": "周末无行情"}
        if today.strftime("%H%M") < "1535":
            return {"skipped": "未到收盘更新时间(15:35)"}
        day = today.strftime("%Y%m%d")
        if cfg.get("last_daily") == day:
            return {"skipped": "今日已更新"}
    import tushare as ts
    from engine import DATA_DIR

    pro = _pro()
    d = None
    for _ in range(4):
        try:
            d = pro.daily(trade_date=day)
            if d is not None and len(d):
                break
        except Exception:  # noqa: BLE001
            time.sleep(61)
    if d is None or not len(d):
        return {"error": f"{day} 无数据或限流"}
    import csv

    # 同日换手率（daily_basic 单次调用，失败不阻断）
    turn: dict[str, float] = {}
    try:
        db = pro.daily_basic(trade_date=day, fields="ts_code,turnover_rate")
        if db is not None and len(db):
            for _, r in db.iterrows():
                sym = str(r["ts_code"]).split(".")[0]
                tr = r["turnover_rate"]
                turn[sym] = float(tr) if tr == tr else 0.0
    except Exception:  # noqa: BLE001
        pass

    updated = 0
    files = list(DATA_DIR.glob("*_*.csv"))
    by_code: dict[str, list[Path]] = {}
    for f in files:
        code = f.stem.split("_", 1)[0]
        by_code.setdefault(code, []).append(f)
    for _, r in d.iterrows():
        sym = str(r["ts_code"]).split(".")[0]
        fs = by_code.get(sym)
        if not fs:
            continue
        td = str(r["trade_date"])
        date_dash = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
        new_row = {
            "date": date_dash,
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["vol"]) * 100.0,
            "pct": float(r["pct_chg"]) if r["pct_chg"] == r["pct_chg"] else 0.0,
            "amount": float(r["amount"]) * 1000.0,
            "turnover": turn.get(sym, 0.0),
            "chg": float(r["change"]) if r["change"] == r["change"] else 0.0,
        }
        for f in fs:
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
                head, body = lines[0], [ln for ln in lines[1:] if not ln.startswith(date_dash + ",")]
                body.append(",".join(str(new_row[k]) for k in CSV_HEADER))
                body.sort()
                f.write_text(head + "\n" + "\n".join(body) + "\n", encoding="utf-8")
                updated += 1
            except Exception:  # noqa: BLE001
                continue
    cfg = _load()
    cfg["last_daily"] = str(day)
    _save(cfg)
    return {"day": day, "updated_files": updated, "rows": len(d)}


def backfill_turnover(days: int = 40) -> dict:
    """后台补历史换手率：每天一次 daily_basic（限流1次/分钟，约需 days 分钟）。"""
    cfg = _load()
    if not cfg.get("token"):
        return {"error": "未配置 token"}
    if (cfg.get("turnover_backfill") or {}).get("running"):
        return {"error": "已有补数任务在运行"}

    def run():
        import csv
        from engine import DATA_DIR

        pro = _pro()
        status = {"running": True, "done": 0, "total": days, "current": "准备中", "updated": ""}

        def up(**kw):
            status.update(kw)
            status["updated"] = time.strftime("%H:%M:%S")
            c = _load()
            c["turnover_backfill"] = dict(status)
            _save(c)

        up()
        try:
            # 本地交易日
            files = list(DATA_DIR.glob("*_*.csv"))
            ds = []
            if files:
                with open(files[len(files) // 2], encoding="utf-8") as f:
                    ds = sorted({r["date"] for r in csv.DictReader(f) if r.get("date")})
            ds = [d.replace("-", "") for d in ds][-days:]
            for i, day in enumerate(ds, 1):
                up(done=i - 1, current=f"补 {day} ({i}/{len(ds)})")
                for _ in range(3):
                    try:
                        db = pro.daily_basic(trade_date=day, fields="ts_code,turnover_rate")
                        break
                    except Exception:  # noqa: BLE001
                        time.sleep(61)
                        db = None
                if db is not None and len(db):
                    tr = {}
                    for _, r in db.iterrows():
                        s = str(r["ts_code"]).split(".")[0]
                        v = r["turnover_rate"]
                        tr[s] = float(v) if v == v else 0.0
                    dstr = f"{day[:4]}-{day[4:6]}-{day[6:]}"
                    for f in files:
                        code = f.stem.split("_", 1)[0]
                        val = tr.get(code)
                        if val is None:
                            continue
                        try:
                            lines = f.read_text(encoding="utf-8").splitlines()
                            head = lines[0]
                            idx = head.split(",").index("turnover")
                            out = [lines[0]]
                            for ln in lines[1:]:
                                cols = ln.split(",")
                                if cols[0] == dstr and len(cols) > idx:
                                    cols[idx] = str(val)
                                out.append(",".join(cols))
                            f.write_text("\n".join(out) + "\n", encoding="utf-8")
                        except Exception:  # noqa: BLE001
                            continue
                if i < len(ds):
                    time.sleep(65)  # 120积分限流：1次/分钟
            up(done=len(ds), current="完成", running=False,
               message=f"已补 {len(ds)} 天换手率")
        except Exception as exc:  # noqa: BLE001
            up(running=False, current="失败", message=str(exc)[:100])

    threading.Thread(target=run, daemon=True).start()
    return {"started": True, "days": days}

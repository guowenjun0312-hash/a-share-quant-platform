"""投研意见模块：分析部门全市场评级 → 决策联动。

评级：强烈看好 / 看好 / 中性 / 回避
决策联动（paper.tick）：
- 回避：不进入买入候选；出现卖出信号立即执行（不做 2 天确认）
- 其他：维持默认（卖出信号次日确认）

输入方式：分析部门发来的文字/表格粘贴进平台，自动解析 6 位代码、
评级、关键价位与理由；未覆盖的股票默认“中性”。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = ROOT / "research_opinions.json"
IN_DIR = ROOT / "投研输入"          # 分析部门把意见文件放这里，平台自动解析
DONE_DIR = IN_DIR / "已处理"        # 已解析文件归档
LAST_FILE = ROOT / "research_ingest.json"
RATINGS = ["强烈看好", "看好", "中性", "回避"]
_VERIFY_CACHE: dict = {"t": 0.0, "data": None}
_VERIFY_TTL = 300  # 验证结果缓存 5 分钟


def _load() -> dict:
    if FILE.exists():
        try:
            return json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def _save(d: dict) -> None:
    try:
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def get_rating(code: str) -> str:
    v = _load().get(code)
    return v.get("rating") if v else "中性"


def get(code: str) -> dict:
    v = _load().get(code)
    return v or {"rating": "中性", "levels": "", "reason": "", "updated": None}


def parse_text(text: str) -> dict:
    """从分析部门表格/文字中解析意见：每行取 6 位代码 + 评级 + 关键价位。"""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip().strip("|,，\t")
        if not line:
            continue
        m = re.search(r"\b(\d{6})\b", line)
        if not m:
            continue
        code = m.group(1)
        rating = next((r for r in RATINGS if r in line), "中性")
        lm = re.search(r"(\d+(?:\.\d+)?(?:\s*[/／\-]\s*\d+(?:\.\d+)?)+)", line)
        levels = lm.group(1) if lm else ""
        out[code] = {
            "rating": rating,
            "levels": levels,
            "reason": line,
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    return out


def _record_entry(opinions: dict) -> dict:
    """给没有录入价的意见补记当前价（录入基准），用于后续验证。"""
    missing = [c for c, v in opinions.items() if not v.get("entry_price")]
    if missing:
        try:
            import market

            today = datetime.now().strftime("%Y-%m-%d")
            for q in market.get_quotes(missing, fast=True):
                c = q.get("code")
                if c in opinions and q.get("price"):
                    opinions[c].setdefault("entry_price", q["price"])
                    opinions[c].setdefault("entry_date", today)
                    opinions[c]["last_price"] = q["price"]
        except Exception:  # noqa: BLE001
            pass
    return opinions


def verify_all(force: bool = False) -> dict:
    """用数据验证投研意见：按评级统计方向命中率与平均收益（辩论依据）。"""
    if not force and _VERIFY_CACHE["data"] and time.time() - _VERIFY_CACHE["t"] < _VERIFY_TTL:
        return _VERIFY_CACHE["data"]
    opinions = _load()
    codes = [c for c, v in opinions.items() if v.get("entry_price")]
    cur: dict = {}
    if codes:
        try:
            import market

            for q in market.get_quotes(codes, fast=True):
                if q.get("price"):
                    cur[q["code"]] = q["price"]
        except Exception:  # noqa: BLE001
            pass
    stats = {r: {"n": 0, "hits": 0, "ret": 0.0} for r in RATINGS}
    detail: dict = {}
    today = datetime.now().strftime("%Y-%m-%d")
    for c, v in opinions.items():
        ep = v.get("entry_price")
        if not ep:
            continue
        cp = cur.get(c, v.get("last_price") or ep)
        ret = cp / ep - 1.0 if ep else 0.0
        r = v.get("rating")
        hit = None
        if r in ("强烈看好", "看好"):
            hit = ret > 0
        elif r == "回避":
            hit = ret < 0
        if r in stats:
            stats[r]["n"] += 1
            if hit is not None:
                stats[r]["hits"] += 1 if hit else 0
            stats[r]["ret"] += ret
        days = 0
        ed = v.get("entry_date")
        if ed:
            try:
                days = (datetime.strptime(today, "%Y-%m-%d")
                        - datetime.strptime(ed[:10], "%Y-%m-%d")).days
            except Exception:  # noqa: BLE001
                pass
        detail[c] = {"ret": ret, "hit": hit, "days": days, "rating": r}
    out = {
        "stats": {
            r: {**st,
                "hit_rate": (st["hits"] / st["n"] if st["n"] else None),
                "avg_ret": (st["ret"] / st["n"] if st["n"] else None)}
            for r, st in stats.items()
        },
        "detail": detail,
    }
    _VERIFY_CACHE["t"] = time.time()
    _VERIFY_CACHE["data"] = out
    return out


def ingest_folder() -> dict:
    """扫描“投研输入”文件夹，自动解析并合并意见；已处理文件移入“已处理/”。"""
    IN_DIR.mkdir(exist_ok=True)
    DONE_DIR.mkdir(exist_ok=True)
    files = sorted(
        p for p in IN_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in (".txt", ".csv", ".tsv", ".md", ".json")
        and p.name != ".DS_Store"
    )
    imported = 0
    errors: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
            if f.suffix.lower() == ".json":
                data = json.loads(text)
                if isinstance(data, dict) and "opinions" in data:
                    data = data["opinions"]
                parsed = {}
                if isinstance(data, dict):
                    for k, v in data.items():
                        if re.match(r"^\d{6}$", str(k)) and isinstance(v, dict):
                            parsed[str(k)] = {
                                "rating": v.get("rating", "中性"),
                                "levels": v.get("levels", ""),
                                "reason": v.get("reason", ""),
                                "updated": v.get("updated") or datetime.now().strftime("%Y-%m-%d %H:%M"),
                            }
                elif isinstance(data, list):
                    for row in data:
                        code = str(row.get("code") or row.get("symbol") or "")
                        if re.match(r"^\d{6}$", code):
                            parsed[code] = {
                                "rating": row.get("rating", "中性"),
                                "levels": row.get("levels", ""),
                                "reason": row.get("reason", ""),
                                "updated": row.get("updated") or datetime.now().strftime("%Y-%m-%d %H:%M"),
                            }
            else:
                parsed = parse_text(text)
            if parsed:
                opinions = _load()
                opinions.update(parsed)
                opinions = _record_entry(opinions)
                _save(opinions)
                imported += len(parsed)
            dst = DONE_DIR / f"{time.strftime('%Y%m%d_%H%M%S_%f')}_{f.name}"
            f.replace(dst)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{f.name}: {exc}")
    result = {
        "checked": len(files),
        "imported": imported,
        "total": len(_load()),
        "errors": errors,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        LAST_FILE.write_text(json.dumps({"last": result}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return result


def last_ingest() -> dict:
    try:
        if LAST_FILE.exists():
            return json.loads(LAST_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def import_text(text: str) -> dict:
    parsed = parse_text(text)
    opinions = _load()
    opinions.update(parsed)
    opinions = _record_entry(opinions)
    _save(opinions)
    return {
        "imported": len(parsed),
        "total": len(opinions),
        "summary": summary(opinions),
        "stats": verify_all(force=True),
    }


def set_opinion(code: str, rating: str, levels: str = "", reason: str = "") -> dict:
    if rating not in RATINGS:
        raise ValueError(f"评级必须是 {'/'.join(RATINGS)} 之一")
    opinions = _load()
    old = opinions.get(code) or {}
    opinions[code] = {
        "rating": rating,
        "levels": levels.strip() or old.get("levels", ""),
        "reason": reason.strip() or old.get("reason", ""),
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if not opinions[code].get("entry_price"):
        opinions = _record_entry({code: opinions[code]})
    _save(opinions)
    return {"code": code, **opinions[code], "total": len(opinions),
            "stats": verify_all(force=True)}


def all_opinions() -> dict:
    return _load()


def summary(opinions: dict | None = None) -> dict:
    opinions = opinions if opinions is not None else _load()
    s = {r: 0 for r in RATINGS}
    for v in opinions.values():
        r = v.get("rating")
        if r in s:
            s[r] += 1
    s["total"] = len(opinions)
    return s

"""实时行情：东方财富公开行情接口（与东方财富软件同源）。

- /api/qt/stock/get         单只实时报价
- /api/qt/stock/trends2/get 分时数据
- /api/qt/clist/get         板块列表（行业板块 → BK 代码）
- /api/qt/stock/kline/get   日 K（个股/板块）
- /api/qt/stock/fflow/daykline/get  板块资金流
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

QUOTE_URL = (
    "https://push2.eastmoney.com/api/qt/stock/get"
    "?secid={secid}&invt=2&fltt=2"
    "&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f127,"
    "f107,f116,f117,f162,f167,f168,f169,f170,f171"
)
ULIST_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get"
    "?secids={secids}&fltt=2&invt=2"
    "&fields=f2,f3,f4,f5,f6,f8,f10,f12,f14,f15,f16,f17,f18"
)
TREND_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
    "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&ndays=1&iscr=0"
)
KLINE_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={secid}&klt=101&fqt=1&lmt={lmt}&end=20500101"
    "&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
)
TENCENT_KLINE_URL = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param={symbol},day,,,{lmt},qfq"
)
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={symbols}"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbols}"
BOARD_FFLOW_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    "?secid={secid}&klt=101&lmt={lmt}"
    "&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
)
BOARD_LIST_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
    "&fs=m:90+t:2+f:!50&fields=f12,f14"
)
ALL_A_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn=1&pz=6000&po=1&np=1&fltt=2&invt=2&fid=f3"
    "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    "&fields=f12,f14,f2,f3,f5,f6,f8,f10,f62,f100,f20,f21"
)
BOARD_RANK_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f62"
    "&fs=m:90+t:2+f:!50&fields=f12,f14,f2,f3,f62,f8,f10"
)
BOARD_STOCKS_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f3"
    "&fs=b:{bk}&fields=f12,f14,f2,f3,f5,f6,f8,f10,f62,f100"
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
ROOT = Path(__file__).resolve().parent
BOARD_CACHE_FILE = ROOT / "board_cache.json"
BOARD_CACHE_TTL = 24 * 3600  # 板块列表一天刷新一次
BOARD_DATA_CACHE_DIR = ROOT / ".board_cache"
BOARD_DATA_CACHE_TTL = 60 * 60  # 板块 K 线/资金流 1 小时
KLINE_CACHE_DIR = ROOT / ".kline_cache"
KLINE_CACHE_TTL = 30 * 60  # 个股 K 线 30 分钟

# 板块 K 线 / 资金流内存缓存
_board_cache: dict[str, tuple[float, list[dict]]] = {}
_BOARD_DFLOW_TTL = 10 * 60
_kline_cache: dict[str, tuple[float, list[dict]]] = {}
_SNAP_CACHE: dict[str, tuple[float, dict]] = {}
_SNAP_TTL = 30  # 快照（全市场/板块排行）30 秒
_QUOTES_CACHE: dict[str, tuple[float, list[dict]]] = {}
_QUOTES_TTL = 5  # 批量实时行情 5 秒缓存（新浪主通道）
_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
_QUOTE_TTL = 10 * 60

_INDUSTRY_CACHE: dict[str, str | None] = {}
_NAME_CACHE: dict[str, tuple[float, str]] = {}
_TENCENT_BLOCKED_UNTIL: float = 0.0  # 腾讯接口被 WAF 拦截时的熔断时间
_NAME_TTL = 10 * 60
INDUSTRY_CACHE_FILE = ROOT / "industry_cache.json"


def _industry_from_file() -> dict:
    if INDUSTRY_CACHE_FILE.exists():
        try:
            return json.loads(INDUSTRY_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def _industry_to_file(mapping: dict) -> None:
    try:
        INDUSTRY_CACHE_FILE.write_text(
            json.dumps(mapping, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass

# 指数代码 -> 市场前缀。000001 在用户自选中是平安银行（深市），
# 因此不再映射为上证指数；上证指数用回测别名 999001 访问。
INDEX_SECID = {
    "999001": "1.000001",  # 上证指数（回测别名，000001 已让给平安银行）
    "000016": "1.000016",  # 上证50
    "000300": "1.000300",  # 沪深300
    "000905": "1.000905",  # 中证500
    "000852": "1.000852",  # 中证1000
    "399001": "0.399001",  # 深证成指
    "399005": "0.399005",  # 中小100
    "399006": "0.399006",  # 创业板指
}


def secid_of(code: str) -> str:
    code = code.strip()
    if code in INDEX_SECID:
        return INDEX_SECID[code]
    if len(code) != 6 or not code.isdigit():
        raise ValueError(f"无效代码 {code!r}")
    if code.startswith(("60", "68", "51", "56", "58")):
        return f"1.{code}"
    if code.startswith(("00", "30", "15", "16", "92", "43", "83", "87")):
        return f"0.{code}"
    raise ValueError(f"暂不支持的代码前缀: {code}")


def _get_json(url: str, timeout: float = 12.0, retries: int = 8) -> dict:
    """部分东方财富主机对 urllib 会直接断连，curl 更稳定。"""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            out = subprocess.run(
                [
                    "curl", "-s", "-m", str(timeout),
                    "-A", USER_AGENT,
                    "-e", "https://quote.eastmoney.com/",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=timeout + 8,
                check=True,
            )
            text = out.stdout.strip()
            if not text.startswith("{"):
                raise ValueError("响应不是 JSON")
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.0 * (attempt + 1))
    raise last_exc


def _curl_json(url: str, retries: int = 5, timeout: int = 15) -> dict:
    """别名：与 _get_json 相同实现，保留可读性。"""
    return _get_json(url, timeout=timeout, retries=retries)


def _curl_text(url: str, retries: int = 3, timeout: int = 12,
               extra_headers: list[str] | None = None) -> bytes:
    """返回原始字节（腾讯行情为 GBK 编码，需自行解码）。"""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            out = subprocess.run(
                ["curl", "-s", "-4", "-m", str(timeout), "-A", USER_AGENT]
                + (extra_headers or []) + [url],
                capture_output=True,
                timeout=timeout + 8,
                check=True,
            )
            if out.stdout:
                return out.stdout
            last_exc = ValueError("空响应")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(0.5 * (attempt + 1))
    raise last_exc


def _num(v):
    return None if v in ("-", "") else float(v)


def get_quote(code: str, secid: str | None = None, retries: int = 3) -> dict:
    cached = _QUOTE_CACHE.get(code)
    if cached and time.time() - cached[0] < _QUOTE_TTL:
        return cached[1]
    url = QUOTE_URL.format(secid=secid or secid_of(code))
    payload = _get_json(url, timeout=8.0, retries=retries)
    d = payload.get("data") or {}
    if not d:
        raise ValueError(f"未取到 {code} 实时行情（可能停牌或代码有误）")
    ts = d.get("f86") or 0
    if ts > 10_000_000_000:  # 毫秒 -> 秒
        ts = ts / 1000
    try:
        time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        time_str = ""
    out = {
        "code": d.get("f57") or code,
        "name": d.get("f58") or code,
        "industry": d.get("f127") or None,
        "price": _num(d.get("f43")),
        "high": _num(d.get("f44")),
        "low": _num(d.get("f45")),
        "open": _num(d.get("f46")),
        "volume": _num(d.get("f47")),     # 手
        "amount": _num(d.get("f48")),     # 元
        "vol_ratio": _num(d.get("f50")),  # 量比
        "prev_close": _num(d.get("f60")),
        "change": _num(d.get("f169")),
        "pct": _num(d.get("f170")),
        "amplitude": _num(d.get("f171")),
        "turnover": _num(d.get("f168")),  # 换手率
        "pe": _num(d.get("f162")),
        "pb": _num(d.get("f167")),
        "market_cap": _num(d.get("f116")),
        "float_cap": _num(d.get("f117")),
        "time": time_str,
    }
    _QUOTE_CACHE[code] = (time.time(), out)
    return out


def get_quotes(codes: list[str], fast: bool = False) -> list[dict]:
    """批量实时行情：优先单次请求（ulist），失败再逐只降级。

    fast=True 用于低频撮合场景：只请求一次、失败立即返回错误条目，避免重试风暴。
    """
    key = ",".join(codes)
    cached = _QUOTES_CACHE.get(key)
    ttl = 5 if (cached and not any(q.get("price") is not None for q in cached[1])) else _QUOTES_TTL
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    secids = [secid_of(c) for c in codes]
    sina_map, tx_map = {}, {}
    from concurrent.futures import ThreadPoolExecutor

    def _sina():
        try:
            sina_map.update(
                {q["code"]: q for q in _get_quotes_sina(secids, timeout=5, retries=1) if q.get("code")}
            )
        except Exception:  # noqa: BLE001
            pass

    def _tx():
        try:
            tx_map.update(
                {q["code"]: q for q in _get_quotes_tencent(secids, timeout=4 if fast else 6, retries=1 if fast else 2)
                 if q.get("code")}
            )
        except Exception:  # noqa: BLE001
            pass

    with ThreadPoolExecutor(max_workers=2) as ex:
        ex.submit(_sina)
        ex.submit(_tx)
    out = []
    for code in codes:
        if code in sina_map:
            out.append(sina_map[code])
        elif code in tx_map:
            out.append(tx_map[code])
        else:
            out.append({"code": code, "name": code, "error": "行情接口暂不可用"})
    _QUOTES_CACHE[key] = (time.time(), out)
    return out


def get_quotes_secids(secids: list[str]) -> list[dict]:
    """按显式市场前缀批量行情（用于自选股：000001=平安银行等）。

    主力通道：新浪批量行情（约 0.4 秒/200 只）；腾讯兜底。
    """
    key = "secids:" + ",".join(secids)
    cached = _QUOTES_CACHE.get(key)
    ttl = 5 if (cached and not any(q.get("price") is not None for q in cached[1])) else _QUOTES_TTL
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    # 新浪为主、腾讯兜底：并行请求，各自短超时，保证页面秒开
    sina_map, tx_map = {}, {}
    from concurrent.futures import ThreadPoolExecutor

    def _sina():
        try:
            sina_map.update(
                {q["code"]: q for q in _get_quotes_sina(secids, timeout=5, retries=1) if q.get("code")}
            )
        except Exception:  # noqa: BLE001
            pass

    def _tx():
        try:
            tx_map.update(
                {q["code"]: q for q in _get_quotes_tencent(secids, timeout=4, retries=1) if q.get("code")}
            )
        except Exception:  # noqa: BLE001
            pass

    with ThreadPoolExecutor(max_workers=2) as ex:
        ex.submit(_sina)
        ex.submit(_tx)
    out = []
    for sid in secids:
        code = sid.split(".")[-1]
        if code in sina_map:
            out.append(sina_map[code])
        elif code in tx_map:
            out.append(tx_map[code])
        else:
            out.append({"code": code, "name": code, "error": "行情接口暂不可用"})
    _QUOTES_CACHE[key] = (time.time(), out)
    return out


def _get_quotes_tencent(secids: list[str], timeout: int = 8, retries: int = 2) -> list[dict]:
    """腾讯批量行情（qt.gtimg.cn，GBK）。字段与东财 ulist 输出对齐。"""
    symbols = []
    for sid in secids:
        mkt, code = sid.split(".")
        if code.startswith(("92", "43", "83", "87")):
            symbols.append("bj" + code)
        elif mkt == "1":
            symbols.append("sh" + code)
        else:
            symbols.append("sz" + code)
    raw = _curl_text(
        TENCENT_QUOTE_URL.format(symbols=",".join(symbols)),
        retries=retries,
        timeout=timeout,
    )
    text = raw.decode("gbk", errors="replace")
    out = []
    for line in text.split(";"):
        m = re.search(r'v_(?:sz|sh|bj)(\d{6})="([^"]*)"', line)
        if not m:
            continue
        code, payload = m.group(1), m.group(2)
        f = payload.split("~")
        if len(f) < 40:
            continue

        def num(idx):
            try:
                v = f[idx].strip()
                return float(v) if v not in ("", "-") else None
            except Exception:  # noqa: BLE001
                return None

        raw_time = f[30] if len(f) > 30 else ""
        time_str = ""
        if len(raw_time) >= 14 and raw_time.isdigit():
            time_str = f"{raw_time[:4]}-{raw_time[4:6]}-{raw_time[6:8]} {raw_time[8:10]}:{raw_time[10:12]}:{raw_time[12:14]}"
        amount_wan = num(37)
        out.append(
            {
                "code": code,
                "name": f[1] or code,
                "price": num(3),
                "pct": num(32),
                "change": num(31),
                "open": num(5),
                "high": num(33),
                "low": num(34),
                "prev_close": num(4),
                "volume": num(6),
                "amount": amount_wan * 10000 if amount_wan is not None else None,
                "turnover": num(38),
                "vol_ratio": num(49),
                "time": time_str,
            }
        )
    return out


def _get_quotes_sina(secids: list[str], timeout: int = 6, retries: int = 1) -> list[dict]:
    """新浪批量行情（hq.sinajs.cn，GBK）。批量 100-200 只约 0.4 秒，作为主通道。
    字段与腾讯输出对齐：price/pct/change/open/high/low/prev_close/volume/amount/turnover/time。
    """
    symbols = []
    for sid in secids:
        mkt, code = sid.split(".")
        symbols.append(("sh" if mkt == "1" else "sz") + code)
    url = SINA_QUOTE_URL.format(symbols=",".join(symbols))
    raw = _curl_text(url, retries=retries, timeout=timeout,
                     extra_headers=["-H", "Referer: https://finance.sina.com.cn"])
    text = raw.decode("gbk", errors="replace")
    out = []
    for line in text.split(";"):
        m = re.search(r'hq_str_(?:sh|sz)(\d{6})="([^"]*)"', line)
        if not m:
            continue
        code, payload = m.group(1), m.group(2)
        f = payload.split(",")
        if len(f) < 32 or not f[0]:
            continue

        def num(idx):
            try:
                v = f[idx].strip()
                return float(v) if v not in ("", "0.000") else None
            except Exception:  # noqa: BLE001
                return None

        name = f[0]
        open_px = num(1)
        prev_close = num(2)
        price = num(3)
        high = num(4)
        low = num(5)
        volume = num(8)          # 成交量（股）
        amount = num(9)          # 成交额（元）
        pct = None
        change = None
        if price is not None and prev_close and prev_close > 0:
            change = price - prev_close
            pct = change / prev_close * 100.0
        time_str = ""
        if len(f) > 31 and f[30] and f[31]:
            time_str = f"{f[30]} {f[31]}"
        out.append(
            {
                "code": code,
                "name": name,
                "price": price,
                "pct": pct,
                "change": change,
                "open": open_px,
                "high": high,
                "low": low,
                "prev_close": prev_close,
                "volume": volume,
                "amount": amount,
                "turnover": None,
                "vol_ratio": None,
                "time": time_str,
            }
        )
    return out


def _get_quotes_ulist(codes: list[str], fast: bool = False) -> list[dict]:
    secids = ",".join(secid_of(c) for c in codes)
    payload = _get_json(
        ULIST_URL.format(secids=secids),
        timeout=6.0 if fast else 12.0,
        retries=1 if fast else 8,
    )
    diff = ((payload.get("data") or {}).get("diff")) or []
    by_code = {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item in diff:
        code = str(item.get("f12") or "")
        by_code[code] = {
            "code": code,
            "name": item.get("f14") or code,
            "price": item.get("f2"),
            "pct": item.get("f3"),
            "change": item.get("f4"),
            "open": item.get("f17"),
            "high": item.get("f15"),
            "low": item.get("f16"),
            "prev_close": item.get("f18"),
            "volume": item.get("f5"),
            "amount": item.get("f6"),
            "turnover": item.get("f8"),
            "vol_ratio": item.get("f10"),
            "time": now,
        }
    out = []
    for code in codes:
        if code in by_code:
            out.append(by_code[code])
        else:
            out.append({"code": code, "name": code, "error": "接口未返回该代码"})
    return out


def get_trend(code: str, secid: str | None = None) -> dict:
    """分时走势：返回 {time, price, avg, volume, amount, change, pct} 序列。"""
    url = TREND_URL.format(secid=secid or secid_of(code))
    payload = _get_json(url)
    d = payload.get("data") or {}
    lines = d.get("trends") or []
    series = []
    for line in lines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        # 实测字段顺序：时间,价格,均价,最高,最低,成交量,成交额,均价(3位)
        series.append(
            {
                "time": parts[0],
                "price": float(parts[1]),
                "avg": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
            }
        )
    if not series:
        raise ValueError(f"{code} 分时数据为空")
    return {
        "code": code,
        "name": d.get("name") or code,
        "pre_close": d.get("preClose"),
        "series": series,
    }


# ---------------------------------------------------------------------------
# 板块数据（策略：个股涨幅 vs 板块、板块资金净流出）
# ---------------------------------------------------------------------------

def board_map(force: bool = False) -> dict[str, str]:
    """行业板块名称 -> BK 代码（东方财富行业分类，共约 500 个）。"""
    if BOARD_CACHE_FILE.exists() and not force:
        age = time.time() - BOARD_CACHE_FILE.stat().st_mtime
        if age < BOARD_CACHE_TTL:
            try:
                return json.loads(BOARD_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    mapping: dict[str, str] = {}
    for pn in range(1, 7):
        url = BOARD_LIST_URL.format(pn=pn)
        d = _curl_json(url)
        diff = ((d.get("data") or {}).get("diff")) or []
        for item in diff:
            name = item.get("f14")
            bk = item.get("f12")
            if name and bk:
                mapping[name] = bk
        if len(diff) < 100:
            break
        time.sleep(0.6)
    if not mapping:
        raise ValueError("板块列表获取失败")
    BOARD_CACHE_FILE.write_text(
        json.dumps(mapping, ensure_ascii=False), encoding="utf-8"
    )
    return mapping


def get_board_bk(code: str) -> tuple[str, str] | None:
    """返回 (行业名, BK代码)；无行业归属（指数/ETF）返回 None。"""
    if code in _INDUSTRY_CACHE:
        industry = _INDUSTRY_CACHE[code]
    else:
        disk_map = _industry_from_file()
        industry = disk_map.get(code)
        if industry is None:
            try:
                industry = get_quote(code).get("industry")
            except Exception:  # noqa: BLE001
                industry = None
        if industry is None:
            try:
                snap = get_all_stocks()
                by_code = {s["code"]: s.get("industry") for s in snap["stocks"]}
                industry = by_code.get(code)
            except Exception:  # noqa: BLE001
                industry = None
        _INDUSTRY_CACHE[code] = industry
        if industry:
            disk_map[code] = industry
            _industry_to_file(disk_map)
    if not industry or industry == "-":
        return None
    bk = board_map().get(industry)
    if not bk:
        return None
    return industry, bk


def get_name(code: str) -> str:
    """标的名称：本地数据文件优先，其次实时行情（10 分钟缓存）。"""
    from engine import list_symbols

    for sym in list_symbols():
        if sym["code"] == code:
            return sym["name"]
    # 全市场分析缓存里直接取名称，避免网络请求
    try:
        analysis_file = ROOT / "market_analysis.json"
        if analysis_file.exists():
            data = json.loads(analysis_file.read_text(encoding="utf-8"))
            item = data.get(code)
            if item and item.get("name"):
                _NAME_CACHE[code] = (time.time(), item["name"])
                return item["name"]
    except Exception:  # noqa: BLE001
        pass
    item = _NAME_CACHE.get(code)
    if item and time.time() - item[0] < _NAME_TTL:
        return item[1]
    try:
        name = get_quote(code).get("name") or code
    except Exception:  # noqa: BLE001
        name = code
    _NAME_CACHE[code] = (time.time(), name)
    return name


def _cache_get(key: str) -> list[dict] | None:
    item = _board_cache.get(key)
    if item and time.time() - item[0] < _BOARD_DFLOW_TTL:
        return item[1]
    return None


def _cache_put(key: str, value: list[dict]) -> None:
    _board_cache[key] = (time.time(), value)


def _disk_cache_read(path: Path, allow_stale: bool = False) -> list[dict] | None:
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if allow_stale or age < BOARD_DATA_CACHE_TTL:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
    return None


def _disk_cache_write(path: Path, rows: list[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def get_board_kline(bk: str, lmt: int = 1000) -> list[dict]:
    """板块日 K：{date, open, close, high, low, volume, amount, pct, turnover}。"""
    key = f"kline:{bk}:{lmt}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    disk_path = BOARD_DATA_CACHE_DIR / f"{bk}_kline.json"
    disk = _disk_cache_read(disk_path)
    if disk is not None:
        _cache_put(key, disk)
        return disk
    try:
        url = KLINE_URL.format(secid=f"90.{bk}", lmt=lmt)
        d = _curl_json(url)
        rows = _parse_kline_rows(d)
    except Exception:  # noqa: BLE001
        rows = _disk_cache_read(disk_path, allow_stale=True) or []
    if not rows:
        raise ValueError(f"板块 {bk} 日K为空")
    _cache_put(key, rows)
    _disk_cache_write(disk_path, rows)
    return rows


def get_board_fflow(bk: str, lmt: int = 1000) -> list[dict]:
    """板块资金流：{date, main_net, big_net, xl_net, pct, amount, turnover}。

    main_net 为“主力净流入”（元），负数即净流出。
    """
    key = f"fflow:{bk}:{lmt}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    disk_path = BOARD_DATA_CACHE_DIR / f"{bk}_fflow.json"
    disk = _disk_cache_read(disk_path)
    if disk is not None:
        _cache_put(key, disk)
        return disk
    try:
        url = BOARD_FFLOW_URL.format(secid=f"90.{bk}", lmt=lmt)
        d = _curl_json(url)
        data = d.get("data") or {}
        rows = []
        for line in data.get("klines") or []:
            p = line.split(",")
            if len(p) < 15:
                continue
            rows.append(
                {
                    "date": p[0],
                    "main_net": float(p[1]),
                    "small_net": float(p[2]),
                    "mid_net": float(p[3]),
                    "big_net": float(p[4]),
                    "xl_net": float(p[5]),
                    "main_pct": float(p[6]),
                    "close": float(p[11]),
                    "pct": float(p[12]),
                    "amount": float(p[13]),
                    "turnover": float(p[14]),
                }
            )
    except Exception:  # noqa: BLE001
        rows = _disk_cache_read(disk_path, allow_stale=True) or []
    if not rows:
        raise ValueError(f"板块 {bk} 资金流为空")
    _cache_put(key, rows)
    _disk_cache_write(disk_path, rows)
    return rows


def get_board_context(code: str) -> dict:
    """一次取齐某标的的行业板块上下文（板块K线+资金流），供板块页展示。"""
    pair = get_board_bk(code)
    if pair is None:
        return {
            "code": code,
            "industry": None,
            "bk": None,
            "name": None,
            "kline": [],
            "fflow": [],
        }
    industry, bk = pair
    kline = get_board_kline(bk)
    fflow = get_board_fflow(bk)
    # 板块名称：用资金流返回的名称兜底，取不到则用行业名
    name = industry
    return {
        "code": code,
        "industry": industry,
        "bk": bk,
        "name": name,
        "kline": kline,
        "fflow": fflow,
    }


def get_chips(code: str, lookback: int = 120) -> dict:
    """实时筹码指标：抓最近 N 日个股日 K（前复权，与回测数据同口径）。"""
    import chips

    rows = get_kline(code, lmt=lookback + 60)
    return chips.latest_chips(rows, lookback=lookback)


# ---------------------------------------------------------------------------
# 平台扩展：个股日K / 全市场快照 / 板块排行 / 板块成分 / 数据刷新
# ---------------------------------------------------------------------------

def _parse_kline_rows(payload: dict) -> list[dict]:
    data = payload.get("data") or {}
    rows: list[dict] = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 11:
            continue
        rows.append(
            {
                "date": p[0],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),
                "amount": float(p[6]),
                "amplitude": float(p[7]),
                "pct": float(p[8]),
                "chg": float(p[9]),
                "turnover": float(p[10]),
            }
        )
    return rows


def get_kline(code: str, lmt: int = 500, fresh: bool = False,
              retries: int = 2, timeout: float = 8.0,
              source: str = "auto", estimate_turnover: bool = True) -> list[dict]:
    """个股日 K（前复权），带 30 分钟磁盘缓存；fresh=True 强制刷新。
    source="tencent" 时跳过东财、直连腾讯（批量下载更快）。"""
    global _TENCENT_BLOCKED_UNTIL

    def _is_today(rows) -> bool:
        """缓存 K 线最后一根是否为今天（盘中需实时信号）。"""
        if not rows:
            return False
        return rows[-1].get("date") == datetime.now().strftime("%Y-%m-%d")

    key = f"kline:{code}:{lmt}"
    cached = _kline_cache.get(key)
    if cached and time.time() - cached[0] < KLINE_CACHE_TTL and not fresh:
        if _is_today(cached[1]):
            return cached[1]
    path = KLINE_CACHE_DIR / f"{code}_{lmt}.json"
    if not fresh:
        disk = _disk_cache_read(path)
        if disk is not None and _is_today(disk):
            _kline_cache[key] = (time.time(), disk)
            return disk
        # 命中任意长度的缓存（分析脚本写入的 _300 等），避免重复网络请求
        try:
            for p in sorted(KLINE_CACHE_DIR.glob(f"{code}_*.json"), reverse=True):
                disk = _disk_cache_read(p)
                if disk is not None and _is_today(disk):
                    _kline_cache[key] = (time.time(), disk)
                    return disk
        except Exception:  # noqa: BLE001
            pass
    if source == "sina":
        rows = _kline_sina(code, lmt)
    elif source == "tencent":
        if time.time() < _TENCENT_BLOCKED_UNTIL:
            rows = _kline_sina(code, lmt)  # 腾讯已知被拦截，直接走新浪
        else:
            try:
                rows = _kline_tencent_fallback(code, lmt, estimate_turnover=estimate_turnover,
                                               retries=1)
            except Exception:  # noqa: BLE001
                _TENCENT_BLOCKED_UNTIL = time.time() + 300  # 熔断 5 分钟
                rows = _kline_sina(code, lmt)
    else:
        try:
            url = KLINE_URL.format(secid=secid_of(code), lmt=lmt)
            d = _curl_json(url, retries=retries, timeout=timeout)
            rows = _parse_kline_rows(d)
        except Exception:  # noqa: BLE001
            if time.time() < _TENCENT_BLOCKED_UNTIL:
                rows = _kline_sina(code, lmt)
            else:
                try:
                    rows = _kline_tencent_fallback(code, lmt, estimate_turnover=estimate_turnover,
                                                   retries=2)
                except Exception:  # noqa: BLE001
                    _TENCENT_BLOCKED_UNTIL = time.time() + 300
                    rows = _kline_sina(code, lmt)
    if not rows:
        raise ValueError(f"{code} 日K为空")
    _kline_cache[key] = (time.time(), rows)
    _disk_cache_write(path, rows)
    return rows


def _kline_tencent_fallback(code: str, lmt: int, estimate_turnover: bool = True,
                            retries: int = 2) -> list[dict]:
    """东财K线不可用时，用腾讯 qfq 日K兜底。

    腾讯只给 date/open/close/high/low/volume(手)，
    pct/amount/turnover 由收盘价与流通股本估算（用于图表与信号展示）。
    """
    if code.startswith(("92", "43", "83", "87")):
        symbol = "bj" + code
    else:
        symbol = "sh" + code if code.startswith(("60", "68", "51", "56", "58", "9")) else "sz" + code
    url = TENCENT_KLINE_URL.format(symbol=symbol, lmt=lmt + 30)
    d = _curl_json(url, retries=retries, timeout=8)
    data = (d.get("data") or {}).get(symbol) or {}
    lines = data.get("qfqday") or data.get("day") or []
    if not lines:
        raise ValueError(f"{code} 腾讯K线为空")

    # 估算流通股本：流通市值 / 现价（拿不到就用成交额反推换手，见下）
    float_shares = None
    if estimate_turnover:
        try:
            quote = get_quote(code, retries=1)
            px = quote.get("price") or quote.get("prev_close")
            cap = quote.get("float_cap")
            if px and cap:
                float_shares = cap / px
        except Exception:  # noqa: BLE001
            pass

    rows = []
    prev_close = None
    for p in lines:
        if len(p) < 6:
            continue
        date, open_, close, high, low, volume = p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])
        pct = (close / prev_close - 1.0) * 100 if prev_close else 0.0
        chg = close - prev_close if prev_close else 0.0
        amount = volume * 100 * close
        turnover = (volume * 100 / float_shares * 100.0) if float_shares else 0.0
        rows.append(
            {
                "date": date,
                "open": open_,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": amount,
                "amplitude": (high - low) / (prev_close or close) * 100,
                "pct": pct,
                "chg": chg,
                "turnover": turnover,
            }
        )
        prev_close = close
    return rows


def _kline_sina(code: str, lmt: int) -> list[dict]:
    """新浪日K兜底：json_v2 接口，字段 day/open/high/low/close/volume。
    pct/chg 由收盘价计算，amount/turnover 无法获取时置 0（仅兜底展示）。"""
    if code.startswith(("92", "43", "83", "87")):
        symbol = "bj" + code
    else:
        symbol = "sh" + code if code.startswith(("60", "68", "51", "56", "58", "9")) else "sz" + code
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
        f"?symbol={symbol}&scale=240&ma=no&datalen={lmt + 30}"
    )
    raw = _curl_text(url, retries=3, timeout=10,
                     extra_headers=["-H", "Referer: https://finance.sina.com.cn"])
    data = json.loads(raw.decode("utf-8", errors="ignore"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{code} 新浪K线为空")
    rows = []
    prev_close = None
    for p in data:
        try:
            close = float(p["close"])
            rows.append(
                {
                    "date": str(p["day"]),
                    "open": float(p["open"]),
                    "high": float(p["high"]),
                    "low": float(p["low"]),
                    "close": close,
                    "volume": float(p.get("volume") or 0),
                    "amount": 0.0,
                    "amplitude": 0.0,
                    "pct": (close / prev_close - 1.0) * 100 if prev_close else 0.0,
                    "chg": close - prev_close if prev_close else 0.0,
                    "turnover": 0.0,
                }
            )
            prev_close = close
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        raise ValueError(f"{code} 新浪K线为空")
    # 盘中补充：新浪K线当日 bar 可能滞后（只有到上一交易日），用实时行情补一根今日 bar
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        if rows[-1]["date"] != today:
            qs = get_quotes([code], fast=True)
            if qs:
                q = qs[0]
                px = q.get("price")
                if px and px > 0:
                    last_close = rows[-1]["close"]
                    rows.append(
                        {
                            "date": today,
                            "open": float(q.get("open") or last_close),
                            "high": float(q.get("high") or px),
                            "low": float(q.get("low") or px),
                            "close": float(px),
                            "volume": float(q.get("volume") or 0),
                            "amount": float(q.get("amount") or 0),
                            "amplitude": 0.0,
                            "pct": float(q.get("pct") or 0),
                            "chg": float(q.get("change") or (px - last_close)),
                            "turnover": 0.0,
                        }
                    )
    except Exception:  # noqa: BLE001
        pass
    return rows


def _snap_cache(key: str, loader) -> dict:
    item = _SNAP_CACHE.get(key)
    if item and time.time() - item[0] < _SNAP_TTL:
        return item[1]
    value = loader()
    _SNAP_CACHE[key] = (time.time(), value)
    return value


def get_all_stocks() -> dict:
    """全 A 股快照：{total, stocks:[...]}，分页取全量，30 秒内存缓存 + 磁盘缓存。"""
    def loader():
        all_stocks: list[dict] = []
        total = 0
        pn = 1
        while True:
            url = ALL_A_URL.replace("pn=1", f"pn={pn}").replace("pz=6000", "pz=200")
            try:
                d = _curl_json(url, timeout=8.0, retries=2)
            except Exception:  # noqa: BLE001
                break
            diff = ((d.get("data") or {}).get("diff")) or []
            total = (d.get("data") or {}).get("total") or total or 0
            if not diff:
                break
            for x in diff:
                if not x.get("f12"):
                    continue
                all_stocks.append(
                    {
                        "code": str(x.get("f12") or ""),
                        "name": x.get("f14") or "",
                        "price": _num(x.get("f2")),
                        "pct": _num(x.get("f3")),
                        "volume": _num(x.get("f5")),
                        "amount": _num(x.get("f6")),
                        "turnover": _num(x.get("f8")),
                        "vol_ratio": _num(x.get("f10")),
                        "main_net": _num(x.get("f62")),
                        "industry": x.get("f100") or "",
                        "total_cap": _num(x.get("f20")),
                        "float_cap": _num(x.get("f21")),
                    }
                )
            if len(all_stocks) >= total or len(diff) < 100:
                break
            pn += 1
        return {"total": total or len(all_stocks), "stocks": all_stocks}

    return _snap_cache("all_stocks", loader)


def get_board_rank() -> list[dict]:
    """行业板块排行（按主力净流入排序）：{bk,name,price,pct,main_net,turnover,vol_ratio}。"""
    def loader():
        d = _curl_json(BOARD_RANK_URL)
        diff = ((d.get("data") or {}).get("diff")) or []
        return [
            {
                "bk": str(x.get("f12") or ""),
                "name": x.get("f14") or "",
                "price": _num(x.get("f2")),
                "pct": _num(x.get("f3")),
                "main_net": _num(x.get("f62")),
                "turnover": _num(x.get("f8")),
                "vol_ratio": _num(x.get("f10")),
            }
            for x in diff
            if x.get("f12")
        ]

    return _snap_cache("board_rank", loader)


def board_pct_map() -> dict[str, float]:
    """行业名称 -> 当日板块涨幅（用于选股器“跑赢板块”判断）。"""
    return {b["name"]: b["pct"] for b in get_board_rank() if b.get("pct") is not None}


def get_board_stocks(bk: str) -> list[dict]:
    """板块成分股（含实时行情与主力净流入）。"""
    def loader():
        url = BOARD_STOCKS_URL.format(bk=bk)
        d = _curl_json(url)
        diff = ((d.get("data") or {}).get("diff")) or []
        rows = [
            {
                "code": str(x.get("f12") or ""),
                "name": x.get("f14") or "",
                "price": _num(x.get("f2")),
                "pct": _num(x.get("f3")),
                "volume": _num(x.get("f5")),
                "amount": _num(x.get("f6")),
                "turnover": _num(x.get("f8")),
                "vol_ratio": _num(x.get("f10")),
                "main_net": _num(x.get("f62")),
                "industry": x.get("f100") or "",
            }
            for x in diff
            if x.get("f12")
        ]
        if rows:
            _disk_cache_write(BOARD_DATA_CACHE_DIR / f"{bk}_stocks.json", rows)
        return rows

    try:
        return _snap_cache(f"board_stocks:{bk}", loader)
    except Exception:  # noqa: BLE001
        stale = _disk_cache_read(BOARD_DATA_CACHE_DIR / f"{bk}_stocks.json", allow_stale=True)
        if stale is not None:
            return stale
        raise


def refresh_data(code: str, name: str | None = None, source: str = "auto",
                 estimate_turnover: bool = True) -> dict:
    """从东方财富刷新/新增某标的日线并写入 backtest/data/{code}_{name}.csv。"""
    from engine import DATA_DIR

    if name is None:
        quote = get_quote(code)
        name = quote.get("name") or code
    rows = get_kline(
        code, lmt=1200, fresh=True, retries=1, timeout=6,
        source=source, estimate_turnover=estimate_turnover,
    )
    if not rows:
        raise ValueError(f"{code} 无数据")
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{code}_{name}.csv"
    with open(path, "w", encoding="utf-8") as f:
        f.write("date,open,close,high,low,volume,amount,amplitude,pct,chg,turnover\n")
        for r in rows:
            f.write(
                f"{r['date']},{r['open']},{r['close']},{r['high']},{r['low']},"
                f"{r['volume']},{r['amount']},{r.get('amplitude', 0)},{r['pct']},"
                f"{r.get('chg', 0)},{r['turnover']}\n"
            )
    return {"code": code, "name": name, "rows": len(rows), "file": path.name,
            "first": rows[0]["date"], "last": rows[-1]["date"]}

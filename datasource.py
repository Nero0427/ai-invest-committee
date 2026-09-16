"""Market data layer: quotes, klines and technical indicators.

Standard library only - no third-party dependencies.
Data source: East Money public endpoints (no API key required).
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.request

import ths

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}
_TIMEOUT = 12


class DataError(Exception):
    """Raised when market data cannot be fetched or parsed."""


_LAST_CALL = [0.0]
_MIN_INTERVAL = 0.35
_SECID_CACHE = {}
_US_SUFFIX_CACHE = {}


def _throttle(min_gap=None):
    """Space out upstream calls.

    East Money throttles hard and its push2his K-line hosts are the strictest,
    so callers can request a longer gap than the global default.
    """
    needed = _MIN_INTERVAL if min_gap is None else min_gap
    gap = time.time() - _LAST_CALL[0]
    if gap < needed:
        time.sleep(needed - gap)
    _LAST_CALL[0] = time.time()


def _fetch(url: str, encoding: str = "utf-8", retries: int = 3, gap=None,
           headers=None) -> str:
    """Fetch a URL as text.

    The upstream endpoints intermittently drop connections when hit in quick
    succession, so retry with a short backoff before giving up.
    """
    last = None
    for attempt in range(retries):
        _throttle(gap)
        try:
            merged = dict(_UA)
            if headers:
                merged.update(headers)
            req = urllib.request.Request(url, headers=merged)
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return resp.read().decode(encoding, errors="replace")
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(0.4 * (attempt + 1))
    raise DataError("network error: %s: %s" % (type(last).__name__, last))


# East Money load balances across numbered mirrors and throttles each host
# independently, so a blocked mirror is bypassed rather than waited out.
EM_QUOTE_HOSTS = ("push2", "1.push2", "17.push2", "82.push2", "push2delay")
EM_HIST_HOSTS = ("1.push2his", "push2his", "29.push2his")
_MIRROR_POS = {"quote": 0, "hist": 0}
# push2his is far stricter about request spacing than push2.
_GAPS = {"quote": 0.4, "hist": 1.2}


def _fetch_eastmoney(path: str, kind: str = "quote") -> str:
    """Request an East Money API path, rotating mirrors on failure."""
    hosts = EM_QUOTE_HOSTS if kind == "quote" else EM_HIST_HOSTS
    gap = _GAPS.get(kind, _MIN_INTERVAL)
    start = _MIRROR_POS.get(kind, 0)
    last = None
    for step in range(len(hosts)):
        index = (start + step) % len(hosts)
        url = "https://%s.eastmoney.com/%s" % (hosts[index], path)
        try:
            text = _fetch(url, retries=1, gap=gap)
        except DataError as exc:
            last = exc
            continue
        _MIRROR_POS[kind] = index
        return text
    raise DataError("every east money mirror failed (%s): %s" % (kind, last))


def _fetch_em_json(path: str, kind: str = "quote") -> dict:
    raw = _fetch_eastmoney(path, kind)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataError("malformed payload from east money") from exc


def _fetch_json(url: str, gap=None) -> dict:
    raw = _fetch(url, gap=gap)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataError("malformed payload from data source") from exc


# ---------------------------------------------------------------- symbol ----


# Benchmarks. Quotes go through East Money's secid space (the delayed host is
# generous) while the history comes from Tonghuashun, which carries index
# series for free - East Money's kline host is the one that throttles.
INDEX_ALIASES = {
    "上证指数": ("1.000001", "hs_1A0001", "上证指数"),
    "上证": ("1.000001", "hs_1A0001", "上证指数"),
    "SH000001": ("1.000001", "hs_1A0001", "上证指数"),
    "000001.SH": ("1.000001", "hs_1A0001", "上证指数"),
    "深证成指": ("0.399001", "hs_399001", "深证成指"),
    "399001": ("0.399001", "hs_399001", "深证成指"),
    "创业板指": ("0.399006", "hs_399006", "创业板指"),
    "399006": ("0.399006", "hs_399006", "创业板指"),
    "沪深300": ("1.000300", "hs_1B0300", "沪深300"),
    "000300": ("1.000300", "hs_1B0300", "沪深300"),
}


def resolve_symbol(raw: str) -> dict:
    """Resolve user input into an eastmoney secid.

    Accepts 600519 / sh600519 / 600519.SH / 000858 / LITE / NVDA / 上证指数.
    """
    text = (raw or "").strip()
    s = text.upper()
    if not s:
        raise DataError("empty symbol")

    index = INDEX_ALIASES.get(text) or INDEX_ALIASES.get(s)
    if index:
        secid, ths_code, name = index
        return {"input": raw, "secid": secid, "code": ths_code[3:],
                "market": "SH", "kind": "index", "ths": ths_code, "name": name}

    m = re.match(r"^(SH|SZ|BJ)?(\d{6})(?:\.(SH|SZ|BJ))?$", s)
    if m:
        code = m.group(2)
        market = m.group(1) or m.group(3) or ("SH" if code[0] == "6" else "SZ")
        secid = ("1." if market == "SH" else "0.") + code
        return {"input": raw, "secid": secid, "code": code, "market": market, "kind": "A"}

    if re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", s):
        return {"input": raw, "secid": None, "code": s, "market": "US", "kind": "US"}

    raise DataError("unsupported symbol format: %s" % raw)


def _quote_path(secid: str) -> str:
    fields = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f116,f117,f162,f167,f170"
    return "api/qt/stock/get?secid=%s&fields=%s&invt=2&fltt=2" % (secid, fields)


def _kline_path(secid: str, limit: int) -> str:
    return ("api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
            "&klt=101&fqt=1&end=20500101&lmt=%d" % (secid, limit))


def resolve_us_secid(code: str) -> str:
    """Map a US ticker onto its East Money secid.

    Tencent reports the exchange suffix (LITE.OQ, IBM.N ...) which is a single
    reliable call, so it is preferred over probing three East Money prefixes.
    """
    if code in _SECID_CACHE:
        return _SECID_CACHE[code]

    suffix_map = {"OQ": "105", "N": "106", "AM": "107", "A": "107"}
    try:
        parts = _tencent_fields("us" + code)
        suffix = parts[2].split(".")[-1].upper()
        _US_SUFFIX_CACHE[code] = suffix
        prefix = suffix_map.get(suffix)
        if prefix:
            secid = "%s.%s" % (prefix, code)
            _SECID_CACHE[code] = secid
            return secid
    except (DataError, IndexError):
        pass

    for prefix in ("105", "106", "107"):
        secid = "%s.%s" % (prefix, code)
        try:
            data = _fetch_em_json(_quote_path(secid)).get("data") or {}
        except DataError:
            continue
        price = data.get("f43")
        if isinstance(price, (int, float)) and price > 0:
            _SECID_CACHE[code] = secid
            return secid
    raise DataError("US ticker not found: %s" % code)


# ----------------------------------------------------------------- quote ----


def _num(value):
    """East Money returns '-' for suspended or unavailable fields."""
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _tencent_code(symbol: dict) -> str:
    if symbol["kind"] == "A":
        return ("sh" if symbol["market"] == "SH" else "sz") + symbol["code"]
    return "us" + symbol["code"]


def _tencent_fields(tencent_code: str) -> list:
    raw = _fetch("https://qt.gtimg.cn/q=%s" % tencent_code, encoding="gbk")
    if "=" not in raw:
        raise DataError("tencent payload unparsable")
    return raw.split("=", 1)[1].strip().strip(";").strip('"').split("~")


def fetch_quote(symbol: dict) -> dict:
    """Live quote, walking the providers in order of reliability.

    Tonghuashun leads for both markets: it answers in tens of milliseconds,
    does not throttle, needs no exchange suffix for US tickers, and carries
    the same field set either way. East Money and Tencent stay behind it.
    """
    errors = []
    try:
        return _quote_ths(symbol)
    except ths.DataError as exc:
        errors.append(exc)

    try:
        secid = symbol["secid"] or resolve_us_secid(symbol["code"])
        symbol["secid"] = secid
        return _quote_eastmoney(symbol, secid)
    except DataError as exc:
        errors.append(exc)

    try:
        return _quote_tencent(symbol)
    except DataError as exc:
        errors.append(exc)

    raise errors[0] if errors else DataError("no quote source available")


def _quote_ths(symbol: dict) -> dict:
    """Quote from Tonghuashun, for stocks, tickers and indices alike.

    Shaped exactly like the East Money and Tencent readers, so the snapshot,
    the agents and the UI never learn which provider answered.
    """
    kind = symbol["kind"]
    if kind == "US":
        data = ths.fetch_us_quote(symbol["code"])
    elif symbol.get("ths"):
        data = ths.fetch_quote_by_code(symbol["ths"])
    else:
        data = ths.fetch_stock_quote(symbol["code"])

    if data.get("price") is None and data.get("prev_close") is None:
        raise ths.DataError("empty quote for %s" % symbol["code"])

    currency = {"US": "USD", "index": "点"}.get(kind, "CNY")
    return {
        "secid": symbol.get("secid"),
        "code": data.get("code") or symbol["code"],
        "name": data.get("name") or symbol.get("name") or symbol["code"],
        "market": symbol["market"],
        "kind": kind,
        "price": data.get("price"),
        "prev_close": data.get("prev_close"),
        "change": data.get("change"),
        "change_pct": data.get("change_pct"),
        "open": data.get("open"),
        "high": data.get("high"),
        "low": data.get("low"),
        "volume": data.get("volume"),
        "turnover": data.get("turnover"),
        "market_cap": data.get("market_cap"),
        "float_cap": data.get("float_cap"),
        "pe": data.get("pe"),
        "pb": data.get("pb"),
        "currency": currency,
        "source": "ths",
    }


def _quote_eastmoney(symbol: dict, secid: str) -> dict:
    data = _fetch_em_json(_quote_path(secid)).get("data") or {}
    if not data:
        raise DataError("no quote returned for %s" % secid)

    price = _num(data.get("f43"))
    prev = _num(data.get("f60"))
    change = None
    change_pct = _num(data.get("f170"))
    if price is not None and prev:
        change = price - prev

    return {
        "secid": secid,
        "code": data.get("f57") or symbol["code"],
        "name": data.get("f58") or symbol["code"],
        "market": symbol["market"],
        "kind": symbol["kind"],
        "price": price,
        "prev_close": prev,
        "change": change,
        "change_pct": change_pct,
        "open": _num(data.get("f46")),
        "high": _num(data.get("f44")),
        "low": _num(data.get("f45")),
        "volume": _num(data.get("f47")),
        "turnover": _num(data.get("f48")),
        "market_cap": _num(data.get("f116")),
        "float_cap": _num(data.get("f117")),
        "pe": _num(data.get("f162")),
        "pb": _num(data.get("f167")),
        "currency": "CNY" if symbol["kind"] == "A" else "USD",
        "source": "eastmoney",
    }


def _quote_tencent(symbol: dict) -> dict:
    """Fallback quote source.

    Indices 0-6, 30-34 and 44-45 share a stable layout between the A-share and
    US endpoints. Valuation fields sit at different offsets, so each market
    reads its own.
    """
    parts = _tencent_fields(_tencent_code(symbol))
    if len(parts) < 7:
        raise DataError("tencent quote fields too short")

    def f(i):
        try:
            return float(parts[i])
        except (ValueError, IndexError):
            return None

    price, prev = f(3), f(4)
    pct = None
    if price is not None and prev:
        pct = (price - prev) / prev * 100.0

    is_a = symbol["kind"] == "A"
    mcap = f(45)
    fcap = f(44)

    return {
        "secid": symbol["secid"] or _tencent_code(symbol),
        "code": parts[2] if len(parts) > 2 else symbol["code"],
        "name": parts[1] or symbol["code"],
        "market": symbol["market"],
        "kind": symbol["kind"],
        "price": price,
        "prev_close": prev,
        "change": (price - prev) if (price is not None and prev) else None,
        "change_pct": pct,
        "open": f(5),
        "high": f(33),
        "low": f(34),
        "volume": f(6),
        "turnover": None,
        "market_cap": (mcap * 1e8) if mcap is not None else None,
        "float_cap": (fcap * 1e8) if fcap is not None else None,
        "pe": f(39) if is_a else f(47),
        "pb": f(46) if is_a else None,
        "currency": "CNY" if is_a else "USD",
        "source": "tencent",
    }


# ---------------------------------------------------------------- klines ----


def fetch_klines(symbol: dict, limit: int = 250) -> list:
    """Daily forward-adjusted klines, oldest first.

    Tonghuashun leads for both markets. Tencent is second - its history
    endpoint has always stayed reachable, and for US tickers it returns the
    whole window in a single request. East Money's push2his comes last because
    that is the host that blocks this IP for half an hour at a time.
    """
    errors = []
    try:
        if symbol["kind"] == "US":
            rows = ths.fetch_us_klines(symbol["code"], limit)
        elif symbol.get("ths"):
            rows = ths.fetch_klines(symbol["ths"], limit)
        else:
            rows = ths.fetch_stock_klines(symbol["code"], limit)
        if len(rows) >= 20:
            return rows
        errors.append(DataError("ths returned only %d bars" % len(rows)))
    except ths.DataError as exc:
        errors.append(exc)

    try:
        return _klines_tencent(symbol, limit)
    except DataError as exc:
        errors.append(exc)

    try:
        secid = symbol["secid"] or resolve_us_secid(symbol["code"])
        symbol["secid"] = secid
        return _klines_eastmoney(secid, limit)
    except DataError as exc:
        errors.append(exc)

    raise errors[0] if errors else DataError("no kline source available")


def _klines_eastmoney(secid: str, limit: int) -> list:
    data = _fetch_em_json(_kline_path(secid, limit), kind="hist").get("data") or {}
    rows = data.get("klines") or []
    if not rows:
        raise DataError("no kline data for %s" % secid)

    out = []
    for row in rows:
        parts = row.split(",")
        if len(parts) < 6:
            continue
        try:
            out.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        except ValueError:
            continue
    if not out:
        raise DataError("kline rows were unparsable")
    return out


# Tencent's newer kline endpoint serves A-share stocks, US tickers, ETFs and
# sector indices (pt codes) through one interface - unlike East Money's
# push2his, which throttles this IP hard and has no rival for sector history.
_TX_KLINE_URLS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    "?param=%s,day,,,%d,qfq",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq",
)


def _tencent_kline_codes(symbol: dict) -> list:
    """Candidate Tencent codes for a symbol, best guess first.

    US tickers need an exchange suffix (XLK.AM, NVDA.OQ ...); the bare form
    silently returns a single bar rather than failing, so it is tried but the
    length check below rejects it.
    """
    if symbol["kind"] == "US":
        codes = []
        cached = _US_SUFFIX_CACHE.get(symbol["code"])
        if cached:
            codes.append("us%s.%s" % (symbol["code"], cached))
        codes.append("us" + symbol["code"])
        codes += ["us%s.%s" % (symbol["code"], sfx) for sfx in ("OQ", "N", "AM", "A")]
        return codes
    return [_tencent_code(symbol)]


def _parse_tencent_rows(data: dict, code: str) -> list:
    node = (data.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            out.append({
                "date": row[0],
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
        except (ValueError, TypeError):
            continue
    return out


def tencent_kline_by_code(code: str, limit: int = 250) -> list:
    """Klines for a raw Tencent code: sh600519, usXLK.AM, pt01801120.

    Uses a much tighter gap than the East Money calls: this host has never
    throttled across hundreds of requests, and the sector module fans out to
    twenty of these in one go when synthesizing an index.
    """
    last = None
    for template in _TX_KLINE_URLS:
        try:
            data = _fetch_json(template % (code, limit), gap=0.08)
        except DataError as exc:
            last = exc
            continue
        rows = _parse_tencent_rows(data, code)
        if len(rows) >= 20:
            return rows
    raise DataError("tencent kline unavailable for %s: %s" % (code, last))


def _klines_tencent(symbol: dict, limit: int) -> list:
    last = None
    for code in _tencent_kline_codes(symbol):
        for template in _TX_KLINE_URLS:
            try:
                data = _fetch_json(template % (code, limit))
            except DataError as exc:
                last = exc
                continue
            rows = _parse_tencent_rows(data, code)
            if len(rows) >= 20:
                return rows
    raise DataError("tencent kline unavailable for %s: %s"
                    % (symbol["code"], last))


# ------------------------------------------------------------ indicators ----


def sma(values: list, n: int):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values: list, n: int) -> list:
    if not values:
        return []
    k = 2.0 / (n + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def macd(values: list, fast: int = 12, slow: int = 26, signal: int = 9):
    if len(values) < slow + signal:
        return None
    ef, es = ema_series(values, fast), ema_series(values, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, signal)
    hist = [(a - b) * 2.0 for a, b in zip(dif, dea)]
    prev_hist = hist[-2] if len(hist) > 1 else hist[-1]
    return {
        "dif": dif[-1],
        "dea": dea[-1],
        "hist": hist[-1],
        "golden_cross": dif[-1] > dea[-1] and dif[-2] <= dea[-2] if len(dif) > 1 else False,
        "above_zero": dif[-1] > 0,
        "hist_rising": hist[-1] > prev_hist,
    }


def rsi(values: list, n: int = 14):
    if len(values) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(len(values) - n, len(values)):
        ch = values[i] - values[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger(values: list, n: int = 20, k: float = 2.0):
    if len(values) < n:
        return None
    window = values[-n:]
    mid = sum(window) / n
    sd = math.sqrt(sum((x - mid) ** 2 for x in window) / n)
    if sd == 0:
        return {"mid": mid, "upper": mid, "lower": mid, "width_pct": 0.0}
    return {
        "mid": mid,
        "upper": mid + k * sd,
        "lower": mid - k * sd,
        "width_pct": (2.0 * k * sd) / mid * 100.0,
    }


def atr(klines: list, n: int = 14):
    if len(klines) < n + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def _pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a - b) / b * 100.0


# --------------------------------------------------------------- summary ----


def build_market_snapshot(raw_symbol: str) -> dict:
    """Fetch everything the agents need in one call."""
    symbol = resolve_symbol(raw_symbol)
    quote = fetch_quote(symbol)
    klines = fetch_klines(symbol, limit=250)

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]

    ma5, ma10, ma20, ma60 = (sma(closes, n) for n in (5, 10, 20, 60))
    macd_v = macd(closes)
    rsi_v = rsi(closes)
    boll = bollinger(closes)
    atr_v = atr(klines)

    last = closes[-1]
    win20h, win20l = max(highs[-20:]), min(lows[-20:])
    win60h, win60l = max(highs[-60:]), min(lows[-60:])
    win20_vol = sum(k["volume"] for k in klines[-20:]) / min(20, len(klines))
    win5_vol = sum(k["volume"] for k in klines[-5:]) / min(5, len(klines))

    # Derived price levels: nearest real candidate below / above the last close.
    anchor = quote.get("price") or last
    below = [v for v in (win20l, ma20, boll["lower"] if boll else None, win60l)
             if v is not None and v < anchor]
    above = [v for v in (win20h, ma60, boll["upper"] if boll else None, win60h)
             if v is not None and v > anchor]
    support = max(below) if below else win60l
    resistance = min(above) if above else win60h
    if support >= resistance:
        support, resistance = win60l, win60h

    return {
        "quote": quote,
        "klines": klines,
        "indicators": {
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "macd": macd_v, "rsi14": rsi_v, "boll": boll, "atr14": atr_v,
            "last_close": last,
            "change_5d_pct": _pct(last, closes[-6]) if len(closes) > 6 else None,
            "change_20d_pct": _pct(last, closes[-21]) if len(closes) > 21 else None,
            "change_60d_pct": _pct(last, closes[-61]) if len(closes) > 61 else None,
            "high_20d": win20h, "low_20d": win20l,
            "high_60d": win60h, "low_60d": win60l,
            "vol_ratio_5_20": (win5_vol / win20_vol) if win20_vol else None,
        },
        "levels": {
            "anchor": anchor,
            "support": support,
            "resistance": resistance,
            "support_far": win60l,
            "resistance_far": win60h,
            "stop_loss": support * 0.98 if support else None,
            "target_low": resistance,
            "target_high": (resistance + 3.0 * atr_v) if (resistance and atr_v) else win60h,
            "atr": atr_v,
        },
        "bars": len(klines),
        "first_date": klines[0]["date"],
        "last_date": klines[-1]["date"],
    }


def refresh_live(symbol: dict) -> dict:
    """Light refresh used by the frontend polling loop."""
    return fetch_quote(symbol)

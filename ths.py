"""Tonghuashun (10jqka) market data layer for A-shares.

Standard library only, matching the rest of the project. Nothing here needs an
API key, a cookie or a signed request.

Endpoints (verified 2026-09-14):
  http://d.10jqka.com.cn/v6/realhead/{code}/defer/last.js   live quote  (JSONP)
  http://d.10jqka.com.cn/v6/line/{code}/01/{year}.js        daily bars  (JSONP)

Code namespaces:
  hs_600519   A-share stock; `hs_` covers Shanghai and Shenzhen alike
  usa_NVDA    US ticker. No exchange suffix is needed here - unlike Tencent's
              `usNVDA.OQ` form, where dropping the suffix does not fail but
              quietly returns a single bar
  bk_881xxx   industry index
  bk_885xxx   concept index, legacy series
  bk_886xxx   concept index, current series

Things worth knowing before touching this file:

* Tonghuashun is far friendlier than East Money. Several hundred requests in
  a row were never refused, and a 0.05s gap is plenty. That is the whole
  reason for preferring it: East Money's push2his blocks this IP for half an
  hour at a time, which is what left sector history broken before.
* Bars arrive one calendar year per request, so ~250 bars are stitched from
  two year files. `last.js` only ever returns 140.
* Board codes cannot be listed through an API. The numeric ranges are probed
  instead and empty codes dropped; roughly one in three is live. Retired
  concept indices (885284 and friends) still answer with a name but carry no
  history, so they are filtered out by the same pass.
* Volume arrives in *shares*. For A-shares that has to be converted to lots so
  it matches what East Money and Tencent report; for US tickers all three
  already agree on shares, so nothing is converted there.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "http://q.10jqka.com.cn/",
    "Accept": "*/*",
}
_TIMEOUT = 12

_LINE_URL = "http://d.10jqka.com.cn/v6/line/%s/01/%s.js"
_HEAD_URL = "http://d.10jqka.com.cn/v6/realhead/%s/defer/last.js"

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")
BOARD_CACHE = os.path.join(CACHE_DIR, "ths_boards.json")
BOARD_CACHE_TTL = 7 * 24 * 3600

# The payload is wrapped in a fixed callback name that varies per endpoint.
_JSONP = re.compile(r"\(\s*(\{.*\})\s*\)\s*$", re.S)

_LAST_CALL = [0.0]
_MIN_INTERVAL = 0.05


class DataError(Exception):
    """Raised when Tonghuashun data cannot be fetched or parsed."""


def _throttle(gap=None):
    needed = _MIN_INTERVAL if gap is None else gap
    if needed <= 0:
        return
    elapsed = time.time() - _LAST_CALL[0]
    if elapsed < needed:
        time.sleep(needed - elapsed)
    _LAST_CALL[0] = time.time()


def _fetch(url: str, retries: int = 3, gap=None, encoding: str = "gbk") -> str:
    last = None
    for attempt in range(retries):
        _throttle(gap)
        try:
            request = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return response.read().decode(encoding, errors="replace")
        except Exception as exc:  # noqa: BLE001 - retried below
            last = exc
            if attempt < retries - 1:
                time.sleep(0.3 * (attempt + 1))
    raise DataError("network error: %s: %s" % (type(last).__name__, last))


def _jsonp(text: str):
    match = _JSONP.search(text or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ codes --


def stock_code(code: str) -> str:
    """A-share ticker -> Tonghuashun code."""
    return "hs_" + str(code).strip()


def us_code(ticker: str) -> str:
    """US ticker -> Tonghuashun code, with no exchange suffix.

    Tencent needs `.OQ` / `.N` / `.AM` and silently mis-reports a bare ticker,
    so its suffix usually has to be probed first. Here the bare ticker is the
    correct and only form - a bad one 404s instead of returning bad data.
    """
    return "usa_" + str(ticker).strip().upper()


def board_code(code: str) -> str:
    text = str(code).strip().lower()
    return text if text.startswith("bk_") else "bk_" + text


# ------------------------------------------------------------------ quote --


def _quote_from_items(items: dict, code: str, name=None, to_lots=True) -> dict:
    """Map Tonghuashun's numeric field tags onto the project's quote shape.

    The tags are internal, so each one below was checked against Tencent's
    quote for 600519: 6=prev close, 7=open, 8=high, 9=low, 10 and 30=last,
    13=volume (shares), 19=turnover, 199112=change percent. The valuation
    tags were cross-checked against the same symbol's PE/PB as reported
    elsewhere. The same tags hold for US tickers, name included.
    """
    def f(key):
        return _to_float(items.get(key))

    price = f("10")
    if price is None:
        price = f("30")
    prev = f("6")
    if price is None:
        # During the first minutes of a session the live fields go briefly
        # empty while the feed rolls over and only the previous close is left.
        # Falling back to it beats handing the UI a blank price.
        price = prev
    volume = f("13")

    change_pct = f("199112")
    if change_pct is None and price is not None and prev:
        change_pct = (price / prev - 1.0) * 100.0

    return {
        "code": code,
        # Stocks carry their label inside the payload; board indices do not,
        # so a caller-supplied name is the fallback.
        "name": items.get("name") or name or code,
        "price": price,
        "prev_close": prev,
        "change": (price - prev) if (price is not None and prev) else None,
        "change_pct": change_pct,
        "open": f("7"),
        "high": f("8"),
        "low": f("9"),
        # A-shares: shares -> lots, to match East Money and Tencent. US
        # tickers are already quoted in shares everywhere, so they are left
        # alone.
        "volume": (volume / 100.0) if (volume is not None and to_lots) else volume,
        "turnover": f("19"),
        "limit_up": f("69"),
        "limit_down": f("70"),
        "pe": f("2034120"),
        "pb": f("592920"),
        "market_cap": f("3475914"),
        "float_cap": f("3541450"),
    }


def fetch_stock_quote(code: str) -> dict:
    """Live A-share quote."""
    blob = _jsonp(_fetch(_HEAD_URL % stock_code(code)))
    items = (blob or {}).get("items") or {}
    if not items:
        raise DataError("no quote for %s" % code)
    return _quote_from_items(items, code, (blob or {}).get("name"))


def fetch_quote_by_code(code: str) -> dict:
    """Live quote for a full Tonghuashun code: hs_…, usa_…, bk_….

    The per-market helpers above cover the common cases; this one is for
    callers that already hold a code - index benchmarks, for instance, which
    live under `hs_1A0001` rather than a stock ticker.
    """
    blob = _jsonp(_fetch(_HEAD_URL % code))
    items = (blob or {}).get("items") or {}
    if not items:
        raise DataError("no quote for %s" % code)
    return _quote_from_items(items, code, to_lots=not str(code).startswith("usa_"))


def fetch_us_quote(ticker: str, gap=None) -> dict:
    """Live US quote.

    Shaped exactly like the A-share one so callers never branch on market. The
    name comes back in Chinese ("英伟达", "苹果") because that is what the
    source carries, and PE/PB/market cap are populated just as they are for
    A-shares. `gap` lets batch callers drop the shared throttle.
    """
    blob = _jsonp(_fetch(_HEAD_URL % us_code(ticker), gap=gap))
    items = (blob or {}).get("items") or {}
    if not items:
        raise DataError("no quote for %s" % ticker)
    return _quote_from_items(items, ticker, to_lots=False)


def fetch_board_quote(code: str, gap=None) -> dict:
    """Live board index quote.

    The payload carries no board name, so callers that need a label keep using
    the one from the board list. `gap` lets batch callers opt out of throttling
    entirely - this host has never objected to concurrent load.
    """
    blob = _jsonp(_fetch(_HEAD_URL % board_code(code), gap=gap))
    items = (blob or {}).get("items") or {}
    if not items:
        raise DataError("no board quote for %s" % code)
    return _quote_from_items(items, code, (blob or {}).get("name"))


def fetch_board_changes(boards: list, workers: int = 20) -> dict:
    """code -> live change percent, for a batch of boards.

    The endpoint serves a single board per request, so this fans out wide.
    With ~480 boards and a 0.15s round trip, twenty workers finish in about
    three seconds; throttling here would serialise it back to half a minute.
    Boards that fail are simply absent from the result.
    """
    def one(board):
        code = board.get("code")
        if not code:
            return None
        try:
            return code, fetch_board_quote(code, gap=0.0).get("change_pct")
        except DataError:
            return None

    changes = {}
    if not boards:
        return changes
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(one, boards):
            if item:
                changes[item[0]] = item[1]
    return changes


# ------------------------------------------------------------------ klines --


def _parse_rows(blob: dict, to_lots: bool = True) -> list:
    """Rows look like date,open,high,low,close,volume,amount,turnover,..."""
    out = []
    for chunk in ((blob or {}).get("data") or "").split(";"):
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) < 6:
            continue
        try:
            volume = float(parts[5])
            if to_lots:
                volume = volume / 100.0  # shares -> lots
            out.append({
                "date": parts[0],
                "open": float(parts[1]),
                "high": float(parts[2]),
                "low": float(parts[3]),
                "close": float(parts[4]),
                "volume": volume,
            })
        except ValueError:
            continue
    return out


def fetch_klines(code: str, limit: int = 250, to_lots: bool = True) -> list:
    """Daily forward-adjusted bars, oldest first.

    Works for A-share stocks (`hs_600519`), US tickers (`usa_NVDA`) and board
    indices (`bk_885362`) alike. Note that `last.js` carries 140 bars for
    A-shares and nothing at all for US tickers - the year files are the only
    route that works for every market.

    The three year files are fetched together rather than one after another.
    Sequentially, a single dropped request silently leaves the series short
    (seen once as 174 bars instead of 250), and a short series quietly weakens
    every moving average built on top of it without anything looking wrong.
    """
    this_year = time.localtime().tm_year
    years = [str(year) for year in range(this_year, this_year - 3, -1)]

    def grab(year):
        try:
            blob = _jsonp(_fetch(_LINE_URL % (code, year)))
            return _parse_rows(blob, to_lots)
        except DataError:
            return []

    merged = {}
    with ThreadPoolExecutor(max_workers=len(years)) as pool:
        for rows in pool.map(grab, years):
            for row in rows:
                merged[row["date"]] = row

    if not merged:
        raise DataError("no klines for %s" % code)
    return [merged[date] for date in sorted(merged)][-limit:]


def fetch_stock_klines(code: str, limit: int = 250) -> list:
    return fetch_klines(stock_code(code), limit)


def fetch_us_klines(ticker: str, limit: int = 250) -> list:
    return fetch_klines(us_code(ticker), limit, to_lots=False)


def fetch_board_klines(code: str, limit: int = 250) -> list:
    return fetch_klines(board_code(code), limit)


# ------------------------------------------------------------------ boards --


# There is no listing endpoint, so these ranges are swept. The industry range
# is dense from 881101 and thins out quickly; concepts are split across a
# legacy 885xxx series and the current 886xxx one.
_BOARD_SEGMENTS = (
    ("industry", 881101, 882000),
    ("concept", 885000, 886000),
    ("concept", 886000, 887000),
)


def _probe_board(code: str):
    try:
        text = _fetch(_LINE_URL % ("bk_" + code, "last"), retries=1, gap=0.0)
    except DataError:
        return None
    blob = _jsonp(text)
    name = (blob or {}).get("name")
    if not name:
        return None
    return {
        "code": code,
        "name": name,
        # Retired concepts answer with a name but carry no history.
        "has_kline": bool(((blob or {}).get("data") or "").strip()),
    }


def scan_boards(workers: int = 16) -> list:
    """Sweep the board index space and keep the live codes.

    Sixteen workers bring the ~1900-code sweep down from half a minute to a
    few seconds; the host has never pushed back on concurrency.
    """
    found = []
    for board_type, low, high in _BOARD_SEGMENTS:
        codes = [str(number) for number in range(low, high)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for item in pool.map(_probe_board, codes):
                if item and item["has_kline"]:
                    item["board"] = board_type
                    found.append(item)
    return found


def _read_board_cache():
    try:
        with open(BOARD_CACHE, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
        if time.time() - blob.get("ts", 0) < BOARD_CACHE_TTL and blob.get("items"):
            return blob["items"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _write_board_cache(items: list) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(BOARD_CACHE, "w", encoding="utf-8") as handle:
            json.dump({"ts": time.time(), "items": items}, handle,
                      ensure_ascii=False)
    except OSError:
        pass


def load_board_list(force: bool = False) -> list:
    """Every live A-share board, cached on disk.

    A sweep costs ~1900 requests and about twenty seconds, which is why the
    result is cached for a week rather than rebuilt per run.
    """
    if not force:
        cached = _read_board_cache()
        if cached:
            return cached

    items = scan_boards()
    if not items:
        raise DataError("board list unavailable")
    for item in items:
        item["kind"] = "sector_cn"
        item["source"] = "ths"
        item["ths_code"] = "bk_" + item["code"]
        item["secid"] = None
    _write_board_cache(items)
    return items

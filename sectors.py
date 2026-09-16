"""Sector data layer.

A-share boards come from Tonghuashun. Its board index space is swept once and
cached, and every live board carries a real 250-bar index history that never
throttles. That replaced an arrangement where about half the boards had no
history of their own and had to be approximated from their constituents.

East Money is still consulted for exactly one thing: member lists, advance and
decline counts and money flow. Its `clist` endpoint returns all of that for a
whole board in a single request with market caps attached, on the `push2` host
that was never the problem - the throttling that hurt this project was always
`push2his`, the history endpoint. Tonghuashun would need a dozen HTML pages to
say the same thing.

The US has no equivalent board feed on the public endpoints, so SPDR sector
ETFs are used as the sector proxy - which is what the industry itself does
anyway.

Both paths return a snapshot shaped like datasource.build_market_snapshot so
the agents can treat sectors and single stocks uniformly.
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import datasource as ds
import ths

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")
SECTOR_CACHE = os.path.join(CACHE_DIR, "sectors_cn.json")
EM_INDEX_CACHE = os.path.join(CACHE_DIR, "em_board_index.json")
CACHE_TTL = 24 * 3600

# US sector proxies: SPDR's eleven GICS sectors plus the most traded sub-sectors.
US_SECTOR_ETF = {
    "XLK": ("科技", "Technology Select Sector SPDR"),
    "XLF": ("金融", "Financial Select Sector SPDR"),
    "XLV": ("医疗保健", "Health Care Select Sector SPDR"),
    "XLE": ("能源", "Energy Select Sector SPDR"),
    "XLI": ("工业", "Industrial Select Sector SPDR"),
    "XLY": ("可选消费", "Consumer Discretionary Select Sector SPDR"),
    "XLP": ("必需消费", "Consumer Staples Select Sector SPDR"),
    "XLU": ("公用事业", "Utilities Select Sector SPDR"),
    "XLB": ("原材料", "Materials Select Sector SPDR"),
    "XLRE": ("房地产", "Real Estate Select Sector SPDR"),
    "XLC": ("通信服务", "Communication Services Select Sector SPDR"),
    "SMH": ("半导体", "VanEck Semiconductor ETF"),
    "XBI": ("生物科技", "SPDR S&P Biotech ETF"),
    "KRE": ("区域银行", "SPDR S&P Regional Banking ETF"),
    "XOP": ("油气开采", "SPDR S&P Oil & Gas Exploration ETF"),
    "SPY": ("标普500", "SPDR S&P 500 ETF Trust"),
    "QQQ": ("纳斯达克100", "Invesco QQQ Trust"),
    "IWM": ("罗素2000", "iShares Russell 2000 ETF"),
    "IBB": ("生物科技(IBB)", "iShares Biotechnology ETF"),
    "SOXX": ("半导体(SOXX)", "iShares Semiconductor ETF"),
}

# Alias table so users can type Chinese sector names directly.
US_ALIAS = {}
for _code, (_cn, _en) in US_SECTOR_ETF.items():
    US_ALIAS[_cn] = _code
    US_ALIAS[_cn.replace("（", "(").replace("）", ")")] = _code
US_ALIAS.update({
    "科技股": "XLK", "半导体": "SMH", "芯片": "SMH",
    "银行": "XLF", "金融股": "XLF", "医药": "XLV", "医疗": "XLV",
    "石油": "XLE", "能源股": "XLE", "消费": "XLY", "新能源": "XLE",
    "地产": "XLRE", "房地产": "XLRE", "通信": "XLC", "公用": "XLU",
    "材料": "XLB", "工业股": "XLI", "大盘": "SPY", "纳指": "QQQ",
    "标普": "SPY", "小盘": "IWM",
})

_CLIST = ("api/qt/clist/get?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid={fid}"
          "&fs={fs}&fields={fields}")

_LIST_FIELDS = "f12,f14,f2,f3,f104,f105,f128,f136"
_MEMBER_FIELDS = "f12,f14,f2,f3,f62,f9,f20,f21"

_BOARD_PAGE = 100
_BOARD_MAX_PAGES = 14


# --------------------------------------------------------------- listing --


def _fetch_board(board_type: int) -> list:
    """Industry (t:2) or concept (t:3) boards, paged.

    The list API caps pz at 100 while each type holds roughly 500 boards, so
    paging by code (fid=f12) rather than by change is the only way to get full
    coverage - sorting by change would silently drop the quiet boards.
    """
    out = []
    for page in range(1, _BOARD_MAX_PAGES + 1):
        path = _CLIST.format(pn=page, pz=_BOARD_PAGE, fid="f12",
                             fs="m:90+t:%d" % board_type, fields=_LIST_FIELDS)
        try:
            data = ds._fetch_em_json(path).get("data") or {}
        except ds.DataError:
            break
        rows = data.get("diff") or []
        for item in rows:
            code = item.get("f12")
            if not code:
                continue
            out.append({
                "code": code,
                "name": item.get("f14") or code,
                "board": "industry" if board_type == 2 else "concept",
                "kind": "sector_cn",
                "secid": "90." + code,
                "change_pct": item.get("f3"),
                "up": item.get("f104"),
                "down": item.get("f105"),
                "leader": item.get("f128"),
                "leader_pct": item.get("f136"),
            })
        if len(rows) < _BOARD_PAGE:
            break
    return out


def _fetch_em_board_index() -> dict:
    """East Money boards keyed by name.

    The user-facing board list is Tonghuashun's now, but East Money is still
    the cheapest route to member tables, so its board list is built and
    indexed by name for cross-linking.
    """
    index = {}
    for board_type in (2, 3):
        for board in _fetch_board(board_type):
            index.setdefault(board["name"], board)
    return index


def _match_em_board(name: str, index: dict, ordered_names: list):
    """Link a Tonghuashun board to its East Money counterpart by name.

    Exact match first, then containment - but bounded containment, because
    "银行" would otherwise latch onto "银行系保险" and quietly analyse the
    wrong board's members. Boards East Money does not carry simply come back
    without a member list; their index history is unaffected.
    """
    if not name:
        return None
    if name in index:
        return index[name]
    if len(name) < 2:
        return None
    for other in ordered_names:
        shorter, longer = (name, other) if len(name) <= len(other) else (other, name)
        if shorter in longer and len(longer) <= len(shorter) * 2:
            return index[other]
    return None


def _read_cache():
    try:
        with open(SECTOR_CACHE, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
        if time.time() - blob.get("ts", 0) < CACHE_TTL and blob.get("items"):
            return blob["items"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def _write_cache(items: list) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(SECTOR_CACHE, "w", encoding="utf-8") as handle:
            json.dump({"ts": time.time(), "items": items}, handle, ensure_ascii=False)
    except OSError:
        pass


def _em_board_index_cached(force: bool = False) -> dict:
    """East Money board index, cached on disk for a day.

    Only needed to reach member tables and to translate the old BK codes, so a
    day-long cache is plenty and keeps the paged list build off the hot path.
    """
    if not force:
        try:
            with open(EM_INDEX_CACHE, "r", encoding="utf-8") as handle:
                blob = json.load(handle)
            if time.time() - blob.get("ts", 0) < CACHE_TTL and blob.get("items"):
                return {item["name"]: item for item in blob["items"]}
        except (OSError, ValueError, KeyError):
            pass

    index = _fetch_em_board_index()
    if index:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(EM_INDEX_CACHE, "w", encoding="utf-8") as handle:
                json.dump({"ts": time.time(), "items": list(index.values())},
                          handle, ensure_ascii=False)
        except OSError:
            pass
    return index


def load_sector_list(force: bool = False) -> list:
    """All A-share boards, cached on disk.

    The sweep costs roughly 1900 Tonghuashun requests (about twenty seconds)
    plus a few dozen East Money pages, so the result is cached for a week
    rather than rebuilt per run.
    """
    if not force:
        cached = _read_cache()
        if cached:
            return cached

    boards = ths.load_board_list(force)

    index, ordered = {}, []
    try:
        index = _em_board_index_cached(force)
        ordered = sorted(index.keys(), key=len)
    except ds.DataError:
        index = {}

    for board in boards:
        match = _match_em_board(board["name"], index, ordered) if index else None
        board["em_code"] = match["code"] if match else None
        board.pop("has_kline", None)

    if not boards:
        raise ds.DataError("sector list unavailable")
    _write_cache(boards)
    return boards


def us_sector_list() -> list:
    return [{
        "code": code,
        "name": cn,
        "board": "etf",
        "kind": "sector_us",
        "secid": None,
        "desc": en,
    } for code, (cn, en) in US_SECTOR_ETF.items()]


def decorate_us_sectors(boards: list) -> list:
    """Attach live quotes to the US sector ETFs.

    Tencent accepts a comma-separated batch, so all of them come back in one
    request rather than one call per ETF.
    """
    if not boards:
        return boards

    codes = ",".join("us" + b["code"] for b in boards)
    try:
        raw = ds._fetch("https://qt.gtimg.cn/q=" + codes, encoding="gbk",
                        retries=2, gap=0.1)
    except ds.DataError:
        return boards

    quotes = {}
    for chunk in raw.split(";"):
        if "=" not in chunk:
            continue
        parts = chunk.split("=", 1)[1].strip().strip('"').split("~")
        if len(parts) < 33:
            continue
        try:
            quotes[parts[2].split(".")[0].upper()] = float(parts[32])
        except (ValueError, IndexError):
            continue

    for board in boards:
        board["change_pct"] = quotes.get(board["code"].upper())
        board["live"] = board["change_pct"] is not None

    return sorted(
        boards,
        key=lambda b: -(b["change_pct"] if isinstance(b.get("change_pct"), (int, float)) else -999),
    )


_LIVE_CACHE = {"ts": 0.0, "data": {}}
_LIVE_TTL = 180


def _all_board_changes() -> dict:
    """code -> change percent for every board, cached for a few minutes.

    Tonghuashun serves one board per request, but the calls are cheap and fan
    out well, so the whole list refreshes in a couple of seconds. The short
    cache is what keeps tab-switching in the picker from refetching all of it.
    """
    now = time.time()
    if now - _LIVE_CACHE["ts"] < _LIVE_TTL and _LIVE_CACHE["data"]:
        return _LIVE_CACHE["data"]
    try:
        changes = ths.fetch_board_changes(load_sector_list())
    except (ds.DataError, ths.DataError):
        return _LIVE_CACHE["data"]
    if changes:
        _LIVE_CACHE["data"] = changes
        _LIVE_CACHE["ts"] = now
    return _LIVE_CACHE["data"]


def warm_live_quotes() -> int:
    """Refresh every board's change percent and park it in the cache.

    Called from a startup thread so the picker opens with data already in
    hand, instead of making the first visitor wait on ~480 requests.
    """
    return len(_all_board_changes())


def apply_live_quotes(boards: list) -> list:
    """Overlay fresh change percentages onto a board list.

    The board list is cached for a week, so the change percentage stored in it
    would be stale by definition. Each row carries a `live` flag so the UI can
    be honest about which values are current and which are a build-time
    snapshot.
    """
    fresh = _all_board_changes()

    for board in boards:
        value = fresh.get(board.get("code"))
        if value is not None:
            board["change_pct"] = value
            board["live"] = True
        else:
            board["live"] = False

    def sort_key(board):
        pct = board.get("change_pct")
        return -(pct if isinstance(pct, (int, float)) else -999)

    return sorted(boards, key=sort_key)


def search_sectors(keyword: str, limit: int = 25) -> list:
    """Fuzzy match over both markets. Exact code/alias wins."""
    kw = (keyword or "").strip()
    if not kw:
        return []
    upper = kw.upper()

    if upper in US_SECTOR_ETF:
        return [_us_entry(upper)]
    if upper in US_ALIAS:
        return [_us_entry(US_ALIAS[upper])]

    hits = []
    for item in us_sector_list():
        if kw in item["name"] or kw in item.get("desc", ""):
            hits.append(item)

    try:
        boards = load_sector_list()
    except ds.DataError:
        boards = []

    exact, prefix, partial = [], [], []
    for board in boards:
        name = board["name"]
        if board["code"].upper() == upper or name == kw:
            exact.append(board)
        elif name.startswith(kw):
            prefix.append(board)
        elif kw in name:
            partial.append(board)
    # Domestic boards rank first at comparable match quality: this is primarily
    # an A-share tool, with US sector ETFs as the secondary market.
    merged = exact + prefix + partial
    if len(merged) < limit:
        merged += hits
    return merged[:limit]


def _us_entry(code: str) -> dict:
    cn, en = US_SECTOR_ETF[code]
    return {"code": code, "name": cn, "board": "etf", "kind": "sector_us",
            "secid": None, "desc": en}


# Tonghuashun board indices all start with 88; no A-share ticker does, so a
# six-digit code beginning with 88 is unambiguously a board.
_THS_BOARD_RE = re.compile(r"^88\d{4}$")


def resolve_target(raw: str) -> dict:
    """Turn user input into either a stock symbol or a sector descriptor."""
    text = (raw or "").strip()
    if not text:
        raise ds.DataError("empty input")

    upper = text.upper()

    if _THS_BOARD_RE.match(text):
        for board in load_sector_list():
            if board["code"] == text:
                return board
        raise ds.DataError("unknown sector code: %s" % text)

    # Legacy East Money board codes still resolve, by name onto their
    # Tonghuashun counterpart, so old notes keep working.
    if upper.startswith("BK") and upper[2:].isdigit():
        return _resolve_em_code(upper, text)

    if upper in US_SECTOR_ETF:
        return _us_entry(upper)
    if text in US_ALIAS:
        return _us_entry(US_ALIAS[text])

    # Chinese sector name -> board lookup, only when it is clearly not a ticker.
    if not _looks_like_ticker(upper):
        hits = [h for h in search_sectors(text) if h["kind"] == "sector_cn"]
        if hits:
            return hits[0]

    symbol = ds.resolve_symbol(text)
    symbol["kind"] = "stock"
    symbol["name"] = symbol["code"]
    return symbol


def _resolve_em_code(upper: str, text: str) -> dict:
    """East Money board code -> the Tonghuashun board carrying the same name."""
    try:
        index = _em_board_index_cached()
    except ds.DataError:
        index = {}
    legacy = next((b for b in index.values() if b["code"].upper() == upper), None)
    if not legacy:
        raise ds.DataError("unknown sector code: %s" % text)

    for board in load_sector_list():
        if board["name"] == legacy["name"]:
            return board

    # No Tonghuashun counterpart - analyse the East Money board directly. Its
    # history may still be blocked, but its quote and members are intact.
    legacy = dict(legacy)
    legacy["source"] = "eastmoney"
    legacy["kind"] = "sector_cn"
    # Its own code is the East Money board code, which is what member lookup
    # needs; no Tonghuashun code is invented for it.
    legacy["em_code"] = legacy["code"]
    return legacy


def _looks_like_ticker(text: str) -> bool:
    """Guard against reading a Chinese sector name as a ticker: str.isalnum()
    is True for CJK characters, so ASCII has to be required explicitly."""
    if text.isdigit() and len(text) == 6:
        return True
    if not text.isascii():
        return False
    return len(text) <= 6 and text.replace(".", "").replace("-", "").isalnum()


# -------------------------------------------------------------- snapshot --


def _board_members(code: str, page_size: int = 500) -> tuple:
    """Member table and advance/decline split from a single request.

    Both readings come from the same member list, and that endpoint is the most
    aggressively throttled one, so splitting them into two calls would double
    the cost for no gain.
    """
    path = _CLIST.format(pn=1, pz=page_size, fid="f3", fs="b:" + code,
                         fields=_MEMBER_FIELDS)
    data = ds._fetch_em_json(path).get("data") or {}
    rows = data.get("diff") or []

    members = []
    up = down = flat = limit_up = limit_down = 0
    for item in rows:
        pct = item.get("f3")
        if isinstance(pct, (int, float)):
            if pct > 9.8:
                limit_up += 1
            elif pct < -9.8:
                limit_down += 1
            if pct > 0:
                up += 1
            elif pct < 0:
                down += 1
            else:
                flat += 1
        else:
            flat += 1
        members.append({
            "code": item.get("f12"),
            "name": item.get("f14"),
            "price": item.get("f2"),
            "change_pct": pct,
            "main_inflow": item.get("f62"),
            "pe": item.get("f9"),
            "market_cap": item.get("f20"),
        })

    total = up + down + flat
    breadth = {
        "up": up, "down": down, "flat": flat, "total": total,
        "limit_up": limit_up, "limit_down": limit_down,
        "ratio": (up / total) if total else None,
        "complete": total < page_size,
    }
    # Full list is returned: the swap logic needs the heaviest names, which are
    # not necessarily the biggest movers, so it cannot work off a sorted slice.
    return members, breadth


def _board_moneyflow(code: str) -> dict:
    path = ("api/qt/stock/fflow/kline/get?secid=90.%s&fields1=f1,f2,f3,f7"
            "&fields2=f51,f52,f53,f54,f55&klt=101&lmt=6" % code)
    data = ds._fetch_em_json(path).get("data") or {}
    rows = data.get("klines") or []
    if not rows:
        return {}
    latest = rows[-1].split(",")
    if len(latest) < 5:
        return {}

    def num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    recent = []
    for row in rows[-5:]:
        parts = row.split(",")
        if len(parts) >= 2:
            recent.append({"date": parts[0], "main": num(parts[1])})
    return {
        "date": latest[0],
        "main": num(latest[1]),
        "super": num(latest[2]) if len(latest) > 2 else None,
        "big": num(latest[3]) if len(latest) > 3 else None,
        "retail": num(latest[4]) if len(latest) > 4 else None,
        "recent": recent,
    }


KLINE_DIR = os.path.join(CACHE_DIR, "klines")
KLINE_TTL = 1800

SYNTHETIC_WEIGHT_COUNT = 20
SYNTHETIC_MIN_PICKS = 5


def _constituent_symbol(member: dict) -> str:
    """East Money member code (6 digits) -> Tencent symbol."""
    code = (member.get("code") or "").strip()
    if len(code) != 6 or not code.isdigit():
        return ""
    return ("sh" if code[0] == "6" else "sz") + code


def _synthetic_sector_klines(members: list, anchor_price=None, limit: int = 250) -> list:
    """Build a sector index proxy from its heaviest constituents.

    East Money's board history is unreachable whenever push2his throttles, and
    Tencent carries about half the boards. Weighting the largest members gives a
    series that tracks the board's trend closely enough for moving averages and
    MACD.

    The result is rescaled so its latest close matches the board's live index
    level. Without that the series would sit on an arbitrary 1000-point base and
    every derived support/resistance level would be nonsense next to the quoted
    index - off by a factor of five in testing.
    """
    picks = sorted(
        [m for m in members
         if _constituent_symbol(m) and isinstance(m.get("market_cap"), (int, float))
         and m["market_cap"] > 0],
        key=lambda m: -(m.get("market_cap") or 0),
    )[:SYNTHETIC_WEIGHT_COUNT]
    if len(picks) < SYNTHETIC_MIN_PICKS:
        return []

    def fetch_one(member):
        try:
            rows = ds.tencent_kline_by_code(_constituent_symbol(member), limit)
        except ds.DataError:
            return None
        if len(rows) < 30:
            return None
        return member.get("market_cap") or 1.0, rows

    # Fan out: twenty sequential calls took 13s, and the Tencent host is not
    # rate limited, so the only cost of concurrency here is politeness.
    with ThreadPoolExecutor(max_workers=6) as pool:
        series = [item for item in pool.map(fetch_one, picks) if item]

    if len(series) < SYNTHETIC_MIN_PICKS:
        return []

    rows = _merge_series(series)
    if rows and anchor_price:
        scale = float(anchor_price) / rows[-1]["close"]
        for row in rows:
            for key in ("open", "close", "high", "low"):
                row[key] = row[key] * scale
    return rows


def _merge_series(series: list) -> list:
    """Market-cap weighted return index, based at 1000 points."""
    dates = None
    for _, rows in series:
        own = {r["date"] for r in rows}
        dates = own if dates is None else (dates & own)
    dates = sorted(dates or [])
    if len(dates) < 30:
        return []

    books = [(cap, {r["date"]: r for r in rows}) for cap, rows in series]
    total_cap = sum(cap for cap, _ in books) or 1.0
    weights = [cap / total_cap for cap, _ in books]

    level = 1000.0
    out = []
    for index, date in enumerate(dates):
        if index == 0:
            first = [book[date] for _, book in books]
            out.append({
                "date": date, "open": level, "close": level,
                "high": level, "low": level,
                "volume": sum(b.get("volume") or 0.0 for b in first),
            })
            continue

        prev_date = dates[index - 1]
        move = up_move = down_move = 0.0
        volume = 0.0
        for weight, (_, book) in zip(weights, books):
            cur, prev = book.get(date), book.get(prev_date)
            if not cur or not prev or not prev.get("close"):
                continue
            base = prev["close"]
            move += weight * (cur["close"] / base - 1.0)
            up_move += weight * ((cur.get("high") or cur["close"]) / base - 1.0)
            down_move += weight * ((cur.get("low") or cur["close"]) / base - 1.0)
            volume += cur.get("volume") or 0.0

        previous = level
        level = level * (1.0 + move)
        out.append({
            "date": date,
            "open": previous,
            "close": level,
            "high": previous * (1.0 + max(up_move, move)),
            "low": previous * (1.0 + min(down_move, move)),
            "volume": volume,
        })
    return out


def _sector_klines(board: dict, members: list, anchor_price=None,
                   limit: int = 250) -> tuple:
    """Board history with a short disk cache. Returns (rows, source)."""
    key = board.get("ths_code") or board.get("code") or "unknown"
    path = os.path.join(KLINE_DIR, "%s.json" % key)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
        if time.time() - blob.get("ts", 0) < KLINE_TTL and blob.get("rows"):
            return blob["rows"], blob.get("source")
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    rows, source = _fetch_sector_klines(board, members, anchor_price, limit)
    try:
        os.makedirs(KLINE_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"ts": time.time(), "rows": rows, "source": source},
                      handle, ensure_ascii=False)
    except OSError:
        pass
    return rows, source


def _fetch_sector_klines(board: dict, members: list, anchor_price, limit: int) -> tuple:
    """Board history, most accurate source first.

    1. Tonghuashun board index - a real index for every live board, and it
       does not throttle. This is the normal path now, not the lucky one.
    2. Tencent board index - kept for older board records carrying a pt code.
    3. Constituent-weighted proxy - the last resort it was always meant to be.

    East Money's own board history is no longer consulted at all: it was the
    source that kept blocking, and Tonghuashun covers everything it did.
    """
    errors = []

    ths_code = board.get("ths_code")
    if not ths_code and _THS_BOARD_RE.match(str(board.get("code") or "")):
        ths_code = "bk_" + str(board["code"])
    if ths_code:
        try:
            rows = ths.fetch_board_klines(ths_code, limit)
            if len(rows) >= 20:
                return rows, "ths"
        except ths.DataError as exc:
            errors.append("ths: %s" % exc)

    tx_code = board.get("tx_code")
    if tx_code:
        try:
            return ds.tencent_kline_by_code(tx_code, limit), "tencent"
        except ds.DataError as exc:
            errors.append("tencent: %s" % exc)

    rows = _synthetic_sector_klines(members or [], anchor_price, limit)
    if rows:
        return rows, "synthetic"

    raise ds.DataError("; ".join(errors) or "no kline source for %s" % board.get("code"))


def build_sector_snapshot(target: dict) -> dict:
    """Full sector snapshot, shaped like a stock snapshot."""
    if target.get("kind") == "sector_us":
        return _build_us_sector(target)
    return _build_cn_sector(target)


def _build_us_sector(target: dict) -> dict:
    """US sectors ride on their ETF, which Tencent carries reliably."""
    snap = ds.build_market_snapshot(target["code"])
    snap["target"] = {
        "kind": "sector_us", "code": target["code"], "name": target["name"],
        "board": "etf", "desc": target.get("desc", ""),
    }
    snap["quote"]["name"] = "%s板块（%s）" % (target["name"], target["code"])
    snap["quote"]["kind"] = "sector_us"
    snap["quote"]["currency"] = "USD"
    snap["breadth"] = None
    snap["members"] = []
    snap["moneyflow"] = {}
    # US sectors ride the stock path, which is Tonghuashun-backed now.
    snap["kline_source"] = ("ths" if (snap.get("quote") or {}).get("source") == "ths"
                            else "tencent")
    return snap


def _em_sector_quote(secid):
    """Board quote from East Money, in our own quote shape.

    Only reached when a board has no Tonghuashun counterpart, or when
    Tonghuashun is having a moment. Being on the `push2` host, this is not the
    endpoint that ever throttled.
    """
    if not secid:
        return None
    try:
        data = ds._fetch_em_json(ds._quote_path(secid)).get("data") or {}
    except ds.DataError:
        return None
    if not data:
        return None
    return {
        "code": data.get("f57"),
        "name": data.get("f58"),
        "price": ds._num(data.get("f43")),
        "prev_close": ds._num(data.get("f60")),
        "change_pct": ds._num(data.get("f170")),
        "open": ds._num(data.get("f46")),
        "high": ds._num(data.get("f44")),
        "low": ds._num(data.get("f45")),
        "volume": ds._num(data.get("f47")),
        "turnover": ds._num(data.get("f48")),
        "source": "eastmoney",
    }


def _build_cn_sector(target: dict) -> dict:
    code = target["code"]
    # Only Tonghuashun codes get the bk_ prefix - a legacy East Money board
    # has no Tonghuashun counterpart by definition.
    ths_code = target.get("ths_code")
    if not ths_code and _THS_BOARD_RE.match(str(code)):
        ths_code = "bk_" + str(code)
    em_code = target.get("em_code") or (
        code if str(target.get("source")) == "eastmoney" else None)

    quote = {}
    try:
        quote = ths.fetch_board_quote(ths_code)
    except ths.DataError:
        quote = {}
    if quote.get("price") is None:
        quote = _em_sector_quote(target.get("secid")) or quote
    if quote.get("price") is None:
        raise ds.DataError("no quote for sector %s" % code)

    price = quote.get("price")
    prev = quote.get("prev_close")
    change = (price - prev) if (price is not None and prev) else None

    # Members are fetched first: their market caps drive the synthetic kline
    # fallback below, so they have to be in hand before history is requested.
    # They come from East Money by board name - see the module docstring.
    members, breadth, moneyflow = [], {}, {}
    if em_code:
        try:
            members, breadth = _board_members(em_code)
        except ds.DataError:
            pass
        try:
            moneyflow = _board_moneyflow(em_code)
        except ds.DataError:
            pass

    klines, kline_source, kline_error = [], None, None
    try:
        klines, kline_source = _sector_klines(target, members, price, 250)
    except ds.DataError as exc:
        kline_error = "板块K线暂不可用：%s" % exc

    name = target.get("name") or quote.get("name") or code
    snapshot = {
        "target": {
            "kind": "sector_cn", "code": code, "name": name,
            "board": target.get("board", "industry"),
            "secid": target.get("secid"), "em_code": em_code,
        },
        "quote": {
            "secid": target.get("secid"),
            "code": code,
            "name": "%s板块" % name,
            "market": "CN",
            "kind": "sector_cn",
            "price": price,
            "prev_close": prev,
            "change": change,
            "change_pct": quote.get("change_pct"),
            "open": quote.get("open"),
            "high": quote.get("high"),
            "low": quote.get("low"),
            "volume": quote.get("volume"),
            "turnover": quote.get("turnover"),
            "market_cap": None,
            "float_cap": None,
            "pe": None,
            "pb": None,
            "currency": "点",
            "source": quote.get("source", "ths"),
        },
        "klines": klines,
        "members": members,
        "breadth": breadth,
        "moneyflow": moneyflow,
    }
    if kline_source:
        snapshot["kline_source"] = kline_source
    if kline_error:
        snapshot["kline_error"] = kline_error
    if klines:
        return _attach_indicators(snapshot, klines)

    # Degrade rather than fail: the board quote and members are still useful.
    snapshot.update({
        "indicators": {}, "levels": {}, "bars": 0,
        "first_date": None, "last_date": None,
    })
    return snapshot


def _attach_indicators(snapshot: dict, klines: list) -> dict:
    """Reuse the stock indicator stack on the sector index series."""
    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    price = snapshot["quote"].get("price") or closes[-1]

    ma5, ma10, ma20, ma60 = (ds.sma(closes, n) for n in (5, 10, 20, 60))
    macd_v = ds.macd(closes)
    rsi_v = ds.rsi(closes)
    boll = ds.bollinger(closes)
    atr_v = ds.atr(klines)

    win20h, win20l = max(highs[-20:]), min(lows[-20:])
    win60h, win60l = max(highs[-60:]), min(lows[-60:])
    win20_vol = sum(k["volume"] for k in klines[-20:]) / min(20, len(klines))
    win5_vol = sum(k["volume"] for k in klines[-5:]) / min(5, len(klines))

    def pct(a, b):
        return ds._pct(a, b)

    below = [v for v in (win20l, ma20, boll["lower"] if boll else None, win60l)
             if v is not None and v < price]
    above = [v for v in (win20h, ma60, boll["upper"] if boll else None, win60h)
             if v is not None and v > price]
    support = max(below) if below else win60l
    resistance = min(above) if above else win60h
    if support >= resistance:
        support, resistance = win60l, win60h

    snapshot["indicators"] = {
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "macd": macd_v, "rsi14": rsi_v, "boll": boll, "atr14": atr_v,
        "last_close": closes[-1],
        "change_5d_pct": pct(closes[-1], closes[-6]) if len(closes) > 6 else None,
        "change_20d_pct": pct(closes[-1], closes[-21]) if len(closes) > 21 else None,
        "change_60d_pct": pct(closes[-1], closes[-61]) if len(closes) > 61 else None,
        "high_20d": win20h, "low_20d": win20l,
        "high_60d": win60h, "low_60d": win60l,
        "vol_ratio_5_20": (win5_vol / win20_vol) if win20_vol else None,
    }
    snapshot["levels"] = {
        "anchor": price,
        "support": support,
        "resistance": resistance,
        "support_far": win60l,
        "resistance_far": win60h,
        "stop_loss": support * 0.98 if support else None,
        "target_low": resistance,
        "target_high": (resistance + 3.0 * atr_v) if (resistance and atr_v) else win60h,
        "atr": atr_v,
    }
    snapshot["bars"] = len(klines)
    snapshot["first_date"] = klines[0]["date"]
    snapshot["last_date"] = klines[-1]["date"]
    return snapshot

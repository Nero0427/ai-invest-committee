"""Whole-market snapshot and the daily billboard, backing the dashboard pages.

Two feeds live here.

The heat map needs every listed A-share - roughly 5900 rows. East Money's
`push2delay` host serves them 100 at a time, and paging it across ten workers
returns the whole market in a couple of seconds. The live `push2` host refused
the identical query outright, and its ~15 minute lag is invisible on a heat
map, so the delayed host is the right trade here.

The billboard (龙虎榜) comes from East Money's datacenter API. That endpoint
demands an explicit trade date, so the most recent date that actually carries
data is probed once and then remembered - weekends and holidays simply have
none.

Payloads going out to the browser use one-letter keys: 1500 rows times four
fields is already a sizeable JSON document, and the names are meaningless to
the client anyway.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import ths

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_TIMEOUT = 20


class DataError(Exception):
    """Raised when market-wide data cannot be fetched."""


def _num(value):
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _get_json(url: str, referer: str = "https://quote.eastmoney.com/",
              retries: int = 2) -> dict:
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Referer": referer, "Accept": "*/*"})
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - retried below
            last = exc
            if attempt < retries - 1:
                time.sleep(0.3 * (attempt + 1))
    raise DataError("network error: %s: %s" % (type(last).__name__, last))


def _get(url: str, referer: str = "https://quote.eastmoney.com/",
         encoding: str = "utf-8", retries: int = 2) -> str:
    """Fetch a URL as text.

    Sina and Tencent both answer in GBK, East Money in UTF-8, so callers that
    know better pass `encoding` rather than being surprised later.
    """
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": _UA, "Referer": referer, "Accept": "*/*"})
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return response.read().decode(encoding, "replace")
        except Exception as exc:  # noqa: BLE001 - retried below
            last = exc
            if attempt < retries - 1:
                time.sleep(0.3 * (attempt + 1))
    raise DataError("network error: %s: %s" % (type(last).__name__, last))


# ------------------------------------------------------------ whole market --


_CLIST = ("https://push2delay.eastmoney.com/api/qt/clist/get"
          "?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields={fields}")

# Shenzhen main board, ChiNext, Shanghai main board, STAR, Beijing exchange.
# Indices and B shares are deliberately left out.
_FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_FIELDS = "f12,f14,f2,f3,f20,f21,f100,f8,f6"

_PAGE_WORKERS = 10
_MARKET_TTL = 45

_market_lock = threading.Lock()
_market_cache = {"ts": 0.0, "rows": None, "total": 0}


def _fetch_page(page: int):
    url = _CLIST.format(pn=page, fs=_FS_ALL_A, fields=_FIELDS)
    data = _get_json(url).get("data") or {}
    rows = []
    for item in data.get("diff") or []:
        code = str(item.get("f12") or "")
        if len(code) != 6 or not code.isdigit():
            continue
        rows.append({
            "code": code,
            "name": item.get("f14") or code,
            "price": _num(item.get("f2")),
            "change_pct": _num(item.get("f3")),
            "market_cap": _num(item.get("f20")),
            "float_cap": _num(item.get("f21")),
            "industry": (item.get("f100") or "").strip() or "其他",
            "turnover": _num(item.get("f8")),
            "amount": _num(item.get("f6")),
        })
    return _num(data.get("total")), rows


def fetch_all_stocks(force: bool = False) -> tuple:
    """(rows, total) covering every listed A-share, cached for a short while.

    The heat map refreshes on a timer, so a 45 second cache keeps the upstream
    load at a sane level while still feeling live.
    """
    with _market_lock:
        if (not force and _market_cache["rows"]
                and time.time() - _market_cache["ts"] < _MARKET_TTL):
            return _market_cache["rows"], _market_cache["total"]

    total, first = _fetch_page(1)
    if not first:
        raise DataError("market snapshot unavailable")

    pages = min(max(1, (int(total or len(first)) + 99) // 100), 80)
    rows = list(first)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=_PAGE_WORKERS) as pool:
            for _, chunk in pool.map(_fetch_page, range(2, pages + 1)):
                rows.extend(chunk)

    with _market_lock:
        _market_cache.update({"ts": time.time(), "rows": rows,
                              "total": int(total or len(rows))})
    return rows, int(total or len(rows))


def market_stats() -> dict:
    """Advance/decline split and the day's extremes, for the dashboard header."""
    rows, total = fetch_all_stocks()
    up = down = flat = limit_up = limit_down = 0
    amount = 0.0
    for row in rows:
        pct = row.get("change_pct")
        if pct is None:
            flat += 1
            continue
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        if pct >= 9.8:
            limit_up += 1
        elif pct <= -9.8:
            limit_down += 1
        amount += row.get("amount") or 0.0
    return {
        "total": total,
        "up": up, "down": down, "flat": flat,
        "limit_up": limit_up, "limit_down": limit_down,
        "amount": amount,
    }


def build_treemap(limit: int = 1500) -> dict:
    """Industry-grouped slice of the market, sized for the heat map.

    Only the heaviest `limit` names survive: the rest are invisible at any
    sane canvas size, and shipping them would triple the payload for nothing.
    Cumulative coverage is reported so the UI can say what it is showing.
    """
    rows, total = fetch_all_stocks()
    usable = [r for r in rows if (r.get("market_cap") or 0) > 0]
    usable.sort(key=lambda r: -r["market_cap"])

    all_cap = sum(r["market_cap"] for r in usable) or 1.0
    picked = usable[:max(1, limit)]
    covered = sum(r["market_cap"] for r in picked) / all_cap

    groups = {}
    for row in picked:
        groups.setdefault(row["industry"], []).append(row)

    out = []
    for name, items in groups.items():
        cap_sum = sum(i["market_cap"] for i in items)
        weighted = sum((i.get("change_pct") or 0.0) * i["market_cap"] for i in items)
        out.append({
            "g": name,
            "v": cap_sum,
            "p": (weighted / cap_sum) if cap_sum else None,
            "n": len(items),
            "s": [{"c": i["code"], "n": i["name"],
                   "p": i.get("change_pct"), "v": i["market_cap"]} for i in items],
        })
    out.sort(key=lambda group: -group["v"])

    return {
        "updated": time.strftime("%H:%M:%S"),
        "total": total,
        "shown": len(picked),
        "covered": round(covered * 100, 1),
        "groups": out,
    }


def top_movers(limit: int = 20) -> dict:
    """Biggest gainers and losers, plus the heaviest turnover."""
    rows, _ = fetch_all_stocks()
    live = [r for r in rows if r.get("change_pct") is not None and r.get("price")]

    def brief(row):
        return {
            "code": row["code"], "name": row["name"], "price": row.get("price"),
            "change_pct": row.get("change_pct"), "market_cap": row.get("market_cap"),
            "turnover": row.get("turnover"), "amount": row.get("amount"),
            "industry": row.get("industry"),
        }

    by_pct = sorted(live, key=lambda r: -r["change_pct"])
    by_amount = sorted(live, key=lambda r: -(r.get("amount") or 0))
    return {
        "gainers": [brief(r) for r in by_pct[:limit]],
        "losers": [brief(r) for r in by_pct[-limit:][::-1]],
        "active": [brief(r) for r in by_amount[:limit]],
    }


# --------------------------------------------------------------- billboard --

_BILLBOARD = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
              "?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL"
              "&pageNumber=1&pageSize=200&sortColumns=BILLBOARD_NET_AMT"
              "&sortTypes=-1&source=WEB&client=WEB&filter={filter}")

_billboard_lock = threading.Lock()
_billboard_cache = {"date": None, "ts": 0.0, "items": None}
_BILLBOARD_TTL = 600


def _billboard_filter(day: str) -> str:
    return urllib.parse.quote("(TRADE_DATE='%s')" % day, safe="")


def _latest_trade_date() -> str:
    """The newest date that actually carries billboard rows."""
    cached = _billboard_cache.get("date")
    if cached:
        return cached
    now = time.time()
    for back in range(0, 12):
        day = time.strftime("%Y-%m-%d", time.localtime(now - back * 86400))
        try:
            data = _get_json(_BILLBOARD.format(filter=_billboard_filter(day)),
                             referer="https://data.eastmoney.com/")
        except DataError:
            continue
        if ((data.get("result") or {}).get("data")):
            return day
    raise DataError("no billboard rows found in the last 12 days")


def fetch_billboard(force: bool = False) -> dict:
    """Today's (or the most recent) billboard rows."""
    with _billboard_lock:
        if (not force and _billboard_cache["items"]
                and time.time() - _billboard_cache["ts"] < _BILLBOARD_TTL):
            return {"date": _billboard_cache["date"], "items": _billboard_cache["items"]}

    day = _latest_trade_date()
    data = _get_json(_BILLBOARD.format(filter=_billboard_filter(day)),
                     referer="https://data.eastmoney.com/")
    rows = ((data.get("result") or {}).get("data")) or []

    items = []
    for row in rows:
        code = str(row.get("SECURITY_CODE") or "")
        if not code:
            continue
        items.append({
            "code": code,
            "name": row.get("SECURITY_NAME_ABBR") or code,
            "date": (row.get("TRADE_DATE") or "")[:10],
            "price": _num(row.get("CLOSE_PRICE")),
            "change_pct": _num(row.get("CHANGE_RATE")),
            "net_amt": _num(row.get("BILLBOARD_NET_AMT")),
            "buy_amt": _num(row.get("BILLBOARD_BUY_AMT")),
            "sell_amt": _num(row.get("BILLBOARD_SELL_AMT")),
            "amount": _num(row.get("ACCUM_AMOUNT")),
            "turnover": _num(row.get("TURNOVERRATE")),
            "float_cap": _num(row.get("FREE_MARKET_CAP")),
            "reason": row.get("EXPLANATION") or "",
            "after_1d": _num(row.get("D1_CLOSE_ADJCHRATE")),
            "after_5d": _num(row.get("D5_CLOSE_ADJCHRATE")),
            "after_10d": _num(row.get("D10_CLOSE_ADJCHRATE")),
        })

    with _billboard_lock:
        _billboard_cache.update({"date": day, "ts": time.time(), "items": items})
    return {"date": day, "items": items}


def warm_cache() -> str:
    """Pull both feeds once, off the request path."""
    fetch_all_stocks()
    day = _latest_trade_date()
    fetch_billboard()
    return day


# ------------------------------------------------------------- US sectors --


# US sectors ride on the ETFs the industry itself uses, grouped the way a
# trader would group them. Every tile is deliberately the same size: any
# weighting here would be invented, and an honest equal-weight grid beats a
# hierarchy nobody can verify.
US_SECTOR_GROUPS = (
    ("宽基指数", (("SPY", "标普500"), ("QQQ", "纳斯达克100"),
                  ("IWM", "罗素2000"), ("DIA", "道琼斯"))),
    ("科技", (("XLK", "科技"), ("SMH", "半导体"), ("SOXX", "半导体·iShares"),
              ("IGV", "软件"), ("ARKK", "创新ETF"), ("SKYY", "云计算"))),
    ("医疗健康", (("XLV", "医疗保健"), ("XBI", "生物科技"),
                  ("IBB", "生物科技·iShares"))),
    ("金融", (("XLF", "金融"), ("KRE", "区域银行"), ("KBE", "银行"))),
    ("能源", (("XLE", "能源"), ("XOP", "油气开采"), ("USO", "原油"))),
    ("原材料与贵金属", (("GLD", "黄金"), ("SLV", "白银"), ("GDX", "金矿"),
                        ("XLB", "原材料"), ("CPER", "铜"))),
    ("工业", (("XLI", "工业"), ("ITA", "航空军工"))),
    ("消费", (("XLY", "可选消费"), ("XLP", "必需消费"), ("XRT", "零售"))),
    ("公用事业", (("XLU", "公用事业"),)),
    ("房地产", (("XLRE", "房地产"),)),
    ("海外与债券", (("TLT", "20年美债"), ("EEM", "新兴市场"),
                    ("FXI", "中国大盘"), ("KWEB", "中概互联"))),
)

_US_TTL = 20
_us_cache = {"ts": 0.0, "quotes": None}


def fetch_us_sector_quotes(force: bool = False) -> dict:
    """Live quotes for every US sector ETF.

    Tonghuashun, not Tencent. During the opening minutes Tencent's US feed
    reports the previous close equal to the last price, which makes every
    single change read as a flat 0.00% - verified side by side, where Tencent
    had XLK at 187.67/187.67 and Tonghuashun at 187.67/185.22 (+1.32%). The
    cost is 35 small requests instead of one batched one, which fans out fine.
    """
    with _market_lock:
        if (not force and _us_cache["quotes"]
                and time.time() - _us_cache["ts"] < _US_TTL):
            return _us_cache["quotes"]

    codes = [code for _, items in US_SECTOR_GROUPS for code, _ in items]

    def one(code):
        try:
            quote = ths.fetch_us_quote(code, gap=0.0)
            return code, {"name": quote.get("name") or code,
                          "price": quote.get("price"),
                          "change_pct": quote.get("change_pct")}
        except Exception:  # noqa: BLE001 - one dead ticker should not sink the map
            return code, None

    quotes = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        for code, quote in pool.map(one, codes):
            if quote:
                quotes[code] = quote

    with _market_lock:
        _us_cache.update({"ts": time.time(), "quotes": quotes})
    return quotes


def build_us_treemap() -> dict:
    """Sector heat map for the US market."""
    quotes = fetch_us_sector_quotes()
    groups = []
    for group_name, items in US_SECTOR_GROUPS:
        stocks = []
        for code, label in items:
            quote = quotes.get(code) or {}
            stocks.append({
                "c": code, "n": label,
                "p": quote.get("change_pct"),
                "v": 1.0,  # equal weight; see the note on US_SECTOR_GROUPS
            })
        cap = sum(s["v"] for s in stocks) or 1.0
        weighted = sum((s["p"] or 0.0) * s["v"] for s in stocks)
        groups.append({"g": group_name, "v": cap, "p": weighted / cap,
                       "n": len(stocks), "s": stocks})
    groups.sort(key=lambda g: -g["v"])

    return {
        "updated": time.strftime("%H:%M:%S"),
        "market": "us",
        "total": len(groups),
        "shown": sum(g["n"] for g in groups),
        "covered": 100.0,
        "groups": groups,
    }


# ------------------------------------------------------------ commodities --


# Two providers here. East Money carries the domestic futures and the COMEX
# contracts; Sina carries the London spot pairs, which East Money does not
# list at all.
COMMODITIES = (
    {"code": "hf_XAU", "name": "伦敦金", "unit": "美元/盎司", "source": "sina", "group": "贵金属"},
    {"code": "hf_XAG", "name": "伦敦银", "unit": "美元/盎司", "source": "sina", "group": "贵金属"},
    {"code": "113.aum", "name": "沪金主连", "unit": "元/克", "source": "em", "group": "贵金属"},
    {"code": "113.agm", "name": "沪银主连", "unit": "元/千克", "source": "em", "group": "贵金属"},
    {"code": "101.GC00Y", "name": "COMEX 黄金", "unit": "美元/盎司", "source": "em", "group": "贵金属"},
    {"code": "101.SI00Y", "name": "COMEX 白银", "unit": "美元/盎司", "source": "em", "group": "贵金属"},
    {"code": "113.cum", "name": "沪铜主连", "unit": "元/吨", "source": "em", "group": "有色"},
)


def _commodity_quote(item: dict) -> dict:
    """One commodity quote, normalised to the project's shape."""
    if item["source"] == "sina":
        raw = _get("https://hq.sinajs.cn/list=" + item["code"],
                   referer="https://finance.sina.com.cn/", encoding="gbk")
        body = raw.split('"', 1)[1].rsplit('"', 1)[0] if '"' in raw else ""
        parts = body.split(",")
        if len(parts) < 8:
            raise DataError("no sina quote for %s" % item["code"])
        price = float(parts[0]) if parts[0] else None
        prev = float(parts[7]) if parts[7] else None
    else:
        url = ("https://push2delay.eastmoney.com/api/qt/stock/get?secid=%s"
               "&fields=f43,f57,f58,f60,f170,f44,f45&invt=2&fltt=2" % item["code"])
        data = _get_json(url).get("data") or {}
        price = _num(data.get("f43"))
        prev = _num(data.get("f60"))

    pct = None
    if price is not None and prev:
        pct = (price / prev - 1.0) * 100.0
    return {
        "code": item["code"], "name": item["name"], "unit": item["unit"],
        "group": item["group"], "price": price, "prev_close": prev,
        "change_pct": pct, "kind": "commodity", "source": item["source"],
    }


def fetch_commodities() -> list:
    """Every tracked commodity, quoted concurrently."""
    def one(item):
        try:
            return _commodity_quote(item)
        except Exception:  # noqa: BLE001 - one dead feed should not sink the list
            return None

    with ThreadPoolExecutor(max_workers=len(COMMODITIES)) as pool:
        return [row for row in pool.map(one, COMMODITIES) if row]


# ------------------------------------------------------- billboard seats --


_SEATS = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
          "?reportName=%s&columns=ALL&pageNumber=1&pageSize=60"
          "&source=WEB&client=WEB&filter=%s")

_seat_lock = threading.Lock()
_seat_cache = {}


def fetch_billboard_seats(code: str, day: str) -> dict:
    """Buy and sell seats behind one billboard entry.

    The list endpoint only names the seats as opaque codes; the names live in
    two separate reports, one per side, which is why this costs two requests.
    """
    key = "%s|%s" % (code, day)
    with _seat_lock:
        cached = _seat_cache.get(key)
        if cached and time.time() - cached["ts"] < _BILLBOARD_TTL:
            return cached["data"]

    def side(report):
        query = urllib.parse.quote(
            "(TRADE_DATE='%s')(SECURITY_CODE=\"%s\")" % (day, code), safe="")
        data = _get_json(_SEATS % (report, query),
                         referer="https://data.eastmoney.com/")
        rows = ((data.get("result") or {}).get("data")) or []
        out = []
        for row in rows:
            out.append({
                "name": row.get("OPERATEDEPT_NAME") or "—",
                "buy": _num(row.get("BUY")),
                "sell": _num(row.get("SELL")),
                "net": _num(row.get("NET")),
            })
        out.sort(key=lambda r: -(abs(r["net"] or 0)))
        return out

    with ThreadPoolExecutor(max_workers=2) as pool:
        buy_future = pool.submit(side, "RPT_BILLBOARD_DAILYDETAILSBUY")
        sell_future = pool.submit(side, "RPT_BILLBOARD_DAILYDETAILSSELL")
        result = {"code": code, "date": day,
                  "buy": buy_future.result(), "sell": sell_future.result()}

    with _seat_lock:
        _seat_cache[key] = {"ts": time.time(), "data": result}
    return result

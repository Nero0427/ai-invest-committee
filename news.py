"""News layer: Jin10 (金十数据) flash feed.

Jin10's flash API returns structured Chinese-language bulletins, which makes it
a better fit than scraping a headlines page. Two endpoints are used: the
primary returns full Chinese text, the fallback returns the same stream with a
lighter payload.

Everything is best-effort: if the feed is unreachable the committee still runs,
and the news analyst says so plainly rather than inventing events.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse

import datasource as ds

FLASH_API = "https://flash-api.jin10.com/get_flash_list?channel=-8200&vip=1"
FLASH_FALLBACK = "https://www.jin10.com/flash_newest.js"

_HEADERS = {
    "x-app-id": "bVBF4FyRTn5NJF5n",
    "x-version": "1.0.0",
    "Referer": "https://www.jin10.com/",
    "Origin": "https://www.jin10.com",
}

_CACHE = {"ts": 0.0, "items": []}
MEM_TTL = 600
DISK_TTL = 1800

# The feed returns a fixed 21 bulletins per page and ignores every page-size
# parameter tried (count / limit / size / page_size), so reaching a thousand
# means walking ~50 pages. Measured: 55 pages in 13.9s with no throttling.
TARGET_ITEMS = 1000
MAX_PAGES = 60
PAGE_GAP = 0.12

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "cache", "jin10_flash.json")


def _load_disk():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
        items = blob.get("items") or []
        if items:
            return items, blob.get("ts", 0.0)
    except (OSError, json.JSONDecodeError):
        pass
    return [], 0.0


def _save_disk(items: list, ts: float) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump({"ts": ts, "items": items}, handle, ensure_ascii=False)
    except OSError:
        pass


def _parse_entry(entry: dict):
    entry_id = entry.get("id")
    content = clean_html((entry.get("data") or {}).get("content") or "")
    if not entry_id or not content:
        return None  # VIP-locked or media-only entry
    return {
        "id": entry_id,
        "time": entry.get("time"),
        "content": content,
        "important": int(entry.get("important") or 0),
    }


def _page(max_time=None) -> list:
    url = FLASH_API
    if max_time:
        url += "&max_time=" + urllib.parse.quote(max_time)
    raw = ds._fetch(url, headers=_HEADERS, retries=2, gap=PAGE_GAP)
    return (json.loads(raw).get("data")) or []


def _fallback_items() -> list:
    raw = ds._fetch(FLASH_FALLBACK, headers={"Referer": "https://www.jin10.com/"},
                    retries=2, gap=0.1)
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        raise ds.DataError("jin10 fallback payload unparsable")
    out = []
    for entry in json.loads(raw[start:end + 1]):
        parsed = _parse_entry(entry)
        if parsed:
            out.append(parsed)
    return out


def _walk(limit: int, previous: list = None) -> list:
    """Page backwards until the limit is reached or the previous snapshot is
    caught up with.

    The early exit is what keeps repeat runs cheap: a warm cache means the
    first page or two already covers everything new, so a refresh costs two
    requests instead of fifty.
    """
    previous = previous or []
    known = {item.get("id") for item in previous if item.get("id")}
    fresh, seen = [], set()
    max_time = None
    reached_known = False

    for _ in range(MAX_PAGES):
        try:
            rows = _page(max_time)
        except Exception:
            break
        if not rows:
            break

        for entry in rows:
            entry_id = entry.get("id")
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            if entry_id in known:
                reached_known = True
                continue
            parsed = _parse_entry(entry)
            if parsed:
                fresh.append(parsed)

        if reached_known or len(fresh) >= limit:
            break
        max_time = rows[-1].get("time")

    # De-duplicate against the freshly fetched batch only. `seen` also holds
    # ids that merely appeared on a fetched page but were already cached, and
    # filtering on that shaved a couple of bulletins off the list every refresh.
    fresh_ids = {item.get("id") for item in fresh}
    tail = [item for item in previous if item.get("id") not in fresh_ids]
    return (fresh + tail)[:limit]


def fetch_flash(limit: int = TARGET_ITEMS, force: bool = False) -> list:
    """Bulletins, newest first, spanning roughly the last few days."""
    now = time.time()

    if not force and _CACHE["items"] and now - _CACHE["ts"] < MEM_TTL:
        return _CACHE["items"][:limit]

    disk_items, disk_ts = _load_disk()
    if not force and disk_items and now - disk_ts < DISK_TTL:
        _CACHE["items"], _CACHE["ts"] = disk_items, disk_ts
        return disk_items[:limit]

    # A short cache means the previous snapshot never reached the target size,
    # so it cannot serve as a stopping point - rebuild from scratch instead,
    # otherwise the list would stay stuck below the limit forever.
    previous = disk_items if len(disk_items) >= limit else []
    try:
        items = _walk(limit, previous)
    except Exception:
        items = []

    if not items:
        try:
            items = _fallback_items()
        except Exception:
            items = []
    if not items:
        if disk_items:
            return disk_items[:limit]
        raise ds.DataError("news feed unavailable")

    _CACHE["items"], _CACHE["ts"] = items, now
    _save_disk(items, now)
    return items[:limit]


def warm_cache() -> int:
    """Preload the feed in the background so the first analysis is not blocked."""
    try:
        return len(fetch_flash())
    except Exception:
        return 0

# Terms that make a bulletin relevant regardless of the target.
MACRO_TERMS = ("美联储", "央行", "降息", "加息", "通胀", "关税", "A股", "美股",
               "港股", "原油", "黄金", "汇率", "人民币", "国债", "GDP", "PMI",
               "国务院", "证监会", "关税", "非农", "CPI")


def clean_html(text: str) -> str:
    """Strip Jin10's inline markup, which includes <b>, <br/> and section spans."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


# ----------------------------------------------------------- matching --


def _strip_board_suffix(name: str) -> str:
    for suffix in ("板块", "Ⅲ", "Ⅱ", "Ⅰ", "指数"):
        name = name.replace(suffix, "")
    return name.strip(" ()（）")


def keywords_for(snapshot: dict) -> list:
    """Search terms for tying the news stream to a target."""
    words = []
    target = snapshot.get("target") or {}
    name = _strip_board_suffix(target.get("name") or "")
    if len(name) >= 2:
        words.append(name)
        if len(name) >= 4:
            words.append(name[:2])

    # Member names make the match much sharper for sector queries, and with a
    # thousand bulletins in hand a wider keyword set pays off.
    for member in (snapshot.get("members") or [])[:20]:
        member_name = (member.get("name") or "").strip()
        if len(member_name) >= 2:
            words.append(member_name)

    quote_name = _strip_board_suffix((snapshot.get("quote") or {}).get("name") or "")
    if len(quote_name) >= 2 and quote_name not in words:
        words.append(quote_name)

    seen, unique = set(), []
    for word in words:
        if word not in seen:
            seen.add(word)
            unique.append(word)
    return unique


def related_news(snapshot: dict, items: list = None, limit: int = 15) -> dict:
    """Split the stream into target-specific and macro bulletins.

    Returns both so the analyst can distinguish "news about this sector" from
    "market-wide context", instead of pretending everything is relevant.
    """
    if items is None:
        items = fetch_flash()

    keywords = keywords_for(snapshot)
    matched, macro = [], []
    for item in items:
        text = item["content"]
        hit = next((k for k in keywords if k in text), None)
        if hit:
            row = dict(item)
            row["matched"] = hit
            matched.append(row)
        elif any(term in text for term in MACRO_TERMS):
            macro.append(item)

    matched.sort(key=lambda x: (-x.get("important", 0), x.get("time") or ""))
    macro.sort(key=lambda x: (-x.get("important", 0), x.get("time") or ""))

    return {
        "keywords": keywords,
        "matched": matched[:limit],
        "macro": macro[:limit],
        "total_scanned": len(items),
    }

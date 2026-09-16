"""Unified entry point: turn any user input into a snapshot.

Accepts an A-share or US ticker, an A-share board (by BK/tencent code or
Chinese name) or a US sector ETF, and returns the same snapshot shape so the
agents never need to branch on instrument type.
"""

from __future__ import annotations

import datasource as ds
import sectors


def build(raw: str) -> dict:
    target = sectors.resolve_target(raw)

    if str(target.get("kind", "")).startswith("sector"):
        return sectors.build_sector_snapshot(target)

    snap = ds.build_market_snapshot(raw)
    quote = snap.get("quote") or {}
    snap["target"] = {
        "kind": quote.get("kind") or "stock",
        "code": quote.get("code") or raw,
        "name": quote.get("name") or raw,
        "board": None,
    }
    snap["breadth"] = None
    snap["members"] = []
    snap["moneyflow"] = {}
    # Bars follow the quote provider for stocks: A-shares are served by
    # Tonghuashun, everything else by Tencent.
    snap["kline_source"] = "ths" if quote.get("source") == "ths" else "tencent"
    return snap


def describe(snap: dict) -> str:
    """Short label for logs and the UI header."""
    target = snap.get("target") or {}
    kind = target.get("kind") or "stock"
    label = {
        "stock": "个股",
        "sector_cn": "A股板块",
        "sector_us": "美股板块",
    }.get(kind, "标的")
    return "%s %s" % (label, target.get("name") or target.get("code") or "")

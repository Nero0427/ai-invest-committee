"""Smoke test for the market data layer."""
import json
import sys

sys.path.insert(0, ".")
import datasource as ds


def show(sym):
    print("=" * 60)
    print("symbol:", sym)
    snap = ds.build_market_snapshot(sym)
    q = snap["quote"]
    ind = snap["indicators"]
    lv = snap["levels"]
    print("name          :", q["name"], "|", q["code"], "|", q["currency"])
    print("price         :", q["price"], "chg%:", q["change_pct"])
    print("mktcap/pe/pb  :", q["market_cap"], q["pe"], q["pb"])
    print("bars          :", snap["bars"], snap["first_date"], "->", snap["last_date"])
    print("ma5/10/20/60  : %.2f %.2f %.2f %.2f" % (ind["ma5"], ind["ma10"], ind["ma20"], ind["ma60"]))
    print("rsi14         : %.1f" % ind["rsi14"])
    print("macd          : dif=%.3f dea=%.3f hist=%.3f" % (
        ind["macd"]["dif"], ind["macd"]["dea"], ind["macd"]["hist"]))
    print("boll          : %.2f / %.2f / %.2f" % (
        ind["boll"]["lower"], ind["boll"]["mid"], ind["boll"]["upper"]))
    print("atr14         : %.2f" % ind["atr14"])
    print("5d/20d/60d %%  : %s %s %s" % (
        round(ind["change_5d_pct"], 2) if ind["change_5d_pct"] else None,
        round(ind["change_20d_pct"], 2) if ind["change_20d_pct"] else None,
        round(ind["change_60d_pct"], 2) if ind["change_60d_pct"] else None))
    print("vol_ratio     : %s" % (round(ind["vol_ratio_5_20"], 2) if ind["vol_ratio_5_20"] else None))
    print("levels        : support=%.2f resistance=%.2f stop=%.2f" % (
        lv["support"], lv["resistance"], lv["stop_loss"]))
    print("last 3 bars   :")
    for k in snap["klines"][-3:]:
        print("   ", k["date"], "O=%.2f C=%.2f H=%.2f L=%.2f" % (
            k["open"], k["close"], k["high"], k["low"]))
    return snap


for s in ("LITE", "600519"):
    try:
        show(s)
    except Exception as e:
        print("[FAIL]", s, type(e).__name__, e)

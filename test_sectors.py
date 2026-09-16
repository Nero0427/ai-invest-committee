"""Smoke test for the sector layer."""
import sys
import time

sys.path.insert(0, ".")
import sectors


def show_tag(t):
    return "%s/%s/%s" % (t["kind"], t["code"], t.get("name"))


print("=== sector list ===")
t0 = time.time()
items = sectors.load_sector_list()
counts = {}
for it in items:
    counts[it["board"]] = counts.get(it["board"], 0) + 1
print("total=%d  %s  (%.1fs)" % (len(items), counts, time.time() - t0))

print("\n=== search ===")
for kw in ("医疗", "半导体", "白酒", "光模块", "XLK", "科技", "银行"):
    hits = sectors.search_sectors(kw, limit=3)
    print("  %-6s -> %s" % (kw, [(h["code"], h["name"], h["kind"]) for h in hits]))

print("\n=== resolve_target ===")
for raw in ("BK1600", "XLK", "600519", "LITE", "半导体", "医疗研发外包"):
    try:
        t = sectors.resolve_target(raw)
        print("  %-10s -> %s" % (raw, show_tag(t)))
    except Exception as exc:
        print("  %-10s -> FAIL %s: %s" % (raw, type(exc).__name__, exc))

print("\n=== A-share sector snapshot ===")
for code in ("BK1600", "BK0475"):
    try:
        target = sectors.resolve_target(code)
        snap = sectors.build_sector_snapshot(target)
        q = snap["quote"]
        ind = snap["indicators"]
        lv = snap["levels"]
        br = snap.get("breadth") or {}
        mf = snap.get("moneyflow") or {}
        print("  %s %s" % (code, q["name"]))
        print("     price=%s chg=%s%% bars=%d" % (q["price"], q["change_pct"], snap["bars"]))
        print("     ma20=%s rsi=%s" % (
            round(ind["ma20"], 2) if ind.get("ma20") else None,
            round(ind["rsi14"], 1) if ind.get("rsi14") else None))
        print("     levels: support=%s resistance=%s" % (
            round(lv["support"], 2) if lv.get("support") else None,
            round(lv["resistance"], 2) if lv.get("resistance") else None))
        if snap.get("kline_error"):
            print("     kline_error:", snap["kline_error"][:80])
        print("     breadth: up=%s down=%s flat=%s total=%s limit_up=%s" % (
            br.get("up"), br.get("down"), br.get("flat"), br.get("total"), br.get("limit_up")))
        print("     members=%d  top=%s" % (
            len(snap.get("members") or []),
            [(m["name"], m["change_pct"]) for m in (snap.get("members") or [])[:3]]))
        print("     moneyflow main=%s date=%s" % (mf.get("main"), mf.get("date")))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print("  %s FAIL %s: %s" % (code, type(exc).__name__, exc))

print("\n=== US sector snapshot ===")
for code in ("XLK", "SMH"):
    try:
        target = sectors.resolve_target(code)
        snap = sectors.build_sector_snapshot(target)
        q = snap["quote"]
        ind = snap["indicators"]
        print("  %s  %s  price=%s chg=%s%%  bars=%d  ma20=%s rsi=%s" % (
            code, q["name"], q["price"], q["change_pct"], snap["bars"],
            round(ind["ma20"], 2) if ind.get("ma20") else None,
            round(ind["rsi14"], 1) if ind.get("rsi14") else None))
    except Exception as exc:
        print("  %s FAIL %s: %s" % (code, type(exc).__name__, exc))

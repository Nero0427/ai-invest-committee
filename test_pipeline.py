"""End-to-end pipeline test across sectors and single stocks."""
import sys

sys.path.insert(0, ".")
import agents
import settings
import snapshot


class StubLLM:
    """Unconfigured model, so the rule engine runs."""
    ready = False

    def describe(self):
        return {"ready": False}


CFG = settings.load_config()
CFG["committee"]["debate_rounds"] = 2


def run(raw):
    print("=" * 68)
    print("INPUT:", raw)
    try:
        snap = snapshot.build(raw)
    except Exception as exc:
        print("  [snapshot FAIL]", type(exc).__name__, str(exc)[:110])
        return

    print("  target  :", snapshot.describe(snap))
    print("  price   : %s  chg=%s%%  bars=%s" % (
        snap["quote"]["price"], snap["quote"].get("change_pct"), snap.get("bars")))
    br = snap.get("breadth") or {}
    if br.get("total"):
        print("  breadth : up=%s down=%s limit_up=%s total=%s" % (
            br.get("up"), br.get("down"), br.get("limit_up"), br.get("total")))
    if snap.get("members"):
        print("  members : %d  top=%s" % (
            len(snap["members"]),
            [(m["name"], m["change_pct"]) for m in snap["members"][:3]]))
    mf = snap.get("moneyflow") or {}
    if mf.get("main") is not None:
        print("  flow    : main=%s" % round(mf["main"] / 1e8, 2))
    if snap.get("kline_error"):
        print("  kline   : UNAVAILABLE")

    spoken = []

    def emit(evt):
        et = evt.get("type")
        if et == "stage":
            print("  [stage] %-8s %s%s" % (
                evt["stage"], evt["status"],
                ("  " + evt["note"]) if evt.get("note") else ""))
        elif et == "speech":
            d = evt.get("data") or {}
            spoken.append(evt["agent"])
            print("  [say  ] %-12s %-4s %-4s %s" % (
                evt["agent"], d.get("stance"), d.get("confidence"),
                (evt.get("text") or "")[:46]))
        elif et == "news":
            print("  [news ] scanned=%s matched=%s kw=%s" % (
                evt.get("count"), evt.get("matched"), evt.get("keywords")))
        elif et == "warn":
            print("  [warn ] %s" % str(evt.get("message"))[:90])

    result = agents.run_committee(snap, StubLLM(), CFG, emit)
    lv = result["levels"]
    print("  RATING  : %s  consensus=%s  speakers=%d" % (
        result["rating"], result["consensus"], len(spoken)))
    print("  levels  : support=%s resistance=%s stop=%s pos=%s" % (
        lv.get("support"), lv.get("resistance"), lv.get("stop_loss"),
        lv.get("position_pct")))
    for line in result["thesis"][:2]:
        print("  thesis  :", line[:64])
    for line in result["risks"][:2]:
        print("  risk    :", line[:64])
    print("  action  :", result["action"][:70])


for target in ("pt02GN2211", "BK0896", "XLK", "600519", "LITE"):
    try:
        run(target)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print("  [FAIL]", target, type(exc).__name__, str(exc)[:110])

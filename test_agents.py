"""Smoke test: run the committee with the rule engine (no API key)."""
import sys

sys.path.insert(0, ".")
import agents
import datasource
import settings


class StubLLM:
    """Stands in for an unconfigured model so the pipeline uses its fallback."""
    ready = False

    def describe(self):
        return {"ready": False}


def run(sym):
    print("=" * 66)
    print("SYMBOL:", sym)
    snap = datasource.build_market_snapshot(sym)
    print("price=%.2f name=%s bars=%d" % (
        snap["quote"]["price"], snap["quote"]["name"], snap["bars"]))

    seen = []

    def emit(event):
        et = event.get("type")
        if et == "stage":
            print("  [stage] %-8s %s" % (event["stage"], event["status"]))
        elif et == "agent_start":
            print("  [start] %s" % event["agent"])
        elif et == "speech":
            d = event.get("data") or {}
            seen.append(event["agent"])
            print("  [speak] %-12s stance=%-4s conf=%-4s %s" % (
                event["agent"], d.get("stance"), d.get("confidence"),
                (event.get("text") or "")[:52]))
        elif et == "warn":
            print("  [warn ] %s" % event.get("message"))
        elif et == "consensus":
            pass
        else:
            print("  [%s] %s" % (et, str(event)[:80]))

    cfg = settings.load_config()
    cfg["committee"]["debate_rounds"] = 2
    result = agents.run_committee(snap, StubLLM(), cfg, emit)

    print("-" * 66)
    print("agents_spoken :", len(seen), sorted(seen))
    print("rating        :", result["rating"], "| consensus:", result["consensus"])
    lv = result["levels"]
    print("levels        : support=%s resistance=%s target=%s-%s stop=%s pos=%s" % (
        lv.get("support"), lv.get("resistance"), lv.get("target_low"),
        lv.get("target_high"), lv.get("stop_loss"), lv.get("position_pct")))
    price = snap["quote"]["price"]
    ok = (lv.get("support") or 0) < price < (lv.get("resistance") or 0)
    print("level_order_ok:", ok)
    print("thesis        :", len(result["thesis"]), "items")
    for t in result["thesis"]:
        print("   -", t)
    print("risks         :", len(result["risks"]), "items")
    for r in result["risks"]:
        print("   -", r)
    print("action        :", result["action"])
    print("members       :", len(result["members"]))
    return result


for s in ("LITE", "600519"):
    try:
        run(s)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print("[FAIL]", s, type(exc).__name__, exc)

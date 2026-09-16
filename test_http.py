"""End-to-end check of the running server: config, quote and the SSE stream."""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8848"
SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "LITE"


def get_json(path, timeout=30):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


print("--- /api/config ---")
try:
    cfg = get_json("/api/config")
    eng = cfg.get("engine", {})
    print("ok        :", cfg.get("ok"))
    print("provider  :", eng.get("provider"), "| ready:", eng.get("ready"))
    print("providers :", len(cfg.get("providers", [])))
except Exception as exc:
    print("[FAIL]", type(exc).__name__, exc)

print("--- /api/quote?symbol=%s ---" % SYMBOL)
try:
    q = get_json("/api/quote?symbol=" + SYMBOL)
    d = q.get("data", {}).get("quote", {})
    print("ok        :", q.get("ok"))
    print("name/price:", d.get("name"), d.get("price"), d.get("change_pct"))
    print("spark len :", len(q["data"].get("spark") or []))
except Exception as exc:
    print("[FAIL]", type(exc).__name__, exc)

print("--- / (index.html) ---")
try:
    with urllib.request.urlopen(BASE + "/", timeout=15) as resp:
        body = resp.read().decode("utf-8")
    print("status    :", resp.status, "| bytes:", len(body),
          "| has title:", "AI 投委会" in body)
except Exception as exc:
    print("[FAIL]", type(exc).__name__, exc)

print("--- /static/app.js ---")
try:
    with urllib.request.urlopen(BASE + "/static/app.js", timeout=15) as resp:
        print("status    :", resp.status, "| bytes:", len(resp.read()))
except Exception as exc:
    print("[FAIL]", type(exc).__name__, exc)

print("--- /api/analyze?symbol=%s (SSE) ---" % SYMBOL)
try:
    req = urllib.request.Request(BASE + "/api/analyze?symbol=" + SYMBOL)
    counts = {}
    speeches = []
    rating = None
    errors = []
    started = time.time()

    with urllib.request.urlopen(req, timeout=180) as resp:
        print("content   :", resp.headers.get("Content-Type"))
        buf = b""
        while True:
            chunk = resp.read(1)
            if not chunk:
                break
            buf += chunk
            if not buf.endswith(b"\n\n"):
                continue
            block = buf.decode("utf-8").strip()
            buf = b""
            if not block.startswith("data:"):
                continue
            try:
                evt = json.loads(block[5:].strip())
            except json.JSONDecodeError:
                continue
            et = evt.get("type")
            counts[et] = counts.get(et, 0) + 1
            if et == "speech":
                d = evt.get("data") or {}
                pts = d.get("points") or []
                speeches.append((
                    evt.get("agent"),
                    (evt.get("text") or "")[:28],
                    (pts[-1] if pts else "")[:30],
                ))
            elif et == "consensus":
                rating = (evt["data"].get("rating"), evt["data"].get("consensus"))
            elif et == "error":
                errors.append(evt.get("message"))
            elif et == "done":
                break

    print("events    :", counts)
    print("elapsed   : %.1fs" % (time.time() - started))
    print("speeches  :", len(speeches))
    for row in speeches:
        print("   %-12s %-30s | %s" % row)
    print("rating    :", rating)
    if errors:
        print("errors    :", errors)
except Exception as exc:
    import traceback
    traceback.print_exc()
    print("[FAIL]", type(exc).__name__, exc)

print("--- /api/decision ---")
try:
    payload = json.dumps({"action": "approve", "note": "selftest"}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/decision", data=payload,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(json.loads(resp.read().decode("utf-8")))
except Exception as exc:
    print("[FAIL]", type(exc).__name__, exc)

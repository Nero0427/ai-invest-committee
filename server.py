"""HTTP server: static files, quote lookup, config editing and the SSE stream
that carries the committee session to the browser.

Standard library only, so the whole project runs with a bare Python install.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import agents
import datasource
import llm as llm_module
import market
import sectors
import settings
import snapshot as snapshot_module

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# Hosted containers run on UTC, which would shift every date this app derives -
# the trading day it probes, cache expiry, the "updated at" stamps - by eight
# hours. Pin Beijing time so the same code reports the same dates locally and
# in the cloud. tzset() is POSIX-only, hence the guard.
os.environ.setdefault("TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):
    time.tzset()


class ClientGone(Exception):
    """Raised when the browser closes the SSE connection."""


class Server(ThreadingHTTPServer):
    """Threaded server with a deeper accept queue.

    The stdlib default queue is 5. A browser opening a single page fires the
    document plus its scripts, stylesheet and favicon within the same instant,
    and anything that does not fit in the backlog is reset before it is ever
    read. That is what showed up in the console as "Failed to fetch" on
    requests this process never even saw.
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def public_snapshot(snap: dict) -> dict:
    """Trim the snapshot so the browser does not receive 250 raw candles."""
    q = snap["quote"]
    ind = snap.get("indicators") or {}
    klines = snap.get("klines") or []
    return {
        "quote": q,
        "target": snap.get("target") or {"kind": "stock"},
        "levels": snap.get("levels") or {},
        "indicators": {
            "ma5": ind.get("ma5"), "ma20": ind.get("ma20"), "ma60": ind.get("ma60"),
            "rsi14": ind.get("rsi14"), "atr14": ind.get("atr14"),
            "change_5d_pct": ind.get("change_5d_pct"),
            "change_20d_pct": ind.get("change_20d_pct"),
            "change_60d_pct": ind.get("change_60d_pct"),
            "high_20d": ind.get("high_20d"), "low_20d": ind.get("low_20d"),
            "vol_ratio_5_20": ind.get("vol_ratio_5_20"),
        },
        "breadth": snap.get("breadth") or {},
        "moneyflow": snap.get("moneyflow") or {},
        "members": (snap.get("members") or [])[:20],
        "kline_source": snap.get("kline_source"),
        "kline_error": snap.get("kline_error"),
        "spark": [k["close"] for k in klines[-60:]],
        "spark_dates": [k["date"] for k in klines[-60:]],
        "bars": snap.get("bars") or 0,
        "first_date": snap.get("first_date"),
        "last_date": snap.get("last_date"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AICommittee/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing --

    def log_message(self, fmt, *args):
        """Quiet the default stderr logging, which corrupts on GBK consoles."""
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._json({"ok": False, "error": message}, status)

    # ----------------------------------------------------------------- GET --

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        try:
            if path == "/api/analyze":
                self._sse_analyze(params)
            elif path == "/api/quote":
                self._quote(params)
            elif path == "/api/sectors":
                self._sectors(params)
            elif path == "/api/treemap":
                self._treemap(params)
            elif path == "/api/billboard":
                self._billboard(params)
            elif path == "/api/billboard/seats":
                self._billboard_seats(params)
            elif path == "/api/commodities":
                self._commodities()
            elif path == "/api/search":
                self._search(params)
            elif path == "/api/market":
                self._market(params)
            elif path == "/api/config":
                self._get_config()
            elif path in ("/", "/index.html"):
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._error("not found", 404)
        except (BrokenPipeError, ConnectionResetError, ClientGone):
            pass
        except Exception as exc:  # keep the server alive on any handler error
            try:
                self._error("%s: %s" % (type(exc).__name__, exc), 500)
            except Exception:
                pass

    def _static(self, relative):
        safe = os.path.normpath(relative).replace("\\", "/").lstrip("/")
        if safe.startswith("..") or os.path.isabs(safe):
            self._error("forbidden", 403)
            return
        target = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(target):
            self._error("not found", 404)
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _quote(self, params):
        symbol = (params.get("symbol") or [""])[0].strip()
        if not symbol:
            self._error("missing symbol")
            return
        try:
            snap = snapshot_module.build(symbol)
        except datasource.DataError as exc:
            self._error(str(exc), 502)
            return
        self._json({"ok": True, "data": public_snapshot(snap)})

    def _sectors(self, params):
        """Sector search and browsing for the UI picker."""
        keyword = (params.get("q") or [""])[0].strip()
        kind = (params.get("type") or [""])[0].strip()
        try:
            if keyword:
                items = sectors.search_sectors(keyword, limit=40)
            elif kind == "us":
                items = sectors.decorate_us_sectors(sectors.us_sector_list())
            elif kind in ("industry", "concept"):
                items = sectors.apply_live_quotes(
                    [b for b in sectors.load_sector_list() if b.get("board") == kind])
            else:
                items = sectors.apply_live_quotes(sectors.load_sector_list())[:200]
        except Exception as exc:
            self._error("sector list unavailable: %s" % exc, 502)
            return

        trimmed = [{
            "code": it.get("code"), "name": it.get("name"),
            "kind": it.get("kind"), "board": it.get("board"),
            "change_pct": it.get("change_pct"), "leader": it.get("leader"),
            "live": bool(it.get("live")),
            # Which provider serves this board's history. Tonghuashun covers
            # every live A-share board and the US sector ETFs as well, so
            # nothing here has to warn about a source that might be blocked.
            "history_source": ("ths" if (it.get("ths_code") or it.get("kind") == "sector_us")
                               else ("eastmoney" if it.get("secid") else None)),
        } for it in items[:1000]]
        self._json({"ok": True, "items": trimmed})

    def _treemap(self, params):
        """Heat map data. `market=us` swaps in the US sector view."""
        name = (params.get("market") or ["cn"])[0].strip().lower()
        raw = (params.get("limit") or [""])[0]
        limit = int(raw) if raw.isdigit() else 1500
        try:
            if name == "us":
                data = market.build_us_treemap()
            else:
                data = market.build_treemap(max(200, min(3000, limit)))
        except market.DataError as exc:
            self._error("market snapshot unavailable: %s" % exc, 502)
            return
        self._json({"ok": True, "data": data})

    def _billboard_seats(self, params):
        """Buy / sell seats behind one billboard entry."""
        code = (params.get("code") or [""])[0].strip()
        if not code:
            self._error("missing code")
            return
        day = (params.get("date") or [""])[0].strip()
        try:
            if not day:
                day = market.fetch_billboard()["date"]
            data = market.fetch_billboard_seats(code, day)
        except market.DataError as exc:
            self._error("seats unavailable: %s" % exc, 502)
            return
        self._json({"ok": True, "data": data})

    def _commodities(self):
        """Gold, silver and the rest of what the commodity tab lists."""
        try:
            self._json({"ok": True, "items": market.fetch_commodities()})
        except Exception as exc:
            self._error("commodities unavailable: %s" % exc, 502)

    def _search(self, params):
        """One box that looks in indices, boards and every listed stock.

        The committee's input used to suggest boards only, so typing a stock
        code came back empty. Indices, boards and stocks all surface now,
        ordered by match quality and size.
        """
        keyword = (params.get("q") or [""])[0].strip()
        if not keyword:
            self._json({"ok": True, "items": []})
            return

        items, seen_index = [], set()

        for _, (_, ths_code, name) in datasource.INDEX_ALIASES.items():
            if ths_code in seen_index or keyword not in name:
                continue
            seen_index.add(ths_code)
            items.append({"code": ths_code[3:], "name": name, "kind": "index",
                          "board": None, "value": name, "tag": "指数"})

        for item in market.COMMODITIES:
            if keyword in item["name"]:
                items.append({"code": item["code"], "name": item["name"],
                              "kind": "commodity", "board": None,
                              "value": item["code"], "tag": "商品"})

        try:
            for board in sectors.search_sectors(keyword, limit=8):
                items.append({
                    "code": board.get("code"), "name": board.get("name"),
                    "kind": board.get("kind"), "board": board.get("board"),
                    "value": board.get("code"),
                    "tag": "美股板块" if board.get("kind") == "sector_us" else "板块",
                })
        except Exception:
            pass

        try:
            rows, _ = market.fetch_all_stocks()
            hits = [r for r in rows if keyword in r["name"] or r["code"] == keyword]
            hits.sort(key=lambda r: -(r.get("market_cap") or 0))
            for row in hits[:8]:
                items.append({
                    "code": row["code"], "name": row["name"], "kind": "stock",
                    "board": None, "value": row["code"], "tag": "个股",
                    "change_pct": row.get("change_pct"),
                })
        except Exception:
            pass

        self._json({"ok": True, "items": items[:20]})

    def _billboard(self, params):
        """Daily billboard (龙虎榜)."""
        force = (params.get("force") or [""])[0] == "1"
        try:
            data = market.fetch_billboard(force=force)
        except market.DataError as exc:
            self._error("billboard unavailable: %s" % exc, 502)
            return
        self._json({"ok": True, "data": data})

    def _market(self, params):
        """Advance/decline stats plus the day's movers."""
        try:
            payload = market.top_movers(20)
            payload["stats"] = market.market_stats()
        except market.DataError as exc:
            self._error("market data unavailable: %s" % exc, 502)
            return
        self._json({"ok": True, "data": payload})

    def _get_config(self):
        cfg = settings.load_config()
        descriptor = llm_module.LLM(cfg).describe()
        self._json({
            "ok": True,
            "config": settings.public_config(cfg),
            "engine": descriptor,
            "providers": [
                {
                    "id": pid, "label": p["label"], "model": p["model"],
                    "note": p["note"], "env": list(p.get("env") or []),
                }
                for pid, p in llm_module.PROVIDERS.items()
            ],
        })

    # ---------------------------------------------------------------- POST --

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._error("invalid JSON body")
            return

        if parsed.path == "/api/config":
            self._save_config(body)
        elif parsed.path == "/api/decision":
            self._decision(body)
        else:
            self._error("not found", 404)

    def _save_config(self, body):
        cfg = settings.load_config()
        if "provider" in body:
            cfg["llm"]["provider"] = str(body["provider"])
        if "api_key" in body:
            cfg["llm"]["api_key"] = str(body["api_key"]).strip()
        if body.get("model"):
            cfg["llm"]["model"] = str(body["model"]).strip()
        if body.get("base_url"):
            cfg["llm"]["base_url"] = str(body["base_url"]).strip()
        if body.get("debate_rounds") is not None:
            try:
                cfg["committee"]["debate_rounds"] = max(1, min(4, int(body["debate_rounds"])))
            except (TypeError, ValueError):
                pass
        settings.save_config(cfg)
        self._json({
            "ok": True,
            "config": settings.public_config(cfg),
            "engine": llm_module.LLM(cfg).describe(),
        })

    def _decision(self, body):
        """The human gate. Nothing is traded here - the decision is echoed
        back so the UI can confirm it."""
        action = body.get("action")
        if action not in ("approve", "modify", "reject"):
            self._error("action must be approve, modify or reject")
            return
        note = str(body.get("note") or "")[:500]
        self._json({"ok": True, "action": action, "note": note,
                    "recorded_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S")})

    # ----------------------------------------------------------------- SSE --

    def _sse_analyze(self, params):
        symbol = (params.get("symbol") or [""])[0].strip()
        if not symbol:
            self._error("missing symbol")
            return

        cfg = settings.load_config()
        override = (params.get("rounds") or [""])[0].strip()
        if override.isdigit():
            cfg["committee"]["debate_rounds"] = max(1, min(4, int(override)))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        stop_flag = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(stop_flag,), daemon=True)
        heartbeat.start()

        def emit(event):
            self._write_event(event)

        try:
            engine = llm_module.LLM(cfg)
            emit({"type": "start", "symbol": symbol,
                  "engine": engine.describe(),
                  "members": [
                      {"id": a["id"], "name": a["name"],
                       "expertise": a.get("expertise", "")}
                      for a in agents.ALL_AGENTS
                  ]})
            try:
                snap = snapshot_module.build(symbol)
            except datasource.DataError as exc:
                emit({"type": "error", "message": "无法获取行情数据：%s" % exc})
                emit({"type": "done"})
                return

            emit({"type": "snapshot", "data": public_snapshot(snap)})
            result = agents.run_committee(snap, engine, cfg, emit,
                                          should_stop=stop_flag.is_set)
            if result:
                emit({"type": "complete", "data": result})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                emit({"type": "error", "message": "%s: %s" % (type(exc).__name__, exc)})
            except Exception:
                pass
        finally:
            stop_flag.set()
            try:
                emit({"type": "done"})
            except Exception:
                pass

    def _write_event(self, event):
        payload = json.dumps(event, ensure_ascii=False)
        try:
            self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise ClientGone() from exc

    def _heartbeat(self, stop_flag):
        while not stop_flag.wait(15):
            try:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
            except Exception:
                return


def _warm_news():
    """Preload the bulletin feed off the request path.

    Building the first thousand-bulletin snapshot takes about fourteen seconds
    of paging, which would otherwise land on whichever analysis runs first.
    """
    try:
        import news as news_module
        print("news cache : preloading bulletins...")
        count = news_module.warm_cache()
        print("news cache : %d bulletins ready" % count)
    except Exception as exc:
        print("news cache : warm-up failed (%s)" % exc)


def _warm_sectors():
    """Preload the board list and its live quotes, off the request path.

    The first sweep costs ~1900 upstream requests, and it has to happen before
    any board code can even be resolved. Left lazy, the first click on "browse
    boards" sits on a spinner for half a minute - which is exactly what used to
    happen.
    """
    try:
        print("board list : preloading boards...")
        boards = sectors.load_sector_list()
        print("board list : %d boards ready" % len(boards))
        sectors.warm_live_quotes()
        print("board list : live quotes ready")
    except Exception as exc:
        print("board list : warm-up failed (%s)" % exc)


def _warm_market():
    """Pull the whole-market snapshot and the billboard off the request path."""
    try:
        print("market     : preloading snapshot...")
        day = market.warm_cache()
        print("market     : ready (billboard %s)" % day)
    except Exception as exc:
        print("market     : warm-up failed (%s)" % exc)


def main():
    cfg = settings.load_config()

    # Hosting platforms hand the port over through the environment and expect
    # the process to bind every interface. The local defaults stay in place so
    # double-clicking start.bat behaves exactly as before.
    cloud = bool(os.environ.get("PORT"))
    host = "0.0.0.0" if cloud else cfg["server"].get("host", "127.0.0.1")
    port = int(os.environ.get("PORT") or cfg["server"].get("port", 8848))

    httpd = Server((host, port), Handler)
    httpd.daemon_threads = True
    url = "http://%s:%d/" % (host, port)

    engine = llm_module.LLM(cfg).describe()
    print("=" * 58)
    print("Investment Committee")
    print("url      : %s" % url)
    print("mode     : %s" % ("hosted" if cloud else "local"))
    print("provider : %s (%s)" % (engine["provider"], engine["model"]))
    print("engine   : %s" % ("LLM" if engine["ready"] else "rule-based fallback (no API key)"))
    print("static   : %s" % STATIC_DIR)
    print("press Ctrl+C to stop")
    print("=" * 58)

    threading.Thread(target=_warm_news, daemon=True).start()
    threading.Thread(target=_warm_sectors, daemon=True).start()
    threading.Thread(target=_warm_market, daemon=True).start()

    if not cloud and cfg["server"].get("open_browser", True):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())

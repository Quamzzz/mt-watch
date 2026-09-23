"""The receiver and the status page.

MT's own health check runs on the MT box. That catches a wedged process, but it cannot catch
the machine being gone — if the box dies, its watcher dies with it and the silence looks exactly
like health. This service is the part that has to live somewhere else.

It is deliberately stateless: all state is in Postgres, so Railway can redeploy it with no
downtime. That is the opposite of MT itself, which keeps everything in one SQLite file and
therefore cannot be deployed that way at all.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone
from html import escape
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from watch import db

app = FastAPI(title="mt-watch", docs_url=None, redoc_url=None)

SILENT_AFTER_S = float(os.environ.get("SILENT_AFTER_S", 1800))

# How long each MT service may go between beats before we call it late. These mirror
# mt/health.py: the cadences there are 300s for the scanner and 360s for the monitor, and a
# service is only late once it has missed several beats.
EXPECTED_S: dict[str, float] = {"scanner": 300 * 3, "monitor": 360 * 3}


@app.on_event("startup")
def _startup() -> None:
    db.init()


def _authorise(authorization: str | None) -> None:
    expected = os.environ.get("BEAT_TOKEN")
    if not expected:
        raise HTTPException(500, "BEAT_TOKEN is not set on this service")
    got = (authorization or "").removeprefix("Bearer ").strip()
    # compare_digest rather than ==: string comparison returns early on the first differing
    # byte, which leaks the token a character at a time to anyone willing to time the replies.
    if not hmac.compare_digest(got, expected):
        raise HTTPException(401, "bad token")


def _percent(value: Any) -> int | None:
    """Disk percentage, from whatever the client sent. `df` gives an integer, but accept a
    float or a numeric string rather than silently dropping the field."""
    if value is None:
        return None
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Railway's healthcheck target. Says nothing about MT — only that this service is up."""
    return {"status": "ok"}


@app.post("/beat")
async def beat(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Receive one report from an MT box.

    Body:
      {"host": "mt-prod", "healthy": true, "disk_pct": 37,
       "services": {"scanner": "2026-09-23T10:22:01+00:00", "monitor": null},
       "report": "optional text from `mt health`"}
    """
    _authorise(authorization)
    body: dict[str, Any] = await request.json()

    host = str(body.get("host") or "unknown")[:64]
    services: dict[str, datetime | None] = {}
    for name, stamp in (body.get("services") or {}).items():
        if not stamp:
            services[str(name)[:64]] = None
            continue
        try:
            parsed = datetime.fromisoformat(str(stamp))
        except ValueError:
            services[str(name)[:64]] = None
            continue
        # MT writes ISO-8601 UTC. Anything arriving naive is assumed UTC rather than rejected:
        # a report with a slightly odd timestamp is far more useful than no report.
        services[str(name)[:64]] = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    db.record(
        host=host,
        healthy=bool(body.get("healthy", False)),
        disk_pct=_percent(body.get("disk_pct")),
        report=str(body.get("report"))[:4000] if body.get("report") else None,
        services=services,
    )
    return JSONResponse({"ok": True, "host": host, "services": len(services)})


def _ago(then: datetime | None) -> str:
    if then is None:
        return "never"
    s = (datetime.now(timezone.utc) - then).total_seconds()
    if s < 90:
        return f"{s:.0f}s ago"
    if s < 5400:
        return f"{s / 60:.0f} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{s / 86400:.1f} days ago"


def _state(host: dict[str, Any]) -> tuple[str, str]:
    """Overall verdict for one box: (css class, words)."""
    silent_for = (datetime.now(timezone.utc) - host["received_at"]).total_seconds()
    if silent_for > SILENT_AFTER_S:
        return "bad", f"silent for {_ago(host['received_at'])[:-4] or 'a while'}"
    if not host["healthy"]:
        return "warn", "reporting, but something is stale"
    if host["disk_pct"] is not None and host["disk_pct"] >= 80:
        return "warn", f"disk at {host['disk_pct']}%"
    return "ok", "healthy"


PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mt-watch</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #fbfbfa; --fg: #1a1a18; --dim: #6b6b66; --line: #e4e4e0; --card: #fff;
    --ok: #1a7f4b; --warn: #a06800; --bad: #b4241f;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#14141a; --fg:#e8e8e4; --dim:#9a9a93; --line:#2b2b33; --card:#1c1c23;
             --ok:#4ac98a; --warn:#e0a83a; --bad:#f0625c; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:32px 16px; background:var(--bg); color:var(--fg);
         font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width: 680px; margin: 0 auto; }}
  h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:-.01em; }}
  .sub {{ color:var(--dim); font-size:13px; margin:0 0 28px; }}
  .box {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:18px 20px; margin-bottom:16px; }}
  .head {{ display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; margin-bottom:14px; }}
  .name {{ font-weight:600; }}
  .pill {{ font-size:12px; padding:2px 9px; border-radius:999px; border:1px solid currentColor; }}
  .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  td {{ padding:6px 0; border-top:1px solid var(--line); }}
  td:last-child {{ text-align:right; color:var(--dim); }}
  tr:first-child td {{ border-top:0; }}
  .empty {{ color:var(--dim); }}
  footer {{ color:var(--dim); font-size:12px; margin-top:28px; }}
  code {{ font-size:12px; background:var(--card); border:1px solid var(--line);
          padding:1px 5px; border-radius:4px; }}
</style>
<main>
  <h1>mt-watch</h1>
  <p class="sub">External heartbeat for the MT engine. If this page says nothing arrived,
     the box itself is gone — which is the one failure MT cannot report on its own.</p>
  {body}
  <footer>Checked {checked}. A box is called silent after {silent:.0f} minutes.</footer>
</main>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    hosts = db.overview()
    if not hosts:
        body = ('<div class="box"><span class="empty">No reports yet. Point an MT box at '
                '<code>POST /beat</code> and it will appear here.</span></div>')
    else:
        chunks = []
        for h in hosts:
            css, words = _state(h)
            rows = "".join(
                f"<tr><td>{escape(s['service'])}</td>"
                f"<td>last cycle {escape(_ago(s['last_beat_at']))}</td></tr>"
                for s in h["services"]
            ) or '<tr><td colspan="2" class="empty">no services reported</td></tr>'
            disk = f" · disk {h['disk_pct']}%" if h["disk_pct"] is not None else ""
            chunks.append(
                f'<div class="box"><div class="head">'
                f'<span class="name">{escape(h["host"])}</span>'
                f'<span class="pill {css}">{escape(words)}</span></div>'
                f'<table>{rows}</table>'
                f'<p class="sub" style="margin:12px 0 0">heard from '
                f'{escape(_ago(h["received_at"]))}{escape(disk)}</p></div>'
            )
        body = "".join(chunks)
    return HTMLResponse(PAGE.format(
        body=body,
        checked=datetime.now(timezone.utc).strftime("%H:%M UTC"),
        silent=SILENT_AFTER_S / 60,
    ))

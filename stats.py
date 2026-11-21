#!/usr/bin/env python3
"""Session history + local stats page for the pomodoro timer.

Ideas taken from sasa95/pomodor (MIT): a session is
    { duration, label, createdAt }
and stats are "focus minutes / session counts per day and per label".
Storage is replaced: SQLite instead of Firebase, and the page is rendered
server-side so it works with no internet and no JS framework.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.path.expanduser("~/.local/share/pomodoro")
DB_PATH = os.path.join(DATA_DIR, "history.db")

DEFAULT_LABELS = [
    ("focus", "#DA291C"),
    ("study", "#89b4fa"),
    ("code", "#a6e3a1"),
    ("review", "#FFC72C"),
]

PORT_FILE = os.path.join(DATA_DIR, "port")

_lock = threading.Lock()


# ---------------------------------------------------------------- storage

def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ended_at  INTEGER NOT NULL,
                minutes   INTEGER NOT NULL,
                label     TEXT    NOT NULL DEFAULT 'focus'
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_ended ON sessions(ended_at)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS labels (
                name  TEXT PRIMARY KEY,
                color TEXT NOT NULL
            )
        """)
        for name, color in DEFAULT_LABELS:
            db.execute("INSERT OR IGNORE INTO labels(name, color) VALUES (?, ?)", (name, color))


def record(minutes: int, label: str = "focus", ended_at: int | None = None) -> None:
    if minutes <= 0:
        return
    with _lock, sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO sessions(ended_at, minutes, label) VALUES (?, ?, ?)",
            (ended_at or int(time.time()), int(minutes), label or "focus"),
        )


def labels() -> list[tuple[str, str]]:
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute("SELECT name, color FROM labels ORDER BY rowid").fetchall()
    return rows or DEFAULT_LABELS


# ---------------------------------------------------------------- queries

def _day_start(d: date) -> int:
    return int(datetime(d.year, d.month, d.day).timestamp())


def collect(days: int = 14) -> dict:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    with sqlite3.connect(DB_PATH) as db:
        def total(since: int | None):
            q = "SELECT COALESCE(SUM(minutes),0), COUNT(*) FROM sessions"
            if since is None:
                return db.execute(q).fetchone()
            return db.execute(q + " WHERE ended_at >= ?", (since,)).fetchone()

        today_min, today_n = total(_day_start(today))
        week_min, week_n = total(_day_start(week_start))
        month_min, month_n = total(_day_start(month_start))
        all_min, all_n = total(None)

        # last N days, oldest first, zero-filled
        series = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            start = _day_start(d)
            end = start + 86400
            m = db.execute(
                "SELECT COALESCE(SUM(minutes),0), COUNT(*) FROM sessions WHERE ended_at >= ? AND ended_at < ?",
                (start, end),
            ).fetchone()
            series.append({"day": d.isoformat(), "weekday": d.strftime("%a"), "minutes": m[0], "count": m[1]})

        by_label = db.execute(
            "SELECT label, COALESCE(SUM(minutes),0), COUNT(*) FROM sessions GROUP BY label ORDER BY 2 DESC"
        ).fetchall()

        recent = db.execute(
            "SELECT ended_at, minutes, label FROM sessions ORDER BY ended_at DESC LIMIT 25"
        ).fetchall()

        # streak: consecutive days with at least one session, ending today
        streak = 0
        for i in range(0, 400):
            d = today - timedelta(days=i)
            start = _day_start(d)
            row = db.execute(
                "SELECT COUNT(*) FROM sessions WHERE ended_at >= ? AND ended_at < ?",
                (start, start + 86400),
            ).fetchone()
            if row[0]:
                streak += 1
            elif i > 0 or True:
                break

    colors = dict(labels())
    return {
        "generated": datetime.now().strftime("%a %d %b %Y, %H:%M"),
        "today": {"minutes": today_min, "sessions": today_n},
        "week": {"minutes": week_min, "sessions": week_n},
        "month": {"minutes": month_min, "sessions": month_n},
        "all": {"minutes": all_min, "sessions": all_n},
        "streak": streak,
        "series": series,
        "by_label": [
            {"label": l, "minutes": m, "count": c, "color": colors.get(l, "#DA291C")}
            for l, m, c in by_label
        ],
        "recent": [
            {"time": datetime.fromtimestamp(ts).strftime("%H:%M"), "date": datetime.fromtimestamp(ts).strftime("%d %b"),
             "minutes": m, "label": l}
            for ts, m, l in recent
        ],
    }


# ---------------------------------------------------------------- page

def _hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def render_html() -> str:
    d = collect(14)
    peak = max([s["minutes"] for s in d["series"]] + [1])

    cards = [
        ("TODAY", d["today"]),
        ("THIS WEEK", d["week"]),
        ("THIS MONTH", d["month"]),
        ("ALL TIME", d["all"]),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v">{_hm(v["minutes"])}</div>'
        f'<div class="s">{v["sessions"]} session{"s" if v["sessions"] != 1 else ""}</div></div>'
        for k, v in cards
    )

    bars = ""
    for s in d["series"]:
        h = int((s["minutes"] / peak) * 100) if peak else 0
        cls = "bar zero" if not s["minutes"] else "bar"
        bars += (
            f'<div class="col" title="{s["day"]}: {_hm(s["minutes"])}">'
            f'<div class="barwrap"><div class="{cls}" style="height:{max(h, 2)}%"></div></div>'
            f'<div class="lbl">{s["weekday"][0]}</div></div>'
        )

    total_label_min = sum(x["minutes"] for x in d["by_label"]) or 1
    labels_html = "".join(
        f'<div class="lrow"><span class="dot" style="background:{x["color"]}"></span>'
        f'<span class="lname">{x["label"]}</span>'
        f'<span class="lbar"><i style="width:{int(x["minutes"] / total_label_min * 100)}%;background:{x["color"]}"></i></span>'
        f'<span class="lval">{_hm(x["minutes"])}</span></div>'
        for x in d["by_label"]
    ) or '<div class="empty">Nothing yet. Finish a focus block and this fills in.</div>'

    recent_html = "".join(
        f'<tr><td>{r["date"]}</td><td>{r["time"]}</td><td>{r["minutes"]} min</td>'
        f'<td><span class="pill">{r["label"]}</span></td></tr>'
        for r in d["recent"]
    ) or '<tr><td colspan="4" class="empty">No sessions recorded yet. Finish a focus block and it lands here.</td></tr>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Pomodoro stats</title>
<style>
  :root {{ --bg:#0d0d0d; --card:#151515; --line:#242424; --fg:#ffffff;
           --muted:#8a8a8a; --accent:#DA291C; --blue:#89b4fa; --green:#a6e3a1; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--fg); font:14px/1.5 "JetBrainsMono Nerd Font",ui-monospace,monospace;
          padding:28px; max-width:1080px; margin:0 auto; }}
  header {{ display:flex; align-items:baseline; gap:14px; margin-bottom:22px; }}
  h1 {{ font-size:20px; letter-spacing:2px; font-weight:700; }}
  h1 span {{ color:var(--accent); }}
  .gen {{ color:var(--muted); font-size:12px; margin-left:auto; }}
  .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:12px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 16px; }}
  .k {{ color:var(--muted); font-size:11px; letter-spacing:1.6px; }}
  .v {{ font-size:26px; font-weight:700; margin:6px 0 2px; }}
  .s {{ color:var(--muted); font-size:12px; }}
  .panel {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; margin-bottom:12px; }}
  .pt {{ color:var(--muted); font-size:11px; letter-spacing:1.6px; margin-bottom:14px; }}
  .chart {{ display:flex; align-items:flex-end; gap:6px; height:150px; }}
  .col {{ flex:1; display:flex; flex-direction:column; align-items:center; gap:6px; height:100%; }}
  .barwrap {{ flex:1; width:100%; display:flex; align-items:flex-end; }}
  .bar {{ width:100%; background:var(--accent); border-radius:4px 4px 0 0; min-height:2px; transition:height .2s; }}
  .bar.zero {{ background:#222; }}
  .lbl {{ color:var(--muted); font-size:10px; }}
  .lrow {{ display:flex; align-items:center; gap:10px; margin-bottom:10px; }}
  .dot {{ width:9px; height:9px; border-radius:50%; flex:none; }}
  .lname {{ width:80px; color:#ddd; }}
  .lbar {{ flex:1; height:8px; background:#1e1e1e; border-radius:4px; overflow:hidden; }}
  .lbar i {{ display:block; height:100%; border-radius:4px; }}
  .lval {{ width:70px; text-align:right; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:7px 0; border-bottom:1px solid var(--line); color:#ccc; }}
  tr:last-child td {{ border-bottom:none; }}
  .pill {{ background:#1e1e1e; border-radius:8px; padding:2px 8px; font-size:11px; }}
  .empty {{ color:var(--muted); }}
  .streak {{ color:var(--muted); font-size:12px; margin-top:8px; }}
  @media (max-width:720px) {{ .grid {{ grid-template-columns:repeat(2,1fr); }} }}
</style></head>
<body>
<header><h1>POMODORO<span>.</span></h1><div class="gen">{d["generated"]}</div></header>
<div class="grid">{card_html}</div>
<div class="panel"><div class="pt">LAST 14 DAYS</div><div class="chart">{bars}</div>
  <div class="streak">Streak: {d["streak"]} day{"s" if d["streak"] != 1 else ""} in a row</div></div>
<div class="panel"><div class="pt">BY LABEL</div>{labels_html}</div>
<div class="panel"><div class="pt">RECENT SESSIONS</div>
<table>{recent_html}</table></div>
</body></html>"""


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] in ("/", "/index.html", "/stats"):
            body = render_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/data.json"):
            body = json.dumps(collect(30)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_a):  # keep the terminal quiet
        pass


PREFERRED_PORT = 8791


def start_server() -> int:
    """Start the stats page on 127.0.0.1 and return the port.

    The port is fixed so the URL is stable and bookmarkable. If it is taken we
    fall back to any free port rather than refusing to start.
    """
    init_db()
    srv = None
    for port in (PREFERRED_PORT, 0):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        raise OSError("could not bind the stats server")
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with open(PORT_FILE, "w") as f:
            f.write(str(srv.server_port))
    except OSError:
        pass
    return srv.server_port


if __name__ == "__main__":
    init_db()
    port = start_server()
    print(f"http://127.0.0.1:{port}/")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass

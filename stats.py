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
from contextlib import contextmanager
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DATA_DIR = os.path.expanduser("~/.local/share/pomodoro")
DB_PATH = os.path.join(DATA_DIR, "history.db")

LABEL_PALETTE = ["#DA291C", "#89b4fa", "#a6e3a1", "#FFC72C",
                 "#cba6f7", "#fab387", "#94e2d5", "#f5c2e7"]

DEFAULT_LABELS = [
    ("focus", "#DA291C"),
    ("study", "#89b4fa"),
    ("code", "#a6e3a1"),
    ("review", "#FFC72C"),
]

PORT_FILE = os.path.join(DATA_DIR, "port")

_lock = threading.Lock()


@contextmanager
def _db():
    """A connection that commits on success and always closes.

    `with sqlite3.connect(...)` commits but never closes (fd leak), and
    `closing()` closes but never commits (silent data loss). Neither alone is
    correct, so do both here.
    """
    db = sqlite3.connect(DB_PATH)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------- storage

def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with _db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ended_at  INTEGER NOT NULL,
                minutes   INTEGER NOT NULL,
                label     TEXT    NOT NULL DEFAULT 'focus'
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_ended ON sessions(ended_at)")
        cols = [r[1] for r in db.execute("PRAGMA table_info(sessions)")]
        if "sref" not in cols:
            db.execute("ALTER TABLE sessions ADD COLUMN sref TEXT")
        db.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                sref    TEXT NOT NULL,
                app     TEXT NOT NULL,
                title   TEXT NOT NULL,
                seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (sref, app, title)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS labels (
                name  TEXT PRIMARY KEY,
                color TEXT NOT NULL
            )
        """)
        for name, color in DEFAULT_LABELS:
            db.execute("INSERT OR IGNORE INTO labels(name, color) VALUES (?, ?)", (name, color))


def record(minutes: int, label: str = "focus", ended_at: int | None = None,
           sref: str | None = None) -> None:
    if minutes <= 0:
        return
    with _lock, _db() as db:
        db.execute(
            "INSERT INTO sessions(ended_at, minutes, label, sref) VALUES (?, ?, ?, ?)",
            (ended_at or int(time.time()), int(minutes), label or "focus", sref),
        )


def add_activity(sref: str, app: str, title: str, seconds: int) -> None:
    if not sref or seconds <= 0:
        return
    with _lock, _db() as db:
        db.execute(
            "INSERT INTO activity(sref, app, title, seconds) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(sref, app, title) DO UPDATE SET seconds = seconds + excluded.seconds",
            (sref, app[:64], title[:160], int(seconds)),
        )


def discard_activity(sref: str | None) -> None:
    if not sref:
        return
    with _lock, _db() as db:
        db.execute("DELETE FROM activity WHERE sref = ?", (sref,))


def ensure_label(name: str) -> None:
    """Give a freshly typed label a colour so the stats page can show it."""
    name = (name or "").strip()[:32]
    if not name:
        return
    with _lock, _db() as db:
        row = db.execute("SELECT COUNT(*) FROM labels WHERE name = ?", (name,)).fetchone()
        if row and row[0]:
            return
        n = db.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
        db.execute("INSERT INTO labels(name, color) VALUES (?, ?)",
                   (name, LABEL_PALETTE[n % len(LABEL_PALETTE)]))


def labels() -> list[tuple[str, str]]:
    with _db() as db:
        rows = db.execute("SELECT name, color FROM labels ORDER BY rowid").fetchall()
    return rows or DEFAULT_LABELS


# ---------------------------------------------------------------- queries

def _day_start(d: date) -> int:
    return int(datetime(d.year, d.month, d.day).timestamp())


def collect(days: int = 14, day: date | None = None) -> dict:
    today = date.today()
    day = day or today
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    with _db() as db:
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

        # activity for the selected day only: a block belongs to the day it ended
        dstart = _day_start(day)
        rows = db.execute("""
            SELECT a.app, a.title, SUM(a.seconds)
            FROM activity a JOIN sessions s ON s.sref = a.sref
            WHERE a.seconds > 0 AND s.ended_at >= ? AND s.ended_at < ?
            GROUP BY a.app, a.title
            ORDER BY 3 DESC
        """, (dstart, dstart + 86400)).fetchall()

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

    apps = {}
    for app_name, title, secs in rows:
        e = apps.setdefault(app_name, {"app": app_name, "seconds": 0, "titles": []})
        e["seconds"] += secs
        e["titles"].append({"title": title, "seconds": secs})
    app_list = sorted(apps.values(), key=lambda x: -x["seconds"])

    colors = dict(labels())
    return {
        "day": day.isoformat(),
        "day_label": "Today" if day == today else day.strftime("%a %d %b"),
        "apps": app_list,
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

def read_live():
    """The block that is running right now, so the page is not silent mid-session."""
    X = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    try:
        with open(os.path.join(X, "pomodoro", "state.json")) as f:
            d = json.load(f)
    except Exception:
        return None
    phase = d.get("phase")
    if phase == "idle" or not d.get("remaining_s"):
        return None
    left = d["remaining_s"]
    if d.get("running"):
        left = max(0.0, left - (time.time() - d.get("updated_at", 0)))
    dur = {"focus": d.get("cfg", {}).get("focus"),
           "break": d.get("cfg", {}).get("break"),
           "longbreak": d.get("cfg", {}).get("long")}.get(phase)
    if not dur:
        return None
    elapsed = max(0.0, dur * 60 - left)
    now_app = None
    sref = d.get("sref")
    if sref:
        try:
            with _db() as db:
                r = db.execute(
                    "SELECT app, title FROM activity WHERE sref=? ORDER BY seconds DESC LIMIT 1",
                    (sref,)).fetchone()
            if r:
                now_app = f"{r[0]} — {r[1]}"
        except Exception:
            pass
    return {"app": now_app, "phase": phase, "running": bool(d.get("running")),
            "alarm": bool(d.get("alarm")), "label": d.get("label") or "focus",
            "elapsed_min": int(elapsed // 60), "elapsed_s": int(elapsed % 60),
            "left": int(left)}


ICONS = {
    "browser": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.6 3 14.4 0 18M12 3c-3 3.6-3 14.4 0 18"/></svg>',
    "terminal": '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7.5 9.5l3 2.5-3 2.5M13 15h4"/></svg>',
    "note": '<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 13h6M9 17h4"/></svg>',
    "music": '<svg viewBox="0 0 24 24"><path d="M9 17V6l10-2v11"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="16.5" cy="16" r="2.5"/></svg>',
    "video": '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10.5 9.5l4.5 2.5-4.5 2.5z"/></svg>',
    "code": '<svg viewBox="0 0 24 24"><path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/></svg>',
    "chat": '<svg viewBox="0 0 24 24"><path d="M4 5h16v11H9.5L4 20z"/></svg>',
    "folder": '<svg viewBox="0 0 24 24"><path d="M3 7h6l2 2h10v10H3z"/></svg>',
    "generic": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/></svg>',
}

APP_ICON_RULES = [
    ("browser", ("firefox", "helium", "chromium", "chrome", "brave", "edge", "browser")),
    ("terminal", ("ghostty", "kitty", "alacritty", "foot", "wezterm", "terminal", "konsole")),
    ("folder", ("nautilus", "thunar", "dolphin", "nemo", "pcmanfm", "files")),
    ("note", ("obsidian", "notes", "logseq", "joplin")),
    ("music", ("spotifast", "spotify", "music", "rhythmbox", "feishin")),
    ("video", ("obsproject", "studio", "video", "mpv", "vlc", "celluloid", "totem")),
    ("code", ("code", "cursor", "neovim", "nvim", "idea", "pycharm", "sublime")),
    ("chat", ("discord", "slack", "telegram", "signal", "element")),
]


def icon_for(app: str) -> str:
    a = (app or "").lower()
    for key, needles in APP_ICON_RULES:
        if any(n in a for n in needles):
            return ICONS[key]
    return ICONS["generic"]


def _dur(seconds: int) -> str:
    sec = int(seconds)
    if sec < 60:
        return f"{sec}s"
    h, rest = divmod(sec, 3600)
    m = rest // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def render_html(day: date | None = None) -> str:
    today = date.today()
    day = day or today
    d = collect(14, day=day)
    live = read_live()
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

    if live:
        label = {"focus": "FOCUS", "break": "BREAK", "longbreak": "LONG BREAK"}.get(live["phase"], "SESSION")
        state = "PAUSED" if not live["running"] else ("ALARM" if live["alarm"] else "IN PROGRESS")
        m, sec = divmod(live["left"], 60)
        now_html = (
            f'<div class="now{" alarm" if live["alarm"] else ""}">'
            f'<span class="live-dot"></span>'
            f'<b>{label}</b> {state} &middot; {live["elapsed_min"]}m {live["elapsed_s"]:02d}s elapsed'
            f' &middot; {m}:{sec:02d} left &middot; {live.get("app") or live["label"]}'
            f'<span class="hint">saved when the block reaches zero</span></div>'
        )
    else:
        now_html = ""

    if d["apps"]:
        total_sec = sum(a["seconds"] for a in d["apps"]) or 1
        apps_html = ""
        for a in d["apps"][:8]:
            pct = max(2, int(a["seconds"] / total_sec * 100))
            sub = "".join(
                f'<div class="trow"><span class="ttl">{t["title"]}</span>'
                f'<span class="tmin">{_dur(t["seconds"])}</span></div>'
                for t in a["titles"][:3])
            apps_html += (
                f'<div class="arow"><span class="ico">{icon_for(a["app"])}</span>'
                f'<span class="aname">{a["app"]}</span>'
                f'<span class="abar"><i style="width:{pct}%"></i></span>'
                f'<span class="aval">{_dur(a["seconds"])}</span></div>{sub}')
    else:
        apps_html = ('<div class="empty">No app activity recorded on this day. '
                     'It is sampled every 5 s while a focus block runs.</div>')

    chips = ""
    for i in range(6, -1, -1):
        dd = today - timedelta(days=i)
        href = "/" if dd == today else f"/?day={dd.isoformat()}"
        cls = "chip active" if dd == day else "chip"
        txt = "Today" if dd == today else dd.strftime("%a %d")
        chips += f'<a class="{cls}" href="{href}">{txt}</a>' 

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
  .days {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px; }}
  .chip {{ color:var(--muted); text-decoration:none; font-size:11px; letter-spacing:1px;
           padding:5px 10px; border-radius:9px; background:#151515; border:1px solid #242424; }}
  .chip:hover {{ color:#fff; border-color:#333; }}
  .chip.active {{ color:#fff; background:#DA291C; border-color:#DA291C; }}
  .streak {{ color:var(--muted); font-size:12px; margin-top:8px; }}
  .now {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap;
         background:#161616; border:1px solid #2a2a2a; border-left:3px solid var(--accent);
         border-radius:12px; padding:12px 16px; margin-bottom:12px; color:#ddd; }}
  .now.alarm {{ border-left-color:#ffffff; background:#1d1010; }}
  .now b {{ color:#fff; letter-spacing:1.5px; }}
  .arow {{ display:flex; align-items:center; gap:10px; margin-bottom:6px; }}
  .ico {{ width:16px; height:16px; flex:none; display:inline-flex; }}
  .ico svg {{ width:16px; height:16px; stroke:#8a8a8a; fill:none; stroke-width:1.6;
              stroke-linecap:round; stroke-linejoin:round; }}
  .aname {{ width:190px; color:#ddd; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .abar {{ flex:1; height:8px; background:#1e1e1e; border-radius:4px; overflow:hidden; }}
  .abar i {{ display:block; height:100%; background:var(--accent); border-radius:4px; }}
  .aval {{ width:70px; text-align:right; color:var(--muted); }}
  .trow {{ display:flex; gap:10px; padding:1px 0 3px 26px; color:var(--muted); font-size:11.5px; }}
  .trow .ttl {{ flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .trow .tmin {{ width:70px; text-align:right; }}
  .now .hint {{ margin-left:auto; padding-left:14px; border-left:1px solid #2a2a2a;
                color:var(--muted); font-size:11px; }}
  .live-dot {{ width:8px; height:8px; border-radius:50%; background:var(--accent);
               box-shadow:0 0 0 4px rgba(218,41,28,.18); flex:none; }}
  @media (max-width:720px) {{ .grid {{ grid-template-columns:repeat(2,1fr); }} }}
</style></head>
<body>
<header><h1>POMODORO<span>.</span></h1><div class="gen">{d["generated"]}</div></header>
{now_html}
<div class="grid">{card_html}</div>
<div class="panel"><div class="pt">LAST 14 DAYS</div><div class="chart">{bars}</div>
  <div class="streak">Streak: {d["streak"]} day{"s" if d["streak"] != 1 else ""} in a row</div></div>
<div class="panel"><div class="pt">BY LABEL</div>{labels_html}</div>
<div class="days">{chips}</div>
<div class="panel"><div class="pt">WHAT YOU WORKED ON &middot; {d["day_label"].upper()}</div>{apps_html}</div>
<div class="panel"><div class="pt">RECENT SESSIONS</div>
<table>{recent_html}</table></div>
</body></html>"""


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        day = None
        if q.get("day"):
            try:
                day = date.fromisoformat(q["day"][0])
            except ValueError:
                day = None

        if self.path.split("?")[0] in ("/", "/index.html", "/stats"):
            body = render_html(day).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/data.json"):
            body = json.dumps(collect(30, day=day)).encode("utf-8")
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

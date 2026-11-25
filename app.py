#!/usr/bin/env python3
"""Pomodoro daemon + dropdown panel.

The daemon owns the timer, serves a unix socket for control, and keeps the
Waybar module in sync via $XDG_RUNTIME_DIR/pomodoro/state.json.

The panel is a normal floating GTK4 window. Hyprland floats it and hides it
from the taskbar via a window rule. It is opened on the monitor that has
focus, so it follows you.
"""

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")

import atexit
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

from gi.repository import Gdk, GLib, Gtk

import stats

PORT = 0
ALARM_PROC = None
ALARM_NOTIFY = None
SAMPLE_SECS = 5
PRIVATE_APPS = ("keepassxc", "bitwarden", "1password", "gnome-keyring")

XDG = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
SOCK = os.path.join(XDG, "pomodoro.sock")
PIDF = os.path.join(XDG, "pomodoro.pid")
STATEDIR = os.path.join(XDG, "pomodoro")
STATE = os.path.join(STATEDIR, "state.json")
DATA_RUNTIME = os.path.expanduser("~/.local/share/pomodoro")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log")

APP_ID = "io.github.hawkaii.pomodoro"

LABELS = {"focus": "FOCUS", "break": "BREAK", "long": "LONG BREAK", "every": "EVERY"}

S = {
    "phase": "idle",
    "running": False,
    "remaining_s": 0,
    "cfg": {"focus": 20, "break": 5, "long": 20, "every": 4},
    "sessions": 0,
    "breaks": 0,
    "panel_open": False,
    "auto": True,
    "label": "focus",
    "alarm": False,
    "sref": None,
    "ticks": 0,
    "updated_at": 0.0,
}

W = {}


def _log(*a):
    try:
        with open(LOG, "a") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except OSError:
        pass


def _on_exception(t, v, tb):
    import traceback
    _log("EXCEPTION:", t, v)
    _log("".join(traceback.format_tb(tb)))


sys.excepthook = _on_exception


# ---------------------------------------------------------------- state

def fmt(seconds):
    s = max(0, int(seconds + 0.5))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def load():
    try:
        with open(STATE) as f:
            S.update(json.load(f))
    except Exception:
        save()


def save():
    os.makedirs(STATEDIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(S, f)
    os.replace(tmp, STATE)


def now():
    return time.time()


def ctl(cmd):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(SOCK)
        s.sendall((cmd + "\n").encode())
        s.close()
    except OSError:
        pass


def alarm_wav():
    """A short two-tone chime, synthesised once into the data dir."""
    import math
    import struct
    import wave

    os.makedirs(os.path.join(DATA_RUNTIME, ""), exist_ok=True)
    path = os.path.join(DATA_RUNTIME, "alarm.wav")
    if os.path.exists(path) and os.path.getsize(path) > 2000:
        return path
    rate = 48000

    def tone(freq, dur, amp=0.34):
        n = int(rate * dur)
        out = []
        for i in range(n):
            env = min(1.0, i / (rate * 0.008)) * min(1.0, (n - i) / (rate * 0.02))
            out.append(amp * env * math.sin(2 * math.pi * freq * i / rate))
        return out

    gap = [0.0] * int(rate * 0.09)
    rest = [0.0] * int(rate * 0.30)
    seq = []
    for _ in range(3):
        seq += tone(880, 0.12) + gap + tone(1318, 0.14) + rest
    try:
        with wave.open(path, "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"".join(struct.pack("<hh", int(v * 32767), int(v * 32767)) for v in seq))
    except OSError as exc:
        _log("could not write alarm wav:", exc)
    return path


def alarm_start():
    """Loop the chime until alarm_stop(). One process, killed as a group."""
    global ALARM_PROC
    if ALARM_PROC and ALARM_PROC.poll() is None:
        return
    path = alarm_wav()
    if not os.path.exists(path):
        return
    try:
        ALARM_PROC = subprocess.Popen(
            ["sh", "-c", f'while :; do paplay "{path}"; done'],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        _log("alarm_start failed:", exc)
        ALARM_PROC = None


def alarm_stop():
    global ALARM_PROC, ALARM_NOTIFY
    if ALARM_NOTIFY:
        try:
            ALARM_NOTIFY.kill()
        except Exception:
            pass
    if not ALARM_PROC:
        return
    try:
        os.killpg(os.getpgid(ALARM_PROC.pid), signal.SIGTERM)
    except Exception:
        try:
            ALARM_PROC.terminate()
        except Exception:
            pass
    ALARM_PROC = None


def notify_alarm(title, body):
    """Critical notification. If the daemon reports a click, dismiss the alarm.

    Bounded to 3 minutes and killed on dismiss — an unbounded --wait left a
    blocked thread and two descriptors behind for every block.
    """
    global ALARM_NOTIFY

    def run():
        global ALARM_NOTIFY
        try:
            ALARM_NOTIFY = subprocess.Popen(
                ["notify-send", "-u", "critical", "-a", "pomodoro",
                 "-A", "default=Dismiss", title, body],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            out, _ = ALARM_NOTIFY.communicate(timeout=180)
            if out and out.strip():
                GLib.idle_add(lambda: (cmd_dismiss(), False)[1])
        except subprocess.TimeoutExpired:
            try:
                ALARM_NOTIFY.kill()
            except Exception:
                pass
        except Exception as exc:
            _log("notify_alarm failed:", exc)
        finally:
            ALARM_NOTIFY = None

    threading.Thread(target=run, daemon=True).start()


def new_sref():
    return uuid.uuid4().hex[:12]


def clean_title(app, title):
    """Turn a window title into a readable label, per app."""
    t = (title or "").strip()
    a = (app or "").lower()
    if "firefox" in a:
        t = re.sub(r"\s+[\u2014\u2013-]\s+.*?Mozilla Firefox\s*$", "", t)
    if any(x in a for x in ("helium", "chromium", "chrome", "brave", "edge")):
        t = re.sub(r"\s+-\s+(Helium|Chromium|Google Chrome|Brave|Microsoft Edge)\s*$",
                   "", t, flags=re.I)
    if "obsidian" in a:
        t = re.sub(r"\s+-\s+.*?\s+-\s+Obsidian\s+[\d.]+\s*$", "", t)
    if "mpv" in a:
        t = re.sub(r"\s+-\s+mpv\s*$", "", t)
    if any(x in a for x in ("vlc", "celluloid", "totem")):
        t = re.sub(r"\s+-\s+(VLC media player|Celluloid|Videos)\s*$", "", t, flags=re.I)
    if any(x in a for x in ("ghostty", "kitty", "alacritty", "foot", "wezterm")):
        if ": " in t:
            t = t.split(": ", 1)[1]
    return (t or app or "unknown")[:120]


def sample_activity():
    """Record what has focus, but only while a focus block is running."""
    if not (S["phase"] == "focus" and S["running"] and not S.get("alarm")):
        return
    sref = S.get("sref")
    if not sref:
        return
    try:
        out = subprocess.run(["hyprctl", "activewindow", "-j"],
                             capture_output=True, timeout=2, text=True)
        d = json.loads(out.stdout or "{}")
    except Exception:
        return
    cls = (d.get("class") or "").strip()
    if not cls or cls == APP_ID:
        return
    low = cls.lower()
    title = cls if any(p in low for p in PRIVATE_APPS) else clean_title(cls, d.get("title"))
    try:
        stats.add_activity(sref, cls, title, SAMPLE_SECS)
    except Exception as exc:
        _log("activity sample failed:", exc)


def notify(title, body):
    try:
        subprocess.Popen(["notify-send", "-a", "pomodoro", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def label_names():
    try:
        return [n for n, _ in stats.labels()]
    except Exception:
        return ["focus"]


def cmd_cycle_label():
    names = label_names()
    cur = S.get("label") or names[0]
    i = names.index(cur) + 1 if cur in names else 0
    S["label"] = names[i % len(names)]
    save()
    sync_ui()


def open_stats():
    url = f"http://127.0.0.1:{PORT}/"
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def phase_css():
    if S["phase"] == "focus":
        return "pomodoro-focus"
    if S["phase"] in ("break", "longbreak"):
        return "pomodoro-break"
    if S["running"]:
        return "pomodoro-paused"
    return "pomodoro-idle"


# ---------------------------------------------------------------- timer

def cmd_toggle():
    t = time.monotonic()
    if t - W.get("last_toggle", 0.0) < 0.4:
        return        # the panel can open under the cursor and replay the click
    W["last_toggle"] = t
    if S["panel_open"]:
        cmd_close()
    else:
        cmd_open()


def cmd_open():
    _log("cmd_open")
    # Opening is not starting. Nothing runs until START is pressed.
    S["panel_open"] = True
    save()
    sync_ui()
    GLib.idle_add(set_panel, True)


def cmd_close():
    _log("cmd_close")
    S["panel_open"] = False
    save()
    sync_ui()
    GLib.idle_add(set_panel, False)


def cmd_start():
    if S["phase"] == "idle":
        S["phase"] = "focus"
        S["remaining_s"] = S["cfg"]["focus"] * 60
    if S["phase"] == "focus" and not S.get("sref"):
        S["sref"] = new_sref()
    S["running"] = True
    S["updated_at"] = now()
    save()
    sync_ui()


def cmd_primary():
    """The middle button: dismiss the alarm, else start/pause/resume."""
    if S.get("alarm"):
        cmd_dismiss()
        return
    if S["phase"] == "idle":
        cmd_start()
    elif S["running"]:
        cmd_pause()
    else:
        S["running"] = True
        S["updated_at"] = now()
        save()
        sync_ui()


def cmd_pause():
    S["running"] = False
    save()
    sync_ui()


def cmd_reset():
    try:
        stats.discard_activity(S.get("sref"))
    except Exception as exc:
        _log("discard failed:", exc)
    S["phase"] = "idle"
    S["running"] = False
    S["remaining_s"] = 0
    S["sref"] = None
    save()
    sync_ui()


def phase_dur():
    if S["phase"] == "focus":
        return S["cfg"]["focus"] * 60
    if S["phase"] == "break":
        return S["cfg"]["break"] * 60
    if S["phase"] == "longbreak":
        return S["cfg"]["long"] * 60
    return 0


def advance(record=True, ring=True):
    """Move to the next phase.

    ring=True  -> the block ended on its own: stop the clock, sound the alarm.
    ring=False -> SKIP was pressed: move on without an alarm or a saved block.
    """
    if S["phase"] == "focus":
        if not record:
            try:
                stats.discard_activity(S.get("sref"))
            except Exception as exc:
                _log("discard failed:", exc)
        if record:
            try:
                stats.record(S["cfg"]["focus"], S.get("label") or "focus",
                             sref=S.get("sref"))
                _log("recorded", S["cfg"]["focus"], "min label=",
                     S.get("label"), "sref=", S.get("sref"))
            except Exception as exc:      # stats must never break the timer
                _log("stats.record failed:", exc)
        S["sessions"] += 1
        S["breaks"] += 1
        if S["breaks"] >= S["cfg"]["every"]:
            S["phase"] = "longbreak"
            S["breaks"] = 0
        else:
            S["phase"] = "break"
        S["remaining_s"] = S["cfg"]["long" if S["phase"] == "longbreak" else "break"] * 60
    elif S["phase"] in ("break", "longbreak"):
        S["phase"] = "focus"
        S["remaining_s"] = S["cfg"]["focus"] * 60
        S["sref"] = new_sref()
    else:
        return

    if S["phase"] in ("break", "longbreak"):
        S["sref"] = None

    S["updated_at"] = now()
    if ring:
        S["running"] = False        # nothing drains while the alarm rings
        S["alarm"] = True
    else:
        S["running"] = True
    save()
    sync_ui()

    if ring:
        alarm_start()
        label = {"focus": "Focus done", "break": "Break over",
                 "longbreak": "Long break over"}.get(S["phase"], "Timer")
        extra = " - tap to dismiss" if S["auto"] else ""
        notify_alarm(f"{label}", f"{phase_text()}: {fmt(S['remaining_s'])}{extra}")


def cmd_dismiss():
    S["alarm"] = False
    alarm_stop()
    if S["auto"] and S["phase"] != "idle" and S["remaining_s"] > 0:
        S["running"] = True         # AUTO: carry on into the next phase
        S["updated_at"] = now()
    save()
    sync_ui()


def cmd_skip():
    advance(record=False, ring=False)   # skipping is not completed work


def cmd_set(key, value):
    if key in S["cfg"]:
        S["cfg"][key] = max(1, int(value))
    save()
    sync_ui()


def cmd_set_auto(on):
    S["auto"] = on
    save()
    sync_ui()


def on_tick():
    # A raise here would make GLib drop the timeout and freeze the clock
    # forever, so nothing may escape.
    try:
        return _tick()
    except Exception as exc:
        _log("tick failed:", exc)
        return True


def _tick():
    if S["running"] and S["phase"] != "idle":
        S["remaining_s"] -= 1
        S["updated_at"] = now()
        if S["remaining_s"] <= 0:
            advance()
        save()          # only write while a session is actually running
        sync_ui()
        S["ticks"] = S.get("ticks", 0) + 1
        if S["ticks"] % SAMPLE_SECS == 0:
            sample_activity()
    return True


# ---------------------------------------------------------------- socket

def _listen():
    try:
        _serve()
    except Exception as exc:          # never die silently
        import traceback
        _log("listener died:", exc)
        _log(traceback.format_exc())


def _serve():
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(8)
    srv.setblocking(False)
    _log("listener up on", SOCK)
    while True:
        try:
            c, _ = srv.accept()
        except BlockingIOError:
            time.sleep(0.2)      # never busy-loop
            continue
        except OSError:
            time.sleep(0.5)
            continue
        c.settimeout(1.0)
        data = b""
        try:
            while b"\n" not in data and len(data) < 8192:
                chunk = c.recv(1024)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        finally:
            c.close()
        cmd = data.decode(errors="replace").strip()
        if cmd:
            GLib.idle_add(dispatch, cmd)


def dispatch(cmd):
    handlers = {
        "toggle": cmd_toggle, "open": cmd_open, "close": cmd_close,
        "start": cmd_start, "pause": cmd_pause, "reset": cmd_reset,
        "skip": cmd_skip, "label": cmd_cycle_label, "stats": open_stats,
        "primary": cmd_primary, "dismiss": cmd_dismiss,
    }
    if cmd in handlers:
        handlers[cmd]()
    elif cmd.startswith("set_auto:"):
        cmd_set_auto(cmd[9:] == "1")
    elif cmd.startswith("set_"):
        key, _, val = cmd[4:].partition(":")
        if val:
            cmd_set(key, val)
    return False          # idle callbacks must return False or they repeat


# ---------------------------------------------------------------- window

def focused_monitor():
    try:
        out = subprocess.run(["hyprctl", "activeworkspace", "-j"],
                             capture_output=True, timeout=2, text=True)
        return json.loads(out.stdout).get("monitor", "")
    except Exception:
        return ""


def place_window():
    """Park the panel at the top-right of the focused monitor.

    Wayland clients cannot position themselves, and Hyprland's movewindowpixel
    refuses this window, so we compute the delta and use moveactive.
    """
    if not W.get("win"):
        return
    try:
        clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"],
                                            capture_output=True, timeout=2, text=True).stdout)
        monitors = json.loads(subprocess.run(["hyprctl", "monitors", "-j"],
                                             capture_output=True, timeout=2, text=True).stdout)
    except Exception:
        return
    me = next((c for c in clients if c.get("class") == APP_ID), None)
    mon = next((m for m in monitors if m.get("focused")), None)
    if not me or not mon:
        return
    scale = mon.get("scale") or 1.0
    logical_w = mon["width"] / scale
    w, h = me["size"]
    target_x = mon["x"] + logical_w - w - 40
    target_y = mon["y"] + 48
    dx = int(target_x - me["at"][0])
    dy = int(target_y - me["at"][1])
    if dx or dy:
        subprocess.run(["hyprctl", "dispatch", "moveactive", str(dx), str(dy)],
                       capture_output=True, timeout=2)


def set_panel(up):
    _log("set_panel", up, "win" if W.get("win") else "nowin")
    win = W.get("win")
    if not win:
        return
    if up:
        win.set_visible(True)
        win.present()
        sync_ui(force=True)
        GLib.timeout_add(120, lambda: (place_window(), False)[1])
    else:
        win.set_visible(False)


def phase_text():
    return {"focus": "FOCUS", "break": "BREAK",
            "longbreak": "LONG BREAK", "idle": "READY"}.get(S["phase"], "READY")


def ring_rgb():
    if S["phase"] in ("break", "longbreak"):
        return (0.537, 0.706, 0.980)      # ice blue
    if S["phase"] == "idle":
        return (0.35, 0.35, 0.35)
    return (0.855, 0.161, 0.110)          # accent red


def draw_ring(area, cr, width, height):
    import math
    cx, cy = width / 2.0, height / 2.0
    r = max(10.0, min(width, height) / 2.0 - 8)
    cr.set_line_width(9)
    cr.set_line_cap(1)                    # cairo ROUND_CAP
    cr.set_source_rgb(0.14, 0.14, 0.14)
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.stroke()
    frac = max(0.0, min(1.0, W.get("frac", 0.0)))
    if frac > 0.002:
        cr.set_source_rgb(*W.get("ring_color", (0.855, 0.161, 0.110)))
        cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * frac)
        cr.stroke()


def sync_ui(force=False):
    if not W:
        return
    win = W.get("win")
    if not force and (win is None or not win.get_visible()):
        return          # nothing on screen; state.json already has the truth
    rem = S["remaining_s"]
    if S["running"]:
        rem = max(0, rem - (now() - S["updated_at"]))

    W["digits"].set_text(fmt(rem))
    W["phase"].set_text(phase_text())
    W["root"].set_css_classes([phase_css()])

    d = phase_dur()
    W["frac"] = (rem / d) if d > 0 else 0.0
    W["ring_color"] = ring_rgb()
    W["ring"].queue_draw()

    n = max(1, int(S["cfg"]["every"]))
    done = max(0, min(int(S["breaks"]), n))
    W["rounds"].set_markup(
        f'<span foreground="#DA291C">{"● " * done}</span>'
        f'<span foreground="#3d3d3d">{"● " * (n - done)}</span>'
    )

    if S.get("alarm"):
        W["primary"].set_label("DISMISS")
        W["primary"].add_css_class("alarm")
    else:
        W["primary"].remove_css_class("alarm")
        W["primary"].set_label("START" if S["phase"] == "idle"
                               else ("PAUSE" if S["running"] else "RESUME"))

    for key, w in W["steppers"].items():
        w["val"].set_text(str(S["cfg"][key]))

    W["label"].set_label(S.get("label") or "focus")
    W["auto"].set_label("AUTO")
    if S["auto"]:
        W["auto"].add_css_class("on")
    else:
        W["auto"].remove_css_class("on")


# ---------------------------------------------------------------- UI

def stepper(box, key, label, value):
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2, name=f"row-{key}")
    row.append(Gtk.Label(label=label, name="steplabel", halign=Gtk.Align.START, hexpand=True))
    minus = Gtk.Button(label="−", name="stepbtn")
    minus.connect("clicked", lambda b: cmd_set(key, S["cfg"][key] - 1))
    val = Gtk.Label(label=str(value), name="stepval", halign=Gtk.Align.CENTER)
    W["steppers"][key] = {"val": val}
    plus = Gtk.Button(label="+", name="stepbtn")
    plus.connect("clicked", lambda b: cmd_set(key, S["cfg"][key] + 1))
    row.append(minus)
    row.append(val)
    row.append(plus)
    box.append(row)


def build_ui(win):
    W["steppers"] = {}
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, name="pomodoro-panel")
    for side in ("top", "bottom", "start", "end"):
        getattr(root, f"set_margin_{side}")(12)
    root.set_size_request(296, -1)
    W["root"] = root
    win.set_child(root)

    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, name="pomodoro-head")
    root.append(head)
    head.append(Gtk.Label(label="POMODORO", name="title", hexpand=True, halign=Gtk.Align.START))

    W["auto"] = Gtk.Button(name="pomodoro-auto")
    W["auto"].connect("clicked", lambda bb: cmd_set_auto(not S["auto"]))
    head.append(W["auto"])

    close = Gtk.Button(label="✕", name="pomodoro-close")
    close.connect("clicked", lambda bb: cmd_close())
    head.append(close)

    # countdown inside a progress ring, Pomodor style
    overlay = Gtk.Overlay(name="pomodoro-ringwrap")
    root.append(overlay)
    W["ring"] = Gtk.DrawingArea(name="pomodoro-ring")
    W["ring"].set_size_request(152, 152)
    W["ring"].set_draw_func(draw_ring)
    overlay.set_child(W["ring"])

    mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                  halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    mid.set_can_target(False)
    W["digits"] = Gtk.Label(name="pomodoro-digits", halign=Gtk.Align.CENTER)
    W["phase"] = Gtk.Label(name="pomodoro-phase", halign=Gtk.Align.CENTER)
    mid.append(W["digits"])
    mid.append(W["phase"])
    overlay.add_overlay(mid)

    W["rounds"] = Gtk.Label(name="pomodoro-rounds", halign=Gtk.Align.CENTER)
    root.append(W["rounds"])

    controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, name="pomodoro-controls")
    root.append(controls)
    reset = Gtk.Button(label="RESET", name="pomodoro-btn")
    reset.connect("clicked", lambda bb: cmd_reset())
    W["primary"] = Gtk.Button(label="START", name="pomodoro-btn", css_classes=["primary"])
    W["primary"].connect("clicked", lambda bb: cmd_primary())
    skip = Gtk.Button(label="SKIP", name="pomodoro-btn")
    skip.connect("clicked", lambda bb: cmd_skip())
    controls.append(reset)
    controls.append(W["primary"])
    controls.append(skip)

    rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, name="pomodoro-rows")
    root.append(rows)
    stepper(rows, "focus", LABELS["focus"], S["cfg"]["focus"])
    stepper(rows, "break", LABELS["break"], S["cfg"]["break"])
    stepper(rows, "long", LABELS["long"], S["cfg"]["long"])
    stepper(rows, "every", LABELS["every"], S["cfg"]["every"])

    lab = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, name="pomodoro-labelrow")
    root.append(lab)
    lab.append(Gtk.Label(label="LABEL", name="steplabel", halign=Gtk.Align.START, hexpand=True))
    W["label"] = Gtk.Button(name="stepbtn")
    W["label"].connect("clicked", lambda bb: cmd_cycle_label())
    lab.append(W["label"])

    W["stats"] = Gtk.Button(label="STATS", name="pomodoro-stats", halign=Gtk.Align.CENTER)
    W["stats"].connect("clicked", lambda bb: open_stats())
    root.append(W["stats"])

    sync_ui()


CSS = """
#pomodoro-panel {
	background-color: #0d0d0d;
	border: 1px solid #242424;
	border-radius: 18px;
	padding: 12px;
	font-family: "JetBrainsMono Nerd Font";
	font-weight: bold;
	font-size: 13px;
}
#pomodoro-title { color: #8a8a8a; font-size: 11px; letter-spacing: 3px; }
#pomodoro-digits { color: #ffffff; font-size: 34px; font-weight: bold; }
#pomodoro-phase { color: #8a8a8a; font-size: 10px; letter-spacing: 3px; margin-top: 1px; }
#pomodoro-panel.pomodoro-break #pomodoro-digits { color: #89b4fa; }
#pomodoro-panel.pomodoro-paused #pomodoro-digits { color: #666666; }
#pomodoro-panel.pomodoro-idle #pomodoro-digits { color: #cfcfcf; }
#pomodoro-rounds { font-size: 11px; margin: 2px 0 4px 0; }
#pomodoro-controls { margin: 2px 0 8px 0; }
#pomodoro-btn {
	background-image: none;
	box-shadow: none;
	background-color: #1c1c1c;
	color: #e8e8e8;
	border: none;
	border-radius: 16px;
	font-size: 11px;
	letter-spacing: 1.5px;
	min-height: 30px;
	padding: 0 10px;
}
#pomodoro-btn:hover { background-color: #2a2a2a; }
#pomodoro-btn.primary { background-color: #DA291C; color: #ffffff; }
#pomodoro-btn.primary:hover { background-color: #f03a2c; }
#pomodoro-btn.alarm { background-image: none; background-color: #ffffff; color: #DA291C; }
#pomodoro-rows { margin: 0 0 6px 0; }
#steplabel { min-width: 74px; color: #8a8a8a; font-size: 10px; letter-spacing: 2px; }
#stepval { min-width: 26px; color: #ffffff; font-size: 13px; }
#stepbtn {
	background-color: #1c1c1c;
	color: #cfcfcf;
	border: none;
	border-radius: 10px;
	font-size: 13px;
	min-width: 24px;
	min-height: 24px;
	padding: 0 6px;
}
#stepbtn:hover { background-color: #2a2a2a; color: #ffffff; }
#pomodoro-auto {
	color: #4a4a4a;
	background-color: transparent;
	border: none;
	border-radius: 10px;
	font-size: 10px;
	letter-spacing: 1.5px;
	min-height: 22px;
	padding: 0 6px;
}
#pomodoro-auto.on { color: #DA291C; }
#pomodoro-auto:hover { background-color: #1c1c1c; }
#pomodoro-close {
	color: #777777;
	background-color: transparent;
	border: none;
	border-radius: 10px;
	font-size: 12px;
	min-width: 24px;
	min-height: 22px;
	padding: 0;
}
#pomodoro-close:hover { background-color: #1c1c1c; color: #DA291C; }
#pomodoro-stats {
	color: #8a8a8a;
	background-color: transparent;
	border: none;
	border-radius: 10px;
	font-size: 10px;
	letter-spacing: 2px;
	min-height: 22px;
	padding: 0 8px;
}
#pomodoro-stats:hover { background-color: #1c1c1c; color: #DA291C; }
"""

_prov = Gtk.CssProvider()
_prov.load_from_string(CSS)
Gtk.StyleContext.add_provider_for_display(
    Gdk.Display.get_default(), _prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
)


def on_activate(app):
    _log("activate start")
    win = Gtk.Window(application=app)
    W["win"] = win
    win.set_title("Pomodoro")
    win.set_decorated(False)
    win.set_resizable(False)
    build_ui(win)

    def on_close(w):
        cmd_close()
        return True          # hide, do not destroy

    win.connect("close-request", on_close)
    if S["panel_open"]:
        win.set_visible(True)
        W["mon_name"] = None
    GLib.timeout_add(1000, on_tick)


def cleanup():
    # Only remove the socket if this process is the daemon that owns it.
    try:
        owner = int(open(PIDF).read().strip())
    except Exception:
        owner = None
    if owner == os.getpid():
        for p in (SOCK, PIDF):
            try:
                os.unlink(p)
            except OSError:
                pass


def main():
    global PORT
    sys.stderr = open(LOG, "a", buffering=1)

    try:
        PORT = stats.start_server()
        _log("stats server on", PORT)
    except Exception as exc:
        _log("stats server failed:", exc)

    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", on_activate)

    load()
    try:
        os.unlink(PIDF)
    except OSError:
        pass
    with open(PIDF, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(cleanup)

    threading.Thread(target=_listen, daemon=True).start()
    app.run()


if __name__ == "__main__":
    sys.exit(main())

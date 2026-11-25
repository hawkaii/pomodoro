# pomodoro

A small Pomodoro timer for **Waybar** on **Hyprland**. The bar box shows the
countdown. Click it for a floating control panel. Finished focus blocks are
saved to a local SQLite database and shown on a stats page at `localhost`.

No Electron, no Node, no browser runtime for the timer itself.

```
idle     [ 󰌽 ]            the glyph from custom/distro
running  [ 19:58 ]        same red box, countdown replaces the glyph
```

## What it looks like

- **Bar**: one module, styled exactly like the `custom/distro` box it replaces
  (red `@accent` background, dark foreground, 20 px icon / 14 px countdown).
- **Panel**: 320 px wide, dark `#0d0d0d`, rounded 18 px, JetBrainsMono.
  The countdown sits inside a Cairo progress ring that drains as the block
  runs, like Pomodor's circle. Round dots under it show progress through the
  cycle. Red while focusing, ice blue on a break, grey when idle.
  Opens at the top-right of the focused monitor.
- **Stats**: dark page, four stat cards, a 14-day bar chart, a per-label
  breakdown and the 25 most recent sessions.

## Architecture

Two programs. They share a file and a socket, not code.

```
app.py            580 lines   GTK4 window + timer + unix socket + stats server
stats.py          330 lines   SQLite history + local HTTP page
waybar/pomodoro    91 lines   bar front-end (JSON in, commands out)
```

```
   click the red box
            │
            ▼
   waybar ── runs ──▶ waybar/pomodoro toggle
            │              │ spawns app.py if the socket is dead
            │              ▼
            │         unix socket ──▶ app.py   (timer + GTK panel)
            │                            │
            │                            ├── SQLite  ~/.local/share/pomodoro/history.db
            │                            ├── HTTP    127.0.0.1:<random port>  ← stats page
            │                            └── hyprctl (position, monitor geometry)
            ▼
   every 1 s: waybar/pomodoro status
            reads $XDG_RUNTIME_DIR/pomodoro/state.json
            prints {"text": "19:58", "class": ["pomodoro-focus"]}
```

The timer does not need Waybar. It does need Hyprland for window placement
(`hyprctl`); that is about 30 lines in `place_window()`.

## Install

Requires: Python 3.11+, PyGObject, GTK 4, Waybar, Hyprland.

```bash
git clone https://github.com/hawkaii/pomodoro ~/.config/pomodoro-mc
chmod +x ~/.config/pomodoro-mc/waybar/pomodoro

ln -sf ~/.config/pomodoro-mc/waybar/pomodoro  ~/.config/waybar/scripts/pomodoro
cp    ~/.config/pomodoro-mc/waybar/pomodoro.jsonc ~/.config/waybar/modules/custom/
```

Then add the module to `~/.config/waybar/config.jsonc` in `modules-center`,
where `custom/distro` used to sit:

```jsonc
"custom/distro",
"custom/pomodoro",
```

Append the CSS from `waybar/modules-center.css` to
`~/.config/waybar/styles/modules-center.css`, and the window rules from
`hypr/hyprland.conf` to `~/.config/hypr/hyprland.conf`. Reload both:

```bash
kill -SIGUSR2 -x waybar
hyprctl reload
```

## Use

| Action | Result |
|---|---|
| Left click the box | Opens the panel. **Does not start anything.** |
| `START` / `PAUSE` / `RESUME` | One primary button, changes with the state. |
| `DISMISS` | Appears while the alarm rings. Stops the sound. |
| `✕` | Hides the panel. A running timer keeps counting down. |
| Right click the box | Pause / resume without opening the panel |
| Middle click the box | Reset |
| `STATS` in the panel | Opens `http://127.0.0.1:<port>/` in your browser |

Defaults: focus 20 min, break 5, long break 20, long break after every 4
breaks. Every number has `[-]` / `[+]` in the panel. Values persist.

### Alarm

When a block ends the clock **stops** and an alarm loops until you dismiss it.
Nothing drains while it rings, so you never lose the start of the next block.

Dismiss in either place:

- the `DISMISS` button, which replaces the primary button while ringing
- the critical desktop notification, if your notification daemon reports the
  click back to `notify-send`

`AUTO` decides what happens after you dismiss: on, the next block starts by
itself; off, it waits for `START`.

The chime is synthesised once into `~/.local/share/pomodoro/alarm.wav` and
looped with `paplay`. No audio assets are shipped in this repo.

The `LABEL` field is a text box: type any name and press Enter. A new name gets
a colour automatically the first time it is used. Four defaults ship: `focus`,
`study`, `code`, `review`.

**The label freezes when a block starts.** If you rename it mid-block, that
applies to the next block — the running one still saves under the name it began
with. The `i` button in the panel explains this, on hover and on click.

Every panel action is also a socket command, so you can bind them to keys:

```bash
~/.config/pomodoro-mc/waybar/pomodoro toggle   # also: open close start pause
                                               # reset skip label stats primary
                                               # dismiss
```


## Session record

Inspired by [sasa95/pomodor](https://github.com/sasa95/pomodor) (MIT), where a
session is `{ duration, label, createdAt }` and stats are minutes and session
counts per day and per label. This project keeps that model and replaces
Firebase with a local SQLite file.

```sql
CREATE TABLE sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ended_at INTEGER NOT NULL,   -- unix seconds
    minutes  INTEGER NOT NULL,
    label    TEXT    NOT NULL
);
CREATE TABLE labels (name TEXT PRIMARY KEY, color TEXT NOT NULL);
```

A row is written only when a focus block **completes**. Skipping or resetting
records nothing.

## Stats

`stats.py` serves two routes on `127.0.0.1`, on an ephemeral port:

- `/` — server-rendered HTML. No JavaScript, no CDN, works offline.
- `/data.json` — the same numbers as JSON.

It listens on **http://127.0.0.1:8791/** — a fixed port, so you can bookmark it.
If 8791 is taken it falls back to any free port and records it in
`~/.local/share/pomodoro/port`. The page refreshes itself every 30 s.

A row appears only when a focus block **runs to zero**. Skipping or resetting
records nothing, so an empty page means no block has finished yet.

Metrics: today, this week (Monday start), this month, all time, day streak,
last 14 days, minutes per label, 25 most recent sessions.

*WHAT YOU WORKED ON* is scoped to **one day**, not all time. It opens on today
and has a chip per day for the last week (`/?day=2026-11-22`). A block counts
towards the day it **finished** on, so a block that crosses midnight lands on
the later day.

## What you worked on

While a focus block runs, the daemon samples the focused window every 5 s and
records the app and the tab title. Nothing is sampled on a break, when paused,
or while the alarm rings.

```sql
CREATE TABLE activity (
    sref    TEXT NOT NULL,   -- links a block to its samples
    app     TEXT NOT NULL,   -- window class
    title   TEXT NOT NULL,   -- cleaned title
    seconds INTEGER NOT NULL,
    PRIMARY KEY (sref, app, title)
);
```

`seconds` accumulates, so a 20-minute block writes a handful of rows, not 240.
Activity is joined to `sessions.sref`, so a block you reset or skipped never
reaches the page — and its samples are deleted on reset.

Titles are cleaned per app, because the window title is the only source that
works without a browser extension:

```
firefox   "Need advice : r/kolkata — Original profile — Mozilla Firefox"
          -> "Need advice : r/kolkata"
helium    "New Tab - Helium"                        -> "New Tab"
obsidian  "Drawing … - vault - Obsidian 1.15.1"     -> "Drawing …"
ghostty   "hawkaii-IdeaPad: projects"               -> "projects"
mpv       "camera-2026-11-22.mkv - mpv"             -> "camera-2026-11-22.mkv"
```

URLs are **not** recorded. The address bar is not observable without a browser
extension, so the page shows the tab title only.

Password managers (`keepassxc`, `bitwarden`, `1password`, `gnome-keyring`) store
the app class and nothing else.

## Notes

- One daemon only. `Gtk.Application` with a fixed app id enforces this through
  D-Bus; a second launch exits and forwards.
- The socket listener is non-blocking with a sleep, so it can never spin.
- The 1 s tick only writes state and redraws while a session is running.
  Idle cost is ~0%. Panel open costs ~0.3% of one core.
- The UI never talks to the socket. Buttons call functions in-process, so the
  panel cannot become unresponsive if the socket dies.

## Licence

MIT. The session and stats model is derived from
[sasa95/pomodor](https://github.com/sasa95/pomodor), also MIT.

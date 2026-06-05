# Palm Rejection — What Was Set Up & Changed

Machine: Lenovo Legion 5 · Ubuntu GNOME · **X11** · libinput driver
Touchpad: `SYNA2BA6:00 06CB:CEFE` (Synaptics, xinput id **18**, `/dev/input/event5`)
Date: 2026-06-05

---

## TL;DR

Built a custom **typing-guard daemon** that keeps tap-to-click ON (MacBook-style)
but suppresses *accidental* taps from palms resting in the left/right/top edge
zones and during typing. No permanent system settings were modified — the daemon
toggles a libinput property at runtime and restores it on exit.

---

## Key findings (why the default fails)

- Touchpad reports **no pressure and no contact-size** data. Only available axes:
  `ABS_MT_POSITION_X/Y`, `ABS_MT_SLOT`, `ABS_MT_TRACKING_ID`, `ABS_MT_TOOL_TYPE`,
  plus finger-count buttons (`BTN_TOOL_FINGER`…`QUINTTAP`).
  → macOS-style size/pressure palm detection is impossible on this hardware.
- Logged a real typing session (`session_20260605_111207.json`): of 24 contacts,
  **9 "dangerous" taps fired mis-clicks, ALL 9 in the left/right edge bands**:
  - Right palm → **top-right corner** (x ≈ 96–98% across, y ≈ 35–39% down)
  - Left palm → **left edge** (x ≈ 7–8% across, mid-height)
  - Center of the pad: no stray taps.
- **Every dangerous tap landed within 21–181 ms of a keystroke.**
- The firmware `MT_TOOL_PALM` flag catches the *long* palm rests, but the short
  (33–170 ms) edge taps come through as `FINGER` and become clicks.
- Discriminators available on this pad: **position + timing** (no size/pressure).

---

## Files created (all in `~/Desktop/palm_rejection/`)

| File | Purpose |
|------|---------|
| `palm_logger.py` | PyQt6 + evdev tool: type in a window while it logs every touchpad contact, then correlates with keystrokes/clicks and prints a spatial analysis + ASCII heat-map. Run a saved session offline: `./.venv/bin/python palm_logger.py session_*.json` |
| `palm_guard.py` | The fix. Runtime palm-rejection daemon (details below). |
| `changed.md` | This file. |
| `.venv/` | Python venv (`--system-site-packages`) with **evdev** installed (system PyQt6 reused). |
| `session_20260605_111032.json`, `session_20260605_111207.json` | Recorded test sessions. |
| `session_*.report.txt` | Saved analyses. |
| `gui.log`, `guard.log` | Runtime logs of the logger GUI / guard daemon. |

## Packages installed

- **No system packages** (sudo was unavailable / not used).
- `evdev` installed **only inside the local `.venv`** via
  `./.venv/bin/pip install evdev`. Nothing global touched.

---

## What `palm_guard.py` does

Keeps `tap-to-click` enabled but disables **tapping only** (pointer motion and
physical clicks still work) whenever a tap is almost certainly accidental:

- **Typing guard** — within `--window` ms (default **300 ms**) of any real
  keystroke (pure modifier keys ignored so Ctrl/Super chords don't stick).
- **Zone guard** — a contact resting in a palm zone keeps the guard hot:
  - RIGHT: `x > 0.85` and `y < 0.55`  (right palm / top-right)
  - LEFT:  `x < 0.13`                  (left palm / wrist)
  - TOP:   `y < 0.15`                  (thumb base near keyboard)

Mechanism: reads keyboard + touchpad evdev streams (needs `input` group, **no
sudo**) and toggles the X11 property `libinput Tapping Enabled` on the touchpad
(xinput id 18) **only on state transitions**. Center taps are never blocked.

### Run / stop
```bash
cd ~/Desktop/palm_rejection
./.venv/bin/python -u palm_guard.py --window 300 --verbose   # start (foreground)
pkill -INT -f palm_guard                                     # stop + restore
```
Tunables: `--window`, `--mode {tap|full}` (`full` also blocks pointer motion),
`--right-x --right-y --left-x --top-y`.

### Currently running
Started in the background during this session:
`./.venv/bin/python -u palm_guard.py --window 300 --verbose` (mode=tap).

---

## System settings changed: NONE (persistent)

- GNOME `org.gnome.desktop.peripherals.touchpad` settings: **unchanged**
  (`tap-to-click` still true, `disable-while-typing` still true, etc.).
- The daemon flips `libinput Tapping Enabled` at runtime and **restores the
  original value on clean exit** (Ctrl-C / `pkill -INT`). A hard kill (`-9`/
  `SIGTERM`) skips restore; if tapping is left off, reset with:
  ```bash
  xinput set-prop 18 "libinput Tapping Enabled" 1
  ```

## Installed as a systemd --user service (auto-start on boot/login)

A **user** service is used (not a system service) because the guard calls
`xinput` and needs the live X session (`DISPLAY`/`XAUTHORITY`). It starts when
you log into the graphical session and runs as your user (touchpad access via
the `input` group, no sudo).

Install layout:
| Path | What |
|------|------|
| `~/.local/share/palm-guard/palm_guard.py` | installed copy of the daemon |
| `~/.local/share/palm-guard/venv/` | dedicated venv with `evdev` |
| `~/.local/share/palm-guard/run.sh` | launcher; sets DISPLAY/XAUTHORITY fallback, waits for X, execs the guard |
| `~/.config/systemd/user/palm-guard.service` | the unit (`WantedBy=graphical-session.target`, `--window 300`) |

The unit handles `SIGTERM` (added to `palm_guard.py`) so `systemctl stop`
restores tapping cleanly; an `ExecStopPost` resets `Tapping Enabled` to 1 as a
belt-and-suspenders fallback.

### Manage the service
```bash
systemctl --user status   palm-guard.service     # check
systemctl --user restart  palm-guard.service     # apply edits
systemctl --user stop     palm-guard.service     # stop now (restores tapping)
systemctl --user disable  palm-guard.service     # don't start on boot
journalctl --user -u palm-guard.service -f       # live log
```
To change the window/zones: edit `ExecStart=` in
`~/.config/systemd/user/palm-guard.service`, then `daemon-reload` + `restart`.
To update the code: edit the Desktop copy, `cp -f` it to
`~/.local/share/palm-guard/`, then `restart`.

Status at install: **enabled** (auto-starts at login) and **active**.

## Not yet done (optional next steps)

- Tune `--window` / zone thresholds to taste after real-world use.
- If the touchpad's xinput id ever changes from 18, update the `ExecStopPost`
  line in the unit (the daemon itself resolves the id dynamically; only the
  fallback hardcodes 18).

# palm-guard

**MacBook-style palm rejection for Linux laptops whose touchpad reports no
pressure or contact size** (e.g. Lenovo Legion 5 / Synaptics `SYNA2BA6`).

If you mis-click while typing because your palm brushes the trackpad, this fixes
it — **without disabling tap-to-click**. It briefly suppresses *tapping only*
(pointer motion and physical clicks still work) when a tap is almost certainly
accidental, and never touches deliberate taps in the center of the pad.

---

## Why this exists

macOS palm rejection leans heavily on **contact size and pressure** — a palm is
a big, soft blob, easy to tell from a fingertip. Many cheaper Linux touchpads
(Synaptics I²C, among others) report **none of that**. On this hardware the only
signals available to `libinput` are position, finger count, and an occasional
firmware "palm" flag — so its size/pressure-based palm detection has nothing to
work with, and short edge taps slip through as clicks while you type.

`palm-guard` works with the two signals that *are* available on such pads:

| Signal | This hardware | Used by palm-guard |
|--------|---------------|--------------------|
| Contact size / pressure | ❌ not reported | — |
| Firmware palm flag (`MT_TOOL_TYPE`) | ⚠️ only for long rests | (libinput already uses it) |
| **Timing vs. keystrokes** | ✅ | ✅ primary |
| **Position (edge/corner zones)** | ✅ | ✅ primary |
| Movement / duration | ✅ | (analysis tool) |

In a real recorded typing session on the reference machine, **every** stray tap
that became a mis-click was either within ~180 ms of a keystroke **or** sat in
the left/right/top edge zone — and nothing strayed in the center. palm-guard
targets exactly those.

```
  +----------------------------+   <- top edge (near keyboard)
  |                          D |    D = stray touch while typing
  |                          DX|    <- right palm: top-right corner
  |  DD                        |    <- left palm: left edge
  +----------------------------+
            (pad seen from above; center stays clear)
```

## How it works

A small daemon reads the keyboard and touchpad event streams (`evdev`, no root —
just the `input` group) and toggles the X11 `libinput Tapping Enabled` property
**only on transitions**. Tapping is suppressed while either guard is "hot":

- **Typing guard** — within `--window` ms (default **300**) of a real keystroke.
  Pure modifier keys are ignored so Ctrl/Super shortcuts don't keep it disabled.
- **Zone guard** — a contact resting in a palm zone keeps the guard refreshed for
  as long as the palm is down:
  - `RIGHT`: `x > 0.85` and `y < 0.55` (right palm / top-right corner)
  - `LEFT` : `x < 0.13` (left palm / wrist)
  - `TOP`  : `y < 0.15` (thumb base near the keyboard)

Center taps are never blocked, so deliberate clicks feel normal.

## Requirements

- **X11** session (uses `xinput`; **Wayland is not supported**).
- `libinput` touchpad driver (`xserver-xorg-input-libinput`).
- `xinput`, `python3`, and membership in the **`input`** group.

```bash
# one-time prerequisites, if missing:
sudo apt install xinput python3-venv
sudo usermod -aG input "$USER"   # then log out and back in
```

## Install

```bash
git clone https://github.com/USERNAME/palm-guard.git
cd palm-guard
./install.sh
```

The installer (no sudo) copies files to `~/.local/share/palm-guard/`, creates a
venv with `evdev`, installs a **systemd `--user`** service, and enables it so it
auto-starts every time you log into the graphical session. It's idempotent —
re-run it any time to update.

### Uninstall

```bash
./uninstall.sh
```

## Tuning

Edit `ExecStart=` in `~/.config/systemd/user/palm-guard.service`, then:

```bash
systemctl --user daemon-reload && systemctl --user restart palm-guard.service
```

| Flag | Default | Effect |
|------|---------|--------|
| `--window <ms>` | `300` | Suppression window after a keystroke / palm contact. Raise if mis-clicks still slip through; lower if center taps feel laggy right after typing. |
| `--mode tap\|full` | `tap` | `tap` blocks taps only; `full` also freezes pointer motion (no cursor drift — most MacBook-like). |
| `--right-x --right-y` | `0.85 0.55` | Right/top-right palm zone (normalized 0–1). |
| `--left-x` | `0.13` | Left-edge palm zone. |
| `--top-y` | `0.15` | Top-strip palm zone. |

The zone defaults are tuned for a right-handed user on a 117×72 mm pad — adjust
to your hands using the profiler below.

## Profile your own touchpad

`palm_logger.py` is a PyQt6 tool that records exactly where *your* palm lands so
you can pick zones that fit you:

```bash
python3 -m venv --system-site-packages .venv
./.venv/bin/pip install -r requirements.txt   # evdev (PyQt6 comes from system)
./.venv/bin/python palm_logger.py
```

Click **Start**, type naturally for ~30–60 s (reproduce your mis-clicks), then
**Stop → Save + Analyze**. Recordings are written to `dev-tmp/` (git-ignored) as
`session_*.json` plus a report with an ASCII heat-map, a 9-zone breakdown, and
the timing of every stray tap. Re-analyze a saved session offline:

```bash
./.venv/bin/python palm_logger.py dev-tmp/session_YYYYMMDD_HHMMSS.json
```

Requires `python3-pyqt6` (`sudo apt install python3-pyqt6`) for the GUI.

## Manage the service

```bash
systemctl --user status   palm-guard.service     # is it running?
systemctl --user restart  palm-guard.service     # apply config changes
systemctl --user stop     palm-guard.service     # stop now (restores tapping)
systemctl --user disable  palm-guard.service     # don't auto-start on boot
journalctl     --user -u  palm-guard.service -f  # live log
```

If the service is force-killed and tap-to-click is left disabled, restore it
with `~/.local/share/palm-guard/restore-tapping.sh`.

## Repository layout

```
palm-guard/
├── palm_guard.py              # the daemon
├── palm_logger.py             # PyQt6 profiler / analysis tool
├── install.sh  uninstall.sh   # systemd --user installer
├── requirements.txt           # evdev
├── systemd/palm-guard.service # unit template (paths via %h)
└── scripts/
    ├── run.sh                 # launcher (sets DISPLAY, waits for X)
    └── restore-tapping.sh     # re-enable tap-to-click (id resolved dynamically)
```

## Limitations

- **X11 only.** Wayland exposes no equivalent of `xinput set-prop`; a Wayland
  port would need a libinput-level filter or a compositor plugin.
- Touchpad must report multitouch position (`ABS_MT_POSITION_X/Y`) — virtually
  all do.
- Default zones assume a right-handed grip; profile and adjust if needed.

## License

MIT — see [LICENSE](LICENSE).

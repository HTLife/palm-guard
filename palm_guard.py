#!/usr/bin/env python3
"""
palm_guard.py — MacBook-style palm rejection for libinput touchpads that
report no pressure/size (e.g. Lenovo Legion 5 / Synaptics SYNA2BA6).

Keeps tap-to-click ENABLED, but briefly suppresses *tapping only* (pointer
motion and physical clicks still work) whenever a tap would almost certainly
be accidental:

  • TYPING GUARD : within --window ms of a keystroke.
  • ZONE GUARD   : a contact is resting in a left/right/top palm zone.

Both feed one state: tapping is disabled while either guard is "hot", and the
zone contact keeps the guard refreshed for as long as the palm stays down.
Center taps are never blocked, so deliberate clicks feel normal.

Implementation: reads the keyboard + touchpad evdev streams (needs `input`
group, no sudo) and toggles the X11 `libinput Tapping Enabled` property via
xinput only on state transitions. Restores your original setting on exit.

Usage:
  ./.venv/bin/python palm_guard.py                  # sensible defaults
  ./.venv/bin/python palm_guard.py --window 300 --verbose
  ./.venv/bin/python palm_guard.py --mode full      # also block pointer motion
Stop with Ctrl-C.
"""

import argparse
import re
import signal
import subprocess
import sys
import time
import selectors

from evdev import InputDevice, ecodes, list_devices


# --------------------------------------------------------------------------
# device discovery
# --------------------------------------------------------------------------
def find_touchpad():
    cands = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except Exception:
            continue
        caps = d.capabilities()
        abs_axes = [c for c, _ in caps.get(ecodes.EV_ABS, [])]
        keys = caps.get(ecodes.EV_KEY, [])
        if (ecodes.ABS_MT_SLOT in abs_axes and ecodes.BTN_TOOL_FINGER in keys
                and ecodes.BTN_TOUCH in keys):
            cands.append(d)
    for d in cands:
        if "ouchpad" in d.name:
            return d
    return cands[0] if cands else None


def find_keyboards(exclude_path):
    """Any device that has letter keys and isn't the touchpad."""
    kbs = []
    for path in list_devices():
        if path == exclude_path:
            continue
        try:
            d = InputDevice(path)
        except Exception:
            continue
        keys = d.capabilities().get(ecodes.EV_KEY, [])
        if ecodes.KEY_A in keys and ecodes.KEY_Z in keys:
            kbs.append(d)
    return kbs


# --------------------------------------------------------------------------
# xinput property control
# --------------------------------------------------------------------------
def xinput_touchpad_id():
    out = subprocess.run(["xinput", "list", "--name-only"],
                         capture_output=True, text=True).stdout.splitlines()
    for name in out:
        if "ouchpad" in name:
            r = subprocess.run(["xinput", "list", "--id-only", name],
                               capture_output=True, text=True)
            return r.stdout.strip()
    return None


def get_prop(dev_id, prop):
    r = subprocess.run(["xinput", "list-props", dev_id],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if prop in line and "Default" not in line:
            m = re.search(r":\s*([0-9, ]+)$", line.strip())
            if m:
                return m.group(1).strip()
    return None


def set_prop(dev_id, prop, value):
    subprocess.run(["xinput", "set-prop", dev_id, prop] + value.split(", "),
                   capture_output=True, text=True)


# --------------------------------------------------------------------------
# main guard loop
# --------------------------------------------------------------------------
class Guard:
    PROP = "libinput Tapping Enabled"
    PROP_SEND = "libinput Send Events Mode Enabled"

    def __init__(self, args):
        self.args = args
        self.tp = find_touchpad()
        if self.tp is None:
            sys.exit("No touchpad found (are you in the `input` group?).")
        self.kbs = find_keyboards(self.tp.path)
        self.xid = xinput_touchpad_id()
        if self.xid is None:
            sys.exit("Could not find touchpad via xinput (X11 only).")

        # touchpad geometry for zone math
        absinfo = dict(self.tp.capabilities().get(ecodes.EV_ABS, []))
        self.MX = absinfo[ecodes.ABS_MT_POSITION_X].max or 1
        self.MY = absinfo[ecodes.ABS_MT_POSITION_Y].max or 1

        self.window = args.window / 1000.0
        self.hot_until = 0.0          # guard hot until this monotonic time
        self.tapping_on = True        # our belief of current state
        self.slots = {}               # slot -> (x, y)
        self.cur_slot = 0
        self.zone_active = False

        # remember original states to restore on exit
        self.orig_tap = get_prop(self.xid, self.PROP) or "1"
        self.orig_send = get_prop(self.xid, self.PROP_SEND) or "1, 0"

    # ---- zone test ----
    def in_palm_zone(self, x, y):
        nx, ny = x / self.MX, y / self.MY
        a = self.args
        if nx > a.right_x and ny < a.right_y:      # right palm / top-right
            return "RIGHT"
        if nx < a.left_x:                          # left palm / wrist
            return "LEFT"
        if ny < a.top_y:                           # thumb base near keys
            return "TOP"
        return None

    # ---- apply state ----
    def set_tapping(self, on):
        if on == self.tapping_on:
            return
        self.tapping_on = on
        if self.args.mode == "full":
            # disable the whole pad (motion+tap) while hot — most MacBook-like
            val = "0, 0" if on else "1, 0"
            set_prop(self.xid, self.PROP_SEND, val)
        else:
            set_prop(self.xid, self.PROP, "1" if on else "0")
        if self.args.verbose:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] tapping {'ON ' if on else 'OFF'} "
                  f"{'(zone)' if self.zone_active else '(typing)'}", flush=True)

    def refresh(self, reason):
        self.hot_until = time.monotonic() + self.window
        if self.args.verbose and reason == "key":
            pass  # too noisy to log every key

    # ---- event handling ----
    def on_touch_event(self, ev):
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_MT_SLOT:
                self.cur_slot = ev.value
            elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                if ev.value == -1:
                    self.slots.pop(self.cur_slot, None)
                else:
                    self.slots[self.cur_slot] = [None, None]
            elif ev.code == ecodes.ABS_MT_POSITION_X:
                s = self.slots.get(self.cur_slot)
                if s:
                    s[0] = ev.value
            elif ev.code == ecodes.ABS_MT_POSITION_Y:
                s = self.slots.get(self.cur_slot)
                if s:
                    s[1] = ev.value
        elif ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_REPORT:
            # after a frame, check whether any contact sits in a palm zone
            zone = None
            for x, y in self.slots.values():
                if x is None or y is None:
                    continue
                z = self.in_palm_zone(x, y)
                if z:
                    zone = z
                    break
            self.zone_active = zone is not None
            if self.zone_active:
                self.refresh("zone")

    def run(self):
        # systemd sends SIGTERM on stop -> turn it into a clean shutdown so
        # restore() runs and tapping is re-enabled.
        def _term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)
        print(f"palm_guard: touchpad='{self.tp.name}' xid={self.xid} "
              f"keyboards={len(self.kbs)} window={self.args.window}ms "
              f"mode={self.args.mode}")
        print("Zones (normalized): "
              f"RIGHT x>{self.args.right_x} & y<{self.args.right_y}, "
              f"LEFT x<{self.args.left_x}, TOP y<{self.args.top_y}")
        print("Running. Press Ctrl-C to stop and restore settings.")

        sel = selectors.DefaultSelector()
        sel.register(self.tp.fd, selectors.EVENT_READ, "tp")
        for kb in self.kbs:
            sel.register(kb.fd, selectors.EVENT_READ, kb)

        fd_to_dev = {self.tp.fd: self.tp}
        for kb in self.kbs:
            fd_to_dev[kb.fd] = kb

        try:
            while True:
                for key, _ in sel.select(timeout=0.05):
                    dev = fd_to_dev[key.fd]
                    try:
                        for ev in dev.read():
                            if dev is self.tp:
                                self.on_touch_event(ev)
                            elif (ev.type == ecodes.EV_KEY and ev.value == 1
                                  and self._is_typing_key(ev.code)):
                                self.refresh("key")
                    except OSError:
                        pass
                # reconcile desired state
                hot = time.monotonic() < self.hot_until
                self.set_tapping(not hot)
        except KeyboardInterrupt:
            pass
        finally:
            self.restore()

    @staticmethod
    def _is_typing_key(code):
        # block-worthy keys = real typing; ignore pure modifiers so chord
        # shortcuts (Ctrl, Super) don't keep the pad disabled forever
        mods = {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL,
                ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT,
                ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
                ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
                ecodes.KEY_CAPSLOCK}
        return code not in mods

    def restore(self):
        print("\nRestoring touchpad settings...")
        set_prop(self.xid, self.PROP, self.orig_tap)
        set_prop(self.xid, self.PROP_SEND, self.orig_send)
        print("Done.")


def main():
    p = argparse.ArgumentParser(description="MacBook-style palm guard")
    p.add_argument("--window", type=int, default=300,
                   help="ms to suppress taps after a keystroke / palm contact")
    p.add_argument("--mode", choices=["tap", "full"], default="tap",
                   help="tap=block taps only (default); full=block all pad input")
    p.add_argument("--right-x", type=float, default=0.85)
    p.add_argument("--right-y", type=float, default=0.55)
    p.add_argument("--left-x", type=float, default=0.13)
    p.add_argument("--top-y", type=float, default=0.15)
    p.add_argument("--verbose", action="store_true")
    Guard(p.parse_args()).run()


if __name__ == "__main__":
    main()

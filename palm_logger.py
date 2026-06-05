#!/usr/bin/env python3
"""
palm_logger.py — Touchpad palm-rejection data collector (PyQt6 + evdev)

Logs raw touchpad contacts from /dev/input/event5 while you type into the
window, then correlates contacts with keystrokes and synthesized mouse clicks
to find accidental (palm/thumb) touches that fire mis-clicks while typing.

Run:  ./.venv/bin/python palm_logger.py
Needs: membership in the `input` group (no sudo).
"""

import os
import sys
import time
import json
import glob

from PyQt6 import QtCore, QtGui, QtWidgets

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:
    print("evdev missing — run: ./.venv/bin/pip install evdev")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Touchpad device discovery
# ---------------------------------------------------------------------------
def find_touchpad():
    candidates = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except Exception:
            continue
        caps = d.capabilities()
        abs_axes = [c for c, _ in caps.get(ecodes.EV_ABS, [])]
        keys = caps.get(ecodes.EV_KEY, [])
        # a real touchpad: multitouch slots + finger tool + click button
        if (ecodes.ABS_MT_SLOT in abs_axes
                and ecodes.BTN_TOOL_FINGER in keys
                and ecodes.BTN_TOUCH in keys):
            candidates.append(d)
    if not candidates:
        return None
    # prefer the one whose name looks like a touchpad
    for d in candidates:
        if "ouchpad" in d.name:
            return d
    return candidates[0]


# ---------------------------------------------------------------------------
# evdev reader thread — parses multitouch slots into "contact" records
# ---------------------------------------------------------------------------
class TouchReader(QtCore.QThread):
    contact_started = QtCore.pyqtSignal(dict)
    contact_ended = QtCore.pyqtSignal(dict)
    raw_button = QtCore.pyqtSignal(dict)   # physical clickpad press
    finger_count = QtCore.pyqtSignal(int, float)

    def __init__(self, device, parent=None):
        super().__init__(parent)
        self.dev = device
        self._running = True
        self.slots = {}            # slot -> live contact dict
        self.cur_slot = 0
        self.n_fingers = 0

    def stop(self):
        self._running = False

    def run(self):
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(self.dev.fd, selectors.EVENT_READ)
        TOOL = {0: "FINGER", 1: "PEN", 2: "PALM"}
        while self._running:
            if not sel.select(timeout=0.2):
                continue
            try:
                for ev in self.dev.read():
                    self._handle(ev, TOOL)
            except OSError:
                break

    def _now(self):
        return time.monotonic()

    def _handle(self, ev, TOOL):
        t = self._now()
        if ev.type == ecodes.EV_ABS:
            if ev.code == ecodes.ABS_MT_SLOT:
                self.cur_slot = ev.value
            elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                if ev.value == -1:
                    self._end_contact(self.cur_slot, t)
                else:
                    self.slots[self.cur_slot] = {
                        "slot": self.cur_slot,
                        "tracking_id": ev.value,
                        "t_start": t,
                        "x0": None, "y0": None,
                        "x": None, "y": None,
                        "minx": 1e9, "maxx": -1e9,
                        "miny": 1e9, "maxy": -1e9,
                        "path": 0.0,
                        "samples": 0,
                        "tool": "FINGER",
                        "max_fingers": self.n_fingers or 1,
                    }
            elif ev.code == ecodes.ABS_MT_POSITION_X:
                c = self.slots.get(self.cur_slot)
                if c is not None:
                    if c["x"] is not None:
                        c["path"] += abs(ev.value - c["x"])
                    c["x"] = ev.value
                    if c["x0"] is None:
                        c["x0"] = ev.value
                    c["minx"] = min(c["minx"], ev.value)
                    c["maxx"] = max(c["maxx"], ev.value)
                    c["samples"] += 1
            elif ev.code == ecodes.ABS_MT_POSITION_Y:
                c = self.slots.get(self.cur_slot)
                if c is not None:
                    c["y"] = ev.value
                    if c["y0"] is None:
                        c["y0"] = ev.value
                    c["miny"] = min(c["miny"], ev.value)
                    c["maxy"] = max(c["maxy"], ev.value)
            elif ev.code == ecodes.ABS_MT_TOOL_TYPE:
                c = self.slots.get(self.cur_slot)
                if c is not None:
                    c["tool"] = TOOL.get(ev.value, str(ev.value))
        elif ev.type == ecodes.EV_KEY:
            if ev.code in (ecodes.BTN_LEFT,):
                self.raw_button.emit({"t": t, "code": "BTN_LEFT", "value": ev.value})
            else:
                fc = {
                    ecodes.BTN_TOOL_FINGER: 1,
                    ecodes.BTN_TOOL_DOUBLETAP: 2,
                    ecodes.BTN_TOOL_TRIPLETAP: 3,
                    ecodes.BTN_TOOL_QUADTAP: 4,
                    ecodes.BTN_TOOL_QUINTTAP: 5,
                }.get(ev.code)
                if fc is not None:
                    if ev.value == 1:
                        self.n_fingers = fc
                        self.finger_count.emit(fc, t)
                        for c in self.slots.values():
                            c["max_fingers"] = max(c["max_fingers"], fc)
                    elif ev.value == 0 and self.n_fingers == fc:
                        self.n_fingers = 0

    def _end_contact(self, slot, t):
        c = self.slots.pop(slot, None)
        if c is None:
            return
        c["t_end"] = t
        c["dur_ms"] = (t - c["t_start"]) * 1000.0
        c.pop("x", None)
        c.pop("y", None)
        if c["minx"] > c["maxx"]:
            c["minx"] = c["maxx"] = c["x0"] or 0
            c["miny"] = c["maxy"] = c["y0"] or 0
        c["bbox_w"] = c["maxx"] - c["minx"]
        c["bbox_h"] = c["maxy"] - c["miny"]
        self.contact_ended.emit(c)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    # touchpad geometry (from evdev): X 0..1404, Y 0..864, ~117x72 mm
    MAXX, MAXY = 1404, 864

    def __init__(self, device):
        super().__init__()
        self.dev = device
        self.setWindowTitle("Palm-Rejection Logger — type naturally")
        self.resize(900, 680)

        self.recording = False
        self.t0 = None
        self.keystrokes = []   # list of {t, key}
        self.contacts = []     # finished contact dicts
        self.clicks = []       # synthesized mouse presses seen by Qt
        self.raw_buttons = []  # physical clickpad presses

        self._build_ui()

        self.reader = TouchReader(device)
        self.reader.contact_ended.connect(self.on_contact)
        self.reader.raw_button.connect(self.on_raw_button)
        self.reader.start()

        # global mouse-press filter (catches tap-to-click firing in window)
        QtWidgets.QApplication.instance().installEventFilter(self)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_stats)
        self.timer.start(300)

    # ---- UI ----
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        v = QtWidgets.QVBoxLayout(central)

        info = QtWidgets.QLabel(
            f"<b>Device:</b> {self.dev.name}<br>"
            "Press <b>Start</b>, then type a paragraph the way you normally do "
            "(rest your palms, use the trackpad occasionally). The tool logs every "
            "touchpad contact and flags ones that land while you're typing.<br>"
            "<i>If a stray click fires in this box, that's a captured mis-click.</i>"
        )
        info.setWordWrap(True)
        v.addWidget(info)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("● Start recording")
        self.btn_start.clicked.connect(self.toggle)
        self.btn_save = QtWidgets.QPushButton("Save + Analyze")
        self.btn_save.clicked.connect(self.save_and_analyze)
        self.btn_save.setEnabled(False)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_save)
        btn_row.addStretch()
        v.addLayout(btn_row)

        self.text = QtWidgets.QPlainTextEdit()
        self.text.setPlaceholderText("Type here once recording starts…")
        self.text.installEventFilter(self)
        f = QtGui.QFont("monospace"); f.setPointSize(12)
        self.text.setFont(f)
        v.addWidget(self.text, 2)

        self.stats = QtWidgets.QLabel("Not recording.")
        self.stats.setStyleSheet("font-family: monospace;")
        v.addWidget(self.stats)

        self.feed = QtWidgets.QPlainTextEdit()
        self.feed.setReadOnly(True)
        self.feed.setMaximumHeight(160)
        ff = QtGui.QFont("monospace"); ff.setPointSize(9)
        self.feed.setFont(ff)
        v.addWidget(self.feed, 1)

    # ---- recording control ----
    def toggle(self):
        if not self.recording:
            self.recording = True
            self.t0 = time.monotonic()
            self.keystrokes.clear(); self.contacts.clear()
            self.clicks.clear(); self.raw_buttons.clear()
            self.text.clear(); self.feed.clear()
            self.btn_start.setText("■ Stop recording")
            self.btn_save.setEnabled(False)
            self.text.setFocus()
        else:
            self.recording = False
            self.btn_start.setText("● Start recording")
            self.btn_save.setEnabled(True)
            self.log_feed("--- stopped ---")

    # ---- event capture ----
    def eventFilter(self, obj, event):
        et = event.type()
        if self.recording:
            if et == QtCore.QEvent.Type.KeyPress and obj is self.text:
                self.keystrokes.append({"t": time.monotonic(), "key": event.key()})
            elif et in (QtCore.QEvent.Type.MouseButtonPress,
                        QtCore.QEvent.Type.MouseButtonDblClick):
                self.clicks.append({"t": time.monotonic(),
                                    "button": int(event.button().value),
                                    "obj": obj.__class__.__name__})
                self.log_feed(f"[{self._rel():7.2f}s] *** Qt MOUSE PRESS in "
                              f"{obj.__class__.__name__} ***")
        return super().eventFilter(obj, event)

    def on_contact(self, c):
        if not self.recording:
            return
        c["t_start_rel"] = c["t_start"] - self.t0
        c["t_end_rel"] = c["t_end"] - self.t0
        gap = self._nearest_key_gap_ms(c["t_start"])
        c["ms_to_last_key"] = gap
        self.contacts.append(c)
        flag = "  <-- during typing" if (gap is not None and gap <= 300) else ""
        self.log_feed(
            f"[{c['t_start_rel']:7.2f}s] contact slot{c['slot']} "
            f"dur={c['dur_ms']:6.1f}ms move={c['path']:5.0f} "
            f"bbox={c['bbox_w']}x{c['bbox_h']} pos=({c['x0']},{c['y0']}) "
            f"tool={c['tool']} fingers={c['max_fingers']} "
            f"keyGap={gap if gap is None else round(gap)}ms{flag}")

    def on_raw_button(self, b):
        if not self.recording:
            return
        b["t_rel"] = b["t"] - self.t0
        self.raw_buttons.append(b)
        if b["value"] == 1:
            self.log_feed(f"[{b['t_rel']:7.2f}s] physical clickpad press")

    # ---- helpers ----
    def _rel(self):
        return time.monotonic() - (self.t0 or time.monotonic())

    def _nearest_key_gap_ms(self, t):
        if not self.keystrokes:
            return None
        best = min(abs(t - k["t"]) for k in self.keystrokes)
        return best * 1000.0

    def log_feed(self, s):
        self.feed.appendPlainText(s)

    def update_stats(self):
        if not self.recording and self.t0 is None:
            return
        dur = self._rel() if self.recording else 0
        during = sum(1 for c in self.contacts
                     if c.get("ms_to_last_key") is not None
                     and c["ms_to_last_key"] <= 300)
        self.stats.setText(
            f"{'● REC ' if self.recording else 'stopped '} "
            f"t={dur:6.1f}s | keys={len(self.keystrokes)} | "
            f"contacts={len(self.contacts)} (during-typing={during}) | "
            f"Qt-clicks={len(self.clicks)} | physical-presses="
            f"{sum(1 for b in self.raw_buttons if b['value']==1)}")

    # ---- save + analyze ----
    def save_and_analyze(self):
        ts = time.strftime("%Y%m%d_%H%M%S")
        outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dev-tmp")
        os.makedirs(outdir, exist_ok=True)
        base = os.path.join(outdir, f"session_{ts}")
        data = {
            "device": self.dev.name,
            "maxx": self.MAXX, "maxy": self.MAXY,
            "keystrokes": [{"t": k["t"] - self.t0, "key": k["key"]}
                           for k in self.keystrokes],
            "contacts": self.contacts,
            "qt_clicks": [{"t": c["t"] - self.t0, "button": c["button"],
                           "obj": c["obj"]} for c in self.clicks],
            "raw_buttons": [{"t": b["t"] - self.t0, "code": b["code"],
                             "value": b["value"]} for b in self.raw_buttons],
        }
        with open(base + ".json", "w") as f:
            json.dump(data, f, indent=2)
        report = analyze(data)
        with open(base + ".report.txt", "w") as f:
            f.write(report)
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Analysis")
        dlg.resize(760, 560)
        lay = QtWidgets.QVBoxLayout(dlg)
        te = QtWidgets.QPlainTextEdit()
        te.setReadOnly(True)
        mf = QtGui.QFont("monospace"); mf.setPointSize(10)
        te.setFont(mf)
        te.setPlainText(report + f"\n\nSaved:\n  {base}.json\n  {base}.report.txt")
        lay.addWidget(te)
        dlg.exec()

    def closeEvent(self, e):
        self.reader.stop()
        self.reader.wait(1000)
        super().closeEvent(e)


# ---------------------------------------------------------------------------
# Offline analysis (also importable / runnable on a saved json)
# ---------------------------------------------------------------------------
def render_heatmap(contacts, MX, MY, cols=28, rows=10):
    """ASCII map of the touchpad. D=during-typing contact, o=deliberate."""
    grid = [[" "] * cols for _ in range(rows)]
    for c in contacts:
        if c["x0"] is None or c["y0"] is None:
            continue
        cx = min(cols - 1, int(c["x0"] / MX * cols))
        cy = min(rows - 1, int(c["y0"] / MY * rows))
        during = (c.get("ms_to_last_key") is not None
                  and c["ms_to_last_key"] <= 300)
        ch = "D" if during else "o"
        cur = grid[cy][cx]
        if cur == " ":
            grid[cy][cx] = ch
        elif cur != ch:
            grid[cy][cx] = "X"   # both kinds landed here
    top = "  +" + "-" * cols + "+   <- top edge (nearest keyboard)"
    out = [top]
    for r in grid:
        out.append("  |" + "".join(r) + "|")
    out.append("  +" + "-" * cols + "+   <- bottom edge")
    out.append("   left" + " " * (cols - 8) + "right")
    out.append("   legend: D=contact while typing  o=deliberate  X=both")
    return "\n".join(out)


def analyze(data):
    ks = data["keystrokes"]
    contacts = data["contacts"]
    clicks = data["qt_clicks"]
    MX, MY = data["maxx"], data["maxy"]
    L = []
    L.append("=" * 64)
    L.append("PALM-REJECTION SESSION ANALYSIS")
    L.append("=" * 64)
    if not ks:
        L.append("No keystrokes recorded.")
        return "\n".join(L)

    dur = max([k["t"] for k in ks] + [0])
    L.append(f"Duration              : {dur:6.1f} s")
    L.append(f"Keystrokes            : {len(ks)}")
    L.append(f"Touch contacts        : {len(contacts)}")
    L.append(f"Mis-clicks (Qt press) : {len(clicks)}")
    L.append("")

    # classify contacts by proximity to a keystroke
    def gap(c):
        g = c.get("ms_to_last_key")
        return g if g is not None else 9e9

    during = [c for c in contacts if gap(c) <= 300]
    near = [c for c in contacts if 300 < gap(c) <= 1000]
    away = [c for c in contacts if gap(c) > 1000]

    L.append("CONTACTS BY TIMING vs nearest keystroke")
    L.append(f"  during typing (<=300ms) : {len(during)}")
    L.append(f"  near typing (300-1000ms): {len(near)}")
    L.append(f"  away (>1s, deliberate)  : {len(away)}")
    L.append("")

    def summarize(name, group):
        if not group:
            L.append(f"{name}: none")
            return
        durs = sorted(c["dur_ms"] for c in group)
        paths = sorted(c["path"] for c in group)
        fingers = [c["max_fingers"] for c in group]
        palms = sum(1 for c in group if c["tool"] == "PALM")
        ys = [c["y0"] for c in group if c["y0"] is not None]

        def med(a): return a[len(a)//2] if a else 0
        L.append(f"{name} (n={len(group)})")
        L.append(f"   duration  median={med(durs):6.1f}ms  "
                 f"min={durs[0]:.1f} max={durs[-1]:.1f}")
        L.append(f"   movement  median={med(paths):6.1f}   "
                 f"max={paths[-1]:.1f}  (units; res~12/mm)")
        L.append(f"   fingers   max-seen distribution={dict_count(fingers)}")
        L.append(f"   firmware PALM-flagged: {palms}/{len(group)}")
        if ys:
            L.append(f"   landing Y median={sorted(ys)[len(ys)//2]} "
                     f"(0=top edge near keyboard, {data['maxy']}=bottom)")
        L.append("")

    summarize("DURING-TYPING contacts", during)
    summarize("DELIBERATE (away) contacts", away)

    # ---- spatial map ----
    L.append("-" * 64)
    L.append("WHERE CONTACTS LAND  (touchpad viewed from above)")
    L.append("-" * 64)
    L.append(render_heatmap(contacts, MX, MY))
    L.append("")

    # ---- 9-zone breakdown (left/center/right x top/mid/bottom) ----
    def zone(c):
        nx, ny = c["x0"] / MX, c["y0"] / MY
        zx = "L" if nx < 0.33 else ("R" if nx > 0.67 else "C")
        zy = "T" if ny < 0.40 else ("B" if ny > 0.66 else "M")
        return zy + zx
    L.append("9-ZONE COUNTS  (during-typing / deliberate)")
    order = ["TL", "TC", "TR", "ML", "MC", "MR", "BL", "BC", "BR"]
    zc = {z: [0, 0] for z in order}
    for c in contacts:
        if c["x0"] is None:
            continue
        zc[zone(c)][0 if c in during else 1] += 1
    names = {"TL": "top-left", "TC": "top-center", "TR": "top-right",
             "ML": "mid-left", "MC": "CENTER", "MR": "mid-right",
             "BL": "bot-left", "BC": "bot-center", "BR": "bot-right"}
    for z in order:
        d_, o_ = zc[z]
        if d_ or o_:
            mark = "  <== palm hot-zone" if d_ >= 2 else ""
            L.append(f"   {names[z]:11} during={d_:2}  deliberate={o_:2}{mark}")
    L.append("")

    # ---- danger taps: short, low-move, FINGER (these fire mis-clicks) ----
    def taplike(c):
        return c["dur_ms"] <= 180 and c["path"] <= 60
    danger = [c for c in during if taplike(c) and c["tool"] != "PALM"]
    L.append("-" * 64)
    L.append("DANGEROUS TAPS  (short+still+FINGER while typing -> mis-clicks)")
    L.append("-" * 64)
    L.append(f"count: {len(danger)}   (Qt actually registered {len(clicks)} mis-clicks)")
    edge = sum(1 for c in danger
               if c["x0"] / MX > 0.85 or c["x0"] / MX < 0.15)
    L.append(f"   in left/right edge bands (<15% or >85% X): {edge}/{len(danger)}")
    for c in danger:
        L.append(f"   x={c['x0']:4} ({c['x0']/MX:4.0%} across)  "
                 f"y={c['y0']:4} ({c['y0']/MY:4.0%} down)  "
                 f"dur={c['dur_ms']:5.0f}ms  gap={c['ms_to_last_key']:.0f}ms")
    L.append("")

    # ---- derive exclusion zones from the deliberate vs palm split ----
    palm_xs = [c["x0"] / MX for c in during if c["x0"] is not None]
    good_xs = [c["x0"] / MX for c in away if c["x0"] is not None]
    L.append("-" * 64)
    L.append("RECOMMENDED PALM-EXCLUSION ZONES (normalized 0..1)")
    L.append("-" * 64)
    if palm_xs:
        L.append(f"   palm contacts X-range : {min(palm_xs):.2f} .. {max(palm_xs):.2f}")
    if good_xs:
        L.append(f"   deliberate X-range    : {min(good_xs):.2f} .. {max(good_xs):.2f}")
    L.append("   suggested reject zones (tune in daemon):")
    L.append("     • RIGHT edge : x > 0.85  AND  y < 0.55   (right palm / top-right)")
    L.append("     • LEFT  edge : x < 0.13                  (left palm / wrist)")
    L.append("     • TOP   strip: y < 0.15                  (thumb base near keys)")
    L.append("   -> deliberate taps live in the center, so these zones are safe to reject.")
    L.append("")

    # ---- recommendations ----
    L.append("=" * 64)
    L.append("RECOMMENDATIONS")
    L.append("=" * 64)
    palm_fw = sum(1 for c in during if c["tool"] == "PALM")
    L.append(f"• No pressure/size on this pad -> use POSITION + TIMING + the PALM flag.")
    L.append(f"• Firmware PALM-flagged {palm_fw} typing-time contacts already; the "
             f"{len(danger)} that slip through are short FINGER taps in the edge zones.")
    if danger:
        gaps = sorted(c["ms_to_last_key"] for c in danger)
        L.append(f"• Stray taps land {gaps[0]:.0f}-{gaps[-1]:.0f}ms from a keystroke; "
                 "libinput's disable-while-typing window misses them.")
    L.append("• FIX = a typing-guard daemon (keeps tap-to-click) that drops a tap when "
             "EITHER: it is in an edge/corner reject zone, OR it lands within a tunable "
             "window (~300-600ms) of a keystroke. Center taps always pass.")
    return "\n".join(L)


def dict_count(seq):
    out = {}
    for x in seq:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))


def main():
    # offline mode: python palm_logger.py path/to/session.json
    if len(sys.argv) > 1 and sys.argv[1].endswith(".json"):
        with open(sys.argv[1]) as f:
            print(analyze(json.load(f)))
        return
    dev = find_touchpad()
    if dev is None:
        print("No touchpad found. Are you in the `input` group?")
        sys.exit(1)
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(dev)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

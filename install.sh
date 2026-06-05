#!/usr/bin/env bash
# install.sh — install palm-guard as a systemd --user service (no sudo).
#
#   ./install.sh            # install + enable + start
#
# Re-running is safe (idempotent): it refreshes files and restarts the service.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/.local/share/palm-guard"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="palm-guard.service"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- prerequisite checks ---------------------------------------------------
say "Checking prerequisites..."
[ "${XDG_SESSION_TYPE:-}" = "x11" ] || \
    echo "  WARNING: session is '${XDG_SESSION_TYPE:-unknown}', not x11 — palm-guard uses xinput and only works on X11."
command -v xinput >/dev/null   || die "xinput not found. Install: sudo apt install xinput"
command -v python3 >/dev/null  || die "python3 not found."
id -nG | tr ' ' '\n' | grep -qx input || \
    die "You are not in the 'input' group. Run: sudo usermod -aG input $USER  (then log out/in)"
xinput list --name-only | grep -qi touchpad || die "No touchpad found via xinput."

# ---- install files ---------------------------------------------------------
say "Installing to $APP ..."
mkdir -p "$APP" "$UNIT_DIR"
install -m 0644 "$REPO/palm_guard.py"            "$APP/palm_guard.py"
install -m 0755 "$REPO/scripts/run.sh"           "$APP/run.sh"
install -m 0755 "$REPO/scripts/restore-tapping.sh" "$APP/restore-tapping.sh"

# ---- python venv with evdev ------------------------------------------------
if [ ! -x "$APP/venv/bin/python" ]; then
    say "Creating venv (uses system site-packages) ..."
    python3 -m venv --system-site-packages "$APP/venv"
fi
say "Installing Python deps (evdev) ..."
"$APP/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$APP/venv/bin/pip" install --quiet -r "$REPO/requirements.txt"
"$APP/venv/bin/python" -c "import evdev" || die "evdev failed to import."

# ---- systemd unit ----------------------------------------------------------
say "Installing systemd --user unit ..."
install -m 0644 "$REPO/systemd/$UNIT" "$UNIT_DIR/$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

sleep 2
say "Status:"
systemctl --user --no-pager status "$UNIT" | head -8 || true
echo
say "Done. palm-guard is running and will auto-start on login."
echo "   Logs:    journalctl --user -u $UNIT -f"
echo "   Stop:    systemctl --user stop $UNIT"
echo "   Tune:    edit ExecStart in $UNIT_DIR/$UNIT, then:"
echo "            systemctl --user daemon-reload && systemctl --user restart $UNIT"

#!/usr/bin/env bash
# Launcher for palm-guard under systemd --user.
# Ensures DISPLAY/XAUTHORITY are set (xinput needs the X session), waits for the
# touchpad to be queryable, then execs the guard from its dedicated venv.
set -euo pipefail

APP="$HOME/.local/share/palm-guard"

# Fall back to a sane DISPLAY if the user manager didn't import one.
if [ -z "${DISPLAY:-}" ]; then
    for d in :0 :1 :2; do
        if [ -e "/tmp/.X11-unix/X${d#:}" ]; then export DISPLAY="$d"; break; fi
    done
    export DISPLAY="${DISPLAY:-:0}"
fi
# Fall back to a sane XAUTHORITY.
if [ -z "${XAUTHORITY:-}" ]; then
    if [ -e "/run/user/$(id -u)/gdm/Xauthority" ]; then
        export XAUTHORITY="/run/user/$(id -u)/gdm/Xauthority"
    elif [ -e "$HOME/.Xauthority" ]; then
        export XAUTHORITY="$HOME/.Xauthority"
    fi
fi

# Wait for the X server / touchpad to appear (boot race).
for _ in $(seq 1 30); do
    if xinput list --name-only 2>/dev/null | grep -qi touchpad; then break; fi
    sleep 1
done

exec "$APP/venv/bin/python" -u "$APP/palm_guard.py" "$@"

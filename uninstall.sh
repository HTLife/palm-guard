#!/usr/bin/env bash
# uninstall.sh — remove the palm-guard service and files. Restores tap-to-click.
set -euo pipefail

APP="$HOME/.local/share/palm-guard"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="palm-guard.service"

echo "==> Stopping and disabling service ..."
systemctl --user disable --now "$UNIT" 2>/dev/null || true
rm -f "$UNIT_DIR/$UNIT"
systemctl --user daemon-reload 2>/dev/null || true

echo "==> Restoring tap-to-click ..."
[ -x "$APP/restore-tapping.sh" ] && "$APP/restore-tapping.sh" || \
    { name="$(xinput list --name-only 2>/dev/null | grep -i touchpad | head -1)"; \
      [ -n "$name" ] && xinput set-prop "$(xinput list --id-only "$name")" \
      "libinput Tapping Enabled" 1 2>/dev/null || true; }

echo "==> Removing $APP ..."
rm -rf "$APP"

echo "Done. palm-guard removed."

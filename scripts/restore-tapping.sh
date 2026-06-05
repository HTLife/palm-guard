#!/usr/bin/env bash
# Re-enable tap-to-click on the touchpad. Used as the service's ExecStopPost
# fallback (the daemon also restores on a clean SIGTERM). Resolves the touchpad
# xinput id dynamically so it is portable across machines.
name="$(xinput list --name-only 2>/dev/null | grep -i touchpad | head -1)" || exit 0
[ -z "$name" ] && exit 0
id="$(xinput list --id-only "$name" 2>/dev/null)" || exit 0
[ -n "$id" ] && xinput set-prop "$id" "libinput Tapping Enabled" 1 2>/dev/null || true
exit 0

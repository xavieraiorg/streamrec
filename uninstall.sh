#!/usr/bin/env bash
# Remove StreamRec. Recordings are never touched.
#   ./uninstall.sh           remove app, keep settings and log
#   ./uninstall.sh --purge   also remove settings (~/.config/StreamRec) and log
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"

rm -rf "$DATA/streamrec"
rm -f  "$HOME/.local/bin/streamrec"
rm -f  "$DATA/applications/streamrec.desktop"
rm -f  "$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")/streamrec.desktop"
rm -f  "$DATA/icons/hicolor/scalable/apps/streamrec.svg"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$DATA/applications" || true

if [ "${1:-}" = "--purge" ]; then
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/StreamRec"
  rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/streamrec"
  echo "Removed StreamRec, its settings and log."
else
  echo "Removed StreamRec. Settings kept in ~/.config/StreamRec (use --purge to remove)."
fi
echo "Your recordings were not touched. System packages (ffmpeg, v4l-utils) were left installed."

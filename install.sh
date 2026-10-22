#!/usr/bin/env bash
# StreamRec installer for Ubuntu / Debian (also works for updating).
#   chmod +x install.sh && ./install.sh
# Options:
#   --no-apt   skip system packages (install ffmpeg etc. yourself)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA/streamrec"
BIN="$HOME/.local/bin"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/scalable/apps"
NO_APT=0
for a in "$@"; do
  case "$a" in
    --no-apt) NO_APT=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
  esac
done

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*"; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run as your normal user (not with sudo). It asks for your password when needed."
[ -f "$SRC/streamrec.py" ] || die "streamrec.py not found next to install.sh"

# ---------------------------------------------------------------- 1. system packages
PKGS="ffmpeg python3 python3-venv python3-pip v4l-utils xdg-utils
      libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1 libxcb-image0
      libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libegl1 libgl1 libfontconfig1 libdbus-1-3"
if [ "$NO_APT" = 0 ] && command -v apt-get >/dev/null 2>&1; then
  say "Installing system packages (ffmpeg, python venv, camera tools)…"
  sudo apt-get update -qq || warn "apt update had problems – continuing"
  WANT=""
  for p in $PKGS; do
    if apt-cache show "$p" >/dev/null 2>&1; then WANT="$WANT $p"; fi
  done
  # shellcheck disable=SC2086
  sudo apt-get install -y --no-install-recommends $WANT
  ok "system packages"
elif [ "$NO_APT" = 0 ]; then
  warn "apt-get not found. Install these with your package manager: ffmpeg python3 python3-venv v4l-utils"
fi

command -v ffmpeg  >/dev/null 2>&1 || die "ffmpeg is missing (sudo apt install ffmpeg)"
command -v ffprobe >/dev/null 2>&1 || die "ffprobe is missing (comes with ffmpeg)"
python3 - <<'EOF' || die "Python 3.9 or newer is needed"
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
EOF

# ---------------------------------------------------------------- 2. python env
say "Setting up Python environment in $PREFIX/venv …"
mkdir -p "$PREFIX/app"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
  python3 -m venv "$PREFIX/venv" || die "python3 -m venv failed (sudo apt install python3-venv)"
fi
"$PREFIX/venv/bin/python" -m pip install -q --upgrade pip
say "Installing PySide6 (Qt for Python, ~150 MB download the first time)…"
"$PREFIX/venv/bin/python" -m pip install -q --upgrade "PySide6-Essentials>=6.5,<7"
ok "python packages"

# ---------------------------------------------------------------- 3. app files + launchers
say "Installing StreamRec…"
install -m 644 "$SRC/streamrec.py" "$PREFIX/app/streamrec.py"
[ -f "$SRC/streamrec.svg" ] && install -m 644 "$SRC/streamrec.svg" "$PREFIX/app/streamrec.svg"
[ -f "$SRC/uninstall.sh" ] && install -m 755 "$SRC/uninstall.sh" "$PREFIX/uninstall.sh"

mkdir -p "$BIN"
cat > "$BIN/streamrec" <<EOF
#!/bin/sh
# StreamRec launcher (made by install.sh)
exec "$PREFIX/venv/bin/python" "$PREFIX/app/streamrec.py" "\$@"
EOF
chmod 755 "$BIN/streamrec"

mkdir -p "$APPS" "$ICONS"
[ -f "$SRC/streamrec.svg" ] && install -m 644 "$SRC/streamrec.svg" "$ICONS/streamrec.svg"
cat > "$APPS/streamrec.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=StreamRec
GenericName=Camera Recorder
Comment=Record RTSP / USB camera streams
Exec=$BIN/streamrec
Icon=streamrec
Terminal=false
Categories=AudioVideo;Video;Recorder;
Keywords=rtsp;camera;thermal;record;webcam;
StartupWMClass=StreamRec
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
ok "command 'streamrec' and app-menu entry"

DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESK" ]; then
  install -m 755 "$APPS/streamrec.desktop" "$DESK/streamrec.desktop"
  command -v gio >/dev/null 2>&1 && gio set "$DESK/streamrec.desktop" metadata::trusted true 2>/dev/null || true
  ok "desktop icon"
fi

# ---------------------------------------------------------------- 4. camera permission
RELOGIN=0
if getent group video >/dev/null 2>&1 && ! id -nG "$USER" | tr ' ' '\n' | grep -qx video; then
  say "Adding $USER to the 'video' group (needed for USB cameras)…"
  if sudo usermod -aG video "$USER"; then RELOGIN=1; ok "video group"; else warn "could not add to video group"; fi
fi

mkdir -p "$HOME/Videos"

# ---------------------------------------------------------------- 5. check
echo
"$BIN/streamrec" --check || warn "self-check reported a problem (see above)"
echo
ok "StreamRec installed."
echo "    Start it from the app menu (StreamRec) or run:  streamrec"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) warn "$BIN is not in your PATH yet – log out and back in, or run: $BIN/streamrec" ;;
esac
[ "$RELOGIN" = 1 ] && warn "Log out and back in once so USB camera access works."
echo "    Uninstall:  $PREFIX/uninstall.sh"

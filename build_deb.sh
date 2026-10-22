#!/usr/bin/env bash
# Build streamrec_<version>_amd64.deb: one offline installer with Python, Qt and FFmpeg bundled.
#   ./build_deb.sh [output-dir]       (needs internet while building, not when installing)
# Install on any Ubuntu 22.04+ PC:  sudo apt install ./streamrec_<version>_amd64.deb
set -euo pipefail
unset PYTHONPATH PYTHONHOME          # keep ROS etc. out of the bundle

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$(cd "${1:-$SRC/..}" && pwd)"
VER=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$SRC/streamrec.py")
FF_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest
FF_TAR=ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

say "Python build env…"
python3 -m venv "$W/venv"
"$W/venv/bin/pip" install -q --upgrade pip "PySide6-Essentials>=6.5,<7" pyinstaller

say "Freezing app with PyInstaller…"
cp "$SRC/streamrec.py" "$SRC/streamrec.svg" "$W/"
(cd "$W" && venv/bin/pyinstaller -y --log-level ERROR --name streamrec --windowed \
  --add-data streamrec.svg:. --exclude-module tkinter --exclude-module PySide6.QtQml \
  --exclude-module PySide6.QtQuick --exclude-module PySide6.QtNetwork --exclude-module PySide6.QtPdf \
  streamrec.py)

say "Static FFmpeg ($FF_TAR)…"
(cd "$W" && curl -fsSLO "$FF_URL/$FF_TAR" && curl -fsSLO "$FF_URL/checksums.sha256" \
  && grep " $FF_TAR\$" checksums.sha256 | sha256sum -c --quiet && tar xf "$FF_TAR")

say "libxcb-cursor0 (Qt on X11 sessions)…"
(cd "$W" && apt-get download -qq libxcb-cursor0 && dpkg-deb -x libxcb-cursor0_*.deb xcbc)

say "Assembling package…"
P="$W/pkg"
mkdir -p "$P/DEBIAN" "$P/opt/streamrec/bin" "$P/usr/bin" "$P/usr/share/applications" \
         "$P/usr/share/icons/hicolor/scalable/apps"
cp -a "$W/dist/streamrec/." "$P/opt/streamrec/"
cp -a "$W"/xcbc/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0* "$P/opt/streamrec/_internal/"
install -m 755 "$W"/ffmpeg-n8.1*/bin/ffmpeg "$W"/ffmpeg-n8.1*/bin/ffprobe "$P/opt/streamrec/bin/"
install -m 644 "$SRC/streamrec.svg" "$P/usr/share/icons/hicolor/scalable/apps/streamrec.svg"

cat > "$P/usr/bin/streamrec" <<'EOF'
#!/bin/sh
# StreamRec launcher: use the bundled ffmpeg, ignore any outside Python setup (e.g. ROS)
PATH="/opt/streamrec/bin:$PATH"; export PATH
unset PYTHONPATH PYTHONHOME
exec /opt/streamrec/streamrec "$@"
EOF
chmod 755 "$P/usr/bin/streamrec"

cat > "$P/usr/share/applications/streamrec.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=StreamRec
GenericName=Camera Recorder
Comment=Record RTSP / USB camera streams
Exec=/usr/bin/streamrec
Icon=streamrec
Terminal=false
Categories=AudioVideo;Video;Recorder;
Keywords=rtsp;camera;thermal;record;webcam;
StartupWMClass=StreamRec
EOF

cat > "$P/DEBIAN/control" <<EOF
Package: streamrec
Version: $VER
Architecture: amd64
Maintainer: Nishan <nishan.s@xavierai.co>
Installed-Size: $(du -sk "$P/opt" "$P/usr" | awk '{s+=$1} END{print s}')
Depends: libc6 (>= 2.35), libgl1, libegl1, libfontconfig1, libxkbcommon0, libdbus-1-3
Recommends: v4l-utils
Section: video
Priority: optional
Description: StreamRec - RTSP / USB camera recorder
 Desktop app to record thermal and other camera streams (RTSP or USB)
 into split MKV/MP4 files with live preview and auto-reconnect.
 Python, Qt and FFmpeg are bundled; no internet needed to install.
EOF

# Desktop icon + camera access for the user who ran "sudo apt install"
cat > "$P/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
U="${SUDO_USER:-}"
if [ -n "$U" ] && [ "$U" != root ] && id "$U" >/dev/null 2>&1; then
  getent group video >/dev/null && usermod -aG video "$U" || true
  H=$(getent passwd "$U" | cut -d: -f6)
  D=$(su -s /bin/sh "$U" -c 'xdg-user-dir DESKTOP' 2>/dev/null || echo "$H/Desktop")
  if [ -d "$D" ]; then
    install -o "$U" -m 755 /usr/share/applications/streamrec.desktop "$D/streamrec.desktop"
    su -s /bin/sh "$U" -c "gio set '$D/streamrec.desktop' metadata::trusted true" 2>/dev/null || true
  fi
fi
exit 0
EOF
cat > "$P/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
  U="${SUDO_USER:-}"
  if [ -n "$U" ] && [ "$U" != root ]; then
    D=$(su -s /bin/sh "$U" -c 'xdg-user-dir DESKTOP' 2>/dev/null || true)
    [ -n "$D" ] && rm -f "$D/streamrec.desktop" || true
  fi
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
fi
exit 0
EOF
chmod 755 "$P/DEBIAN/postinst" "$P/DEBIAN/postrm"

DEB="$OUT/streamrec_${VER}_amd64.deb"
dpkg-deb --root-owner-group -Zxz --build "$P" "$DEB" >/dev/null
say "Done: $DEB ($(du -h "$DEB" | cut -f1))"
echo "    Install:  sudo apt install $DEB"

# StreamRec

Record RTSP (IP / thermal) cameras or USB / laptop cameras to video files, with live preview, auto-split and auto-reconnect. Linux (Ubuntu 22.04 / 24.04 tested), uses FFmpeg underneath.

## Install (recommended: one file, works offline)

Copy `streamrec_1.0.1_amd64.deb` to the PC, open a terminal in that folder and run:

```bash
sudo apt install ./streamrec_1.0.1_amd64.deb
```

Everything is inside (Python, Qt, FFmpeg), so nothing else is downloaded. It adds **StreamRec** to the app menu and the Desktop, and gives your user camera access. Log out and back in once if a USB camera isn't found.

- Update: install the newer `.deb` the same way. Settings are kept.
- Uninstall: `sudo apt remove streamrec`. Recordings and settings are kept.
- Build the `.deb` again after changing the code: `./build_deb.sh` (needs internet while building).

### Alternative: script install (needs internet)

`chmod +x install.sh && ./install.sh` installs ffmpeg with apt and PySide6 in `~/.local/share/streamrec`. Remove it with `~/.local/share/streamrec/uninstall.sh`.

## Use

1. **Source**: pick *RTSP* and paste the camera URL (e.g. `rtsp://admin:pass@192.168.1.64:554/stream1`), then press **Test connection**. For a USB camera, pick *USB*, choose the device (⟳ rescans) and the resolution.
2. **Camera**: `zt6`, `uv40`, or type a new name. It's used in the file names.
3. **Recording**: folder (default `~/Videos`), format, quality and split time.
4. Press **Start recording**. **Stop** finishes the current file properly.

Files are saved as:

```
~/Videos/<camera>/<YYYY-MM-DD>/<camera>_<YYYY-MM-DD>_<HH-MM-SS>.mkv
```

With splitting on, a new file starts on the clock (for example every 10 min at :00, :10, :20…), and after every reconnect. After midnight new files go into the new day's folder.

### Quality modes

| Mode | RTSP | USB |
|---|---|---|
| **Auto** (default) | Keeps the camera's video untouched if it already sends the chosen FPS, otherwise converts to that FPS (H.264) | Compresses to H.264 at the chosen FPS |
| **Original** | Never re-encodes: exact camera video, lowest CPU. FPS is the camera's own | n/a |
| **Re-encode H.264** | Always converts. Smaller files, more CPU | same as Auto |

**MKV** (default) stays playable if the power is cut. **MP4** files are also written in a crash-safe (fragmented) way.

### Reconnect

If the stream drops or freezes for 15 s, StreamRec closes the file cleanly, waits the *Retry every* time (default 5 s) and starts again with a new file. The RECONNECTS counter shows how many times the stream dropped. Recording stops on its own if the disk drops below 300 MB free.

## Recording two cameras at once

Start StreamRec twice (one window per camera) and give each a different camera name.

## Troubleshooting

- **Wrong username or password / 404 path not found**: check the RTSP URL in VLC (*Media → Open Network Stream*). The path after the IP differs by brand.
- **No answer from camera**: check that the IP is reachable (`ping 192.168.1.64`) and that RTSP is enabled on the camera.
- **USB camera: permission denied**: log out and back in after installing (video group).
- **Camera used by another app**: close other apps using it (Cheese, browser, Zoom).
- **Grainy or blocky UDP video**: switch Transport to TCP.
- Self-check: `streamrec --check`
- Log file: `~/.local/state/streamrec/streamrec.log` (has the exact ffmpeg command, with the password hidden)

## Notes

- Video only. Camera audio is not recorded.
- The RTSP URL (with its password) is saved in `~/.config/StreamRec/StreamRec.conf` so it's there next time. Passwords are hidden in the log.

## Uninstall

```bash
~/.local/share/streamrec/uninstall.sh            # keeps settings
~/.local/share/streamrec/uninstall.sh --purge    # removes settings and log too
```

Recordings are never deleted.

#!/usr/bin/env python3
"""
StreamRec - record RTSP / USB (V4L2) camera streams to video files.

Uses FFmpeg as the recording engine (one process per recording: it writes the
split files and also feeds a small preview stream to the window).

Run:      python3 streamrec.py        (or `streamrec` after ./install.sh)
Check:    python3 streamrec.py --check
"""
import collections
import datetime as dt
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import Qt, QObject, QTimer, QRectF, QSettings, QUrl, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QImage, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QComboBox, QLineEdit, QPushButton,
    QGroupBox, QFormLayout, QHBoxLayout, QVBoxLayout, QCheckBox, QSpinBox,
    QPlainTextEdit, QSizePolicy, QFrame, QToolButton, QFileDialog, QMessageBox,
)

APP_NAME = "StreamRec"
APP_VERSION = "1.0.1"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

PREVIEW_FPS = 15          # preview frames per second sent to the window
PREVIEW_W = 640           # preview width in pixels (height follows the camera)
STALL_SECONDS = 15        # no new video for this long -> restart ffmpeg
FIRST_FRAME_SECONDS = 25  # no first frame within this time -> restart ffmpeg
NET_TIMEOUT_S = 10        # RTSP socket timeout
DISK_WARN = 2 * 1024**3   # warn when free space drops below this
DISK_STOP = 300 * 1024**2 # stop recording below this (avoid filling the disk)


# --------------------------------------------------------------------------- helpers
def now_s():
    return dt.datetime.now().strftime("%H:%M:%S")


def human_size(n):
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.2f} {unit}"
        n /= 1024


def fmt_dur(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s.strip()).strip("._")
    return s or "cam"


def mask_url(url):
    """rtsp://user:secret@host/... -> rtsp://user:****@host/..."""
    try:
        p = urlsplit(url)
        if p.password:
            host = p.hostname or ""
            if p.port:
                host += f":{p.port}"
            return urlunsplit((p.scheme, f"{p.username}:****@{host}", p.path, p.query, p.fragment))
    except Exception:
        pass
    return url


def ffmpeg_major():
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"version\s+n?(\d+)\.", out)
        return int(m.group(1)) if m else 7     # git builds ("N-1234-g...") are new
    except Exception:
        return 6


FF_MAJOR = None


def rtsp_input_opts(transport):
    """RTSP options. The socket-timeout option was renamed in FFmpeg 5
    (-stimeout -> -timeout). On FFmpeg 4, -timeout means 'listen', so pick carefully."""
    global FF_MAJOR
    if FF_MAJOR is None:
        FF_MAJOR = ffmpeg_major()
    us = str(NET_TIMEOUT_S * 1_000_000)
    tmo = ["-timeout", us] if FF_MAJOR >= 5 else ["-stimeout", us]
    return ["-rtsp_transport", transport] + tmo


def parse_rate(r):
    try:
        a, b = r.split("/")
        a, b = float(a), float(b)
        if b and 0 < a / b < 240:
            return round(a / b, 2)
    except Exception:
        pass
    return None


FRIENDLY = [
    (r"401|Unauthorized", "Wrong username or password in the RTSP URL"),
    (r"404|Not Found", "Stream path not found on the camera (check the part after the IP)"),
    (r"Connection refused", "Camera refused the connection (wrong port, or RTSP disabled)"),
    (r"timed out|Timeout|Connection timed", "No answer from camera (check IP / network / power)"),
    (r"No route to host|Network is unreachable", "Camera not reachable on the network"),
    (r"Device or resource busy", "Camera is already used by another app"),
    (r"Permission denied", "Permission denied (USB: log out/in after install so the 'video' group applies)"),
    (r"No such file or directory", "Device or folder not found"),
    (r"Invalid data found", "Camera sent data FFmpeg can't read"),
    (r"No space left", "Disk is full"),
]


def scrub(text):
    """Hide passwords in any rtsp://user:pass@ inside a message."""
    return re.sub(r"(://[^/:@\s]+):[^@\s/]+@", r"\1:****@", text)


def friendly(msg):
    msg = scrub(msg)
    for pat, text in FRIENDLY:
        if re.search(pat, msg, re.I):
            return f"{text}  [{msg.strip()[:160]}]"
    return msg.strip()[:300]


class Cancelled(Exception):
    pass


def run_cancellable(cmd, timeout, stop_event):
    """Run a short command; abort when stop_event is set or timeout passes."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    t0 = time.time()
    while p.poll() is None:
        if stop_event is not None and stop_event.is_set():
            p.kill(); p.wait()
            raise Cancelled()
        if time.time() - t0 > timeout:
            p.kill(); p.wait()
            raise RuntimeError("Timeout – no answer")
        time.sleep(0.1)
    out, err = p.communicate()
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def probe(input_args, stop_event=None, timeout=NET_TIMEOUT_S + 8):
    """ffprobe the first video stream. Returns dict(codec, width, height, fps)."""
    args = list(input_args)
    if "-thread_queue_size" in args:     # ffmpeg-only option; ffprobe ≥ 7 rejects it
        i = args.index("-thread_queue_size")
        del args[i:i + 2]
    cmd = [FFPROBE, "-v", "error"] + args + [
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json"]
    rc, out, err = run_cancellable(cmd, timeout, stop_event)
    try:
        st = json.loads(out or "{}").get("streams", [])
    except ValueError:
        st = []
    if rc != 0 or not st:
        last = [l for l in err.strip().splitlines() if l.strip()]
        raise RuntimeError(friendly(last[-1] if last else "No video stream found"))
    s = st[0]
    fps = parse_rate(s.get("avg_frame_rate", "")) or parse_rate(s.get("r_frame_rate", ""))
    return {"codec": s.get("codec_name", "?"), "width": s.get("width") or 0,
            "height": s.get("height") or 0, "fps": fps}


# ---- USB / V4L2 -----------------------------------------------------------
def list_v4l2_devices():
    """[(path, name)] of capture devices. Skips metadata nodes (videoN with index>0)."""
    devs = []
    paths = sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))
    have_ctl = shutil.which("v4l2-ctl")
    for p in paths:
        base = os.path.basename(p)
        name = ""
        try:
            with open(f"/sys/class/video4linux/{base}/name") as f:
                name = f.read().strip()
        except OSError:
            pass
        if have_ctl:
            try:
                out = subprocess.run(["v4l2-ctl", "-d", p, "--list-formats"], capture_output=True,
                                     text=True, timeout=3).stdout
                if "[0]" not in out:
                    continue
            except Exception:
                pass
        else:
            try:
                with open(f"/sys/class/video4linux/{base}/index") as f:
                    if f.read().strip() != "0":
                        continue
            except OSError:
                pass
        devs.append((p, name or base))
    return devs


def v4l2_formats(dev):
    """{'raw': {(w,h)}, 'mjpeg': {(w,h)}} supported by the device."""
    res = {"raw": set(), "mjpeg": set()}
    try:
        err = subprocess.run([FFMPEG, "-hide_banner", "-f", "v4l2", "-list_formats", "all", "-i", dev],
                             capture_output=True, text=True, timeout=5).stderr
    except Exception:
        return res
    for line in err.splitlines():
        m = re.search(r"(Raw|Compressed)\s*:\s*(\S+)\s*:.*?:\s*([\dx ]+)$", line)
        if not m:
            continue
        kind, fmt, sizes = m.groups()
        key = "raw" if kind == "Raw" else ("mjpeg" if fmt == "mjpeg" else None)
        if not key:
            continue
        for s in sizes.split():
            if re.fullmatch(r"\d+x\d+", s):
                w, h = map(int, s.split("x"))
                res[key].add((w, h))
    return res


# --------------------------------------------------------------------------- engine
class Recorder(QObject):
    """Runs and supervises ffmpeg in a background thread."""
    log = Signal(str, str)      # level ('info'|'warn'|'error'|'ok'), message
    state = Signal(str)         # 'connecting' | 'rec' | 'retry' | 'idle'
    frame = Signal(QImage)
    info = Signal(str)          # overlay text e.g. "zt6 · 640x512 · 30 fps · H.264 (original)"
    finished = Signal()

    PROG_RE = re.compile(r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time(?:_us|_ms)?|"
                         r"dup_frames|drop_frames|speed|progress)=(.*)$")
    LVL_RE = re.compile(r"\[(trace|debug|verbose|info|warning|error|fatal|panic)\]\s?")
    # Decoder noise from joining a live H.264/H.265 stream between keyframes. Harmless at the
    # start (recorded file starts at a keyframe); only worth a warning if it keeps happening.
    GLITCH_RE = re.compile(r"Could not find ref with POC|Error constructing the frame RPS|"
                           r"undecodable NALU|non-existing PPS|no frame!|decode_slice_header error|"
                           r"error while decoding MB|concealing \d+ DC|Missing reference picture|"
                           r"Two slices reporting being the first|Invalid NAL unit size|"
                           r"PPS id out of range|Duplicate POC")
    GLITCH_GRACE_S = 10         # ignore decoder noise this long after (re)connecting
    GLITCH_WARN_N = 5           # warn when this many happen within a minute after that

    def __init__(self):
        super().__init__()
        self._thread = None
        self._stop = threading.Event()
        self.proc = None
        self.preview_wanted = True
        self.frame_pending = False
        self._copy = False
        self._src_fps = None
        self.snap = self._blank_snap()

    @staticmethod
    def _blank_snap():
        return {"start": None, "reconnects": 0, "fps": None, "file": None, "file_size": None,
                "total": 0, "bitrate": None, "state": "idle"}

    # ---- public API -------------------------------------------------------
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, cfg):
        if self.is_running():
            return
        self.cfg = cfg
        self._stop.clear()
        self.snap = self._blank_snap()
        self.snap["start"] = time.time()
        self._thread = threading.Thread(target=self._supervise, name="recorder", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)
        return not self.is_running()

    # ---- internals ----------------------------------------------------------
    def _log(self, level, msg):
        self.log.emit(level, msg)

    def _set_state(self, s):
        self.snap["state"] = s
        self.state.emit(s)

    def _supervise(self):
        cfg = self.cfg
        attempt = 0
        try:
            while not self._stop.is_set():
                self._set_state("connecting" if attempt == 0 else "retry")
                had_video = self._run_once(attempt)
                attempt += 1
                if self._stop.is_set():
                    break
                if not cfg["reconnect"]:
                    self._log("error", "Recording stopped (auto-reconnect is off).")
                    break
                if had_video:
                    self.snap["reconnects"] += 1     # counts stream drops
                self.snap["fps"] = None
                self.snap["bitrate"] = None
                self._set_state("retry")
                self._log("warn", f"Retrying in {cfg['retry']} s")
                if self._stop.wait(cfg["retry"]):
                    break
        except Exception as e:  # never die silently
            self._log("error", f"Internal error: {e!r}")
        finally:
            self.proc = None
            self._set_state("idle")
            self._log("info", "Stopped.")
            self.finished.emit()

    # -- plan -------------------------------------------------------------------
    def _input_args(self):
        c = self.cfg
        if c["source"] == "rtsp":
            return rtsp_input_opts(c["transport"]) + ["-i", c["url"]]
        a = ["-f", "v4l2", "-thread_queue_size", "512"]
        if c.get("input_format"):
            a += ["-input_format", c["input_format"]]
        if c.get("resolution"):
            a += ["-video_size", c["resolution"]]
        if c.get("fps"):
            a += ["-framerate", str(c["fps"])]
        return a + ["-i", c["device"]]

    def _decide(self, meta):
        """-> (copy: bool, out_fps: int|None, why: str)"""
        c = self.cfg
        want, src, codec = c["fps"], meta["fps"], meta["codec"]
        copy_ok = codec in ("h264", "hevc") or c["ext"] == "mkv"
        if c["source"] == "usb":
            return False, want, "USB camera → H.264"
        if c["mode"] == "encode":
            return False, want, "re-encode selected"
        if not copy_ok:
            return False, want, f"{codec} can't go into MP4 as-is → H.264"
        if c["mode"] == "copy":
            return True, None, "original selected"
        if want is None:
            return True, None, "camera FPS kept"
        if src is None:
            return True, None, "camera FPS unknown → kept as-is"
        if abs(src - want) < 0.6:
            return True, None, f"camera already sends {src:g} fps"
        return False, want, f"camera sends {src:g} fps → converting to {want}"

    def _day_dir(self, ts=None):
        """Folder for files started at time ts: <base>/<cam>/<YYYY-MM-DD> or <base>."""
        c = self.cfg
        if not c["subdirs"]:
            return c["outdir"]
        return os.path.join(c["outdir"], c["cam"], time.strftime("%Y-%m-%d", time.localtime(ts)))

    def _ensure_dirs(self):
        t = time.time()
        for ts in (t, t + 180):   # also make tomorrow's folder just before midnight
            os.makedirs(self._day_dir(ts), exist_ok=True)

    def _build_cmd(self, meta, copy, out_fps, pw, ph):
        c = self.cfg
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "level+info", "-nostats",
               "-progress", "pipe:2", "-y"]
        if c["source"] == "rtsp":
            cmd += ["-fflags", "+genpts"]
        cmd += self._input_args()
        # --- output 1: the recording ---
        cmd += ["-map", "0:v:0", "-an", "-sn", "-dn"]
        if copy:
            cmd += ["-c:v", "copy"]
        else:
            vf = []
            if out_fps:
                vf.append(f"fps={out_fps}")
            vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p")
            gop = int(round((out_fps or meta.get("fps") or 30) * 2))
            cmd += ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-g", str(gop)]
        fmt = "matroska" if c["ext"] == "mkv" else "mp4"
        frag = "+frag_keyframe+empty_moov+default_base_moof"   # MP4 that survives a crash
        f_pat = f"{c['cam']}_%Y-%m-%d_%H-%M-%S.{c['ext']}"
        if c["split"]:
            base_esc = c["outdir"].replace("%", "%%")      # literal % in folder names
            d_esc = os.path.join(base_esc, c["cam"], "%Y-%m-%d") if c["subdirs"] else base_esc
            pattern = os.path.join(d_esc, f_pat)
            cmd += ["-f", "segment", "-segment_time", str(c["split"]), "-segment_atclocktime", "1",
                    "-reset_timestamps", "1", "-strftime", "1", "-segment_format", fmt]
            if fmt == "mp4":
                cmd += ["-segment_format_options", f"movflags={frag}"]
            cmd.append(pattern)
        else:
            d = self._day_dir()
            path = os.path.join(d, time.strftime(f_pat))
            n = 2
            while os.path.exists(path):
                path = os.path.join(d, time.strftime(f_pat).replace(f".{c['ext']}", f"_{n}.{c['ext']}"))
                n += 1
            if fmt == "mp4":
                cmd += ["-movflags", frag]
            cmd += ["-f", fmt, path]
            self._single_path = path
        # --- output 2: small raw preview for the window ---
        if c["preview"]:
            cmd += ["-map", "0:v:0", "-an", "-sn", "-dn", "-vf",
                    f"fps={PREVIEW_FPS},scale={pw}:{ph}:force_original_aspect_ratio=decrease,"
                    f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=rgb24",
                    "-f", "rawvideo", "pipe:1"]
        return cmd

    # -- one ffmpeg run ------------------------------------------------------
    def _run_once(self, attempt):
        c = self.cfg
        self._single_path = None
        # 1) probe (also our connection test)
        if c["source"] == "rtsp":
            self._log("info", f"Connecting {mask_url(c['url'])} ({c['transport'].upper()})…")
        else:
            self._log("info", f"Opening {c['device']}…")
        try:
            meta = probe(self._input_args(), self._stop)
        except Cancelled:
            return False
        except Exception as e:
            self._log("error", f"Can't open stream: {e}")
            return False
        fps_txt = f"{meta['fps']:g} fps" if meta["fps"] else "? fps"
        self._log("ok", f"Connected · {meta['codec'].upper()} {meta['width']}x{meta['height']} @ {fps_txt}")

        copy, out_fps, why = self._decide(meta)
        self._copy, self._src_fps = copy, meta["fps"]
        pw = PREVIEW_W
        ph = PREVIEW_W * 9 // 16
        if meta["width"] and meta["height"]:
            ph = max(2, min(PREVIEW_W, int(PREVIEW_W * meta["height"] / meta["width"]) // 2 * 2))
        try:
            self._ensure_dirs()
        except OSError as e:
            self._log("error", f"Can't create folder: {e}")
            self._stop.set()
            return False
        cmd = self._build_cmd(meta, copy, out_fps, pw, ph)
        self._log("debug", "ffmpeg command: " + scrub(subprocess.list2cmdline(cmd)))
        rec_fps = out_fps or meta["fps"]
        mode_txt = f"{'H.264' if not copy else meta['codec'].upper()} ({'original' if copy else 're-encoded'})"
        self.info.emit(f"{c['cam']} · {meta['width']}x{meta['height']} · "
                       f"{(f'{rec_fps:g} fps') if rec_fps else '? fps'} · {mode_txt}")
        sp = c["split"]
        split_txt = (f"split {sp // 60} min" if sp >= 60 else f"split {sp} s") if sp else "one file"
        self._log("info", f"Mode: {'original, no re-encode' if copy else 're-encode H.264'} ({why}), {split_txt}")

        # 2) launch ffmpeg
        self._errtail = collections.deque(maxlen=20)
        self._last_alive = time.time()
        self._progress = {}
        self._got_frame = False
        self._session_files = getattr(self, "_session_files", [])
        if attempt == 0:
            self._session_files = []
        self._seen_msgs = {}
        self._glitches = collections.deque()
        self._proc_t0 = time.time()
        self._run_files = 0
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE if c["preview"] else subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            self._log("error", f"Can't start ffmpeg: {e}")
            self._stop.set()
            return False
        self.proc = proc
        if self._single_path:
            self._add_file(self._single_path)
        t_err = threading.Thread(target=self._read_err, args=(proc,), daemon=True)
        t_err.start()
        t_out = None
        if c["preview"]:
            t_out = threading.Thread(target=self._read_frames, args=(proc, pw, ph), daemon=True)
            t_out.start()

        # 3) watch it
        t0 = time.time()
        last_scan = last_disk = last_dirs = 0
        sizes = collections.deque(maxlen=12)
        stopped_by_us = False
        announced = False
        while proc.poll() is None:
            now = time.time()
            if self._stop.is_set():
                self._graceful(proc)
                stopped_by_us = True
                break
            if self._got_frame and not announced:
                announced = True
                self._set_state("rec")
            if self._got_frame and now - self._last_alive > STALL_SECONDS:
                self._log("warn", f"No video for {STALL_SECONDS} s – stream stalled")
                self._graceful(proc, quick=True)
                break
            if not self._got_frame and now - t0 > FIRST_FRAME_SECONDS:
                self._log("warn", "No video received from camera")
                self._graceful(proc, quick=True)
                break
            if now - last_scan >= 1:
                last_scan = now
                self._update_sizes(sizes)
            if now - last_dirs >= 30:
                last_dirs = now
                try:
                    self._ensure_dirs()
                except OSError:
                    pass
            if now - last_disk >= 10:
                last_disk = now
                self._check_disk()
            time.sleep(0.25)

        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        t_err.join(3)
        if t_out:
            t_out.join(3)
        self._update_sizes(sizes)
        self.proc = None
        if stopped_by_us or self._stop.is_set():
            return self._got_frame
        errs = [m for lvl, m in self._errtail if lvl != "warning"]
        warns = [m for lvl, m in self._errtail if lvl == "warning"]
        if errs:
            reason = friendly(errs[-1])
        elif proc.returncode == 0:
            reason = "camera closed the stream"
        elif warns:
            reason = friendly(warns[-1])
        else:
            reason = f"ffmpeg exited (code {proc.returncode})"
        self._log("error", f"Stream lost · {reason}")
        return self._got_frame

    def _graceful(self, proc, quick=False):
        """Ask ffmpeg to finish the file properly: 'q' -> SIGINT -> SIGKILL."""
        steps = [(None, 1 if quick else 4), (signal.SIGINT, 5), (signal.SIGKILL, 3)]
        for sig, wait in steps:
            try:
                if sig is None:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                else:
                    proc.send_signal(sig)
            except Exception:
                pass
            try:
                proc.wait(wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def _add_file(self, path):
        if path not in self._session_files:
            self._session_files.append(path)
            self.snap["file"] = path
            name = os.path.basename(path)
            self._run_files += 1
            if len(self._session_files) == 1:
                self._log("ok", f"Recording → {name}")
            elif self._run_files == 1:
                self._log("ok", f"Reconnected · new file → {name}")
            else:
                self._log("info", f"New file → {name}")

    def _update_sizes(self, sizes):
        total = 0
        cur = None
        for p in self._session_files:
            try:
                s = os.path.getsize(p)
            except OSError:
                s = 0
            total += s
            if p == self.snap["file"]:
                cur = s
        self.snap["total"] = total
        self.snap["file_size"] = cur
        try:
            frames = int(self._progress.get("frame", "0"))
        except ValueError:
            frames = 0
        try:
            out_t = int(self._progress.get("out_time_us", "0")) / 1e6
        except ValueError:
            out_t = 0.0
        sizes.append((time.time(), total, frames, out_t))
        if len(sizes) >= 3:      # rates over the last ~10 s
            (t1, b1, f1, o1), (t2, b2, f2, o2) = sizes[0], sizes[-1]
            if t2 > t1:
                if b2 >= b1:
                    self.snap["bitrate"] = (b2 - b1) * 8 / (t2 - t1)
                if not self._copy and f2 >= f1:
                    self.snap["fps"] = (f2 - f1) / (t2 - t1)
                elif self._copy and self._src_fps and o2 >= o1:
                    # stream copy: ffmpeg doesn't count frames, so scale the camera FPS
                    # by how fast video time advances vs. wall time
                    self.snap["fps"] = min(self._src_fps, self._src_fps * (o2 - o1) / (t2 - t1))

    def _check_disk(self):
        try:
            free = shutil.disk_usage(self.cfg["outdir"]).free
        except OSError:
            return
        if free < DISK_STOP:
            self._log("error", f"Disk almost full ({human_size(free)} left) – stopping to protect the system")
            self._stop.set()
        elif free < DISK_WARN and not getattr(self, "_disk_warned", False):
            self._disk_warned = True
            self._log("warn", f"Low disk space: {human_size(free)} left")

    def _read_err(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            m = self.PROG_RE.match(line)
            if m:
                k, v = m.groups()
                # copy-only runs print no frame=, so out_time_us is the liveness signal
                if k in ("frame", "out_time_us"):
                    if self._progress.get(k) != v:
                        self._progress[k] = v
                        try:
                            moving = int(v) > 0
                        except ValueError:
                            moving = False
                        if moving:
                            self._got_frame = True
                            self._last_alive = time.time()
                continue
            lm = self.LVL_RE.search(line)
            level = lm.group(1) if lm else "info"
            msg = (line[:lm.start()] + line[lm.end():]) if lm else line
            if level == "info":
                fm = re.search(r"Opening '(.+)' for writing", msg)
                if fm and not fm.group(1).startswith("pipe:"):
                    self._add_file(fm.group(1))
                continue
            if level in ("warning", "error", "fatal", "panic"):
                clean = scrub(re.sub(r"\[[^\]]* @ 0x[0-9a-f]+\]\s*", "", msg).strip())
                if self.GLITCH_RE.search(clean):
                    self._glitch()
                    continue
                self._errtail.append((level, clean))
                key = re.sub(r"\d+", "#", clean)
                t = time.time()
                if t - self._seen_msgs.get(key, 0) > 60:   # show each kind at most once a minute
                    self._seen_msgs[key] = t
                    self._log("warn" if level == "warning" else "error", "ffmpeg: " + clean[:200])

    def _glitch(self):
        t = time.time()
        if t - self._proc_t0 < self.GLITCH_GRACE_S:
            return
        g = self._glitches
        g.append(t)
        while g and t - g[0] > 60:
            g.popleft()
        if len(g) >= self.GLITCH_WARN_N and t - self._seen_msgs.get("_glitch", 0) > 60:
            self._seen_msgs["_glitch"] = t
            self._log("warn", f"Stream is losing data ({len(g)} broken frames in the last minute) – "
                              "check the cable / network; use TCP")

    def _read_frames(self, proc, w, h):
        n = w * h * 3
        f = proc.stdout
        while True:
            buf = f.read(n)
            if not buf or len(buf) < n:
                break
            if self.preview_wanted and not self.frame_pending:
                self.frame_pending = True
                self.frame.emit(QImage(buf, w, h, 3 * w, QImage.Format_RGB888).copy())


# --------------------------------------------------------------------------- UI
STYLE = """
QWidget { background:#15181d; color:#e6e8eb; font-size:13px; }
QGroupBox { border:1px solid #2a2f37; border-radius:8px; margin-top:14px; padding:10px 10px 8px 10px; }
QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#8b949e; font-weight:600; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
    background:#0f1216; border:1px solid #2f353e; border-radius:6px; padding:5px 7px; }
QLineEdit:focus, QComboBox:focus { border:1px solid #3b82f6; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color:#7d8794; }
QPushButton, QToolButton { background:#252b33; border:1px solid #333a44; border-radius:6px; padding:6px 12px; }
QPushButton:hover, QToolButton:hover { background:#2e353f; }
QPushButton:disabled, QToolButton:disabled { color:#6b7480; }
QPushButton#start { background:#d93636; border:none; color:white; font-weight:700; padding:10px; font-size:14px; }
QPushButton#start:disabled { background:#5a2a2a; color:#a88; }
QPushButton#stop  { background:#2b3038; font-weight:700; padding:10px; font-size:14px; }
QLabel#title { font-size:17px; font-weight:700; }
QLabel#pill { border-radius:10px; padding:3px 10px; font-weight:600; }
QLabel#statKey { color:#8b949e; font-size:11px; }
QLabel#statVal { font-size:15px; font-weight:600; }
QFrame#stats { background:#1b1f25; border:1px solid #2a2f37; border-radius:8px; }
QFrame#stats QLabel { background:transparent; }
QLabel#hint { color:#7d8794; font-size:11px; }
QGroupBox QLabel, QGroupBox QCheckBox { background:transparent; }
"""

STATES = {"idle": ("#2b3038", "#9aa4b1", "IDLE", "Idle"),
          "connecting": ("#16304f", "#7cb4ff", "CONNECTING", "Connecting"),
          "rec": ("#4a1717", "#ff6b6b", "● RECORDING", "Recording"),
          "retry": ("#4a3a12", "#f5c04a", "RECONNECTING", "Reconnecting")}

FPS_ITEMS = ["30", "25", "20", "15", "10", "5", "Camera's own"]
SPLIT_ITEMS = [("Off (one file)", 0), ("Every 5 min", 300), ("Every 10 min", 600),
               ("Every 30 min", 1800), ("Every 60 min", 3600)]


class Preview(QLabel):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.img = None
        self.state = "idle"
        self.elapsed = "00:00:00"
        self.info = ""
        self.show_frames = True

    def clear(self):
        self.img = None
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#0b0d10"))
        if self.img is not None and self.show_frames:
            iw, ih = self.img.width(), self.img.height()
            s = min(w / iw, h / ih)
            r = QRectF((w - iw * s) / 2, (h - ih * s) / 2, iw * s, ih * s)
            p.drawImage(r, self.img)
        else:
            vw = min(w, h * 16 / 9); vh = vw * 9 / 16
            r = QRectF((w - vw) / 2, (h - vh) / 2, vw, vh)
            p.fillRect(r, QColor("#101317"))
            p.setPen(QColor("#5b636e")); p.setFont(QFont("Sans Serif", 13))
            msg = {"idle": "No signal\nPick a source and press  Start recording",
                   "connecting": "Connecting…", "retry": "Stream lost – reconnecting…",
                   "rec": "Recording\n(live preview is off)"}.get(self.state, "")
            p.drawText(r, Qt.AlignCenter, msg)
        if self.state in ("rec", "retry"):
            badge = QColor("#ff3b3b") if self.state == "rec" else QColor("#f5c04a")
            p.setBrush(QColor(0, 0, 0, 150)); p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(r.left() + 12, r.top() + 12, 150, 30), 6, 6)
            p.setBrush(badge); p.drawEllipse(QRectF(r.left() + 22, r.top() + 21, 12, 12))
            p.setPen(QColor("white")); p.setFont(QFont("", 12, QFont.Bold))
            p.drawText(QRectF(r.left() + 42, r.top() + 12, 120, 30), Qt.AlignVCenter, f"REC  {self.elapsed}")
            if self.info:
                p.setFont(QFont("", 10))
                tw = 520
                p.setPen(Qt.NoPen); p.setBrush(QColor(0, 0, 0, 110))
                fm = p.fontMetrics()
                bw = min(tw, fm.horizontalAdvance(self.info) + 16)
                p.drawRoundedRect(QRectF(r.right() - bw - 8, r.bottom() - 32, bw, 24), 5, 5)
                p.setPen(QColor(225, 225, 225))
                p.drawText(QRectF(r.right() - bw - 8, r.bottom() - 32, bw, 24), Qt.AlignCenter, self.info)


def stat(key, val):
    box = QVBoxLayout(); box.setSpacing(1)
    k = QLabel(key); k.setObjectName("statKey")
    v = QLabel(val); v.setObjectName("statVal")
    box.addWidget(k); box.addWidget(v)
    return box, v


def log_path():
    d = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "streamrec")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "streamrec.log")
    try:
        if os.path.getsize(p) > 5 * 1024**2:
            os.replace(p, p + ".1")
    except OSError:
        pass
    return p


class MainWindow(QMainWindow):
    test_done = Signal()

    def __init__(self):
        super().__init__()
        self.test_done.connect(self._test_done)
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 800)
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.rec = Recorder()
        self.usb_formats = {}
        self.state = "idle"
        try:
            self.logfile = open(log_path(), "a", buffering=1)
        except OSError:
            self.logfile = None

        root = QWidget(); self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(14, 10, 14, 12)

        head = QHBoxLayout()
        t = QLabel("StreamRec  ·  RTSP / USB video recorder"); t.setObjectName("title")
        self.pill = QLabel("IDLE"); self.pill.setObjectName("pill")
        head.addWidget(t); head.addStretch(); head.addWidget(self.pill)
        outer.addLayout(head)

        body = QHBoxLayout(); body.setSpacing(14); outer.addLayout(body, 1)
        left = QVBoxLayout(); left.setSpacing(4)
        lw = QWidget(); lw.setLayout(left); lw.setFixedWidth(370)
        body.addWidget(lw)

        # ---- 1 · Source
        self.src_box = src = QGroupBox("1 · Source"); sf = QFormLayout(src)
        self.src_type = QComboBox()
        self.src_type.addItems(["RTSP / IP camera (network)", "USB / laptop camera (plugged in)"])
        sf.addRow("Type", self.src_type)
        self.camera = QComboBox(); self.camera.setEditable(True); self.camera.setInsertPolicy(QComboBox.NoInsert)
        self.camera.setToolTip("Used in the file name. Pick one or type a new name.")
        sf.addRow("Camera", self.camera)

        rp = QWidget(); rf = QFormLayout(rp); rf.setContentsMargins(0, 0, 0, 0)
        self.url = QLineEdit(); self.url.setPlaceholderText("rtsp://user:pass@192.168.1.64:554/stream1")
        self.transport = QComboBox(); self.transport.addItems(["TCP (stable, recommended)", "UDP (lower latency)"])
        rbtn = QHBoxLayout(); self.test_btn = QPushButton("Test connection"); rbtn.addWidget(self.test_btn); rbtn.addStretch()
        rf.addRow("RTSP URL", self.url); rf.addRow("Transport", self.transport); rf.addRow("", rbtn)

        up = QWidget(); uf = QFormLayout(up); uf.setContentsMargins(0, 0, 0, 0)
        drow = QHBoxLayout()
        self.device = QComboBox()
        self.rescan = QToolButton(); self.rescan.setText("⟳"); self.rescan.setToolTip("Rescan devices")
        drow.addWidget(self.device, 1); drow.addWidget(self.rescan)
        self.res = QComboBox()
        uf.addRow("Device", drow); uf.addRow("Resolution", self.res)
        self.pages = [rp, up]
        sf.addRow(rp); sf.addRow(up)
        self.fps = QComboBox(); self.fps.addItems(FPS_ITEMS)
        sf.addRow("FPS", self.fps)
        left.addWidget(src)

        # ---- 2 · Recording
        self.rec_box = recb = QGroupBox("2 · Recording"); rf2 = QFormLayout(recb)
        orow = QHBoxLayout(); self.out = QLineEdit("~/Videos"); self.browse = QPushButton("Browse…")
        orow.addWidget(self.out, 1); orow.addWidget(self.browse)
        self.subdirs = QCheckBox("Folder per camera and day"); self.subdirs.setChecked(True)
        self.container = QComboBox(); self.container.addItems(["MKV (safe if power cuts)", "MP4 (plays everywhere)"])
        self.mode = QComboBox(); self.mode.addItems(["Auto (recommended)", "Original - no re-encode", "Re-encode H.264"])
        self.split = QComboBox(); self.split.addItems([s for s, _ in SPLIT_ITEMS]); self.split.setCurrentIndex(2)
        self.name_preview = QLabel(); self.name_preview.setObjectName("hint"); self.name_preview.setWordWrap(True)
        self.mode_hint = QLabel(); self.mode_hint.setObjectName("hint"); self.mode_hint.setWordWrap(True)
        rf2.addRow("Save to", orow); rf2.addRow("", self.subdirs)
        rf2.addRow("Format", self.container); rf2.addRow("Quality", self.mode); rf2.addRow("", self.mode_hint)
        rf2.addRow("Split", self.split); rf2.addRow("File", self.name_preview)
        left.addWidget(recb)

        # ---- 3 · Connection
        self.con_box = con = QGroupBox("3 · Connection"); cf = QFormLayout(con)
        self.reconnect = QCheckBox("Auto-reconnect if stream drops"); self.reconnect.setChecked(True)
        self.retry = QSpinBox(); self.retry.setRange(1, 120); self.retry.setValue(5); self.retry.setSuffix(" s")
        self.preview_on = QCheckBox("Show live preview"); self.preview_on.setChecked(True)
        cf.addRow(self.reconnect); cf.addRow("Retry every", self.retry); cf.addRow(self.preview_on)
        left.addWidget(con)

        left.addStretch()
        brow = QHBoxLayout()
        self.start = QPushButton("●  Start recording"); self.start.setObjectName("start")
        self.stop = QPushButton("■  Stop"); self.stop.setObjectName("stop")
        brow.addWidget(self.start, 2); brow.addWidget(self.stop, 1)
        left.addLayout(brow)

        # ---- right side
        right = QVBoxLayout(); right.setSpacing(10); body.addLayout(right, 1)
        self.preview = Preview(); right.addWidget(self.preview, 1)
        sfr = QFrame(); sfr.setObjectName("stats"); sl = QHBoxLayout(sfr); sl.setContentsMargins(14, 8, 14, 8)
        self.vals = {}
        for k, v in [("STATUS", "Idle"), ("DURATION", "00:00:00"), ("FPS", "–"),
                     ("BITRATE", "–"), ("FILE SIZE", "–"), ("RECONNECTS", "0")]:
            b, lab = stat(k, v); sl.addLayout(b); self.vals[k] = lab
            sl.addStretch()
        right.addWidget(sfr)
        frow = QHBoxLayout(); fk = QLabel("Current file:"); fk.setObjectName("statKey")
        self.curfile = QLabel("–"); self.curfile.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.open_btn = QPushButton("Open folder")
        frow.addWidget(fk); frow.addWidget(self.curfile, 1); frow.addWidget(self.open_btn)
        right.addLayout(frow)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setFixedHeight(130)
        self.log.setMaximumBlockCount(2000)
        self.log.setFont(QFont("Monospace", 9))
        right.addWidget(self.log)

        # ---- signals
        self.src_type.currentIndexChanged.connect(self.on_source_change)
        self.device.currentIndexChanged.connect(self.on_device_change)
        self.rescan.clicked.connect(lambda: self.scan_devices(log=True))
        self.test_btn.clicked.connect(self.on_test)
        self.browse.clicked.connect(self.on_browse)
        self.open_btn.clicked.connect(self.on_open_folder)
        self.start.clicked.connect(self.on_start)
        self.stop.clicked.connect(self.on_stop)
        self.preview_on.toggled.connect(self.on_preview_toggle)
        for w in (self.camera, self.container, self.mode, self.split, self.fps):
            w.currentIndexChanged.connect(self.update_hints)
        self.camera.editTextChanged.connect(self.update_hints)
        self.subdirs.toggled.connect(self.update_hints)
        self.out.textChanged.connect(self.update_hints)
        self.rec.log.connect(self.add_log)
        self.rec.state.connect(self.set_state)
        self.rec.frame.connect(self.on_frame)
        self.rec.info.connect(self.on_info)
        self.rec.finished.connect(self.on_finished)

        self.ticker = QTimer(self); self.ticker.timeout.connect(self.tick); self.ticker.start(500)

        self.load_settings()
        self.on_source_change(self.src_type.currentIndex())
        self.set_state("idle")
        self.check_ffmpeg()

    # ---------------------------------------------------------------- settings
    def load_settings(self):
        s = self.settings
        cams = s.value("cameras", "")
        try:
            cams = json.loads(cams) if cams else ["zt6", "uv40"]
        except ValueError:
            cams = ["zt6", "uv40"]
        self.camera.addItems(cams)
        self.camera.setCurrentText(s.value("camera", cams[0] if cams else "cam"))
        self.url.setText(s.value("url", ""))
        self.transport.setCurrentIndex(int(s.value("transport", 0)))
        self.out.setText(s.value("outdir", "~/Videos"))
        self.subdirs.setChecked(s.value("subdirs", "true") in (True, "true"))
        self.container.setCurrentIndex(int(s.value("container", 0)))
        self.mode.setCurrentIndex(int(s.value("mode", 0)))
        self.split.setCurrentIndex(int(s.value("split", 2)))
        fps = s.value("fps", "30")
        self.fps.setCurrentIndex(max(0, self.fps.findText(fps)))
        self.reconnect.setChecked(s.value("reconnect", "true") in (True, "true"))
        self.retry.setValue(int(s.value("retry", 5)))
        self.preview_on.setChecked(s.value("preview", "true") in (True, "true"))
        self._saved_device = s.value("device", "")
        self._saved_res = s.value("resolution", "")
        self.src_type.setCurrentIndex(int(s.value("source", 0)))
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def save_settings(self):
        s = self.settings
        cams = [self.camera.itemText(i) for i in range(self.camera.count())]
        s.setValue("cameras", json.dumps(cams))
        s.setValue("camera", self.camera.currentText())
        s.setValue("source", self.src_type.currentIndex())
        s.setValue("url", self.url.text().strip())
        s.setValue("transport", self.transport.currentIndex())
        s.setValue("device", self.device.currentData() or "")
        s.setValue("resolution", self.res.currentData() or "")
        s.setValue("fps", self.fps.currentText())
        s.setValue("outdir", self.out.text().strip())
        s.setValue("subdirs", self.subdirs.isChecked())
        s.setValue("container", self.container.currentIndex())
        s.setValue("mode", self.mode.currentIndex())
        s.setValue("split", self.split.currentIndex())
        s.setValue("reconnect", self.reconnect.isChecked())
        s.setValue("retry", self.retry.value())
        s.setValue("preview", self.preview_on.isChecked())
        s.setValue("geometry", self.saveGeometry())

    # ---------------------------------------------------------------- helpers
    def check_ffmpeg(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.add_log("error", "FFmpeg not found. Install it:  sudo apt install ffmpeg")
            QTimer.singleShot(200, lambda: QMessageBox.critical(
                self, APP_NAME, "FFmpeg is not installed.\n\nRun install.sh again, or:\n  sudo apt install ffmpeg"))
        else:
            self.add_log("info", f"StreamRec {APP_VERSION} · FFmpeg {ffmpeg_major()}.x ready")

    def add_log(self, level, msg):
        line = f"{now_s()}  {msg}"
        if level == "debug":            # technical details: log file only
            if self.logfile:
                try:
                    self.logfile.write(f"{dt.datetime.now():%Y-%m-%d} {line}\n")
                except OSError:
                    pass
            return
        color = {"error": "#ff7b72", "warn": "#f5c04a", "ok": "#7ee787"}.get(level)
        esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.log.appendHtml(f'<span style="color:{color}">{esc}</span>' if color else esc)
        if self.logfile:
            try:
                self.logfile.write(f"{dt.datetime.now():%Y-%m-%d} {line}\n")
            except OSError:
                pass

    def outdir(self):
        return os.path.abspath(os.path.expanduser(self.out.text().strip() or "~/Videos"))

    def cam_name(self):
        return safe_name(self.camera.currentText())

    def is_usb(self):
        return self.src_type.currentIndex() == 1

    # ---------------------------------------------------------------- source
    def on_source_change(self, i):
        for j, page in enumerate(self.pages):
            page.setVisible(j == i)
        item = self.mode.model().item(1)          # "Original" is RTSP only
        item.setEnabled(i == 0)
        if i == 1 and self.mode.currentIndex() == 1:
            self.mode.setCurrentIndex(0)
        if i == 1 and self.device.count() == 0:
            self.scan_devices()
        self.update_hints()

    def scan_devices(self, log=False):
        if self.rec.is_running():
            return
        prev = self.device.currentData() or getattr(self, "_saved_device", "")
        self.device.blockSignals(True)
        self.device.clear()
        devs = list_v4l2_devices()
        for path, name in devs:
            self.device.addItem(f"{path} · {name}", path)
        if not devs:
            self.device.addItem("No camera found – plug in and press ⟳", None)
        idx = self.device.findData(prev)
        self.device.setCurrentIndex(idx if idx >= 0 else 0)
        self.device.blockSignals(False)
        if log:
            self.add_log("info", f"Found {len(devs)} camera(s)" + (": " + ", ".join(p for p, _ in devs) if devs else ""))
        self.on_device_change()

    def on_device_change(self, *_):
        dev = self.device.currentData()
        prev = self.res.currentData() or getattr(self, "_saved_res", "")
        self.res.clear()
        self.res.addItem("Auto (camera default)", None)
        if dev:
            fm = v4l2_formats(dev)
            self.usb_formats[dev] = fm
            sizes = sorted(fm["raw"] | fm["mjpeg"], key=lambda s: (s[0] * s[1], s[0]), reverse=True)
            for w, h in sizes:
                tag = "" if (w, h) in fm["raw"] else "  (MJPEG)"
                self.res.addItem(f"{w}x{h}{tag}", f"{w}x{h}")
        idx = self.res.findData(prev) if prev else 0
        self.res.setCurrentIndex(max(0, idx))

    def update_hints(self, *_):
        cam = self.cam_name()
        ext = "mkv" if self.container.currentIndex() == 0 else "mp4"
        today = dt.date.today().isoformat()
        sub = f"{cam}/{today}/" if self.subdirs.isChecked() else ""
        self.name_preview.setText(f"{sub}{cam}_{today}_{dt.datetime.now():%H-%M-%S}.{ext}")
        usb = self.is_usb()
        if self.mode.currentIndex() == 0:
            self.mode_hint.setText("USB: compresses to H.264 at the FPS you chose." if usb else
                                   "RTSP: keeps camera video as-is if it already sends this FPS, "
                                   "otherwise converts to it.")
        elif self.mode.currentIndex() == 1:
            self.mode_hint.setText("Exact camera video, lowest CPU. FPS = camera's own. RTSP only.")
        else:
            self.mode_hint.setText("Always converts. Smaller files, more CPU.")

    # ---------------------------------------------------------------- actions
    def build_cfg(self):
        usb = self.is_usb()
        fps_t = self.fps.currentText()
        cfg = {
            "source": "usb" if usb else "rtsp",
            "url": self.url.text().strip(),
            "transport": "udp" if self.transport.currentIndex() == 1 else "tcp",
            "device": self.device.currentData(),
            "resolution": self.res.currentData() if usb else None,
            "input_format": None,
            "fps": int(fps_t) if fps_t.isdigit() else None,
            "cam": self.cam_name(),
            "outdir": self.outdir(),
            "subdirs": self.subdirs.isChecked(),
            "ext": "mkv" if self.container.currentIndex() == 0 else "mp4",
            "mode": ["auto", "copy", "encode"][self.mode.currentIndex()],
            "split": SPLIT_ITEMS[self.split.currentIndex()][1],
            "reconnect": self.reconnect.isChecked(),
            "retry": self.retry.value(),
            "preview": self.preview_on.isChecked(),
        }
        if usb:
            if not cfg["device"]:
                QMessageBox.warning(self, APP_NAME, "No USB camera selected. Plug it in and press ⟳.")
                return None
            fm = self.usb_formats.get(cfg["device"], {"raw": set(), "mjpeg": set()})
            if cfg["resolution"]:
                wh = tuple(map(int, cfg["resolution"].split("x")))
                if wh in fm["mjpeg"] and (wh not in fm["raw"] or wh[0] * wh[1] >= 1280 * 720):
                    cfg["input_format"] = "mjpeg"   # big raw frames are slow over USB
        else:
            if "://" not in cfg["url"]:
                QMessageBox.warning(self, APP_NAME, "Enter the camera's RTSP URL, e.g.\n"
                                    "rtsp://user:pass@192.168.1.64:554/stream1")
                return None
        try:
            os.makedirs(cfg["outdir"], exist_ok=True)
            if not os.access(cfg["outdir"], os.W_OK):
                raise OSError("not writable")
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Can't write to {cfg['outdir']}:\n{e}")
            return None
        return cfg

    def on_start(self):
        if self.rec.is_running():
            return
        cfg = self.build_cfg()
        if not cfg:
            return
        name = self.camera.currentText().strip()
        if name and self.camera.findText(name) < 0:
            self.camera.addItem(name)
        self.save_settings()
        self.add_log("info", "─" * 40)
        self.preview.clear()
        self.preview.info = ""
        self.rec.preview_wanted = self.preview_on.isChecked()
        self.set_controls(False)
        self.rec.start(cfg)

    def on_stop(self):
        if self.rec.is_running():
            self.add_log("info", "Stopping – finishing the file…")
            self.stop.setEnabled(False)
            self.rec.stop()

    def on_finished(self):
        self.set_controls(True)
        self.preview.clear()

    def set_controls(self, enabled):
        for w in (self.src_box, self.rec_box, self.reconnect, self.retry):
            w.setEnabled(enabled)
        self.start.setEnabled(enabled)
        self.stop.setEnabled(not enabled)

    def on_preview_toggle(self, on):
        self.rec.preview_wanted = on
        self.preview.show_frames = on
        self.preview.update()

    def on_frame(self, img):
        self.rec.frame_pending = False
        self.preview.img = img
        self.preview.update()

    def on_info(self, text):
        self.preview.info = text

    def on_test(self):
        url = self.url.text().strip()
        if "://" not in url:
            self.add_log("error", "Enter an RTSP URL first")
            return
        tr = "udp" if self.transport.currentIndex() == 1 else "tcp"
        self.test_btn.setEnabled(False); self.test_btn.setText("Testing…")
        self.add_log("info", f"Testing {mask_url(url)} ({tr.upper()})…")

        def work():
            try:
                m = probe(rtsp_input_opts(tr) + ["-i", url])
                fps = f"{m['fps']:g} fps" if m["fps"] else "? fps"
                self.rec.log.emit("ok", f"OK · {m['codec'].upper()} {m['width']}x{m['height']} @ {fps}")
            except Exception as e:
                self.rec.log.emit("error", f"Failed · {e}")
            self.test_done.emit()
        threading.Thread(target=work, daemon=True).start()

    def _test_done(self):
        self.test_btn.setEnabled(True); self.test_btn.setText("Test connection")

    def on_browse(self):
        d = QFileDialog.getExistingDirectory(self, "Save recordings to", self.outdir())
        if d:
            home = os.path.expanduser("~")
            self.out.setText("~" + d[len(home):] if d.startswith(home + os.sep) else d)

    def on_open_folder(self):
        f = self.rec.snap.get("file")
        d = os.path.dirname(f) if f else self.outdir()
        if not f and self.subdirs.isChecked():
            cand = os.path.join(d, self.cam_name(), dt.date.today().isoformat())
            d = cand if os.path.isdir(cand) else (os.path.join(d, self.cam_name())
                                                  if os.path.isdir(os.path.join(d, self.cam_name())) else d)
        os.makedirs(d, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    # ---------------------------------------------------------------- state
    def set_state(self, s):
        self.state = s
        bg, fg, pill, status = STATES[s]
        self.pill.setText(pill); self.pill.setStyleSheet(f"background:{bg}; color:{fg};")
        self.vals["STATUS"].setText(status)
        self.vals["STATUS"].setStyleSheet("" if s == "idle" else f"color:{fg};")
        self.preview.state = s
        if s in ("connecting", "retry"):
            self.preview.img = None
        self.preview.update()

    def tick(self):
        sn = self.rec.snap
        if self.rec.is_running() and sn.get("start"):
            el = fmt_dur(time.time() - sn["start"])
            self.vals["DURATION"].setText(el)
            self.preview.elapsed = el
            self.vals["FPS"].setText(f"{sn['fps']:.1f}" if sn.get("fps") and self.state == "rec" else "–")
            br = sn.get("bitrate")
            self.vals["BITRATE"].setText(f"{br / 1e6:.1f} Mb/s" if br else "–")
            self.vals["FILE SIZE"].setText(human_size(sn.get("file_size")) if sn.get("file") else "–")
            self.vals["FILE SIZE"].setToolTip(f"All files this session: {human_size(sn.get('total', 0))}")
            self.vals["RECONNECTS"].setText(str(sn.get("reconnects", 0)))
            f = sn.get("file")
            if f:
                home = os.path.expanduser("~")
                self.curfile.setText("~" + f[len(home):] if f.startswith(home) else f)
                self.curfile.setToolTip(f)
            if self.state == "rec":
                self.preview.update()
        elif not self.rec.is_running():
            for k in ("FPS", "BITRATE"):
                self.vals[k].setText("–")
            if self.state == "idle":
                self.update_hints()

    def closeEvent(self, e):
        if self.rec.is_running():
            r = QMessageBox.question(self, APP_NAME, "Recording is running.\nStop recording and quit?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.add_log("info", "Stopping before exit…")
            self.rec.stop()
            t0 = time.time()
            while self.rec.is_running() and time.time() - t0 < 15:
                QApplication.processEvents()
                time.sleep(0.05)
        self.save_settings()
        if self.logfile:
            self.logfile.close()
        e.accept()


# --------------------------------------------------------------------------- main
def self_check():
    print(f"StreamRec {APP_VERSION}")
    print(f"python   : {sys.version.split()[0]}")
    try:
        import PySide6
        print(f"PySide6  : {PySide6.__version__}")
    except Exception as e:
        print(f"PySide6  : MISSING ({e})")
    print(f"ffmpeg   : {shutil.which('ffmpeg') or 'MISSING'} (major {ffmpeg_major()})")
    print(f"ffprobe  : {shutil.which('ffprobe') or 'MISSING'}")
    print(f"v4l2-ctl : {shutil.which('v4l2-ctl') or 'missing (optional)'}")
    devs = list_v4l2_devices()
    print(f"cameras  : {', '.join(f'{p} ({n})' for p, n in devs) or 'none'}")
    try:
        import grp
        groups = [grp.getgrgid(g).gr_name for g in os.getgroups()]
        print(f"video grp: {'yes' if 'video' in groups else 'NO - log out/in after install'}")
    except Exception:
        pass


def main():
    if "--version" in sys.argv:
        print(APP_VERSION); return
    if "--check" in sys.argv:
        self_check(); return
    QApplication.setApplicationName(APP_NAME)
    QApplication.setDesktopFileName("streamrec")
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamrec.svg")
    if os.path.exists(icon):
        app.setWindowIcon(QIcon(icon))
    w = MainWindow()
    # stop cleanly on Ctrl+C / logout (SIGTERM)
    def on_signal(*_):
        w.rec.stop(); w.rec.wait(15); app.quit()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    keepalive = QTimer(); keepalive.start(300); keepalive.timeout.connect(lambda: None)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

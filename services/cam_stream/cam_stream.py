#!/usr/bin/env python3
"""
Minimal MJPEG HTTP streamer for mmclaw demo.

- Auto-detects /dev/videoN (0..9), opens the first one that yields a frame.
- Serves a multipart/x-mixed-replace MJPEG stream at GET /stream.
- Health endpoint at GET / and GET /health.
- Listens on 127.0.0.1:8080 by default (override via env CAM_HOST/CAM_PORT).
- Stdlib only + opencv-python (cv2).

Capture mode (NEW):
- When CAPTURE_ENABLED=1 (default), every CAPTURE_INTERVAL_SECONDS the most
  recent published frame is also written to CAPTURE_DIR as a JPEG file with
  ISO timestamp + per-day sequence (cam_2026-04-25T17-35-00_001.jpg).
- Old captures beyond CAPTURE_RETENTION_COUNT (default 100) are pruned by
  mtime so the disk does not fill up.
- The MJPEG stream is unaffected; capture is a parallel side-effect on the
  same FrameBroker.

Usage:
  python3 cam_stream.py
"""
from __future__ import annotations

import logging
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

try:
    import cv2  # type: ignore
except Exception as e:  # pragma: no cover
    print("FATAL: opencv-python (cv2) is required:", e, file=sys.stderr)
    sys.exit(2)

LOG = logging.getLogger("cam_stream")
HOST = os.environ.get("CAM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CAM_PORT", "8080"))
WIDTH = int(os.environ.get("CAM_WIDTH", "640"))
HEIGHT = int(os.environ.get("CAM_HEIGHT", "480"))
FPS = float(os.environ.get("CAM_FPS", "15"))
JPEG_QUALITY = int(os.environ.get("CAM_JPEG_QUALITY", "70"))
DEVICE_OVERRIDE = os.environ.get("CAM_DEVICE")  # e.g. "/dev/video0" or "0"

# ------- capture mode config -------
def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


CAPTURE_ENABLED = _env_bool("CAPTURE_ENABLED", True)
CAPTURE_INTERVAL_SECONDS = float(os.environ.get("CAPTURE_INTERVAL_SECONDS", "5"))
CAPTURE_RETENTION_COUNT = int(os.environ.get("CAPTURE_RETENTION_COUNT", "100"))
CAPTURE_DIR = Path(
    os.environ.get("CAPTURE_DIR", "/root/mmclaw/services/captures")
).resolve()
# Quality of saved jpgs (independent of streaming jpeg quality).
CAPTURE_JPEG_QUALITY = int(os.environ.get("CAPTURE_JPEG_QUALITY", "85"))

BOUNDARY = "mmclawframe"
CAPTURE_FILENAME_RE = re.compile(r"^cam_.*\.jpg$")


def _open_capture(path: str) -> Optional["cv2.VideoCapture"]:
    """Try to open a camera device and grab one frame to confirm it works."""
    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)
    # warm up: some UVC cameras return a black frame the first time
    for _ in range(3):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size > 0:
            return cap
        time.sleep(0.05)
    cap.release()
    return None


def find_camera() -> Optional["cv2.VideoCapture"]:
    candidates = []
    if DEVICE_OVERRIDE:
        candidates.append(DEVICE_OVERRIDE)
    else:
        for i in range(0, 10):
            candidates.append(f"/dev/video{i}")
    for path in candidates:
        LOG.info("trying camera %s", path)
        cap = _open_capture(path)
        if cap is not None:
            LOG.info("opened camera %s @ %dx%d", path, WIDTH, HEIGHT)
            return cap
    return None


class FrameBroker:
    """Single-producer multi-consumer: holds the most recent frame.

    For streaming we cache the JPEG bytes (encoded at JPEG_QUALITY).
    For capture we keep the raw BGR frame so we can re-encode at a different
    JPEG quality (typically higher, 85) without paying CPU on the hot stream
    path. capture_loop only re-encodes once per CAPTURE_INTERVAL_SECONDS.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._frame: Optional[bytes] = None
        self._raw = None  # most recent BGR ndarray, or None
        self._seq: int = 0
        self._alive = True

    def publish(self, jpeg: bytes, raw=None) -> None:
        with self._cv:
            self._frame = jpeg
            if raw is not None:
                self._raw = raw
            self._seq += 1
            self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            self._alive = False
            self._cv.notify_all()

    def latest_raw(self):
        with self._cv:
            return self._raw, self._seq, self._alive

    def wait_for_next(self, last_seq: int, timeout: float = 5.0):
        """Block until a new frame is available. Returns (frame, seq, alive)."""
        with self._cv:
            if not self._alive:
                return None, last_seq, False
            if self._seq == last_seq:
                self._cv.wait(timeout=timeout)
            return self._frame, self._seq, self._alive


def capture_loop(cap: "cv2.VideoCapture", broker: FrameBroker) -> None:
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
    miss = 0
    period = 1.0 / max(FPS, 1.0)
    next_t = time.monotonic()
    while broker._alive:
        ok, frame = cap.read()
        if not ok or frame is None:
            miss += 1
            LOG.warning("camera read failed (%d)", miss)
            if miss > 30:
                LOG.error("camera lost; exiting capture loop")
                break
            time.sleep(0.1)
            continue
        miss = 0
        ok, buf = cv2.imencode(".jpg", frame, encode_params)
        if not ok:
            continue
        # publish JPEG for streaming + raw for periodic disk capture
        broker.publish(buf.tobytes(), raw=frame)
        # simple FPS pacing
        next_t += period
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_t = time.monotonic()
    broker.stop()
    cap.release()


# -------- Persistent capture (write frames to disk) --------

def _iso_compact(ts: float) -> str:
    """ISO-8601-ish timestamp safe for filenames: 2026-04-25T17-35-00."""
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%dT%H-%M-%S")


def _existing_captures(d: Path):
    if not d.exists():
        return []
    out = []
    for p in d.iterdir():
        if p.is_file() and CAPTURE_FILENAME_RE.match(p.name):
            try:
                out.append((p.stat().st_mtime, p))
            except OSError:
                continue
    out.sort(key=lambda x: x[0])
    return out


def _next_seq_for_date(d: Path, date_prefix: str) -> int:
    """Find next per-day sequence number based on existing files."""
    max_seq = 0
    pat = re.compile(rf"^cam_{re.escape(date_prefix)}T[0-9-]+_(\d+)\.jpg$")
    if not d.exists():
        return 1
    for p in d.iterdir():
        if not p.is_file():
            continue
        m = pat.match(p.name)
        if m:
            try:
                n = int(m.group(1))
                if n > max_seq:
                    max_seq = n
            except ValueError:
                pass
    return max_seq + 1


def _prune_old_captures(d: Path, retention: int) -> None:
    items = _existing_captures(d)
    overflow = len(items) - retention
    if overflow <= 0:
        return
    for _, p in items[:overflow]:
        try:
            p.unlink()
        except OSError as e:
            LOG.warning("failed to prune %s: %s", p, e)


def persistent_capture_loop(broker: FrameBroker) -> None:
    """Periodically dump the latest raw frame to CAPTURE_DIR as JPEG."""
    if not CAPTURE_ENABLED:
        LOG.info("persistent capture disabled (CAPTURE_ENABLED=0)")
        return
    try:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        LOG.error("cannot create capture dir %s: %s", CAPTURE_DIR, e)
        return
    LOG.info(
        "persistent capture: dir=%s interval=%.1fs retention=%d quality=%d",
        CAPTURE_DIR,
        CAPTURE_INTERVAL_SECONDS,
        CAPTURE_RETENTION_COUNT,
        CAPTURE_JPEG_QUALITY,
    )
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), CAPTURE_JPEG_QUALITY]
    last_seq = -1
    interval = max(0.5, CAPTURE_INTERVAL_SECONDS)
    next_t = time.monotonic()
    # initial prune (in case the previous run left a backlog).
    _prune_old_captures(CAPTURE_DIR, CAPTURE_RETENTION_COUNT)
    while broker._alive:
        # sleep in small slices so shutdown is responsive
        while broker._alive and time.monotonic() < next_t:
            time.sleep(min(0.5, max(0.0, next_t - time.monotonic())))
        if not broker._alive:
            break
        next_t = time.monotonic() + interval

        raw, seq, alive = broker.latest_raw()
        if not alive:
            break
        if raw is None or seq == last_seq:
            # no new frame since last capture -> skip this tick
            continue
        last_seq = seq

        ts = time.time()
        date_prefix = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        seq_no = _next_seq_for_date(CAPTURE_DIR, date_prefix)
        fname = f"cam_{_iso_compact(ts)}_{seq_no:03d}.jpg"
        target = CAPTURE_DIR / fname
        tmp = CAPTURE_DIR / f".{fname}.tmp"
        try:
            ok, buf = cv2.imencode(".jpg", raw, encode_params)
            if not ok:
                LOG.warning("imencode failed for capture")
                continue
            tmp.write_bytes(buf.tobytes())
            tmp.replace(target)
            LOG.info("captured %s (%d bytes)", target.name, target.stat().st_size)
        except OSError as e:
            LOG.warning("write capture %s failed: %s", target, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            continue
        # retention sweep after each successful write
        _prune_old_captures(CAPTURE_DIR, CAPTURE_RETENTION_COUNT)

    LOG.info("persistent capture loop exiting")


class StreamHandler(BaseHTTPRequestHandler):
    broker: FrameBroker = None  # injected via class attr

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send_simple(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/health"):
            alive = self.broker is not None and self.broker._alive
            body = (b"ok\n" if alive else b"degraded\n")
            self._send_simple(200 if alive else 503, body)
            return
        if self.path.startswith("/stream"):
            self._serve_stream()
            return
        self._send_simple(404, b"not found\n")

    def _serve_stream(self) -> None:
        if self.broker is None:
            self._send_simple(503, b"camera not ready\n")
            return
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        last_seq = -1
        try:
            while True:
                frame, seq, alive = self.broker.wait_for_next(last_seq, timeout=5.0)
                if not alive:
                    break
                if frame is None or seq == last_seq:
                    continue
                last_seq = seq
                header = (
                    f"--{BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode("ascii")
                self.wfile.write(header)
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:  # pragma: no cover
            LOG.warning("stream client dropped: %s", e)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cap = find_camera()
    if cap is None:
        LOG.error(
            "no camera device available; tried %s",
            DEVICE_OVERRIDE or "/dev/video0../dev/video9",
        )
        return 1

    broker = FrameBroker()
    StreamHandler.broker = broker

    cap_thread = threading.Thread(
        target=capture_loop, args=(cap, broker), daemon=True, name="cam-capture"
    )
    cap_thread.start()

    persist_thread = None
    if CAPTURE_ENABLED:
        persist_thread = threading.Thread(
            target=persistent_capture_loop,
            args=(broker,),
            daemon=True,
            name="cam-persist",
        )
        persist_thread.start()

    # ThreadingHTTPServer: each /stream client gets its own thread
    server = ThreadingHTTPServer((HOST, PORT), StreamHandler)
    # be polite under load; reuseaddr already on by default in py3
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    LOG.info("MJPEG server listening on http://%s:%d/stream", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutdown requested")
    finally:
        broker.stop()
        server.server_close()
        cap_thread.join(timeout=2.0)
        if persist_thread is not None:
            persist_thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

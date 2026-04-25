#!/usr/bin/env python3
"""
perception_input/watcher.py

Watches the cam_stream capture directory and, every N seconds:

  1. injects the latest JPEG into the chat_proxy /chat endpoint as a
     multimodal user message (image_url with base64 data URI), so the
     OpenClaw upstream model "sees" what the camera sees;

  2. (optional) calls DashScope multimodal-embedding-v1 on the same frame
     and writes the 1024-d vector + metadata to embeddings/cam_<ts>.json
     for later OpenClaw memory backfill.

Stdlib only (urllib + ssl + json + base64). No third-party deps so the
Atlas board doesn't need any extra pip install. Designed for "pet/home
demo" scale: one frame every 10-30s.

Why a watcher instead of an OpenClaw extension?
  Because the remote docker daemon is currently dead and the
  `mmclaw-dev` container can't be restarted, the OpenClaw runtime
  cannot be reached over its native channel API. The chat_proxy
  HTTP endpoint is reachable, so we use it as the injection seam.
  Once OpenClaw is back, swap PERCEPTION_CHAT_URL to a real openclaw
  channel ingest endpoint (or use the bundled extension under
  openclaw/extensions/pet-perception-input/).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("perception_input")

HERE = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("PERCEPTION_ENV_PATH", HERE.parent / ".env"))


def _load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_env(ENV_PATH)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


CAPTURES_DIR = Path(
    os.environ.get("PERCEPTION_CAPTURES_DIR", "/root/mmclaw/services/captures")
).resolve()
EMBEDDINGS_DIR = Path(
    os.environ.get("PERCEPTION_EMBEDDINGS_DIR", "/root/mmclaw/services/embeddings")
).resolve()
CHAT_URL = os.environ.get("PERCEPTION_CHAT_URL", "http://127.0.0.1:18790/chat")
INTERVAL = float(os.environ.get("PERCEPTION_INTERVAL_SECONDS", "15"))
INJECT_CHAT = _env_bool("PERCEPTION_INJECT_CHAT", True)
EMBED = _env_bool("PERCEPTION_EMBED", True)
PROMPT = os.environ.get("PERCEPTION_PROMPT", "[perception] new camera frame")
SESSION_ID = os.environ.get("PERCEPTION_SESSION_ID", "perception-cam")
HTTP_TIMEOUT = float(os.environ.get("PERCEPTION_HTTP_TIMEOUT", "30"))

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com"
).rstrip("/")
DASHSCOPE_MODEL = os.environ.get("DASHSCOPE_MM_MODEL", "multimodal-embedding-v1")
DASHSCOPE_PATH = (
    "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
)

CAPTURE_FNAME_RE = re.compile(r"^cam_.*\.jpg$")


# ---------- helpers ----------


def latest_capture(d: Path) -> Optional[Tuple[Path, float]]:
    if not d.exists():
        return None
    best: Optional[Tuple[Path, float]] = None
    for p in d.iterdir():
        if not p.is_file():
            continue
        if not CAPTURE_FNAME_RE.match(p.name):
            continue
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if best is None or mt > best[1]:
            best = (p, mt)
    return best


def b64_data_uri(jpg_bytes: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpg_bytes).decode("ascii")


# ---------- chat_proxy injection ----------


def inject_to_chat(jpg_bytes: bytes, fname: str) -> Tuple[bool, str]:
    """POST a multimodal user message into chat_proxy /chat (non-stream)."""
    payload: Dict[str, Any] = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"{PROMPT}: {fname}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": b64_data_uri(jpg_bytes)},
                    },
                ],
            }
        ],
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        CHAT_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
            try:
                j = json.loads(body.decode("utf-8"))
                # try to surface a short reply for log visibility
                content = ""
                ch = j.get("choices") or []
                if ch and isinstance(ch[0], dict):
                    msg = ch[0].get("message") or {}
                    content = msg.get("content") or ""
                return True, content[:200] if content else f"http {resp.status}"
            except Exception:
                return True, f"http {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTPError {e.code}: {e.read()[:200]!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------- DashScope multimodal embedding ----------


class EmbedError(Exception):
    pass


def dashscope_mm_embed(jpg_bytes: bytes) -> Dict[str, Any]:
    """Call DashScope multimodal-embedding-v1 with a base64 image part.

    Returns the parsed JSON response (caller can pull .output.embeddings[0]).
    """
    if not DASHSCOPE_API_KEY:
        raise EmbedError("DASHSCOPE_API_KEY missing")
    url = DASHSCOPE_BASE_URL + DASHSCOPE_PATH
    payload = {
        "model": DASHSCOPE_MODEL,
        "input": {
            "contents": [
                {
                    "image": "data:image/jpeg;base64,"
                    + base64.b64encode(jpg_bytes).decode("ascii")
                }
            ]
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise EmbedError(f"HTTP {e.code}: {e.read()[:300]!r}") from e
    except Exception as e:
        raise EmbedError(f"{type(e).__name__}: {e}") from e
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as e:
        raise EmbedError(f"bad json: {e}") from e


def write_embedding_record(
    out_dir: Path,
    fname: str,
    src: Path,
    resp: Dict[str, Any],
) -> Optional[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    embeddings = (resp.get("output") or {}).get("embeddings") or []
    if not embeddings:
        LOG.warning("dashscope returned no embeddings for %s", fname)
        return None
    vec = embeddings[0].get("embedding") or []
    record = {
        "schema": "mmclaw.perception_input.embedding/v1",
        "source": str(src),
        "filename": fname,
        "captured_at": datetime.fromtimestamp(src.stat().st_mtime).isoformat(),
        "embedded_at": datetime.utcnow().isoformat() + "Z",
        "model": DASHSCOPE_MODEL,
        "dim": len(vec),
        "embedding": vec,
        "usage": resp.get("usage") or {},
        "request_id": resp.get("request_id"),
    }
    base = src.stem  # cam_...
    out_path = out_dir / f"{base}.json"
    tmp = out_dir / f".{base}.json.tmp"
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)
    return out_path


# ---------- main loop ----------


_running = True


def _graceful(_sig, _frm):
    global _running
    _running = False
    LOG.info("signal received, exiting")


def main_loop() -> int:
    LOG.info(
        "perception_input watcher: captures=%s embeddings=%s chat=%s "
        "interval=%.1fs inject_chat=%s embed=%s",
        CAPTURES_DIR,
        EMBEDDINGS_DIR,
        CHAT_URL,
        INTERVAL,
        INJECT_CHAT,
        EMBED,
    )
    if EMBED and not DASHSCOPE_API_KEY:
        LOG.warning(
            "PERCEPTION_EMBED=1 but DASHSCOPE_API_KEY missing in env "
            "(%s); embedding will be skipped per-frame.",
            ENV_PATH,
        )
    last_seen: Optional[Path] = None
    next_t = time.monotonic()
    while _running:
        # responsive sleep
        while _running and time.monotonic() < next_t:
            time.sleep(min(0.5, max(0.0, next_t - time.monotonic())))
        if not _running:
            break
        next_t = time.monotonic() + max(1.0, INTERVAL)

        latest = latest_capture(CAPTURES_DIR)
        if latest is None:
            LOG.info("no captures yet in %s", CAPTURES_DIR)
            continue
        path, _mt = latest
        if last_seen is not None and path == last_seen:
            LOG.debug("no new frame since %s, skipping", path.name)
            continue
        try:
            jpg = path.read_bytes()
        except OSError as e:
            LOG.warning("read %s failed: %s", path, e)
            continue
        if not jpg:
            continue
        last_seen = path

        # 1. inject to chat_proxy
        if INJECT_CHAT:
            ok, info = inject_to_chat(jpg, path.name)
            LOG.info(
                "inject %s -> %s (%s)",
                path.name,
                "ok" if ok else "FAIL",
                info,
            )

        # 2. embed via DashScope (best-effort; never blocks the loop)
        if EMBED and DASHSCOPE_API_KEY:
            try:
                resp = dashscope_mm_embed(jpg)
                out_path = write_embedding_record(
                    EMBEDDINGS_DIR, path.name, path, resp
                )
                if out_path is not None:
                    LOG.info("embedded %s -> %s", path.name, out_path.name)
            except EmbedError as e:
                LOG.warning("embed %s failed: %s", path.name, e)

    LOG.info("watcher exiting cleanly")
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("PERCEPTION_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)
    try:
        return main_loop()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

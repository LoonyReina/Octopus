"""Side experiments to nail down whether video_url is silently ignored.

Three quick probes:
  A) plain text only, ask the model whether it can process video at all
  B) attach the same mp4 base64 but as type=image_url (some gateways
     accept arbitrary mime there); see if prompt_tokens jumps
  C) attach an mp4 with type=video_url but explicitly tell the model
     "if the video field is empty or unsupported, say UNSUPPORTED" so
     a routing-aware gateway can flag it
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load_env(p: Path) -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(p)
        return
    except Exception:
        pass
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


HERE = Path(__file__).parent
_load_env(HERE / ".env")
API_KEY = os.environ["ORBITAI_API_KEY"]
URL = "https://aiapi.orbitai.global/v1/chat/completions"
MODEL = "gpt-5.4"
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}

VIDEO = HERE / "test_videos" / "short_8s.mp4"


def post(payload, label):
    print(f"\n=== {label} ===")
    t0 = time.perf_counter()
    r = requests.post(URL, headers=HEADERS, json=payload, timeout=600)
    dt = time.perf_counter() - t0
    print(f"status={r.status_code}  latency={dt:.2f}s")
    try:
        j = r.json()
        print(json.dumps(j, ensure_ascii=False, indent=2))
        return j
    except Exception:
        print(r.text)
        return None


def b64_video():
    return base64.b64encode(VIDEO.read_bytes()).decode()


# A) text-only capability question
post({
    "model": MODEL,
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Do you, gpt-5.4 served via this endpoint, "
                                  "support video input (mp4, webm, gif)? "
                                  "Answer YES or NO and one short sentence."},
    ]}],
}, "A: text-only capability question")

# B) same mp4 base64 sent as image_url
b64 = b64_video()
post({
    "model": MODEL,
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Describe what you see; if you cannot decode the attachment, say SO."},
        {"type": "image_url", "image_url": {"url": f"data:video/mp4;base64,{b64}"}},
    ]}],
}, "B: mp4 sent as image_url")

# C) video_url with explicit fallback instruction
post({
    "model": MODEL,
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "If the attached video field is empty, "
                                  "ignored, or unsupported, reply EXACTLY 'UNSUPPORTED'. "
                                  "Otherwise describe the video."},
        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}},
    ]}],
}, "C: video_url with UNSUPPORTED fallback")

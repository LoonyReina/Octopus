"""Probe whether gpt-5.4 (via orbitai OpenAI-compat endpoint) understands video.

Usage:
    python video_test.py                # tests every mp4 under ./test_videos/
    python video_test.py path/to/x.mp4  # tests one specific file

For each video we:
  - send a base64 data: URL as content-type "video_url"
  - prompt the model to describe time-ordered changes (so a single-frame
    answer is distinguishable from a real video answer)
  - record HTTP status, raw body, model reply, usage tokens, end-to-end latency
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

# Force UTF-8 stdout so logging non-ASCII model replies works on Windows (cp/gbk).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# env / config
# ---------------------------------------------------------------------------
def _load_env(env_path: Path) -> None:
    """Best-effort .env loader (uses python-dotenv if present, else a tiny
    parser). Existing os.environ values win, just like dotenv defaults."""
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path)
        return
    except Exception:
        pass
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


_HERE = Path(__file__).parent
_load_env(_HERE / ".env")

API_KEY = os.environ.get("ORBITAI_API_KEY")
if not API_KEY:
    raise RuntimeError("ORBITAI_API_KEY not found in environment / .env")

BASE_URL = "https://aiapi.orbitai.global/v1/chat/completions"
MODEL = "gpt-5.4"
TIMEOUT_SECONDS = 600  # video uploads can be slow

PROMPT = (
    "You are testing your video understanding ability. The clip is short. "
    "Please describe what you see, and IMPORTANTLY focus on time-ordered "
    "changes: which scene comes first, what happens next, what motions or "
    "color shifts occur over time. If you can only see a single still frame, "
    "say so explicitly."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def video_to_data_url(video_path: Path) -> str:
    suffix = video_path.suffix.lower().lstrip(".") or "mp4"
    mime = f"video/{suffix}"
    with open(video_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def call_api(video_path: Path) -> dict:
    data_url = video_to_data_url(video_path)
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "video_url", "video_url": {"url": data_url}},
                ],
            }
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    file_size = video_path.stat().st_size
    print(f"\n=== {video_path.name}  ({file_size/1024:.1f} KiB) ===")
    print(f"POST {BASE_URL}  model={MODEL}")

    t0 = time.perf_counter()
    try:
        resp = requests.post(BASE_URL, headers=headers, json=payload,
                             timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        elapsed = time.perf_counter() - t0
        print(f"!! request failed after {elapsed:.2f}s: {e!r}")
        raise
    elapsed = time.perf_counter() - t0

    print(f"status: {resp.status_code}   latency: {elapsed:.2f}s")
    raw_text = resp.text

    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        pass

    if parsed is not None:
        # full pretty body for log
        print("--- response json ---")
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    else:
        print("--- response (non-json) ---")
        print(raw_text)

    reply = None
    usage = None
    if isinstance(parsed, dict):
        try:
            reply = parsed["choices"][0]["message"]["content"]
        except Exception:
            reply = None
        usage = parsed.get("usage")

    if reply:
        print("--- model reply ---")
        print(reply)
    if usage:
        print("--- usage ---")
        print(json.dumps(usage, ensure_ascii=False))

    return {
        "file": str(video_path),
        "size_bytes": file_size,
        "status_code": resp.status_code,
        "latency_s": round(elapsed, 3),
        "reply": reply,
        "usage": usage,
        "raw_response": parsed if parsed is not None else raw_text,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?",
                    help="single video file; default = all *.mp4 in ./test_videos/")
    ap.add_argument("--out", default=str(_HERE / "video_test_results.json"),
                    help="where to dump per-video results json")
    args = ap.parse_args()

    if args.video:
        targets = [Path(args.video)]
    else:
        vd = _HERE / "test_videos"
        targets = sorted(vd.glob("*.mp4"))
        if not targets:
            print(f"no .mp4 files under {vd}", file=sys.stderr)
            return 2

    results = []
    for v in targets:
        try:
            results.append(call_api(v))
        except Exception as e:
            results.append({
                "file": str(v),
                "error": repr(e),
            })

    out_path = Path(args.out)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

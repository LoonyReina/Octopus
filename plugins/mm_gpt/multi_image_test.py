"""Probe whether gpt-5.4 (orbitai) can understand a video by ingesting N
ffmpeg-extracted keyframes as a multi-image `image_url` array.

Strategy (Route 1 from VIDEO_RESULTS.md):
  - Take ./test_videos/medium_43s.mp4 (43s, scene RED -> GREEN -> BLUE,
    incrementing counter, moving square)
  - Use local ffmpeg to extract N frames uniformly across the timeline,
    each downscaled so the longest side <= FRAME_MAX_SIDE px, JPEG.
  - For each N in {4, 6, 8, 12, 16}:
      - Send the frames as a multi-image array in a single chat.completions
        call, once with prompt A (no temporal hint), once with prompt B
        (explicit chronological+timestamp hint).
  - Score each reply on: scene-color sequence, counter incrementing,
    moving square. 1 point each, 0..3 total.

Outputs:
  - frames/{N}/frame_*.jpg            (kept around for debugging)
  - multi_image_test_results.json     (raw structured results)
  - stdout                            (per-call summary)
"""

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests


# Force UTF-8 stdout so non-ASCII model replies log cleanly on Windows cp/gbk.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# env / config
# ---------------------------------------------------------------------------
def _load_env(env_path: Path) -> None:
    """Best-effort .env loader (same logic as gpt_test.py / video_test.py)."""
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
print("API key: loaded from .env" if os.environ.get("ORBITAI_API_KEY")
      else "API key: NOT FOUND")

API_KEY = os.environ.get("ORBITAI_API_KEY")
if not API_KEY:
    raise RuntimeError("ORBITAI_API_KEY not found in environment / .env")

BASE_URL = "https://aiapi.orbitai.global/v1/chat/completions"
MODEL = "gpt-5.4"
TIMEOUT_SECONDS = 300

VIDEO_PATH = _HERE / "test_videos" / "medium_43s.mp4"
VIDEO_DURATION_S = 43.0  # known fixed-length synth clip
FRAME_MAX_SIDE = 768
FRAMES_ROOT = _HERE / "frames"
RESULTS_PATH = _HERE / "multi_image_test_results.json"

N_VALUES = [4, 6, 8, 12, 16]
PROMPT_A = "Describe what you see in these images."
PROMPT_B_TEMPLATE = (
    "These {n} frames are sampled in chronological order from a single "
    "{duration}-second video at timestamps {ts} seconds. Describe the "
    "temporal sequence of events you observe across these frames as a "
    "coherent video narrative."
)


# ---------------------------------------------------------------------------
# ffmpeg frame extraction
# ---------------------------------------------------------------------------
def _ffmpeg_path() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg not found on PATH")
    return p


def uniform_timestamps(duration_s: float, n: int) -> list[float]:
    """N evenly spaced timestamps inside (0, duration). Avoids exact 0 and
    duration to dodge edge-frame artifacts."""
    # Place the i-th of N samples at (i + 0.5) / N of the timeline.
    return [round((i + 0.5) / n * duration_s, 3) for i in range(n)]


def extract_frames(video: Path, n: int, out_dir: Path,
                   max_side: int) -> list[Path]:
    """Extract N frames at uniform timestamps; downscale longest side <= max_side.
    One ffmpeg invocation per frame (cleanest seek; clip is tiny so cost is low).
    Returns list of frame paths in chronological order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # nuke stale frames
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()

    ts_list = uniform_timestamps(VIDEO_DURATION_S, n)
    paths: list[Path] = []
    ffmpeg = _ffmpeg_path()
    # vf: downscale only if longer side > max_side, keep aspect, even dims for jpg
    vf = (f"scale='if(gt(iw,ih),min({max_side},iw),-2)':"
          f"'if(gt(iw,ih),-2,min({max_side},ih))'")
    for i, ts in enumerate(ts_list):
        out = out_dir / f"frame_{i:02d}_{ts:08.3f}.jpg"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{ts:.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", vf,
            "-q:v", "3",
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(
                f"ffmpeg failed extracting frame {i} at t={ts}s: "
                f"rc={r.returncode} stderr={r.stderr.strip()}")
        paths.append(out)
    return paths


def image_to_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        mime = "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------
def call_multi_image(frames: list[Path], prompt: str) -> dict:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for fp in frames:
        content.append({
            "type": "image_url",
            "image_url": {"url": image_to_data_url(fp)},
        })

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    t0 = time.perf_counter()
    try:
        resp = requests.post(BASE_URL, headers=headers, json=payload,
                             timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        elapsed = time.perf_counter() - t0
        return {
            "status_code": None,
            "latency_s": round(elapsed, 3),
            "error": repr(e),
            "reply": None,
            "usage": None,
        }
    elapsed = time.perf_counter() - t0

    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        pass

    reply = None
    usage = None
    if isinstance(parsed, dict):
        try:
            reply = parsed["choices"][0]["message"]["content"]
        except Exception:
            reply = None
        usage = parsed.get("usage")

    return {
        "status_code": resp.status_code,
        "latency_s": round(elapsed, 3),
        "reply": reply,
        "usage": usage,
        "raw": parsed if parsed is not None else resp.text,
    }


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
def score_reply(text: str | None) -> tuple[int, dict]:
    """Score the reply on 3 binary criteria.

    1. scene_color_sequence: mentions red AND green AND blue (any order, but
       reward chronological ordering) -- 1 point if all three colors named.
    2. counter_increment: mentions counter / number(s) increasing / increment
       / changing digits / "1, 2, 3" -- 1 point.
    3. moving_square: mentions square / box / rectangle moving / shifting /
       different position -- 1 point.

    Returns (total_score, per-criterion-dict).
    """
    if not text:
        return 0, {
            "scene_color_sequence": False,
            "counter_increment": False,
            "moving_square": False,
        }
    t = text.lower()

    has_red = bool(re.search(r"\bred\b", t))
    has_green = bool(re.search(r"\bgreen\b", t))
    has_blue = bool(re.search(r"\bblue\b", t))
    color_seq = has_red and has_green and has_blue

    counter_terms = [
        r"counter",
        r"\bnumber(s)?\b.*\b(increas|increment|chang|count|grow|rising|ascend)",
        r"\b(increas|increment|count up|rising|ascend)\w*\b.*\bnumber",
        r"\bdigit",
        r"\b(1\s*,\s*2\s*,\s*3|0\s*,\s*1\s*,\s*2)",
        r"increment",
    ]
    counter_inc = any(re.search(p, t) for p in counter_terms)

    square_terms = [
        r"\bsquare\b",
        r"\bbox\b",
        r"\brectangle\b",
        r"\bcube\b",
        r"\bblock\b",
    ]
    has_shape = any(re.search(p, t) for p in square_terms)
    motion_terms = [
        r"mov(e|ing|ed)",
        r"shift(s|ing|ed)?",
        r"travel",
        r"slid(e|ing)",
        r"position\w*\s+chang",
        r"different\s+position",
        r"new\s+position",
        r"bounc",
        r"animat",
        r"reposition",
    ]
    has_motion = any(re.search(p, t) for p in motion_terms)
    moving_square = has_shape and has_motion

    detail = {
        "scene_color_sequence": color_seq,
        "counter_increment": counter_inc,
        "moving_square": moving_square,
        "_debug": {
            "has_red": has_red, "has_green": has_green, "has_blue": has_blue,
            "has_shape": has_shape, "has_motion": has_motion,
        },
    }
    total = int(color_seq) + int(counter_inc) + int(moving_square)
    return total, detail


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def short_summary(text: str | None, limit: int = 220) -> str:
    if not text:
        return "<no reply>"
    s = " ".join(text.split())
    return s if len(s) <= limit else s[: limit - 1] + "..."


def main() -> int:
    if not VIDEO_PATH.exists():
        print(f"FATAL: missing {VIDEO_PATH}", file=sys.stderr)
        return 2

    print(f"video: {VIDEO_PATH.name}  duration={VIDEO_DURATION_S}s  "
          f"max_side={FRAME_MAX_SIDE}")
    print(f"endpoint: {BASE_URL}  model={MODEL}")

    results: list[dict] = []

    for n in N_VALUES:
        out_dir = FRAMES_ROOT / f"n{n:02d}"
        print(f"\n=== Extracting N={n} frames -> {out_dir} ===")
        frames = extract_frames(VIDEO_PATH, n, out_dir, FRAME_MAX_SIDE)
        ts_list = uniform_timestamps(VIDEO_DURATION_S, n)
        ts_str = ", ".join(f"{t:.1f}" for t in ts_list)
        total_bytes = sum(p.stat().st_size for p in frames)
        print(f"    {len(frames)} frames, total {total_bytes/1024:.1f} KiB, "
              f"timestamps=[{ts_str}]")

        for prompt_kind, prompt_text in [
            ("A_no_hint", PROMPT_A),
            ("B_temporal", PROMPT_B_TEMPLATE.format(
                n=n, duration=int(VIDEO_DURATION_S), ts=ts_str)),
        ]:
            print(f"--- N={n}  prompt={prompt_kind} ---")
            r = call_multi_image(frames, prompt_text)
            score, detail = score_reply(r.get("reply"))
            usage = r.get("usage") or {}
            row = {
                "n_frames": n,
                "prompt_kind": prompt_kind,
                "prompt_text": prompt_text,
                "status_code": r.get("status_code"),
                "latency_s": r.get("latency_s"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "reply": r.get("reply"),
                "score": score,
                "score_detail": detail,
                "frames_total_bytes": total_bytes,
                "error": r.get("error"),
            }
            results.append(row)
            print(f"    status={row['status_code']}  "
                  f"latency={row['latency_s']}s  "
                  f"tok={row['prompt_tokens']}/{row['completion_tokens']}/"
                  f"{row['total_tokens']}  score={score}/3")
            print(f"    detail={ {k: detail[k] for k in detail if not k.startswith('_')} }")
            print(f"    reply: {short_summary(r.get('reply'))}")

    RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {RESULTS_PATH}")

    # final summary table
    print("\n=== SUMMARY ===")
    print(f"{'N':>3} {'prompt':<11} {'score':<6} {'p_tok':<6} {'c_tok':<6} "
          f"{'lat_s':<7}")
    for row in results:
        print(f"{row['n_frames']:>3} {row['prompt_kind']:<11} "
              f"{str(row['score']) + '/3':<6} "
              f"{str(row['prompt_tokens']):<6} "
              f"{str(row['completion_tokens']):<6} "
              f"{str(row['latency_s']):<7}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

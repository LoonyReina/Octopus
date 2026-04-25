"""
DashScope multimodal-embedding-v1 probe.

- Loads API key from .env (DASHSCOPE_API_KEY)
- Calls the multimodal-embedding HTTP endpoint with text and image inputs
- Prints model, dim, usage and latency
- Never logs the API key
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
TEST_ASSETS = HERE / "test_assets"

# DashScope multimodal embedding endpoint (HTTP, not WebSocket).
# Reference: bailian.console.aliyun.com -> 多模态向量 multimodal-embedding-v1
ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)
MODEL = "multimodal-embedding-v1"


def load_api_key() -> str:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "DASHSCOPE_API_KEY":
                return v.strip()
    env = os.environ.get("DASHSCOPE_API_KEY", "")
    if env:
        return env
    raise SystemExit("DASHSCOPE_API_KEY not found in .env or environment")


def image_to_data_uri(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        mime = "image/jpeg"
    b = path.read_bytes()
    b64 = base64.b64encode(b).decode("ascii")
    return f"data:{mime};base64,{b64}"


def embed(api_key: str, contents: list[dict]) -> dict:
    """Call DashScope multimodal embedding.

    contents: list of dicts, each with one of {"text": ...} or {"image": data-uri or url}
    """
    payload = {
        "model": MODEL,
        "input": {"contents": contents},
        "parameters": {},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.perf_counter()
    r = requests.post(ENDPOINT, headers=headers, data=json.dumps(payload), timeout=60)
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        raise RuntimeError(
            f"HTTP {r.status_code}: {r.text[:500]}"
        )
    body = r.json()
    body["_latency_s"] = dt
    return body


def summarize(label: str, body: dict) -> None:
    out = body.get("output", {})
    embs = out.get("embeddings", [])
    usage = body.get("usage", {})
    req_id = body.get("request_id", "")
    if not embs:
        print(f"[{label}] BAD response: {json.dumps(body)[:600]}")
        return
    vec = embs[0].get("embedding", [])
    print(
        f"[{label}] dim={len(vec)} type={embs[0].get('type')} "
        f"latency={body['_latency_s']:.2f}s usage={usage} req_id={req_id[:12]}..."
    )


def main() -> int:
    api_key = load_api_key()

    # ---- Probe 1: text ----
    print("=== Probe 1: text 'a photo of a dog' ===")
    body = embed(api_key, [{"text": "a photo of a dog"}])
    summarize("text", body)

    # ---- Probe 2: image (a single existing frame) ----
    sample_image = (
        HERE.parent / "mm_gpt" / "frames" / "n06" / "frame_00_0003.583.jpg"
    )
    if not sample_image.exists():
        print(f"sample image missing: {sample_image}", file=sys.stderr)
        return 2
    print(f"\n=== Probe 2: image {sample_image.name} ===")
    data_uri = image_to_data_uri(sample_image)
    body = embed(api_key, [{"image": data_uri}])
    summarize("image", body)

    # ---- Probe 3: text+image multi-content (combined) ----
    print("\n=== Probe 3: multi-content (text + image) ===")
    body = embed(
        api_key,
        [
            {"text": "a photo of a scene"},
            {"image": data_uri},
        ],
    )
    summarize("multi", body)

    print("\nProbe OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

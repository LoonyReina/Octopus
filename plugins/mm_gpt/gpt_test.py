import base64
import mimetypes
import os
import requests
import json
from pathlib import Path


def _load_env(env_path: Path) -> None:
    """Minimal .env loader; falls back if python-dotenv is unavailable."""
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


_load_env(Path(__file__).parent / ".env")

API_KEY = os.environ.get("ORBITAI_API_KEY")
if not API_KEY:
    raise RuntimeError("ORBITAI_API_KEY not found in environment / .env")
BASE_URL = "https://aiapi.orbitai.global/v1/chat/completions"
MODEL = "gpt-5.4"

IMAGE_PATH = "test.png"   # 本地图片路径，可改成 png/jpeg/webp 等


def local_image_to_data_url(image_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream"

    with open(image_path, "rb") as f:
        base64_data = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime_type};base64,{base64_data}"


image_data_url = local_image_to_data_url(IMAGE_PATH)

payload = {
    "model": MODEL,
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Describe it."
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_data_url
                    }
                }
            ]
        }
    ]
}

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

resp = requests.post(BASE_URL, headers=headers, json=payload, timeout=120)

print("status:", resp.status_code)
try:
    print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
except Exception:
    print(resp.text)
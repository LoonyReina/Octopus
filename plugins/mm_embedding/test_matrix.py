"""
Multimodal embedding test matrix for DashScope multimodal-embedding-v1.

Tests:
  A. text-text similarity (synonym vs unrelated)
  B. image-image similarity (same video frames vs cross-video)
  C. cross-modal text -> image (red text vs red/green image)
  D. face-likeness (synthetic faces, no internet) - identity vs different vs
     transformed (rotate/crop) of same identity.

Note: DashScope multimodal-embedding returns ONE fused embedding per
contents list. So to embed each item independently we call the API once
per item.
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import time
from pathlib import Path
from typing import Iterable

import requests
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
TEST_ASSETS = HERE / "test_assets"
TEST_ASSETS.mkdir(exist_ok=True)
FRAMES_N06 = HERE.parent / "mm_gpt" / "frames" / "n06"
FRAMES_N12 = HERE.parent / "mm_gpt" / "frames" / "n12"

ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)
MODEL = "multimodal-embedding-v1"


def load_api_key() -> str:
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "DASHSCOPE_API_KEY":
                return v.strip()
    env = os.environ.get("DASHSCOPE_API_KEY", "")
    if env:
        return env
    raise SystemExit("DASHSCOPE_API_KEY not found")


def image_to_data_uri(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime is None:
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def embed_text(api_key: str, text: str) -> tuple[list[float], dict]:
    return _embed(api_key, [{"text": text}])


def embed_image(api_key: str, path: Path) -> tuple[list[float], dict]:
    return _embed(api_key, [{"image": image_to_data_uri(path)}])


def _embed(api_key: str, contents: list[dict]) -> tuple[list[float], dict]:
    payload = {"model": MODEL, "input": {"contents": contents}, "parameters": {}}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    for attempt in range(3):
        t0 = time.perf_counter()
        r = requests.post(ENDPOINT, headers=headers, data=json.dumps(payload), timeout=60)
        dt = time.perf_counter() - t0
        if r.status_code == 200:
            body = r.json()
            embs = body.get("output", {}).get("embeddings", [])
            if not embs:
                raise RuntimeError(f"empty embeddings: {body}")
            return embs[0]["embedding"], {
                "usage": body.get("usage", {}),
                "latency_s": dt,
                "request_id": body.get("request_id"),
            }
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(1.5 * (attempt + 1))
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    raise RuntimeError("retry exhausted")


def cosine(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return s / (na * nb)


# ----- asset prep -----

def ensure_color_frames() -> dict[str, Path]:
    """medium_43s.mp4 has RED 1-10s, GREEN 15-25s, BLUE 30-40s
    These were already extracted to TEST_ASSETS during setup, but if any are
    missing we will not regenerate (ffmpeg already produced them). Fallback:
    create solid color jpegs.
    """
    out = {}
    targets = {
        "red": "med_t5.jpg",
        "green": "med_t20.jpg",
        "blue": "med_t35.jpg",
    }
    for color, name in targets.items():
        p = TEST_ASSETS / name
        if not p.exists():
            # synthesize a solid color jpeg
            im = Image.new(
                "RGB",
                (640, 480),
                {"red": (240, 10, 10), "green": (10, 200, 10), "blue": (10, 10, 240)}[color],
            )
            im.save(p, "JPEG", quality=90)
        out[color] = p
    # short video frame already extracted
    out["short"] = TEST_ASSETS / "short_t2.jpg"
    return out


def ensure_synthetic_faces() -> dict[str, Path]:
    """Synthesize tiny cartoon faces (no internet, no real-person data).

    face_a, face_a_rot (rotated 15deg), face_a_crop (zoom 80%), face_b
    (different face).  Each is 256x256.
    """
    out = {}

    def draw_face(
        path: Path,
        eye_color: tuple[int, int, int],
        skin: tuple[int, int, int],
        hair: tuple[int, int, int],
        mouth_curve: int,
    ) -> None:
        im = Image.new("RGB", (256, 256), (245, 245, 240))
        d = ImageDraw.Draw(im)
        # head
        d.ellipse((40, 40, 216, 220), fill=skin, outline=(50, 30, 30), width=3)
        # hair
        d.pieslice((40, 30, 216, 130), 200, 340, fill=hair)
        # eyes
        d.ellipse((85, 110, 115, 140), fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        d.ellipse((140, 110, 170, 140), fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        d.ellipse((95, 118, 108, 132), fill=eye_color)
        d.ellipse((150, 118, 163, 132), fill=eye_color)
        # nose
        d.line((128, 145, 128, 170), fill=(80, 50, 30), width=3)
        # mouth
        d.arc((100, 160, 156, 160 + mouth_curve), 0, 180, fill=(160, 30, 30), width=4)
        im.save(path, "JPEG", quality=92)

    a = TEST_ASSETS / "face_a.jpg"
    b = TEST_ASSETS / "face_b.jpg"
    a_rot = TEST_ASSETS / "face_a_rot.jpg"
    a_crop = TEST_ASSETS / "face_a_crop.jpg"

    if not a.exists():
        draw_face(a, (40, 80, 200), (235, 200, 175), (40, 30, 20), 30)
    if not b.exists():
        draw_face(b, (60, 130, 50), (200, 160, 140), (180, 130, 60), 50)
    if not a_rot.exists():
        Image.open(a).rotate(15, expand=False, fillcolor=(245, 245, 240)).save(
            a_rot, "JPEG", quality=92
        )
    if not a_crop.exists():
        im = Image.open(a)
        w, h = im.size
        left, top = int(w * 0.1), int(h * 0.05)
        right, bottom = int(w * 0.9), int(h * 0.85)
        im.crop((left, top, right, bottom)).resize((w, h)).save(
            a_crop, "JPEG", quality=92
        )

    out["a"] = a
    out["a_rot"] = a_rot
    out["a_crop"] = a_crop
    out["b"] = b
    return out


# ----- test groups -----

def test_a_text(api_key: str) -> list[tuple[str, str, float]]:
    print("\n--- A. text-text ---")
    samples = {
        "red_dog": "a red dog",
        "crimson_canine": "a crimson canine",
        "green_car": "a green car",
        "puppy": "a small puppy on the grass",
        "spaceship": "an alien spaceship in deep space",
    }
    vecs = {}
    total_usage = {"input_tokens": 0}
    for k, t in samples.items():
        v, info = embed_text(api_key, t)
        vecs[k] = v
        total_usage["input_tokens"] += info["usage"].get("input_tokens", 0)
        print(f"  emb '{k}' tokens={info['usage'].get('input_tokens')} t={info['latency_s']:.2f}s")
    pairs = [
        ("red_dog", "crimson_canine"),  # synonym
        ("red_dog", "puppy"),  # related
        ("red_dog", "green_car"),  # unrelated
        ("red_dog", "spaceship"),  # very unrelated
        ("crimson_canine", "puppy"),
    ]
    rows = []
    for a, b in pairs:
        c = cosine(vecs[a], vecs[b])
        rows.append((a, b, c))
        print(f"  cos({a:>16s}, {b:<16s}) = {c:.4f}")
    return rows


def test_b_image(api_key: str) -> list[tuple[str, str, float]]:
    """Image-image similarity.

    Caveat: medium_43s.mp4 is a SOLID-COLOR test card (RED 1-10s, GREEN
    15-25s, BLUE 30-40s).  All n0x/ frame dirs sample the same source.
    So 'same scene' means 'same solid color' here.  We use:

      red_a, red_b: two RED frames (same scene, same video)
      red, green:   different scenes inside the same video (color change)
      red, real:    cross-domain, real footage from short_8s.mp4
      green, real:  cross-domain
    """
    print("\n--- B. image-image ---")
    red_a = FRAMES_N06 / "frame_00_0003.583.jpg"  # red frame
    red_b = FRAMES_N06 / "frame_01_0010.750.jpg"  # red frame, different t
    green = FRAMES_N06 / "frame_02_0017.917.jpg"  # green frame
    blue = FRAMES_N06 / "frame_04_0032.250.jpg"  # blue frame
    real = TEST_ASSETS / "short_t2.jpg"  # real footage from short_8s.mp4
    paths = {
        "red_a": red_a,
        "red_b": red_b,
        "green": green,
        "blue": blue,
        "real": real,
    }
    vecs = {}
    for k, p in paths.items():
        if not p.exists():
            print(f"  MISSING {p} -- skipping")
            continue
        v, info = embed_image(api_key, p)
        vecs[k] = v
        print(f"  emb '{k}' image_tokens={info['usage'].get('image_tokens')} t={info['latency_s']:.2f}s")
    pairs = [
        ("red_a", "red_b"),  # same scene
        ("red_a", "green"),  # different scene, same video
        ("red_a", "blue"),  # different scene, same video
        ("green", "blue"),  # different scene, same video
        ("red_a", "real"),  # cross-domain
        ("green", "real"),  # cross-domain
        ("blue", "real"),  # cross-domain
    ]
    rows = []
    for a, b in pairs:
        if a not in vecs or b not in vecs:
            continue
        c = cosine(vecs[a], vecs[b])
        rows.append((a, b, c))
        print(f"  cos({a:>10s}, {b:<10s}) = {c:.4f}")
    return rows


def test_c_cross_modal(api_key: str, color_imgs: dict[str, Path]) -> list[tuple[str, str, float]]:
    print("\n--- C. cross-modal text->image ---")
    texts = {
        "txt_red": "a vivid red colored screen",
        "txt_green": "a bright green colored screen",
        "txt_blue": "a deep blue colored screen",
        "txt_yellow_sq": "red background with a yellow square",
    }
    text_vecs = {}
    for k, t in texts.items():
        v, _ = embed_text(api_key, t)
        text_vecs[k] = v
        print(f"  emb text '{k}' = '{t}'")

    image_vecs = {}
    for k, p in color_imgs.items():
        if not p.exists():
            continue
        v, _ = embed_image(api_key, p)
        image_vecs[k] = v
        print(f"  emb image '{k}' from {p.name}")

    rows = []
    for tk in text_vecs:
        for ik in image_vecs:
            c = cosine(text_vecs[tk], image_vecs[ik])
            rows.append((tk, ik, c))
            print(f"  cos({tk:>15s}, {ik:<8s}) = {c:.4f}")
    return rows


def test_d_face(api_key: str, faces: dict[str, Path]) -> list[tuple[str, str, float]]:
    print("\n--- D. face proxy (synthetic cartoons) ---")
    print("  NOTE: These are synthesized cartoon faces, not real photographs.")
    print("        This is a sanity check, not a real face-recognition test.")
    vecs = {}
    for k, p in faces.items():
        v, _ = embed_image(api_key, p)
        vecs[k] = v
        print(f"  emb face '{k}' from {p.name}")
    pairs = [
        ("a", "a_rot"),  # same identity, rotated
        ("a", "a_crop"),  # same identity, cropped
        ("a_rot", "a_crop"),  # same identity, both transformed
        ("a", "b"),  # different identity
        ("a_rot", "b"),
        ("a_crop", "b"),
    ]
    rows = []
    for x, y in pairs:
        c = cosine(vecs[x], vecs[y])
        rows.append((x, y, c))
        print(f"  cos({x:>8s}, {y:<8s}) = {c:.4f}")
    return rows


def main() -> None:
    api_key = load_api_key()
    color_imgs = ensure_color_frames()
    faces = ensure_synthetic_faces()

    a = test_a_text(api_key)
    b = test_b_image(api_key)
    c = test_c_cross_modal(api_key, color_imgs)
    d = test_d_face(api_key, faces)

    # write a JSON for results
    out = {
        "model": MODEL,
        "endpoint": ENDPOINT,
        "A_text_text": [{"x": x, "y": y, "cos": v} for x, y, v in a],
        "B_image_image": [{"x": x, "y": y, "cos": v} for x, y, v in b],
        "C_cross_modal": [{"x": x, "y": y, "cos": v} for x, y, v in c],
        "D_face_proxy": [{"x": x, "y": y, "cos": v} for x, y, v in d],
    }
    (HERE / "test_matrix_results.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\nWrote test_matrix_results.json")


if __name__ == "__main__":
    main()

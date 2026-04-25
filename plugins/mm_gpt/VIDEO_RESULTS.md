# gpt-5.4 (orbitai) Video Input — Probe Results

## Conclusion: NOT USABLE for video

The endpoint accepts the request payload syntactically (HTTP 200) but the
attached `video_url` field is silently dropped — the model itself confirms the
endpoint is text + image only, the provider 400s if the same mp4 is wrapped
as `image_url`, and prompt_tokens is independent of file size, proving the
bytes are never tokenized. **Route 0 (route OpenClaw video to this endpoint
unchanged) is not viable. Use Route 1: local ffmpeg keyframe extraction +
existing `image_url` ingestion.**

---

## Test environment

| item                | value                                                          |
| ------------------- | -------------------------------------------------------------- |
| API endpoint        | `https://aiapi.orbitai.global/v1/chat/completions`             |
| Model               | `gpt-5.4`                                                      |
| Auth                | Bearer (key loaded from `plugins/mm_gpt/.env`, never logged)   |
| Wire format         | OpenAI-compatible chat.completions                             |
| Content type tested | `{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}}` |
| Date                | 2026-04-24                                                     |

Test videos were synthesized locally with ffmpeg from `lavfi` color sources +
`drawtext`/`drawbox` so each clip has unmistakable time-ordered changes
(scene labels, counters, moving boxes, fading text). Specs:

| file                 | duration | resolution | fps | codec  | size     |
| -------------------- | -------- | ---------- | --- | ------ | -------- |
| `short_8s.mp4`       | 8.0 s    | 640×480    | 24  | h264   | 107 KiB  |
| `medium_43s.mp4`     | 43.0 s   | 640×480    | 24  | h264   | 100 KiB  |
| `long_118s.mp4`      | 118.0 s  | 640×480    | 24  | h264   | 466 KiB  |

---

## Per-video results

| video        | HTTP | latency | prompt_tok | compl_tok | total | reflects content? | time-ordered understanding? |
| ------------ | ---- | ------- | ---------- | --------- | ----- | ----------------- | --------------------------- |
| short_8s     | 200  | 6.06 s  | 68         | 45        | 113   | NO (says "single still frame, no video ability") | NO |
| medium_43s   | 200  | 5.06 s  | 78         | 127       | 205   | NO — hallucinated "close-up of a person's face, red/orange light" (clip is solid red/green/blue with text labels and a bouncing cyan box) | NO |
| long_118s    | 200  | 13.34 s | 68         | 61        | 129   | NO ("single still frame, no video ability") | NO |

Reply summaries:

- **short_8s**: *"I currently see a single still frame and do not have the
  ability to view or analyze video clips."*
- **medium_43s**: *"I only have a single still frame here … close-up of a
  person's face … strong red lighting and shadow."* (entirely hallucinated —
  ground truth is `SCENE 1: RED` / `SCENE 2: GREEN` / `SCENE 3: BLUE` flat
  color backgrounds with overlay text and animated boxes)
- **long_118s**: *"I can only see a single still frame and do not have the
  ability to view video or observe changes over time."*

`prompt_tokens` is **flat at 68–78 across 107 KiB → 466 KiB videos**, i.e.
the body of the video file is not entering the model's context at all.

---

## Side probes (`video_test_probe.py`)

| probe | request shape                          | result | takeaway |
| ----- | -------------------------------------- | ------ | -------- |
| A     | text-only: "do you support video?"     | 200, model answers literally "NO — this endpoint currently supports text and image input, not direct video files like mp4, webm, or gif." | model self-reports no video |
| B     | same mp4 base64 inlined as `image_url` | **HTTP 400** `{"error":{"message":"Provider returned error","type":"invalid_request_error"}}` | provider rejects mp4 in image slot |
| C     | `video_url` with instruction "if attachment is missing/unsupported, reply UNSUPPORTED" | 200, content == `"UNSUPPORTED"`, prompt_tokens=42 | the `video_url` block is silently stripped server-side |

---

## Inline base64 support

Inline `data:video/mp4;base64,...` does **not** work as a video carrier:

- Wrapped in `video_url`: HTTP 200, but the field is silently dropped (no
  error, no tokens, hallucinated reply).
- Wrapped in `image_url`: HTTP 400 `invalid_request_error` from the provider.

We did not test http(s) URL hosting or signed-upload flows — the gateway does
not advertise either, and the model's self-report indicates the underlying
provider lacks the capability altogether, so chasing alternate transports is
unlikely to help.

---

## Cost estimate

Because the video bytes are not tokenized, "cost per minute of video" through
this endpoint is effectively the cost of a single text turn that returns a
plausible-sounding hallucination:

- ~50–80 prompt tokens regardless of clip length
- ~30–130 completion tokens
- Total: ~80–210 tokens per call, **independent of video duration**

This is misleadingly cheap precisely because the video content is being
ignored. Real video understanding would require either a different
endpoint/model or local frame extraction (Route 1), whose cost scales with
the number of sampled frames at the existing per-image rate.

---

## Recommendation for OpenClaw integration

**Route 0 — verdict: REJECTED.** Do not point OpenClaw's video pipeline at
`gpt-5.4` on `aiapi.orbitai.global`. A naive cfg route would always return
HTTP 200 with hallucinated descriptions and no usage signal that anything
went wrong, producing silently wrong behaviour for the pet system.

**Route 1 — recommended.** Add a video provider that:

1. Uses local ffmpeg to extract N keyframes (e.g. 4–8 frames evenly spaced,
   or scene-detect: `ffmpeg -i in.mp4 -vf "select='gt(scene,0.3)'" -vsync vfr out%03d.jpg`).
2. Sends them as a multi-image `image_url` array in a single chat call (the
   image path is already proven by `gpt_test.py`), with a prompt that
   explicitly states "these N frames are sampled at t=… from a single video
   in chronological order; describe what happens".
3. Optionally caches per-video results keyed by content hash since N-frame
   sampling is deterministic.

Suggested OpenClaw cfg shape (illustrative):

```jsonc
{
  "video_provider": "ffmpeg_keyframes_to_gpt",
  "frame_count": 6,
  "frame_select": "uniform",          // or "scene>0.3"
  "frame_max_side": 768,              // downscale before base64
  "image_endpoint": "orbitai_gpt54",  // reuses existing image route
  "image_endpoint_url": "https://aiapi.orbitai.global/v1/chat/completions",
  "image_endpoint_model": "gpt-5.4"
}
```

If a future endpoint advertises true video support, drop Route 0 in next to
Route 1 and A/B them — but the current `gpt-5.4` deployment is image-only.

---

## Reproducibility

Everything used to produce these numbers is committed under
`plugins/mm_gpt/`:

- `video_test.py` — main per-file probe (saves results JSON next to itself)
- `video_test_probe.py` — A/B/C side experiments (text-only / mp4-as-image / fallback prompt)
- `test_videos/{short_8s,medium_43s,long_118s}.mp4` — synthesized clips
- `.env` — holds `ORBITAI_API_KEY` (gitignored)
- `.gitignore` — excludes `.env`, `__pycache__/`, `test_videos/`

Run `python video_test.py` to re-probe all clips, or `python video_test.py
test_videos/short_8s.mp4` for one. Raw API responses are written to
`video_test_results*.json`.

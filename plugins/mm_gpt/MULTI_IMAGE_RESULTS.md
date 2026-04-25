# gpt-5.4 (orbitai) Multi-Image Video Probe — Route 1 Results

## Conclusion: Route 1 is VIABLE. Recommended config: **N=6 frames, prompt B (temporal hint), longest-side <= 768 px JPEG**.

The endpoint accepts a single chat-completion containing N base64 `image_url`
items and produces a coherent, time-ordered narrative for our 43 s synth clip
across N ∈ {4, 6, 8, 12, 16}. With the temporal-hint prompt (B), N=4 / 6 / 8 /
16 all hit the full 3/3 quality score. N=6 is the sweet spot: full score at the
lowest token + latency budget. Wire route OpenClaw video understanding through
ffmpeg uniform keyframe extraction + multi-image `image_url` array.

---

## Test environment

| item             | value                                                            |
| ---------------- | ---------------------------------------------------------------- |
| API endpoint     | `https://aiapi.orbitai.global/v1/chat/completions`               |
| Model            | `gpt-5.4`                                                        |
| Auth             | Bearer (key loaded from `plugins/mm_gpt/.env`, never logged)     |
| Wire format      | OpenAI-compatible chat.completions, multi `image_url` content    |
| Date             | 2026-04-24                                                       |
| Test clip        | `test_videos/medium_43s.mp4` (43 s, 640x480, h264, 100 KiB)      |
| Ground truth     | Scene 1 RED + yellow square moving L→R, Scene 2 GREEN + counter 1..5, Scene 3 BLUE + shrinking white square |
| Frame extractor  | local ffmpeg, uniform timestamps, longest side <= 768 px, JPEG q=3 |
| Encoding         | base64 data URL per frame, `image_url` content blocks            |

`gpt_test.py` already proves single-image `image_url` works against this same
endpoint; this experiment exercises the multi-image generalisation.

---

## Test matrix (10 calls = 5 frame counts x 2 prompts)

Prompts:

- **A (no hint)**: `"Describe what you see in these images."`
- **B (temporal)**: `"These {n} frames are sampled in chronological order from a single 43-second video at timestamps {ts} seconds. Describe the temporal sequence of events you observe across these frames as a coherent video narrative."`

Quality score (0–3): one point each for (a) naming red→green→blue scene
colours, (b) calling out the counter / digits changing, (c) calling out the
square / box / rectangle moving / shifting / shrinking. All checks were done
case-insensitively on the model reply.

| N  | prompt | quality | prompt_tok | compl_tok | total_tok | latency | reply summary |
| -- | ------ | ------- | ---------- | --------- | --------- | ------- | ------------- |
| 4  | A      | 0/3     | 25         | 59        | 84        | 10.06 s | **anomaly**: gateway returned "I don't actually have the images yet"; prompt_tokens=25 indicates the image array was silently dropped on this single call (compare N=4 B where the same frames produced 1517 prompt tokens). Ignore. |
| 4  | B      | 3/3     | 1517       | 200       | 1717      | 9.35 s  | "red bg with yellow shape on the left → green bg with yellow number 3 (cycling 1..5) → blue bg with shrinking white square that has nearly disappeared by the final frame" |
| 6  | A      | 3/3     | 2943       | 223       | 3166      | 6.81 s  | "three labeled scenes: red+yellow square (label says red circle moving L→R), green+yellow numbers '4' then '2', blue with shrinking white square no longer visible" |
| 6  | B      | 3/3     | 2249       | 198       | 2447      | 7.19 s  | "Scene 1 red bg yellow shape near left moving L→R, Scene 2 green bg numbers 4 then 2 cycling 1..5, Scene 3 blue bg white square shrinking until barely visible" |
| 8  | A      | 2/3     | 3919       | 236       | 4155      | 5.98 s  | All three scenes named with colours; lists numbers "5" and "1" verbatim but does not describe the **change** as incrementing → counter check missed by lexical scorer (semantically present). Square+motion both noted. |
| 8  | B      | 3/3     | 2981       | 246       | 3227      | 8.70 s  | Cleanest narrative of the run: explicit three-part sequence, numbers cycling, shrinking white square. |
| 12 | A      | 2/3     | 4357       | 242       | 4599      | 7.52 s  | All scenes + colours + square; lists numbers "3, 1, 5, 3" but no explicit "increment / changing over time" verb → counter check missed by lexical scorer. |
| 12 | B      | 2/3     | 4445       | 211       | 4656      | 9.49 s  | Similar regression: numbers listed in order but verb "incrementing" / "increasing" never appears, even though the model says "cycling through". |
| 16 | A      | 3/3     | 5801       | 319       | 6120      | 11.22 s | Detail-rich; explicitly says numbers "changing across frames: 1, 4, 2, 4, 2, 5". |
| 16 | B      | 3/3     | 5909       | 253       | 6162      | 8.40 s  | Coherent narrative including "count sequence from 1 to 5". |

Notes:

- The scorer is intentionally lexical/conservative. The "2/3" rows on N=8 A,
  N=12 A, N=12 B all *did* perceive the counter but used phrasings ("the
  numbers shown are 5 and 1", "cycling through 1..5") that didn't trip the
  "increment / counter" regex. So the **true** semantic floor across all 10
  calls (excluding the N=4 A gateway anomaly) is 3/3.
- N=4 A is a one-off transport anomaly (prompt_tokens=25 vs 1517 on the
  identical-frame B call). Treat as gateway flake, not a property of N=4.
- B (temporal hint) is strictly more robust: every B run produced an explicit
  chronological narrative; A runs occasionally describe the frames as
  independent images.

---

## Recommended parameters for `extensions/pet-video-keyframes/`

```jsonc
{
  "video_provider":      "ffmpeg_keyframes_to_gpt",
  "frame_count":         6,                // sweet spot
  "frame_select":        "uniform",        // (i + 0.5) / N * duration_s
  "frame_max_side":      768,              // longest side, px
  "frame_format":        "jpeg",           // q ~ 3, ~12 KiB / 640x480 frame
  "image_endpoint":      "orbitai_gpt54",
  "image_endpoint_url":  "https://aiapi.orbitai.global/v1/chat/completions",
  "image_endpoint_model":"gpt-5.4",
  "auth":                "Bearer ${ORBITAI_API_KEY}",
  "request_timeout_s":   120
}
```

### Recommended prompt template (copy verbatim into the extension)

```text
These {N} frames are sampled in chronological order from a single {DURATION}-second video at timestamps {T1}, {T2}, ..., {TN} seconds. Describe the temporal sequence of events you observe across these frames as a coherent video narrative.
```

`{N}` = literal frame count, `{DURATION}` = clip duration in integer seconds,
`{T1}..{TN}` = the same uniform timestamps used for `ffmpeg -ss` (formatted to
one decimal place, comma-separated).

Suggested ffmpeg command per frame (matches what this probe used):

```bash
ffmpeg -hide_banner -loglevel error -y \
       -ss <ts> -i <video> -frames:v 1 \
       -vf "scale='if(gt(iw,ih),min(768,iw),-2)':'if(gt(iw,ih),-2,min(768,ih))'" \
       -q:v 3 frame_<i>.jpg
```

### Pet-system message construction

For each chat turn that involves a video input:

1. ffmpeg-extract 6 uniform-timestamp frames (longest side ≤ 768 px, JPEG q=3).
2. Build one user message whose `content` array is `[ {text: PROMPT_B}, {image_url: frame_0}, {image_url: frame_1}, ..., {image_url: frame_5} ]`.
3. POST to `chat.completions`. Expect ~2.2 k prompt + ~0.2 k completion tokens
   per call, ~7 s end-to-end on this gateway.
4. Optional: cache by SHA256 of (model || prompt || concat(frame_bytes)) since
   uniform sampling is deterministic for a given (clip, N).

---

## Cost estimate (per minute of input video, route 1, N=6)

Token consumption scales linearly with the number of frames sent, **not** with
clip duration. For N=6 our measured cost per **call** was 2249 prompt + 198
completion tokens. Each call covers an entire clip regardless of length, so:

> cost-per-video-minute = cost-per-call / clip-length-minutes

For our 43 s reference clip that's 1 call per 43 s, i.e. 1.395 calls per video
minute, giving:

| metric                   | per call | per video-minute (43 s clip) |
| ------------------------ | -------- | ---------------------------- |
| prompt tokens            | 2249     | ~3137                        |
| completion tokens        | 198      | ~276                         |
| latency                  | 7.2 s    | ~10 s                        |

**USD assumption** (no public tariff for orbitai's `gpt-5.4` re-export — using
gpt-5-class typical pricing as a placeholder; the deployment may differ):
`$2.50 / M input tokens, $10.00 / M output tokens`. Plug in:

> 3137 * $2.5e-6 + 276 * $10e-6 = **~$0.011 per minute of video** with N=6.

Switching N to 16 (no quality gain on this clip) raises that to ~$0.026/min;
N=4 cuts it to ~$0.0073/min but loses safety margin.

If the deployment publishes its own price list, replace the two `e-6` numbers
in the formula above; the token counts are first-party measurements.

For very long clips, the dominant question is sampling density: at 6 uniform
frames a 1-minute clip samples every 10 s and a 10-minute clip samples every
100 s. For pet behaviour where >30 s of unbroken context matters, scale N
proportional to clip length (e.g. `N = max(6, ceil(duration_s / 10))`) but cap
at 16 to stay inside the latency / token / quality envelope.

---

## Known limits and failure modes

1. **More frames does not strictly help.** N=12 hit 2/3 (vs 3/3 at N=4 / 6 / 8 / 16 with prompt B) — extra frames sometimes dilute the model's narrative voice into a per-frame catalogue without explicit incrementation language. Pick the smallest N that already wins.
2. **Gateway can transparently drop the image array on a single call** (our N=4 A run: prompt_tokens=25, model replied "I don't actually have the images yet"). Production should detect this with a `prompt_tokens < 200` sanity check and retry once.
3. **Lexical-only scorer underestimates semantic quality.** "5 and 1, indicating counting" is correct but does not match `/increment/` etc. Real downstream consumers should use the model's natural-language summary directly, not regex it.
4. **Prompt B is strictly better than A for video tasks** (every B run produced an explicit "first ... then ... finally ..." narrative; A runs occasionally read the frames as a still-life set). Always include the temporal hint, even when N is small.
5. **Probe was on a synthetic high-contrast clip.** Real pet-cam footage with subtle motion may need higher N or scene-aware sampling (`select='gt(scene,0.3)'`). Consider a fallback: if uniform-N reply contains words like "no movement" / "static" / "same frame", retry with N=12 or scene-detect mode.
6. **Single-clip benchmark.** All numbers above came from one 43-second test clip. Generalisation to longer / lower-contrast / real-world video should be re-measured before tightening the parameters further.

---

## Reproducibility

- `multi_image_test.py` — this probe (extracts frames, runs all 10 calls, scores, dumps json)
- `multi_image_test_results.json` — raw per-call response, usage, score, and score detail
- `frames/n{04,06,08,12,16}/frame_*.jpg` — extracted keyframes, kept for inspection

Run `python multi_image_test.py` from `plugins/mm_gpt/` to re-measure. The
script reads `ORBITAI_API_KEY` from `.env`; no key is ever printed or written
to results / logs.

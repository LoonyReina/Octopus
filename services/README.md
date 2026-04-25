# mmclaw services (demo bypass of openclaw_423)

Two tiny standalone services for the mmclaw MVP demo. Both are pure stdlib
(plus `cv2` for the camera) so they can run on the Atlas board without any
extra package install.

> Port note: the system already has an `openclaw-gateway` daemon listening on
> `0.0.0.0:18789` from boot, so this chat proxy uses **18790** on the board
> and tunnels to **18789** locally (`-L 18789:localhost:18790`).

```
services/
  .env                 # ORBITAI_API_KEY=, DASHSCOPE_API_KEY=  (chmod 600, NOT committed)
  cam_stream/          # MJPEG http server  -> 127.0.0.1:8080/stream
                       #   + persistent capture mode -> captures/cam_*.jpg
  chat_proxy/          # OpenAI proxy + UI   -> 127.0.0.1:18790/
  perception_input/    # captures -> chat_proxy injector (+ optional embedding)
  captures/            # rolling jpg buffer written by cam_stream (max 100)
  embeddings/          # optional: cam_<ts>.json from perception_input
  run_all.sh
  stop_all.sh
```

## Start (on the board)

```
cd /root/mmclaw/services
./run_all.sh
```

Logs:

- `/tmp/mmclaw_cam_stream.log`
- `/tmp/mmclaw_chat_proxy.log`

## Stop

```
./stop_all.sh
```

## SSH tunnel (from your workstation)

```
ssh -L 18789:localhost:18790 -L 8080:localhost:8080 root@192.168.31.51
```

Then open the local dashboard (see `../dashboard/README.md`).

## Endpoints

- `GET  http://127.0.0.1:8080/stream` — MJPEG (multipart/x-mixed-replace).
- `GET  http://127.0.0.1:8080/health` — liveness.
- `GET  http://127.0.0.1:18790/` — embedded chat page (remote).
- `POST http://127.0.0.1:18790/chat` — body `{messages: [...]}`. Append
  `?stream=1` (or `{"stream": true}`) to receive SSE.

## Capture mode (cam_stream)

`cam_stream` writes one JPEG per `CAPTURE_INTERVAL_SECONDS` (default 5s) to
`CAPTURE_DIR` (default `/root/mmclaw/services/captures`). Filenames look like
`cam_2026-04-25T17-35-00_001.jpg`. Up to `CAPTURE_RETENTION_COUNT` (default
100) files are retained — the oldest are pruned automatically.

Disable by exporting `CAPTURE_ENABLED=0` before `run_all.sh`.

## perception_input watcher

`perception_input/watcher.py` polls `CAPTURE_DIR` for new JPEGs and (a) POSTs
the most-recent frame to `chat_proxy /chat` as a multimodal user message every
`PERCEPTION_INTERVAL_SECONDS`, and (b) (optionally) calls DashScope
`multimodal-embedding-v1` with the same frame, dumping
`embeddings/cam_<ts>.json` for downstream OpenClaw memory ingestion.

Tunables (all optional):

```
PERCEPTION_CAPTURES_DIR        default /root/mmclaw/services/captures
PERCEPTION_EMBEDDINGS_DIR      default /root/mmclaw/services/embeddings
PERCEPTION_CHAT_URL            default http://127.0.0.1:18790/chat
PERCEPTION_INTERVAL_SECONDS    default 15
PERCEPTION_INJECT_CHAT         default 1
PERCEPTION_EMBED               default 1   (requires DASHSCOPE_API_KEY)
PERCEPTION_PROMPT              default "[perception] new camera frame"
DASHSCOPE_API_KEY              required when PERCEPTION_EMBED=1
```

## Notes

- API keys live in `services/.env` (chmod 600). `ORBITAI_API_KEY` for the
  chat proxy, `DASHSCOPE_API_KEY` for the perception_input embedder.
- These services never touch the `openclaw_423` image or the
  `/root/mmclaw/openclaw/` tree.

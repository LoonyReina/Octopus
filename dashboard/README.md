# mmclaw dashboard

A single-file vanilla-JS dashboard with two panes:

- **Left:** the real **OpenClaw** chat UI served by the `mmclaw-dev`
  container on the board (image: `openclaw_423`, with the host source
  bind-mounted at `/app` so PET / extension changes are picked up).
- **Right:** the MJPEG camera stream served by `services/cam_stream` on
  port `8080`.

The container listens on `127.0.0.1:18890` on the board (the standard
`18789` is squatted by the `openclaw-gateway` boot daemon — do **not**
collide with it). The SSH tunnel maps your local `18789` -> remote
`18890`, so the dashboard URL stays `http://localhost:18789/`.

## Quick start (one click)

From `code/`, just double-click **`dashboard_start.bat`** (or run it from
a `cmd` prompt). It will:

1. SSH to `root@192.168.31.51` and run `services/run_all.sh` (idempotent;
   currently keeps `cam_stream` alive — `chat_proxy` is no longer used).
2. SSH to the board and `docker start mmclaw-dev` (creating it on first
   run with `-p 127.0.0.1:18890:18789` and the source bind-mount).
3. Spawn an SSH tunnel in the background:
   `-L 18789:localhost:18890 -L 8080:localhost:8080`.
4. Spawn `python -m http.server 8000 --directory dashboard` in the
   background.
5. Open `http://localhost:8000` in your default browser.

PIDs are written to `%TEMP%\mmclaw_tunnel.pid` and
`%TEMP%\mmclaw_http.pid`; logs go to `%TEMP%\mmclaw_tunnel.log` and
`%TEMP%\mmclaw_http.log`.

To shut everything down (local only — the container is left created so
the next `docker start` is fast):

```
dashboard_stop.bat
```

To also stop the remote services on the board (cam_stream + container):

```
dashboard_stop.bat --remote
```

Requirements on your workstation: `ssh.exe` (Windows OpenSSH or git-bash),
`python` (3.x), `powershell`. SSH key expected at `%USERPROFILE%\.ssh\id_rsa`
(falls back to ssh-agent / config if missing).

If the launcher reports that ports `18789` or `8000` are already in use,
run `dashboard_stop.bat` first to clear stale processes.

## Prerequisites

The board (`192.168.31.51`) must be reachable over SSH. The board must
already have:

- `/root/mmclaw/openclaw/` synced from the workstation (source tree).
- Docker image `openclaw_423` built / loaded.
- `services/cam_stream` runnable for the right pane.

## Manual fallback / troubleshooting

If the one-click launcher can't be used, the steps below reproduce it by
hand.

## 1. Start cam_stream on the board

```
ssh root@192.168.31.51 'cd /root/mmclaw/services && ./run_all.sh'
```

This brings up `cam_stream` on `127.0.0.1:8080` (MJPEG, `/stream`).

## 2. Start the mmclaw-dev container on the board

First time only:

```
ssh root@192.168.31.51 'docker run -d --name mmclaw-dev \
  -p 127.0.0.1:18890:18789 \
  -v /root/mmclaw/openclaw:/app \
  -v /app/node_modules \
  -v /app/dist \
  -w /app \
  --user root \
  openclaw_423'
```

Subsequent boots:

```
ssh root@192.168.31.51 'docker start mmclaw-dev'
```

Why these flags:

- `127.0.0.1:18890:18789` — the board's `openclaw-gateway` daemon already
  squats `0.0.0.0:18789`, so we expose the container on `18890` instead.
- `-v /root/mmclaw/openclaw:/app` — host source tree replaces `/app` so
  PET / extension edits are live.
- `-v /app/node_modules` and `-v /app/dist` — anonymous volumes that
  preserve the image's pre-built `dist/` and pre-installed
  `node_modules/` instead of being shadowed by the bind-mount.
- No `--restart` — keeps a bad start from looping.

Verify:

```
ssh root@192.168.31.51 'curl -fsS http://127.0.0.1:18890/healthz'
```

## 3. Open an SSH tunnel from your workstation

```
ssh -L 18789:localhost:18890 -L 8080:localhost:8080 root@192.168.31.51
```

- Local 18789 -> remote 18890 (mmclaw-dev container, the OpenClaw HTTP UI
  + APIs).
- Local 8080  -> remote 8080  (camera MJPEG stream).

Leave that terminal open while using the dashboard.

## 4. Serve the dashboard locally

`file://` URLs cannot embed a remote `iframe` cleanly under modern browser
security rules, so serve the folder over HTTP:

```
python -m http.server 8000 --directory dashboard
```

Then open `http://localhost:8000` in your browser.

## What you should see

- **Top bar** - status dots for OpenClaw (`/healthz` on :18789), the
  camera stream (`/health`), and the last sync timestamp (if a sidecar
  populated `.last-sync`).
- **Left pane** - iframe at `http://localhost:18789/`. The real OpenClaw
  HTTP UI from inside the `mmclaw-dev` container.
- **Right pane** - `<img>` at `http://localhost:8080/stream`. Live MJPEG
  from the USB camera on the board.
- **Bottom log** - timestamped probe results from the periodic health
  check (every 5 s).

## Known limitations

- The SSH tunnel must be up before the iframe and the MJPEG image can
  load.
- Probes use `mode: 'no-cors'`, which means the dashboard cannot read the
  response body. It only confirms that the socket accepted the connection.
- Keep the tunnel command in a normal terminal; do not paste it into any
  config file checked into git (it pins your workstation IP).

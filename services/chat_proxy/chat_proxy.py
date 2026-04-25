#!/usr/bin/env python3
"""
Minimal chat HTTP server for mmclaw demo.

Responsibilities:
  - GET /            -> serve the embedded vanilla chat page (chat.html).
  - GET /index.html  -> alias.
  - GET /healthz     -> "ok" if API key is configured.
  - POST /chat       -> proxy to https://aiapi.orbitai.global/v1/chat/completions
                        with model "gpt-5.4". Streams the upstream response back
                        as Server-Sent Events when client sets {"stream": true}
                        in body or ?stream=1; otherwise returns the JSON.

Stdlib only (http.server + urllib + json + ssl). No FastAPI/Flask dep so we
don't have to install anything on the Atlas board.

Listens on 127.0.0.1:18789.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import sys
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

LOG = logging.getLogger("chat_proxy")
HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHAT_PORT", "18790"))
UPSTREAM = "https://aiapi.orbitai.global/v1/chat/completions"
MODEL = os.environ.get("CHAT_MODEL", "gpt-5.4")
TIMEOUT = float(os.environ.get("CHAT_TIMEOUT", "180"))
HERE = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("CHAT_ENV_PATH", HERE.parent / ".env"))


def _load_env(env_path: Path) -> None:
    if not env_path.exists():
        LOG.warning(".env not found at %s", env_path)
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_env(ENV_PATH)
API_KEY = os.environ.get("ORBITAI_API_KEY", "").strip()


CHAT_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>mmclaw chat</title>
<style>
  :root {
    --bg:#0e1014; --panel:#181b21; --border:#2a2f38;
    --fg:#d8dde6; --muted:#8a93a3; --accent:#60a5fa;
    --user:#1f2937; --assistant:#0f172a; --error:#7f1d1d;
  }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--fg);
    font:14px/1.45 system-ui, sans-serif; }
  #app { display:flex; flex-direction:column; height:100vh; }
  header { padding:8px 12px; border-bottom:1px solid var(--border);
    background:var(--panel); display:flex; align-items:center; gap:10px; }
  header .title { font-weight:600; }
  header .muted { color:var(--muted); font-size:12px; }
  #log { flex:1; overflow-y:auto; padding:12px; display:flex;
    flex-direction:column; gap:8px; }
  .msg { padding:8px 12px; border-radius:6px; max-width:90%;
    white-space:pre-wrap; word-wrap:break-word; }
  .msg.user { background:var(--user); align-self:flex-end; }
  .msg.assistant { background:var(--assistant); border:1px solid var(--border);
    align-self:flex-start; }
  .msg.error { background:var(--error); color:#fff; align-self:stretch; }
  .msg .role { font-size:10px; color:var(--muted); text-transform:uppercase;
    margin-bottom:2px; letter-spacing:0.05em; }
  .msg img.thumb { max-width:160px; max-height:160px; border-radius:4px;
    display:block; margin-top:4px; }
  form { display:flex; gap:6px; padding:8px; border-top:1px solid var(--border);
    background:var(--panel); }
  textarea { flex:1; resize:none; min-height:40px; max-height:160px;
    background:#0b0d11; color:var(--fg); border:1px solid var(--border);
    border-radius:6px; padding:8px; font:inherit; }
  button { background:var(--accent); color:#0b1220; border:0; border-radius:6px;
    padding:0 14px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:0.5; cursor:not-allowed; }
  button.ghost { background:transparent; color:var(--muted); border:1px solid var(--border); }
  .imgs { display:flex; gap:6px; flex-wrap:wrap; padding:0 8px; }
  .imgs .chip { display:flex; align-items:center; gap:4px; background:#0b0d11;
    border:1px solid var(--border); border-radius:4px; padding:2px 6px; font-size:11px; }
  .imgs .chip img { width:24px; height:24px; object-fit:cover; border-radius:2px; }
  label.upload { display:inline-flex; align-items:center; gap:4px;
    border:1px dashed var(--border); border-radius:6px; padding:0 10px;
    color:var(--muted); cursor:pointer; }
  label.upload input { display:none; }
</style>
</head>
<body>
<div id="app">
  <header>
    <span class="title">mmclaw chat</span>
    <span class="muted" id="hdrModel">model: gpt-5.4</span>
    <span class="muted" id="hdrStatus">idle</span>
    <span style="flex:1"></span>
    <button class="ghost" id="btnClear" type="button">clear</button>
  </header>
  <div id="log"></div>
  <div class="imgs" id="pendingImgs"></div>
  <form id="form">
    <textarea id="input" rows="1" placeholder="ask gpt-5.4..." required></textarea>
    <label class="upload">
      img<input type="file" id="file" accept="image/*" multiple />
    </label>
    <button id="send" type="submit">send</button>
  </form>
</div>
<script>
(function () {
  const logEl = document.getElementById("log");
  const form = document.getElementById("form");
  const input = document.getElementById("input");
  const fileEl = document.getElementById("file");
  const sendBtn = document.getElementById("send");
  const statusEl = document.getElementById("hdrStatus");
  const pendingEl = document.getElementById("pendingImgs");
  const clearBtn = document.getElementById("btnClear");

  // history is the list we send upstream; uiHistory tracks rendered DOM.
  let history = [];
  let pendingImages = []; // [{name, dataUrl}]

  function setStatus(s) { statusEl.textContent = s; }

  function addBubble(role, text, images) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    const r = document.createElement("div");
    r.className = "role";
    r.textContent = role;
    el.appendChild(r);
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = text || "";
    el.appendChild(body);
    if (images && images.length) {
      for (const url of images) {
        const img = document.createElement("img");
        img.className = "thumb";
        img.src = url;
        el.appendChild(img);
      }
    }
    logEl.appendChild(el);
    logEl.scrollTop = logEl.scrollHeight;
    return body;
  }

  function refreshPending() {
    pendingEl.innerHTML = "";
    pendingImages.forEach((p, i) => {
      const c = document.createElement("span");
      c.className = "chip";
      const img = document.createElement("img");
      img.src = p.dataUrl;
      c.appendChild(img);
      const t = document.createElement("span");
      t.textContent = p.name;
      c.appendChild(t);
      const x = document.createElement("a");
      x.textContent = "x";
      x.href = "#";
      x.onclick = (e) => { e.preventDefault(); pendingImages.splice(i, 1); refreshPending(); };
      c.appendChild(x);
      pendingEl.appendChild(c);
    });
  }

  fileEl.addEventListener("change", async () => {
    for (const f of fileEl.files) {
      const dataUrl = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = rej;
        fr.readAsDataURL(f);
      });
      pendingImages.push({ name: f.name, dataUrl });
    }
    fileEl.value = "";
    refreshPending();
  });

  clearBtn.addEventListener("click", () => {
    history = [];
    pendingImages = [];
    refreshPending();
    logEl.innerHTML = "";
    setStatus("idle");
  });

  function userContent(text, images) {
    if (!images.length) return text;
    const parts = [{ type: "text", text }];
    for (const im of images) parts.push({ type: "image_url", image_url: { url: im.dataUrl } });
    return parts;
  }

  async function sendMessage(text) {
    const imgs = pendingImages.slice();
    pendingImages = [];
    refreshPending();
    addBubble("user", text, imgs.map(i => i.dataUrl));
    history.push({ role: "user", content: userContent(text, imgs) });

    sendBtn.disabled = true;
    setStatus("thinking...");
    const assistantBody = addBubble("assistant", "");
    let acc = "";
    try {
      const resp = await fetch("/chat?stream=1", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history, stream: true }),
      });
      if (!resp.ok || !resp.body) {
        const txt = await resp.text();
        throw new Error("HTTP " + resp.status + ": " + txt.slice(0, 400));
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const event = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of event.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const data = line.slice(5).trim();
            if (!data || data === "[DONE]") continue;
            try {
              const j = JSON.parse(data);
              const delta = j.choices && j.choices[0] && j.choices[0].delta;
              if (delta && delta.content) {
                acc += delta.content;
                assistantBody.textContent = acc;
                logEl.scrollTop = logEl.scrollHeight;
              }
            } catch (_) { /* ignore parse errors on keepalive */ }
          }
        }
      }
      if (!acc) {
        // fall back: maybe upstream returned non-stream JSON despite stream=1
        // re-request without streaming
        const r2 = await fetch("/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: history }),
        });
        const j2 = await r2.json();
        acc = (j2.choices && j2.choices[0] && j2.choices[0].message && j2.choices[0].message.content) || "(empty)";
        assistantBody.textContent = acc;
      }
      history.push({ role: "assistant", content: acc });
      setStatus("idle");
    } catch (e) {
      assistantBody.textContent = "(error) " + (e && e.message || e);
      assistantBody.parentElement.classList.remove("assistant");
      assistantBody.parentElement.classList.add("error");
      setStatus("error");
    } finally {
      sendBtn.disabled = false;
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text && pendingImages.length === 0) return;
    input.value = "";
    sendMessage(text || "(see images)");
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
})();
</script>
</body>
</html>
"""


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "mmclawChatProxy/0.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        LOG.info("%s - %s", self.address_string(), fmt % args)

    # -------- helpers --------
    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> Optional[Dict[str, Any]]:
        n = int(self.headers.get("Content-Length") or "0")
        if n <= 0 or n > 50 * 1024 * 1024:  # 50 MB cap
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    # -------- routes --------
    def do_OPTIONS(self) -> None:  # noqa: N802 (CORS preflight)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/chat.html"):
            body = CHAT_HTML.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/healthz":
            ok = bool(API_KEY)
            self._send(200 if ok else 503, b"ok\n" if ok else b"missing key\n")
            return
        self._send(404, b"not found\n")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        query = self.path[len(path) + 1:] if "?" in self.path else ""
        if path != "/chat":
            self._send(404, b"not found\n")
            return
        if not API_KEY:
            self._send_json(503, {"error": "ORBITAI_API_KEY not configured on server"})
            return
        body = self._read_json()
        if not body or "messages" not in body:
            self._send_json(400, {"error": "expected JSON body with 'messages'"})
            return
        stream = bool(body.get("stream")) or ("stream=1" in query) or ("stream=true" in query)
        upstream_payload = {
            "model": body.get("model") or MODEL,
            "messages": body["messages"],
        }
        if stream:
            upstream_payload["stream"] = True
        # passthrough optional knobs
        for k in ("temperature", "max_tokens", "top_p"):
            if k in body:
                upstream_payload[k] = body[k]

        if stream:
            self._proxy_stream(upstream_payload)
        else:
            self._proxy_blocking(upstream_payload)

    def _open_upstream(self, payload: Dict[str, Any]) -> urllib.request.addinfourl:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            UPSTREAM,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
                "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            },
        )
        ctx = ssl.create_default_context()
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)

    def _proxy_blocking(self, payload: Dict[str, Any]) -> None:
        try:
            resp = self._open_upstream(payload)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            LOG.warning("upstream HTTP %s: %s", e.code, err_body[:400])
            self._send(e.code, err_body, "application/json; charset=utf-8")
            return
        except Exception as e:
            LOG.exception("upstream error")
            self._send_json(502, {"error": "upstream", "detail": str(e)})
            return
        body = resp.read()
        self._send(200, body, "application/json; charset=utf-8")

    def _proxy_stream(self, payload: Dict[str, Any]) -> None:
        try:
            resp = self._open_upstream(payload)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            LOG.warning("upstream HTTP %s: %s", e.code, err_body[:400])
            try:
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(err_body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        except Exception as e:
            LOG.exception("upstream error")
            self._send_json(502, {"error": "upstream", "detail": str(e)})
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        try:
            # upstream is already SSE-formatted (OpenAI compatible). Just pass through.
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # pragma: no cover
            LOG.warning("stream proxy error: %s", e)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not API_KEY:
        LOG.error("ORBITAI_API_KEY not loaded from %s; server will return 503 on /chat", ENV_PATH)
    server = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    LOG.info("chat proxy listening on http://%s:%d/  (model=%s, env=%s)", HOST, PORT, MODEL, ENV_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutdown requested")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

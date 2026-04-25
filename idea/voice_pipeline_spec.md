# Voice Pipeline 实现规格

> **目标**：让另一个 Claude 模型从零实现远端语音输入到 OpenClaw 的完整 pipeline。
> **状态**：原版已在远端 `/root/plugin/voice_wake/voice_pipeline.py` 跑通过端到端，但有改进空间（healthz precheck、Queue 串行、流式 ASR）。本规格基于实测教训，重写时**必须遵守**所有"已知坑"和"约束"。
> **撰写**：Claude Opus 4.7 main agent，2026-04-25。

---

## 1. 总流程

```
[USB mic plughw:1,0]
        │ arecord raw S16_LE PCM 16kHz mono
        ▼
[VAD: dB threshold + state machine]
        │ 检测说话开始/结束 → 句子 buffer
        ▼
[腾讯云 SentenceRecognition (TC3 签名)]
        │ wav bytes → base64 → HTTP POST → text
        ▼
[实时显示 + 异步注入 OpenClaw]
        │ docker exec node /app/openclaw.mjs agent --agent main -m "<text>"
        ▼
[octopus_openclaw 容器内 gateway → orbitai/gpt-5.4 LLM]
```

---

## 2. 远端环境（必读）

### 2.1 SSH 访问
- Host: `192.168.31.51`
- User: `root`
- 私钥（开发者本地）: `C:\Users\kousaka\.ssh\id_rsa`
- 远端共享多人机器，**不要 reboot 或 restart docker**

### 2.2 Audio 设备真相（关键，必须按此选择）

`arecord -l` 输出：
- `card 0: Device [USB2.0 Device]` USB ID `8087:1024` Intel Corp. `iProduct: USB2.0 Device` (Generic) — **❌ 假麦克风**，是 USB bridge/controller，driver 给的 capture buffer 是 garbage（zcr 1Hz、peak 极不对称、波形几乎静音带偶发负向脉冲）
- `card 1: Device_1 [USB Composite Device]` USB ID `4c4a:4155` Jieli Technology Composite — **✅ 真麦克风**（zcr 1500Hz, peak ±对称，speech-like）

`aplay -l` 输出：**只有 card 0 有 PLAYBACK** subdevice。card 1 没 playback。

| 用途 | 设备字符串 | 卡号 |
|---|---|---|
| **录音** | `plughw:1,0` | card 1 (Jieli) |
| **播放** | `plughw:0,0` | card 0 (USB2.0 Device) |

**职能错开**——不要复用同一个设备。原 `radio_test.py` 把这两个设备搞反了，导致全脚本失败。

### 2.3 容器 / Docker 状态
- `octopus_openclaw` 是**共享的** OpenClaw 实例，跑在 `ghcr.io/openclaw/openclaw:latest`（image ID `070ef3bab46d`）
- 我们**自己的镜像** `openclaw_423` (image ID `db8dd9c1f4c5`) 不要混淆
- **绝对不要** `docker run` 新容器（远端 `containerd.service` 是 inactive 状态，新容器创建会卡 30+ 秒后 timeout，并留下 ghost name reservation——即 docker daemon 内部锁住名字但容器不存在；唯一清除是 reboot 或 restart docker daemon，影响共享）
- **绝对不要** `systemctl restart docker` 或 `reboot`（影响队友）
- 共用 `octopus_openclaw` 是合规做法

### 2.4 Python 环境
- `python3` 可用（系统 python3.9）
- stdlib 已足够：`base64, hashlib, hmac, io, json, math, os, re, shutil, struct, subprocess, sys, threading, time, urllib.request, urllib.error, wave`
- **不要 pip install** 到 root（共享环境）
- 不需要 `tencentcloud` SDK——手写 TC3 签名 50 行搞定
- 如果将来要流式 ASR：`websocket-client 1.9.0` 和 `websockets 15.0.1` 已装

---

## 3. 工作目录与凭据

### 3.1 路径
- 远端工作目录：`/root/plugin/voice_wake/`
- 主脚本：`/root/plugin/voice_wake/voice_pipeline.py`
- 凭据文件：`/root/plugin/voice_wake/sdk.md`

### 3.2 凭据格式（sdk.md）
```
SecretId:AKID<...>
SecretKey:<...>

https://github.com/TencentCloud/tencentcloud-speech-sdk-python

唤醒旧openclaw（docker-openclaw:local）:docker exec -it octopus_openclaw node dist/index.js tui
```

每行 `Key:Value`，用正则 `^SecretId:(.+)$` 和 `^SecretKey:(.+)$` parse。

### 3.3 凭据安全（必须遵守）
- **绝不** `print()` SecretId/SecretKey 完整值
- 启动时打印 `[boot] tencent creds loaded id=AKI***YEm`（首尾各 3 字符 + `***`）
- 失败时只 mask 后 echo
- 不写入任何 log 文件
- 不写入 idea/、文档、README

---

## 4. 模块详细规格

### 4.1 录音 + VAD

**录音命令**:
```python
cmd = [
    "arecord", "-q", "-t", "raw",
    "-f", "S16_LE",
    "-r", "16000",
    "-c", "1",
    "-D", "plughw:1,0",
    "-",
]
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
```

**chunk 参数**:
- `CHUNK_DURATION = 0.5` 秒
- `CHUNK_BYTES = int(16000 * 2 * 0.5)` = 16000 bytes（每 chunk）
- 每次 `proc.stdout.read(CHUNK_BYTES)` 读一个 chunk

**RMS / dBFS 计算**:
```python
import struct, math
def rms_db(chunk_bytes):
    n = len(chunk_bytes) // 2
    if n == 0: return -120.0
    samples = struct.unpack(f"{n}h", chunk_bytes)
    rms = math.sqrt(sum(s*s for s in samples) / n) or 1e-9
    return 20.0 * math.log10(rms / 32767.0)  # dBFS, ≤ 0
```

**校准期**:
- 前 8 个 chunk（4 秒）累积 `baseline_db = mean(chunks_db)`
- 校准期间 print `[mic] cal chunk i/8 db=...`
- 校准结束 print `[mic] baseline=X dB, threshold=Y dB`

**触发阈值**:
- `threshold_db = baseline_db + 6.0`（说话比环境噪音响 ~4 倍）
- 静音期间 EMA 滑动更新 baseline：`baseline_db = 0.95*baseline_db + 0.05*chunk_db`，对应 threshold 也跟着调

**说话状态机**:
```
state = "silence"
sentence_buf = bytearray()
silence_chunks = 0
speaking_chunks = 0

for each chunk:
    db = rms_db(chunk)
    is_speaking = db > threshold_db
    
    if state == "silence":
        if is_speaking:
            state = "speaking"
            sentence_buf = bytearray(chunk)  # 包含触发的这个 chunk
            speaking_chunks = 1
            silence_chunks = 0
            print("[speaking] start ...")
        else:
            # 静音 - 滑动更新 baseline
            baseline_db = 0.95*baseline_db + 0.05*db
    
    else:  # state == "speaking"
        sentence_buf.extend(chunk)
        if is_speaking:
            speaking_chunks += 1
            silence_chunks = 0
        else:
            silence_chunks += 1
            if silence_chunks >= 2:  # 连续 1 秒（2 × 0.5s）静音
                # 句子结束
                trim = silence_chunks * CHUNK_BYTES
                audio = bytes(sentence_buf[:-trim] if trim < len(sentence_buf) else sentence_buf)
                if speaking_chunks >= 2 and len(audio) >= 16000*2*0.5:
                    # 至少 0.5s 实际语音才送 ASR
                    sentence_id += 1
                    threading.Thread(
                        target=process_sentence,
                        args=(audio, sentence_id),
                        daemon=True,
                    ).start()
                state = "silence"
                sentence_buf = bytearray()
                speaking_chunks = 0
                silence_chunks = 0
                print("[speaking] end ...")
    
    # 单句最长 15s 截断（防止持续说话）
    if state == "speaking" and len(sentence_buf) >= 16000*2*15:
        # 强制 cut
        ...
```

**已知坑**:
- arecord 启动后**第一个 chunk** 经常有 transient（-21 dB 而非环境的 -72 dB）。会略抬 baseline。可以选择跳过第 1 个 chunk，但实测不致命（threshold 仍能正确触发）。
- 短促非语音（嘬嘴、吞咽）会触发 `[speaking]` 但 ASR 返回空字符串/标点。`process_sentence` 必须容错（`[asr] (empty)` 不 inject）。

### 4.2 腾讯 ASR（SentenceRecognition + 手写 TC3）

**API 信息**:
- Endpoint: `https://asr.tencentcloudapi.com/`
- Service: `asr`
- Version: `2019-06-14`
- Action: `SentenceRecognition`
- Region: `ap-shanghai`（任意可选）
- Method: POST，body JSON

**Request Body**:
```python
body = {
    "ProjectId": 0,
    "SubServiceType": 2,
    "EngSerViceType": "16k_zh",   # 普通话 16k 引擎
    "SourceType": 1,              # 数据为 base64
    "VoiceFormat": "wav",
    "Data": base64.b64encode(wav_bytes).decode(),
    "DataLen": len(wav_bytes),
}
body_str = json.dumps(body, separators=(",", ":"))
```

**PCM → WAV in-memory**:
```python
import io, wave
def pcm_to_wav_bytes(pcm_data, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()
```

**TC3-HMAC-SHA256 签名算法**（腾讯云 v3 标准，stdlib 实现）:

参考腾讯官方文档：https://cloud.tencent.com/document/api/1093/35646

```python
import hashlib, hmac, time, json

def tc3_sign(secret_id, secret_key, body_str, region="ap-shanghai"):
    service = "asr"
    host = "asr.tencentcloudapi.com"
    action = "SentenceRecognition"
    version = "2019-06-14"
    timestamp = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    algorithm = "TC3-HMAC-SHA256"
    
    # 1. canonical request
    http_method = "POST"
    canonical_uri = "/"
    canonical_qs = ""
    ct = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{ct}\nhost:{host}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(body_str.encode()).hexdigest()
    canonical_request = (
        f"{http_method}\n{canonical_uri}\n{canonical_qs}\n"
        f"{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    )
    
    # 2. string to sign
    credential_scope = f"{date}/{service}/tc3_request"
    hashed_canonical = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = (
        f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical}"
    )
    
    # 3. signature
    def hmac_sha256(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    secret_date = hmac_sha256(("TC3" + secret_key).encode(), date)
    secret_service = hmac_sha256(secret_date, service)
    secret_signing = hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    
    # 4. authorization header
    authorization = (
        f"{algorithm} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return authorization, timestamp
```

**HTTP 请求**:
```python
import urllib.request
def call_asr(body_str, auth, ts):
    req = urllib.request.Request(
        "https://asr.tencentcloudapi.com/",
        data=body_str.encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": auth,
            "Host": "asr.tencentcloudapi.com",
            "X-TC-Action": "SentenceRecognition",
            "X-TC-Version": "2019-06-14",
            "X-TC-Region": "ap-shanghai",
            "X-TC-Timestamp": str(ts),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())
```

返回 JSON 顶层 key：`Response`（含 `Result`、`RequestId`、`AudioDuration` 等）。如果 error 则 `Response.Error`。

### 4.3 注入 OpenClaw（**唯一稳定路径**）

**❌ 不要走 HTTP**：实测 octopus_openclaw 的 gateway (`http://127.0.0.1:18789`) 所有 GET 200 路径在 POST 时返回 404：
- `/`、`/healthz`（GET 200）
- `/chat`、`/dispatch`、`/send-message`（GET 200，POST 404）
- `/agent`、`/messages`、`/api/agent`、`/v1/agent`（GET 404，POST 404）

实际可用的就 `/healthz`（GET 用作 health probe）。

**✅ 走 docker exec CLI**:
```python
cmd = [
    "docker", "exec", "octopus_openclaw",
    "node", "/app/openclaw.mjs", "agent",
    "--agent", "main",        # ⚠️ 必须 main，不是 default（default 报 "Unknown agent"）
    "-m", text,
    "--json",
    "--timeout", "60",
]
```

返回 JSON:
```json
{
  "runId": "uuid",
  "status": "ok",
  "summary": "completed",
  "result": {
    "payloads": [{"text": "<assistant reply>", "mediaUrl": null}],
    "meta": {"durationMs": ..., "agentMeta": {...}, ...}
  }
}
```

`status="ok"` 且 `summary="completed"` 表示 gateway 接受并处理了消息。

`payloads[0].text` 是 LLM 回复——但**注意**：octopus_openclaw 配的 LLM provider 是 `orbitai/gpt-5.4`，它会拒绝 OpenClaw 默认发的 OpenAI tool schema 字段，导致 reply 是 `"LLM request failed: provider rejected the request schema or tool payload."`。**这不是我们 pipeline 的问题，是 octopus_openclaw 内部 LLM provider 配置问题**。

### 4.4 Health Precheck（**关键**——避免僵尸）

**问题背景**：`octopus_openclaw` 重启时进入 `Up X minutes (health: starting)` 状态，gateway 还没真正监听。此期间任何 `docker exec` 会：
- rc=126 OCI runtime exec failed: `read init-p: connection reset by peer`
- rc=137 timeout SIGKILL（命令本身 hang，containerd 卡）
- 每次失败留下 docker exec / runc 僵尸进程
- 实测一次可积累 48 个僵尸，远端 load 飙到 22+

**修复**: 每次 `inject_openclaw` 前先 healthz check：

```python
def gateway_healthy() -> bool:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:18789/healthz", timeout=3
        ) as r:
            return r.status == 200 and b'"ok":true' in r.read()
    except Exception:
        return False

def inject_openclaw(text: str):
    if not text:
        return
    if not gateway_healthy():
        print("[inject] -> skipped: gateway not healthy (octopus_openclaw still starting?)", flush=True)
        return
    # ... docker exec ...
```

**这是必须项**，不是 nice-to-have。

### 4.5 异步注入 + 串行（建议）

```python
def inject_async(text):
    threading.Thread(target=inject_openclaw, args=(text,), daemon=True).start()
```

**已知坑**：`docker exec` 单次 11–36s（OpenClaw 内部跑一次 LLM call）。多个句子快速到来 → 多个 thread 并发 docker exec → daemon 资源紧张。

**改进建议**：用 `queue.Queue` 单消费者串行化：
```python
import queue
INJECT_Q = queue.Queue(maxsize=20)
def inject_worker():
    while True:
        text = INJECT_Q.get()
        inject_openclaw(text)
        INJECT_Q.task_done()
threading.Thread(target=inject_worker, daemon=True).start()
def inject_async(text):
    try:
        INJECT_Q.put_nowait(text)
    except queue.Full:
        print(f"[inject] -> dropped (queue full)")
```

---

## 5. 输出格式（stdout）

```
[boot] tencent creds loaded id=AKI***YEm
[boot] injection: docker exec octopus_openclaw agent --agent main
[mic] calibrating environment noise...
[mic] cal chunk 1/8 db=-21.0
[mic] cal chunk 2/8 db=-72.3
...
[mic] cal chunk 8/8 db=-69.5
[mic] baseline=-58.3 dB, threshold=-52.3 dB
[mic] ready. speak 中文 (Ctrl+C to stop).
[speaking] start (energy=-44.1 dB > -52.3)
[speaking] end (silence, dur=1.5s, 3 chunks)
[句1] 你好，今天天气怎么样
[inject] -> docker exec agent rc=0 status=ok summary=completed dur=11.1s runId=3e8f5de2-... reply='LLM request failed: provider rejected the request schema or tool payload.'
[speaking] start ...
[speaking] end ...
[asr] (empty)
^C
[bye] stopped
```

---

## 6. 完整文件清单（绝对路径）

### 远端（开发主副本）
- `/root/plugin/voice_wake/voice_pipeline.py` — 主脚本（**唯一权威版本**，~ 400 行）
- `/root/plugin/voice_wake/sdk.md` — 凭据文件（**不进版本控制**）
- `/root/plugin/voice_wake/__pycache__/` — python 编译缓存（运行时生成，可忽略）

### 远端兄弟目录（不同任务，**不要碰**）
- `/root/plugin/image_input/` — M agent 的摄像头输入工作区（不冲突）

### 本机（仅做参考备份）
- `C:\Reina_desktop\program_and_contest\mmclaw\code\openclaw\voice_pipeline.py` — agent O 写脚本时落到本机镜像位置，但**已与远端不同步**（user 重组目录后远端是独立路径）。**重写时以远端为准**。

### 不可变 / 共享
- 共享镜像：`openclaw_423` (`db8dd9c1f4c5`, 2.7GB)、`ghcr.io/openclaw/openclaw:latest` (`070ef3bab46d`, 2.62GB)
- 共享容器：`octopus_openclaw`（基于上面 070ef3 镜像，开机自启 healthy）
- ❌ 不修改这些镜像/容器

---

## 7. 强制约束（必读）

### 7.1 不可破坏
1. ❌ **不修改任何 docker 镜像**（不 `docker rmi/tag/commit`）
2. ❌ **不 `docker run` 新容器**（远端 containerd inactive，会 hang + 留 ghost name reservation）
3. ❌ **不 `systemctl restart docker / containerd`**（影响共享容器）
4. ❌ **不 `reboot`**
5. ❌ **不修改 `octopus_openclaw` 状态**（不 stop/restart/rm；只读用 `docker exec` 跑子命令）
6. ❌ **不 `pip install` 到 root**（共享 Python env）；如必须，用 `pip install --user` 或 git clone 到 `/root/plugin/`
7. ❌ **不动 `/root/plugin/image_input/`**（其他工作区）
8. ❌ **不输出 SecretId/SecretKey 完整值**到任何地方

### 7.2 SSH 节奏控制
- 单连接 batch：用 `ssh ... 'bash -s' << EOF` 一次跑多命令
- 失败立即停下报告（之前 sshd MaxStartups 触发过限流）
- 不并发 SSH 命令
- 累计 SSH ≤ 12 次/任务

---

## 8. 已知坑速查表

| 现象 | 原因 | 解决 |
|---|---|---|
| 录音 RMS 高但听不到内容 | 用了 `plughw:0,0`（card 0 假 mic）| 换 `plughw:1,0` (Jieli card 1) |
| `aplay` 报 `audio open error` 用 `plughw:1,0` | card 1 没 playback subdevice | 播放用 `plughw:0,0` |
| 录音 zcr ≈ 1Hz, peak 不对称 | card 0 给 garbage data | 同上换 card 1 |
| `docker exec` rc=126 init-p reset | 容器 starting / containerd 不稳 | healthz precheck 跳过 |
| `docker exec` rc=137 dur 60s out='' | docker exec 卡 + voice_pipeline 自杀 | 同上 healthz precheck |
| 远端 load 22+ 越涨越高 | 僵尸 docker exec 进程堆积 | 杀 `pkill -9 -f "docker exec octopus_openclaw"` |
| OpenClaw reply 是 `LLM request failed: schema rejected` | octopus_openclaw 配的 orbitai/gpt-5.4 拒绝 tool 字段 | **不是 pipeline 问题**，是 OpenClaw 内部 LLM provider 配置问题 |
| ASR 返回空字符串 | 短促非语音（吞咽、嘬嘴）触发 VAD | 优雅处理，print `[asr] (empty)`，不 inject |
| OpenClaw CLI `--agent default` Unknown | agent name 是 `main` 不是 `default` | 用 `--agent main` |
| HTTP POST `/chat` `/dispatch` 全 404 | gateway 这些路径只接 GET（GET 200 误导）| 必须走 `docker exec ... agent` CLI |
| arecord 第 1 个 chunk -21 dB | USB 启动 transient | 抬高 baseline 但不致命；可选跳第 1 chunk |
| voice_pipeline 启动后 sdk.md 找不到 | 凭据路径硬编码错 | 默认从 `/root/plugin/voice_wake/sdk.md` 读 |

---

## 9. 测试策略

### 9.1 单元
- 录音模块：录 5 秒到临时 wav，验证 wav 头部 + signal 分析（zcr 100-3000Hz, peak 对称）
- ASR 模块：mock wav (e.g. 1s sine wave) → call ASR → expect 200 status + 某个 Result 字段
- 注入模块：调 `docker exec ... agent -m "test"` 看 status="ok" + 解析 JSON 成功
- Health probe：`gateway_healthy()` 返回 True/False 都测

### 9.2 端到端
- 启动 voice_pipeline.py
- 等校准完成（4 秒）
- 在 mic 旁边播放音频（user 用手机播放是常见 setup）
- 看 stdout 出现 `[句1]` + `[inject] -> ... status=ok`
- Ctrl+C 优雅退出（wait subprocess.terminate）

### 9.3 故障注入
- 重启 octopus_openclaw（`docker restart octopus_openclaw`）后立刻跑 voice_pipeline
- 期望：看到 `[inject] -> skipped: gateway not healthy` 而不是 docker exec failures
- 容器 healthy 后 `[inject]` 自动转为成功

---

## 10. 改进 backlog（重写时可选）

按收益排序：

1. **Queue 串行化 docker exec**（防多句并发过载，2 小时工作量）
2. **跳过 arecord 启动首 chunk**（清晰 baseline，30 分钟）
3. **真流式 ASR**（websocket 边录边出文字，而不是说完才识别）— 需要腾讯 appid（sdk.md 里没给），可能要联系 user 补充
4. **指标输出**（每 N 句统计平均 rms / inject 成功率），帮调试 / 监控
5. **优雅 shutdown**（Ctrl+C 时把当前句子缓冲 flush，等 inject 完）
6. **配置文件**（YAML 或环境变量，把硬编码常量挪出代码）

---

## 11. 当前已知未解（不属于本 pipeline 范畴）

1. **NPU 硬件 Alarm**（`Health=Alarm`, LPM/TS IPC 挂死）—— 任何 NPU 推理都 hang，需要 reboot 或售后
2. **远端 `containerd.service` inactive** —— 任何新 docker 容器创建都卡，需 `systemctl start containerd`（影响共享）
3. **octopus_openclaw 配的 orbitai/gpt-5.4 schema 不兼容** —— OpenClaw → orbitai 调用时 LLM 拒绝 tool 字段；reply 永远是 `schema rejected` error；属于上游配置问题
4. **camera UVC driver wedged**（M agent 触发后未恢复）—— `/dev/video0/1` reboot 后已恢复

这四项都不阻塞 voice pipeline 本身工作（前提是 octopus_openclaw 处于 healthy 状态）。

---

## 12. 重写者起步流程（建议）

1. SSH 进远端确认环境：`ssh root@192.168.31.51 'arecord -l; aplay -l; docker ps; ls /root/plugin/voice_wake/'`
2. 读现有 `voice_pipeline.py`（远端 405 行）作为参考
3. 按本规格重写到 `/root/plugin/voice_wake/voice_pipeline_v2.py`（不覆盖原版，平行验证）
4. 单元测试每模块（录音 / ASR / 注入 / health）
5. 端到端测试（user 播音频）
6. 比对新旧版本输出，确认改进项
7. 改进项落地后让 user review，决定是否替换原版

**SSH 节奏 ≤ 12 次/任务**。失败立刻停下报告。

# 远端 OpenClaw 调试观测方案

> **背景**：远端开发板将放进硬件展示，本地需要实时观测 OpenClaw 当前的对话/操作、摄像头画面，便于调试和演示。
>
> **目标**：本机 → 远端建立观测通道，看到三类信息：(a) 对话流；(b) 内部状态/工具调用/记忆变化；(c) 摄像头画面。
>
> **现状**：尚未实施，下面给方案。

---

## 总体架构

```
[本机 Windows]                       [远端开发板]
                                   ┌──────────────────────┐
浏览器 ──┐                          │ docker container     │
         │  SSH 隧道                │  ┌────────────────┐  │
         │  -L 3000:localhost:3000 │  │ OpenClaw web   │  │  ← 对话/操作 (a)(b)
         │  -L 9090:localhost:9090 │  │  port 3000     │  │
         │  -L 8080:localhost:8080 │  └────────────────┘  │
         │                          │  ┌────────────────┐  │
ssh ─────┼──→ root@192.168.31.51   │  │ debug bridge   │  │  ← 状态/事件流 (b)
         │                          │  │  port 9090     │  │
         │                          │  └────────────────┘  │
         │                          │                       │
         │                          │  ┌────────────────┐  │
         │                          │  │ camera streamer│ ←┼── USB cam
         │                          │  │  port 8080     │  │
         │                          │  └────────────────┘  │
         │                          │                       │
本机 IDE ┘                          └──────────────────────┘
(看远端日志/改代码)
```

---

## 三层信息源

### 层 1：对话与基本操作（已有，最快）

OpenClaw 自带 `web/` UI 和 `chat/` 视图（`src/web/`、`ui/src/ui/chat/`）。这是开箱即用的对话观测。

**步骤**：
1. 容器启动时 expose web 端口（默认看 OpenClaw 配置，通常 3000）：
   ```bash
   docker run -p 3000:3000 ... openclaw-image
   ```
2. 远端宿主机防火墙放行 3000（或仅本地监听 + SSH tunnel）。
3. 本机：
   ```bash
   ssh -L 3000:localhost:3000 root@192.168.31.51
   ```
4. 浏览器开 `http://localhost:3000`，看到 OpenClaw chat UI，所有对话历史、tool 调用、回复都在里面。

**优点**：零代码。
**缺点**：只能看 OpenClaw 知道的事；外设状态（屏幕表情、触手位置）、摄像头画面看不了。

### 层 2：宠物内部状态 + 事件总线（需轻量自建）

模拟宠物 P0 改造后会有 mood/affection/energy/learned-skills/owner_profile 等内部状态，主 LLM 之前/之后的钩子里有大量调试信息。这些目前只在日志里出现，看起来很碎。

**方案**：在 `src/pet/` 下加一个 `debug-bridge.ts`，启动一个 WebSocket server（端口 9090，仅 localhost 监听），把以下事件推过去：
- `state.update`：状态变化时
- `memory.capture` / `memory.recall`：记忆抓取/召回
- `skill.taught` / `skill.triggered`：技能学习/触发
- `tool.call` / `tool.result`：工具调用与返回
- `prompt.assembled`：每轮组装好的 system prompt（可选，体积大）

本机自建一个简单的 dashboard（单文件 HTML，订阅 WebSocket，展示成时间轴）放在 `idea/debug-dashboard.html`，浏览器开 `file://...` 直接连 SSH tunnel 后的 `ws://localhost:9090`。

**实施成本**：~200 行 TS + ~150 行 HTML，可以做成 P0 改造的一部分（"PET 调试支撑"）。

### 层 3：摄像头画面（外设侧）

摄像头插在远端硬件上，OpenClaw 本身不消费视频流（它是请求式的，前面 `mm_workflow.md` 里讲过）。摄像头画面是「外设观测通道」，独立于 OpenClaw。

**方案**：
- 远端起一个 Python/Node 进程（或独立 docker 容器）读 USB 摄像头（OpenCV `cv2.VideoCapture` 或 v4l2），把帧推成 MJPEG 流（HTTP）或 WebRTC（更复杂但延迟低）。
- 端口 8080，仅 localhost 监听。
- 本机 SSH tunnel 后浏览器开 `http://localhost:8080/stream`。
- 推荐 MJPEG（最简，<30 行代码即可），延迟通常 200-500ms 足够调试。

**和 OpenClaw 集成**：在 P1/P2 阶段，OpenClaw 需要"看到"摄像头时（比如视频输入/感知层），它和 streamer 之间需要一个共享的帧捕获机制（streamer 推 MJPEG 给浏览器，同时把抽帧后的 jpg 写到 OpenClaw 的 inbox 目录或通过 IPC 传给 OpenClaw 的视频 capability 管道）。这一层等 P1 再做。

---

## 调试模式建议

可以在 `cfg.pet.debug` 加几个开关：

```yaml
pet:
  debug:
    bridge:
      enabled: true
      port: 9090
      includePromptDump: false   # 大体积，按需打开
    verboseStateLog: true
    verboseMemoryLog: true
```

不开 debug 时：bridge 不启动，无性能损耗，仅产生标准日志。

---

## 安全约束

- **本机的 web/bridge/cam 端口都通过 SSH tunnel 暴露**，不在远端公网上裸露，避免摄像头/对话泄露。
- **API key/secret 不能进 debug bridge**——event 序列化时用 schema 白名单，不要用 `JSON.stringify(everything)`。
- **prompt dump 默认关**——真实对话可能含主人隐私（声纹、人脸特征、家庭关系），上线后绝对不能开。
- 远端容器**不绑 0.0.0.0**——一律 `localhost` 监听 + SSH tunnel。

---

## 实施任务（待加进 plan）

| Task | 范围 | 优先级 |
|---|---|---|
| DEBUG-1 | OpenClaw web UI 端口暴露 + SSH tunnel 文档 | INFRA 阶段（与 INFRA-3 同期） |
| DEBUG-2 | `src/pet/debug-bridge.ts` WebSocket server + event schema | PET 改造期，与 PET-1 同期 |
| DEBUG-3 | `idea/debug-dashboard.html` 单文件浏览器 dashboard | PET 改造期 |
| DEBUG-4 | 远端摄像头 MJPEG streamer（独立进程） | INFRA 阶段后期，与 plugins/外设 同期 |
| DEBUG-5 | OpenClaw 与摄像头帧的 IPC 集成 | P1（视频感知层接入时） |

DEBUG-1 立刻可做（零代码），DEBUG-2/3 推荐和 PET-1 同步起步——P0 一开始就让自己看得清，调试效率拉满。

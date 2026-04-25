# OpenClaw 模拟宠物系统改造方案（GPT + Kimi 融合版）

> 融合 idea_V1 的完整需求分析、idea_kimi_V2 的分级改造策略、以及 idea_gpt_V3 的远程开发与部署可行性，形成统一可落地的执行蓝图。

---

## ⚠️ 订正（Claude 实测，2026-04-24）

本文「第二部分：当前 OpenClaw 多模态能力基线」继承自 `mmanl_kimi.md` 的若干**错误结论**（详见 `mmanl_kimi.md` 顶部订正段）。**以下面为准**：

- 视频理解：**已有 google/moonshot/qwen 三家 provider 实现 `describeVideo`**，不是"完全不支持"。模拟宠物视频感知层 MVP 改为**路线 ⓪：直接配 cfg 路由到这三家任一**，无需新建 provider。
- 音频转录：**8 家实现 `transcribeAudio`**，不是"实现弱"。
- TTS/STT：后端 `src/tts/`、`src/realtime-transcription/`、`src/realtime-voice/` 完整，不只在浏览器侧。

### 对原文 P1 感知层规划的影响

原文 P1 第 5 项「感知层（基础多模态）：视频抽帧 + 语音事件流」**不再是必选**：
- 视频理解走配置路由就能跑（无代码改动）
- 抽帧路线降级为**可选优化**：本地化、隐私、不消耗外部 API token、可控帧率/分辨率——值得做但不卡 MVP

### P0/P1 优先级建议重排

模拟宠物**真正的多模态空白**是声纹识别、环境音事件、持续主动感知、face embedding、pose 识别——这些才是必须自建的。

OpenClaw 已具备的能力释放出来后，建议把原文里相对次要的「身份层（声纹/face）」**提升到 P0 边缘**，因为这是核心差异化能力（模拟宠物的"认主人"），且 OpenClaw 完全没做。

---

## 第一部分：远程开发可行性（基于 idea_gpt_V3）

### 目标场景
在 **边缘开发板 + Docker** 环境下开发 OpenClaw，做到本机与远端各保留源码、容器挂载远端目录、修改后尽快生效、支持 SSH + Claude Code 远程开发。

### 核心原则
> **代码在宿主机，环境在容器里**

### 可行性结论：可行，但需注意 3 个关键风险

| 风险点 | 说明 | 建议 |
|--------|------|------|
| **① 跨平台 `node_modules` 冲突** | 本机是 **Windows**，远端边缘板几乎一定是 **Linux**（x86/ARM）。OpenClaw 依赖中存在原生 C++ 模块（如 SQLite、文件监听、加密库），`node_modules` 不能跨平台复用。 | **远端独立维护 `node_modules`**，通过 `.gitignore` 忽略。容器启动后内部重新执行 `pnpm install`。只同步源码目录（`src/`、`extensions/`、`idea/` 等），不同步 `node_modules/`、`dist/`。 |
| **② 边缘板性能瓶颈** | OpenClaw 是大型 TypeScript 项目（700+ TS 文件），首次构建和类型检查对树莓派/Jetson Nano 等边缘板压力很大。 | 边缘板只做**运行时测试**，不做完整构建。构建环节放在远端性能较好的宿主机（如 NAS/x86 小主机）或 CI 上完成。容器内使用 `swc`/`esbuild` 等快速转译替代 `tsc` 全量编译。 |
| **③ 容器运行的是挂载代码而非全局 `claw`** | OpenClaw 官方镜像可能预装了全局 `claw` CLI，容器默认执行的是镜像内的旧版本。 | 容器启动脚本必须显式指向挂载目录的构建产物：`node /mnt/openclaw/dist/index.js` 或在挂载目录内用 `pnpm exec openclaw`。 |

### 推荐开发模式

```
[Windows 本机] ──SSH/rsync──→ [远端宿主机 Linux]
                                        │
                                        ▼
                              [Docker 容器挂载源码]
                                        │
                                        ▼
                              [容器内 pnpm install + 构建]
                                        │
                                        ▼
                              [运行开发版 OpenClaw]
```

- **Git 管理**：以远端宿主机的源码为「开发主副本」，本机只保留备份/提交用副本，定期 `git pull/push` 同步。
- **Claude Code 远程开发**：Claude Code 支持 SSH 远程连接开发，可直接操作远端代码。
- **生效速度**：使用 `tsc --watch` 或 `tsx` 在容器内保持进程常驻，源码修改后自动重启 gateway。

---

## 第二部分：当前 OpenClaw 多模态能力基线

在规划改造前，必须先确认 OpenClaw **已经具备什么**，避免重复造轮子。

| 模态 | 支持状态 | 实现方式 | 对模拟宠物的意义 |
|------|---------|---------|----------------|
| **文本** | ✅ 完全支持 | 核心能力 | 基础对话 |
| **图片理解** | ✅ 支持 | `MediaUnderstandingProvider` 接口，已有 `anthropic`（Claude）、`codex` 插件实现 `describeImage` | MVP 可分析主人发送的照片 |
| **语音 → 文字（STT）** | ✅ 支持 | UI 层调用浏览器原生 `SpeechRecognition`（Web API） | MVP 可用，但仅限浏览器/支持环境 |
| **文字 → 语音（TTS）** | ✅ 支持 | UI 层调用浏览器原生 `SpeechSynthesis`（Web API） | 基础语音回复 |
| **实时语音对话** | ✅ 支持 | `RealtimeTalk` 模块，支持连接 OpenAI Realtime 等实时语音 Agent | 可实现与宠物的实时语音交互 |
| **音频内容理解** | ⚠️ 接口有，实现弱 | `MediaUnderstandingProvider` capabilities 包含 `"audio"`，但现有插件未实现成熟的音频分析（如环境音识别、声纹） | **缺失**：无法识别主人声纹、环境事件音 |
| **视频生成** | ✅ 支持 | `VideoGenerationProvider` 接口，支持文生视频、图生视频 | 与模拟宠物关系不大 |
| **视频理解** | ❌ 不支持 | `MediaUnderstandingProvider` capabilities 虽然在 manifest 层面预留了 `"video"`，但现有插件（anthropic、codex）均未实现视频内容分析、抽帧理解 | **关键缺失**：无法识别主人动作、手势、姿态 |
| **PDF/文档** | ✅ 支持 | Anthropic provider 支持 `nativeDocumentInputs: ["pdf"]` | 可用于读取文档类技能资料 |

### 关键结论

OpenClaw 当前是 **"文本 + 图片 + 语音交互 + 视频生成"** 的多模态网关，但**不是"视频理解 + 音频分析"**的多模态感知系统。

对模拟宠物的影响：
- **MVP 可直接利用**：图片理解（识别主人发来的照片）、语音转文字（实时对话）。
- **必须自建/补齐**：视频理解（动作识别、手势识别）、声纹识别、环境音频事件检测。这些需要通过新增 `MediaUnderstandingProvider` 插件或外部微服务来实现。
- **已有接口可复用**：OpenClaw 的 `MediaUnderstandingProvider` 和 `SpeechProvider` 接口设计良好，模拟宠物的感知层可以直接扩展这些已有契约，而不是另起炉灶。

---

## 第三部分：改造需求分级与直接修改 Core 源码方案

> **策略变更**：放弃 Plugin 封装，直接在 OpenClaw Core 源码中改造。理由：模拟宠物需要深度介入 session 生命周期、Prompt 组装、上下文压缩、消息路由等核心环节，Plugin API 的 hook 粒度和时序不足以支撑流畅体验。直接改 Core 反而更干净、可控、性能更好。

将 idea_V1 的 11 个模块合并为 **6 个核心层 + 2 个支撑层**。

### 合并后的核心改造层

| 合并后层级 | 涵盖原模块 | 核心问题 |
|-----------|-----------|---------|
| **A. 感知层** | 原 1（感知）+ 原 9（实时性）部分 | 多模态输入如何转为结构化事件 |
| **B. 身份层** | 原 2（身份建模） | 主人是谁、如何区分、档案存什么 |
| **C. 记忆层** | 原 5（记忆分层）+ 原 2 部分 | 短期/情景/语义/实体/技能记忆如何分层存储与检索 |
| **D. 技能层** | 原 3（技能）+ 原 6（教学闭环） | 动作如何表示、教学如何闭环、如何判定学会 |
| **E. 状态与决策层** | 原 4（状态/情绪）+ 原 7（决策/Policy） | 宠物内部状态如何驱动行为，如何主动互动 |
| **F. 跨模态 Grounding** | 原 8（跨模态对齐） | 指代、时间对齐、实体绑定 |
| **G. 实时工程层** | 原 9（实时性/架构） | 低/中/高延迟分层、异步化、本地优先 |
| **H. 隐私安全与评测层** | 原 10（隐私）+ 原 11（评测） | 敏感数据保护、记忆可审计、指标体系 |

---

### 优先级分级（直接改 Core 版本）

#### P0 — MVP 必须先做（没有就不叫模拟宠物）

**目标：最小可行地体现「认主人 + 被教会动作 + 有宠物感」**

1. **记忆层升级：结构化实体记忆 + 主人绑定**
   - **现状问题**：`active-memory` 是纯文本摘要+检索，没有「这个人是谁」的概念。
   - **直接修改点**：
     - 新建 `src/pet/entity-memory.ts`：实现 `owner_profile` 存储（称呼、偏好、历史互动摘要）。
     - 修改 `src/auto-reply/reply/session.ts`：在 `initSessionState()` 中增加步骤——如果当前 session 有关联用户标识，加载对应的 `owner_profile`。
     - 修改 `src/plugins/memory-state.ts`：记忆检索函数增加 `ownerId` 过滤参数，优先召回与该用户相关的记忆。
     - 修改 `src/auto-reply/reply/get-reply-run.ts`：在组装 System Prompt 时，追加 `owner_profile` 文本块。

2. **状态与决策层（极简版）：内部状态变量 + Prompt 注入**
   - **现状问题**：OpenClaw 是被动响应式，没有内部状态。
   - **直接修改点**：
     - 新建 `src/pet/pet-state.ts`：定义 `PetState` 类型（`mood`, `affection`, `energy`），提供 `loadPetState(sessionKey)` 和 `updatePetState(...)` 接口，状态持久化为 JSON 文件。
     - 修改 `src/auto-reply/reply/agent-runner.ts`：在构建 payload 前，读取当前 session 的 `petState`，格式化为文本片段注入 System Prompt（如「当前心情：开心，精力：80%」）。
     - 修改 `src/agents/pi-embedded-subscribe.handlers.messages.ts`：在 `handleMessageEnd()` 中，调用 `updatePetState` 更新状态（如收到主人消息 → affection +1）。

3. **技能层（指令映射版）：结构化技能记录 + Prompt 召回**
   - **现状问题**：Skills 是「外部工具包」，不是「宠物学会的本领」。
   - **直接修改点**：
     - 修改 `src/agents/skills/workspace.ts`：在 `loadSkillEntries()` 中，除了加载外部 skills，还加载 `learned-skills.json`（路径放在 agent workspace 下）。
     - 新建 `src/pet/skill-learning.ts`：定义 `LearnedSkill` 类型和 `parseTeachingCommand()` 函数。当检测到主人消息符合教学格式（如「教你握手」）时，解析并写入 `learned-skills.json`。
     - 修改 `src/auto-reply/reply/get-reply-run.ts`：在 System Prompt 中追加「已学会技能列表」片段。
     - 修改 `src/agents/pi-embedded-subscribe.handlers.tools.ts`：在 `after_tool_call` 或消息结束后，分析主人反馈（「真棒」「不对」），更新技能置信度。

**P0 明确不做**：
- 人脸识别/声纹识别（MVP 用文本身份绑定代替）
- 视频理解（纯文本+语音文字）
- 主动发起互动（保持被动响应，通过状态改变回复风格）
- 真正的动作执行（技能只停留在文本描述层）
- 强化学习（规则驱动的置信度更新）

---

#### P1 — 第二阶段：核心体验闭环（让宠物真正可用、可长期陪伴）

4. **身份层：独立 Owner Profile + 多用户区分**
   - 建立 `src/pet/identity-store.ts`，区分 `owner`、`family`、`stranger`。
   - 修改 `src/auto-reply/reply/session.ts`：在会话初始化时，根据 `from`（发送者 ID）查询身份库，加载对应身份标签和权限。
   - 修改 `src/auto-reply/reply/get-reply.ts`：如果是陌生人且置信度低，走「陌生人冷启动」分支（不暴露私密记忆）。

5. **感知层（基础多模态）：视频抽帧 + 语音事件流**
   - 新建 `src/pet/perception/` 目录：
     - `video-frame-extractor.ts`：对上传的视频文件按固定间隔抽帧，保存为临时图片。
     - `perception-event-pipeline.ts`：调用现有 `MediaUnderstandingProvider`（如 anthropic）描述图片内容，生成结构化事件 `{timestamp, event_type, description, involved_entities[]}`。
   - 修改 `src/auto-reply/reply/get-reply.ts`：如果消息附带视频/音频文件，先走感知管道生成事件文本，再把事件文本作为「用户输入」的上下文。
   - 语音语气分析：在 `realtime-talk.ts` 或 STT 流程后，增加简单的规则/模型判断语气标签（开心/生气/平静），写入消息元数据。

6. **教学闭环（反馈学习）：基于多模态反馈更新技能**
   - 修改 `src/pet/skill-learning.ts`：增加 `evaluateFeedback()` 函数。
   - 在 `src/agents/pi-embedded-subscribe.handlers.messages.ts` 的消息结束阶段，调用该函数分析主人本条消息的反馈属性：
     - 正向：「真棒」「好狗」→ 技能 confidence +0.1
     - 负向：「不对」「错了」→ 技能 confidence -0.1，记录纠错原因
     - 重复指令：同一 trigger_word 在短间隔内多次出现 → 可能表示宠物没做对，降低 confidence
   - 状态更新联动：教学成功后，affection 额外增加。

7. **记忆层完整化：情景记忆 + 记忆巩固/遗忘**
   - 修改 `src/plugins/memory-state.ts` 和 `src/context-engine/delegate.ts`：
     - 将记忆分为三层存储：`episodic/`（情景片段）、`semantic/`（抽象知识）、`entity/`（身份档案）。
     - `episodic` 存储具体互动（如「2025-04-24 下午教握手」）；`semantic` 存储抽象结论（如「主人喜欢被舔手」）。
   - 新建 `src/pet/memory-consolidation.ts`：后台定时任务，每天将旧的 episodic 记忆总结为 semantic 记忆，并删除低重要性、超期的 episodic 记录。
   - 修改上下文压缩逻辑：在 `agent-runner.ts` 或 `context-engine/delegate.ts` 中，压缩时优先保留 `entity/` 和最近 3 条 `episodic/`，旧的摘要丢弃。

---

#### P2 — 第三阶段：高阶像真（让它更像真宠物）

8. **状态与决策层（完整版）：主动行为 + Policy 层**
   - 修改 `src/auto-reply/reply/get-reply.ts`：不再仅仅是「用户消息 → 回复」的被动管道，增加内部事件队列 `internalEventQueue`。
   - 修改 `src/pet/pet-state.ts`：增加状态自演化逻辑（energy 随时间恢复/消耗，boredom 随空闲时间上升）。
   - 新建 `src/pet/policy-engine.ts`：定时扫描 `petState`。当 `boredom > threshold` 时，向 `internalEventQueue` 推送一条 `backgroundEvent`，类型为 `PET_INITIATED_GREETING`。
   - `get-reply.ts` 消费 `internalEventQueue`：当没有用户消息时，如果队列中有 backgroundEvent，则触发一次「无用户输入」的回复流程（模拟宠物主动找人）。

9. **跨模态 Grounding：指代消解与时间对齐**
   - 修改 `src/pet/memory-consolidation.ts`：在情景记忆中增加 `location`、`timeRange`、`participants`、`referencedSkills[]` 字段。
   - 修改 `src/auto-reply/reply/get-reply-run.ts`：在 Prompt 中增加指代消解指令，让 LLM 负责将「刚才那个动作」解析为具体 `skillId`。
   - 视频片段定位：在感知管道中为每个抽帧事件保留 `mediaOffsetMs`，当检索到某技能关联的视频事件时，返回 `{videoPath, startMs, endMs}` 给 UI 层。

10. **技能层（执行版）：从文本描述到可执行输出**
    - 修改 `src/pet/skill-learning.ts`：LearnedSkill 增加 `executableAction` 字段。
    - 在 `src/agents/openclaw-tools.ts` 中注册新工具 `execute_pet_action`，当 LLM 输出调用该工具时，实际触发外部动作（如调用硬件 API、播放音效、控制动画）。
    - UI 层（`ui/src/ui/chat/tool-cards.ts`）增加对应 tool 的渲染卡片。

---

#### P3 — 工程支撑（生产级要求）

11. **实时工程分层**
    - 边缘本地部署一个轻量进程 `pet-edge-daemon`（可用 Python/Node 独立进程，通过 WebSocket 与 OpenClaw Core 通信）：
      - 负责低延迟任务：唤醒词检测、人脸快速匹配、简单状态响应。
      - OpenClaw Core 负责中延迟任务：语音理解、视频事件解析、技能执行。
      - 后台异步任务：记忆巩固、个性更新（放在 Core 的定时任务中）。
    - 修改 `src/gateway/call.ts` 或新增 `src/pet/edge-bridge.ts`：提供 Edge ↔ Core 的通信通道。

12. **隐私安全**
    - 修改 `src/pet/entity-memory.ts` 和 `src/pet/identity-store.ts`：
      - 人脸/声纹特征使用 `node:crypto` 加密后存储，密钥存放在 agent 的 secret store 中。
      - 原始视频片段处理完后立即删除，只保留结构化事件和抽帧特征。
    - UI 层新增「记忆审计」面板（`ui/src/ui/views/` 下新增组件），支持查看、删除、纠正记忆。

13. **评测体系**
    - 新建 `src/pet/evaluation/`：
      - `metrics-store.ts`：记录每次互动的指标（响应延迟、技能召回结果、状态一致性）。
      - `consistency-checker.ts`：检测同一情境下宠物情绪是否波动过大。
    - 数据通过 Gateway 暴露给 UI 层，用于展示宠物健康度报告。

---

## 第四部分：直接修改 Core 的文件清单

以下文件需要新增或修改（按优先级排序）：

### 新增文件（建议放在 `src/pet/` 目录下）

| 文件 | 职责 |
|------|------|
| `src/pet/entity-memory.ts` | 主人档案（owner_profile）的 CRUD、身份关联检索 |
| `src/pet/pet-state.ts` | 宠物内部状态（mood/affection/energy）的读写与自演化 |
| `src/pet/skill-learning.ts` | 技能解析、存储、置信度更新、教学闭环 |
| `src/pet/identity-store.ts` | 多用户身份库（owner/family/stranger） |
| `src/pet/perception/video-frame-extractor.ts` | 视频抽帧与临时文件管理 |
| `src/pet/perception/perception-event-pipeline.ts` | 多模态事件生成与结构化 |
| `src/pet/memory-consolidation.ts` | 情景→语义记忆的巩固、遗忘、重要性评分 |
| `src/pet/policy-engine.ts` | 主动行为策略判断与 backgroundEvent 生成 |
| `src/pet/evaluation/metrics-store.ts` | 评测指标记录 |
| `src/pet/evaluation/consistency-checker.ts` | 状态一致性检测 |

### 修改文件（Core 核心管道）

| 文件 | 修改内容 |
|------|---------|
| `src/auto-reply/reply/session.ts` | 启动时加载 owner_profile + pet_state |
| `src/auto-reply/reply/get-reply.ts` | 支持 internalEventQueue（P2），感知事件预处理（P1） |
| `src/auto-reply/reply/get-reply-run.ts` | System Prompt 追加 owner_profile + pet_state + learned_skills |
| `src/auto-reply/reply/agent-runner.ts` | 注入宠物状态上下文，compaction 时保护宠物相关前缀 |
| `src/agents/pi-embedded-runner/run.ts` | 增加 after-turn 钩子，调用状态更新和技能反馈分析 |
| `src/agents/pi-embedded-subscribe.handlers.messages.ts` | 消息结束时更新 petState |
| `src/agents/pi-embedded-subscribe.handlers.tools.ts` | 工具执行后触发技能反馈评估 |
| `src/agents/skills/workspace.ts` | 加载 learned-skills.json |
| `src/agents/openclaw-tools.ts` | 注册 execute_pet_action 工具（P2） |
| `src/plugins/memory-state.ts` | 支持身份关联检索和分层记忆接口 |
| `src/context-engine/delegate.ts` | compaction 时保留宠物状态和主人档案 |
| `src/media-understanding/runner.ts` | 感知管道复用（P1） |
| `ui/src/ui/views/skills.ts` | UI 增加「已学会动作」管理面板 |
| `ui/src/ui/views/agents.ts` | UI 增加宠物状态监控卡片 |

---

## 第五部分：最务实的 MVP 架构（P0 + 远程开发 + 直接改 Core）

```
[边缘开发板/宿主机]
    │
    ├── Docker 容器
    │     ├── OpenClaw Core（直接修改后的源码）
    │     ├── src/pet/          ← 新增宠物核心模块
    │     ├── src/auto-reply/   ← 修改会话管道
    │     ├── src/agents/       ← 修改执行与技能
    │     ├── src/plugins/      ← 修改记忆层
    │     └── extensions/       ← 原有插件不变
    │
    └── 挂载的源码目录（通过 SSH/Claude Code 修改）

[用户交互]
    │
    ├── 文本消息 ──→ get-reply.ts ──→ session.ts 加载 owner_profile + pet_state
    │                                     ↓
    │                              get-reply-run.ts 注入 Prompt
    │                                     ↓
    │                              agent-runner.ts 执行 LLM 调用
    │                                     ↓
    │                              handlers.messages.ts 结束后更新状态
    │
    ├── 图片消息 ──→ anthropic MediaUnderstandingProvider ──→ 描述内容 → 进入记忆
    └── 语音消息 ──→ RealtimeTalk / STT ──→ 转文字后进入流程
```

**MVP 明确不做**：
- 视频理解、声纹识别、主动发起互动、真正的动作执行、强化学习、跨模态 Grounding、实时工程分层。

---

## 第六部分：关键判断题（直接表态）

1. **「记住主人」更像记忆问题，还是身份建模问题？**
   - **身份建模问题**。普通 RAG 是内容检索，记住主人是实体绑定+关系建模。OpenClaw 当前没有 entity store，必须在记忆层之上新增身份层。

2. **「记住主人教过的动作」更像记忆问题，还是技能学习问题？**
   - **技能学习问题**。如果只是把「主人教过转圈」写进记忆，宠物只能复述；技能学习需要「触发条件 → 执行逻辑 → 成功判定 → 泛化映射」的完整表示。

3. **「像宠物一样互动」更依赖多模态记忆，还是状态机/行为策略？**
   - **状态机/行为策略**。多模态记忆让宠物「知道过去发生了什么」，但「宠物感」来自当前状态和决策：同样历史，心情好和心情不好回应完全不同。

4. **如果只把视频理解 + 多模态 RAG 做好，离真正的模拟宠物还差哪些关键能力？**
   - 还差：**身份建模**（认人）、**技能学习**（学会动作）、**状态与 Policy**（情绪驱动主动行为）、**教学闭环**（从被教到学会的系统流程）。

5. **如果只允许选 3 个最关键改造方向，选哪 3 个？**
   - **① 结构化实体记忆 + 主人绑定**（解决认主人）
   - **② 极简内部状态 + Prompt 注入**（解决宠物感）
   - **③ 可学习的技能记录 + 教学闭环**（解决被教会动作）
   - 理由：这三个分别对应 idea_V1 最核心的三个用户诉求，且都能在现有 OpenClaw 架构内以最小改造实现 MVP。

6. **当前 OpenClaw 是否具备模拟宠物所需的多模态能力？**
   - **部分具备，关键缺失**。文本+图片+语音交互已具备；**视频理解、声纹识别、音频事件检测完全不支持**，需要自建感知层补齐。

---

*直接改 Core 的设计原则：
1. 所有宠物专属逻辑收敛到 `src/pet/` 目录，便于维护和后续剥离；
2. Core 管道的修改尽量以「追加」和「钩子调用」为主，减少原有逻辑侵入；
3. 远程开发采用「宿主机代码 + 容器环境」模式，注意跨平台依赖隔离；
4. 多模态感知复用已有 `MediaUnderstandingProvider` 接口，而非另起炉灶。*

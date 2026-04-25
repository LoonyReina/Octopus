# mmclaw 实施计划 v0.1（草稿）

> **状态**：草稿，与 Claude 讨论中持续迭代。后续会形成 v0.2/v0.3 直至定稿。
> **最后更新**：2026-04-24
> **依据**：`idea_gpt_kimi.md`（已订正）+ `mmanl_kimi.md`（已订正）+ Claude 实测代码结论
> **目标读者**：用户 + 后续接手的 agent team

---

## 0. 术语约定

- **本地** = 用户的 Windows 机器（`C:\Reina_desktop\program_and_contest\mmclaw\code`）
- **远端** = 开发板，`ssh root@192.168.31.51`
- **docker** = 远端上跑 OpenClaw 的容器
- **三大宠物核心**：认主人 + 被教会动作 + 有宠物感

---

## 1. 已确认事实（不要重复调研）

### OpenClaw 4.23 多模态能力（实测）

- 图片理解：双轨制（原生 VLM 注入 + 独立视觉模型 fallback），多家 provider 支持。
- 视频理解：架构完整，`describeVideo` 已被 google/moonshot/qwen 三家实现。**配 cfg 即可用**。
- 音频转录：8 家 provider 实现 `transcribeAudio`。
- 后端 TTS/STT/实时语音/实时转录：模块齐备。
- 真正空白：声纹识别、环境音事件、持续主动感知、face embedding、pose 识别。
- `model.input` 类型 union 是 `Array<"text" | "image">`，**没有 `"video"`/`"audio"`**——视频/音频必须走独立 capability 管道，不能直接塞进主 LLM prompt。

### 开发链路

- OpenClaw 用 `pnpm dev` → `scripts/run-node.mjs` → tsdown 增量构建 + node 进程重启。改 .ts 自动重建+重启，plugin 注册会重跑（无热改不生效问题）。
- 跨平台 `node_modules` 不能复用（Windows ↔ Linux），远端独立 `pnpm install`。
- 容器必须跑挂载源码的构建产物，不是镜像内预装的 `claw`。

### 远端
- IP `192.168.31.51`，user root，私钥 `C:\Users\kousaka\.ssh`。
- 工作目录 `/root/mmclaw/`，当前**只有 `plugin/`**（含 yolov8 onnx），`openclaw/` 还未同步。
- Docker 镜像 `openclaw_423` 已就绪。
- NPU 环境：cann 7.0 + npu 23.0（华为 Atlas 系列开发板，原生安装）。

### 远端共享约束（不可破坏）
- ❌ 不修改 `openclaw_*` 系列任何已有 docker 镜像（多人共用）
- ❌ 不修改 docker 镜像默认启动设置（不动 Dockerfile/CMD/ENTRYPOINT）
- ✅ 允许：`docker run` 时挂卷+暴露端口+传 env（运行时参数）
- ✅ 新建容器命名要避开 `openclaw_*` 前缀（约定用 `mmclaw-dev` 等）

---

## 2. 第一阶段：基础设施验证（infra-bootstrap）

**目标**：本地改一行 .ts，能在 docker 里看到日志输出。这是后续一切改造的前提。

**当前状态**：INFRA-1/2/3 + DEBUG-1 + 本地监控 dashboard 由一个 opus agent 接手，**部分完成（2026-04-24）**：
- ✅ INFRA-1 同步工具链就绪（`sync.py`/`sync.bat`/`sync.sh`，rsync-style）
- ✅ DEBUG-1 dashboard 就绪（`dashboard/index.html`/`README.md`）
- ⚠️ INFRA-2 docker 启动**命令模板就绪但未实测**——agent 报告异常截断，未确认容器真起过
- ⚠️ INFRA-3 改码→重建验证**未做**
- ❌ `idea/infra_setup.md` agent 没生成，已由 Claude 主体补写（含未验证项清单）

INFRA-4（API key 配置）单独保留——它涉及具体模型 cfg，等 INFRA-1/2/3 跑通且 MM-1.5 出结果后再做。

---

## 2.bis 远端 NPU 推理（与 INFRA 并行）

### Task NPU-1：远端 yolov8 NPU 推理探索 🔄 进行中
- **背景**：远端 `/root/mmclaw/plugin/` 下用户已部署 yolov8 onnx + 推理代码（当前可能跑在 onnxruntime CPU/GPU），需要切换到原生 NPU 推理（cann 7.0 + npu 23.0）。这是后续多种模型（face/speaker/pose）NPU 推理的第一步。
- **agent**：opus assist agent（后台运行中）
- **工作目录**：远端 `/root/mmclaw/plugin/npu_infer/`（新建）
- **预期产出**：
  - 远端 `npu_infer/yolov8.om`（atc 转换后）
  - 远端 `npu_infer/npu_infer.py`（pyACL 推理代码）
  - 远端 `npu_infer/README.md`（**generic** 文档：环境信息、onnx→om 转换通用模板、acl 推理代码骨架、已知坑）
- **关键风险**：onnx 算子版本兼容（已在 prompt 里告知 fallback 策略：试不同 opset / 重 export from .pt）。
- **注意**：与 INFRA agent 并行——两个 agent 工作目录互不重叠（`/plugin/` vs `/openclaw/`）。

---

## 2.ter 第一阶段任务清单（已分配）

### Task INFRA-1：远端代码同步
- **背景**：本地是 Windows，远端是 Linux，源码需要双向同步。
- **可选实现**：rsync over SSH、git（在远端建 bare repo）、IDE 自带的 SFTP sync。
- **产出**：本地 → 远端的同步命令文档（写到 `idea/infra_setup.md`），包含 `node_modules`/`dist`/`.git` 的排除规则。
- **验收**：本地改 `openclaw/src/entry.ts` 加一行注释，5 秒内远端文件同步更新。

### Task INFRA-2：docker 启动 + 源码挂载
- **背景**：远端已有镜像，需要起容器并挂载远端源码目录。
- **产出**：`docker-compose.yml`（或启动命令）写到 `idea/infra_setup.md`，挂载 `/远端源码路径:/mnt/openclaw`，覆盖 `WORKDIR`、`CMD`。
- **验收**：`docker exec -it <container> sh` 进容器后 `ls /mnt/openclaw/src/entry.ts` 能看到本地改动。

### Task DEBUG-1：OpenClaw Web UI + SSH tunnel
- **背景**：远端开发板将放进硬件展示，本机要观测对话。详见 `idea/debug_setup.md` 层 1。
- **产出**：在 `idea/infra_setup.md` 补一段 docker run 端口映射 + SSH tunnel 命令。
- **验收**：本机浏览器 `http://localhost:3000` 能打开远端 OpenClaw 的 chat UI 并发消息收到回复。
- **依赖**：INFRA-3 完成。

### Task INFRA-3：容器内构建 + 启动开发版
- **背景**：容器内 `pnpm install` 装 Linux 版 native modules，`pnpm dev` 启动 tsdown watch + node。
- **产出**：启动脚本 + 启动后看到的关键日志样例（写到 `idea/infra_setup.md`）。
- **验收**：本地改 `openclaw/src/entry.ts` 加 `console.log("hello mmclaw")`，10 秒内容器日志看到该行。

### Task INFRA-4：模型 API key 配置
- **背景**：OpenClaw 启动后默认模型如何选、API key 从哪读。
- **依赖文件**：`agents/openclaw/models.json`、`extensions/anthropic|openai|google|moonshot|qwen/`。
- **产出**：在远端配好至少一家 provider 的 key，能跑通最简文本 chat。文档写到 `idea/infra_setup.md`。
- **验收**：在 chat 里发 `hi`，模型有回复。

---

## 3. 第二阶段：多模态能力验证（mm-validation）

**目标**：在不改 OpenClaw 任何核心代码的前提下，验证当前已有多模态能力的边界。决定下一阶段走哪条路线。

### Task MM-1.5：多图模拟视频测试（路线 ① 实操参数预验证） ✅ 完成（2026-04-24）
- **结论**：路线 ① **VIABLE**。最佳参数 **N=6 uniform 帧 + prompt B（时序引导）+ 最长边 ≤ 768px JPEG q=3**。
- **数据**：N=6 性能 2249/198 tokens、~7s 延迟、3/3 质量分；cost ~$0.011/video-minute（gpt-5 typical pricing 假设）。
- **关键发现 ⚠️**：orbitai gateway 偶尔会**静默丢弃 image_url 数组**（一次 N=4 A 命中：prompt_tokens=25，模型回 "I don't actually have the images yet"）。**MM-3 实现时必须加 sanity check：`prompt_tokens < 200` → 视为异常 → retry once**。否则模拟宠物会随机性地把"什么也没看见"当成视频内容写进记忆。
- **产出**：`plugins/mm_gpt/{multi_image_test.py, multi_image_test_results.json, MULTI_IMAGE_RESULTS.md, frames/n{04,06,08,12,16}/}`，含完整 cfg shape + ffmpeg 命令模板 + prompt 文本（agent team 直接抄）。
- **prompt 模板**（精确文本）：`These {N} frames are sampled in chronological order from a single {DURATION}-second video at timestamps {T1}, ..., {TN} seconds. Describe the temporal sequence of events you observe across these frames as a coherent video narrative.`

### Task MM-1：plugins/mm_gpt 视频测试 ✅ 完成（2026-04-24）
- **结论**：**不可用（NOT USABLE）**。详见 `plugins/mm_gpt/VIDEO_RESULTS.md`。
- **关键发现**：
  - gpt-5.4 (orbitai) 服务器**静默丢弃** `video_url` 字段——HTTP 200 + 完全幻觉的回复 + prompt_tokens 与文件大小无关
  - 静默失败是最危险的失败模式，cfg 直接路由会污染主人记忆/技能学习
- **影响**：路线 ⓪（OpenClaw 视频 cfg 路由到 orbitai/gpt-5.4）**REJECTED**。改走路线 ①。
- **附带产物**：API key 已 `.env` 化，三段合成视频在 `plugins/mm_gpt/test_videos/`，`video_test.py` 可重跑。

### Task MM-2：OpenClaw 视频路由验证（路线 ⓪ via google/moonshot/qwen，可选）
- **背景**：MM-1 已否定 orbitai/gpt-5.4 视频路径。但 OpenClaw 内置 `extensions/{google,moonshot,qwen}/media-understanding-provider.ts` 三家已实现 `describeVideo`。本任务验证它们在当前部署里能否工作。
- **前置**：拿到 google Gemini / moonshot / qwen 任一 API key（**不要复用 orbitai key**，是不同来源）。
- **依赖文件**：`src/media-understanding/runner.entries.ts:640+`、`extensions/{google|moonshot|qwen}/media-understanding-provider.ts`、cfg 里 `tools.media.video`。
- **任务**：cfg 配某家的 key+模型；发 `plugins/mm_gpt/test_videos/medium_43s.mp4`（已就绪）；验证模型是否真识别时序变化（场景切换、计数器、运动方块）。
- **产出**：`idea/mm_route_zero_results.md`。
- **验收**：模型回复必须正确反映 lavfi 素材的**时序变化**，不是单帧描述也不是幻觉。
- **优先级**：**可选**——若 MM-3 路线 ① 满足 P1 需求，MM-2 可延到 P2 高级感知阶段再评估。

### Task MM-3：本地抽帧路线 ①（推荐主线）
- **背景**：MM-1 否定了 orbitai 视频接口但**确认了 orbitai 图片接口可用**。本任务实现"ffmpeg 抽帧 → 复用 orbitai image 接口"通路，作为模拟宠物视频感知的**默认实现**。`plugins/mm_gpt/VIDEO_RESULTS.md` 末尾给了示例 cfg shape，可以直接对照落地。
- **依赖**：`src/media/ffmpeg-exec.ts`（已封装 ffmpeg）、`src/plugin-sdk/media-understanding.ts:describeImageWithModel`、`src/agents/tools/image-tool.ts`、`plugins/mm_gpt/gpt_test.py`（已验证 orbitai 图片接口）。
- **任务**：
  - 新建 `extensions/pet-video-keyframes/`（`openclaw.plugin.json` + `media-understanding-provider.ts` + `index.ts`，参考 `extensions/anthropic/` 结构）
  - `MediaUnderstandingProvider` 实现：
    - `capabilities: ["video"]`
    - `describeVideo(req)` = ffmpeg 抽 4–8 关键帧（默认 6 帧 uniform，每帧最长边 768px 降采样）→ 拼成多 image_url 数组单次调主 image 接口
    - prompt 模板必须明示「这 N 帧采样于 t=… 来自同一段视频，按时间顺序」
    - 可选：按内容 hash 缓存（uniform 抽帧确定性）
  - cfg shape 仿 `VIDEO_RESULTS.md` 示例
- **产出**：完整 extension + `idea/mm_route_one_results.md`（性能数据：延迟 / token cost / 抽帧策略对比）。
- **验收**：发 `medium_43s.mp4`，输出反映场景切换 + 方块运动；token ≈ 6× 单帧 cost；端到端 ≤ 10s。
- **优先级**：**P1 主线**，模拟宠物视频感知的默认实现。
- **MM-1.5 已确认参数**：直接采用 MM-1.5 推荐参数，不要再做参数搜索。具体：
  - N=6 uniform，时间戳 `(i+0.5)/N * duration_s`
  - 帧最长边 ≤ 768px，JPEG q=3
  - prompt 模板见 MM-1.5 结果（精确文本，逐字使用）
  - **必须**实现 `prompt_tokens < 200` sanity check + 一次重试（防 gateway 静默丢图）
  - 长视频 N 调节规则：`N = clamp(ceil(duration_s/10), 6, 16)`
  - 可选：按 SHA256(model || prompt || concat(frame_bytes)) 缓存（uniform 抽帧确定性）

---

## 4. 第三阶段：宠物核心改造 P0（pet-core）

**目标**：开始改 OpenClaw 核心，新建 `src/pet/` 目录，落地三大核心的最简版本。

详细修改清单见 `idea_gpt_kimi.md` 第三、四部分（P0 共 3 项 + 文件清单），下面是 agent team 视角的任务粒度。

### Task PET-1：实体记忆 + 主人绑定
- **背景**：让宠物认主人，从「所有人一视同仁」变成「认得主人的称呼/偏好」。
- **新增**：`src/pet/entity-memory.ts`（owner_profile CRUD）。
- **修改**：
  - `src/auto-reply/reply/session.ts`（initSessionState 加载 owner_profile）
  - `src/plugins/memory-state.ts`（记忆检索加 ownerId 过滤）
  - `src/auto-reply/reply/get-reply-run.ts`（System Prompt 追加 owner_profile）
- **产出**：可在 chat 里 `教你认我，叫我 Reina，喜欢科技话题` → 下次启动 session 模型回复体现"知道你叫 Reina"。
- **验收**：跨 session 持久化 + System Prompt 实际包含 owner 信息（看 raw payload）。

### Task PET-2：极简状态机
- **背景**：宠物有 mood/affection/energy 三变量，影响回复语气。
- **新增**：`src/pet/pet-state.ts`（读写 + 自演化逻辑，状态持久化为 JSON）。
- **修改**：
  - `src/auto-reply/reply/agent-runner.ts`（payload 前注入状态文本）
  - `src/agents/pi-embedded-subscribe.handlers.messages.ts`（handleMessageEnd 更新状态）
- **产出**：连续聊 10 条 → affection 上升，回复变亲昵。长时间不理 → energy 下降，回复变简短。
- **验收**：状态变化在日志里可观测，回复风格肉眼可辨差异。

### Task PET-3：可学习技能
- **背景**：主人说「教你握手」，宠物记住这个技能，下次说「握手」能识别。
- **新增**：`src/pet/skill-learning.ts`（解析教学指令，写入 `learned-skills.json`）。
- **修改**：
  - `src/agents/skills/workspace.ts`（loadSkillEntries 时加载 learned-skills.json）
  - `src/auto-reply/reply/get-reply-run.ts`（System Prompt 追加技能列表）
- **产出**：教学 → 触发词识别 → 模型表现学会的动作。
- **验收**：跨 session 持久化 + 触发词识别准确率 > 80%（手测 10 例）。

### Task PET-4：状态/记忆/技能联动测试
- **背景**：三个 P0 模块组合工作的端到端验证。
- **任务**：写一个 e2e 测试场景脚本——主人 → 教学 → 反馈 → 多轮对话 → 重启 session → 验证记忆/状态/技能仍生效。
- **产出**：`test/pet-e2e.test.ts` 或手测脚本 + 验收报告。

---

## 5. 第四阶段：未明确（待讨论）

以下方向已识别但优先级 / 范围还没定，等讨论：

- **P1 身份层（声纹+face）**：OpenClaw 真正空白，是否提到 P0 边缘？
- **plugins/ 外设**（屏幕表情、触手）：和宠物核心并行起步还是 P2 再做？
- **持续主动感知**（摄像头/麦克风 daemon）：是否纳入 P1？
- ~~**路线 ① 抽帧**：MM-2 跑通后是否仍要做？~~ → **已决定：路线 ① 升级为主线**（MM-3）
- **隐私/评测/边缘分层**：P3，暂不规划具体任务。

---

## 6. 已知风险

- **跨平台依赖**：本地装的 node_modules 拷到远端会炸，必须容器内 `pnpm install`。
- **plugin 加载阶段错误**：tsdown 重建后 plugin 注册重跑，写错容易整个进程起不来。需要在每个 PET-* 任务里加日志打点。
- **API key 管理**：plugins/mm_gpt 已经有硬编码 key，需要立刻挪到 .env。后续 cfg 里的 key 也要统一管理。
- **mmanl_kimi.md 错误结论的传染**：本文已订正，但如果后续 agent 不读订正段，可能再被误导。每个任务背景必须独立复述事实，不能"参考 idea_gpt_kimi.md 第二部分"。

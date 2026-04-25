# mmclaw 实施计划 v0.2

> **状态**：v0.2 重组（2026-04-25），围绕用户最新明确的总任务-子任务结构。v0.1 已过期，仅作历史参考。
> **目标读者**：用户 + 后续 agent team
> **关键变化**：(a) 不再用 `openclaw_423` 镜像默认 CMD（Z agent 命令死循环教训），(b) 改走极简 demo 通路 + 后期 PET 改造路线，(c) 新增多模态 embedding 和时效性优化任务线

---

## 总任务

> **高实时性的多模态 claw 基础设施**——包括多模态信息获取、保存、搜索、调用链路。

四条腿（每条独立 agent，互不阻塞）：

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ A. 调用链路       │  │ B. NPU 推理     │  │ C. 时效性       │  │ D. 多模态记忆     │
│ (demo 通路)      │  │ (本地视觉算力)   │  │ (端到端低延迟)   │  │ (embedding)     │
│                 │  │                 │  │                 │  │                 │
│ cam stream      │  │ yolov8 om       │  │ prompt cache    │  │ 阿里百炼 mm     │
│ chat backend    │  │ pyACL 推理      │  │ streaming       │  │ embedding       │
│ dashboard       │  │ generic 文档    │  │ 多模态优化       │  │ face/scene 测试 │
└─────────────────┘  └─────────────────┘  └─────────────────┘  └─────────────────┘
       │                    │                   │                    │
       └────────────────────┴───────────────────┴────────────────────┘
                                     │
                          ┌──────────▼──────────┐
                          │ PET-1 实体记忆改造   │
                          │ (P0 第一刀)         │
                          └─────────────────────┘
```

---

## 0. 术语 / 共享约束（沿用 v0.1）

- **本地** = `C:\Reina_desktop\program_and_contest\mmclaw\code\` (Windows)
- **远端** = `ssh root@192.168.31.51`，工作目录 `/root/mmclaw/`
- **远端镜像** = `openclaw_423`（**慎用**，见下面"关键事实订正"）
- **三大宠物核心**：认主人 + 被教会动作 + 有宠物感

### 远端目录布局（agent 工作区严格分隔）

```
/root/mmclaw/
├── openclaw/        # OpenClaw 源码同步目标（Z agent 已建，2.2G）
├── plugins/         # （正式目录，复数）
│   ├── yolo/        # 用户原 yolov8（只读：yolov8m.onnx, yolo_camara_onnx.py）
│   └── npu_infer/   # B agent 工作区
├── plugin/          # ❌ 旧的孤儿目录（已确认空），B agent 第 0 步会清掉
└── services/        # A agent 工作区（cam streamer + chat backend）
```

### 共享约束（不可破坏）
- ❌ 不修改 `openclaw_*` 任何已有 docker 镜像
- ❌ 不修改 docker 镜像默认启动设置
- ✅ 容器命名避开 `openclaw_*` 前缀（用 `mmclaw-*`）

---

## 1. 关键事实订正（v0.1 过期点）

### 1.1 fail2ban 不存在
之前怀疑 SSH 触发 fail2ban，**实测远端没装 fail2ban**。SSH 不通的真因是远端被重启过 + sshd MaxStartups 限流（默认 10:30:100）。仍要做 SSH 节奏控制（单连接 batch、不并发、失败不重试），但不需要 unban。

### 1.2 `openclaw_423` 镜像是开箱即用的（详见记忆 `openclaw_423_image.md`）
- WORKDIR=`/app`，里面有 dist + node_modules + extensions 全套
- 默认 CMD = `node openclaw.mjs gateway --allow-unconfigured`
- HEALTHCHECK 端口是 **18789**，**不是 3000**
- **Z agent 启动命令 4 个错误**：`pnpm install` 多余 + `--bind 0.0.0.0` 无效 + 端口 3000 错 + `--restart unless-stopped` 配错命令 → 死循环

### 1.3 yolov8 真路径
**`/root/mmclaw/plugins/yolo/yolov8m.onnx`**（不是 `/mmclaw/plugins/yolo`，根是 `/root`）。

### 1.4 多模态视频路由路线已锁
- 路线 ⓪ via orbitai/gpt-5.4 = ❌ REJECTED（静默丢弃 + 幻觉）
- 路线 ① 本地 ffmpeg 抽帧 + orbitai image 接口 = ✅ 推荐（参数已敲：N=6 uniform，prompt B 模板，详见 `plugins/mm_gpt/MULTI_IMAGE_RESULTS.md`）
- 必须做 sanity check：`prompt_tokens < 200` → 视为 gateway 静默丢图 → retry once

### 1.5 已就绪资产
| 资产 | 路径 | 状态 |
|---|---|---|
| 本地 → 远端同步脚本 | 本地 `sync.py` / `sync.bat` / `sync.sh` | ✅ 已用过，2.2G 同步成功 |
| 视频探针实验 | 本地 `plugins/mm_gpt/{gpt_test, video_test, multi_image_test}.py` | ✅ 完整，可重跑 |
| 测试视频 | 本地 `plugins/mm_gpt/test_videos/{short_8s, medium_43s, long_118s}.mp4` | ✅ ffmpeg lavfi 合成 |
| 抽帧素材 | 本地 `plugins/mm_gpt/frames/n{04,06,08,12,16}/` | ✅ |
| 本机 dashboard 雏形 | 本地 `dashboard/index.html` + README | ⚠️ 端口 3000 错（A agent 修） |

---

## 2. 子任务（agent 分配）

### 子任务 A：调用链路 demo（端到端）—— Opus agent in-flight

**目标**：本机浏览器同时看到「远端摄像头画面 + 极简 chat 页面（调 mm_gpt API）」。绕开 `openclaw_423` 默认 CMD。

**产出预期**：
- 远端 `/root/mmclaw/services/cam_stream/` —— Python+OpenCV 或 ffmpeg MJPEG 推流，监听 127.0.0.1:8080
- 远端 `/root/mmclaw/services/chat_proxy/` —— Python/Node 极简 chat HTTP server，监听 127.0.0.1:18789，转发到 orbitai/gpt-5.4
- 本机 `dashboard/index.html` 改用 :18789 + :8080
- 启动命令文档化（systemd-style 或 nohup）

**SSH tunnel 模板**：`ssh -L 18789:localhost:18789 -L 8080:localhost:8080 root@192.168.31.51`

**用户验证**：开 SSH tunnel + `python -m http.server 8000 --directory dashboard` + 浏览器开 http://localhost:8000

### 子任务 B：NPU 推理（本地视觉算力）—— Opus agent in-flight

**目标**：yolov8 跑在 Ascend 310B4 NPU 上，给后续 face/pose/scene 模型铺路。

**产出预期**：
- 远端 `/root/mmclaw/plugins/npu_infer/yolov8m.om` (atc 转换产物)
- 远端 `/root/mmclaw/plugins/npu_infer/npu_infer.py` (pyACL 推理)
- 远端 `/root/mmclaw/plugins/npu_infer/README.md` (**generic** 文档：CANN 环境 + onnx→om 通用模板 + acl 骨架 + 已知坑)

**fallback 已埋**：onnx opset 不兼容时试 11/12/13/15、onnx-simplifier、重 export from .pt。全失败停下报告。

**后续推理其他模型**：用同一 README 模板，改 input_shape 和后处理即可。

### 子任务 C：时效性体系调研 —— Sonnet agent in-flight

**目标**：体系性的延迟优化方案，针对模拟宠物的多模态多轮长 prompt 场景。

**产出预期**：`idea/latency_optimization.md`，覆盖 8 个方向：
1. Prompt caching（重点：orbitai 是否透传？怎么验证？）
2. Streaming
3. Speculative decoding
4. 多模态特定（图 token 减少、抽帧策略 cache）
5. Context engine 复用
6. 本地 + 远程混合架构
7. Prompt 工程
8. Agent 路由

末尾给 top 3 优先级 + TL;DR。

### 子任务 D：多模态 embedding 验证 —— Opus agent in-flight

**目标**：验证阿里百炼多模态 embedding 是否能做模拟宠物的 face / 场景记忆。

**产出预期**：
- `plugins/mm_embedding/{.env, .gitignore, probe.py, test_matrix.py, MM_EMBEDDING_RESULTS.md}`
- 测试矩阵：文-文 / 图-图 / 文图跨模态 / 人脸近似（如可获取测试图）
- 给 OpenClaw `extensions/pet-multimodal-embedding/` 实现伪代码

**关键问题（D 要回答）**：
- 阿里 mm embedding 适合做主人 face 识别吗？还是要专用 face embedding（FaceNet/ArcFace）？
- 适合做场景记忆吗？
- 维度多少？延迟多少？cost 多少？

---

## 3. 任务依赖与下一阶段

### 阶段 1：基础设施落地（当前）—— 4 agent 并行
A、B、C、D 完成后，主体（Claude）综合结果做路线决策：
- A 通了 → 用户能看到 demo
- B 通了 → 后续 NPU-2 接 face/pose 模型
- C 出来 → top 3 优化方向落地为具体改造任务
- D 出来 → 决定 OpenClaw embedding provider 怎么写（PET 改造的输入）

### 阶段 2：PET 改造（基础设施稳定后）

**PET-1：实体记忆 + 主人绑定**（依赖 D + A）
- 新建 `src/pet/entity-memory.ts`
- 改 `src/auto-reply/reply/session.ts`（initSessionState 加载 owner_profile）
- 改 `src/plugins/memory-state.ts`（记忆检索加 ownerId 过滤）
- 改 `src/auto-reply/reply/get-reply-run.ts`（System Prompt 追加 owner_profile）
- 用 D 的 embedding 给 owner_profile 做向量索引

**PET-2：极简状态机**（mood/affection/energy）
**PET-3：可学习技能**（learned-skills.json）
**PET-4：路线 ① 视频感知 extension**（参数已敲）
**PET-5：multimodal embedding provider**（D 结论落地）

### 阶段 3：高阶（P2/P3）

- 主动行为 policy 层
- 跨模态 grounding
- 边缘 daemon
- 隐私/评测体系
- 外设接入（plugins/ 屏幕表情、触手）

---

## 4. agent team 接手指南（重要）

每个未来 agent 任务**必须自包含**——不要写"参考之前对话"、"看上下文"等依赖人工脑补的内容。每个任务 prompt 至少含：

1. 项目背景（一段，自包含）
2. 远端访问信息（user/host/key 路径）
3. 共享约束（不可破坏的禁止项）
4. SSH 节奏控制（如涉及远端）
5. 工作目录边界（与其他 agent 不重叠）
6. 任务步骤
7. 失败 fallback 策略
8. 报告格式

参考 v0.2 `idea/plan_v0.2.md` 第 2 节的 4 个子任务 prompt 即可（实际 prompt 见对话历史，agent team 可向用户索取）。

---

## 5. 风险登记

| 风险 | 缓解 |
|---|---|
| 远端 sshd MaxStartups 触发 → SSH 不通 | 单连接 batch、累计 ≤ 15 次/任务、失败不重试 |
| 远端 load 高（曾飙 21+） | 任务前先 `uptime`，> 10 等几分钟；不要 `--restart unless-stopped` 配错误命令 |
| `openclaw_423` 默认 CMD 假设外部 cfg 完整 | 当前**绕开它**走 demo 通路；后期开发模式时再单独设计挂载策略 |
| orbitai 偶发 silent-drop（image_url 数组被静默丢） | 必须 `prompt_tokens < 200` sanity check + retry once |
| onnx opset 与 atc 不兼容 | 多 opset fallback 试，全失败停下报告（已写入 B agent prompt） |
| 队友共享机器 | 容器命名 `mmclaw-*`、不动 `openclaw_*` 镜像、SSH 节奏控制（队友也用同一台机） |

---

## 6. 当前任务状态（2026-04-25）

| Task ID | 子任务 | 状态 | Owner |
|---|---|---|---|
| 1 | A 端到端 demo | 🔄 in_progress | opus agent (a0d3be5141b9fe265) |
| 2 | B NPU yolov8 | 🔄 in_progress | opus agent (a505ffddc20c2703d) |
| 3 | C 时效性调研 | 🔄 in_progress | sonnet agent (ab7e8e078b9fe4388) |
| 4 | D 多模态 embedding | 🔄 in_progress | opus agent (a3936d6b0362adc7c) |
| 5 | 写 plan_v0.2 | ✅ 本文档 | Claude main |
| 6 | 综合结果决策 | ⏸ blocked by 1,2,3,4 | Claude main |
| 7 | PET-1 改造 | ⏸ blocked by 1,4,6 | TBD |

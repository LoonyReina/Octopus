# OpenClaw 4.23 多模态多轮对话端到端流程（未修改版）

> **目的**：在不改 OpenClaw 任何代码的前提下，模拟一次「用户发文+图+视频 → 模型回复 → 触发 skill/tool → 进入记忆 → 下轮自动召回」的完整流程，搞清楚每一步发生在哪个文件、做了什么决策。
>
> **来源**：Claude 实测代码，2026-04-24。
>
> **目标读者**：负责 mmclaw P0/P1 改造的 agent team。改造时知道「在哪插钩子」「不要破坏哪条线」。

---

## 流程图（ASCII）

```
                              ┌─────────────────┐
  外部 channel/UI 推消息 ──→ │  消息入口        │
  (whatsapp/slack/web/cli)    │  get-reply.ts   │
                              └────────┬────────┘
                                       │
                              ┌────────▼────────────────────────┐
                              │ session 初始化                    │
                              │ session.ts:initSessionState     │
                              │ - 读 session 历史                  │
                              │ - 加载 agent 配置/skill 列表       │
                              └────────┬────────────────────────┘
                                       │
                              ┌────────▼────────────────────────┐
                              │ 多模态附件归一化                   │
                              │ attachments.normalize.ts        │
                              │ - 读 ctx.MediaPath/Paths/Urls   │
                              │ - 识别 kind: image/audio/video  │
                              │   按 MIME + 后缀（.mp4/.png 等）│
                              └────────┬────────────────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
       (kind=image)              (kind=audio)             (kind=video)
              │                        │                        │
   ┌──────────▼─────────┐  ┌───────────▼──────────┐  ┌──────────▼───────────┐
   │ 双轨制图片处理      │  │ 转录 capability       │  │ 视频 capability       │
   │ run/images.ts:    │  │ runner.entries.ts:   │  │ runner.entries.ts:   │
   │ detectAndLoad...  │  │ → provider.transcribeAudio│ → provider.describeVideo│
   │                   │  │ (deepgram/openai/...)│  │ (google/moonshot/qwen)│
   │ 主模型有 vision?  │  │ → 文本转录             │  │ → 文本描述             │
   │ Y: 直接注入主 prompt│  │                       │  │                       │
   │ N: 调 image-tool  │  │                       │  │                       │
   │   → 文本描述       │  │                       │  │                       │
   └──────────┬─────────┘  └───────────┬──────────┘  └──────────┬───────────┘
              └────────────────────────┼─────────────────────────┘
                                       │
                              ┌────────▼────────────────────────┐
                              │ 主 prompt 组装                    │
                              │ get-reply-run.ts                │
                              │ - System Prompt = 默认提示词      │
                              │   + skill 列表（formatSkills...）│
                              │   + active-memory 召回片段        │
                              │   + 历史消息                      │
                              │   + 用户当前消息                  │
                              │   + 多模态文本/图片块             │
                              └────────┬────────────────────────┘
                                       │
                              ┌────────▼────────────────────────┐
                              │ active-memory 自动召回             │
                              │ extensions/active-memory/index.ts│
                              │ - 模型评估「当前消息需要什么旧记忆」│
                              │ - 对 memory-lancedb 做向量检索      │
                              │ - 把命中的片段塞进 system prompt  │
                              └────────┬────────────────────────┘
                                       │
                              ┌────────▼────────────────────────┐
                              │ 主 LLM 调用                       │
                              │ pi-embedded-runner/run/attempt.ts│
                              │ - detectAndLoadPromptImages     │
                              │   再次确认图片注入                │
                              │ - 流式调主模型                    │
                              └────────┬────────────────────────┘
                                       │
                              ┌────────▼────────────────────────┐
                              │ Tool 调用循环                     │
                              │ pi-embedded-runner/run/tools.ts │
                              │ - 模型决定调 skill/tool          │
                              │ - 执行 → 结果回流              │
                              │ - 循环直到模型不再调 tool         │
                              └────────┬────────────────────────┘
                                       │
                              ┌────────▼────────────────────────┐
                              │ 消息结束 hook                     │
                              │ pi-embedded-subscribe.handlers.  │
                              │   messages.ts:handleMessageEnd  │
                              │ - autoCapture：抓取重要信息        │
                              │ - 写入 memory-lancedb 向量索引     │
                              │ - 触发 dreaming（异步巩固）       │
                              └────────┬────────────────────────┘
                                       │
                              ┌────────▼────────────────────────┐
                              │ 回复递送                          │
                              │ reply-delivery.ts               │
                              │ → 回到外部 channel                │
                              └─────────────────────────────────┘
```

---

## Skill 与 Tool 的调度机制

### Skill 是什么
OpenClaw 里 skill 是 **markdown frontmatter 定义的可调用能力**，每个 skill 是一份带 metadata 的 .md 文档，模型通过工具调用（`Skill` tool 或类似）来执行。Skill 来自三类来源：
1. **bundled**（`extensions/*/skills/`）：插件自带
2. **plugin**（`resolvePluginSkillDirs`）：第三方插件注册
3. **user**（`~/.claude/skills/` 类似路径）：用户本地

加载入口：`src/agents/skills/workspace.ts:loadSkillsFromDirSafe`，会读 frontmatter（含 `name`/`description`/`when_to_use` 等），过滤合规性，按 agent 配置允许的范围筛选，最终通过 `formatSkillsForPrompt(skills)` 序列化为 system prompt 的一段「可用 skills」列表。

模型在主 LLM 阶段看到这段列表，决定要不要调用某个 skill；调用通过 tool call 协议（Skill 工具内部调度具体 .md）。

### Tool 是什么
Tool 是 OpenClaw 注册到主 LLM 的 function call。**Skill 是 Tool 的一种特化**——在主模型看来 skill 也是 tool，只是它的"实现"是去读那份 .md 并按指引执行。其他 tool 还有 `image`（独立图片理解）、`pdf`、`bash`、`read`/`write`、`web-fetch` 等。

注册入口：`src/agents/openclaw-tools.ts`，按当前 agent 的工具白名单和能力筛选后，写进 LLM payload 的 `tools` 字段。

### 调度时序
- LLM 一次回复中可能产出 0..N 个 tool call。
- `pi-embedded-runner/run/tools.ts` 拦截每个 call，路由到对应的 executor，结果作为 `tool` 角色消息回流。
- 模型可在下一轮基于 tool 结果继续调更多 tool，形成多轮 tool-calling 循环。

---

## 记忆系统

### 已有组件（`extensions/`）

| 组件 | 角色 | 备注 |
|---|---|---|
| `active-memory` | 短期/会话级活跃记忆。autoCapture（消息结束抓取重要信息）、autoRecall（每轮注入相关旧记忆） | 看 `active-memory/index.ts` 的 `recentUserTurns/Chars` 等参数 |
| `memory-core` | 核心记忆 SDK 与 manager runtime | API 提供方 |
| `memory-lancedb` | LanceDB 向量后端，用 OpenAI text-embedding-3-small（默认）做 chunk embedding | 真实向量索引 |
| `memory-wiki` | wiki 风格分类长期记忆 | skill 形式提供 SETUP/CONTRACT |

### Embedding pipeline
- 文本：进 `src/memory-host-sdk/host/embeddings.ts:createLocalEmbeddingProvider` 或远程 OpenAI 兼容 embedding。
- chunk → SQLite (`chunks` 表，含 embedding TEXT 字段) + LanceDB 向量库。
- FTS5（SQLite 全文检索）做关键词检索的备选。
- 检索时合并向量相似度 + 全文匹配。

### 关键现状：embedding API 已预留多模态接口
`src/memory-host-sdk/host/embedding-inputs.ts`：
```ts
export type EmbeddingInputInlineDataPart = {
  type: "inline-data";
  mimeType: string;
  data: string;  // base64
};
export type EmbeddingProvider = {
  embedQuery, embedBatch,
  embedBatchInputs?: (inputs: EmbeddingInput[]) => Promise<number[][]>;  // 多模态入口
};
```
**但**：当前所有 provider 实现（local node-llama-cpp、remote OpenAI 兼容）**都没实现 `embedBatchInputs`**，且默认 embedding 模型（`embeddinggemma-300m`、`text-embedding-3-small`）都是**纯文本模型**——架构允许塞图片，模型不接受。

---

## 多模态多轮对话举例（10 轮场景）

| 轮 | 用户输入 | OpenClaw 行为 | 记忆变化 |
|---|---|---|---|
| 1 | "你好" | 主 LLM 直接回复 | autoCapture: "首次问候" |
| 2 | "[图片] 这是我的猫" | image 注入主 prompt → 主 LLM 描述并记住 | 抓取「用户有只猫，特征 X」→ 向量化入库 |
| 3 | "教你「拜拜」要摇尾巴" | 主 LLM 把它当成普通对话回应 | autoCapture: "用户教了 拜拜=摇尾巴"（**但仅是文本记忆，不是结构化技能**） |
| 4 | "[视频] 我在跳舞" | video 路由到 google/moonshot/qwen `describeVideo` → 文本描述注入主 prompt | 视频内容文本入库 |
| 5 | "我家猫叫什么？" | autoRecall 命中第 2 轮记忆 → 注入 system → 主 LLM 据此回答 | - |
| 6 | "拜拜" | autoRecall 可能命中第 3 轮文本，但**模型不会真的"做"摇尾巴**——它只能"提到"摇尾巴。**这就是 idea_gpt_kimi.md 强调的「学会动作 ≠ 记住描述」的差异** | - |
| 7 | "[图片] 这个人是我妈" | image 注入主 prompt → 主 LLM 文字记住"图里有妈" | 抓取「用户的妈，外观特征 X」入库（**纯文本描述，下次发同一张照片不会重新匹配上**——因为 embedding 是文本的，不是图像的） |
| 8 | "[图片] 这个人是谁？"（同样的妈） | autoRecall 用当前消息文本（"这个人是谁"）做 query，**未必能召回第 7 轮**——因为：(a) 当前 query 是纯文本，(b) 旧记忆是描述性文本，(c) embedding 是文本嵌入，跨图片召回靠文本描述质量。**这就是图像记忆缺失的具体表现** | - |
| 9 | "刚才那个动作再做一次" | 主 LLM 看到 autoRecall 召回的"摇尾巴"片段，模型可能猜对，可能猜错——**指代消解全靠 LLM 推理，没有 grounding 模块** | - |
| 10 | "再见" | 正常结束 | dreaming 异步巩固今日记忆 |

---

## 改造时的「不要破坏」清单

P0 改造涉及的核心管道（详见 `idea_gpt_kimi.md` P0/P1），改的时候要保护下面这些：

1. **multipart skill 加载顺序**（`workspace.ts`）：bundled → plugin → user，新加的 `learned-skills.json` 应该在 user 之后或单独类别，不能覆盖 bundled。
2. **autoCapture/autoRecall 钩子时序**（`active-memory/index.ts`）：状态/技能更新一定要在 `handleMessageEnd` 之**后**触发，不要在消息中途改状态导致同一轮 capture 拿到不一致快照。
3. **多模态附件路由**（`attachments.normalize.ts` → `runner.entries.ts`）：不要直接拦截 video，而是注册新 capability 或新 provider，保持现有 google/moonshot/qwen 路径可用。
4. **System Prompt 拼装顺序**（`get-reply-run.ts`）：宠物状态/owner_profile/技能列表追加，不要替换原 system prompt（破坏 active-memory 的 promptStyle 模板）。

---

## 关键文件速查

| 关心什么 | 看哪里 |
|---|---|
| 消息进入路径 | `src/auto-reply/reply/get-reply.ts` |
| Session 初始化 | `src/auto-reply/reply/session.ts` |
| 多模态附件识别 | `src/media-understanding/attachments.normalize.ts` |
| 视频/音频调度 | `src/media-understanding/runner.entries.ts:640+` |
| 图片注入 | `src/agents/pi-embedded-runner/run/images.ts` |
| Prompt 组装 | `src/auto-reply/reply/get-reply-run.ts` |
| Skill 加载 | `src/agents/skills/workspace.ts` |
| Tool 注册 | `src/agents/openclaw-tools.ts` |
| 主 LLM 循环 | `src/agents/pi-embedded-runner/run/attempt.ts` |
| Tool 执行 | `src/agents/pi-embedded-runner/run/tools.ts` |
| 消息结束 hook | `src/agents/pi-embedded-subscribe.handlers.messages.ts` |
| 记忆 autoCapture/autoRecall | `extensions/active-memory/index.ts` |
| 向量索引 | `extensions/memory-lancedb/lancedb-runtime.ts` |
| Embedding SDK | `src/memory-host-sdk/host/embeddings.ts` + `embedding-inputs.ts` |

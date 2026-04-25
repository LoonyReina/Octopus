# OpenClaw 多模态能力分析（Kimi 调研版）

> 基于 OpenClaw 2026.4.24 版本源码的完整多模态能力梳理，用于指导模拟宠物系统的感知层设计。

---

## ⚠️ 订正（Claude 实测，2026-04-24）

读源码发现以下结论与本文档原结论冲突，**以下面订正为准**：

| 本文原结论 | 实际代码 | 影响 |
|---|---|---|
| 视频理解 ❌ 不支持，所有现有 Provider 均未实现 `describeVideo` | **3 家已实现生产级 `describeVideo`**：`extensions/google/media-understanding-provider.ts:163`（Gemini）、`extensions/moonshot/media-understanding-provider.ts:84`（Kimi）、`extensions/qwen/media-understanding-provider.ts:108` | 视频理解的 MVP 不需要新建 provider，配 cfg 路由到这三家任一即可 |
| `openai-compatible-video.ts` 是"预留"的视频请求体构造 | 是**已就绪的 helper 函数**，新 provider 可直接调用 | 写新 provider 时直接 import 用，无需自己构造 body |
| 音频内容理解"接口有，实现弱" | **8 家实现 `transcribeAudio`**：deepgram、elevenlabs、google、groq、mistral、openai、xai；anthropic 链路也有 | 模拟宠物的语音转文字管道有完整 provider 池可选 |
| STT/TTS 只在 UI 浏览器侧 | 后端有完整模块：`src/tts/`（provider registry）、`src/realtime-transcription/`（WebSocket session）、`src/realtime-voice/`（session runtime） | 不依赖浏览器也能跑 |
| 视频理解走「抽帧 → 图片理解 → 文本事件流」是"MVP/P1 推荐" | 仍然是**有效路线**，但定位变了：不是补"缺失"，而是补"本地化/隐私/可控抽帧"。最快 MVP 是路线 ⓪：直接配 google/moonshot/qwen 的 cfg | 路线选择多一条 |

### 真正的多模态空白（与模拟宠物相关）

- **声纹识别 / speaker diarization**：仅有转录，不出 speaker 标签。
- **环境音事件分类**：吠叫、关门声、脚步声等。
- **持续主动感知**：摄像头常开、麦克风监听、运动事件触发——OpenClaw 是请求式响应，无 daemon 拉流。
- **face embedding / 主人识别**：完全空白。
- **pose / action 专用识别**：可走"抽帧+视觉描述"弱替代，无专用模型集成。

### `model.input` capability 类型限制（本文未提）

`Array<"text" | "image">` —— `pi-embedded-runner/openrouter-model-capabilities.ts:62`。**字面量里没有 `"video"`/`"audio"`/`"document"`**。意味着「主对话模型直接吃视频」走不通，视频/音频/文档必须走独立 capability 管道（`describeVideo`/`transcribeAudio`/PDF），结果文本注入主 prompt。这不是 4.23 才有的限制，是 pi-ai 长期约定。

---

## 一、总体结论

OpenClaw 当前是 **"文本 + 图片 + 语音交互 + 视频生成"** 的多模态网关，但**不是"视频理解 + 音频内容分析"**的多模态感知系统。

对模拟宠物的影响：
- **MVP 可直接利用**：图片理解（识别主人发来的照片）、语音转文字（实时对话）。
- **必须自建/补齐**：视频理解（动作识别、手势识别）、声纹识别、环境音频事件检测。
- **已有接口可复用**：`MediaUnderstandingProvider` 和 `SpeechProvider` 接口设计良好，模拟宠物的感知层可以直接扩展这些已有契约，而不是另起炉灶。

---

## 二、各模态能力详表

| 模态 | 支持状态 | 实现方式 | 对模拟宠物的意义 |
|------|---------|---------|----------------|
| **文本** | ✅ 完全支持 | 核心能力，贯穿所有对话管道 | 基础对话 |
| **图片理解** | ✅ 支持 | `MediaUnderstandingProvider` 接口 + `detectAndLoadPromptImages` 自动注入 | MVP 可分析主人发送的照片 |
| **语音 → 文字（STT）** | ✅ 支持 | UI 层调用浏览器原生 `SpeechRecognition`（Web API） | MVP 可用，但仅限浏览器/支持环境 |
| **文字 → 语音（TTS）** | ✅ 支持 | UI 层调用浏览器原生 `SpeechSynthesis`（Web API） | 基础语音回复 |
| **实时语音对话** | ✅ 支持 | `RealtimeTalk` 模块，支持连接 OpenAI Realtime 等实时语音 Agent | 可实现与宠物的实时语音交互 |
| **音频内容理解** | ⚠️ 接口有，实现弱 | `MediaUnderstandingProvider` capabilities 包含 `"audio"`，但现有插件未实现成熟的音频分析（如环境音识别、声纹） | **缺失**：无法识别主人声纹、环境事件音 |
| **视频生成** | ✅ 支持 | `VideoGenerationProvider` 接口，支持文生视频、图生视频 | 与模拟宠物关系不大 |
| **视频理解** | ❌ 不支持 | `MediaUnderstandingProvider` capabilities 虽然在 manifest 层面预留了 `"video"`，但现有插件（anthropic、codex）均未实现视频内容分析、抽帧理解 | **关键缺失**：无法识别主人动作、手势、姿态 |
| **PDF/文档** | ✅ 支持 | Anthropic provider 支持 `nativeDocumentInputs: ["pdf"]` | 可用于读取文档类技能资料 |

---

## 三、实现模块精确位置

### 1. 文本（核心能力）
- 消息接收与路由：`src/auto-reply/reply/get-reply.ts`
- 会话执行管道：`src/agents/pi-embedded-runner/run.ts`
- 流处理与输出：`src/agents/pi-embedded-subscribe.ts` + `src/agents/pi-embedded-subscribe.handlers.messages.ts`

### 2. 图片理解（Image Understanding）

| 层级 | 文件路径 | 作用 |
|------|---------|------|
| **SDK 接口** | `src/plugin-sdk/media-understanding.ts` | `MediaUnderstandingProvider` 类型定义，`describeImage`/`describeImages` 通用实现 |
| **Provider 注册** | `src/media-understanding/provider-registry.ts` | Provider 发现与注册 |
| **调度运行器** | `src/media-understanding/runner.ts` | 构建 Provider Registry，执行媒体理解请求 |
| **默认配置** | `src/media-understanding/defaults.ts` | 自动解析默认媒体模型 |
| **Agent 工具** | `src/agents/tools/image-tool.ts` | Agent 可调用的图片分析工具 |
| **工具辅助** | `src/agents/tools/image-tool.helpers.ts` | 图片模型配置、DataURL 解码、响应解析 |
| **Prompt 图像注入** | `src/agents/pi-embedded-runner/run/images.ts` | `detectAndLoadPromptImages`：检测 prompt 中的图片引用并加载为 `ImageContent` |
| **Attempt 图像合并** | `src/agents/pi-embedded-runner/run/attempt.ts` | 将图片自动注入到主 LLM 的 payload 中 |
| **Anthropic 实现** | `extensions/anthropic/media-understanding-provider.ts` | Claude 图片理解实现，`capabilities: ["image"]` |
| **Codex 实现** | `extensions/codex/media-understanding-provider.ts` | Codex 图片理解实现，基于 App Server 线程 |
| **Manifest 契约** | `src/plugins/manifest.ts` | `MEDIA_UNDERSTANDING_CAPABILITIES = ["image", "audio", "video"]` |

### 3. 语音交互（Speech）

| 能力 | 文件路径 | 作用 |
|------|---------|------|
| **浏览器 STT** | `ui/src/ui/chat/speech.ts` | 调用浏览器原生 `SpeechRecognition` 进行语音转文字 |
| **浏览器 TTS** | `ui/src/ui/chat/speech.ts` | 调用浏览器原生 `SpeechSynthesis` 进行文字转语音 |
| **实时语音对话（UI）** | `ui/src/ui/chat/realtime-talk.ts` | WebSocket 实时语音会话，连接 OpenAI Realtime 等后端 |
| **实时语音后端** | `src/realtime-voice/session-runtime.ts` | 实时语音会话运行时管理 |
| **实时语音 Provider** | `src/realtime-voice/provider-registry.ts` | 实时语音 Provider 注册与解析 |
| **实时语音类型** | `src/realtime-voice/provider-types.ts` | 实时语音 Provider 类型定义 |
| **实时语音工具** | `src/realtime-voice/agent-consult-tool.ts` | 实时语音模式下 Agent 的 consult 工具 |

### 4. 视频生成（Video Generation）

| 层级 | 文件路径 | 作用 |
|------|---------|------|
| **类型定义** | `src/video-generation/types.ts` | `VideoGenerationProvider`、能力配置、请求/响应类型 |
| **运行时** | `src/video-generation/runtime.ts` | 视频生成调度、参数校验、Provider 选择 |
| **能力解析** | `src/video-generation/capabilities.ts` | 解析 Provider 支持的模式（text-to-video、image-to-video 等） |
| **Provider 注册** | `src/plugins/contracts/media-provider-registry.ts` → `src/plugins/contracts/registry.ts` | 视频/音乐生成 Provider 的契约注册 |
| **SDK 导出** | `src/plugin-sdk/video-generation.ts` | 供外部插件使用的视频生成类型 |

### 5. PDF/文档输入

| 文件路径 | 作用 |
|---------|------|
| `extensions/anthropic/media-understanding-provider.ts` | 声明 `nativeDocumentInputs: ["pdf"]`，支持 Claude 原生 PDF 理解 |

### 6. 音频内容理解（Audio Understanding）

| 层级 | 文件路径 | 现状 |
|------|---------|------|
| **接口层面** | `src/plugin-sdk/media-understanding.ts` | 类型系统支持 `capabilities: ["audio"]`，预留了 `transcribeAudio` 等扩展 |
| **实际实现** | 现有 extensions（anthropic、codex 等） | **均未实现成熟的音频内容分析**（如环境音识别、声纹识别、音乐分析） |

### 7. 视频理解（Video Understanding）

| 层级 | 文件路径 | 现状 |
|------|---------|------|
| **接口层面** | `src/plugins/manifest.ts` | `MEDIA_UNDERSTANDING_CAPABILITIES` 预留了 `"video"` |
| **OpenAI 兼容请求体** | `src/media-understanding/openai-compatible-video.ts` | 预留了 `buildOpenAiCompatibleVideoRequestBody`，支持 `type: "video_url"` + base64 视频注入 |
| **实际实现** | 所有现有 Provider | **均未实现**。没有视频抽帧、视频内容描述、时序事件检测功能 |

---

## 四、图像理解的「双轨制」机制

OpenClaw 处理图片有 **两条并行路线**，而非单一方式：

### 轨道 A：原生 VLM 多模态输入（优先路线）

当主对话模型本身支持 vision（如 GPT-4V、Claude 3、Gemini）时，图片**直接注入主 LLM 的 prompt**，由主模型原生同时处理视觉+语言。

关键代码：
```ts
// src/agents/pi-embedded-runner/run/attempt.ts
const imageResult = await detectAndLoadPromptImages({
  prompt: effectivePrompt,
  workspaceDir: effectiveWorkspace,
  model: params.model,  // 检查 model.input 是否包含 "image"
});
```

```ts
// src/agents/tools/image-tool.ts 注释明确说明：
// "images are auto-injected into prompts (see attempt.ts detectAndLoadPromptImages)"
// "If model has native vision, images in the prompt are automatically visible to you"
```

**特点**：
- 无需额外 API 调用，图片随主请求一起发送
- LLM 直接"看到"图片，理解最准确
- 依赖主模型是否支持 vision

### 轨道 B：独立 API 调用 → 返回文本 → 再喂给主 LLM（Fallback / 显式调用）

当主模型**不支持 vision**（如纯文本模型），或用户**显式调用 `image` tool** 时，OpenClaw 会走独立的媒体理解管道：

```
用户图片 → image-tool.ts → describeImageWithModel() → MediaUnderstandingProvider
                                                     ↓
                                            调用独立视觉模型（如 Claude/Codex）
                                                     ↓
                                            返回文本描述
                                                     ↓
                                            文本描述注入主 LLM 对话上下文
```

关键代码：
```ts
// src/media-understanding/image.ts
import { complete } from "@mariozechner/pi-ai";  // 独立模型调用
// 视觉模型返回 text，再交给主对话模型使用
```

**特点**：
- 主模型不需要支持 vision
- 额外消耗一次 API 调用
- 图片信息被压缩为文本，可能丢失细节
- `anthropic` 和 `codex` 插件都实现了这条路线

---

## 五、视频理解现状与技术路线建议

### 现状
OpenClaw **目前没有视频理解能力**。所有现有 Provider 的 `MediaUnderstandingProvider` 都只声明了 `capabilities: ["image"]`，没有 `"video"`。

### 预留的架构能力
`src/media-understanding/openai-compatible-video.ts` 已预留视频请求体构造：
```ts
export function buildOpenAiCompatibleVideoRequestBody(params: {
  model: string;
  prompt: string;
  mime: string;
  buffer: Buffer;
}) {
  return {
    model: params.model,
    messages: [{
      role: "user",
      content: [
        { type: "text", text: params.prompt },
        { type: "video_url", video_url: { url: `data:${params.mime};base64,...` } }
      ]
    }]
  };
}
```
这说明架构上预留了「原生 VLM 直接吃视频」的能力，但没有任何 provider 实际实现。

### 对模拟宠物的两条技术路线

| 路线 | 说明 | 适用阶段 |
|------|------|---------|
| **路线 ①：抽帧 → 图片理解 → 文本事件流** | 将视频按固定间隔抽帧，用现有图片理解基础设施（`describeImageWithModel`）处理每帧，拼接成结构化事件 `{timestamp, event_type, description}`。 | **MVP / P1 推荐**。复用 OpenClaw 全部现有基础设施，无需等原生视频 VLM 成熟。 |
| **路线 ②：原生 VLM 直接吃视频** | 直接将视频 base64 传给支持视频输入的模型（如 GPT-4o、Gemini）。 | **P2 可选**。依赖特定模型支持，延迟较高，但理解精度可能更好。 |

**推荐**：模拟宠物 MVP 采用**路线 ①**。原因：
1. 可以复用 `src/media-understanding/` 的全部现有代码
2. 抽帧后的图片可以直接走「轨道 A」（原生 VLM）或「轨道 B」（独立 API）
3. 时序信息由抽帧模块控制，便于与对话时间对齐（Grounding）
4. 不依赖特定模型的视频支持

---

## 六、关键洞察

1. **OpenClaw 的多模态是「网关式」而非「感知式」**：它擅长把用户上传的媒体转发给模型处理，但缺乏持续的、主动的感知能力（如摄像头常开、麦克风监听、环境事件检测）。

2. **图片处理最成熟**：有完整的「自动注入 + Tool 调用」双轨制，这是模拟宠物可以立即复用的部分。

3. **语音是 UI 层能力**：STT/TTS 目前只在浏览器 UI 中实现（`speech.ts`），网关核心（`src/auto-reply/` 管道）本身不处理原始音频流，只处理转好的文本。如果要实现声纹识别，需要在 UI 层或新增感知层处理原始音频。

4. **视频是最大空白**：没有任何实现，但架构预留了扩展点。模拟宠物的视频理解（动作识别、手势识别）需要完全新建。

---

*调研日期：2026-04-24*  
*基于版本：openclaw@2026.4.24*  
*核心调研文件：`src/media-understanding/image.ts`、`src/agents/pi-embedded-runner/run/images.ts`、`src/agents/tools/image-tool.ts`、`src/media-understanding/openai-compatible-video.ts`*

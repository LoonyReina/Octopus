# LLM 推理时效性优化：模拟宠物多模态多轮对话场景

> **版本**：v1.0  
> **日期**：2026-04-24  
> **依据**：OpenClaw 4.23 实测代码 + plan_v0.1.md MM-1.5 实测数据 + 公开 API 文档

---

## 当前瓶颈分析

### 实测数字

根据 MM-1.5 实测（`plan_v0.1.md` 第 117-119 行）：

| 项目 | 数值 |
|------|------|
| 端到端延迟（一轮） | ~7s |
| N=6 帧下 prompt tokens | ~2249 prompt + ~198 output |
| 图片来源 token | 6 张 768px JPEG ≈ 6×370 = **~2200 tokens**（图片占比 ~98%）|
| 纯文本 system | 约 300–800 tokens（随会话增长） |

### token 构成估算（一轮完整输入）

```
system_prompt:
  - 基础指令 + 工具描述     ~400 tokens（基本固定）
  - owner_profile           ~200 tokens（缓慢增长）
  - pet_state               ~50 tokens（每轮变化）
  - learned_skills 列表     ~300 tokens（缓慢增长，学新技能时增加）
  - 召回的记忆片段           ~200–800 tokens（每轮变化，按需召回）
---
system 小计:              ~1150–1750 tokens

user turn (N=6 图):        ~2200 tokens（图片固定大头）
历史对话 (轮数×长度):     ~0–4000 tokens（随会话增长，直至 compaction）
---
总计:                      ~3350–7950 tokens/轮
```

### 瓶颈性质

1. **TTFT（Time To First Token）瓶颈**：orbitai/gpt-5.4 需要处理整个 prefill（尤其是大量图片 token）后才开始生成，输入越大 TTFT 越长。
2. **会话增长效应**：system prompt 随技能学习、记忆召回而增长，每轮 TTFT 会逐渐恶化。
3. **不可控代理层**：orbitai 是第三方代理，无法直接控制模型推理基础设施，无法使用 speculative decoding 等服务端手段。
4. **orbitai 静默丢图问题**：已知 orbitai 偶发静默丢弃 `image_url` 数组（`plan_v0.1.md` 关键发现），这也导致 retry 开销。

---

## 方向一：Prompt Caching（最重要，prefill 复用）

### 原理

Prompt Caching（也称 Prefix Caching）的核心思想：LLM 服务端把 KV Cache 中已计算过的前缀保存在内存或磁盘中，下一次请求如果具有相同前缀，直接复用缓存的 KV，跳过这部分 prefill 计算。结果是被缓存的部分 token 的处理成本接近零，TTFT 显著下降。

"prefill"在用户原话中正是指这个机制——把不变的 system prompt 前缀预先填充到 KV Cache，避免重复计算。

### 各平台实现

#### Anthropic 的 prompt caching

- **机制**：在请求 JSON 的 content block 上加 `"cache_control": {"type": "ephemeral"}` 断点（breakpoint）。Claude 会缓存该断点之前的所有内容，TTL 默认 5 分钟（`short`），最长 1 小时（`long`）。
- **最低 token 要求**：Claude Sonnet/Haiku 系列最少 1024 tokens 才触发缓存，Opus 系列最少 2048 tokens。
- **费用**：写入缓存额外收 25% 费用，命中缓存读取收原始输入价格的 10%（大约 -90% 成本）。
- **OpenClaw 实现**：代码中已有完整支持（`src/agents/pi-embedded-runner/anthropic-cache-control-payload.ts`、`anthropic-family-cache-semantics.ts`、`prompt-cache-retention.ts`），通过 `cacheRetention: "short" | "long"` 参数控制。
- **参考**：[Anthropic Prompt Caching 文档](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)

#### OpenAI / openai-compatible 的 prompt caching

- **机制**：OpenAI 对 GPT-4o、GPT-4o mini、o1、o3 等系列**自动**开启前缀缓存（Automatic Prompt Caching），无需客户端显式标记断点。只要请求前缀（system + 历史消息）与之前的请求相同，服务端自动命中缓存。
- **最低 token 要求**：1024 tokens，以 128 token 为粒度递增。
- **命中条件**：请求必须发到**同一模型**、同一组织、且前缀字节完全一致（包括空格和换行）。
- **费用**：命中缓存的 token 收原始输入价格的 50%（如 gpt-5.4 的 `cacheRead: 0.25` vs `input: 2.5` 美元/百万 token）。OpenClaw 代码中已定义：`OPENAI_GPT_54_COST = { input: 2.5, output: 15, cacheRead: 0.25, cacheWrite: 0 }`（`extensions/openai/openai-provider.ts` 第 38 行）。
- **验证方式**：检查响应 `usage` 字段中的 `cached_tokens`（openai completions API）或 `prompt_tokens_details.cached_tokens`。
- **参考**：[OpenAI Prompt Caching 文档](https://platform.openai.com/docs/guides/prompt-caching)

#### 阿里通义 / DashScope 的 prompt caching

- **机制**：通义千问系列（qwen-long、qwen-max 等）支持 Context Cache，通过 `POST /caches` 接口预先上传长文本内容并获得 `cache_id`，在后续请求的 `messages` 中以 `{"role": "system", "content": [{"type": "text", "text": "...", "cache_id": "xxx"}]}` 引用。
- **优势**：cache_id 有效期可达 24 小时，特别适合长文档/长 system prompt 场景。
- **费用**：缓存命中时输入 token 费用约为原价的 10%，类似 Anthropic。
- **参考**：[DashScope Context Cache 文档](https://help.aliyun.com/zh/model-studio/developer-reference/context-cache-feature)

### 在我们场景的可用性

**关键问题：orbitai 代理是否透传 cache？**

这是最核心的不确定点。orbitai 是 OpenAI 兼容协议的代理网关，位于客户端和真实 OpenAI 模型之间。以下几种情况均可能：

1. **透明代理（最佳）**：orbitai 直接把请求转发给 OpenAI，OpenAI 的自动前缀缓存正常工作，`usage.prompt_tokens_details.cached_tokens` > 0。
2. **非透明代理（失效）**：orbitai 在转发时修改了请求（如重新序列化 JSON、添加/删除 header），导致前缀不匹配，缓存每次失效。
3. **自建推理（未知）**：如果 orbitai 使用自建的 gpt-5.4 推理集群，是否实现了 KV Cache 前缀复用取决于其基础设施。

**验证方法**：

```python
# 发两次相同 system prompt + 相同历史的请求（第二次追加一条新消息）
# 检查 usage 对象
import openai
client = openai.OpenAI(base_url="https://orbitai代理地址", api_key="...")

def check_cache(messages):
    resp = client.chat.completions.create(
        model="gpt-5.4",
        messages=messages,
        stream=False
    )
    usage = resp.usage
    print(f"prompt_tokens: {usage.prompt_tokens}")
    print(f"completion_tokens: {usage.completion_tokens}")
    # 关键字段（OpenAI API）
    if hasattr(usage, 'prompt_tokens_details'):
        print(f"cached_tokens: {usage.prompt_tokens_details.cached_tokens}")

# 第一次调用（建立缓存）
msgs = [
    {"role": "system", "content": "你是一只模拟宠物..." * 200},  # 确保 >1024 tokens
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "汪汪！"},
    {"role": "user", "content": "第二轮"}
]
check_cache(msgs)
# 第二次调用（检查 cached_tokens 是否 > 0）
msgs.append({"role": "assistant", "content": "好的！"})
msgs.append({"role": "user", "content": "第三轮"})
check_cache(msgs)
```

如果第二次 `cached_tokens` > 0，说明缓存在工作。如果始终为 0 或字段不存在，说明 orbitai 没有透传缓存。

### System Prompt 设计原则（最大化缓存命中）

无论哪家平台，前缀越稳定，缓存命中率越高。**黄金法则：把变化少的内容放最前面，把变化多的内容放最后面**。

```
system_prompt 排列顺序（从最不变到最常变）：

[1] 基础行为指令（纯静态）
    "你是一只名叫 xxx 的模拟宠物，..."
    工具使用规范、输出格式要求

[2] owner_profile（慢变，仅主人更新时改变）
    "主人姓名: Reina，偏好: 喜欢猫，历史: ..."

[3] learned_skills 列表（慢变，学新技能时才改变）
    "已学会技能: 握手(confidence:0.9), 转圈(0.7), ..."

[4] pet_state（每轮变化，但变化量小）
    "当前状态: mood=开心, affection=85, energy=70"

[5] 召回的记忆片段（每轮变化，内容差异大）
    "相关记忆: [2026-04-20 教会了握手]..."

[6] 最新用户消息 / 历史对话（每轮必变）
    messages 数组
```

在 Anthropic API 中，把 `cache_control` breakpoint 加在第 [3] 段末（技能列表末尾），让 [1]+[2]+[3] 这一大块稳定前缀进缓存。在 OpenAI API 中只需保证前缀字节顺序不变即可自动触发。

### 实操建议

**OpenAI 兼容（orbitai/gpt-5.4）**：

```python
# 构建 system prompt 时遵守顺序原则
# 每轮把新记忆、状态追加到末尾，不改动前面的内容
system_parts = [
    STATIC_BEHAVIOR_INSTRUCTIONS,      # 完全不变
    format_owner_profile(owner),       # 主人改了才变
    format_skills(learned_skills),     # 学新技能才变
    format_pet_state(pet_state),       # 每轮更新
    format_recalled_memories(memories) # 每轮变化最大
]
system_prompt = "\n\n".join(p for p in system_parts if p)

# 检查 usage 里的 cached_tokens
response = client.chat.completions.create(
    model="gpt-5.4",
    messages=[{"role": "system", "content": system_prompt}, ...],
    stream=False
)
cached = getattr(response.usage.prompt_tokens_details, 'cached_tokens', 0)
print(f"Cache hit: {cached}/{response.usage.prompt_tokens} tokens")
```

**Anthropic（若切换到 claude）**：

```python
# 使用 cache_control breakpoint
messages_payload = {
    "system": [
        {"type": "text", "text": static_core + owner_profile + skills},
        {"type": "text", "text": pet_state + memories,
         "cache_control": {"type": "ephemeral"}}  # 在此打断点
    ],
    "messages": history + [{"role": "user", "content": current_msg}]
}
```

### 预期收益

- 若 orbitai 透传 OpenAI 缓存，system 部分（~1150–1750 tokens）中的前缀（~750 tokens 稳定部分）可每轮命中，**TTFT -15%~-30%**，成本 -10%~-20%。
- 随会话增长，system 越来越长，绝对节省 token 数越来越多，收益递增。
- 若 orbitai 未透传，需通过路由层（方向八）绕过代理，或协商 orbitai 支持缓存透传。

---

## 方向二：Streaming（感知延迟优化）

### 原理

Streaming（流式传输）不减少总延迟，但把**感知延迟从"等待全部完成"变为"等待首 token"**。用户看到第一个字出现，就感知到系统在"思考/回应"了，心理延迟大幅降低。SSE（Server-Sent Events）是 HTTP 上的流式协议；chunked transfer encoding 是另一种方式。对于 ~7s 的端到端延迟，如果 TTFT 能降到 1-2s，用户感知的"响应速度"会提升 3-4 倍。

### OpenClaw 现有 streaming 处理

OpenClaw 已全面使用流式推理，核心在：

- `src/agents/pi-embedded-runner/run/stream-wrapper.ts`：`wrapStreamObjectEvents` 函数，包装异步 stream iterator，在每个事件上触发回调。
- `@mariozechner/pi-ai` 的 `streamSimple`：底层流式调用函数，`extra-params.ts` 通过 `createStreamFnWithExtraParams` 包装 transport/cacheRetention 等参数。
- 流式回复通过 `runs.ts`、`run.ts` 中的 session 管道传递，最终通过 channel 投递到 UI。
- transport 参数支持 `sse`、`websocket`、`auto`（`extra-params.ts`）。

流式推理在 OpenClaw 内部是**默认开启**的，不需要额外改造。问题是：

1. 流输出到哪个 UI？UI 是否实现了流式渲染（逐字显示）？
2. 宠物的回复渲染方式是否体现出"宠物感"？

### 模拟宠物 chat UI 的"快"感知设计

即使总延迟不变，好的流式 UI 设计可以让用户感觉"快"：

**策略一：立即显示"宠物在思考"的状态**（TTFT 前）
```
用户发消息 → 立即显示 "🐾 mmm~（思考中）" 动画
→ 首 token 到达 → 替换为真实流式文字
```

**策略二：逐字渲染模拟宠物说话节奏**
- 普通 AI 回复按 chunk 显示，宠物可以按字符逐个显示，配合模拟"打字机"效果，甚至加入随机短暂停顿模拟喘气，反而让较慢的响应看起来更有生命感。

**策略三：分层回复**（先情绪后内容）
```
[首 1-2 token] 情绪表达：汪汪！/ 呜~ / 哎？
[后续] 实际行动/回答内容
```
让 LLM 的 system prompt 要求**先输出情绪词**，这样 TTFT 后立刻有可见内容，感知延迟最低。

**策略四：预测性 UI**（本地小模型或规则）
对于明显的简单互动（"坐下"、"摇摇尾巴"），本地立即触发宠物动画，同时异步发请求给 gpt-5.4 确认/补充文字回复。

### 实操建议

```python
# 使用 stream=True，立即处理首 token
response = client.chat.completions.create(
    model="gpt-5.4",
    messages=messages,
    stream=True
)

first_token_received = False
for chunk in response:
    delta = chunk.choices[0].delta.content or ""
    if delta and not first_token_received:
        first_token_received = True
        # 停止"思考中"动画，开始显示文字
        ui.stop_thinking_animation()
    ui.append_text(delta)
    # 对每个字符增加微小延迟模拟宠物打字节奏（可选）
    if is_cjk_char(delta):
        time.sleep(0.03)
```

### 预期收益

- 感知延迟（用户主观体验）降低 **40%–60%**，即使总端到端延迟不变。
- 配合"先输出情绪词"策略，首可见内容时间可压缩到 1.5–2.5s（当前 TTFT 约 3–5s）。

---

## 方向三：Speculative Decoding / 草稿模型

### 原理

Speculative Decoding 用一个小（快）模型先生成 N 个 draft token，再由大模型一次性批量验证。如果大多数 draft token 被接受，则整体吞吐量提升 2–3 倍，同时保持大模型的生成质量。典型实现：Meta 的 Medusa、Google 的 SpecDec。

### 对 OpenAI 兼容协议的适用性

**结论：在 orbitai/gpt-5.4 场景下，基本无法使用。**

- OpenAI API 不暴露 speculative decoding 的任何控制参数，它是纯服务端实现细节。
- orbitai 作为代理，更无法控制其后端是否使用 speculative decoding。
- 即使 gpt-5.4 内部使用了某种 draft 机制，客户端也无法感知或调优。

### 我们能做的有限替代

**客户端层面可以模拟 speculative 的思路**：

1. **本地轻量模型预生成情绪响应**：用本地小模型（如 NPU 上的 Gemma-2B 或 Qwen-1.5B）对用户输入生成"初步情绪反应"（汪！/呜~/哎？），立即显示给用户，同时异步发给 gpt-5.4 生成完整回复。gpt-5.4 完成后，如果与本地预测方向一致则平滑替换，否则显示完整回复（小小的"更正"）。

2. **模板化快速响应**：对于常见指令（"坐下"、"摇尾巴"、"吃饭"），本地维护响应模板，立即执行动作和显示反应，gpt-5.4 只用于处理新颖/复杂输入。

### 预期收益

- speculative decoding 本身：**不适用**（外部 API 不可控）。
- 客户端预响应策略：简单互动感知延迟 **-60%**（本地立即响应）；但只适用于有限交互集合。

---

## 方向四：多模态特定优化

### 图片 token 数量是最大瓶颈

根据 MM-1.5 实测，N=6 帧 768px JPEG 贡献约 2200 prompt tokens，占总输入的 60%–80%。这是最值得优化的单点。

#### 图片分辨率降采样

OpenAI vision token 计算公式（tile-based）：
- 图片先缩放到 **最长边不超过 2048px**，再按 **512×512 tile** 分割
- 768×768 → 2 tiles 行 × 2 tiles 列 = 4 tiles + 1 base = **5 tiles ≈ 765 tokens/图**
- 512×512 → 1 tile × 1 tile = 1 tile + 1 base = **2 tiles ≈ 255 tokens/图**
- 384×384 → 同 512（会 pad 到 tile 边界）≈ **255 tokens/图**

| 分辨率 | tokens/图 | N=6 总 tokens | 节省 |
|--------|-----------|--------------|------|
| 768×768（当前） | ~765 | ~2200 | 基准 |
| 512×512 | ~255 | ~765 | **-65%** |
| 384×384 | ~255 | ~765 | **-65%** |
| `detail=low` 强制 | 85 | ~510 | **-77%** |

降到 512px 后，每轮图片 token 从 ~2200 降到 ~765，总 prompt token 从 ~3500 降到 ~2100，理论 TTFT 降低 **30%–40%**（假设处理时间与 token 数正相关）。

**图片质量代价**：512px 对于识别动作、手势、表情已经足够，宠物场景不需要阅读文字或识别小细节。建议先用 512px 测试质量，不满意再回调。

#### `detail=low` 模式

OpenAI vision API 的 `detail` 参数：
```python
{"type": "image_url", "image_url": {"url": "...", "detail": "low"}}
```
强制 low 模式只用 85 tokens/图，但图片被缩放到 512×512 且降采样更激进。对于"识别主人情绪/动作/场景"的宠物用途，low detail 通常已经足够。

#### 多图共享 visual encoder 缓存？

OpenAI API 不暴露这一能力。理论上 vision model 处理 N 张不相关图片可以并行，但这是服务端实现细节，客户端无法控制。

#### 替代方案：vision → text description 缓存

这是最有潜力的架构级优化：

**方案**：用一个便宜/快速的 vision 模型（如 `gpt-5.4-mini` 或本地 MiniCPM-V）先把 6 帧图片转成结构化文本描述，然后把文本描述缓存，后续不再重复发图片 token 给主模型。

```
图片 → [vision小模型] → 结构化描述
  "{scene: 客厅, owner_action: 举起右手, owner_expression: 微笑,
    pet_focus: 主人手部, timestamp_relative: 3s}"
  → 写入当轮 context（~80 tokens，替代 ~365 tokens）
```

优势：
1. 主模型 payload 从 ~2200 视觉 tokens 降到 ~480 文本 tokens（N=6 × 80）
2. 文本描述可以进入 prompt caching（图片 base64 data 无法缓存）
3. 小模型 gpt-5.4-mini 的 vision 能力对场景描述已够用，成本远低于 gpt-5.4

代价：
- 增加一次 vision 小模型 API 调用（约 0.5–1s 额外延迟）
- 可与主模型调用**并行化**（先发 vision 小模型，同时准备主模型请求除图片外的其余部分，vision 回来后合并发送）

#### 视频抽帧 cache 策略：前 N-1 帧 + 末帧新增

若 orbitai 支持 prefix caching，且图片在 messages 中以稳定的顺序出现（不变顺序不变内容），则：
- 每轮视频：前 5 帧与上一轮完全相同 → 可命中缓存（但 OpenAI 图片 cache 需要相同 base64）
- 仅末帧是新内容

**现实问题**：每次抽帧可能因 ffmpeg 参数微小差异导致 base64 不完全一致，缓存不命中。需要确保抽帧结果在 byte 级别可重复（固定质量参数、固定输出格式）。

### 实操建议

```python
# 1. 降采样：在发送前缩放图片到 512px
from PIL import Image
import io, base64

def resize_frame(img_bytes: bytes, max_dim=512) -> str:
    img = Image.open(io.BytesIO(img_bytes))
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()

# 2. 使用 detail=low（更激进，但 token 最少）
image_content = {
    "type": "image_url",
    "image_url": {
        "url": f"data:image/jpeg;base64,{frame_b64}",
        "detail": "low"   # 强制 low: 85 tokens/图
    }
}

# 3. vision→text pipeline（异步并行）
import asyncio

async def get_vision_description(frames: list[str]) -> str:
    """用小模型把图片转文本描述"""
    resp = await client.chat.completions.create(
        model="gpt-5.4-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "请用JSON描述这6帧图片中的场景和动作: "
                 "{scene, owner_action, owner_expression, notable_objects}"},
                *[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{f}",
                   "detail": "low"}} for f in frames]
            ]
        }],
        max_tokens=300
    )
    return resp.choices[0].message.content

async def run_pet_turn(system_prompt, history, frames):
    # 并行：vision描述 + 准备其他
    vision_task = asyncio.create_task(get_vision_description(frames))
    # ... 其他准备工作 ...
    vision_text = await vision_task
    # 主模型只收文本描述，不收图片
    messages = [{"role": "system", "content": system_prompt}] + history
    messages.append({"role": "user", "content": f"[视频描述]: {vision_text}\n用户说: ..."})
    return await client.chat.completions.create(model="gpt-5.4", messages=messages, stream=True)
```

### 预期收益

| 优化手段 | TTFT 改善 | 成本改善 |
|---------|----------|---------|
| 降采样 768→512px | -30%–40% | -65% 图片 token 成本 |
| `detail=low` | -40%–50% | -77% 图片 token 成本 |
| vision→text 替代 | -40%–55%（主模型 prefill 大减）| 图片成本 -70%，增加 mini 调用成本 |

---

## 方向五：Context Engine 复用

### OpenClaw context-engine 现有能力

`src/context-engine/` 是 OpenClaw 的上下文管理层，核心功能：

- **compaction**：当 token 数接近上下文窗口上限时，触发压缩（摘要化历史对话）。实现在 `delegate.ts` 中的 `delegateCompactionToRuntime`，调用 `pi-embedded-runner/compact.runtime.ts`。
- **dedup/compression**：通过 `normalizeStructuredPromptSection`（`prompt-cache-stability.ts`）对 prompt 文本做标准化（去除尾部空格、统一换行），确保 byte 级稳定性（对 prefix caching 至关重要）。
- **缓存观测**：`prompt-cache-observability.ts` 有完整的缓存命中/失效追踪逻辑（`beginPromptCacheObservation` / `completePromptCacheObservation`），追踪 `systemPromptDigest`、`toolDigest` 等，当检测到缓存大幅下降时记录 `PromptCacheBreak`。

### 模拟宠物改造时与 context-engine 的配合

**问题**：默认 compaction 会把历史对话压缩成摘要，**很可能把宠物状态、技能列表摘要掉**，导致丢失。

**保护策略**：

1. **标记"保护区"**：在 compaction 实现中（`compact.runtime.ts`），识别带有特定标记（如 `<!-- PET_STATIC_SECTION -->`）的 system prompt 块，compaction 时跳过这些块，只压缩 messages 部分。

2. **分离存储**：把 owner_profile、skills、pet_state 存在独立的持久化文件（`src/pet/` 目录下），不存在 session history 里。每轮开始时重新注入到 system prompt 的前缀，这样即使历史被 compact，这些信息也不受影响。

3. **利用 normalizeStructuredPromptSection**：已有的 `normalizeStructuredPromptSection` 函数确保 prompt 文本的 byte 稳定性，模拟宠物的 system prompt 构建也应通过这个函数做标准化，以最大化 prefix cache 命中率。

4. **context-engine 缓存层**：目前 context-engine 没有独立的"context 缓存层"（不同于 KV Cache），主要靠 compaction 节省 token。但通过 prefix caching（方向一）可以在服务端实现事实上的缓存复用。

### 实操建议

```python
# 构建系统提示时使用稳定的序列化：
# 1. 对静态部分使用 frozen format（字段顺序固定，不随 dict 顺序变化）
# 2. 避免在静态内容中注入时间戳等变动值

def build_system_prompt(owner, skills, state, memories):
    # 静态部分（按字段名排序序列化，保证 byte 稳定）
    static_section = f"""# 宠物基础设定
{STATIC_BEHAVIOR}

# 主人档案
姓名: {owner['name']}
偏好: {', '.join(sorted(owner['preferences']))}
互动历史摘要: {owner['summary']}

# 已学会技能
{format_skills_stable(skills)}"""
    # 动态部分（每轮变化，放最后）
    dynamic_section = f"""# 当前状态
{format_state(state)}

# 召回的相关记忆
{format_memories(memories)}"""

    return static_section + "\n\n" + dynamic_section

def format_skills_stable(skills):
    # 技能列表按 id 排序，保证顺序稳定
    return "\n".join(
        f"- {s['name']} (confidence:{s['confidence']:.1f}, trigger:{s['trigger']})"
        for s in sorted(skills, key=lambda x: x['id'])
    )
```

### 预期收益

- 防止 compaction 破坏宠物核心信息，**避免体验断档**。
- 与 prefix caching 配合，稳定化 system prompt 前缀，间接提升缓存命中率。
- 不直接减少延迟，但防止了因 compaction 触发额外 LLM 调用导致的延迟峰值。

---

## 方向六：本地 + 远程混合（架构层）

### 原理

"边缘低延迟层"的思路（来自 `idea_gpt_kimi.md` P3）：把响应延迟敏感的简单任务放到本地完成，只把真正需要 gpt-5.4 能力的复杂任务发往远程。远端 NPU（华为 Atlas，cann 7.0 + npu 23.0）是天然的加速载体。

### 可在本地做的事

| 任务 | 工具 | 延迟 | 说明 |
|------|------|------|------|
| 唤醒词检测 | 规则/小模型 | <10ms | "坐下"、"过来"等 |
| 人脸快速匹配 | face embedding（本地 NPU） | <50ms | 确认是谁在跟宠物互动 |
| 情绪预响应 | 规则映射或本地 1B 模型 | <200ms | 简单互动立即触发动作 |
| 意图分类 | BERT/tiny-llm NPU | <100ms | 路由决策（简单/复杂） |
| TTS 合成 | 本地 TTS（已有 `src/tts/`）| <500ms | 宠物声音输出 |

**重点**：`plan_v0.1.md` 已确认远端 NPU 环境可用（NPU-1 任务），一旦 yolov8 NPU 推理通路打通，可以复用这套流程在 NPU 上跑轻量 LLM（Qwen-1.5B 等）。

### 常见简单互动本地处理

```
触发词匹配规则（本地，<5ms）：
  "坐下" → 立即触发坐下动画 + 播放"汪"音效
  "摇尾巴" → 立即触发摇尾动画
  "吃饭" → 立即触发进食动画
  "好狗狗" → affection+1，立即播放开心叫声

→ 同时异步发 gpt-5.4 请求生成文字回复补充内容
→ 如果 gpt-5.4 2s 内未回复（低优先级），直接用本地模板
```

### NPU 本地小模型推理（B agent 跑通后）

等 NPU-1（yolov8 NPU 推理）完成后，可以在同一 NPU 上部署：
- **意图分类模型**：Qwen-1.5B INT4，~50ms/推理，用于判断"这条消息需要 gpt-5.4 吗？"
- **简单对话模型**：Qwen-1.5B SFT（用宠物对话微调），~500ms/推理，处理日常简单互动
- **face embedding**：本地运行，不发往任何外部 API

### 实操建议

```python
# 本地路由决策器（规则 + 可选小模型）
SIMPLE_TRIGGERS = {
    "坐下": "action:sit",
    "站起来": "action:stand",
    "摇尾巴": "action:wag",
    "握手": "action:shake_hand",
    "好狗狗": "reward:positive",
    "坏狗狗": "reward:negative",
}

def route_message(text: str, has_images: bool) -> str:
    """返回 'local' 或 'remote'"""
    if has_images:
        return "remote"  # 含图片 → 必须远程
    stripped = text.strip()
    if stripped in SIMPLE_TRIGGERS:
        return "local"   # 简单触发词 → 本地
    if len(stripped) > 50:
        return "remote"  # 长消息 → 远程
    # 可接入本地意图分类模型（NPU）
    return "remote"      # 默认远程（保守策略）

async def handle_message(text, images):
    route = route_message(text, bool(images))
    if route == "local":
        # 立即响应 + 异步发远程（可选，用于记忆更新）
        immediate_response = execute_local_response(SIMPLE_TRIGGERS[text.strip()])
        asyncio.create_task(update_remote_memory(text, immediate_response))
        return immediate_response
    else:
        return await call_gpt54(text, images)
```

### 预期收益

- 简单互动（约占日常互动的 30%–50%）响应时间从 ~7s 降到 **<500ms**（本地规则）或 **<1s**（本地小模型）。
- 减少约 30% 的远程 API 调用，降低成本和 token 消耗。
- 主人识别（face/voice）本地完成，隐私更好，无需上传生物特征到外部服务。

---

## 方向七：Prompt 工程

### 紧凑序列化格式

不同序列化格式在 token 效率上有显著差异：

| 格式 | owner_profile 示例 | 估计 tokens |
|------|---------------------|------------|
| JSON（verbose） | `{"owner": {"name": "Reina", "preferences": ["cats", "coffee"]}}` | ~25 |
| YAML（简洁） | `owner:\n  name: Reina\n  prefs: [cats, coffee]` | ~18 |
| 自定义紧凑格式 | `OWNER:Reina PREF:cats,coffee MOOD:friendly` | ~12 |
| 自然语言描述 | `主人叫Reina，喜欢猫和咖啡，性格友好。` | ~15（中文） |

**建议**：对技能列表这类结构化、规律性强的数据，使用紧凑格式；对 owner_profile 这类需要模型理解语义的内容，保留自然语言或 YAML，不要过度压缩（影响模型理解质量）。

```
# 紧凑技能列表格式（对比 JSON 节省 40%–50%）
SKILLS:
握手|conf=0.9|trig=来握手
转圈|conf=0.7|trig=转一圈
趴下|conf=0.8|trig=趴下
```

### 记忆召回策略：按需 vs 自动

**当前方案（自动注入）**：每轮自动把 top-K 相关记忆塞进 system prompt，无论是否真正需要。

**问题**：
1. 召回 k 条记忆 × 每条 50–100 tokens = 200–800 tokens 额外输入，即使这轮只是在聊天。
2. 随会话增长，记忆库变大，召回内容越来越多。

**按需召回（Agent 决定调 memory tool）**：

```
system_prompt:
"你有一个 search_memory(query) 工具，当你需要回忆与主人的过去互动时才调用它。
 不需要记忆就不要调用——大多数日常互动不需要访问长期记忆。"
```

优势：
- 减少每轮 ~200–600 tokens 的自动注入
- 只在真正需要时多一次 tool call（工具调用本身 token 少）
- 模型学会"记忆门控"，更像真实宠物的认知（不是全知全觉）

代价：
- 对于模型判断"需不需要记忆"的能力有要求，gpt-5.4 的能力足够。
- 增加一次 tool call 延迟（约 +1–2s），但节省了每轮自动召回的 prefill 开销（约 -0.5–1s 主模型侧），整体效果取决于召回频率。

**推荐策略**：**混合模式**
- 永远注入：最近 3 次互动摘要（~100 tokens，增强连续感）
- 按需召回：深层记忆、技能细节（model 调 tool）
- 状态必注入：pet_state（~50 tokens，宠物感的核心）

### 实操建议

```python
# 混合记忆策略
ALWAYS_INJECT = [
    pet_state,                      # ~50 tokens，宠物感必须
    recent_3_interactions_summary,  # ~100 tokens，上下文连续
]
ON_DEMAND_TOOLS = [
    "search_memory",    # 模型自行决定是否调用
    "recall_skill",     # 检索具体技能细节
]

system_prompt = build_static_core() + format_owner_profile() + format_skills_brief()
system_prompt += "\n# 当前状态\n" + format_state(pet_state)
system_prompt += "\n# 最近互动\n" + format_recent(recent_3)
# 不再自动注入 top-K 记忆，改为 tool
```

### 预期收益

- 紧凑格式：**-10%–20% system token**（技能列表压缩）。
- 按需记忆召回：**-200–600 tokens/轮**（取决于原有召回量），相当于 **-10%–25% TTFT**。
- 注意：这两个优化需要在提示质量和 token 节省之间权衡，先测试后部署。

---

## 方向八：Agent 路由

### OpenClaw 多 agent 架构

OpenClaw 本身支持多 agent 并发（`src/agents/` 下的 `agent-scope.ts`、`lanes.ts`），可以配置多个具有不同能力的 agent，通过 routing 策略分发请求。这在 ACP（Agent Communication Protocol）层面已有支持（`src/agents/acp-spawn.ts`）。

### 路由策略设计

```
消息分类 → 路由到不同 agent：

[类型 A] 简单情绪反应（占 ~40%）
  判断条件：短消息 + 无图 + 触发词匹配
  路由到：本地规则引擎 or NPU 本地小模型
  延迟：<500ms
  示例："好狗狗"、"来玩"、"坐下"

[类型 B] 多模态理解（占 ~40%）
  判断条件：含图片 or 视频帧
  路由到：gpt-5.4（必须）
  延迟：~7s（优化后 ~4s）
  示例：[发了6帧图] + "你看到我了吗？"

[类型 C] 复杂推理/教学（占 ~20%）
  判断条件：长消息 + 教学关键词 + 多轮问题
  路由到：gpt-5.4（必须）
  延迟：~7s
  示例："我来教你新动作：当我说'转圈'时你就..."

路由决策器（本地，<50ms）：
  规则优先 → 小模型分类 → 默认 gpt-5.4
```

### 路由风险：误判破坏宠物感

最大风险是把应该给 gpt-5.4 的请求路由到了本地小模型，导致宠物回复质量下降，主人感觉"它变笨了"。

**风险缓解策略**：

1. **保守路由**：只有非常确定（规则完全匹配 + 短消息 + 无图）才走本地，否则全走 gpt-5.4。宁可损失一点效率，不损失宠物感。

2. **影子模式验证**：本地模型同时也生成回复，但不显示给用户；gpt-5.4 回复到达后，对比两者相似度。如果本地模型正确率达到阈值，才在生产中启用该类型的本地路由。

3. **降级提示**：对于本地小模型处理的回复，UI 不要显示任何"本地处理"的标记，保持体验一致性。

4. **关键路径保护**：技能学习、主人认证、情绪重大事件（主人哭泣、生气等），无论如何都走 gpt-5.4，不路由本地。

### 实操建议

```python
class PetMessageRouter:
    # 规则库（高精度，极低误判率）
    SIMPLE_TRIGGERS_RE = re.compile(
        r'^(坐下|站起来|摇尾巴|握手|好狗狗|坏狗狗|过来|走开|吃饭|睡觉)$'
    )
    TEACHING_RE = re.compile(r'(教你|学一下|记住|以后当我说)')

    def route(self, text: str, images: list, turn_count: int) -> str:
        # 有图片 → 必须远程（不妥协）
        if images:
            return "gpt-5.4"
        # 教学关键词 → 必须远程（不妥协）
        if self.TEACHING_RE.search(text):
            return "gpt-5.4"
        # 完全匹配简单触发词 → 可以本地
        if self.SIMPLE_TRIGGERS_RE.fullmatch(text.strip()):
            return "local-rules"
        # 其他情况 → 远程
        return "gpt-5.4"
```

### 预期收益

- 简单互动（~40%的量）响应时间：**7s → <500ms**（-93%）
- 总体平均响应时间：**7s → ~4–5s**（假设 40% 走本地）
- 远程 API 调用减少 ~40%，成本下降同等比例

---

## orbitai/gpt-5.4 的现实约束

这一节是针对我们具体场景最重要的约束说明。

### 不可控的部分

| 约束 | 影响 |
|------|------|
| orbitai 是第三方代理，代码不可见 | 无法知道是否透传 OpenAI prefix cache，无法修改序列化行为 |
| gpt-5.4 模型本身不可控 | 无法启用 speculative decoding、无法调整 KV cache 策略 |
| 已知静默丢图问题 | N=6 图偶发 prompt_tokens < 200 异常，必须 client 侧 sanity check + retry |
| API 返回的 usage 字段内容不确定 | `cached_tokens` 字段可能不存在或不准确 |

### 可控的部分（我们能做的）

1. **Prompt 设计**：稳定化前缀（方向一、七）是客户端完全可控的，即使不确定 cache 是否工作，稳定前缀也有利于 cache 机会命中，且让代码更规范。

2. **客户端缓存**：在客户端维护 system prompt 的 hash，如果两次请求的 system 完全相同，可以记录"潜在缓存机会"，但实际 KV cache 复用依赖服务端。

3. **图片压缩**（方向四）：完全在客户端做，不依赖 orbitai 实现，立竿见影减少 token。

4. **Streaming 感知优化**（方向二）：客户端 UI 层完全可控。

5. **本地路由**（方向六、八）：完全在本地完成，绕过 orbitai，减少调用次数。

6. **Sanity check 和 retry**：已知 orbitai 丢图 bug，必须实现：
   ```python
   if response.usage.prompt_tokens < 200 and len(images) > 0:
       # orbitai 静默丢图，重试一次
       response = retry_with_logging(request)
   ```

7. **验证 cache 是否工作**：按方向一的验证方法，实测 orbitai 的 cache 行为，决定是否值得投入优化时间，或者考虑绕过 orbitai 直连 OpenAI（如果条件允许）。

---

## 优先级排序

### Top 3（最值得先做）

**第一优先：图片降采样 + detail=low（方向四）**

- **理由**：实现成本极低（2 行 Python 代码），收益最确定（不依赖任何外部服务行为），图片 token 节省 65%–77%，是延迟改善的直接杠杆。
- **风险**：图片质量下降（512px 对宠物场景足够，需实测确认）。
- **时间**：0.5 天。

**第二优先：Prompt 前缀稳定化 + cache 验证（方向一 + 七）**

- **理由**：先验证 orbitai 是否透传 prefix cache（1 小时实验），如果是则 system prompt 优化立刻生效；即使不是，稳定前缀设计也是后续工作的基础，且 prompt 紧凑化带来的 token 节省是确定的。
- **风险**：orbitai 不透传 cache 则 cache 相关收益为零，但 prompt 设计本身还是有价值。
- **时间**：1 天（含 cache 验证实验）。

**第三优先：Streaming 感知优化（方向二）**

- **理由**：零成本改变"感知延迟"，用户体验提升最直观。"先输出情绪词"策略只需一句 system prompt 修改，"思考动画"只需 UI 修改。这两项改动 1 天内可完成，用户感知到的"响应速度"提升明显。
- **风险**：几乎没有负面风险，最坏情况是无效。
- **时间**：0.5 天。

### 中期优先（第二批）

4. **vision→text description pipeline（方向四 进阶）**：额外减少 ~60% 主模型 prefill，且描述文本可进缓存。需要实现异步并行架构，工作量约 2–3 天。

5. **按需记忆召回（方向七 进阶）**：每轮节省 200–600 tokens。需要修改 system prompt + 测试模型工具调用质量，约 1–2 天。

6. **本地简单互动路由（方向六/八）**：大幅降低简单互动延迟。需要 NPU 环境就绪（等 NPU-1 完成）和触发词规则库，约 2–3 天。

### 暂缓（第三批）

7. Context engine 保护（方向五）：重要但不急，在技能/记忆层实现后再做。

8. Agent 多模型路由复杂策略（方向八）：需要本地模型就绪 + 较长时间的影子模式验证，风险较高。

---

## TL;DR：5 条 Actionable 建议

**1. 立刻做：把图片最长边从 768px 降到 512px，或开启 `detail=low`。**
修改抽帧/发送代码中的 resize 参数，图片 token 从 ~2200 降到 ~510–765，预计 TTFT **-35%–50%**。零风险实验，2 小时完成。

**2. 今天验证：发两次相同前缀请求，检查 orbitai 响应的 `cached_tokens` 字段是否 > 0。**
10 行 Python 测试代码，1 小时得出结论。结果决定后续是否值得投入 prefix caching 优化。

**3. 重构 system prompt 顺序：静态内容在前（基础指令 + owner_profile + skills），动态内容在后（pet_state + 召回记忆）。**
最大化 prefix cache 命中概率，同时让代码逻辑更清晰。不依赖 orbitai cache 结果——即使 cache 没工作，这也是更好的代码组织方式。

**4. UI 改造：消息发出后立即显示"宠物思考中"动画，首 token 到达后立即切换显示流式文字。**
感知延迟从 "等待 7 秒" 变为 "等待 1–2 秒看到首字符"，用户体验提升最明显且改动最小。System prompt 加一句"先输出情绪词（汪/呜/哎）再回答"，让首 token 更快有意义。

**5. 中期：实现 vision→text description 异步流水线，小模型先把 6 帧图片转为 JSON 描述，主模型只收文本。**
主模型 prompt 中的图片 token 降为零，省下的 prefill 时间 + 可被 prefix cache 的文本描述，预计 TTFT **-40%–55%**。需要 1–2 天工程实现，是长期降本最有效的手段。

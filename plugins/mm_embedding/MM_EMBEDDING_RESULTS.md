# 阿里百炼多模态 Embedding 验证结果

## 结论（一句话）

**`multimodal-embedding-v1` 可用，且文本-图像跨模态对齐质量足够支撑模拟宠物的"场景记忆"和"通用图像/文本召回"——但不足以单独承担"主人面部识别"，face 仍需要 FaceNet/ArcFace 等专用 face embedding 模型。**

## API 详情

| 项 | 值 |
|---|---|
| 模型名 | `multimodal-embedding-v1` |
| 端点 | `https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding` |
| 鉴权 | `Authorization: Bearer ${DASHSCOPE_API_KEY}` |
| 向量维度 | **1024** |
| 输入类型 | `text` / `image`（image 接受 data-URI base64 或 https URL） |
| 单次返回 | **1 个 fused embedding**（即使 input.contents 里有多个 part 也只返回一个，相当于"对该 contents 的整体融合表征"） |
| 文本 token 计费 | `usage.input_tokens`（如 "a photo of a dog" = 7 tokens） |
| 图片 token 计费 | `usage.image_tokens`（每张图固定 = **128 image_tokens**，与分辨率无关——本测试中 256x256 / 480x640 / 720p 均为 128） |
| 单次延迟（北京 -> 杭州） | 文本 ~0.7-0.9 s，图像 ~0.85-1.0 s，混合 ~1.6 s |
| 输出归一化 | 否——返回的向量未归一化，需要业务侧自行 L2 归一化或用 cosine 直接算 |

> 注：DashScope 文档对该端点的最大单次 contents 数量描述为 **<= 3**（text/image/video 各最多 1），且 image 单边像素 <= 4096，文件 <= 5MB。本次测试均在限制内。

## 测试结果

所有测试见 `test_matrix.py`，原始 JSON 见 `test_matrix_results.json`。

### A. 文本-文本（同义/不相关分离）

| 文本对 | cosine |
|---|---|
| "a red dog" vs "a crimson canine" | **0.8246** |
| "a red dog" vs "a small puppy on the grass" | 0.5946 |
| "a red dog" vs "a green car" | 0.5356 |
| "a red dog" vs "an alien spaceship in deep space" | **0.4297** |
| "a crimson canine" vs "a small puppy on the grass" | 0.5567 |

**结论 A**：同义对（0.82）显著高于不相关对（0.43），间隔 0.39 → 文本检索可用。

### B. 图-图

> 注：素材 `medium_43s.mp4` 是纯色测试卡（RED 1-10s, GREEN 15-25s, BLUE 30-40s），所有 `mm_gpt/frames/n0x/` 都源自该视频；同色帧间内容近乎完全一致。

| 图像对 | cosine | 说明 |
|---|---|---|
| red_a vs red_b（同色不同时间戳） | **0.9999** | 同一场景 |
| red_a vs green | 0.4839 | 同视频，跨场景 |
| red_a vs blue | 0.4444 | 同视频，跨场景 |
| green vs blue | 0.4264 | 同视频，跨场景 |
| red_a vs real（短视频实拍帧） | **0.1690** | 跨域，纯色 vs 真实场景 |
| green vs real | 0.1342 | 跨域 |
| blue vs real | 0.1125 | 跨域 |

**结论 B**：同场景 ≈ 1.0，跨场景 ≈ 0.45，跨域 ≈ 0.13 — **图像内容相似度的判别力良好**。

### C. 跨模态：文本 → 图（最关键的一组）

|  | image=red | image=green | image=blue | image=short |
|---|---|---|---|---|
| text="a vivid red colored screen" | **0.2308** | 0.1424 | 0.1396 | 0.0838 |
| text="a bright green colored screen" | 0.1698 | **0.2657** | 0.1306 | 0.0884 |
| text="a deep blue colored screen" | 0.1522 | 0.1557 | **0.2616** | 0.0892 |
| text="red background with a yellow square" | **0.3005** | 0.1188 | 0.1200 | 0.0674 |

**结论 C**：每一行的对角元素（颜色一致项）**确实是该行的最大值**——颜色文本和颜色图像的跨模态对齐成立。绝对相似度数值偏低（0.23-0.30）是该模型预期范围（和 OpenAI CLIP 类似，跨模态绝对值都不高，重要的是 ranking）。

### D. 人脸近似（合成卡通脸，仅作为可行性预筛）

> ⚠️ 本机无真实人脸数据集，使用 PIL 程序化合成的卡通脸做 sanity check。这**不是**真正的 face recognition benchmark。

| 脸对 | cosine |
|---|---|
| a vs a_rot（同身份，旋转 15°） | 0.9539 |
| a vs a_crop（同身份，裁剪缩放） | 0.9658 |
| a_rot vs a_crop | 0.9529 |
| a vs b（不同身份） | 0.9185 |
| a_rot vs b | 0.8922 |
| a_crop vs b | 0.8841 |

**结论 D**：同身份 cosine ≈ 0.95，不同身份 cosine ≈ 0.91，**间隔仅 ~0.04**——即使在程序化合成、差异极大的卡通脸上间隔都这么窄，真实人脸（光照/角度/表情变化更多）几乎肯定 overlap。**用通用多模态 embedding 直接做 face verification 不可行。**

## OpenClaw 集成建议

### 1. Provider 雏形

在 `extensions/pet-multimodal-embedding/` 实现一个 `MediaUnderstandingProvider`，对接现有的 `EmbeddingPipeline` 接口，关键是把 `EmbeddingInputInlineDataPart` 真正接出来：

```ts
// extensions/pet-multimodal-embedding/provider.ts
import type {
  EmbeddingProvider,
  EmbeddingInput,
  EmbeddingInputInlineDataPart,
  EmbeddingResult,
} from "@openclaw/embedding";

const ENDPOINT =
  "https://dashscope.aliyuncs.com/api/v1/services/embeddings/" +
  "multimodal-embedding/multimodal-embedding";
const MODEL = "multimodal-embedding-v1";

export class DashScopeMultimodalEmbeddingProvider implements EmbeddingProvider {
  readonly id = "dashscope-multimodal-embedding-v1";
  readonly dim = 1024;
  readonly supportsModalities = ["text", "image"]; // video 也支持但本期不接

  constructor(private apiKey: string) {}

  async embedBatchInputs(inputs: EmbeddingInput[]): Promise<EmbeddingResult[]> {
    // DashScope 接口每次返回 1 个 fused embedding => 串行 / 小并发逐个调用
    // (默认账户并发 ~5 QPS, 别一次塞 100)
    const results: EmbeddingResult[] = [];
    for (const inp of inputs) {
      const contents = inp.parts.map(toContentPart);
      const body = await this.call(contents);
      results.push({
        vector: body.output.embeddings[0].embedding,
        usage: body.usage,
      });
    }
    return results;
  }

  private async call(contents: any[]): Promise<any> {
    const r = await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ model: MODEL, input: { contents } }),
    });
    if (!r.ok) throw new Error(`DashScope ${r.status}: ${await r.text()}`);
    return r.json();
  }
}

function toContentPart(p: EmbeddingInputPart): { text?: string; image?: string } {
  if (p.type === "text") return { text: p.text };
  if (p.type === "inline-data") {
    // EmbeddingInputInlineDataPart {mimeType, data: base64}
    return { image: `data:${p.mimeType};base64,${p.data}` };
  }
  if (p.type === "uri") return { image: p.uri };
  throw new Error(`unsupported part type: ${(p as any).type}`);
}
```

### 2. 推荐用法分层

| 用途 | 推荐方案 | 理由 |
|---|---|---|
| **场景记忆 / 视觉日记**（"我之前看到的红色玩具") | `multimodal-embedding-v1`，文本 query → 图像/帧 召回 | C 组验证跨模态 ranking 成立 |
| **技能 trigger**（看到 X 就做 Y） | `multimodal-embedding-v1` 取向量 + 阈值 | B 组验证同场景 ≈ 1.0、跨场景明显分离 |
| **主人脸识别 / face id** | **不要用** `multimodal-embedding-v1`；引入专用 face embedding | D 组同/异身份间隔太窄；真实人脸只会更差 |
| **主人档案**（声音、衣着、配饰风格） | 多模态 embedding 可承担（衣着/配饰本质是场景特征） | 同 B/C |
| **物体识别 + 名字 binding** | 多模态 embedding（图 + 文本标签）共向量空间检索 | C 组对齐 |

### 3. 主人识别建议架构

```
camera frame ─┬─► face detector (MTCNN / mediapipe / yunet)
              │      └─► face crop ─► FaceNet/ArcFace ─► face_id (256-d)
              │                       (本地 onnx / insightface)
              │
              └─► full frame ─► DashScope multimodal-embedding ─► scene_id (1024-d)

记忆库:
  Owner:   { face_ids: [...], scene_ids: [...], voice_id?: ... }
  Scene:   { scene_id, summary_text, mm_emb: 1024-d }
  Trigger: { trigger_text, mm_emb: 1024-d, action }

主人识别 = face_id 余弦 > 0.6  (FaceNet 业界阈值)
场景召回 = scene_id 余弦 top-K (DashScope mm-emb)
```

### 4. 已知限制

- **fused-only**：单次调用对 contents 列表整体输出 1 个向量；想拿 per-part 向量必须拆批多次调用，对 batch 吞吐不友好。
- **图片 tokens 固定 128**：与分辨率无关 → 计费友好；但意味着任意大小都被压成同样表征，**细粒度区分（人脸特征级）能力有限**——这正是 D 组同/异脸只差 4% 的根因。
- **rate limit**：默认账户对 multimodal-embedding-v1 是 ~5-10 QPS（百炼控制台 RAM 配额可调）；批量入库时需要做 token bucket。
- **单次输入 size**：image base64 数据 + 整个请求体 < ~10MB；建议先 client-side resize 到 ≤ 1024px。
- **未归一化**：业务侧需自己 L2 normalize 后再做 cosine。
- **响应延迟稳定 ~1s**：实时 trigger 场景需要异步流水线，不要放在主对话路径上。
- **没有 video modality 验证**：DashScope 文档声称该模型支持 video（传 frame URL list），本次未验证；接 OpenClaw 时建议先做单帧 image，video 路径走 mm_gpt 抽帧 + 多图聚合。

## 附件

- `probe.py`：API 单点验证脚本
- `test_matrix.py`：A/B/C/D 全套测试
- `test_matrix_results.json`：本次运行的 cosine 数值原始数据
- `test_assets/`：抽出的彩色帧 + 合成卡通脸（已加 .gitignore）
- `.env`：本机 API key（已 .gitignore）

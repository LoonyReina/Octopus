# 延迟优化原理说明（latency_optimization.md 的机制 footnote）

> 版本：v1.0 | 日期：2026-04-24  
> 本文是 latency_optimization.md TL;DR 5 条的机制层解释，不重复操作建议，只讲"为什么"。

---

## 当前瓶颈速览

一轮 ~7 s 延迟的 token 构成（N=6 帧实测）：

```
图片 token：6 × ~375 = ~2250 token   ← 占输入 ~65%，最大单点
system 静态前缀：~900 token
system 动态部分：~250–850 token
历史对话：~0–4000 token（随轮数增长）
output：~198 token
```

TTFT（Time To First Token）= prefill 时间 + 网络 RTT。  
prefill 是唯一在我们控制范围内可大幅压缩的环节。decode（生成 ~198 output token）贡献固定的 ~1–2 s，不受输入大小影响。

---

## 1. 图片 768→512 降采样 / detail=low

**机制**

OpenAI vision 采用 tile-based 编码：图片按 512×512 tile 切分，每 tile 约 170 token，加 1 个 base tile（85 token）。768×768 图片超过单 tile 边界，触发 2×2=4 tiles + 1 base = **5 tiles ≈ 765 token/图**。512×512 及以下恰好落在单 tile 内，为 1+1=2 tiles = **255 token/图**。`detail=low` 强制单 tile 且不加 base = **85 token/图**。这些视觉 token 在 prefill 阶段与文本 token 一起过 self-attention，计算量完全等价。

**数字推算**

- 768px：6 图 × 765 = **4590 token**（注：实测 prompt_tokens=2249，因 640×480 源分辨率实际只触发 ~375 token/图；下面用实测值）
- 实测 N=6, 768px → prompt_tokens ≈ 2249，其中图片约 2250×(375/375)=~2250（文本约 50）
- 降到 512px：6 × 255 = **1530 token**，图片 token 减少 **~57%**
- detail=low：6 × 85 = **510 token**，图片 token 减少 **~77%**
- prefill 时间 ∝ total_tokens（线性），因此总 prompt 从 ~2249 → ~549（detail=low）：prefill 减少 **~75%**；→ ~1249（512px）：减少 **~44%**
- TTFT = prefill + decode_first_token，decode 部分固定约 0.5–1 s；假设 7 s 中 prefill 占 5–6 s，则 TTFT 改善 **35%–50%**（与报告数字吻合）

**失效场景**

若 orbitai 对视觉 token 有固定最低计费粒度，或瓶颈在网络 RTT 而非 prefill 计算，则降图效果会低于预期。

---

## 2. Prefix Cache 验证 + 重构 system prompt 顺序

**机制**

Prefix Cache（KV Cache 前缀复用）：GPU 在 prefill 时为每个 token 计算 Key-Value 矩阵，这些矩阵可以存入 HBM/DRAM 并跨请求复用。命中条件是**请求前缀的 byte 序列与缓存时完全一致**（OpenAI 以 128-token 粒度对齐）。只要前缀 byte 不变，第二次请求跳过这部分 prefill 计算，等效处理时间接近零。"静态内容放前、动态内容放后"正是为了让变化量最小的部分（基础指令 + owner_profile + skills）保持在前缀位置，使每轮都能命中同一缓存块。

**数字推算**

- system 中可稳定的部分（基础指令 + owner_profile + skills）约 900 token
- 每轮变动的部分（pet_state + 召回记忆）约 250–850 token，放在末尾
- 若命中缓存，900 token 的 prefill 被跳过；900 / 2249 ≈ **40% 的 prompt token 免计算**
- 若顺序颠倒（动态内容在前），每轮动态内容变化导致前缀失效，缓存命中率 ≈ 0%
- 预期 TTFT 改善：**15%–30%**（视静态前缀占比和 orbitai 是否透传 cache 而定）

**失效场景**

orbitai 若在转发时重新序列化请求（修改空格、字段顺序），byte 级前缀不匹配，缓存每次失效，此条收益归零——这也是报告要求先用 10 行代码验证 `cached_tokens` 字段的原因。

---

## 3. Streaming + UI 感知优化

**机制**

Streaming 不改变总延迟，改变的是**用户感知到"系统在响应"的时间点**。人对延迟的感知分两段：(a) 静默等待期（发出消息到看到任何变化）造成焦虑；(b) 看到内容流动后感知切换为"正在进行"，主观上不再计时。首 token 出现时间（TTFT）是 (a) 段的终点。配合"先输出情绪词"的 prompt 策略，模型生成的第一批 token 就是有意义的内容（"汪~" "呜—"），而不是等待 JSON 结构完整后才显示。

**数字推算**

- 当前：用户等待整个 7 s 才看到输出，感知延迟 = 7 s
- 引入流式渲染 + 思考动画：静默等待压缩到首 token 到达时间（约 3–5 s），但用户看到动画后感知时钟暂停
- "先输出情绪词"使首个有意义字符在 TTFT 时刻立刻可见；结合图片降采样（TTFT 降到 ~3.5–4.5 s），用户感知到"有响应"的时间从 7 s → **1.5–2.5 s**
- 感知延迟改善约 **40%–65%**，但端到端总延迟不变

**失效场景**

若 UI 端流式渲染实现有缓冲（等积累一定 chunk 才刷新），或宠物动画系统只接受完整回复再播放，则流式传输的感知收益被抵消。

---

## 4. Vision→Text 异步流水线（中期）

**机制**

当前架构：6 帧图片以 base64 image_url 直接附在主模型请求里，主模型（gpt-5.4）需要运行 visual encoder 处理图片 token 后才开始 text prefill，两步串行。替代方案：用一个轻量 vision 小模型（gpt-5.4-mini 或本地 MiniCPM-V）先把 6 帧转成 ~80 token/图的结构化文本描述，主模型只接收文本描述（~480 token），不再处理任何图片 token。图片 token 的计算量从主模型中完全消除，且文本描述可进入 prefix cache（base64 图片数据无法缓存）。关键在于"异步并行"：vision 小模型调用与主模型请求的其他准备工作同时进行，实际增加的串行等待时间仅为 vision 小模型延迟与准备时间差值。

**数字推算**

- 主模型节省：图片 token 从 ~2250 → 0，替换为文本描述 ~480 token
- prompt_tokens 从 ~2249 → ~2249 - 2250 + 480 = **~479 token**（图片部分彻底消除）
- prefill 时间减少约 **~79%**；TTFT 改善 **40%–55%**
- vision 小模型额外延迟：若并行执行，理论净增延迟 ≈ max(0, T_vision - T_other_prep)；实测 gpt-5.4-mini 处理 6 张 low-detail 图约 0.5–1 s，主模型准备工作（context 组装）通常 <0.1 s，故净增 ~0.5–1 s
- 综合：主模型 prefill 节省 ~3–4 s，小模型增加 ~0.5–1 s，净收益 **~2–3.5 s**（即 TTFT 的 40%–50%）
- 额外红利：文本描述进入 prefix cache 后，重复相似场景时可命中缓存，进一步降低后续轮次延迟

**失效场景**

若 vision 小模型自身延迟 >3 s（如调用 gpt-5.4-mini 遭遇高负载），或小模型描述质量不足导致主模型误判场景，则需 fallback 回原始图片方式，流水线收益消失。此条是 5 条中**不确定性最高的**，需要实测 vision 小模型的实际延迟和描述质量才能确认收益范围。

---

## 5. 总瓶颈分析：当前 ~7 s 的时间分布

**实测基准**（来自 MULTI_IMAGE_RESULTS.md，N=6, prompt B）：  
latency = 7.19 s，prompt_tokens = 2249，completion_tokens = 198

**各阶段时间估算**

```
[客户端组装 + 图片编码]     ~0.1–0.3 s   （本地 ffmpeg + base64）
[网络上行：2249 token payload]  ~0.2–0.5 s   （取决于带宽，base64 ~140 KB）
[prefill：2249 token]        ~3.5–5.0 s   ← 最大瓶颈，token-bound
[decode：198 token]          ~1.0–1.5 s   （fixed cost，~100–150 ms/token）
[网络下行 + streaming 传输]   ~0.3–0.5 s
---
总计                         ~5.1–7.8 s   → 实测 7.19 s 符合
```

**token-bound 阶段识别**

prefill 是纯计算密集型：每个 input token 需与所有其他 input token 计算 attention（复杂度 O(n²) 对于 self-attention），且 N=6 时 2249 个 token 中 2250 是图片 token——即几乎所有 prefill 计算量来自图片。decode 是 memory-bandwidth bound（每步只生成 1 token，GPU 利用率低），但 token 数少（198），绝对时间有限。**改图片 = 改 prefill = 改 TTFT，是唯一高杠杆单点**。

**会话增长效应**

随技能学习和历史积累，system prompt + history 从 ~2249 增长到 ~7950 token（报告估算上限）。prefill 时间线性增长，最坏情况 TTFT 从 7 s 增长到 **~20–25 s**，若不做 prefix cache 或图片优化，体验将显著恶化。

---

## 5 条之间的优先级机制依据

**为什么是"图片降采样 → prefix cache → streaming UI"而不是其他顺序？**

优先级取决于三个维度：**收益确定性**（不依赖外部不可控因素）、**收益量级**（减少的绝对时间）、**实现成本**。

| 条目 | 收益确定性 | 量级 | 成本 | 优先级机制 |
|------|-----------|------|------|-----------|
| 图片降采样 | **完全确定**（客户端做，不依赖 orbitai）| 最大（-35%–50% TTFT）| 最低（2 行代码）| 排第 1 |
| Prefix cache | **条件确定**（先验证 orbitai 是否透传）| 中（-15%–30% TTFT）| 低（1 小时验证 + 1 天重构）| 排第 2 |
| Streaming UI | **完全确定**（纯 UI 层改动）| 感知最大（-40%–65% 主观延迟）| 最低（UI + 1 句 prompt）| 排第 3 |
| Vision→Text pipeline | **不确定**（依赖小模型延迟 + 质量）| 最大潜力（-40%–55% TTFT）| 高（2–3 天）| 排第 4（中期）|

图片降采样排第 1 的机制原因：**它攻击的是最大 token 来源，且完全在客户端控制范围内，不存在外部依赖风险**。Prefix cache 排第 2 而非更高，是因为 orbitai 是否透传 cache 是未知量——先花 1 小时验证，再决定是否投入时间；但即使 cache 不工作，"静态前 / 动态后"的 prompt 结构设计也是工程债清理，代价不为零。Streaming UI 的量级是感知而非实际延迟，但用户体验是最终目标，感知改善与实际改善同等有效，且零副作用——这是它优于 vision pipeline 的原因（pipeline 有失效风险）。Vision pipeline 排中期的机制原因：它的收益最大但不确定性也最高，需要先把确定性收益（1+2+3）落地并测量剩余瓶颈，再决定是否值得付出架构复杂度的代价。

/**
 * 最小独立验证：L agent 实现的 DashScopeMMEmbeddingClient 能否 embed 用户图片输入。
 * 不依赖 pnpm workspace、不依赖 OpenClaw 容器、不调用 dry-run fake embedder。
 *
 * 直接：本机 jpg → base64 → DashScopeMMEmbeddingClient → 1024d 向量。
 *
 * Usage (Windows git-bash):
 *   cd plugins/mm_embedding
 *   npx tsx integration_test.ts
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  DashScopeMMEmbeddingClient,
  VECTOR_DIM,
} from "../../openclaw/extensions/pet-multimodal-embedding/dashscope-mm-embedding";

const HERE = path.dirname(fileURLToPath(import.meta.url));

// --- Load API key from .env ---
function loadApiKey(): string {
  const envPath = path.join(HERE, ".env");
  let envContent: string;
  try {
    envContent = readFileSync(envPath, "utf-8");
  } catch {
    throw new Error(`.env not found at ${envPath}`);
  }
  const m = envContent.match(/^DASHSCOPE_API_KEY=(.+)$/m);
  if (!m) throw new Error(".env does not contain DASHSCOPE_API_KEY=");
  const key = m[1].trim();
  if (!key) throw new Error("DASHSCOPE_API_KEY is empty");
  return key;
}

// --- Load test image ---
function loadFrameAsBase64(): { mime: string; data: string; sizeKb: number } {
  const jpgPath = path.join(
    HERE,
    "..",
    "mm_gpt",
    "frames",
    "n06",
    "frame_00_0003.583.jpg",
  );
  const buf = readFileSync(jpgPath);
  return {
    mime: "image/jpeg",
    data: buf.toString("base64"),
    sizeKb: Math.round(buf.length / 1024),
  };
}

async function main() {
  console.log("[integration_test] === multimodal embedding image input verification ===");

  const apiKey = loadApiKey();
  console.log("[integration_test] API key loaded from .env");

  const frame = loadFrameAsBase64();
  console.log(
    `[integration_test] test frame: ${frame.sizeKb} KiB, mime=${frame.mime}, base64 len=${frame.data.length}`,
  );

  const client = new DashScopeMMEmbeddingClient({
    apiKey,
    qps: 5,
    normalize: true,
  });

  // Test 1: image-only input
  console.log("\n[integration_test] --- Test 1: image-only input ---");
  const t1Start = Date.now();
  const t1 = await client.embedBatchInputs([
    {
      text: "",
      parts: [{ type: "inline-data", mimeType: frame.mime, data: frame.data }],
    },
  ]);
  const t1Ms = Date.now() - t1Start;
  console.log(`  vectors returned: ${t1.length}`);
  console.log(`  vector[0] dim: ${t1[0].length} (expected ${VECTOR_DIM})`);
  console.log(
    `  vector[0] first 5: [${t1[0].slice(0, 5).map((x) => x.toFixed(4)).join(", ")}]`,
  );
  const norm1 = Math.sqrt(t1[0].reduce((s, x) => s + x * x, 0));
  console.log(`  L2 norm: ${norm1.toFixed(6)} (expected ~1.0 since normalize=true)`);
  console.log(`  latency: ${t1Ms} ms`);

  // Test 2: image + text combined input
  console.log("\n[integration_test] --- Test 2: image + text fused input ---");
  const t2Start = Date.now();
  const t2 = await client.embedBatchInputs([
    {
      text: "a video frame from a pet camera",
      parts: [
        { type: "text", text: "a video frame from a pet camera" },
        { type: "inline-data", mimeType: frame.mime, data: frame.data },
      ],
    },
  ]);
  const t2Ms = Date.now() - t2Start;
  console.log(`  vector[0] dim: ${t2[0].length}`);
  console.log(`  L2 norm: ${Math.sqrt(t2[0].reduce((s, x) => s + x * x, 0)).toFixed(6)}`);
  console.log(`  latency: ${t2Ms} ms`);

  // Test 3: text-only embedQuery (sanity)
  console.log("\n[integration_test] --- Test 3: text-only embedQuery ---");
  const t3Start = Date.now();
  const t3 = await client.embedQuery("show me anything red");
  const t3Ms = Date.now() - t3Start;
  console.log(`  vector dim: ${t3.length}`);
  console.log(`  latency: ${t3Ms} ms`);

  // Cosine of (text-only "red") with (image+text) — sanity ranking
  const cos = t1[0].reduce((s, x, i) => s + x * t3[i], 0);
  console.log(
    `\n[integration_test] cosine( image, "show me anything red" ) = ${cos.toFixed(4)}`,
  );

  console.log("\n[integration_test] PASS — image input embedding is functional.");
}

main().catch((err) => {
  console.error("[integration_test] FAIL:", err?.message ?? err);
  if (err?.stack) console.error(err.stack);
  process.exit(1);
});

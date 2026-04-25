# mmclaw 基础设施搭建文档

> **状态**：部分完成（2026-04-24）。Agent Z 完成了同步工具链 + dashboard 但**没生成本文档也没在远端实际验证容器启动**。本文档由 Claude 主体补写，**包含未验证项需用户协助确认**。

---

## 0. 术语
- **本地** = Windows，`C:\Reina_desktop\program_and_contest\mmclaw\code\`
- **远端** = `ssh root@192.168.31.51`，工作目录 `/root/mmclaw/`
- **远端 docker 镜像**：`openclaw_423`（已就绪）
- **本项目容器名约定**：`mmclaw-dev`（**不要**用 `openclaw_*` 前缀，避免和共享镜像/容器冲突）

## 1. 共享约束（不可破坏）

机器多人共用：
- ❌ 不修改 `openclaw_*` 系列任何已有 docker 镜像（`docker rmi`/`tag`/`commit` 全禁）
- ❌ 不修改镜像默认启动设置（不动 Dockerfile/CMD/ENTRYPOINT）
- ✅ `docker run` 时挂卷+暴露端口+传 env 是允许的（运行时参数）
- ✅ 工作目录限定 `/root/mmclaw/`

---

## 2. 代码同步（已就绪 ✅）

**Agent Z 已交付**，路径在本地 `code/` 下：

| 文件 | 作用 |
|---|---|
| `sync.py` | 核心同步脚本（270 行）。增量同步 `openclaw/` → 远端 `/root/mmclaw/openclaw/`。机制：枚举本地+远端 mtime/size 对比，差异打成 tar.gz 流传 ssh+tar 落地。等价 rsync，但仅依赖 OpenSSH 自带 ssh+tar |
| `sync.bat` | Windows wrapper，调 sync.py |
| `sync.sh` | bash/git-bash/WSL wrapper |

**用法**：
```bash
# 增量同步（默认）
sync.bat

# 同步并删除远端多出来的文件（谨慎用）
sync.bat --delete
```

**SSH 配置**：脚本读 `~/.ssh/id_rsa` 或 `%USERPROFILE%\.ssh\id_rsa`。不通的话先确认私钥路径。

**Exclude 规则**：已覆盖 `node_modules`、`dist`、`.git`、`.openclaw`、`tmp`、`.tsbuildinfo`、`.test.ts.snap` 等（看 sync.py 顶部 EXCLUDE_* 常量）。

**首次全量同步预期**：本地 `openclaw/` 约 700 个 .ts 文件，扣除 node_modules 后总大小 ~50–100 MiB，tar.gz 后约 10–30 MiB，传输应 < 60s（取决于网络）。

---

## 3. 远端 docker 启动（⚠️ 未实际验证）

> **重要**：以下命令由 Claude 推断（基于 OpenClaw `scripts/run-node.mjs` 行为 + 共享约束 + dashboard 对端口的假设），**用户运行后才能确认**。Agent Z 没有在最终报告里确认容器实际启动过。

### 3.1 启动 mmclaw-dev 容器

在远端宿主机执行（先 `ssh root@192.168.31.51`）：

```bash
# 先确认源码已同步过去
ls /root/mmclaw/openclaw/src/entry.ts || echo "请先在本地跑 sync.bat"

# 启动容器（运行时参数，不修改镜像）
docker run -d \
  --name mmclaw-dev \
  --restart unless-stopped \
  -v /root/mmclaw/openclaw:/mnt/openclaw \
  -p 3000:3000 \
  -p 9090:9090 \
  -w /mnt/openclaw \
  openclaw_423 \
  bash -c "pnpm install && pnpm dev"
```

**注意**：
- `-p 3000:3000` 绑 0.0.0.0 还是 `-p 127.0.0.1:3000:3000` 仅本地——视远端是否在内网暴露。出于安全（机器共用），推荐 `-p 127.0.0.1:3000:3000`，配合 SSH tunnel 用
- 容器命名 `mmclaw-dev`，和共享 `openclaw_*` 系列隔离
- `pnpm install` 在 ARM 上可能 10+ min，必要时容器内换镜像源

### 3.2 验证启动

```bash
# 跟踪日志
docker logs mmclaw-dev -f --tail 100

# 期望看到（顺序）：
# - pnpm install 完成
# - tsdown build 输出
# - node 启动 + "Listening on port 3000" 类似日志
```

### 3.3 改代码 → 容器自动重建

理论时序（等用户实测确认）：
1. 本地编辑 `openclaw/src/entry.ts`
2. 本地 `sync.bat`
3. 远端容器内 tsdown watcher 检测变化 → 增量重建（秒级）
4. node 进程重启（`scripts/run-node.mjs` 自动触发）
5. `docker logs mmclaw-dev -f` 看到新日志

**端到端时间**：估计 5–15s（同步 1–3s + tsdown 2–5s + 重启 2–5s）。**实测后请回填本文档**。

---

## 4. SSH tunnel + 本地 dashboard（已就绪 ✅）

**Agent Z 已交付**，路径 `dashboard/`：

```bash
# 1. 本地终端开 SSH tunnel（保持开着）
ssh -L 3000:localhost:3000 -L 8080:localhost:8080 root@192.168.31.51

# 2. 另开终端起本地 http server（避免 file:// 跨源限制）
python -m http.server 8000 --directory dashboard

# 3. 浏览器开 http://localhost:8000
```

Dashboard 显示：
- 顶部状态栏：OpenClaw web UI 连通性、camera stream 状态、last-sync 时间
- 左半屏：iframe 嵌 OpenClaw web UI（依赖步骤 1 SSH tunnel）
- 右半屏：摄像头占位（端口 8080，目前未启）
- 底部：5 秒一次 probe 日志

**已知限制**（agent 已记录在 `dashboard/README.md`）：
- 摄像头流未接入，等 P1/外设阶段补
- OpenClaw 必须在容器里跑起来，iframe 才能加载

---

## 5. 当前未验证项清单

需要用户配合验证（或下一轮派 agent 做）：

- [ ] 远端 SSH 可达（首次连接需要 accept host key）
- [ ] `sync.bat` 全量同步首次跑通（耗时、文件数、tar 大小回填到本文档）
- [ ] 远端 `/root/mmclaw/openclaw/` 内容齐备
- [ ] `docker run mmclaw-dev` 命令能起容器（`openclaw_423` 镜像兼容性、ARM/x86 架构、pnpm install 是否走得通）
- [ ] 容器日志看到 OpenClaw 启动完成
- [ ] 改代码→容器看到日志变化的端到端时长
- [ ] SSH tunnel 后浏览器能加载 OpenClaw web UI

## 6. 已知风险

- **pnpm install 卡国内源**：容器首次启动可能需要换 npmmirror 源。命令模板：
  ```bash
  docker exec -it mmclaw-dev sh -c "cd /mnt/openclaw && pnpm config set registry https://registry.npmmirror.com/"
  ```
- **架构兼容**：`openclaw_423` 镜像如果是 x86 而开发板是 ARM（Atlas 系列通常 ARM），`docker run` 时 native module（如 LanceDB、node-llama-cpp）可能 install 失败。如果失败，需要在 ARM 重新 build native deps（不修改镜像，只在容器内做）。

---

## 7. 后续任务

| Task | 状态 | 备注 |
|---|---|---|
| INFRA-1 同步机制 | ✅ 就绪（sync.py） | 用户验证首次同步耗时 |
| INFRA-2 docker 启动 | ⚠️ 命令就绪未实测 | 用户跑命令并回填本文档 |
| INFRA-3 改码→重建验证 | ⚠️ 未验证 | 启动后做一次端到端测试 |
| DEBUG-1 本地 dashboard | ✅ 就绪（dashboard/） | 用户开 SSH tunnel 后即可用 |
| INFRA-4 模型 API key cfg | 未开始 | 等容器跑通后做 |
| DEBUG-4 摄像头 streamer | 未开始 | P1 外设阶段 |

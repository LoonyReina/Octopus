# mmclaw 远端 docker 启动问题记录

> **时间**：2026-04-25 17:30 截至撰写
> **目的**：记录 docker daemon 状态损坏的复现命令、现象、诊断证据，便于后续 debug 或交给团队成员
> **状态**：未解决

---

## TL;DR

远端 docker daemon **完全无法创建任何新容器**。已用 ephemeral 模式（不带 name、不带 mount、`--rm`、跑 echo）验证：30 秒 timeout 强杀（exit 124）。

每次 `docker run -d --name X` 还会让 daemon 在创建早期 **reserve 名字 X 给一个 ghost container ID** 然后卡死不返回；ghost ID 不存在于 `docker container ls -a`、`docker inspect`、`/var/lib/docker/containers/`，但 daemon 内部 name registry 仍持有，`docker rm` 删不掉。每次 retry 累积新 ghost。

队友已运行的容器（`octopus_openclaw` 等）还能 healthy 服务，但任何**新建容器**都启不起来。

**唯一修复**：restart docker daemon（影响队友）或 reboot 远端（影响所有人）。无侵入性的本地修复路径已穷尽。

---

## 新增问题 #2：HiSilicon UVC 摄像头 driver wedged（2026-04-25 17:50，M agent 触发）

M agent 改造 cam_stream 期间触发：调 `cam_stream/stop.sh && cam_stream/run.sh` 重启时，HiSilicon UVC driver 卡死：
- 3 个 `cam_stream` pid 卡在 D-state（不可中断 sleep）
- WCHAN 显示：`uvc_v4l2_get_format`, `cma_alloc`, `flush_work`
- 即使 SIGKILL + USB unbind/rebind 都没救
- `/dev/video0` 设备消失，只剩 `/dev/video1`、`/dev/video2`（rebind 副作用）
- driver 在 `a5100000.hiusbc2` 这个 USB controller

**M agent 没尝试解 driver 死锁**——守约束没 reboot。**修复路径 = reboot 远端**（同 docker / NPU）。

恢复后 M agent 写的新 cam_stream 代码会自动尝试 video0..video9 多个设备，应该能重新接上摄像头。

---

## 用过的 docker 启动命令（按时间）

### 命令 1：J agent 的初版（含 anonymous volume）

```bash
docker run -d --name mmclaw-dev \
  -p 127.0.0.1:18890:18789 \
  -v /root/mmclaw/openclaw:/app \
  -v /app/node_modules \
  -v /app/dist \
  -w /app \
  --user root \
  openclaw_423
```

**现象**：
- `docker run` 命令本身 hang 8+ 分钟没返回
- `ps -ef` 看到 PID 30220 (bash 父) 和 PID 30222 (docker run client) 持续 sleep
- `docker container ls -a` 看不到 mmclaw-dev
- 但 daemon 已经把 name `mmclaw-dev` reserve 给 ghost ID `7a896395343942ed8bfd4ad92561da7a43ac3e53602f9220426c41fa31ccccb7`

### 命令 2：我尝试清理后重跑同命令

```bash
docker rm -f mmclaw-dev    # → Error: No such container: mmclaw-dev
docker run -d --name mmclaw-dev ...同命令1...
```

**现象**：
- rm 说没有
- run 报 `Conflict. The container name "/mmclaw-dev" is already in use by container "7a896395..."`
- daemon 内部 name registry 与 CLI 视图不一致

### 命令 3：换名 mmclaw-dev2（仍带 anonymous volume）

```bash
docker run -d --name mmclaw-dev2 \
  -p 127.0.0.1:18890:18789 \
  -v /root/mmclaw/openclaw:/app \
  -v /app/node_modules \
  -v /app/dist \
  -w /app \
  --user root \
  openclaw_423
```

**现象**：
- `docker run` 命令再次 hang 8+ 分钟
- PID 31861 持续 sleep
- 我 kill PID 30220 30222 31860 31861 后，daemon 仍持有 `mmclaw-dev2` 的 ghost reservation

### 命令 4：按 ID 强删 ghost

```bash
docker rm -f 7a896395343942ed8bfd4ad92561da7a43ac3e53602f9220426c41fa31ccccb7
```

**现象**：`Error: No such container: 7a896395...`——按 ID rm 也失败。

### 命令 5：尝试简化版（**无任何挂载**）

```bash
docker run -d --name mmclaw-dev -p 127.0.0.1:18890:18789 openclaw_423
```

**现象**：仍然 `Conflict. The container name "/mmclaw-dev" is already in use by container "7a896395..."`。

**关键**：**即使无挂载也无法启动**——所以问题不仅是 anonymous volume 的拷贝慢，是 name lock 死锁。

### 命令 6：再换全新名字 mmclaw-dev3（最简版）

```bash
docker run -d --name mmclaw-dev3 -p 127.0.0.1:18890:18789 openclaw_423
```

**第一次运行**（被 stop 命令 kill 之前）：
- docker run 本身 hang
- daemon 已 reserve `mmclaw-dev3` 给新 ghost ID `760a3fc7396860ccf559abdd384ff290f02c93dc711f59a094d05282ff8a3c17`

**第二次运行**：
- `Conflict. The container name "/mmclaw-dev3" is already in use by container "760a3fc7..."`

**关键**：**每次 `docker run -d` 都创造一个新 ghost name reservation**。

---

## 辅助诊断命令与结果

| 命令 | 结果 | 含义 |
|---|---|---|
| `docker container ls -a --filter name=mmclaw` | 空 | ghost 不在 ls 列表 |
| `docker inspect 7a896395...` | `Error: No such object` | daemon API 也找不到这个 ID |
| `ls /var/lib/docker/containers/ \| grep 7a896` | (not in directory) | 文件系统里没这个容器目录 |
| `ls /var/lib/docker/containers/ \| wc -l` | 4 | 远端总共只有 4 个容器在 fs 上 |
| `docker container prune -f` | `Total reclaimed space: 0B` | prune 能跑，但找不到要清的 stopped 容器 |
| `ctr -n moby c list` | `failed to dial /run/containerd/containerd.sock: context deadline exceeded` | **containerd socket 自己也 timeout** |
| `curl --unix-socket /var/run/docker.sock 'http://localhost/containers/json?all=true'` | hang 无返回 | docker daemon REST API 部分瘫 |
| `systemctl is-active docker` | `active` | 表面看 daemon 还活着 |
| `journalctl -u docker --since '15 min ago'` | 反复 `stream copy error: reading from a closed fifo` 和 `Health check for container 1d1dc452... error: context deadline exceeded`，每 3 分 10 秒一次 | 队友容器 `1d1dc452` 的 health check 进入 timeout 飞循环，daemon 一直在重试 |

---

## 远端镜像清单（实测）

```
openclaw_423:latest                              db8dd9c1f4c5  2.7GB    ← 我们的镜像（用户的，专属）
m.daocloud.io/ghcr.io/openclaw/openclaw:<none>   db8dd9c1f4c5  2.7GB    ← 同 ID 别 tag

m.daocloud.io/ghcr.io/openclaw/openclaw:latest   070ef3bab46d  2.62GB   ← 队友镜像（原始 OpenClaw）
openclaw:local                                   070ef3bab46d  2.62GB   ← 队友的 tag
ghcr.io/openclaw/openclaw:latest                 070ef3bab46d  2.62GB   ← 队友的 tag
```

我所有 `docker run` 都用 `openclaw_423` tag → 对应 `db8dd9c1f4c5`，**没用过队友的 070ef3...**。

---

## 受影响的 ghost name registry（已知）

| Name | Ghost Container ID | Origin |
|---|---|---|
| `mmclaw-dev` | `7a896395343942ed8bfd4ad92561da7a43ac3e53602f9220426c41fa31ccccb7` | J agent 启的 |
| `mmclaw-dev2` | (未确认具体 ID, daemon 已锁 name) | 我跑的命令 3 留下 |
| `mmclaw-dev3` | `760a3fc7396860ccf559abdd384ff290f02c93dc711f59a094d05282ff8a3c17` | 我跑的命令 6 留下 |

---

## 远端运行状态

| 时刻 | uptime | load avg (1/5/15) | 关键事件 |
|---|---|---|---|
| 16:25 | up 57 min | 19.78 / 21.47 / 21.07 | B agent atc 在跑 |
| 16:57 | up 1:28 | 24.99 / 23.87 / 22.87 | J agent docker run hang |
| 17:23 | up 1:54 | **41.76** / 39.51 / 34.17 | 多次 docker run 累积，load 在涨 |

**load 在持续上涨**——daemon 后台还在尝试 finalize 那些卡住的容器创建，烧 CPU。

---

## 根因猜测（按可能性）

### 主因：**docker daemon + containerd 状态损坏**

- `containerd` socket 自己 timeout（`ctr` 连不上）—— 比 docker daemon 更底层瘫了
- daemon 在容器**创建阶段**（namespace 设置、cgroup 创建、等 containerd 接手）卡住
- 卡住后名字 reservation 不释放，但容器没真创建出来
- 每次 retry 累积新的 ghost
- 已运行的容器（队友的）还在跑，但 daemon 已经无法接受新创建请求

### 触发因素（推测）

1. 最初的 `-v /app/node_modules` anonymous volume 触发：daemon 试图 setup 拷贝镜像内 668 个 node_modules 子目录到 anonymous volume，在 ARM 慢 IO + 已存在的高 load 上把 containerd 卡死
2. 或更早就有问题（队友容器 `1d1dc452` 的 health check 一直 timeout 已暴露 daemon 长期紊乱）
3. NPU 硬件 Alarm（同时存在的 LPM/TS IPC 挂死）可能也加剧了系统紊乱

---

## 试过 / 没试的修复

### 试过（无效）

- `docker rm -f <name>` —— 报 No such container
- `docker rm -f <ghost-ID>` —— 报 No such container
- `docker container prune -f` —— 0B reclaimed
- 换 name (mmclaw-dev → dev2 → dev3) —— 每次新 ghost
- 不挂载（minimal docker run）—— 仍卡
- `ctr -n moby` —— socket timeout
- Docker REST API 直接调 —— hang

### 试过 ephemeral test（最关键诊断，新增 2026-04-25 17:33）

```bash
ssh ... 'timeout 30 docker run --rm openclaw_423 echo "hello-mmclaw-ephemeral-test"; echo "---exit=$?---"'
```

**结果**：output 只有 `---exit=124---`，timeout 30 秒**强杀** docker run。

**含义**（conclusive）：
- 不带 `--name`、不带 mount、最简的一次性容器**也卡 30 秒以上**
- daemon **完全无法创建任何新容器**
- 不是名字 lock 问题、不是 anonymous volume 问题——是 daemon 整体瘫痪
- 任何 `docker run`、`docker create` 都会卡

### 没试

- `docker create` + `docker start`（分两步）—— 大概率同样卡（创建阶段就 hang）
- `systemctl restart docker` + `systemctl restart containerd`（**会断队友所有容器**，需用户授权）
- 远端 `reboot`（**用户明确拒绝**，且会断所有共享用户）
- 改 docker daemon 元数据 db（极度危险）

---

## 影响

| 影响 | 说明 |
|---|---|
| 🔴 mmclaw-dev 容器无法启动 | 个性化 OpenClaw 跑不起来 |
| 🔴 PET-1/2/3 改造无法在容器内验证 | L agent 已写好 `pet-multimodal-embedding` extension，等容器恢复才能 reload + 测 |
| 🟡 队友容器仍 healthy | 不影响他们已跑的服务，但他们也启不了新容器 |
| 🟡 远端 load 在涨 | daemon 后台烧 CPU 处理卡住的容器 |

---

## 建议下一步（待用户决策）

1. **A. 跑 ephemeral 测试**：`docker run --rm openclaw_423 echo hello` —— 不带 name，跑完即删，验证 daemon 是否还能创建任何新容器（不会留 ghost）
2. **B. systemctl restart docker（+ containerd）** —— 风险：队友容器全停（开机自启的会自动起，systemd unit 没配的需手动起）
3. **C. reboot** —— 一举解决 docker + NPU Alarm（K agent 诊断的硬件层），断所有共享用户
4. **D. 等队友空闲时机** —— 暂缓 docker 改造，L agent 的 `pet-multimodal-embedding` 可用 dry-run 模式（`MMEMB_DRY_RUN=1`）在本机离线 smoke test

用户**明确拒绝 reboot（C）**，A/B/D 待选。

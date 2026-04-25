# NPU Health=Alarm 诊断报告

**实测时间**：2026-04-25 16:54（Asia/Shanghai）  
**设备**：Ascend 310B4，NPU 0，npu-smi 23.0.0

---

## 实测关键输出

### npu-smi info（摘要）
```
| 0       310B4                 | Alarm           | 0.0          64                15    / 15            |
| 0       0                     | NA              | 0            6680 / 15609                            |
```
- Health=**Alarm**，Temp=64°C，Hugepages=15/15（全满），Memory=6680/15609 MB

### npu-smi info -t health -i 0 -c 0（最关键）
```
Health Status    : Alarm
Error Code       : 80E3A203
Error Information: node type=LPM, sensor type=Chip Hardware,
                   event state=The function of obtaining the current is abnormal
```

### dmesg 关键错误（从开机持续循环，每~5秒一次）
```
[ascend][icm][icm_ipc_msg_send_sync 553] Wait event timeout (count=21, wait_time=30000, chan_id=13)
[ascend][ipcdrv][mdev_ensure_channel 585] Wait mailbox-1-acpu1-tx-ts ipc channel idle timeout
[ascend][ERROR][devdrv][devdrv_imu_mbx3_notifier 173] Get mntn message. (exception_id=0xa6193215)
[ascend][ERROR][dms_module][dms_get_msg_from_ts 261] icm send msg failed. (cmd_type=33; ret=110)
```
- `exception_id=0xa6193215`：从开机后第 23 秒开始，每 ~301 秒（5 分钟）上报一次，截至诊断时已上报 **20 次**，持续整个运行周期（uptime 1h26m）。
- `ret=110`：Linux errno ETIMEDOUT，IPC 消息发送超时。
- `chan_id=13`：mailbox channel 13（acpu1 → TS，即 Task Scheduler 通道）永久 idle timeout。

### hisi-i2c 错误
```
hisi-i2c 82070000.i2c8: hisi_i2c_handle_errors_v400: slave address not acknowledged (7bit mode)
```

---

## Alarm 含义定义

华为 Ascend NPU health 状态机（npu-smi 定义）：

| 状态 | 含义 |
|------|------|
| OK | 正常运行 |
| Warning | 轻度异常，可继续使用但需关注 |
| **Alarm** | **硬件或固件检测到故障，功能受损，NPU 仍上电但不保证推理正确或完成** |
| Critical | 严重故障，通常不可用 |
| Reserved | 状态未定义 |

**Alarm** 不是软件告警，是 MCU/IMU（板载监控单元）上报的硬件级故障信号。与 Warning 的本质区别：Warning 是"值偏高"，Alarm 是"功能已无法正常工作"。

---

## 当前 Alarm 来源诊断（带证据）

### 根因：LPM 电流采集功能异常 → TS IPC 通道永久挂死

**直接证据（npu-smi -t health）**：
```
Error Code       : 80E3A203
Error Information: node type=LPM, sensor type=Chip Hardware,
                   event state=The function of obtaining the current is abnormal
```
- **LPM**（Low Power Management 模块）的电流监测硬件无法正常工作。错误码 `80E3A203` 是华为内部故障码，对应 LPM 子系统传感器故障。

**级联证据（dmesg）**：
- `exception_id=0xa6193215`：从系统启动第 23 秒即开始报出，说明这不是推理脚本造成的，**开机时就已存在**。
- `mailbox-1-acpu1-tx-ts ipc channel idle timeout`（每 30 秒超时一次，累计 count=23+）：acpu1 到 TS（Task Scheduler）的 IPC mailbox channel 永久无响应，表明 TS 子系统已挂死或无法处理请求。
- `dms_get_msg_from_ts failed, ret=110`：DMS（Device Management System）无法从 TS 获取任何消息（ETIMEDOUT），导致 AICore 利用率查询全部失败。
- `hisi-i2c slave address not acknowledged`：I2C 总线上某个从设备（可能是电流传感器/PMU）无应答，与 LPM 电流采集异常吻合。

### 与温度无关
温度 64-65°C 属于 310B4 正常工作范围（热关机阈值约 100°C），不是 Alarm 触发原因。

### 与 Hugepages 关系
Hugepages=15/15（全满）但这是正常使用态，Memory 6680/15609 MB 未耗尽，非内存原因。

---

## 风险评估

**不建议用于生产推理**。TS（Task Scheduler）IPC 通道挂死意味着 NPU 调度器无法正常收发指令，推理任务提交后会卡在 completion queue 无限等待（即 `trs_logic_cq_recv` 死锁），不会崩溃但永远不会返回结果。

---

## 修复建议（按风险升序）

### (a) 等自然恢复（风险：无，效果：不会恢复）
exception_id 从开机第 23 秒即存在，已运行 1h26m 仍未自愈，**不可能自然恢复**。

### (b) 重启 NPU 用户态服务（风险：低，效果：不确定）
```bash
systemctl restart npu-driver 2>/dev/null || true
# 或重启 davinci_driver 相关服务
```
若 TS 挂死是固件级别问题，用户态重启无法清除 LPM 硬件故障标志，可能无效，但值得尝试，无副作用。

### (c) 重载 NPU driver 模块（风险：中，需要所有 NPU 进程停止）
```bash
# 停所有使用 NPU 的进程后：
rmmod drv_davinci_intf ascend_uda ...  # 按依赖顺序
modprobe drv_davinci_intf
```
可能重置 TS 状态。但 LPM I2C 从设备无应答是硬件问题，重载 driver 不能修复 I2C 故障。

### (d) 物理重启机器（风险：中，效果：最可能恢复）
```bash
reboot
```
能清除 TS 挂死状态，并重新初始化 LPM I2C 链路。如果 I2C 无应答是偶发现象（接触不良、上电时序问题），重启后可能恢复为 OK 状态。**这是最推荐的恢复手段**。

### (e) 硬件排查（风险：需停机，针对持久性故障）
若重启后 Alarm 仍存在：检查 310B4 板卡 LPM 传感器（电流检测 IC）物理连接，可能需要华为售后支持。

---

## 给 F Agent 的建议

**Alarm 是 yolov8 推理卡死（trs_logic_cq_recv）的直接根因**，而非只是相关因素。

证据链：
1. LPM 电流监测异常 → MCU 上报 Alarm 状态
2. TS（Task Scheduler）IPC mailbox channel 13 永久挂死（从开机即存在）
3. 推理任务通过 acpu1→TS 通道提交内核，TS 无响应 → completion queue 永远没有回包 → `trs_logic_cq_recv` 永久阻塞

**F Agent 在修 yolov8 代码之前，必须先解决 Alarm 状态**，否则任何推理代码修复都无效——问题不在应用层，在硬件/固件层。

**推荐行动顺序**：
1. 协调相关人员，在合适时间窗口执行 `reboot`（物理重启开发板）
2. 重启后观察 `npu-smi info` 是否回到 `Health=OK`
3. 若恢复 OK，F Agent 再继续推理代码调试
4. 若重启后 Alarm 仍在，需上报华为技术支持检查 LPM 传感器硬件

**超时配置方面**：即便后续 Alarm 解决，F Agent 也应在推理调用加显式超时（如 60s），避免 TS 再次挂死时脚本永久阻塞。

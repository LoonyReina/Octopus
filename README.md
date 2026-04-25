# NPU inference plugin — yolov8m + generic Ascend 310B4 pyACL skeleton

Workdir on remote: `/root/mmclaw/plugins/npu_infer/`
Target: Atlas / Orange-Pi class **Ascend 310B4** Edge SoC, CANN toolkit 7.0.

This directory contains:

| File | Purpose |
| --- | --- |
| `yolov8m.onnx` | source ONNX (input `images:1x3x320x320`, FP32). Read-only handed in. |
| `yolov8m.om`   | offline model produced by `atc` (51 MB, FP16 on AICore). |
| `npu_infer.py` | clean pyACL skeleton (init / load / IO / sync execute / release). |
| `npu_infer_debug.py` | identical logic but logs every ACL call with `t+ms` timestamps; use when bring-up hangs. |
| `kernel_meta/`, `fusion_result.json` | atc compile artefacts. Safe to delete; regenerated on next atc. |

---

## 1. Remote environment (verified 2026-04-25)

| Component | Version |
| --- | --- |
| Host         | aarch64 Linux, root@192.168.31.51 |
| NPU          | 1× Ascend 310B4, 16 GB shared DDR (`npu-smi info`) |
| CANN toolkit | 7.0.0  (`/usr/local/Ascend/ascend-toolkit/7.0.0`, innerver V100R001C15SPC003B226) |
| `atc`        | 7.0.0 (built into toolkit) |
| `npu-smi`    | 23.0.0 |
| Python       | 3.9.9 |
| pyACL        | bundled `/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/acl.so` |

**Always source the env first** in any shell that calls `atc` or imports `acl`:

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
```

Without this, `python3 -c "import acl"` raises `ModuleNotFoundError: No module named 'acl'` and `atc` is not on `$PATH`.

How to look up the **soc_version** for `atc`:

```bash
npu-smi info        # column "Name" -> 310B4 -> soc_version=Ascend310B4
```

---

## 2. ONNX -> OM conversion (generic template)

Generic command:

```bash
atc \
  --model=<path_to.onnx> \
  --framework=5 \
  --output=<output_basename_no_ext> \
  --input_format=NCHW \
  --input_shape="<input_name>:<N>,<C>,<H>,<W>" \
  --soc_version=Ascend310B4 \
  --log=error
```

`--framework=5` means ONNX. (3 = TF, 0 = Caffe.)

Key knobs:

* `--input_shape` — must be **fully static**. If the ONNX has dynamic dims (`-1`), pin them here (`images:1,3,320,320`). Whatever you pin here is what the runtime input must match exactly; mismatch -> `acl.mdl.execute` returns `ACL_ERROR_INVALID_PARAM`.
* `--input_format` — almost always `NCHW` for vision models. CHW order on disk; ATC re-layouts internally to 5D for AICore.
* `--soc_version=Ascend310B4` — locked to this hardware. **Do not** copy `Ascend310P3` from generic blogs — the .om won't load (`load_from_file` returns 500001).
* `--precision_mode=allow_fp32_to_fp16` (default) — AICore is FP16 native; fine for detectors. Switch to `must_keep_origin_dtype` only if the model is sensitive (rare for YOLO/RT-DETR).
* `--log=error` is enough; use `--log=info` only when debugging op fallback.

**Verified yolov8m command (this repo)**:

```bash
source /usr/local/Ascend/ascend-toolkit/set_env.sh
cd /root/mmclaw/plugins/npu_infer/
atc \
  --model=yolov8m.onnx \
  --framework=5 \
  --output=yolov8m \
  --input_format=NCHW \
  --input_shape="images:1,3,320,320" \
  --soc_version=Ascend310B4 \
  --log=error
```

Result: `yolov8m.om` (51 MB), single input `images` (1,3,320,320 fp32), single output (1,84,2100) fp32 (84 = 4 bbox + 80 COCO classes; 2100 = number of grid anchors at 320×320).

Compile takes ~30–60 s and produces `kernel_meta/` + `fusion_result.json`. Both are reusable cache; deleting them only forces a recompile.

If `atc` complains about an op (`E11001 Op[...] is not supported`), fall-backs in priority order:

1. `--op_select_implmode=high_performance` (default) -> try `high_precision`.
2. Replace the unsupported op upstream in PyTorch (e.g. `torch.nn.SiLU` was the historical pain point on older CANN; 7.0 supports it natively).
3. Use `--insert_op_conf=aipp.cfg` only if you intentionally want ATC to fuse pre-processing (resize/normalize) into the .om — adds complexity, only worth it for camera pipelines.

---

## 3. pyACL inference skeleton (generic, model-agnostic)

`npu_infer.py` follows the canonical pyACL lifecycle:

```
acl.init()
  -> acl.rt.set_device(dev)
  -> acl.rt.create_context(dev)
  -> acl.rt.create_stream()
  -> acl.mdl.load_from_file(om_path) -> model_id
  -> acl.mdl.create_desc() + acl.mdl.get_desc(desc, model_id)
  -> for each input/output index:
        size = acl.mdl.get_input_size_by_index / get_output_size_by_index
        buf  = acl.rt.malloc(size, ACL_MEM_MALLOC_HUGE_FIRST)
        acl.mdl.add_dataset_buffer(dataset, acl.create_data_buffer(buf, size))
  inference loop:
        acl.rt.memcpy(in_buf, in_size, host_ptr, nbytes, H2D)   # synchronous
        acl.mdl.execute(model_id, in_dataset, out_dataset)      # synchronous
        acl.rt.memcpy(host_ptr, nbytes, out_buf, size, D2H)     # synchronous
  teardown (reverse order):
        acl.rt.free(buf)*       # all input/output device buffers
        acl.mdl.destroy_dataset(in/out)
        acl.mdl.destroy_desc(desc)
        acl.mdl.unload(model_id)
        acl.rt.destroy_stream(stream)
        acl.rt.destroy_context(ctx)
        acl.rt.reset_device(dev)
        acl.finalize()
```

Sync vs async — important:

* **Sync path** (this repo): `acl.mdl.execute(...)` blocks until done, no stream argument. `acl.rt.memcpy(...)` in synchronous mode also blocks. **No `synchronize_stream` call is needed.** This is the simplest correct form for single-threaded inference.
* **Async path**: `acl.mdl.execute_async(model_id, in_ds, out_ds, stream)` + `acl.rt.memcpy_async(..., stream)` — these queue work and return immediately. **You MUST end the iteration with `acl.rt.synchronize_stream(stream)` or the host-side `D2H` will read garbage / never finish.** This was a candidate hypothesis for the original hang; not the actual cause here, but it is the #1 footgun on pyACL — if you ever switch to async, do not skip `synchronize_stream`.

---

## 4. The bring-up hang (2026-04-25) — diagnosis & recovery

**Symptoms.** First inference run (PID 23672) sat for 5+ minutes inside `trs_logic_cq_recv` and never returned. Even after `kill -9`, the NPU memory counter stayed at ~6.6 GB (out of 15.6 GB) and `npu-smi info` showed `Health=Alarm`. A subsequent fresh debug run hung at the very first `acl.rt.set_device(0)` — i.e. before our model was even loaded — proving the script logic was fine and the issue is below the user-space layer.

**Root cause (from `dmesg`).** The NPU's TS (Task Scheduler) firmware threw an exception:

```
[ascend] [devdrv] devdrv_imu_mbx3_notifier  Get mntn message. (exception_id=0xa6193215; ...)
[ascend] [icm]    icm_ipc_msg_send_sync     Wait event timeout (chan_id=13, wait_time=30000)
[ascend] [ipcdrv] mdev_ensure_channel       Wait mailbox-1-acpu1-tx-ts ipc channel idle timeout
[ascend] [ERROR] dms_get_msg_from_ts        icm send msg failed. (cmd_type=33; ret=110)
```

Every 30 s the kernel driver retries the `acpu<->ts` mailbox and times out (counter `count=17`+ and climbing). A leaked VDEC kernel thread `[hi_vdec_acl_0_0]` is stuck in uninterruptible D-state with `osal_wait_timeout_uninterruptible`, which is what is holding 6.6 GB of device memory.

In one sentence: **the NPU TS firmware is hung at the driver level and host-side ACL calls block on the kernel mailbox; this is not a Python / pyACL bug**.

**Recovery procedure.** Edge 310B4 does NOT support `npu-smi set -t reset` (the command exists but returns *"This device does not support setting reset."*). The only reliable recovery is:

1. `reboot` the host (preferred — clears IMU/TS firmware, frees the leaked VDEC handle and DDR).
2. After reboot, confirm `npu-smi info` shows `Health=OK` and `Memory-Usage` <= 50 MB before re-running.
3. If reboot is not possible, try `rmmod davinci_manager davinci_cdev davinci_drv ; modprobe ...` (driver re-load) — sometimes works, often itself hangs because the leaked D-state thread holds a refcount.

**How to avoid this loop next time.**

* Always wrap a fresh inference run in a host-side timeout: `timeout 60 python3 npu_infer.py`. If it doesn't finish in 60 s on a 51 MB model, something is wrong below ACL — don't let it accumulate.
* After every run, `npu-smi info` should show memory back to baseline. If not, **stop and reboot before the next iteration** — running on top of a dirty NPU just produces more leaked handles.
* Do **not** Ctrl-C / `kill -9` a running pyACL process unless you have to. The clean path is to let `acl.finalize()` run; SIGKILL is what creates the orphan VDEC kernel threads in the first place. Use `try/finally -> model.release()` (already in `npu_infer.py`).
* Watch `dmesg -wT | grep -iE 'davinci|ascend|ipcdrv'` in a side terminal during bring-up; the IMU/TS exception prints there before user-space notices.

---

## 5. Run it

```bash
ssh root@192.168.31.51
cd /root/mmclaw/plugins/npu_infer/
source /usr/local/Ascend/ascend-toolkit/set_env.sh
timeout 60 python3 npu_infer.py             # production-ish
# or, if anything looks suspicious:
timeout 60 python3 -u npu_infer_debug.py    # per-step timestamps
```

Expected output (once NPU health is OK — current host is *not* in this state, see §4):

```
[npu_infer] loading /root/mmclaw/plugins/npu_infer/yolov8m.om
[npu_infer] runs=5  avg_latency=<N> ms
[npu_infer] output[0] shape=(1, 84, 2100) dtype=float32 min=... max=...
[npu_infer] top-3 anchors (random input, scores meaningless but confirms pipeline):
  {'rank_anchor': ..., 'score': ..., 'cls_id': ..., 'box_cxcywh': [...]}
[npu_infer] released. done.
```

---

## 6. Adapting this for the next model (face / pose / etc.)

What stays the same (~95 % of the code):

* The whole pyACL lifecycle in `NpuModel` (init / load / malloc / execute / release).
* The H2D/D2H memcpy plumbing.
* The teardown order.

What you change per model:

| Change | Where | Notes |
| --- | --- | --- |
| `MODEL_PATH` | top of script | new `.om` file. |
| `--input_shape` for `atc` | conversion command | **face**: usually 112×112 or 160×160; **pose (RTMPose-tiny)**: 256×192; **YOLOv8 detection**: 320 / 416 / 640. Match whatever the source ONNX expects — read it with `python3 -c "import onnx; m=onnx.load('x.onnx'); print(m.graph.input)"`. |
| Random input shape | `np.random.rand(...)` | must match `--input_shape` exactly (B, C, H, W, fp32). |
| Pre-processing | before `model.infer(x)` | YOLO: BGR->RGB, /255, NCHW, letterbox-pad. Face recognition: aligned crop + mean/std normalize. Pose: keep aspect, pad to 256×192. None of this is in the .om unless you used `--insert_op_conf=aipp.cfg`. |
| Post-processing | replace `parse_yolov8_top` | YOLO: NMS over (84, N). Face embedding: just `outs[0].squeeze()` -> a 512-d vector. Pose: argmax over heatmap or SimCC decode. |
| Number of outputs | `for i in range(n_out)` | YOLO single-output (1,84,N). Some models (anchor-based, multi-FPN heads) have 3 outputs. The skeleton already handles N outputs. |

**Two pitfalls that bite every time:**

1. **`input_shape` mismatch** between `atc` and runtime. The .om bakes in static shape; `acl.rt.memcpy` will return success with the wrong nbytes silently if you compute it from the wrong array, then `execute` blocks or returns garbage. Always assert `x.nbytes == acl.mdl.get_input_size_by_index(desc, 0)` (the skeleton does this).
2. **NHWC vs NCHW.** ONNX exported from PyTorch is NCHW; ONNX exported from TF/Keras is NHWC. If you copy NHWC bytes into an NCHW-compiled .om, the inference runs but the output is wrong. Use `--input_format=NHWC` in `atc` to match, **or** transpose before memcpy. Don't do both.

---

## 7. Pulling artefacts back to your laptop

Whole workdir:

```bash
rsync -avz root@192.168.31.51:/root/mmclaw/plugins/npu_infer/ ./local-path/
```

Just the deliverables (skip 100 MB onnx + compile cache):

```bash
rsync -avz \
  --exclude='*.onnx' --exclude='kernel_meta/' --exclude='fusion_result.json' \
  root@192.168.31.51:/root/mmclaw/plugins/npu_infer/ ./local-path/
```

Authentication: `ssh -i ~/.ssh/id_rsa root@192.168.31.51` (key-based, no password).

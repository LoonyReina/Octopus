#!/usr/bin/env python3
"""Debug version of NPU inference with per-step timestamp logging."""

import os
import sys
import time
import numpy as np

import acl

ACL_MEM_MALLOC_HUGE_FIRST = 0
ACL_MEM_MALLOC_NORMAL_ONLY = 2
ACL_MEMCPY_HOST_TO_DEVICE = 1
ACL_MEMCPY_DEVICE_TO_HOST = 2
ACL_SUCCESS = 0

MODEL_PATH = "/root/mmclaw/plugins/npu_infer/yolov8m.om"
DEVICE_ID = 0

T0 = time.perf_counter()


def log(msg: str):
    dt = (time.perf_counter() - T0) * 1000.0
    print(f"[t+{dt:8.2f}ms] {msg}", flush=True)


def check(ret, what):
    if ret != ACL_SUCCESS:
        raise RuntimeError(f"{what} FAILED ret={ret}")
    log(f"  ok: {what}")


def main():
    log("START")
    log("acl.init()")
    check(acl.init(), "acl.init")

    log(f"acl.rt.set_device({DEVICE_ID})")
    check(acl.rt.set_device(DEVICE_ID), "set_device")

    log("acl.rt.create_context")
    ctx, ret = acl.rt.create_context(DEVICE_ID)
    check(ret, "create_context")

    log("acl.rt.create_stream")
    stream, ret = acl.rt.create_stream()
    check(ret, "create_stream")

    log(f"acl.mdl.load_from_file({MODEL_PATH})")
    model_id, ret = acl.mdl.load_from_file(MODEL_PATH)
    check(ret, "load_from_file")
    log(f"  model_id={model_id}")

    log("acl.mdl.create_desc + get_desc")
    desc = acl.mdl.create_desc()
    check(acl.mdl.get_desc(desc, model_id), "get_desc")

    n_in = acl.mdl.get_num_inputs(desc)
    n_out = acl.mdl.get_num_outputs(desc)
    log(f"  num_inputs={n_in}  num_outputs={n_out}")

    in_size = acl.mdl.get_input_size_by_index(desc, 0)
    in_dims, _ = acl.mdl.get_input_dims(desc, 0)
    log(f"  input[0] size={in_size}B dims={in_dims}")

    out_dims_list = []
    out_sizes = []
    for i in range(n_out):
        s = acl.mdl.get_output_size_by_index(desc, i)
        d, _ = acl.mdl.get_output_dims(desc, i)
        log(f"  output[{i}] size={s}B dims={d}")
        out_dims_list.append(tuple(d["dims"]))
        out_sizes.append(s)

    # build input dataset
    log("malloc input device buffer")
    in_buf, ret = acl.rt.malloc(in_size, ACL_MEM_MALLOC_HUGE_FIRST)
    check(ret, "malloc in")
    in_data = acl.create_data_buffer(in_buf, in_size)
    in_ds = acl.mdl.create_dataset()
    _, ret = acl.mdl.add_dataset_buffer(in_ds, in_data)
    check(ret, "add in buffer")

    log("malloc output device buffers")
    out_ds = acl.mdl.create_dataset()
    out_bufs = []
    for i, s in enumerate(out_sizes):
        b, ret = acl.rt.malloc(s, ACL_MEM_MALLOC_HUGE_FIRST)
        check(ret, f"malloc out{i}")
        d = acl.create_data_buffer(b, s)
        _, ret = acl.mdl.add_dataset_buffer(out_ds, d)
        check(ret, f"add out{i}")
        out_bufs.append((b, s))

    log("prepare random input (1,3,320,320) float32")
    x = np.random.rand(1, 3, 320, 320).astype(np.float32)
    log(f"  x.nbytes={x.nbytes} expected={in_size}")
    if x.nbytes != in_size:
        raise RuntimeError("size mismatch")

    def run_once(label):
        log(f"--- {label} ---")
        log("memcpy H2D")
        host_ptr = acl.util.numpy_to_ptr(x)
        check(acl.rt.memcpy(in_buf, in_size, host_ptr, x.nbytes,
                            ACL_MEMCPY_HOST_TO_DEVICE), "H2D")
        log("acl.mdl.execute (sync)")
        t0 = time.perf_counter()
        ret = acl.mdl.execute(model_id, in_ds, out_ds)
        t1 = time.perf_counter()
        check(ret, f"execute (took {(t1 - t0) * 1000:.2f}ms)")
        log("memcpy D2H all outputs")
        outs = []
        for i, (b, s) in enumerate(out_bufs):
            shape = out_dims_list[i]
            arr = np.zeros(shape, dtype=np.float32)
            check(acl.rt.memcpy(acl.util.numpy_to_ptr(arr), arr.nbytes,
                                b, s, ACL_MEMCPY_DEVICE_TO_HOST),
                  f"D2H out{i}")
            outs.append(arr)
        return outs, (t1 - t0) * 1000

    # warm-up
    outs, _ = run_once("warm-up #1")
    # 4 timed
    times = []
    for k in range(4):
        outs, dt = run_once(f"timed #{k + 1}")
        times.append(dt)

    avg = sum(times) / len(times)
    log(f"=== execute-only avg over {len(times)} runs: {avg:.2f} ms ===")
    for i, o in enumerate(outs):
        log(f"  output[{i}] shape={o.shape} dtype={o.dtype} "
            f"min={o.min():.4f} max={o.max():.4f}")

    # cleanup
    log("cleanup: free buffers")
    acl.rt.free(in_buf)
    for b, _ in out_bufs:
        acl.rt.free(b)
    log("cleanup: destroy datasets / desc / unload model")
    acl.mdl.destroy_dataset(in_ds)
    acl.mdl.destroy_dataset(out_ds)
    acl.mdl.destroy_desc(desc)
    acl.mdl.unload(model_id)
    log("cleanup: stream / context / device / finalize")
    acl.rt.destroy_stream(stream)
    acl.rt.destroy_context(ctx)
    acl.rt.reset_device(DEVICE_ID)
    acl.finalize()
    log("DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"EXC: {e!r}")
        sys.exit(1)

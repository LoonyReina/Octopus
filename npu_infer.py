#!/usr/bin/env python3
"""
NPU inference skeleton for yolov8m.om on Ascend 310B4 via pyACL.

Generic flow (model-agnostic):
  init -> load .om model -> describe IO -> alloc device buffers ->
  copy input H2D -> execute -> copy output D2H -> parse -> release.

Run:
  source /usr/local/Ascend/ascend-toolkit/set_env.sh
  python3 npu_infer.py
"""

import os
import time
import numpy as np

import acl

# ---- ACL constants ----
ACL_MEM_MALLOC_HUGE_FIRST = 0
ACL_MEMCPY_HOST_TO_DEVICE = 1
ACL_MEMCPY_DEVICE_TO_HOST = 2
ACL_SUCCESS = 0
NPY_FLOAT32 = 11  # acl numpy dtype enum for float32

MODEL_PATH = "/root/mmclaw/plugins/npu_infer/yolov8m.om"
DEVICE_ID = 0


def check(ret, what):
    if ret != ACL_SUCCESS:
        raise RuntimeError(f"{what} failed, ret={ret}")


class NpuModel:
    def __init__(self, model_path: str, device_id: int = 0):
        self.model_path = model_path
        self.device_id = device_id

        self.context = None
        self.stream = None
        self.model_id = None
        self.model_desc = None
        self.input_dataset = None
        self.output_dataset = None
        self.input_buffers = []   # list[(buf_ptr, size)]
        self.output_buffers = []  # list[(buf_ptr, size)]
        self.output_shapes = []   # list[tuple]
        self.output_dtypes = []   # list[np.dtype]

        self._init_acl()
        self._load_model()
        self._prepare_io()

    # ---- init / finalize ----
    def _init_acl(self):
        ret = acl.init()
        check(ret, "acl.init")
        ret = acl.rt.set_device(self.device_id)
        check(ret, "acl.rt.set_device")
        self.context, ret = acl.rt.create_context(self.device_id)
        check(ret, "acl.rt.create_context")
        self.stream, ret = acl.rt.create_stream()
        check(ret, "acl.rt.create_stream")

    def _load_model(self):
        self.model_id, ret = acl.mdl.load_from_file(self.model_path)
        check(ret, f"load_from_file({self.model_path})")
        self.model_desc = acl.mdl.create_desc()
        ret = acl.mdl.get_desc(self.model_desc, self.model_id)
        check(ret, "mdl.get_desc")

    def _prepare_io(self):
        # ---- inputs ----
        self.input_dataset = acl.mdl.create_dataset()
        n_in = acl.mdl.get_num_inputs(self.model_desc)
        for i in range(n_in):
            size = acl.mdl.get_input_size_by_index(self.model_desc, i)
            buf, ret = acl.rt.malloc(size, ACL_MEM_MALLOC_HUGE_FIRST)
            check(ret, f"malloc input {i}")
            data = acl.create_data_buffer(buf, size)
            _, ret = acl.mdl.add_dataset_buffer(self.input_dataset, data)
            check(ret, "add_dataset_buffer in")
            self.input_buffers.append((buf, size))

        # ---- outputs ----
        self.output_dataset = acl.mdl.create_dataset()
        n_out = acl.mdl.get_num_outputs(self.model_desc)
        for i in range(n_out):
            size = acl.mdl.get_output_size_by_index(self.model_desc, i)
            buf, ret = acl.rt.malloc(size, ACL_MEM_MALLOC_HUGE_FIRST)
            check(ret, f"malloc output {i}")
            data = acl.create_data_buffer(buf, size)
            _, ret = acl.mdl.add_dataset_buffer(self.output_dataset, data)
            check(ret, "add_dataset_buffer out")
            self.output_buffers.append((buf, size))
            # record shape + dtype for later parsing
            dims, ret = acl.mdl.get_output_dims(self.model_desc, i)
            check(ret, "get_output_dims")
            self.output_shapes.append(tuple(dims["dims"]))
            # for yolov8 output is float32; could be queried via get_output_data_type
            self.output_dtypes.append(np.float32)

    # ---- infer ----
    def infer(self, input_array: np.ndarray) -> list:
        """Run inference. input_array must match input shape & be float32 contiguous."""
        if not input_array.flags["C_CONTIGUOUS"]:
            input_array = np.ascontiguousarray(input_array)
        if input_array.dtype != np.float32:
            input_array = input_array.astype(np.float32)

        host_ptr = acl.util.numpy_to_ptr(input_array)
        in_buf, in_size = self.input_buffers[0]
        nbytes = input_array.nbytes
        if nbytes != in_size:
            raise RuntimeError(f"input nbytes {nbytes} != model input size {in_size}")
        ret = acl.rt.memcpy(in_buf, in_size, host_ptr, nbytes,
                            ACL_MEMCPY_HOST_TO_DEVICE)
        check(ret, "memcpy H2D")

        ret = acl.mdl.execute(self.model_id, self.input_dataset,
                              self.output_dataset)
        check(ret, "mdl.execute")

        outputs = []
        for i, (buf, size) in enumerate(self.output_buffers):
            shape = self.output_shapes[i]
            dtype = self.output_dtypes[i]
            host_arr = np.zeros(shape, dtype=dtype)
            ret = acl.rt.memcpy(acl.util.numpy_to_ptr(host_arr), host_arr.nbytes,
                                buf, size, ACL_MEMCPY_DEVICE_TO_HOST)
            check(ret, f"memcpy D2H out {i}")
            outputs.append(host_arr)
        return outputs

    # ---- cleanup ----
    def release(self):
        for buf, _ in self.input_buffers:
            acl.rt.free(buf)
        self.input_buffers = []
        for buf, _ in self.output_buffers:
            acl.rt.free(buf)
        self.output_buffers = []
        if self.input_dataset is not None:
            acl.mdl.destroy_dataset(self.input_dataset)
            self.input_dataset = None
        if self.output_dataset is not None:
            acl.mdl.destroy_dataset(self.output_dataset)
            self.output_dataset = None
        if self.model_desc is not None:
            acl.mdl.destroy_desc(self.model_desc)
            self.model_desc = None
        if self.model_id is not None:
            acl.mdl.unload(self.model_id)
            self.model_id = None
        if self.stream is not None:
            acl.rt.destroy_stream(self.stream)
            self.stream = None
        if self.context is not None:
            acl.rt.destroy_context(self.context)
            self.context = None
        acl.rt.reset_device(self.device_id)
        acl.finalize()


def parse_yolov8_top(out: np.ndarray, k: int = 3, conf_thr: float = 0.0):
    """
    out shape: (1, 84, N). 84 = 4 box (cx,cy,w,h) + 80 cls scores.
    Returns top-k boxes by max-class-score.
    """
    arr = out[0]  # (84, N)
    boxes = arr[:4, :].T          # (N, 4)
    cls_scores = arr[4:, :]       # (80, N)
    cls_max = cls_scores.max(axis=0)  # (N,)
    cls_id = cls_scores.argmax(axis=0)  # (N,)
    order = np.argsort(-cls_max)[:k]
    results = []
    for idx in order:
        score = float(cls_max[idx])
        if score < conf_thr:
            continue
        cx, cy, w, h = boxes[idx]
        results.append({
            "rank_anchor": int(idx),
            "score": score,
            "cls_id": int(cls_id[idx]),
            "box_cxcywh": [float(cx), float(cy), float(w), float(h)],
        })
    return results


def main():
    print(f"[npu_infer] loading {MODEL_PATH}")
    model = NpuModel(MODEL_PATH, device_id=DEVICE_ID)
    try:
        # yolov8m.om was compiled with input_shape images:1,3,320,320
        x = np.random.rand(1, 3, 320, 320).astype(np.float32)

        # warm-up
        for _ in range(2):
            _ = model.infer(x)

        # timed runs
        N = 5
        t0 = time.perf_counter()
        outs = None
        for _ in range(N):
            outs = model.infer(x)
        t1 = time.perf_counter()

        avg_ms = (t1 - t0) * 1000.0 / N
        print(f"[npu_infer] runs={N}  avg_latency={avg_ms:.2f} ms")
        for i, o in enumerate(outs):
            print(f"[npu_infer] output[{i}] shape={o.shape} dtype={o.dtype} "
                  f"min={o.min():.4f} max={o.max():.4f}")

        # interpret yolov8 head
        top = parse_yolov8_top(outs[0], k=3)
        print("[npu_infer] top-3 anchors (random input, scores meaningless but"
              " confirms pipeline):")
        for r in top:
            print(f"  {r}")
    finally:
        model.release()
        print("[npu_infer] released. done.")


if __name__ == "__main__":
    main()

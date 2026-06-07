"""Bake the 'auto' (idx 101) language prompt into encoder.int8.onnx via ONNX graph surgery.

The prompt is: encoded(B,T,D) -> prompt_kernel(concat([encoded, onehot(101)])) -> (B,T,D), where
prompt_kernel = Linear(D+128, 2D) -> ReLU -> Linear(2D, D). For a FIXED langid the one-hot folds into
Linear1's bias, so the bake = a 2-layer MLP on the encoder output. We splice it before the graph output.
"""
import sys
import numpy as np
import onnx
from onnx import helper, numpy_helper
import nemo.collections.asr as nemo_asr

LANG = 101  # 'auto'
ENC = "1120/encoder.int8.onnx"

print("loading model for prompt_kernel weights ...", flush=True)
m = nemo_asr.models.ASRModel.from_pretrained("nvidia/nemotron-3.5-asr-streaming-0.6b").eval()
pk = m.prompt_kernel
D = int(m._cfg.model_defaults.enc_hidden)
W1 = pk[0].weight.detach().cpu().numpy()   # (2D, D+128)
b1 = pk[0].bias.detach().cpu().numpy()      # (2D,)
W2 = pk[2].weight.detach().cpu().numpy()    # (D, 2D)
b2 = pk[2].bias.detach().cpu().numpy()      # (D,)
assert W1.shape[1] == D + m.num_prompts, (W1.shape, D, m.num_prompts)
W1f_T = np.ascontiguousarray(W1[:, :D].T).astype(np.float32)          # (D, 2D)
b1f = (b1 + W1[:, D + LANG]).astype(np.float32)                       # (2D,)
W2_T = np.ascontiguousarray(W2.T).astype(np.float32)                  # (2D, D)
b2 = b2.astype(np.float32)
print(f"D={D} W1f_T={W1f_T.shape} b1f={b1f.shape} W2_T={W2_T.shape} b2={b2.shape}")

print("loading", ENC, flush=True)
g = onnx.load(ENC)  # loads external data automatically if referenced
gr = g.graph
ext = any(t.data_location == onnx.TensorProto.EXTERNAL for t in gr.initializer)
print("graph outputs:", [o.name for o in gr.output], "| external_data:", ext)

OUT = "outputs"
assert any(o.name == OUT for o in gr.output), f"no '{OUT}' output"
renamed = 0
for n in gr.node:
    for i, o in enumerate(n.output):
        if o == OUT:
            n.output[i] = "enc_raw"
            renamed += 1
assert renamed == 1, f"expected 1 producer of {OUT}, got {renamed}"

gr.initializer.extend([
    numpy_helper.from_array(W1f_T, "pk_W1f_T"),
    numpy_helper.from_array(b1f, "pk_b1f"),
    numpy_helper.from_array(W2_T, "pk_W2_T"),
    numpy_helper.from_array(b2, "pk_b2"),
])
gr.node.extend([
    helper.make_node("Transpose", ["enc_raw"], ["pk_t1"], perm=[0, 2, 1]),      # (B,D,T)->(B,T,D)
    helper.make_node("MatMul", ["pk_t1", "pk_W1f_T"], ["pk_h0"]),
    helper.make_node("Add", ["pk_h0", "pk_b1f"], ["pk_h1"]),
    helper.make_node("Relu", ["pk_h1"], ["pk_r"]),
    helper.make_node("MatMul", ["pk_r", "pk_W2_T"], ["pk_o0"]),
    helper.make_node("Add", ["pk_o0", "pk_b2"], ["pk_o1"]),
    helper.make_node("Transpose", ["pk_o1"], [OUT], perm=[0, 2, 1]),            # (B,T,D)->(B,D,T)
])

onnx.save(g, ENC, save_as_external_data=ext,
          location="encoder.int8.data" if ext else None, all_tensors_to_one_file=True)
print("surgery written ->", ENC)

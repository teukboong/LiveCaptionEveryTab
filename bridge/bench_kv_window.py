"""5차 P0 최종 검증(window-gate 정책). 긴 프롬프트(prompt+256 > sliding window)는 reuse를 건너뛰고
fresh로 처리 → OFF와 동일해야 한다. 짧은 프롬프트는 reuse. 전 구간 invariant 유지. 브릿지 DOWN."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mlx.core as mx
import server
import translator
from mlx_lm.models.cache import make_prompt_cache
from collections import Counter, deque
mx.set_default_device(mx.gpu)
print("loading lm...", flush=True)
server.load_models(asr=False, lm=True, vad=False)

c = make_prompt_cache(server.lm_model)
kinds = Counter(type(x).__name__ for x in c)
rot = [getattr(x, "max_size", None) for x in c if type(x).__name__ == "RotatingKVCache"]
window = min(r for r in rot if r)
budget = int(server._TX_KV_MAX)
print(f"cache: {dict(kinds)} | window={window} budget={budget}", flush=True)

LONG = (" ".join(
    ["The training pipeline streams tokenized shards from object storage into a sharded data loader, "
     "overlaps host-to-device copies with compute, and checkpoints optimizer state every few thousand steps "
     "so a preempted spot node resumes without replaying the whole epoch across availability zones."] * 14)).strip()
STEPS = [
    ("So the first thing to understand", "lecture"),
    ("So the first thing to understand is that the cache stores keys and values.", "lecture"),
    (LONG, "lecture"),                                   # > window -> fresh fallback expected
    ("And that is exactly why longer context costs more memory.", "lecture"),
    ("Throughput improved by 37 percent.", "lecture"),
    (LONG, "lecture"),                                   # > window -> fresh fallback expected
    ("This is not a cache miss.", "lecture"),
    ("It is memory bandwidth, not compute.", "lecture"),
]
def norm(s): return " ".join((s or "").split())
def plen_of(txt, win, reg):
    msgs = [{"role": "system", "content": server._tx_system("Korean", reg, "", [])}]
    for a, b in server._fewshot("Korean", reg, server._src_lang(txt)):
        msgs += [{"role": "user", "content": a}, {"role": "assistant", "content": b}]
    for a, b in win:
        msgs += [{"role": "user", "content": a}, {"role": "assistant", "content": b}]
    msgs.append({"role": "user", "content": txt})
    return len(server.lm_tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False))

print("warm..."); translator._TX_KVREUSE = True; server._reset_tx_cache()
server.translate_once("warm up please", register="lecture")

translator._TX_KVREUSE = False; server._reset_tx_cache()
rec = deque(maxlen=5); off, windows = [], []
for txt, reg in STEPS:
    windows.append(list(rec)); ko = server.translate_once(txt, list(rec), target="Korean", register=reg)
    off.append(ko); rec.append((txt[:160], ko[:160]))

translator._TX_KVREUSE = True; server._reset_tx_cache()
on, inv_fail, skipped = [], [], []
for i, (txt, reg) in enumerate(STEPS):
    plen = plen_of(txt, windows[i], reg)
    will_skip = (plen + server._TX_GEN_MAX + server._TX_WINDOW_MARGIN) > min(budget, window)
    if will_skip: skipped.append(i)
    ko = server.translate_once(txt, windows[i], target="Korean", register=reg); on.append(ko)
    if server._tx_cache is None:
        ok = (len(server._tx_cache_ids) == 0)
    else:
        o = server._tx_cache_offset(server._tx_cache); ok = (o is not None and o == len(server._tx_cache_ids))
    if not ok: inv_fail.append(i)
    print(f"[{i}] plen={plen:5} skip_reuse={will_skip} inv_ok={ok} match={norm(off[i])==norm(on[i])}")

print("\n== gates ==")
div = [i for i in range(len(STEPS)) if norm(off[i]) != norm(on[i])]
fresh_div = [i for i in div if i in skipped]      # over-window steps use fresh = OFF -> any divergence is structural
print(f"structural divergences (over-window fresh != OFF): {fresh_div}  -> MUST be empty")
print(f"accepted near-tie divergences (in-window reuse): {[i for i in div if i not in skipped]}")
print(f"invariant fails: {inv_fail}  -> MUST be empty")
print(f"over-window steps (fresh fallback exercised): {skipped}  -> MUST be non-empty")
ok = (not fresh_div) and (not inv_fail) and bool(skipped)
print(f"\nPASS: {ok}")
sys.exit(0 if ok else 1)

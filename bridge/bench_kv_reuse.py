"""Strengthened integration gate for hardened KV reuse.
- sliding recent_pairs (deque maxlen=5), config changes, identical-ish repeats
- ON vs OFF compared on the SAME prompt timeline (ON reuses OFF's recorded recent_pairs windows)
- semantic-regression clauses must NOT diverge (negation/number/contrast/modality/entity)
- INVARIANT: after every ON call, cache.offset == len(_tx_cache_ids)  (cache holds prompt-only)
Run with the bridge DOWN."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mlx.core as mx
import server
mx.set_default_device(mx.gpu)
print("loading lm...", flush=True)
server.load_models(asr=False, lm=True, vad=False)

# (text, register, target). idx in REGRESSION must match exactly (semantically dangerous to flip).
STEPS = [
    ("So the first thing to understand", "lecture", "Korean"),
    ("So the first thing to understand is that the cache", "lecture", "Korean"),
    ("So the first thing to understand is that the cache stores keys and values.", "lecture", "Korean"),
    ("And that is exactly why longer context costs more memory.", "lecture", "Korean"),
    ("Throughput improved by 37 percent over the previous generation.", "lecture", "Korean"),   # 4 number
    ("This is not a cache miss.", "lecture", "Korean"),                                          # 5 negation
    ("It is memory bandwidth, not compute.", "lecture", "Korean"),                               # 6 contrast
    ("You do not have to restart the server.", "lecture", "Korean"),                             # 7 modality
    ("Blackwell uses FlashAttention and a larger KV cache.", "lecture", "Korean"),               # 8 entities
    ("Hey everyone, welcome back to the stream.", "casual", "Korean"),                           # 9 register switch
    ("So basically the whole thing crashed mid demo.", "casual", "Korean"),                      # 10
    ("So the first thing to understand", "lecture", "Korean"),                                   # 11 config switch back
    ("Throughput improved by 37 percent over the previous generation.", "lecture", "Korean"),    # 12 same text, new context
    ("Throughput improved by 37 percent over the previous generation.", "lecture", "Korean"),    # 13 identical repeat
]
REGRESSION = {4, 5, 6, 7, 8}
def norm(s): return " ".join((s or "").split())

print("warm..."); server._TX_KVREUSE = True; server._reset_tx_cache()
server.translate_once("warm up please", register="lecture")

from collections import deque

# pass 1: OFF (fresh cache) — record output + the recent_pairs window used at each step
server._TX_KVREUSE = False; server._reset_tx_cache()
rec = deque(maxlen=5); off, windows, t_off = [], [], []
for txt, reg, tgt in STEPS:
    win = list(rec)
    windows.append(win)
    t0 = time.perf_counter()
    ko = server.translate_once(txt, win, target=tgt, register=reg)
    t_off.append((time.perf_counter() - t0) * 1000)
    off.append(ko)
    rec.append((txt[:160], ko[:160]))

# pass 2: ON (KV reuse) — feed the SAME windows so prompts are identical; check invariant each call
server._TX_KVREUSE = True; server._reset_tx_cache()
on, t_on, inv_fail = [], [], []
for i, (txt, reg, tgt) in enumerate(STEPS):
    t0 = time.perf_counter()
    ko = server.translate_once(txt, windows[i], target=tgt, register=reg)
    t_on.append((time.perf_counter() - t0) * 1000)
    on.append(ko)
    off_len = server._tx_cache_offset(server._tx_cache)
    ids_len = len(server._tx_cache_ids)
    if off_len is None or off_len != ids_len:
        inv_fail.append((i, off_len, ids_len))

print("\n== output (OFF | ON) ==")
for i, (txt, _, _) in enumerate(STEPS):
    tag = " [REG]" if i in REGRESSION else ""
    flag = "" if norm(off[i]) == norm(on[i]) else "  <<< DIVERGE"
    print(f"[{i:2}]{tag} {t_off[i]:5.0f}->{t_on[i]:5.0f}ms{flag}")
    print(f"     OFF: {off[i]}")
    print(f"     ON : {on[i]}")

print("\n== gates ==")
norm_div = [i for i in range(len(STEPS)) if norm(off[i]) != norm(on[i])]
reg_div = [i for i in norm_div if i in REGRESSION]
print(f"normalized divergences: {norm_div}  (allowed: synonym flips outside regression set)")
print(f"REGRESSION divergences: {reg_div}  -> MUST be empty")
print(f"invariant fails (offset != len(_tx_cache_ids)): {inv_fail}  -> MUST be empty")
print(f"\nsaved total: {sum(t_off)-sum(t_on):.0f}ms ({100*(sum(t_off)-sum(t_on))/sum(t_off):.0f}%)")
ok = (not reg_div) and (not inv_fail)
print(f"\nPASS: {ok}")

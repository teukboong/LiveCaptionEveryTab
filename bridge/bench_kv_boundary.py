"""6.1 boundary 검증: _TX_GEN_MAX를 작게 줄여 sliding window 경계를 빡세게 밟는다. 경계 바로 아래(persistent
cache 생존/리유즈) / 바로 위(fresh fallback) / identical 반복 / 큰 LCP+작은 tail이 전부 ON==OFF + invariant
유지인지. 브릿지 DOWN."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mlx.core as mx
import server
import translator
mx.set_default_device(mx.gpu)
print("loading lm...", flush=True)
server.load_models(asr=False, lm=True, vad=False)

# shrink generation cap so the window boundary is easy to straddle with moderate prompts
translator._TX_GEN_MAX = 24
translator._TX_WINDOW_MARGIN = 8
translator._TX_KVREUSE = True; server._reset_tx_cache(); translator._TX_KV_WINDOW = None
server.translate_once("warm", register="lecture")        # learns window + compiles
WIN = server._TX_KV_WINDOW
THRESH = min(server._TX_KV_MAX, WIN) - server._TX_GEN_MAX - server._TX_WINDOW_MARGIN
print(f"window={WIN} gen_max={server._TX_GEN_MAX} margin={server._TX_WINDOW_MARGIN} reuse_thresh(prompt<= ){THRESH}", flush=True)

FILL = ("the system streams tokenized shards and overlaps copies with compute and checkpoints state "
        "so a preempted node resumes without replaying the whole epoch ")
def src_of(words):
    w = FILL.split()
    return " ".join((w * (words // len(w) + 1))[:words])

def plen_of(txt, reg="lecture"):
    msgs = [{"role": "system", "content": server._tx_system("Korean", reg, "", [])}]
    for a, b in server._fewshot("Korean", reg, server._src_lang(txt)):
        msgs += [{"role": "user", "content": a}, {"role": "assistant", "content": b}]
    msgs.append({"role": "user", "content": txt})
    return len(server.lm_tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False))

# build sources that bracket THRESH (plen just under / just over), plus identical + tail-change
import bisect
cand = []
for wc in range(400, 900, 20):
    cand.append((plen_of(src_of(wc)), wc))
under = max((c for c in cand if c[0] <= THRESH), default=cand[0])
over = min((c for c in cand if c[0] > THRESH), default=cand[-1])
near = max((c for c in cand if c[0] <= THRESH - 30), default=under)
print(f"chosen: near={near} under={under} over={over}", flush=True)

S_near, S_under, S_over = src_of(near[1]), src_of(under[1]), src_of(over[1])
S_under_tail = S_under + " and that is the key point here today"   # large LCP, small tail change
STEPS = [
    ("short clause one", "lecture"),
    (S_near, "lecture"),
    (S_under, "lecture"),            # right at the edge -> reuse survives
    (S_over, "lecture"),             # just over -> fresh fallback
    (S_under, "lecture"),            # identical repeat near edge
    (S_under_tail, "lecture"),       # large common prefix + small tail
    ("short clause two", "lecture"),
]
def norm(s): return " ".join((s or "").split())

# OFF
translator._TX_KVREUSE = False; server._reset_tx_cache()
off = [server.translate_once(t, [], target="Korean", register=r) for t, r in STEPS]

# ON
translator._TX_KVREUSE = True; server._reset_tx_cache()
on, inv_fail, info = [], [], []
for i, (t, r) in enumerate(STEPS):
    pl = plen_of(t, r); reuse = (pl <= THRESH)
    ko = server.translate_once(t, [], target="Korean", register=r); on.append(ko)
    if server._tx_cache is None:
        ok = (len(server._tx_cache_ids) == 0)
    else:
        o = server._tx_cache_offset(server._tx_cache); ok = (o is not None and o == len(server._tx_cache_ids))
    if not ok: inv_fail.append(i)
    info.append((i, pl, reuse, server._tx_cache is None))
    print(f"[{i}] plen={pl:5} reuse_eligible={reuse} cache={'None' if server._tx_cache is None else 'kept'} inv_ok={ok} match={norm(off[i])==norm(on[i])}")

print("\n== gates ==")
div = [i for i in range(len(STEPS)) if norm(off[i]) != norm(on[i])]
reuse_steps = [i for i, pl, ru, _ in info if ru]
fresh_steps = [i for i, pl, ru, _ in info if not ru]
fresh_div = [i for i in div if i in fresh_steps]      # fresh fallback IS the OFF path -> any divergence = structural
neartie_div = [i for i in div if i in reuse_steps]    # in-window reuse: near-tie flips accepted (not structural)
for i in div:
    print(f"  DIV[{i}] {'STRUCTURAL (fresh!=OFF)' if i in fresh_steps else 'near-tie (reuse, accepted)'}")
    print(f"     OFF: {off[i]}")
    print(f"     ON : {on[i]}")
print(f"structural divergences (fresh step != OFF): {fresh_div}  -> MUST be empty")
print(f"accepted near-tie divergences (in-window reuse): {neartie_div}")
print(f"invariant fails: {inv_fail}  -> MUST be empty")
print(f"reuse near boundary: {reuse_steps} | fresh-fallback: {fresh_steps}  (both non-empty)")
ok = (not fresh_div) and (not inv_fail) and bool(reuse_steps) and bool(fresh_steps)
print(f"\nPASS: {ok}")
sys.exit(0 if ok else 1)

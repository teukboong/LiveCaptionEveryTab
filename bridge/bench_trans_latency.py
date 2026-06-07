"""Translation-method latency: sentence re-translate (whole growing sentence each clause)
vs incremental (only the new clause). Mirrors translate_once (system + recent context, fresh cache).
Run with the bridge stopped (single 26B resident)."""
import time
import mlx.core as mx
from mlx_lm import load as lm_load, stream_generate
from mlx_lm.sample_utils import make_sampler
from mlx_lm.models.cache import make_prompt_cache

LM = "mlx-community/gemma-4-26b-a4b-it-6bit"
sampler = make_sampler(temp=0.0)

# a realistic Computex sentence, split into clauses (the way pauses would segment it)
CLAUSES = [
    "Today, we're announcing",
    "our next-generation Blackwell GPU architecture,",
    "which delivers five petaflops of AI compute",
    "and ships in the fourth quarter.",
]

print(f"[bench] loading {LM} …", flush=True)
model, tok = lm_load(LM)


def translate(text, recent=""):
    mx.set_default_device(mx.gpu)
    sysmsg = ("You are a real-time interpreter rendering a continuous talk/stream into natural Korean. "
              "Translate the user's line into fluent Korean, keeping names, terminology, and tone consistent. "
              "The line may be cut off mid-sentence — translate what is there as naturally as possible. "
              + (f"Preceding speech (context only): {recent} " if recent else "")
              + "Output ONLY the Korean translation, nothing else.")
    msgs = [{"role": "system", "content": sysmsg}, {"role": "user", "content": text}]
    try:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
    cache = make_prompt_cache(model, max_kv_size=2048)
    out, n = [], 0
    t0 = time.perf_counter()
    for r in stream_generate(model, tok, prompt, max_tokens=256, sampler=sampler, prompt_cache=cache):
        out.append(r.text); n += 1
    return "".join(out).strip(), time.perf_counter() - t0, n


print("[bench] warm…", flush=True)
translate("hello world")

print("\n===== Method 1: SENTENCE RE-TRANSLATE (whole growing sentence each clause) =====", flush=True)
m1_total = 0.0
for i in range(len(CLAUSES)):
    prefix = " ".join(CLAUSES[:i + 1])
    ko, dt, n = translate(prefix)
    m1_total += dt
    print(f"  clause {i+1}: {len(prefix.split()):2d}w -> {dt:4.2f}s ({n}tok)  {ko}", flush=True)
print(f"  >>> per-clause max {dt:.2f}s | total translate work {m1_total:.2f}s", flush=True)

print("\n===== Method 2: INCREMENTAL (translate only the new clause, append) =====", flush=True)
m2_total = 0.0; recent = ""
for i, clause in enumerate(CLAUSES):
    ko, dt, n = translate(clause, recent=recent)
    recent = (recent + " " + clause).strip()
    m2_total += dt
    print(f"  clause {i+1}: {len(clause.split()):2d}w -> {dt:4.2f}s ({n}tok)  {ko}", flush=True)
print(f"  >>> per-clause max {dt:.2f}s | total translate work {m2_total:.2f}s", flush=True)

print(f"\n===== SUMMARY =====")
print(f"  Method 1 (re-translate): last/longest clause {m1_total and ''}— see above; total {m1_total:.2f}s")
print(f"  Method 2 (incremental) : total {m2_total:.2f}s")
print(f"  재번역은 절이 길수록 per-step↑(마지막 절이 가장 느림). 증분은 per-step 일정.")

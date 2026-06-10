"""Bench: 26B-A4B translation — baseline vs speculative decoding (E2B draft).

Greedy (temp=0) so spec decoding is EXACT: output must equal baseline token-for-token.
That doubles as a correctness check (lossless == True) while we read off the speedup
and the draft acceptance rate (GenerationResponse.from_draft).

Run AFTER stopping the bridge (no concurrent 26B). Single process, one model resident.
"""
import time, gc, sys
import mlx.core as mx
from mlx_lm import load as lm_load, stream_generate
from mlx_lm.sample_utils import make_sampler

MAIN = "mlx-community/gemma-4-26b-a4b-it-6bit"
DRAFTS = {
    "E2B-4bit": "mlx-community/gemma-4-E2B-it-4bit",
    "E2B-8bit": "mlx-community/gemma-4-E2B-it-8bit",
}
DRAFT_TOKENS = [2, 4, 6]          # num_draft_tokens sweep
TARGET = "Korean"

# clause-sized source lines, like what the ASR stage emits into the translator
SRC = [
    "Okay so the patch notes just dropped and they completely reworked the ranked matchmaking system.",
    "Honestly I think this build is way stronger than what everyone is running right now.",
    "Let me check the chat real quick, someone is asking about my keybinds.",
    "We are going to push mid and try to force a fight before their ultimates come back up.",
    "That was such a clutch play, I cannot believe we actually won that round.",
    "If you look at the minimap you can see they are setting up for a sneaky baron call.",
    "Thanks for the follow, and welcome to everybody who just joined the stream.",
]

SYS = (f"You are a real-time interpreter rendering a continuous talk/stream into natural {TARGET}. "
       "Each user message is one sentence or clause of the source speech. Translate it into fluent "
       f"{TARGET}, keeping names, terminology, and tone consistent with the ongoing context. "
       f"Output ONLY the {TARGET} translation, nothing else.")

sampler = make_sampler(temp=0.0)


def build_prompt(tok, text):
    msgs = [{"role": "system", "content": SYS}, {"role": "user", "content": text}]
    try:
        return tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
    except TypeError:
        return tok.apply_chat_template(msgs, add_generation_prompt=True)


def run(model, tok, draft=None, ndraft=4):
    """Translate every SRC line fresh; return (outputs, total_gen_tokens, wall_s, accept_rate)."""
    outs, gen_toks, accepted, wall = [], 0, 0, 0.0
    for text in SRC:
        prompt = build_prompt(tok, text)
        kw = {"max_tokens": 256, "sampler": sampler}
        if draft is not None:
            kw["draft_model"] = draft
            kw["num_draft_tokens"] = ndraft
        pieces, n, acc = [], 0, 0
        t0 = time.perf_counter()
        for r in stream_generate(model, tok, prompt, **kw):
            pieces.append(r.text)
            n += 1
            if getattr(r, "from_draft", False):
                acc += 1
        wall += time.perf_counter() - t0
        outs.append("".join(pieces).strip())
        gen_toks += n
        accepted += acc
    rate = (accepted / gen_toks) if gen_toks else 0.0
    return outs, gen_toks, wall, rate


def main():
    print(f"[bench] loading MAIN {MAIN} …", flush=True)
    model, tok = lm_load(MAIN)

    print("[bench] warm + BASELINE (no draft)…", flush=True)
    run(model, tok)                                   # warm graph
    base_out, base_n, base_wall, _ = run(model, tok)
    base_tps = base_n / base_wall
    print(f"\n=== BASELINE ===\n  {base_n} tok / {base_wall:.2f}s = {base_tps:.1f} tok/s "
          f"({base_wall/len(SRC):.2f}s/line)\n", flush=True)

    results = [("baseline", None, base_tps, base_wall, 0.0, base_out)]
    ok = True
    for dname, dpath in DRAFTS.items():
        print(f"[bench] loading draft {dname} {dpath} …", flush=True)
        try:
            dmodel, dtok = lm_load(dpath)
        except Exception as e:
            print(f"  draft load FAILED: {e}", flush=True); continue
        # vocab compatibility (spec decoding needs shared tokenizer)
        if dtok.vocab_size != tok.vocab_size:
            print(f"  ⚠ vocab mismatch {dtok.vocab_size} vs {tok.vocab_size} — skipping {dname}", flush=True)
            del dmodel; gc.collect(); mx.clear_cache(); continue
        for nd in DRAFT_TOKENS:
            try:
                run(model, tok, draft=dmodel, ndraft=nd)         # warm
                out, n, wall, rate = run(model, tok, draft=dmodel, ndraft=nd)
                tps = n / wall
                ident = (out == base_out)
                print(f"=== {dname} ndraft={nd} ===\n"
                      f"  {tps:.1f} tok/s ({wall/len(SRC):.2f}s/line)  "
                      f"speedup x{tps/base_tps:.2f}  accept={rate*100:.0f}%  "
                      f"lossless={ident}", flush=True)
                ok = ok and ident
                results.append((f"{dname} nd={nd}", rate, tps, wall, rate, out))
            except Exception as e:
                print(f"=== {dname} ndraft={nd} === FAILED: {e}", flush=True)
        del dmodel; gc.collect(); mx.clear_cache()

    print("\n================ SUMMARY ================")
    print(f"{'config':<20}{'tok/s':>8}{'speedup':>9}{'accept':>8}")
    for name, _, tps, wall, rate, _ in results:
        sp = tps / base_tps
        print(f"{name:<20}{tps:>8.1f}{sp:>8.2f}x{rate*100:>7.0f}%")

    print("\n--- sample output drift check (baseline vs best draft) ---")
    print("SRC[0]:", SRC[0])
    print(" base :", base_out[0])
    if len(results) > 1:
        print(" draft:", results[-1][5][0], "  (identical)" if results[-1][5][0] == base_out[0] else "  (DIFFERS!)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

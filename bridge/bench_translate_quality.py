"""Eyeball A/B: how the register presets + glossary change the 26B translation.

Reuses server.py's real prompt builders (translate_once / _tx_system / _fewshot) so this bench
tests exactly what the bridge does — no duplicated prompt logic. Loads ONLY the 26B (no ASR/VAD).

Run with the bridge STOPPED (single 26B resident; see feedback_mlx_port_safety). Synthetic eyeball,
not a metric — real validation is the browser overlay on a live stream.
"""
import server

# EN->KO and JA->KO clause sets; the same line read in every register to see the tone shift.
SRC = {
    "EN": [
        "Okay so the patch just dropped and they completely reworked ranked matchmaking.",
        "Today we're announcing our next-generation accelerator, which delivers five petaflops.",
        "Officials say the measure will take effect next month, despite mounting criticism.",
        "Honestly I didn't even think it would work, and then it just... did.",
    ],
    "JA": [
        "じゃあ次のステージ行ってみましょうか、たぶんここが一番難しいところです。",
        "本日は新しいアーキテクチャと、その性能評価の結果についてご紹介します。",
        "政府は来週、追加の経済対策を発表する見通しだと関係者は話しています。",
    ],
}
GLOSSARY = [("ranked", "랭크"), ("petaflops", "페타플롭스"), ("Blackwell", "블랙웰")]


def main():
    print("[bench] loading 26B only (no ASR/VAD)…", flush=True)
    server.load_models(asr=False, vad=False)
    server.translate_once("hello world")              # warm the graph

    for lang, lines in SRC.items():
        for text in lines:
            print("\n" + "=" * 88)
            print(f"[{lang}] {text}")
            for reg in server._REGISTERS:
                ko = server.translate_once(text, target="Korean", register=reg)
                print(f"  {reg:<8} {ko}")
        print()

    # glossary effect (casual register), with and without the pinned terms
    print("\n" + "#" * 88 + "\n# GLOSSARY EFFECT (casual)\n" + "#" * 88)
    g_line = "Their ranked build runs on Blackwell and hits almost a petaflops per card."
    print(f"[EN] {g_line}")
    print(f"  no-glossary  {server.translate_once(g_line, register='casual')}")
    print(f"  glossary     {server.translate_once(g_line, register='casual', glossary_pairs=GLOSSARY)}")


if __name__ == "__main__":
    main()

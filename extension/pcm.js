// Shared linear resampler -> 16k PCM16, with fractional-phase carry (no per-block drift)
// and internal batching to >= minOut samples (100ms) before returning. Used by offscreen.js
// and delay.js so the two capture paths feed the bridge identically.
function lccMakeResampler(inRate, outRate, minOut) {
  outRate = outRate || 16000;
  minOut = minOut || 1600;
  const ratio = inRate / outRate;
  let tail = new Float32Array(0);   // input samples not yet consumed
  let phase = 0;                    // fractional read position within `tail`
  let out = [];                     // batched int16 output
  return (chunk) => {
    const buf = new Float32Array(tail.length + chunk.length);
    buf.set(tail); buf.set(chunk, tail.length);
    let pos = phase;
    while (pos + 1 < buf.length) {
      const i0 = Math.floor(pos), f = pos - i0;
      const s = buf[i0] * (1 - f) + buf[i0 + 1] * f;
      out.push(Math.max(-32768, Math.min(32767, Math.round(s * 32768))));
      pos += ratio;
    }
    const consumed = Math.floor(pos);
    tail = buf.slice(consumed);
    phase = pos - consumed;
    if (out.length < minOut) return null;
    const r = Int16Array.from(out);
    out = [];
    return r;
  };
}

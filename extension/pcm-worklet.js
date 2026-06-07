// Forwards each render block of the captured tab audio (Float32 @ context rate) to offscreen.js.
class PCMWorklet extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;          // keep the node alive
  }
}
registerProcessor("pcm-worklet", PCMWorklet);

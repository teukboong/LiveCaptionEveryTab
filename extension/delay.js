// B-2 (experimental): delayed A/V re-render from the page's own <video> element.
// No tabCapture => no tab self-feedback. Audio via createMediaElementSource, video via canvas ring buffer.
(() => {
  let session = null;
  const srcCache = new WeakMap();   // <video> -> {ctx, src}: createMediaElementSource is one-shot per element
  const MAX_DELAY_SEC = 12;
  const MAX_CAPTURE_FPS = 60;
  const SUB_LINGER_MS = 1500;   // the last cue lingers this long past its end when no next cue (readability)
  const SUB_FAIL_OPEN_MS = 6500; // if timing/cue routing drifts, still surface a fresh caption instead of silence
  const RUNTIME_WARN_INTERVAL_MS = 2000;
  let lastRuntimeWarnAt = 0;

  function errorText(e) {
    return String(e && e.message || e || "unknown error");
  }

  function warnRuntimeDelivery(label, e) {
    const now = Date.now();
    if (now - lastRuntimeWarnAt < RUNTIME_WARN_INTERVAL_MS) return;
    lastRuntimeWarnAt = now;
    console.warn("[lcc-vd] runtime delivery failed:", label, errorText(e));
  }

  function sendRuntimeBestEffort(msg, label) {
    try {
      const p = chrome.runtime.sendMessage(msg);
      if (p && typeof p.then === "function") {
        p
          .then((res) => { if (res && res.ok === false) warnRuntimeDelivery(label, res.error || res.msg || "not ok"); })
          .catch((e) => warnRuntimeDelivery(label, e));
      }
    } catch (e) {
      warnRuntimeDelivery(label, e);
    }
  }

  function getSource(video) {
    let e = srcCache.get(video);
    if (!e) {
      const ctx = new AudioContext();
      e = { ctx, src: ctx.createMediaElementSource(video), video };
      srcCache.set(video, e);
    }
    return e;
  }
  function findVideo() {
    const vids = [...document.querySelectorAll("video")].filter((v) => v.videoWidth > 0 && !v.paused);
    vids.sort((a, b) => b.videoWidth * b.videoHeight - a.videoWidth * a.videoHeight);
    return vids[0] || document.querySelector("video");
  }
  function report(t) {
    console.log("[lcc-vd]", t);
    if (window.__lccOverlay) window.__lccOverlay.setLines("", "🎬 " + t);
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "vdelay-start") startVD(msg).catch((e) => report("시작 실패: " + (e && e.message || e)));
    else if (msg.type === "vdelay-stop") stopVD();
  });

  async function startVD(opt) {
    stopVD();
    const delaySec = Math.min(MAX_DELAY_SEC, Math.max(0.5, Number(opt.delaySec) || 3.5));
    const video = findVideo();
    if (!video) { console.log("[lcc-vd] no <video> in this frame — skipping (all_frames)"); return; }

    let ctx, src;
    try { ({ ctx, src } = getSource(video)); }
    catch (e) { return report("오디오 가로채기 실패(DRM/중복?): " + e.message); }
    if (ctx.state === "suspended") {
      try { await ctx.resume(); }
      catch (e) { return report("오디오 컨텍스트 재개 실패: " + errorText(e)); }
    }
    src.disconnect();   // clear any prior routing before we re-wire

    const s = { delaySec, video, ctx, src, raf: 0, buf: [], canvas: null, node: null,
                delayNode: null, zeroNode: null, capturingFrame: false,
                nodeKind: "", captureFps: MAX_CAPTURE_FPS, anchored: false, anchorPerf: 0,
                cues: [], live: null, shownT: 0, lastSub: "", koState: { unit: null, prev: "", last: null, stableW: 0 },
                capInterval: 0, lastCap: 0, maxFrames: Math.ceil((delaySec + 1.2) * MAX_CAPTURE_FPS) };
    session = s;
    // Captions ride this delayed canvas as a subtitle track. content.js routes captions here in video
    // mode (instead of its pacer); cue windows are in bridge audio_ms and renderSub() locks them to the
    // frame actually on screen. final = committed sentence, live = in-progress source/preview.
    window.__lccVideoSub = {
      final: (m) => {
        if (session !== s) return;
        const unit = m.unit_id == null ? null : String(m.unit_id);
        const cue = {
          unit,
          start: +m.start_ms || 0,
          end: +m.end_ms || 0,
          src: m.source || "",
          ko: m.ko || "",
          degraded: !!m.degraded,
          receivedAt: performance.now(),
        };
        // De-dup by unit (a re-emitted final updates in place) and keep cues sorted by start, so
        // renderSub()'s linear "last cue with start<=now" scan stays correct when a late/out-of-order
        // final lands (reconnect, re-finalize). Scanning from the end keeps the in-order case O(1).
        if (unit != null) { const i = s.cues.findIndex((c) => c.unit === unit); if (i >= 0) s.cues.splice(i, 1); }
        let at = s.cues.length;
        while (at > 0 && s.cues[at - 1].start > cue.start) at--;
        s.cues.splice(at, 0, cue);
        if (s.cues.length > 400) s.cues.shift();
        if (s.live && s.live.unit === unit) s.live = null;
      },
      live: (m) => {
        if (session !== s) return;
        const nu = m.unit_id == null ? null : Number(m.unit_id);
        const cu = (s.live && s.live.unit != null) ? Number(s.live.unit) : null;
        if (cu != null && nu != null && nu < cu) return;   // stale live/final_stream must not cover newer source
        s.live = {
          unit: m.unit_id == null ? null : String(m.unit_id),
          start: +(m.start_ms || 0), src: m.source || m.text || "", ko: m.ko || "",
          phase: m.phase || m.kind || "",
          receivedAt: performance.now(),
        };
      },
      reset: () => {
        if (session !== s) return;
        s.cues.length = 0;
        s.live = null;
        s.koState.unit = null;
        s.koState.prev = "";
        s.koState.last = null;
        s.koState.stableW = 0;
        s.lastSub = "";
      },
      reanchor: (perf) => { if (session !== s) return; s.anchorPerf = Number(perf) || s.anchorPerf; s.cues.length = 0; s.live = null; s.koState.unit = null; s.koState.prev = ""; s.koState.last = null; s.koState.stableW = 0; },   // reconnect: bridge audio_ms reset -> old cues invalid
    };

    try {
      report("시작… " + video.videoWidth + "×" + video.videoHeight + " / 지연 " + delaySec + "s");
      if (window.__lccOverlay && window.__lccOverlay.setPlaybackDelay) {
        window.__lccOverlay.setPlaybackDelay("video", delaySec * 1000);
      }

      // audio: delayed audible
      const delay = ctx.createDelay(delaySec + 1); delay.delayTime.value = delaySec;
      s.delayNode = delay;
      src.connect(delay).connect(ctx.destination);

      // audio: PCM tap (undelayed) -> offscreen relay. delay.js can't open ws://127.0.0.1 itself —
      // the page's CSP (connect-src) and the bridge's chrome-extension-only origin allowlist both
      // block a page-context socket. So we tap undelayed PCM here and hand it to the offscreen doc
      // (via the service worker), which streams it to the bridge and relays captions back through
      // background -> content.js. The bridge WS, stream clock, auto-reconnect and on-demand
      // summary/Q&A are all shared with audio mode now; this file only does the delayed A/V
      // re-render + the PCM tap.
      const resample = lccMakeResampler(ctx.sampleRate, 16000);
      await attachPcmTap(s, resample);

      // video: capture per real frame (rVFC), show frame from (now - delaySec)
      const canvas = document.createElement("canvas");
      canvas.id = "lcc-delayed-canvas";
      Object.assign(canvas.style, { position: "fixed", zIndex: "2147483640", pointerEvents: "none", background: "#000" });
      document.documentElement.appendChild(canvas);
      s.canvas = canvas;
      const c2d = canvas.getContext("2d", { alpha: false });

      function frameStampMs(callbackNow, metadata) {
        const expected = metadata && Number(metadata.expectedDisplayTime);
        if (Number.isFinite(expected) && expected > 0) return expected;
        const presentation = metadata && Number(metadata.presentationTime);
        if (Number.isFinite(presentation) && presentation > 0) return presentation;
        const cbNow = Number(callbackNow);
        return Number.isFinite(cbNow) && cbNow > 0 ? cbNow : performance.now();
      }

      const scheduleNextFrameCapture = () => {
        if (session === s && s.video && s.video.requestVideoFrameCallback) {
          s.video.requestVideoFrameCallback(capture);
        }
      };
      const capture = async (callbackNow, metadata) => {
        if (session !== s) return;
        const v = s.video, w = v.videoWidth, h = v.videoHeight;
        const frameT = frameStampMs(callbackNow, metadata);
        if (frameT - s.lastCap < 1000 / s.captureFps) {
          scheduleNextFrameCapture();
          return;
        }
        if (s.capturingFrame) {
          scheduleNextFrameCapture();
          return;
        }
        s.lastCap = frameT;
        s.capturingFrame = true;
        try {
          if (w > 0) {
            let bmp = null;
            try {
              bmp = await createImageBitmap(v);   // native video frame resolution
              if (session !== s) { bmp.close && bmp.close(); return; }
              s.buf.push({ t: frameT / 1000, bmp });
              while (s.buf.length > s.maxFrames) { const o = s.buf.shift(); o.bmp.close && o.bmp.close(); }
            } catch (_) {
              if (bmp) try { bmp.close && bmp.close(); } catch (_) {}
            }
          }
        } finally {
          s.capturingFrame = false;
          scheduleNextFrameCapture();
        }
      };
      if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(capture);
      else s.capInterval = setInterval(capture, Math.round(1000 / MAX_CAPTURE_FPS));

      const display = () => {
        if (session !== s) return;
        const r = s.video.getBoundingClientRect();
        if (r.width > 0) {
          canvas.style.left = r.left + "px"; canvas.style.top = r.top + "px";
          canvas.style.width = Math.round(r.width) + "px";
          canvas.style.height = Math.round(r.height) + "px";
        }
        const showT = performance.now() / 1000 - s.delaySec;
        let idx = -1;
        for (let i = 0; i < s.buf.length && s.buf[i].t <= showT; i++) idx = i;
        if (idx >= 0) {
          const frame = s.buf[idx];
          if (canvas.width !== frame.bmp.width) canvas.width = frame.bmp.width;
          if (canvas.height !== frame.bmp.height) canvas.height = frame.bmp.height;
          try { c2d.drawImage(frame.bmp, 0, 0); } catch (_) {}
          s.shownT = frame.t;                       // perf-sec of the frame actually on screen
          for (let i = 0; i < idx; i++) { const o = s.buf.shift(); o.bmp.close && o.bmp.close(); }
        }
        renderSub(s);                               // captions locked to s.shownT (= the frame on screen)
        s.raf = requestAnimationFrame(display);
      };
      s.raf = requestAnimationFrame(display);
      report("동작 중 (지연 " + delaySec + "s · 최대 " + s.captureFps + "fps/native)");
    } catch (e) {
      report("설정 실패: " + (e && e.message || e));
      stopVD();   // restores live audio (see below)
    }
  }

  function sendPcm(s, pcm) {
    if (!pcm || session !== s) return;
    if (!s.anchored) {
      s.anchored = true;
      // Subtitle anchor: perf(ms) of the first PCM tap == bridge audio_ms 0. The cue track rides the
      // SAME performance.now() clock as the delayed canvas, so renderSub() locks captions to the frame
      // actually on screen — zero relay-hop jitter, and they stay locked even if the canvas stalls.
      s.anchorPerf = performance.now();
    }
    // Hand undelayed PCM to the offscreen relay. chrome messaging doesn't preserve ArrayBuffers,
    // so send a plain number array; offscreen rebuilds the Int16Array.
    sendRuntimeBestEffort({ target: "background", type: "vd-pcm", pcm: Array.from(pcm) }, "vd-pcm");
  }

  // Pick the cue whose window brackets the frame currently on screen and render it via the shared
  // overlay — no pacer/queue/merge. The cue track and the canvas share one clock (s.anchorPerf), so a
  // caption shows exactly while the delayed video plays its [start,end] span. Each cue lingers until the
  // next one starts so short cues stay readable; the in-progress source/preview fills the tail.
  function renderSub(s) {
    if (!window.__lccOverlay || !window.__lccOverlay.setLines) return;
    if (!s.anchorPerf || !s.shownT) return;             // not streaming yet -> keep the "● 자막 대기 중…" message
    let src = "", ko = "", isDraft = false;
    const contentMs = s.shownT * 1000 - s.anchorPerf;   // bridge audio_ms currently on screen
    let cue = null;
    {
      let next = null;
      for (let i = 0; i < s.cues.length; i++) {
        if (s.cues[i].start <= contentMs) { cue = s.cues[i]; next = s.cues[i + 1] || null; }
        else break;
      }
      const lingerEnd = cue ? (next ? next.start : cue.end + SUB_LINGER_MS) : 0;
      if (cue && contentMs <= lingerEnd) {
        src = cue.src; ko = (cue.degraded && cue.ko) ? cue.ko + " …" : cue.ko; isDraft = false;
      }
      else if (s.live && contentMs >= s.live.start) { src = s.live.src; ko = s.live.ko; isDraft = true; }
    }
    if (!src && !ko) {
      const now = performance.now();
      const freshCue = s.cues.length ? s.cues[s.cues.length - 1] : null;
      if (s.live && s.live.receivedAt && now - s.live.receivedAt <= SUB_FAIL_OPEN_MS) {
        src = s.live.src; ko = s.live.ko; isDraft = true;
      } else if (freshCue && freshCue.receivedAt && now - freshCue.receivedAt <= SUB_FAIL_OPEN_MS) {
        src = freshCue.src; ko = (freshCue.degraded && freshCue.ko) ? freshCue.ko + " …" : freshCue.ko; isDraft = false;
      }
    }
    const ov = window.__lccOverlay;
    // Sync debug (video mode): the cue-clock view — what content-ms is on screen, which cue window
    // matched, and the queue depth. Built only when the popup toggle is on (ov.debugEnabled).
    let dbg = "";
    if (ov.debugEnabled && ov.debugEnabled()) {
      dbg = [
        isDraft ? "vd-live" : "vd-cue",
        "t=" + Math.round(contentMs),
        cue ? "c=" + Math.round(cue.start) + ".." + Math.round(cue.end) : "",
        s.live ? "L=" + Math.round(s.live.start) + (s.live.unit != null ? "/u" + s.live.unit : "") : "",
        "q=" + s.cues.length,
        "delay=" + Math.round(s.delaySec * 1000) + "ms",
      ].filter(Boolean).join(" ");
    }
    if (isDraft && ov.setLinesSplit && ov.koSplitInto) {     // live: LocalAgreement stable(solid)/draft(dim)
      const sp = ov.koSplitInto(s.koState, (s.live && s.live.unit) || null, ko);
      const key = src + "|" + sp.stable + "\u22a5" + sp.draft + "|S|" + dbg;
      if (key !== s.lastSub) { s.lastSub = key; ov.setLinesSplit(src, sp.stable, sp.draft, dbg); }
    } else {
      const key = src + "|" + ko + "|" + (isDraft?"D":"C") + "|" + dbg;
      if (key !== s.lastSub) { s.lastSub = key; ov.setLines(src, ko, dbg, isDraft); }
    }
  }

  async function attachPcmTap(s, resample) {
    const zero = s.ctx.createGain();
    zero.gain.value = 0;
    s.zeroNode = zero;
    try {
      if (!s.ctx.__lccPcmWorkletPromise) {
        s.ctx.__lccPcmWorkletPromise = s.ctx.audioWorklet.addModule(chrome.runtime.getURL("pcm-worklet.js"))
          .catch((e) => { s.ctx.__lccPcmWorkletPromise = null; throw e; });
      }
      await s.ctx.__lccPcmWorkletPromise;
      const node = new AudioWorkletNode(s.ctx, "pcm-worklet");
      s.node = node;
      s.nodeKind = "worklet";
      node.port.onmessage = (ev) => sendPcm(s, resample(ev.data));
      s.src.connect(node);
      node.connect(zero).connect(s.ctx.destination);   // keep processor running, inaudible
      return;
    } catch (e) {
      console.warn("[lcc-vd] AudioWorklet unavailable; falling back to ScriptProcessor", e);
    }
    const node = s.ctx.createScriptProcessor(4096, 1, 1);
    s.node = node;
    s.nodeKind = "script";
    node.onaudioprocess = (ev) => sendPcm(s, resample(ev.inputBuffer.getChannelData(0)));
    s.src.connect(node);
    node.connect(zero).connect(s.ctx.destination);
  }

  function stopVD() {
    const s = session; session = null;
    if (!s) return;
    try { if (window.__lccVideoSub) window.__lccVideoSub = null; } catch (_) {}   // stop content.js routing captions here
    if (s.raf) cancelAnimationFrame(s.raf);
    if (s.capInterval) clearInterval(s.capInterval);
    try { s.buf.forEach((o) => o.bmp.close && o.bmp.close()); } catch (_) {}
    try {
      if (s.node) {
        if (s.node.port) s.node.port.onmessage = null;
        s.node.onaudioprocess = null;
        s.node.disconnect();
      }
    } catch (_) {}
    try { s.delayNode && s.delayNode.disconnect(); } catch (_) {}
    try { s.zeroNode && s.zeroNode.disconnect(); } catch (_) {}
    // The bridge WS lives in offscreen now; background.cleanup() closes that doc, which finalizes
    // the trailing sentence bridge-side. Here we only tear down the page-side A/V tap + render.
    try { s.canvas && s.canvas.remove(); } catch (_) {}
    // restore the element's audio to live (undelayed) WITHOUT closing the ctx (so the cached
    // MediaElementSource stays valid for reuse and the video is never left silent).
    try { s.src.disconnect(); s.src.connect(s.ctx.destination); } catch (_) {}
    s.buf.length = 0; s.cues.length = 0; s.live = null; s.video = null;
  }

  window.addEventListener("pagehide", () => { stopVD(); }, { once: true });
})();

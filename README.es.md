# Live Caption Every Tab — Subtítulos en tiempo real de cualquier sitio (extranjero→tu idioma)

[한국어](README.md) · [English](README.en.md) · [日本語](README.ja.md) · **Español** · [中文](README.zh.md)

> 🤖 Este proyecto se construyó **íntegramente mediante vibe coding (programación en pareja con IA)** — desde el código hasta la documentación.

En YouTube, Twitch, **X** o cualquier sitio, captura el audio de la pestaña del navegador y usa un **Gemma-4 local** para transcribir + traducir, mostrando subtítulos de 2 líneas (original / tu idioma) sobre el vídeo. (La captura de pestaña es independiente del dominio, así que funciona en cualquier pestaña con sonido.)
Para la transcripción eliges en el popup entre **Granite Speech 4.1** (fuerte en inglés) y **Qwen3-ASR** (multilingüe, incl. japonés/coreano). Ambos generan puntuación y mayúsculas de forma nativa, y filtran el silencio con `[no speech]`.

## Por qué existe (ya hay herramientas parecidas)

Las herramientas de subtitulado/traducción en tiempo real se dividen en dos grupos, y la combinación **"en el navegador / cualquier pestaña en vivo / traducción por significado con LLM totalmente local"** estaba vacía — esto llena ese hueco.

| | Este proyecto | Extensiones basadas en Whisper | Reproductores de escritorio (p. ej. LLPlayer) |
|---|---|---|---|
| **Entrada** | **Cualquier pestaña** con sonido (incl. directos) | Audio de pestaña | Vídeo descargado / archivos·URLs metidos en un reproductor |
| **ASR** | Granite / Qwen3 (puntuación·truecasing nativos; silencio·música filtrados con `[no speech]`) | Sobre todo Whisper | Sobre todo Whisper |
| **Traducción** | **LLM local (Gemma-4)** por significado — mantiene contexto·pronombres | Ninguna / MT literal / nube | LLM local posible (Ollama, etc.) |
| **Ejecución** | 100% local (cero nube) | Local~mixta | Local |
| **Idioma destino** | Coreano primero (+multilingüe) | Varía | Multilingüe (el ajuste por idioma varía) |

- **Las extensiones basadas en Whisper** capturan bien la pestaña, pero Whisper tiende a alucinar subtítulos sobre silencio/música, y la traducción suele estar ausente, ser literal o en la nube. → Aquí se resuelve distinto: ASR con puntuación nativa + filtrado de silencio + traducción por significado con Gemma local.
- **Los reproductores de escritorio** tienen muy buena traducción con LLM local, pero hay que descargar el vídeo o meterlo en el reproductor, lo que no encaja con directos / sitios arbitrarios. → Aquí, sin descargas — se superpone **directamente sobre cualquier pestaña que emita sonido**.

Todo es **local y gratuito**. A cambio hay un mínimo de hardware (ver requisitos en [SETUP.md](SETUP.md)). En máquinas más modestas el modelo de traducción ajusta su nivel automáticamente a la memoria (full/mid/lite).

## Plataforma — dos runtimes (con soporte equivalente)

El mismo bridge·la misma extensión corren en ambos backends. Elige el de tu equipo con `LCC_BACKEND`.

| Backend | Entorno | Transcripción (ASR) | Traducción | Guía |
|---|---|---|---|---|
| **MLX** (`LCC_BACKEND=mlx`) | Apple Silicon | Granite/Qwen3 (mlx-audio, en proceso) | Gemma-4 · full/mid/lite (mlx-lm) | [SETUP.md](SETUP.md) |
| **CUDA** (`LCC_BACKEND=cuda`) | Windows + NVIDIA (WSL2) | Granite/Qwen3 (transformers, `cuda/asr_server.py`) | llama.cpp · GGUF · full/mid/lite (HTTP compatible con OpenAI) | [SETUP-windows.md](SETUP-windows.md) |

La elección del motor de transcripción (inglés=granite / multilingüe=qwen3) es idéntica en ambos (ruteada por el campo `model`) — sin whisper. VAD·ensamblado de frases·planificador·number-guard·constructor de prompts son **compartidos por ambos backends** (funciones puras); solo cambian las 3 funciones de GPU (transcribir/traducir/resumir) por runtime, y esa frontera es `bridge/backend_cuda.py` (HTTP) y el "Backend seam" en server.py. (El valor por defecto del código es `mlx`.)

## Arquitectura
```
[Extensión Chrome] tabCapture (audio de pestaña) ──WS(PCM16 16k)──▶ [bridge/server.py]
                                                        VAD + soft-cut ASR atom
                                                        → transcripción Granite / Qwen3-ASR (puntuación·multilingüe)
                                                        → unit assembler
                                                        → traducción Gemma-4 (tier)
   [overlay de 2 líneas content.js] ◀──WS(JSON caption)──┘
```
- El ASR elige entre **dos motores mlx-audio** en el popup (▸ Motor de transcripción). **Granite Speech 4.1 2B** (`ibm-granite/granite-speech-4.1-2b` · fiel en inglés, WER ~0%) y **Qwen3-ASR 1.7B** (`Qwen/Qwen3-ASR-1.7B` · 52 idiomas incl. japonés/coreano, ID de idioma automático). Ambos generan puntuación·truecasing de forma nativa, así que el troceado por frases funciona tal cual. Comparte la GPU de Apple con el traductor (serializado). ⚠ granite necesita el **arreglo de conv en mlx-audio main** (ver SETUP).
- Un Parakeet de baja latencia solo para inglés es una vía para usuarios avanzados, solo con `LCC_ASR_ENGINE=parakeet` (CPU, en paralelo a la traducción; modelo `~/.local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8`, `sherpa-onnx==1.13.2`). El selector del popup solo muestra granite/qwen3.
- Traducción: `Gemma-4 (full=26B-A4B / mid=E4B / lite=E2B)` (mlx-lm) — **prompt de calidad** por defecto (expert interpreter·by-meaning·no-translationese + 3 few-shots, coste amortizado por KV-cache → salida hablada natural en vez de un estilo escrito rígido). Baja latencia con `LCC_TX_PROFILE=fast`. **Idioma destino seleccionable** (KO/EN/JA/ZH/ES/FR/DE), origen autodetectado, se omite cuando destino=origen.
- RAM ~26GB (pesos) + un poco de KV por chunk. Latencia ~2.9–3.4s por chunk de habla (ASR ~0.7s + traducción ~1.4s + prefill de audio + espera de límite de cláusula).
- MTP no aporta nada en este hardware, así que no se usa (verificado en MoE·dense·E4B).
- ⚠️ Requiere Chrome/Edge/Brave genuinos — algunos forks de Chromium (p. ej. ChatGPT Atlas) no implementan `chrome.tabCapture`.

## Ejecución
### 1) Servidor bridge
```bash
# desde la raíz del repo (la primera vez, ejecuta ./setup.sh para instalar venv·dependencias)
bash bridge/run_bridge.sh
# listo cuando aparece "[bridge] ready  ws://127.0.0.1:8765" (primera carga ~40s)
```
- Para tenerlo siempre activo (opt-in, reinicio automático al fallar): `bash bridge/autostart.sh install` — ⚠ ~26GB de RAM residentes. Apagar: `… uninstall`
- Para arrancar/parar desde el **botón del popup** sin terminal (`🚀 Iniciar bridge`): ejecuta `bash extension/native-host/install-host.sh` una vez, luego recarga la extensión (host de mensajería nativa — SETUP 6.5). Corre desacoplado, así que sobrevive a cerrar el navegador.
- Si el bridge se reinicia/cae, la extensión **se reconecta automáticamente** (backoff) y almacena hasta 6s de audio reciente. El habla durante cortes más largos puede perderse.
### 2) Cargar la extensión (Chrome)
1. `chrome://extensions` → activa el **Modo de desarrollador** (arriba a la derecha)
2. **Cargar descomprimida** → selecciona la carpeta `extension/` de este repo
3. En una pestaña de vídeo de YouTube/Twitch, **haz clic en el icono de la extensión** → pulsa **`▶ Iniciar subtítulos`** en el popup → insignia `ON`, aparece el overlay
4. Ajustes del popup: **tamaño·posición vertical/horizontal·línea original·corrección de sincronía** de los subtítulos (en vivo), **espera de frase·detección de voz** (se aplican al reiniciar)
5. Detén con **`■ Detener subtítulos`**. (tabCapture requiere un gesto de clic del usuario → sin auto-inicio)

## Funciones
- **Priming automático de términos**: inyecta el título de la página/vídeo como pistas de ASR·traducción (desactivable en el popup).
- **Presets por tipo de contenido**: elige un tipo (general·charla / conferencia·ponencia / noticias·entrevista / streaming personal) una vez y agrupa el registro (tono) + el modo de latencia — ponencia=formal·estable, noticias=equilibrado, streaming=coloquial·inmediato. El tono·las terminaciones de frase·los anclajes few-shot se adaptan al contenido, y el idioma de origen (EN/JA) se autodetecta para elegir ejemplos acordes.
- **Glosario**: introduce `nombre=traducción` (uno por línea) en el popup para sesgar la transcripción + renderizar siempre ese término igual en la traducción (elimina que un nombre se traduzca distinto en cada línea). `Pistas de términos` es sesgo de texto libre.
- **Modo precisión (re-transcripción en 2 pasadas)**: al activarlo, las frases multicláusula que se confirman por un final natural (pause/eos) o por puntuación terminal se re-transcriben enteras una vez justo antes de confirmar → elimina errores de límite al unir fragmentos de VAD. La confirmación es ~0.7s más lenta, por eso es un interruptor (por defecto OFF). Las unidades cuya alineación se rompió por solape/división se excluyen automáticamente (guarda `unit_pure`).
- **Subtítulos en streaming**: la línea original aparece primero por cada ASR atom; la vista previa traducida se debounce/coalesce. Los subtítulos confirmados tienen prioridad en la cola final.
- **3 modos de latencia**: `aggressive` solapa al máximo la transcripción CPU de Parakeet con la traducción MLX y pre-traduce la vista previa de la unidad actual en modo latest-only; `balanced` muestra vista previa solo cuando MLX está libre; `stable` muestra solo traducciones confirmadas. La traducción final siempre tiene prioridad sobre la vista previa.
- **Retardo de vídeo Lookahead**: en modo de retardo de vídeo el audio real se transcribe·traduce de inmediato, y los subtítulos se programan al reloj real de inicio del stream PCM y a la ventana de habla (`start_ms`/`end_ms`). La corrección de sincronía del popup permite ajuste fino de ±2s.
- **Depuración de sincronía**: al activarla en el popup, muestra `kind/unit/start/end/due/now/lag/delay/offset/q` bajo el subtítulo y en la consola para verificar que la salida no llega antes del due time.
- **Caché/prioridad de traducción**: si la vista previa y la final comparten origen, se evita re-traducir, y la final se procesa antes que la vista previa.
- **Registro de subtítulos**: 📜 (abajo a la derecha) → panel de scrollback / exportación bilingüe `.md`.
- **Resumen·Preguntas**: el ✨Resumen · cuadro de preguntas del panel — el Gemma local resume/responde sobre los subtítulos pasados (en streaming).

## Solución de problemas
- "Bridge desconectado" en el overlay → comprueba que `run_bridge.sh` está corriendo y el puerto 8765.
- No aparecen subtítulos → comprueba que el vídeo tiene habla real (lo no hablado se omite como `[no speech]`) y que la pestaña emite sonido.
- No hay sonido → la captura de pestaña intercepta la reproducción; offscreen mantiene la conexión de reproducción `source→destination`, así que suele estar bien.
- Error de puerto ocupado → `lsof -ti:8765 | xargs kill -9`.

## Palancas de ajuste
- Reducir latencia: la traducción usa el prompt de calidad por defecto (coste amortizado por KV-cache). Para reducir más, usa `LCC_TX_PROFILE=fast` para un prompt compacto y baja `SEG_SILENCE_MS`/`SOFT_MAX_SEC`. Si ves truncamiento en el modo precisión largo, sube solo `LCC_ASR_MAX_TOKENS=96`.
- Paralelismo percibido: para emisiones en inglés, pon por defecto `Parakeet + aggressive` en el popup. El modo aggressive usa silencio de frase efectivo ≤900ms, pending commit 120 caracteres/1.8s, preview debounce 180ms, 2 contextos recientes finales, 0 contextos de preview, para usar corto el carril de traducción MLX. El soft-cut de Parakeet se mantiene en 4.0s para evitar duplicados por mala detección. Si los subtítulos cambian demasiado, baja a `balanced`; si la estabilidad de traducción es lo primero, `stable`. El valor por defecto del servidor es `LCC_LATENCY_MODE=aggressive`, y acepta `stable|balanced|aggressive`.
- Sincronía de salida: el bridge transcribe el habla larga con soft-cut de 4.5s + overlap de 220ms, y la pantalla programa con un stream clock basado en `performance.now()`. Los subtítulos cortos solo se fusionan cuando el backlog final realmente se atrasa.
- Retardo de vídeo: `delaySec` hasta 12s. El modo `videoDelay` captura a la resolución original del frame de vídeo, limitando a 60fps. Los timestamps de frame prefieren los metadatos de `requestVideoFrameCallback`, y el PCM tap prefiere AudioWorklet.
- Mejorar la calidad de traducción: ajusta el preset de **tono** del popup al contenido y fija nombres propios en el **glosario**. Si necesitas una transcripción más limpia, activa el **modo precisión** (2 pasadas). Como último recurso, cambia el modelo de traducción a 31B dense (5× más lento). Benches: `bench_translate_quality.py` (A/B de tono/glosario), `bench_2pass.py` (2 pasadas vs 1) — ambos se ejecutan con el bridge detenido.
- Sensibilidad a alucinaciones/ruido: ajusta la agresividad de `webrtcvad.Vad(0..3)`.
- Protección del WS local: por defecto solo se permiten el origin de la extensión Chrome + un client token. Para cambiar el token, mantén `LCC_WS_TOKEN` y `extension/protocol.js` sincronizados.

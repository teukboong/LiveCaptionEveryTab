# Live Caption Every Tab — 任意网站的实时外语→你的语言字幕

[한국어](README.md) · [English](README.en.md) · [日本語](README.ja.md) · [Español](README.es.md) · **中文**

> 🤖 本项目**从代码到文档全部通过 vibe coding（AI 结对编程）完成**。

在 YouTube、Twitch、**X** 或任何网站上，抓取浏览器标签页的音频，用**本地 Gemma-4** 转写+翻译，在视频上方显示两行字幕（原文/你的语言）。（标签页捕获与域名无关，只要标签页有声音就行。）
转写可在弹窗中选择 **Granite Speech 4.1**（英语强）或 **Qwen3-ASR**（多语种，含日语/韩语）。两者都原生输出标点与大小写，无人声时用 `[no speech]` 进行门控。

> 世上有数不清的视频与音频，但语言之墙至今仍是**内容之墙**。
> 这是抱着在那堵墙上凿开一个小洞的心情做的。

## 为什么要做（明明已有类似工具）

实时字幕/翻译工具大体分两派，而**“在浏览器里 / 任意正在播放的标签页 / 完全本地 LLM 按语义翻译”**这一组合是空缺的——本项目正是来填补这个空白。

| | 本项目 | 基于 Whisper 的浏览器扩展 | 桌面播放器（如 LLPlayer） |
|---|---|---|---|
| **输入** | 有声音的**任意标签页**（含直播） | 标签页音频 | 下载的视频 / 喂入播放器的文件·URL |
| **转写(ASR)** | Granite / Qwen3（原生标点·truecasing；静音·音乐用 `[no speech]` 门控） | 多为 Whisper | 多为 Whisper |
| **翻译** | **本地 LLM（Gemma-4）**按语义翻译——保留上下文·代词 | 无 / 直译 MT / 云端 | 可本地 LLM（Ollama 等） |
| **运行** | 100% 本地（零云端） | 本地~混合 | 本地 |
| **目标语言** | 韩语优先（+多语种） | 因工具而异 | 多语种（各语言优化程度不一） |

- **基于 Whisper 的扩展**能很好地抓取标签页，但 Whisper 在静音/音乐段容易产生幻觉字幕，翻译往往缺失、直译或走云端。→ 这里换了另一种解法：原生标点的 ASR + 静音门控 + 本地 Gemma 按语义翻译。
- **桌面播放器**的本地 LLM 翻译质量很好，但需要先下载视频或把它喂进播放器，不适合直播/任意网站。→ 这里无需下载——**只要标签页有声音就直接叠加**。

全部**本地·免费**。代价是有硬件门槛（要求见下方 [SETUP.md](SETUP.md)）。配置较弱时，翻译模型会按内存自动分级（full/mid/lite）。

## 平台 — 两种 runtime（同等支持）

同一套 bridge·同一个扩展在两种 backend 上都能运行。用 `LCC_BACKEND` 选择适合你机器的那一种。

| Backend | 环境 | 转写(ASR) | 翻译 | 指南 |
|---|---|---|---|---|
| **MLX** (`LCC_BACKEND=mlx`) | Apple Silicon | Granite/Qwen3（mlx-audio，进程内） | Gemma-4 · full/mid/lite（mlx-lm） | [SETUP.md](SETUP.md) |
| **CUDA** (`LCC_BACKEND=cuda`) | Windows + NVIDIA（WSL2） | Granite/Qwen3（transformers，`cuda/asr_server.py`） | llama.cpp · GGUF · full/mid/lite（OpenAI 兼容 HTTP） | [SETUP-windows.md](SETUP-windows.md) |

转写引擎的选择（英语=granite / 多语种=qwen3）两端完全一致（按 `model` 字段路由）——不用 whisper。VAD·句子组装·调度器·number-guard·prompt 构建器为**两种 backend 共享**（纯函数）；随 runtime 改变的只有 3 个 GPU 函数（转写/翻译/摘要），其边界就是 `bridge/backend_cuda.py`（HTTP）与 server.py 中的 “Backend seam”。（代码默认值为 `mlx`。）

## 架构
```
[Chrome 扩展] tabCapture（标签页音频） ──WS(PCM16 16k)──▶ [bridge/server.py]
                                                        VAD + soft-cut ASR atom
                                                        → Granite / Qwen3-ASR 转写（标点·多语种）
                                                        → unit assembler
                                                        → Gemma-4 (tier) 翻译
   [content.js 两行覆盖层] ◀──WS(JSON caption)──────────┘
```
- ASR 在弹窗中从**两个 mlx-audio 引擎**里选（▸ 转写引擎）。**Granite Speech 4.1 2B**（`ibm-granite/granite-speech-4.1-2b`·英语忠实，WER 接近 0%）与 **Qwen3-ASR 1.7B**（`Qwen/Qwen3-ASR-1.7B`·含日语/韩语共 52 种语言，自动语种识别）。两者都原生输出标点·truecasing，所以句子切分可直接进行。与翻译模型共享同一块 Apple GPU（串行）。⚠ granite 需要 mlx-audio **main 上的 conv 修复**（见 SETUP）。
- 仅英语的低延迟 Parakeet 是给高级用户的出口，仅通过 `LCC_ASR_ENGINE=parakeet` 启用（CPU，与翻译并行；模型 `~/.local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8`，`sherpa-onnx==1.13.2`）。弹窗选择器只暴露 granite/qwen3。
- 翻译：`Gemma-4 (full=26B-A4B / mid=E4B / lite=E2B)`（mlx-lm）——默认 **quality 提示词**（expert interpreter·by-meaning·no-translationese + 3 个 few-shot，靠 KV-cache 摊销开销 → 比书面语更自然的口语）。低延迟用 `LCC_TX_PROFILE=fast`。**目标语言可选**（45 种语言 — Gemma 多语种），源语言自动检测，目标=源时跳过。
- RAM ~26GB（full 层级权重；mid ~8 / lite ~6GB 更小）+ 每个 chunk 少量 KV。延迟 ~2.9–3.4s/语音 chunk（ASR ~0.7s + 翻译 ~1.4s + 音频 prefill + 等待小句边界）。
- MTP 在此硬件上无意义，故未使用（MoE·dense·E4B 均已验证）。
- ⚠️ 需正版 Chrome/Edge/Brave——部分 Chromium 分支（如 ChatGPT Atlas）未实现 `chrome.tabCapture`。

## 安装（最简单）

不想用终端的话，**双击安装**：
- **macOS** — 双击 `install-mac.command`（被拦截就右键 → 打开）。一次搞定 venv·依赖·弹窗宿主。
- **Windows** — 双击 `install-windows-oneclick.bat`（WSL2 + CUDA + 模型，全自动）。

之后**扩展弹窗就能全包**——启动 bridge，并且**只下载你选的层级**（Full/Mid/Lite）来省磁盘。（用终端的人：`./setup.sh [--models --tier lite]`）

## 运行
### 1) Bridge 服务器
```bash
# 在仓库根目录（首次先运行 ./setup.sh 安装 venv·依赖）
bash bridge/run_bridge.sh
# 出现 "[bridge] ready  ws://127.0.0.1:8765" 即就绪（首次加载 ~40s）
```
- 想常驻（opt-in，崩溃自动重启）：`bash bridge/autostart.sh install` — ⚠ 常驻约 26GB 内存（full 层级）。关闭：`… uninstall`
- 不用终端，弹窗按钮（**启动 bridge**·模型 **Full/Mid/Lite**）就能全包——需要原生消息宿主，而 **`./setup.sh` 已安装**它（浏览器沙箱唯一做不了的引导步骤）。之后重新加载扩展。以 detached 运行，关浏览器也不退出（SETUP 6.5）。
- bridge 重启/断开时，扩展会**自动重连**（退避），并缓冲最近最多 6 秒的音频。更长故障期间的语音可能丢失。
### 2) 加载扩展（Chrome）
1. `chrome://extensions` → 打开右上角的**开发者模式**
2. **加载已解压的扩展程序** → 选择本仓库的 `extension/` 文件夹
3. 在 YouTube/Twitch 视频标签页中**点击扩展图标** → 点击弹窗里的 **`开始字幕`** → 徽标 `ON`，出现覆盖层
4. 弹窗设置：字幕**大小·上下/左右位置·原文行·同步校正**（实时），**句子等待·语音检测**（重启后生效）
5. 再次用 **`停止字幕`** 停止。（tabCapture 需要用户点击手势 → 无法自动开始）

## 功能
- **自动术语预热**：把页面/视频标题作为 ASR·翻译提示自动注入（可在弹窗关闭）。
- **内容类型预设**：在弹窗里选一次内容类型（一般·闲聊 / 会议·讲座 / 新闻·访谈 / 个人直播），即把语体（register）与延迟模式打包匹配——讲座=正式·稳定，新闻=均衡，直播=口语·即时。语气·句末·few-shot 锚点随内容变化，并自动检测源语言（EN/JA）选取匹配示例。
- **术语表**：在弹窗里填 `名称=译法`（每行一个），即可对该术语进行转写偏置 + 在翻译中始终渲染一致（消除同一名称每行译法不同的抖动）。`术语提示` 为自由文本偏置。
- **精度模式（两遍重转写）**：开启后，由自然结束（pause/eos）或终止标点确定的多小句句子，会在确定前把累积音频整体再转写一遍 → 消除拼接 VAD 片段造成的边界错误。确定会慢约 0.7s，故为开关（默认 OFF）。因重叠/拆分导致对齐损坏的单元会被自动排除（`unit_pure` 守卫）。
- **流式字幕**：原文按 ASR atom 先显示，翻译预览经 debounce/coalesce。已确定字幕在 final 队列中优先处理。
- **三档延迟模式**：`aggressive` 尽量让 Parakeet 的 CPU 转写与 MLX 翻译重叠，并以 latest-only 预翻当前 unit 预览；`balanced` 仅在 MLX 空闲时预览；`stable` 只显示已确定的翻译。final 翻译始终优先于预览。
- **Lookahead 视频延迟**：在视频延迟模式下，实际音频立即转写·翻译，字幕则按真实 PCM 流起始 clock 与语音区间（`start_ms`/`end_ms`）排程输出。弹窗的同步校正可做 ±2 秒微调。
- **同步调试**：在弹窗开启后，会在字幕下方与控制台显示 `kind/unit/start/end/due/now/lag/delay/offset/q`，用于确认输出是否早于 due time。
- **翻译缓存/优先级**：若预览与 final 的源相同则避免重复翻译，且 final 翻译先于预览处理。
- **字幕记录**：弹窗的字幕回滚 + 双语 `.md` 导出（`.md` 按钮）。
- **摘要·提问**：面板的 摘要 · 提问框——本地 Gemma 对过往字幕进行摘要/问答（流式）。

## 排错
- 覆盖层显示“bridge 连接断开” → 检查 `run_bridge.sh` 是否在运行、端口 8765。
- 没有字幕 → 检查视频是否有真实人声（非人声会作为 `[no speech]` 跳过）、标签页是否有声音。
- 没有声音 → 标签页捕获拦截了播放；offscreen 会保持 `source→destination` 的播放连接，通常正常。
- 端口被占用错误 → 先用弹窗里的 `Bridge Stop`；如果仍有 listener，再运行 `lsof -ti tcp:8765 -sTCP:LISTEN | xargs kill`。

## 调优杠杆
- 降低延迟：翻译默认用 quality 提示词（靠 KV-cache 摊销开销）。想再降，用 `LCC_TX_PROFILE=fast` 切换到 compact 提示词，并调低 `SEG_SILENCE_MS`/`SOFT_MAX_SEC`。若在长精度模式下出现截断，只调高 `LCC_ASR_MAX_TOKENS=96`。
- 并行体感：英语广播在弹窗里默认用 `Parakeet + aggressive`。aggressive 模式用有效句末静音 ≤900ms、pending commit 120 字/1.8s、preview debounce 180ms、final 最近上下文 2 个、preview 上下文 0 个，从而缩短 MLX 翻译车道。Parakeet soft-cut 保持 4.0s 以避免误识别重复。若字幕频繁替换扰人就降到 `balanced`，若翻译稳定最重要就用 `stable`。服务器默认 `LCC_LATENCY_MODE=aggressive`，接受 `stable|balanced|aggressive`。
- 输出同步：bridge 用 4.5 秒 soft-cut + 220ms overlap 转写长语音，画面用基于 `performance.now()` 的 stream clock 排程。仅当 final backlog 真的落后时才合并短字幕。
- 视频延迟：`delaySec` 最大 12 秒。`videoDelay` 模式按原始视频帧分辨率捕获，帧率限制为最高 60fps。帧时间戳优先用 `requestVideoFrameCallback` 的 metadata，PCM tap 优先用 AudioWorklet。
- 提升翻译质量：把弹窗的**语气**预设匹配内容，并在**术语表**里固定专有名词。若需要更干净的转写，开启**精度模式**（两遍）。最后手段是把翻译模型换成 31B dense（慢 5 倍）。基准：`bench_translate_quality.py`（语气/术语表 A/B）、`bench_2pass.py`（两遍 vs 一遍）——都需在 bridge 停止后运行。
- 幻觉/噪声敏感度：调节 `webrtcvad.Vad(0..3)` 的强度。
- 本地 WS 保护：默认只允许 Chrome 扩展 origin + client token。要改 token，需同时同步 `LCC_WS_TOKEN` 与 `extension/protocol.js`。

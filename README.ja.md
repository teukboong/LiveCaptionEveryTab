# Live Caption Every Tab — あらゆるサイトの外国語音声をリアルタイム韓国語字幕に

[한국어](README.md) · [English](README.en.md) · **日本語** · [Español](README.es.md) · [中文](README.zh.md)

> 🤖 このプロジェクトは**コードからドキュメントまで、すべてバイブコーディング（AIペアプログラミング）で**作られています。

YouTube・Twitch・**X**・その他どんなサイトでも、ブラウザタブの音声を取り込み、**ローカルの Gemma-4** で文字起こし＋翻訳して、映像の上に2行字幕（原文／韓国語）を表示します。（タブキャプチャはドメインに依存しないので、音が出るタブなら何でも動きます。）
文字起こしは、ポップアップで **Granite Speech 4.1**（英語に強い）と **Qwen3-ASR**（日本語・韓国語など多言語）から選びます。どちらも句読点・大文字小文字をネイティブに付与し、発話がなければ `[no speech]` でゲートします。

## なぜ作ったか（似たツールはすでにあるが）

リアルタイム字幕／翻訳ツールは大きく2系統に分かれますが、**「ブラウザで／生きている任意のタブを／完全ローカルの LLM で意味翻訳」**という組み合わせが空いていたので、その隙間を埋めるために作りました。

| | 本プロジェクト | Whisper ベースのブラウザ拡張 | デスクトップ プレーヤー（例：LLPlayer） |
|---|---|---|---|
| **入力** | 音が出る**任意のタブ**（ライブ配信を含む） | タブ音声 | ダウンロードした動画／プレーヤーに入れたファイル・URL |
| **文字起こし(ASR)** | Granite / Qwen3（句読点・truecasing がネイティブ、無音・音楽は `[no speech]` でゲート） | 主に Whisper | 主に Whisper |
| **翻訳** | **ローカル LLM（Gemma-4）**による意味翻訳 — 文脈・代名詞を維持 | なし／直訳MT／クラウド | ローカル LLM 可（Ollama など） |
| **実行** | 100% ローカル（クラウド0） | ローカル～混在 | ローカル |
| **対象言語** | 韓国語優先（＋多言語） | ツール次第 | 多言語（言語別の最適化はまちまち） |

- **Whisper ベースの拡張**はタブはうまく取り込めますが、Whisper は無音・音楽の区間で字幕を幻覚しがちで、翻訳は無し・直訳・クラウドであることが多いです。→ ここでは句読点ネイティブの ASR ＋ 無音ゲーティング ＋ ローカル Gemma の意味翻訳で、その部分を別の解き方にしています。
- **デスクトッププレーヤー**はローカル LLM 翻訳の品質が高いものの、動画をダウンロードするかプレーヤーに入れる必要があり、ライブ配信・任意サイトには向きません。→ ここではダウンロード不要で、**音が出るタブならその場で**重ねます。

すべて**ローカル・無料**です。代わりにハードウェアの下限があります（要件は下記 [SETUP.md](SETUP.md) を参照）。非力な環境では翻訳モデルがメモリに合わせて自動でティアリングします（full/mid/lite）。

**プラットフォーム（backend）：** 同じブリッジ・同じ拡張が2つのランタイムで動き、`LCC_BACKEND` で選びます。
- **`mlx`**（既定、Apple Silicon）：インプロセス MLX — 文字起こし Granite/Qwen3、翻訳 26B-A4B。→ [SETUP.md](SETUP.md)
- **`cuda`**（Windows+NVIDIA、WSL2）：OpenAI 互換 **HTTP** — 翻訳 llama.cpp（26B GGUF）、文字起こしは **Mac と同じ granite/qwen3**（transformers, `cuda/asr_server.py`）。ポップアップの**文字起こしエンジン**切替（英語=granite／多言語=qwen3）がそのまま効き（`model` フィールドでルーティング）、エンジンごとに別サーバへ向けることもできます。whisper は使いません。→ [SETUP-windows.md](SETUP-windows.md)

VAD・文ごとの組み立て・スケジューラ・ナンバーガード・プロンプトビルダーは**プラットフォーム共有**（純粋関数）。ランタイムで変わるのは GPU の3関数（文字起こし／翻訳／要約）だけで、その境界が `bridge/backend_cuda.py`（HTTP）と server.py の "Backend seam" です。

## 構成
```
[Chrome 拡張] tabCapture（タブ音声） ──WS(PCM16 16k)──▶ [bridge/server.py]
                                                        VAD + soft-cut ASR atom
                                                        → Granite / Qwen3-ASR 文字起こし（句読点・多言語）
                                                        → unit assembler
                                                        → 26B-A4B MoE 韓国語翻訳
   [content.js 2行オーバーレイ] ◀──WS(JSON caption)──────┘
```
- ASR はポップアップで**2つの mlx-audio エンジン**から選びます（▸ 文字起こしエンジン）。**Granite Speech 4.1 2B**（`ibm-granite/granite-speech-4.1-2b`・英語に忠実、WER 0%台）と **Qwen3-ASR 1.7B**（`Qwen/Qwen3-ASR-1.7B`・日本語・韓国語を含む52言語、言語自動判定）。どちらも句読点・truecasing をネイティブに付与するので文のチャンク化がそのまま通ります。26B と同じ Apple GPU を共有（直列）。⚠ granite は mlx-audio **main の conv 修正**が必要（SETUP 参照）。
- 英語専用・低遅延の Parakeet はパワーユーザー向けの逃げ道として `LCC_ASR_ENGINE=parakeet` のみ（CPU・翻訳と並列、モデル `~/.local/share/models/live-caption/parakeet-tdt-0.6b-v2-int8`、`sherpa-onnx==1.13.2`）。ポップアップのセレクタには granite/qwen3 のみ表示。
- 翻訳：`mlx-community/gemma-4-26b-a4b-it-4bit`（mlx-lm） — 既定は **quality プロンプト**（expert interpreter・by-meaning・no-translationese ＋ few-shot 3、KV-cache でコスト償却 → 硬い文語より自然な口語）。低遅延は `LCC_TX_PROFILE=fast`。**対象言語を選択可能**（韓/英/日/中/西/仏/独）、ソース自動判定、対象=ソースならスキップ。
- RAM ~26GB（重み）＋チャンクごとに少量の KV。遅延 ~2.9–3.4s／発話チャンク（ASR ~0.7s ＋ 翻訳 ~1.4s ＋ 音声 prefill ＋ 節境界の待ち）。
- MTP はこのハードでは無意味なので未使用（MoE・dense・E4B すべて検証済み）。
- ⚠️ 正規の Chrome/Edge/Brave が必要 — 一部の Chromium フォーク（ChatGPT Atlas など）は `chrome.tabCapture` 未実装。

## 実行
### 1) ブリッジサーバ
```bash
# リポジトリのルートで（初回は ./setup.sh で venv・依存をインストール）
bash bridge/run_bridge.sh
# "[bridge] ready  ws://127.0.0.1:8765" が出たら準備完了（初回ロード ~40s）
```
- 常時起動（オプトイン、クラッシュ時自動再起動）：`bash bridge/autostart.sh install` — ⚠ ~26GB RAM 常駐。停止：`… uninstall`
- ターミナルなしで**ポップアップのボタン**で起動／停止（`🚀 ブリッジ起動`）：`bash extension/native-host/install-host.sh` を1回実行後、拡張を再読み込み（ネイティブメッセージングホスト — SETUP 6.5）。detached で起動するのでブラウザを閉じても維持されます。
- ブリッジが再起動／切断しても拡張が**自動再接続**（バックオフ）し、直近の音声を最大6秒バッファします。それより長い障害中の発話は失われることがあります。
### 2) 拡張の読み込み（Chrome）
1. `chrome://extensions` → 右上の**デベロッパーモード**をオン
2. **パッケージ化されていない拡張機能を読み込む** → このリポジトリの `extension/` フォルダを選択
3. YouTube/Twitch の動画タブで**拡張アイコンをクリック** → ポップアップの **`▶ 字幕開始`** をクリック → バッジ `ON`、オーバーレイ表示
4. ポップアップ設定：字幕の**サイズ・上下／左右位置・原文行・同期補正**（即時）、**文の待ち・音声検出**（再起動時に反映）
5. 停止は **`■ 字幕停止`**。（tabCapture はユーザーのクリック操作が必須 → 自動開始不可）

## 機能
- **自動用語プライミング**：ページ／動画タイトルを ASR・翻訳のヒントとして自動注入（ポップアップでオフ可）。
- **コンテンツ種別プリセット**：ポップアップでコンテンツ種別（一般・雑談／カンファレンス・講演／ニュース・インタビュー／個人配信）を一度選ぶと、語調（register）と遅延モードをまとめて合わせます — 講演=格式・安定、ニュース=バランス、配信=口語・即時。語調・終助詞・few-shot アンカーが内容に合わせて変わり、ソース言語（EN/JA）も自動判定して合う例を選びます。
- **用語集**：ポップアップに `名前=訳`（1行に1つ）を入れると、その語を文字起こしバイアス＋翻訳で常に同じに描画（1行ごとに訳がぶれるのを除去）。`用語ヒント` は自由テキストのバイアス。
- **精度モード（2パス再文字起こし）**：オンにすると、自然な終わり（pause/eos）や終止符号で確定する複数節の文の累積音声を、確定直前にもう一度まるごと文字起こし → VAD 断片の継ぎ目エラーを除去。確定が ~0.7s 遅くなるためトグル（既定 OFF）。オーバーラップ／分割で整列が崩れたユニットは自動除外（`unit_pure` ガード）。
- **ストリーミング字幕**：原文は ASR atom 単位で先に表示、韓国語プレビューは debounce/coalesce。確定字幕は final キューで優先処理。
- **遅延モード3段階**：`aggressive` は Parakeet の CPU 文字起こしと MLX 翻訳をできるだけ重ね、現在の unit プレビューを latest-only で先に翻訳。`balanced` は MLX がアイドルのときだけプレビュー。`stable` は確定翻訳のみ表示。final 翻訳は常にプレビューより優先。
- **Lookahead 映像遅延**：映像遅延モードでは実音声を即座に文字起こし・翻訳し、字幕は実 PCM ストリーム開始 clock と発話区間（`start_ms`/`end_ms`）に合わせて予約出力。ポップアップの同期補正で ±2秒の微調整が可能。
- **同期デバッグ**：ポップアップでオンにすると、字幕の下とコンソールに `kind/unit/start/end/due/now/lag/delay/offset/q` を表示し、出力が due time より早くないか確認できます。
- **翻訳キャッシュ／優先度**：プレビューと final のソースが同じなら再翻訳を回避し、final 翻訳をプレビューより先に処理。
- **字幕ログ**：右下の 📜 → スクロールバックパネル／二言語 `.md` エクスポート。
- **要約・質問**：パネルの ✨要約・質問欄 — ローカル 26B が過去の字幕を要約／質疑応答（ストリーミング）。

## トラブルシューティング
- オーバーレイに「ブリッジ接続が切れた」 → `run_bridge.sh` が実行中か、ポート 8765 を確認。
- 字幕が出ない → 動画に実際の発話があるか（非発話は `[no speech]` でスキップ）、タブから音が出ているか。
- 音が出ない → タブキャプチャが再生を横取りする場合。offscreen が `source→destination` の再生接続を維持するので通常は問題なし。
- ポート使用中エラー → `lsof -ti:8765 | xargs kill -9`。

## チューニングレバー
- 遅延を下げる：翻訳は既定で quality プロンプト（KV-cache でコスト償却）。さらに下げるには `LCC_TX_PROFILE=fast` で compact プロンプトを使い、`SEG_SILENCE_MS`/`SOFT_MAX_SEC` を下げます。長い精度モードで切れが見えたら `LCC_ASR_MAX_TOKENS=96` だけ上げます。
- 並列の体感：英語放送はポップアップで `Parakeet + aggressive` を既定に。aggressive は effective sentence silence ≤900ms、pending commit 120文字/1.8s、preview debounce 180ms、final recent context 2件、preview context 0件で MLX 翻訳レーンを短く使います。Parakeet soft-cut は誤認識の重複を避けるため 4.0s を維持。字幕の差し替えが頻繁で気になれば `balanced`、翻訳の安定が最優先なら `stable`。サーバ既定は `LCC_LATENCY_MODE=aggressive`、`stable|balanced|aggressive` を受け付けます。
- 出力同期：ブリッジは 4.5秒 soft-cut ＋ 220ms overlap で長い発話を文字起こしし、画面は `performance.now()` ベースの stream clock で予約。final backlog が実際に遅れたときだけ短い字幕を併合します。
- 映像遅延：`delaySec` は最大12秒。`videoDelay` モードは元の動画フレーム解像度でキャプチャし、フレームは最大60fpsに制限。フレームのタイムスタンプは `requestVideoFrameCallback` metadata を優先、PCM tap は AudioWorklet を優先。
- 翻訳品質を上げる：ポップアップの**語調**プリセットを内容に合わせ、**用語集**に固有名詞をピン留め。よりクリーンな文字起こしが必要なら**精度モード**（2パス）をオン。最後の手段として翻訳モデルを 31B dense に（5倍遅くなる）。ベンチ：`bench_translate_quality.py`（語調/用語集 A/B）、`bench_2pass.py`（2パス vs 1パス）— どちらもブリッジ停止後に実行。
- 幻覚／ノイズ感度：`webrtcvad.Vad(0..3)` の強度を調整。
- ローカル WS 保護：既定は Chrome 拡張 origin ＋ client token のみ許可。token を変えるには `LCC_WS_TOKEN` と `extension/protocol.js` を合わせて揃えます。

# rqark-agent-cloud

## ASR

`POST /api/asr` 收一段音訊，回粵語文字。`GET /api/asr/health` 講返 engine 載入咗未。

```bash
curl -H "Authorization: Bearer $TOKEN" -F file=@speech.webm http://localhost:8000/api/asr
```

WAV 由 Python 直接解，其他格式（webm / ogg / mp4）交 ffmpeg，所以 image 一定要有 ffmpeg。

### Engine

| `ASR_ENGINE` | 實際用 | 平台 | 模型預設 |
| --- | --- | --- | --- |
| 空（預設） | Apple Silicon 用 `qwen3`，其餘用 `faster-whisper` | 自動 | 見下 |
| `qwen3` | `mlx-audio` + Qwen3-ASR | 只限 Apple Silicon | `mlx-community/Qwen3-ASR-0.6B-8bit` |
| `faster-whisper` | `ctranslate2` | CPU / GPU | `large-v3`（CPU server 用 `ASR_MODEL=small`） |

粵語用 `qwen3`：Whisper 嘅 yue 會將口語粵語書面語化，`initial_prompt` 拗唔返。

**兩個 engine 都唔喺 `uv.lock` 入面**，因為 lock 要用 registry 憑證 regen。Deployment 要自己加：

```bash
uv add faster-whisper   # 或者 mlx-audio
uv lock
```

冇裝嘅話 `/api/asr` 回 503 `ASR_UNAVAILABLE`，`/api/asr/health` 嘅 `loaded` 一直係 `false`。

### 資源

| 項目 | 需求 |
| --- | --- |
| 記憶體 | `faster-whisper large-v3` int8 約 2GB，`qwen3` 0.6B-8bit 約 2GB，連 app 建議 container limit ≥ 4GB（compose 預設 `ASR_MEM_LIMIT=4g`） |
| 併發 | `ASR_CONCURRENCY`（預設 1），超額排隊 `ASR_QUEUE_TIMEOUT` 秒，再超就 429 |
| 逾時 | `ASR_TIMEOUT`（預設 60s），超時回 504。thread 停唔到，所以併發要細 |
| 上傳上限 | `ASR_MAX_BYTES`（預設 25MB）、`ASR_MAX_SECONDS`（預設 60s），超出回 413 |
| 模型快取 | `ASR_MODEL_DIR` / `HF_HOME` 指去 `/models`，compose 用 named volume `asr-models` 掛住，唔會每次 rebuild 重下 |

### 後處理

ASR 原輸出唔可以直接用。`postprocess` 做三件事：

1. 簡體繫詞「系」改粵語「係」，但保留構詞（系統、聯繫）—— 要喺 OpenCC 之前做，否則會出「繫系統」
2. OpenCC `s2hk` 轉香港繁體（`ASR_OPENCC` / `ASR_OPENCC_CONFIG`）
3. 過濾靜音幻覺（只得「係。」「嗯。」或純標點 → 回空字串）

`text` 係空字串即係冇聽到嘢，前端唔應該當成一句用戶訊息送出。

## Voice（`/api/voice/ws`）

同 `/api/asr` 唔同：`/api/asr` 收一段完整錄音，voice WebSocket 收**連續串流**，由 server 決定一句幾時開始幾時完。

```
ws://localhost:8000/api/voice/ws?token=<data cloud token>
```

### 連線

`accept` 之後先驗 token（同 chat WS 一樣用 `?token=`），失敗回 `4401`；同一帳號第二條連線或超出全域上限回 `4429`（先送 `VOICE_BUSY` 錯誤幀）。

連上即刻收到：

```json
{"type":"ready","sample_rate":16000,"format":"pcm_s16le","channels":1,"max_seconds":60,"max_bytes":26214400,"max_frame_bytes":65536,"preroll_ms":400,"concurrency":1,"wake_word":true,"wake_models":["wednesday","diana"],"vad":true}
```

### 上行

| 幀 | 內容 |
| --- | --- |
| binary | PCM int16 LE、單聲道、16kHz。任意長度，server 自己拼 frame |
| `{"type":"mode","value":"wake"}` | 轉 always-on 模式：未聽到喚醒詞之前，音訊只餵 wake model，唔會 VAD、唔會轉錄 |
| `{"type":"start"}` | 丟棄手上音訊，開新一句 |
| `{"type":"cancel"}` | 丟棄手上音訊 |
| `{"type":"stop"}` | 即刻收句，唔等 VAD |

控制幀嘅 discriminator 收 `type` 或者 `kind`。一定要係 JSON object；唔係就回 `BAD_FRAME`，socket 唔會斷。未知 `type` 一樣係 `BAD_FRAME`。

### 下行

| `type` | 幾時送 |
| --- | --- |
| `ready` | 連上 |
| `started` | 收到 `start` |
| `wake` | 偵測到 wake word（`score` + `name`，講邊個 model 叫醒） |
| `final` | 一句轉好：`text` / `duration_ms` / `elapsed_ms` / `truncated` |
| `error` | `code` + `message`，socket 通常仲開得 |

`text` 空白代表聽到聲但冇字，唔好當一句用戶訊息送出。

### 流程

1. **Wake word**（內置 `src/apps/voice/models/wednesday.onnx`，openWakeWord ONNX）。`VOICE_WAKE_MODEL` 填內置名（`wednesday,diana`）或路徑，逗號分隔可以多個，一個 openWakeWord 實例會同時幫全部 model 打分，共用同一份 feature extractor，所以多一個詞成本好低。逐 80ms frame 打分。`mode` 係 `wake` 時，叫醒之前嘅音訊只入 wake model，VAD 同 ASR 完全唔行
2. 任何一個 model 過 `VOICE_WAKE_THRESHOLD` 就送 `wake`（`name` 講邊個中）；同一 chunk 喚醒詞後面嘅音訊會交返落 VAD（唔會漏咗命令開頭），之後嗰個 model 喺 `VOICE_WAKE_COOLDOWN` 秒內唔再 fire，其他 model 照計
3. 喚醒詞之前 `VOICE_PREROLL_MS`（預設 400ms）嘅音訊、同叫醒之後 `VOICE_WAKE_GUARD_MS`（預設 400ms）嘅音訊，都會被留住但**唔會即刻開一句**。要等 VAD 真正聽到人聲才開，開嗰時連埋呢兩段一齊用，所以「wednesday 今日天氣」唔會漏咗開頭。太長會連喚醒詞本身都轉埋字。守衛期係為咗蓋住喚醒詞尾巴——唔蓋嘅話，尾巴會被 VAD 當成人聲，結果「叫醒 + 靜音」都變成一句送去轉錄，出「聽唔清楚」
4. 叫醒之後一直冇人聲，`VOICE_WAKE_WINDOW`（預設 10s）之後送 `VOICE_WAKE_TIMEOUT` 並回去等喚醒詞 —— 唔會拿住一段靜音去轉錄
5. **VAD**（Silero，openWakeWord 自帶 `silero_vad.onnx`）逐 30ms frame 判斷有冇人聲。講嘢之前嘅 250ms 放喺 ring buffer，一開口就連埋，唔會斬頭
6. 靜 `VOICE_VAD_SILENCE_MS` 之後收句，短過 `VOICE_VAD_MIN_MS` 當雜音丟；長過 `VOICE_MAX_SECONDS` 就切開並標 `truncated`
7. 收句交 `asr.engine.transcribe`（同 `/api/asr` 同一條路：併發 1、60s timeout、OpenCC `s2hk`）

喚醒詞模型跟住 package 一齊入 image（`.onnx` 831KB），唔使另外下載，亦唔會因為 `.env` 留空就關掉。

openWakeWord 0.4.0 嘅 `Model.reset()` 只清 prediction buffer，唔會清 preprocessor 嘅 `raw_data_buffer` / `melspectrogram_buffer` / `feature_buffer`。唔自己清，fire 完之後頭 ~1.3 秒會用上一輪嘅特徵打分，下次叫唔醒，pooled model 更加會將上一條連線嘅音帶落下一條。所以 `Detector.reset()` 同收回 pool 之前都會自己清。

兩個 stage 都係 lazy 載入，推論（wake 15.6ms + VAD 3.0ms / 秒音訊）行 thread pool，唔阻 event loop。載好嘅模型會留喺 process 內重用，第二條連線唔使再等半秒 load model。

package 冇裝或者冇配置就靜靜關掉嗰個 stage，`ready` 嘅 `wake_word` / `vad` 會講返；但配置咗而載入失敗就唔會靜，會送 `WAKE_UNAVAILABLE` / `VAD_UNAVAILABLE` 錯誤幀，否則打錯字會變成「全部轉錄」而冇人發覺。

### 限制

| 項目 | 設定 | 預設 |
| --- | --- | --- |
| 全域連線 | `VOICE_MAX_CONNECTIONS`（Redis lease，跨 worker） | 8 |
| 每帳號連線 | `VOICE_MAX_PER_USER` | 1 |
| 跨 worker 計數 | `VOICE_REDIS` / `VOICE_LEASE_TTL` | true / 360 |
| 閒置 | `VOICE_IDLE_TIMEOUT` 秒冇 frame 就 `4408` | 300 |
| 單句長度 | `VOICE_MAX_SECONDS` | 60 |
| 單句位元組 | `VOICE_MAX_BYTES`，超出 `AUDIO_TOO_LARGE` 並 `close(1009)` | 25MB |
| 單幀上限 | `VOICE_MAX_FRAME_BYTES`，超出 `FRAME_TOO_LARGE`（唔斷線） | 64KB |
| 預留音訊 | `VOICE_PREROLL_MS` | 400 |
| 叫醒後靜默期 | `VOICE_WAKE_GUARD_MS`，呢段音唔計入「開始講嘢」 | 400 |
| VAD | `VOICE_VAD_THRESHOLD` / `VOICE_VAD_SILENCE_MS` / `VOICE_VAD_LEAD_MS` / `VOICE_VAD_MIN_MS` | 0.5 / 800 / 250 / 250 |
| Wake word | `VOICE_WAKE_MODEL`（空 = 用內置 `wednesday`；可寫 `wednesday,diana` 或多個路徑）/ `VOICE_WAKE_THRESHOLD` | 空 / 0.5 |
| 重複觸發 | `VOICE_WAKE_COOLDOWN` 秒內同一個 model 唔再 fire | 1.5 |
| 叫醒後等人聲 | `VOICE_WAKE_WINDOW` 秒冇人聲就 `VOICE_WAKE_TIMEOUT` | 10 |

幀率開支：16kHz int16 單聲道 = 32KB/s，比 Opus（4KB/s 以內）大一個數量級。Opus 要 server 邊收邊 decode（libav pipe），未做。

`GET /api/voice/health` 回連線數、各 stage 狀態同 ASR 排隊數。

連線數用 Redis lease 計（per-user 一個 key、每個 slot 一個 key，`SET NX EX`），所以 4 個 uvicorn worker 加起來先係真上限；Redis 冇回應就降級做 per-worker 計數，唔會拒絕所有連線。

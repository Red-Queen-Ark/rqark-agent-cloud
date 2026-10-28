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
| `faster-whisper` | `ctranslate2` | CPU / GPU | `large-v3`（`ASR_MODEL=small` 落細機） |

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
| 記憶體 | `faster-whisper large-v3` int8 約 2GB，`qwen3` 0.6B-8bit 約 2GB，連 app 建議 container limit ≥ 3GB（compose 預設 `ASR_MEM_LIMIT=3g`） |
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
{"type":"ready","sample_rate":16000,"format":"pcm_s16le","channels":1,"max_seconds":60,"max_bytes":26214400,"concurrency":1,"wake_word":false,"vad":true}
```

### 上行

| 幀 | 內容 |
| --- | --- |
| binary | PCM int16 LE、單聲道、16kHz。任意長度，server 自己拼 frame |
| `{"type":"start"}` | 丟棄手上音訊，開新一句 |
| `{"type":"cancel"}` | 丟棄手上音訊 |
| `{"type":"stop"}` | 即刻收句，唔等 VAD |

### 下行

| `type` | 幾時送 |
| --- | --- |
| `ready` | 連上 |
| `started` | 收到 `start` |
| `wake` | 偵測到 wake word（`score`） |
| `final` | 一句轉好：`text` / `duration_ms` / `elapsed_ms` / `truncated` |
| `error` | `code` + `message`，socket 通常仲開得 |

`text` 空白代表聽到聲但冇字，唔好當一句用戶訊息送出。

### 流程

1. **Wake word**（`VOICE_WAKE_MODEL`，openWakeWord ONNX）逐 80ms frame 打分，過 `VOICE_WAKE_THRESHOLD` 就送 `wake` 並清走緩衝，唔想將 wake word 本身送去轉錄
2. **VAD**（Silero，openWakeWord 自帶 `silero_vad.onnx`）逐 30ms frame 判斷有冇人聲。講嘢之前嘅 250ms 放喺 ring buffer，一開口就連埋，唔會斬頭
3. 靜 `VOICE_VAD_SILENCE_MS` 之後收句，短過 `VOICE_VAD_MIN_MS` 當雜音丟；長過 `VOICE_MAX_SECONDS` 就切開並標 `truncated`
4. 收句交 `asr.engine.transcribe`（同 `/api/asr` 同一條路：併發 1、60s timeout、OpenCC `s2hk`）

兩個 stage 都係 lazy 載入，模型唔喺度就靜靜關掉嗰個 stage，`ready` 嘅 `wake_word` / `vad` 會講返。

### 限制

| 項目 | 設定 | 預設 |
| --- | --- | --- |
| 全域連線 | `VOICE_MAX_CONNECTIONS` | 4 |
| 每帳號連線 | `VOICE_MAX_PER_USER` | 1 |
| 閒置 | `VOICE_IDLE_TIMEOUT` 秒冇 frame 就 `4408` | 300 |
| 單句長度 | `VOICE_MAX_SECONDS` | 60 |
| VAD | `VOICE_VAD_THRESHOLD` / `VOICE_VAD_SILENCE_MS` / `VOICE_VAD_LEAD_MS` / `VOICE_VAD_MIN_MS` | 0.5 / 1000 / 250 / 250 |
| Wake word | `VOICE_WAKE_MODEL`（空 = 關）/ `VOICE_WAKE_THRESHOLD` | 空 / 0.5 |

幀率開支：16kHz int16 單聲道 = 32KB/s，比 Opus（4KB/s 以內）大一個數量級。Opus 要 server 邊收邊 decode（libav pipe），未做。

`GET /api/voice/health` 回連線數同各 stage 狀態。

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

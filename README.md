# rqark-agent-cloud

The online runtime of the RQArk Ecology System: an HTTP and WebSocket service over `rqark-app` that keeps an agent reachable while the machine that owns it is off, and that turns speech into text.

## Why rqark-agent-cloud

`rqark-app` runs an agent in the process that started it. This is the process that stays up: conversations survive a closed laptop, scheduled runs happen without one, and the microphone is somewhere the browser can reach.

- **One socket per conversation** — the browser opens `/api/chat/ws/{session_id}` and receives the turn as it is produced, with the history replayed first so a reconnected tab picks up where it left off.
- **Speech is part of the service, not the client** — ASR, wake word and voice activity detection run here, so a thin client only sends PCM frames.
- **A workspace in a container** — file, shell and git calls go to a per-user container through `/api/workspace`, so a turn cannot touch the host it runs on.
- **The credentials stay in the data cloud** — a chat turn is proxied with the stored provider, and this service never holds the key.

## The ecosystem

| Repository | What it is | Used here for |
| --- | --- | --- |
| [`rqark-core`](../rqark-core) | Stateless agent engine for Python. | The agent loop behind a chat turn. |
| [`rqark-app`](../rqark-app) | Sessions, providers and agents on top of `rqark-core`. | The agent, its session storage and its provider rotation. |
| [`rqark-sandbox`](../rqark-sandbox) | Docker workspace with file, shell and git tools. | The workspace behind `/api/workspace`. |
| [`rqark-data-cloud`](../rqark-data-cloud) | Accounts and stored data. | Identity for every request, and the provider used for a turn. |
| [`rqark-task`](../rqark-task) | Worker that runs scheduled agent tasks. | Nothing; it calls the data cloud, not this service. |
| `rqark-agent-cloud` | This repository. | — |

## Architecture

FastAPI over uvicorn, with `rqark-app` doing the agent work and Redis carrying the socket state between workers.

```
main.py                uvicorn entry; `python main.py debug` turns on reload
src/
├── app.py             the FastAPI app: prefix, CORS, exception handlers, routers
├── apps/
│   ├── chat/          the conversation socket, replay, revert, images, idle sweep
│   ├── asr/           speech to text over a posted recording
│   ├── voice/         the streaming socket: wake word, VAD, one utterance at a time
│   ├── workspace/     file, shell and archive calls into the sandbox container
│   └── ping/          health check
├── config/            every setting, read from the environment at import
├── middleware/        bearer auth against the data cloud
└── utils/             logging and the Redis client
docker/                the image and its entrypoint
_docs/                 the written notes on the workspace and the sandbox
```

- **Routers are conditional** — `asr` and `voice` are only mounted when `SPEECH_ENABLED` is true, and `workspace` only when `SANDBOX_ENABLED` is. The imports are lazy, so a text-only deployment never loads the wake word or ASR stacks.
- **Auth is borrowed** — every request carries a data cloud bearer token, which this service validates against `users/me`. There is no account store here.
- **Socket state lives in Redis** — the pub/sub channel, the replay buffer, the per-room lock and the voice leases are all keys, so the two uvicorn workers see one conversation rather than two.
- **A turn has no round limit and no retry limit** — that is the agent's design, inherited from `rqark-app`, not an error handler here.

## Running

```bash
docker compose up -d
```

That builds the image and publishes port `8000`, with the docker socket mounted for the workspace containers and `host.docker.internal` mapped so a data cloud on the host is reachable.

On the host instead:

```bash
uv sync
python main.py          # port 8000
python main.py debug    # the same, with reload
```

Both need Redis and a reachable data cloud; `.env` points at both on localhost.

## API

Everything is mounted under `API_PREFIX`, `/api` by default. FastAPI serves its own OpenAPI document at `/api/openapi.json`, with `/api/docs` and `/api/redoc` beside it.

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/api/ping` | Health check. |
| WS | `/api/chat/ws/{session_id}` | The conversation. Token in `?token=`. |
| GET | `/api/chat/{session_id}/replay` | The stored transcript. |
| POST | `/api/chat/{session_id}/revert` | Drop the conversation back to a message. |
| POST | `/api/asr` | Transcribe one posted recording. |
| GET | `/api/asr/health` | Whether the engine is loaded. |
| WS | `/api/voice/ws` | Streaming speech. Token in `?token=`. |
| GET | `/api/voice/health` | Connections, stage state, ASR queue depth. |
| GET | `/api/workspace/*` | List, stat, read, write, upload, archive. |

## Speech

Speech is off by default. `SPEECH_ENABLED=false` unmounts `/api/asr` and `/api/voice` and skips importing the stacks, which is what a small machine serving text only wants.

### ASR

`POST /api/asr` takes one recording and returns Cantonese text. `GET /api/asr/health` reports whether the engine is loaded.

```bash
curl -H "Authorization: Bearer $TOKEN" -F file=@speech.webm http://localhost:8000/api/asr
```

WAV is decoded in Python; every other format (webm, ogg, mp4) goes through ffmpeg, so the image has to carry it.

| `ASR_ENGINE` | Actually used | Platform | Default model |
| --- | --- | --- | --- |
| empty (default) | `qwen3` on Apple Silicon, `faster-whisper` elsewhere | automatic | see below |
| `qwen3` | `mlx-audio` + Qwen3-ASR | Apple Silicon only | `mlx-community/Qwen3-ASR-0.6B-8bit` |
| `faster-whisper` | `ctranslate2` | CPU / GPU | `large-v3` (`ASR_MODEL=small` on a CPU server) |

Cantonese uses `qwen3`: Whisper's `yue` writes spoken Cantonese as Standard Written Chinese, and an `initial_prompt` does not talk it out of that.

**Neither engine is in `uv.lock`**, because regenerating the lock needs registry credentials. A deployment adds its own:

```bash
uv add faster-whisper   # or mlx-audio
uv lock
```

Without one, `/api/asr` answers `503 ASR_UNAVAILABLE` and `loaded` in `/api/asr/health` stays `false`.

| Resource | Requirement |
| --- | --- |
| Memory | `faster-whisper large-v3` int8 is about 2GB, `qwen3` 0.6B-8bit about 2GB; a container limit of at least 4GB with the app (the production compose sets `mem_limit` to `${MEM_LIMIT:-4g}`) |
| Concurrency | `ASR_CONCURRENCY` (default 1); beyond that a call queues for `ASR_QUEUE_TIMEOUT` seconds, then gets `429` |
| Timeout | `ASR_TIMEOUT` (default 60s), then `504`. A thread cannot be stopped, which is why concurrency stays low |
| Upload cap | `ASR_MAX_BYTES` (default 25MB), `ASR_MAX_SECONDS` (default 60s), then `413` |
| Model cache | `ASR_MODEL_DIR` / `HF_HOME` decide where models land; point both at a mounted path so a rebuild does not download again |

The raw output is not fit to use, so `postprocess` does three things:

1. Rewrites the Simplified copula 「系」 to Cantonese 「係」 but keeps compounds (系統, 聯繫) — this has to happen before OpenCC, or it produces 「繫系統」.
2. Converts to Hong Kong Traditional with OpenCC `s2hk` (`ASR_OPENCC` / `ASR_OPENCC_CONFIG`).
3. Drops silence hallucinations: a result that is only 「係。」, 「嗯。」 or punctuation becomes an empty string.

An empty `text` means nothing was heard. A client should not send it as a user message.

### Voice

`/api/asr` takes a finished recording. `/api/voice/ws` takes a **continuous stream** and decides where an utterance starts and ends.

```
ws://localhost:8000/api/voice/ws?token=<data cloud token>
```

**Connecting.** The token is checked after `accept`, the same `?token=` as the chat socket; a failure closes with `4401`. A second connection for the same account, or one over the global cap, closes with `4429` after a `VOICE_BUSY` error frame.

The first frame is always:

```json
{"type":"ready","sample_rate":16000,"format":"pcm_s16le","channels":1,"max_seconds":60,"max_bytes":26214400,"max_frame_bytes":65536,"preroll_ms":400,"concurrency":1,"wake_word":true,"wake_models":["wednesday","diana"],"vad":true}
```

**Upstream.**

| Frame | Contents |
| --- | --- |
| binary | PCM int16 LE, mono, 16kHz. Any length; the server reassembles frames. |
| `{"type":"mode","value":"wake"}` | Always-on mode: until the wake word is heard, audio only feeds the wake models — no VAD, no transcription. |
| `{"type":"start"}` | Discard the audio in hand and begin a new utterance. |
| `{"type":"cancel"}` | Discard the audio in hand. |
| `{"type":"stop"}` | Close the utterance now, without waiting for VAD. |

The discriminator on a control frame may be `type` or `kind`. It has to be a JSON object; anything else is `BAD_FRAME` and the socket stays open. An unknown `type` is `BAD_FRAME` too.

**Downstream.**

| `type` | When |
| --- | --- |
| `ready` | On connect. |
| `started` | On `start`. |
| `wake` | The wake word fired: `score` plus `name`, naming the model that heard it. |
| `final` | One utterance transcribed: `text` / `duration_ms` / `elapsed_ms` / `truncated`. |
| `error` | `code` and `message`; the socket usually stays open. |

A blank `text` means sound without words, not a user message.

**The flow.**

1. **Wake word** — built in at `src/apps/voice/models/wednesday.onnx`, an openWakeWord ONNX model. `VOICE_WAKE_MODEL` takes a built-in name (`wednesday,diana`) or a path, comma separated for several; one openWakeWord instance scores every model and shares the feature extractor, so an extra word costs very little. Scoring is per 80ms frame. In `mode: wake`, audio before the wake word only reaches the wake models.
2. A model over `VOICE_WAKE_THRESHOLD` sends `wake` with the `name` that matched; the rest of that chunk goes back into VAD so the start of the command is not lost, and that model does not fire again for `VOICE_WAKE_COOLDOWN` seconds while the others keep scoring.
3. The `VOICE_PREROLL_MS` (default 400ms) before the wake word and the `VOICE_WAKE_GUARD_MS` (default 400ms) after it are kept but do **not** open an utterance on their own. VAD has to hear a voice first, and when it does the utterance includes both stretches — so 「wednesday 今日天氣」 does not lose its opening. Held too long, even the wake word itself gets transcribed. The guard period covers the tail of the wake word: without it the tail reads as a voice and 「wake word + silence」 becomes an utterance, which transcribes to 「聽唔清楚」.
4. If nobody speaks within `VOICE_WAKE_WINDOW` (default 10s) the server sends `VOICE_WAKE_TIMEOUT` and goes back to waiting, rather than transcribing silence.
5. **VAD** — Silero, from openWakeWord's own `silero_vad.onnx`, decides per 30ms frame. The 250ms before speech sits in a ring buffer and is prepended, so a word is never clipped.
6. After `VOICE_VAD_SILENCE_MS` of silence the utterance closes; shorter than `VOICE_VAD_MIN_MS` is dropped as noise, longer than `VOICE_MAX_SECONDS` is split and marked `truncated`.
7. The utterance goes to `asr.engine.transcribe` — the same path as `/api/asr`: concurrency 1, 60s timeout, OpenCC `s2hk`.

The wake models ship in the image (831KB of `.onnx`), so nothing is downloaded and an empty `.env` does not switch them off.

openWakeWord 0.4.0's `Model.reset()` clears the prediction buffer but not the preprocessor's `raw_data_buffer`, `melspectrogram_buffer` or `feature_buffer`. Left alone, the first ~1.3 seconds after a fire score against the previous round's features, the next wake fails, and a pooled model carries one connection's audio into the next. `Detector.reset()` and the return to the pool both clear them explicitly.

Both stages load lazily and run inference in a thread pool (15.6ms wake plus 3.0ms VAD per second of audio), so the event loop is never blocked. A loaded model stays in the process, so a second connection does not wait out the load again.

A stage whose package is missing or unconfigured turns itself off quietly, and `ready` reports it in `wake_word` / `vad`. A stage that is configured but fails to load is not quiet: it sends `WAKE_UNAVAILABLE` or `VAD_UNAVAILABLE`, because otherwise a typo would silently turn everything into plain transcription.

**Limits.**

| Item | Setting | Default |
| --- | --- | --- |
| Global switch | `SPEECH_ENABLED` for both ASR and voice; false unmounts the routes and skips the imports | false |
| Global connections | `VOICE_MAX_CONNECTIONS`, a Redis lease across workers | 8 |
| Per account | `VOICE_MAX_PER_USER` | 1 |
| Cross-worker counting | `VOICE_REDIS` / `VOICE_LEASE_TTL` | true / 360 |
| Idle | `VOICE_IDLE_TIMEOUT` seconds without a frame, then `4408` | 300 |
| Utterance length | `VOICE_MAX_SECONDS` | 60 |
| Utterance bytes | `VOICE_MAX_BYTES`, over it `AUDIO_TOO_LARGE` and `close(1009)` | 25MB |
| Frame size | `VOICE_MAX_FRAME_BYTES`, over it `FRAME_TOO_LARGE` without closing | 64KB |
| Preroll | `VOICE_PREROLL_MS` | 400 |
| Post-wake guard | `VOICE_WAKE_GUARD_MS`, audio that does not count as speech starting | 400 |
| VAD | `VOICE_VAD_THRESHOLD` / `VOICE_VAD_SILENCE_MS` / `VOICE_VAD_LEAD_MS` / `VOICE_VAD_MIN_MS` | 0.5 / 800 / 250 / 250 |
| Wake word | `VOICE_WAKE_MODEL` (empty means the built-in `wednesday`; `wednesday,diana` or paths for more) / `VOICE_WAKE_THRESHOLD` | empty / 0.5 |
| Repeat fire | `VOICE_WAKE_COOLDOWN` seconds before the same model fires again | 1.5 |
| Waiting after wake | `VOICE_WAKE_TIMEOUT` after `VOICE_WAKE_WINDOW` seconds without a voice | 10 |

The frame rate costs 32KB/s at 16kHz int16 mono, an order of magnitude more than Opus (under 4KB/s). Opus would need the server to decode as it receives, through a libav pipe; that is not done.

Connection counts are Redis leases — one key per user, one per slot, `SET NX EX` — so four uvicorn workers share one real cap. With Redis unreachable it falls back to a per-worker count instead of refusing everything.

## Environment

`.env` holds the values a local checkout needs. Every key falls back to the listed default when it is unset or empty.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROJECT_NAME` | empty | Prefix for the container name compose creates. |
| `API_PREFIX` | `/api` | Path every route is mounted under. |
| `DOCS_URL` | `/api/docs` | Swagger UI. |
| `REDOC_URL` | `/api/redoc` | ReDoc. |
| `OPENAPI_URL` | `/api/openapi.json` | The generated document. |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins. |
| `CORS_ALLOW_CREDENTIALS` | `true` | Whether credentials are allowed across origins. |
| `REDIS_SERVER` | `localhost` | Redis host. Compose overrides it. |
| `REDIS_PORT` | `6379` | Redis port. |
| `REDIS_URL` | `redis://{server}:{port}/0` | Wins over the two above when set. |
| `DATA_CLOUD_URL` | `http://localhost:7302` | Where tokens are validated and stored data is read. |
| `DATA_CLOUD_TIMEOUT` | `30` | Seconds per call to it. |
| `CHAT_IDLE_TIMEOUT` | `900` | Seconds a room may sit idle before it is closed. |
| `CHAT_IDLE_CHECK` | `5` | How often the sweep looks. |
| `CHAT_SHUTDOWN_TIMEOUT` | `30` | How long a shutdown waits for pending writes. |
| `IMAGE_MAX_BYTES` | `5242880` | Largest image accepted in a message. |
| `IMAGE_MAX_PER_MESSAGE` | `4` | Images allowed in one message. |
| `SPEECH_ENABLED` | `false` | Mounts `/api/asr` and `/api/voice` and loads the stacks. |
| `ASR_ENGINE` | empty | `qwen3`, `faster-whisper`, or empty to pick by platform. |
| `ASR_MODEL` | empty | Model id or size; empty uses the engine default. |
| `ASR_MODEL_DIR` | empty | Where models are cached. |
| `ASR_LANGUAGE` | `Cantonese` | Language hint passed to the engine. |
| `ASR_SAMPLE_RATE` | `16000` | Rate the audio is resampled to. |
| `ASR_MAX_BYTES` | `26214400` | Upload cap. |
| `ASR_MAX_SECONDS` | `60` | Length cap. |
| `ASR_TIMEOUT` | `60` | Seconds before the request gives up. |
| `ASR_QUEUE_TIMEOUT` | `30` | Seconds a call may wait for a slot. |
| `ASR_QUEUE_DEPTH` | `2` | Calls allowed to queue. |
| `ASR_CONCURRENCY` | `1` | Transcriptions at once. |
| `ASR_OPENCC` | `true` | Whether the OpenCC step runs. |
| `ASR_OPENCC_CONFIG` | `s2hk` | OpenCC configuration. |
| `VOICE_MAX_BYTES` | `ASR_MAX_BYTES` | Utterance cap. |
| `VOICE_MAX_SECONDS` | `ASR_MAX_SECONDS` | Utterance length cap. |
| `VOICE_MAX_FRAME_BYTES` | `65536` | Frame cap. |
| `VOICE_PREROLL_MS` | `400` | Audio kept ahead of a trigger. |
| `VOICE_WAKE_MODEL` | empty | Built-in names or paths, comma separated. |
| `VOICE_WAKE_THRESHOLD` | `0.5` | Score a model must reach. |
| `VOICE_WAKE_COOLDOWN` | `1.5` | Seconds before the same model fires again. |
| `VOICE_WAKE_WINDOW` | `10` | Seconds to wait for a voice after the wake word. |
| `VOICE_WAKE_GUARD_MS` | `400` | Audio after the wake word that is not speech starting. |
| `VOICE_VAD_THRESHOLD` | `0.5` | VAD decision threshold. |
| `VOICE_VAD_SILENCE_MS` | `800` | Silence that closes an utterance. |
| `VOICE_VAD_LEAD_MS` | `250` | Audio kept before speech begins. |
| `VOICE_VAD_MIN_MS` | `250` | Shortest utterance kept. |
| `VOICE_MAX_CONNECTIONS` | `8` | Global socket cap. |
| `VOICE_MAX_PER_USER` | `1` | Per-account cap. |
| `VOICE_IDLE_TIMEOUT` | `300` | Seconds without a frame before `4408`. |
| `VOICE_REDIS` | `true` | Whether the lease counters use Redis. |
| `VOICE_LEASE_TTL` | `360` | Life of a lease. |
| `SANDBOX_ENABLED` | `true` | Mounts `/api/workspace` and uses the sandbox. |
| `SANDBOX_IMAGE` | `ubuntu:24.04` | Image a workspace container is created from. |
| `WORKSPACE_START_TIMEOUT` | `30` | Seconds a container may take to start. |
| `WORKSPACE_MAX_BYTES` | `104857600` | Largest file read or written. |
| `WORKSPACE_SHELL_MAX_BYTES` | `8388608` | stdout and stderr cap for one shell call. |

## Testing

```bash
uv run pytest
```

Before a change, also run `uv run ruff check --fix` and `uv run ty check --fix`; `uv run pyright` and `uv run mypy` are there to confirm type safety. Comments are not written in this repository — the code is expected to say it.

## Deployment

`docker-compose.production.yml` is the deployment shape: one container, the docker socket mounted because the workspace containers it starts are siblings rather than children, `mem_limit` 4GB for the ASR models, and `stop_grace_period` 90s so an in-flight turn can finish. It joins the external `rqark-project` network and resolves the data cloud as `server:7302`; the network is created once per host with `docker network create rqark-project`.

The host has to carry two things: `/var/run/docker.sock`, and a local copy of `SANDBOX_IMAGE`. A missing image makes `_create()` raise `ImageNotFound` and every workspace tool fails, so `docker pull ubuntu:24.04` belongs in the deploy step.

A `v*` tag builds `docker/Dockerfile` and pushes the image to the GitHub Container Registry as both `X.Y.Z` and `X.Y`.

## Contributing

Issues, commits, pull requests, versions and releases follow [`CONTRIBUTING.md`](./CONTRIBUTING.md).

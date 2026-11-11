# Sandbox 接駁

## 已完成

### 工具

`src/apps/sandbox/tools/`，喺 `ChatHelper.chat()` build agent 時經 `swap_agent()` 換入：

| 換走 | 沙盒版 |
|---|---|
| `exec` `read_file` `write_file` `edit_file` `grep` `list_dir` | 直接換 |
| `gitea` `github` `gitlab` | 換，`params()`（token / base_url / username）原封帶過去 |
| `subagent` | 重建，**遞迴**換埋入面啲 tool 同 prompt |
| `web_fetch` `web_search` `plan_update` | 唔換，原物件直接回 |

`swap_agent()` 係唯一入口（`src/apps/sandbox/__init__.py`），同時處理 prompts 同 tools，
避免將來只換一邊。`swap_tools()` 內部見到 `SubAgent` 會遞迴埋 prompts。

### Prompt

`src/apps/sandbox/prompts.py`。四個 core prompt 有沙盒版，API 同 core 對齊
（`priority` 一樣、`params()` 回 `{}`，`to_dict()['name']` 唔變，config 相容）：

| Core | 沙盒 |
|---|---|
| `AgentsMdPrompt` | `cat /workspace/AGENTS.md`，唔存在回 `None` |
| `GitProjectPrompt` | 喺 `/workspace` 跑 git，輸出格式同 core 一致（含 40 行 truncate） |
| `PlatformPrompt` | 固定講「Linux in a Docker container，`/bin/sh`」（core 用 `sys.platform`，dev 喺 macOS 會講錯） |
| `WorkspacePrompt` | `Current workspace: /workspace` |

靜態嗰啲（`AntiSycophancy` / `Character` / `DateTime` / `Role` / `SubAgent`）唔換。

### Session 綁 owner

數據層由 data-cloud 按 token 隔離（`authorize_session()` 喺 `helper.py`，websocket 用
`claim=True`、HTTP replay 唔 claim）。傳輸層（Redis channel / replay / lock / runner）
就以 `room_key(user_id, session_id)` = `f'{user_id}:{session_id}'` 做 namespace，
收喺 `src/apps/chat/scope.py`。

即係 A 同 B 用同一個 `session_id` 都唔會撞：

| 用途 | key |
|---|---|
| pub/sub | `chat:{room}` |
| control | `chat:control:{room}` |
| replay | `chat:replay:{room}` |
| lock | `chat:busy:{room}` |
| runner | `_runners[room]` |

`user_id` 由 data-cloud `users/me` 派（UUID，client 揀唔到），所以 `:` 分隔唔會造成歧義。

## Deploy

Production 用 `docker-compose.production.yml`（tag `v*` 觸發 `.gitea/workflows/deploy.yaml`），
應用唔使帶 sandbox image 入 build context，只要 host daemon 拉到就得。

Prerequisites：

1. host 有 `/var/run/docker.sock`（已經掛入 `agent-cloud`）。
2. host 拉到 `SANDBOX_IMAGE`（預設 `ubuntu:24.04`）。拉唔到就 `_create()` 會掟
   `ImageNotFound`，tools 全滅。想穩陣就 deploy 前 `docker pull ubuntu:24.04`。
3. `SANDBOX_IMAGE` 兩個 compose file 都有 expose，可以 `SANDBOX_IMAGE=... docker compose up`。

現階段 `ubuntu:24.04` = 冇 git / node / uv，所以 `exec` / 讀寫檔 / grep / list_dir 正常，
clone / `npm i` / `uv sync` 唔得。要全套功能就自己 build 一個有 git / node / uv 嘅
image，再指 `SANDBOX_IMAGE=rqark-sandbox:latest`，唔使改 code。repo 已經冇 ship
sandbox Dockerfile。

## 沙盒幾時建立

`Workspace(...)` 只係一個物件，唔掂 docker。真正 `docker run` 發生喺**第一次**
`_ensure()`，而 `_ensure()` 由 prompt 同 tool 觸發：

| 觸發點 | 幾時 |
|---|---|
| `ChatHelper.chat()` 開頭 `await self.workspace.ensure()` | build agent 之前，喺 worker thread 起容器（cold start / image pull 都喺呢度） |
| `AgentsMdPrompt` / `GitProjectPrompt` 砌 system prompt | 落沙盒讀檔、跑 git；冇 pre-warm 就會喺呢度同步起容器 |
| 第一個 sandbox tool call | `exec` / `read_file` 之類 |

`swap_agent()` 本身唔會起容器，佢只係換 object。

所以 `ChatHelper.chat()` 開頭嗰個 `await self.workspace.ensure()` 就係 pre-warm：
cold start 由「同步塞住 event loop 幾百 ms 至幾十秒」變成 worker thread 內進行，
loop stall 由 ~220ms 降到 ~12ms（等於 baseline）。

仍然存在：容器熱咗之後，`get_messages()` 仲係同步，5 次 `run_shell` 會 stall ~72ms。
要再修就要 cache prompt 數據（branch / status / log 喺 async 路徑預先攞好再注入），
或者上游令 `BasePrompt.get_messages()` 可以係 async。

## 未完成

### 冇嘢會停容器

`ChatHelper` / `SessionRunner` 冇 call `workspace.stop()`，retire 只係閂 HTTP client。
即係容器一旦起咗就長生，直到 host reboot 或者手動 `docker rm`。
Plan：reaper 掃 `rqark.sandbox=1` label 停 idle 容器（volume 要保留）。

### Sandbox image

`SANDBOX_IMAGE` 預設 `ubuntu:24.04`。repo 已經冇 ship sandbox Dockerfile，要全套功能
就自己 build 一個再將 `SANDBOX_IMAGE` 指過去。`ubuntu:24.04` 冇 git / node / uv，clone 會回
`Clone failed: git command not found in the sandbox image`（exit 127 已 map）。

如果用 apt 就要 `cap_add=['DAC_OVERRIDE','SETUID','SETGID']`（實測：單加
`DAC_OVERRIDE` 或 `SETUID+SETGID` 都唔夠）。正路係裝喺 image 度，唔好 runtime 裝。

### Token 會寫入沙盒

core 嘅 `_clone_url()` 產生 `https://x-access-token:<token>@host/repo.git`，clone 完
`.git/config` 有明文 token，沙盒內任何程式（或 prompt injection）都讀得到再帶出街。

- 短期：clone 完 `git remote set-url origin <無 token 版>`。
- 長期：credential helper proxy。
- 未拍板。

### 其他

- **Egress**：每個 user 一條 bridge，可以出街（`npm i` / `uv sync` 行到），
  但內網 DNS 解唔到、host gateway 嘅 redis / data-cloud 連唔到（已驗）。
  要更嚴就要 egress proxy 做 allowlist。
- **Docker socket**：`agent-cloud` 直掛 `/var/run/docker.sock` = host root。
  要收窄就加 `tecnativa/docker-socket-proxy`。
- **孤兒清理**：container / network / volume 都有 `rqark.sandbox=1` label。
  未寫 reaper 去停 idle container（volume 要永遠保留）。
- **`_locks` dict**：`src/apps/sandbox/workspace.py`，每個 user 加一條就唔清，長期會 leak。
- **`--workers 4`**：`_runners` / `_locks` 係 per-process，同一 session 有機會被兩個
  worker 各起一個 runner。docker 係唯一真相所以唔會壞，但值得知。

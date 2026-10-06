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

## 未完成

### Sandbox image

`SANDBOX_IMAGE` 預設 `rqark-sandbox:latest`（`docker/Dockerfile.sandbox` 已寫，未 build）。
`ubuntu:24.04` 冇 git / node / uv，clone 會回 `Clone failed: git command not found in the
sandbox image`（exit 127 已 map）。

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
- **舊 volume**：`rqark-user-ansoncar-test`（舊命名，唔會再被 reuse）。

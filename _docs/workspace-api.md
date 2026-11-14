# Workspace API

User 透過 HTTP 瀏覽 / 操作自己 agent 沙盒嘅 `/workspace`。
實作：`src/apps/workspace/routes.py`（HTTP）、`src/apps/sandbox/workspace.py`（docker primitive）。

## Auth

全部 route 都要 `Authorization: Bearer <data-cloud token>`，`user_id` 由 `users/me` 派，
唔接受 request 傳入。冇 id → 401。

## 路徑規則

- `path` 一律係**相對**工作目錄，例如 `docs/a.txt`；省略 = 工作目錄本身。
  工作目錄預設係 `/workspace`，帶 `project_id` 時係該 project 嘅目錄。
- 拒收：absolute path（`/etc`）、任何 `..` segment、null byte → `400 INVALID_PATH`。
- Server 會喺容器內 `realpath -e` resolve，確認結果仍然 under `/workspace`；
  symlink 指去容器其他地方（例如 `ln -s /etc escape`）→ `400 INVALID_PATH`。
- `delete` / `move` 唔跟隨最後一段 symlink（`rm` / `mv` 本身都唔跟），所以可以刪走上面嗰條 `escape`。
- 回傳嘅 `path` 一律相對於工作目錄：project session 上 `list?path=` 列出 project 根目錄，
  上唔到 project 以外。

## Project

每個 endpoint 都收 `project_id` query（data-cloud 嘅 project uuid）：

- 帶 `project_id`：工作目錄收窄到該 project，路徑相對於 project 根目錄。
- 唔帶：工作目錄仍然係 `/workspace`，即整個 volume，同以前一樣。

`project_id` 對應唔到 caller 嘅 project 時，data-cloud 回 `404`，agent-cloud 原樣轉回。

## Endpoints

| Method | Path | 說明 |
|---|---|---|
| GET | `/api/workspace/list?path=&show_hidden=false` | `{path, entries[]}`；預設隱藏 dotfiles |
| GET | `/api/workspace/stat?path=` | 單一 entry metadata |
| GET | `/api/workspace/complete?query=&limit=50` | `@` picker completion; `{entries[]}` |
| GET | `/api/workspace/file?path=` | 下載；`Content-Type` 由副檔名推，`Content-Length` 有齊 |
| GET | `/api/workspace/archive?path=` | 目錄 / 檔案打包 zip（stream） |
| POST | `/api/workspace/upload?path=<dir>&overwrite=true` | multipart，field name **`files`**，可多檔 |
| PUT | `/api/workspace/file?path=&overwrite=true` | raw body 覆寫 / 新建（text 或 binary） |
| POST | `/api/workspace/mkdir?path=` | `mkdir -p` |
| PATCH | `/api/workspace/file?path=&overwrite=false` | body `{"new_path": "..."}`，rename / move |
| DELETE | `/api/workspace?path=` | 檔案或目錄（遞迴） |

Entry shape：

```json
{"name": "a.txt", "path": "docs/a.txt", "type": "file", "size": 10, "mtime": 1792230499.0, "target": null}
```

`type` = `file` / `dir` / `link` / `other`；`target` 只有 symlink 有值。

## Completion

`/complete` mirrors the terminal client's `@` behaviour. The query splits at its last
slash: the part before it picks the directory, the rest is a case-insensitive basename
prefix. Search stops at depth 8 and skips `.git`, `node_modules`, `dist`, `build` and the
other directories the sandbox already ignores. A missing directory returns an empty list
instead of an error, because the query is still being typed.

## 限制

- `WORKSPACE_MAX_BYTES`（預設 100MB）：單檔下載、upload 每個檔、PUT body、zip 解壓前總大小。
  超過 → `413 FILE_TOO_LARGE`。`archive` 會先用 `find -type f -printf '%s\n'` 計總數，所以係 413 而唔係截斷。
- 敏感檔讀取上限 4MB（要 redact，唔可以 stream）。
- Upload / PUT 先落 host temp file（`SpooledTemporaryFile`，8MB 後落 disk），再 stage 去
  `/workspace/.rqark-tmp/<uuid>/`，最後 `mv` 去目標 = 同 filesystem atomic rename。
- 預設 hide dotfiles（`show_hidden=true` 可以睇）；`.rqark-tmp` 正常情況下即刻清走。
- 敏感檔（`.env*`、`.git/config`、`.npmrc`、`.netrc`、`.git-credentials`、`.pypirc`、
  `id_rsa*`、`id_ed25519*`、`credentials`）GET 時會 redact URL 內嘅 credential
  （`https://user:token@` → `https://***@`）同 `token|secret|password|api_key|authorization` 行，
  回 `X-Content-Redacted: true`。Archive 唔 redact（zip 係原檔）。
- 寫入防打架：`PUT` 可帶 `If-Unmodified-Since`（HTTP date，用 Entry `mtime`）；
  metadata 比 header 新 → `412 PRECONDITION_FAILED`。`overwrite=false` 而目標存在 → `409 CONFLICT`。

## 冷啟

每個 request 先 `ensure()`（可能 pull image / 起 container）。超過
`WORKSPACE_START_TIMEOUT`（預設 30s）→ `503 SANDBOX_NOT_READY` + `Retry-After: 10`。
Background thread 會繼續起，retry 就會中。

## 並發

Streaming（download / zip）**唔會**揸住 `Workspace._lock`；`_lock` 只用嚟 serialize `_ensure()`，
所以 UI 長 download 唔會塞住 agent tool。但 agent 同 UI 仍然可以同時改同一批檔，
所以 list / stat 回 `mtime` + `size`，寫入用 precondition 減少靜靜哋覆蓋。

## 未做

- Rate limit（每個 request 都 `ensure()`，auth 又打 data-cloud，production 應該喺 gateway 或 Redis 加）。
- ETag / `If-Match`（現時只有 `If-Unmodified-Since`）。
- TOCTOU：`realpath` 檢查同實際 op 之間 symlink 可以掉包；單一 user 嘅容器，escape 只去到 image 檔案，未上 openat2。
- Zip 中途出錯（client 已收 header）只會 truncate，冇得轉 status code。
- Resumable upload / range download。

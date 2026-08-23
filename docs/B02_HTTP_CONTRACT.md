# B02 local HTTP contract

状态：B02 本地契约实现；不等同于原版产品复刻完成，也不宣称原版视觉已复现。

## 范围与证据

本批以本机脱敏 `PROTOCOL.md` 的 HTTP 清单为调用契约证据，保留既有 `/toy/*` 路径和主要字段。原版页面的实际打开、截图和逐帧对照没有在本批完成，因此信箱、回信、阅读、音乐页面的视觉状态保持 `UNVERIFIED`；页面无音频，听觉证据为 `N/A`。

B01 的私有 manifest/state matrix 只允许存在于 ignored `.evidence/`。本批登记文件为 `.evidence/b02/run/visual-ui-baseline.json`，只记录状态、计数和证据边界，不写入源路径、真实 logical ID、原文、媒体或用户资料。

## 版本与 envelope

- `contract_version`: `b02.v1`
- `schema_version`: `1`
- 正常响应：`{"code":0,"message":"ok","data":{...}}`
- 错误响应：`{"code":<HTTP 状态>,"message":"<error_code>","data":{"status":"FAILED","error_code":"<error_code>"}}`
- 真正未实现能力：HTTP `501`，`data.status=NOT_IMPLEMENTED`。
- 已知但不可用的可选能力：HTTP `501`，`data.status=UNAVAILABLE`，并带 `capability`；不会返回 200 假成功。
- HTTP 发信先返回 `200`/`PENDING`；LLM 超时或不可用随后写入信件 `FAILED` 终态，detail 返回稳定 `error_code` 与 `retryable`。重启只恢复尚未派发的 `PENDING`；不确定是否已到达 provider 的 `PROCESSING` 会 fail closed 为 `LLM_INTERRUPTED`，不自动重复生成。

机器可读定义：

- `http_contract.py`：运行时 route/capability registry 和 envelope helpers。
- `contracts/http_contract.schema.json`：版本化 schema。
- `contracts/http_contract.example.json`：脱敏最小示例。

## 路由状态

| 路由组 | 路径 | 状态 | 说明 |
|---|---|---|---|
| core | `/health?profile=core` | available | 进程内 core 健康检查；不探测外部 provider |
| session | `/toy/signIn`, `/toy/getUserInfo` | available | local-memory/session fixture |
| 信件只读 | `/toy/letter/list`, `/toy/letter/detail`, `/toy/letter/unread_count` | available | `scope=current` 默认；`scope=legacy` 只读隔离视图 |
| 发信 | `/toy/letter/send` | available/degraded | 先确认 `PENDING`，后台调用 LLM adapter；失败写入 detail，不生成占位回信 |
| 回信重发/分享 | `/toy/letter/resend`, `/toy/letter/share` | unavailable | 501 稳定错误；未实现不伪造写入 |
| 音乐目录 | `/toy/getMusicTypeInfo`, `/toy/searchSongs`, `/toy/searchPlaylist`, `/toy/searchUserSongs`, `/toy/searchPerformances`, `/toy/getSongStats` | available | 只返回脱敏 fixture 或显式空数据 |
| 音乐写操作 | `/toy/addPerformance` 等 | unavailable | 501 `MUSIC_WRITE_NOT_IMPLEMENTED` |
| MIDI | `/toy/midi/*` | terminal/partial | 任务状态兼容现有原型；生成/上传/分享码导入明确 501 |
| legacy import | `/toy/letter/legacy/import` | available | SQLite 本地扩展；仅接受 `mode=read_only`，以单事务原子导入旧信并按内容哈希去重；导入后旧信域只读且不与新聊天合并 |

原生 WebSocket、ASR、TTS、Live 没有假 route；`/health` 的 capability registry 明确为 `unavailable`，错误码分别为 `WEBSOCKET_UNAVAILABLE`、`ASR_UNAVAILABLE`、`TTS_UNAVAILABLE`、`LIVE_UNAVAILABLE`。

## 输入、空数据和重试

- 缺少 `letter_id`/`content`：400 `MISSING_FIELD`。
- `content` 为空、`material` 非 object、JSON 非法或请求体非 object：400 稳定错误。
- legacy import 的 body、`mode` 或 `letters` 不合法：400 `INVALID_BODY`；SQLite 存储不可用：503 `MEMORY_UNAVAILABLE`。两类错误都不回显旧信正文、路径或密钥。
- 找不到信件或 MIDI job：404，不返回空的成功对象。
- 空信箱、无匹配歌曲、空 playlist：200，但 `source=local-memory|empty`、列表和计数明确为空。
- LLM timeout/error：发信确认保持 200/PENDING，detail 随后标记 `FAILED` 并带错误码；不得写入 `reply_text` 占位符。
- 相同正文和素材在短窗口内失败后重试成功时，旧 FAILED 记录保留在本地审计状态，但从 current list 隐藏；直接查询旧 ID 返回 410 `LETTER_SUPERSEDED` 和替代信件 ID，不回显旧正文。
- `/letter/resend` 当前未实现；重复请求返回相同 501，不改变信件内容或状态。
- `scope=legacy` 的 list/detail 只读，detail 不改变 `is_read`；send 对 legacy scope 返回 403 `READ_ONLY_SCOPE`。

## 隐私边界

请求 body、query value、回复正文、真实 token、源路径、原版资产和真实信件不进入结构化日志、提交 fixture、schema、健康检查或文档。legacy import 响应只返回导入/重复计数和只读范围，不回显正文或密钥；运行时响应只在客户端请求 detail 时返回对应内容；B02 测试使用 synthetic/fixture 文本。

## 命令

```text
rtk proxy pytest -q tests/http/test_contract.py
rtk python tools/healthcheck.py --profile core
rtk python tools/verify_b02_scope.py
rtk python -m compileall -q local_server.py http_contract.py tools tests/http
```

`rtk proxy pytest` 仅用于绕过当前 RTK 对 pytest 目录参数的过滤；实际测试仍是本地 pytest，无网络和官方服务依赖。

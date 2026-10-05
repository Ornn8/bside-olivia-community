# B02 local HTTP contract

状态：B02 本地契约实现；不等同于原版产品复刻完成，也不宣称原版视觉已复现。

## 范围与证据

本批以本机脱敏 `PROTOCOL.md` 的 HTTP 清单为调用契约证据，保留既有 `/toy/*` 路径和主要字段。原版页面的实际打开、截图和逐帧对照没有在本批完成，因此信箱、回信、阅读、音乐页面的视觉状态保持 `UNVERIFIED`；页面无音频，听觉证据为 `N/A`。

B01 的私有 manifest/state matrix 只允许存在于 ignored `.evidence/`。本批登记文件为 `.evidence/b02/run/visual-ui-baseline.json`，只记录状态、计数和证据边界，不写入源路径、真实 logical ID、原文、媒体或用户资料。

## 版本与 envelope

### 本地信件与聊天备份

`POST /toy/letter/backup/export` 导出本机已保存的信件以及 QQ／微信文字聊天，保持 `schema_version=olivia.letters.v1`、`letters` 数组和 `Olivia-letters-*.json` 文件名兼容。聊天项增加可选 `channel`（`qq` 或 `wechat`）与 `delivery_status`（`DELIVERED` 或 `RECEIVED_ONLY`）；没有 `channel` 的旧项仍按信件解释。

聊天回复仅在确认送达后导出；已收到用户文字但回复未送达时，保留用户文字并以 `RECEIVED_ONLY`、空 `reply_text` 导出，不备份未发出的回复草稿。已送达的主动聊天回复可以没有用户文字。仅备份文字、时间和允许的记录类型字段，不包含图片、音视频附件、账号凭据、内部路径或待发送任务；导出不调用模型、不消耗模型额度。

`POST /toy/letter/backup/import` 继续使用已有用户确认和导入互斥门禁。`backup` 字段支持原有 JSON 字符串/信件与聊天备份对象，以及 `{ "format": "soul", "manifest": { "memory": { "exchanges": [...] } } }`（见 `contracts/soul_letter_import.schema.json`）。QQ／微信记录必须包含在 Olivia JSON 备份中；本入口不从 QQ／微信服务器拉取历史。

设置页在本地读取 `.soul` 的 `SOUL0001` 文件头、8 字节小端清单长度和 JSON 清单，只提交 `incoming`、`reply`、`date`、`time`；不读取或提交尾部媒体及媒体地址。清单上限 16 MiB，服务端另限制最多 10000 封并在事务前验证全部记录。日期按北京时间解释，无法解析时保留未知时间；信箱输出时间为 epoch 秒，相同时间按源文件顺序显示。双方原文相同（忽略空白差异）的已有信件跳过，不自动覆盖或修复已有记录。

返回原有 `APPLIED/inserted/duplicates` 结果，计数包含信件与聊天记录，格式错误沿用 `LETTER_BACKUP_INVALID`。导入内容只写入只读历史 Archive，保留聊天频道与送达状态，不重新发送旧消息、不恢复待发送任务。保存原文不调用模型；成功后沿用历史关系评估流程，每五组成对往返记录评估一次，只有单方文字的记录不进入关系评估，界面确认文案明确提示模型用量。

- 运行时 HTTP envelope：`contract_version=b02.v1`、`schema_version=1`，保持原客户端兼容。
- 机器可读 contract document：`contract_version=b02.v2`、`schema_version=2`；v2 新增必填 `letter_detail_generation`，因此不冒充 v1 schema。
- core health 的 `backend_id` 是组件版本与安装实例摘要组成的不透明标识；不包含安装路径、用户名或密钥。启动器只复用与当前安装实例和活动组件同时匹配的本机后端。
- 正常响应：`{"code":0,"message":"ok","data":{...}}`
- 错误响应：`{"code":<HTTP 状态>,"message":"<error_code>","data":{"status":"FAILED","error_code":"<error_code>"}}`
- 真正未实现能力：HTTP `501`，`data.status=NOT_IMPLEMENTED`。
- 已知但不可用的可选能力：HTTP `501`，`data.status=UNAVAILABLE`，并带 `capability`；不会返回 200 假成功。
- HTTP 发信先返回 `200`/`PENDING`；启用的 Mem0 或 durable outbox 尚未就绪时信件在自收信起最长 120 秒的总 deadline 内保持 `PENDING`，运行时恢复后只派发一次，不会降级为无记忆生成；重启不会重置该 deadline。deadline 到期会在调用 LLM、PrivateWorld、memory 或 media 前写入可重试的 `FAILED/MEMORY_UNAVAILABLE`，释放“一次一封”的 active gate；进程关闭导致的任务取消仍保持 `PENDING`。用户明确选择的 `MEMORY_ADMIN_PAUSED` 是隔离态而非故障：回信继续使用 Archive，不检索或写入 Mem0，canonical letter 与 PrivateWorld 仍按既有合同提交，outbox 将该次 Mem0 delivery terminal-skip。LLM 超时或不可用随后写入信件 `FAILED` 终态，detail 返回稳定 `error_code` 与 `retryable`。重启只恢复尚未派发的 `PENDING`；不确定是否已到达 provider 的 `PROCESSING` 会 fail closed 为 `LLM_INTERRUPTED`，不自动重复生成。
- 视频回信的 detail 额外公开 `media_status`、`media_error_code` 与布尔值 `media_retryable`。路由选中视频后、正文仍在生成时为 `PENDING`；若正文生成、质量检查或重启恢复在媒体启动前失败，则为 `NOT_REQUESTED` 且没有媒体错误；只有正文成功后才进入实际媒体任务。`TTS_CONTENT_GATE_UNAVAILABLE` 为可重试的 `UNAVAILABLE`；三条有效候选均被内容门拒绝时，`TTS_CONTENT_GATE_REJECTED` 为不可重试的 `FAILED`。

机器可读定义：

- `http_contract.py`：运行时 route/capability registry 和 envelope helpers。
- `contracts/http_contract.schema.json`：版本化 schema。
- `contracts/http_contract.example.json`：脱敏最小示例。

### 信箱对比与整理

三个端点均为 POST，要求 `X-Olivia-Companion-Action: confirmed`，使用记忆管理互斥锁。请求上限为 16 MiB + 1024 字节；`backup` 可选，沿用信件文件导入格式。所有操作只在本地完成，无模型调用。

- `/toy/letter/maintenance/preview`：接收 `{backup?, page?: 0}`，返回 `READY/token/counts/total/items/near_limited`。每页 30 项，`items` 包含对比摘要、处理选项 ID，不返回可任意写入的内部计划。近似比较达到上限时 `near_limited=true`，完全重复检查不受影响。
- `/toy/letter/maintenance/detail`：接收 `{backup?, key}`，返回所选信件的完整 `content/reply_text`；不会返回文件路径、其他媒体或凭据。
- `/toy/letter/maintenance/apply`：接收 `{backup?, token, selected: [操作ID]}`。重新核对预览与当前状态，冲突或过期不写入；成功返回 `APPLIED/changed/provider_calls:0`。写入可恢复的信箱覆盖项，不修改原始档案。

错误：`LETTER_MAINTENANCE_STALE`（409，重新预览）、`MEMORY_ADMIN_BUSY`（409）、`LETTER_MAINTENANCE_INVALID`（400）、`LETTER_BACKUP_STORAGE_UNAVAILABLE`（503）。详见 [信件整理说明](LETTER_MAINTENANCE.md)。

## 路由状态

| 路由组 | 路径 | 状态 | 说明 |
|---|---|---|---|
| core | `/health?profile=core` | available | 进程内 core 健康检查；不探测外部 provider |
| session | `/toy/signIn`, `/toy/getUserInfo` | available | local-memory/session fixture |
| 信件只读 | `/toy/letter/list`, `/toy/letter/detail`, `/toy/letter/unread_count` | available | `scope=current` 默认；`scope=legacy` 只读隔离视图 |
| 发信 | `/toy/letter/send` | available/degraded | 先确认 `PENDING`，后台调用 LLM adapter；失败写入 detail，不生成占位回信 |
| 回信重发/分享 | `/toy/letter/resend`, `/toy/letter/share` | unavailable | 501 稳定错误；未实现不伪造写入 |
| 原生信箱兼容别名 | `/letter/list`, `/letter/detail`, `/letter/unread_count`, `/letter/send`, `/letter/resend`, `/letter/share` | 与对应 `/toy/letter/*` 路由一致 | 仅精确路径映射；方法、查询、请求体、延迟回信与错误状态均沿用对应版本化路由，未知 `/letter/*` 前缀不会被映射 |
| 音乐目录 | `/toy/getMusicTypeInfo`, `/toy/searchSongs`, `/toy/searchPlaylist`, `/toy/searchUserSongs`, `/toy/searchPerformances`, `/toy/getSongStats` | available | 只返回脱敏 fixture 或显式空数据 |
| 音乐写操作 | `/toy/addPerformance` 等 | unavailable | 501 `MUSIC_WRITE_NOT_IMPLEMENTED` |
| MIDI | `/toy/midi/*` | terminal/partial | 任务状态兼容现有原型；生成/上传/分享码导入明确 501 |
| legacy import | `/toy/letter/legacy/import` | available | SQLite 本地扩展；仅接受 `mode=read_only`，以单事务原子导入旧信并按内容哈希去重；导入后旧信域只读且不与新聊天合并 |
| 本地历史信件恢复 | `/toy/letter/legacy/local-import` | available/degraded | 设置页只读取安装时选定的原版游戏目录中的 `letter_pairs.json`；用户确认后把成对文字记录作为只读历史原子写入本地信箱。官方服务器已关闭，本入口不读取登录日志、不访问官方接口，也不调用 LLM、Mem0、PrivateWorld 或媒体 |
| 长期记忆重试 | `/toy/companion/memory/retry` | available/degraded | 仅接受带确认头的 `POST`；返回 `INITIALIZING`、`AVAILABLE`、`DEGRADED`、`UNAVAILABLE` 或 `DISABLED`，其中显式禁用的 `DISABLED` 不可重试；重试真实 Mem0 factory，并在 delegate 已就绪时重新启动 durable outbox，不要求退出重进 |
| 诊断包导出 | `/toy/diagnostics/export` | available | 设置页显式触发，只在本机生成并下载严格白名单 ZIP；不上传，不包含密钥、正文、记忆、真实 ID、绝对路径、完整 URL 或原始日志 |

CORS 仅允许 loopback Origin，或精确命中原生客户端固定信任源
`https://olivia.local`、`https://toy-cnbeta01.olivia.miyoushe.com`。匹配是完整字符串匹配；
相似后缀、子域和其他 HTTPS Origin 均返回 `403 CORS_ORIGIN_DENIED`。

原生 WebSocket、ASR、TTS、Live 没有假 route；`/health` 的 capability registry 明确为 `unavailable`，错误码分别为 `WEBSOCKET_UNAVAILABLE`、`ASR_UNAVAILABLE`、`TTS_UNAVAILABLE`、`LIVE_UNAVAILABLE`。

## 输入、空数据和重试

- 缺少 `letter_id`/`content`：400 `MISSING_FIELD`。
- `content` 为空、`material` 非 object、JSON 非法或请求体非 object：400 稳定错误。
- legacy import 的 body、`mode` 或 `letters` 不合法：400 `INVALID_BODY`；SQLite 存储不可用：503 `MEMORY_UNAVAILABLE`。两类错误都不回显旧信正文、路径或密钥。
- 本地历史恢复找不到 `letter_pairs.json` 时返回 404 `OFFLINE_LETTER_BACKUP_REQUIRED`，设置页明确提示官方服务器已关闭并要求准备本地备份；格式不合法时返回 400 `OFFLINE_LETTER_BACKUP_INVALID`。两种情况都不会发起网络请求或写入部分记录。
- 找不到信件或 MIDI job：404，不返回空的成功对象。
- 空信箱、无匹配歌曲、空 playlist：200，但 `source=local-memory|empty`、列表和计数明确为空。
- LLM timeout/error：发信确认保持 200/PENDING，detail 随后标记 `FAILED` 并带错误码；不得写入 `reply_text` 占位符。
- 当前信箱状态损坏或原子写入在 replace 前失败：current list/unread/detail/send 返回 503 `STORE_STATE_UNAVAILABLE`，legacy 只读信箱保持独立；replace 后仅目录同步失败时保留已提交信并继续调度，日志只记录脱敏诊断码。
- 同一时间只接收一封未交付信：上一封仍为 PENDING/PROCESSING，或虽已生成但尚未到 `reply_not_before` 时，不同的新信返回 409 `LETTER_IN_PROGRESS`；相同请求的网络重试仍复用原信，回信可见后允许继续寄信。
- 相同正文和素材在短窗口内失败后重试成功时，旧 FAILED 记录保留在本地审计状态，但从 current list 隐藏；直接查询旧 ID 返回 410 `LETTER_SUPERSEDED` 和替代信件 ID，不回显旧正文。
- `/letter/resend` 当前未实现；重复请求返回相同 501，不改变信件内容或状态。
- `scope=legacy` 的 list/detail 只读，detail 不改变 `is_read`；send 对 legacy scope 返回 403 `READ_ONLY_SCOPE`。

## 历史关系评估的 JEV 请求与恢复

历史关系的五项评分（`familiarity`、`trust`、`comfort`、`closeness`、`tension`）保留 `0` 到 `100` 的选项标签，选项说明使用字符串，符合上游 Choice 请求格式。云端仅为 `historical-relationship` 中这五项评分兼容旧客户端的同值整数说明；兼容转换只作用于上游请求，原始请求摘要和计费身份保持不变。

原文导入与后续关系整理分别持久化。关系评估失败不撤销已保存的信件，也不自动重复调用模型；用户点击“评估历史关系 / 重试”后继续未完成的批次，无需重新导入备份。其他无效请求仍保留失败状态。

模型返回空的关系证据列表是有效的“不更新”判断：该批次保存评估并完成，不应用任何分数或关系阶段，接着处理下一批。重启和旧评估回填也不为这类批次提交关系变更。越界、重复或超过八项的证据仍按无效结果暂停。

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

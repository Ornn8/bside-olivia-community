# B02 error codes

错误 envelope 固定为 `code`、`message`、`data.status`、`data.error_code`；`data` 只携带行动所需的脱敏字段。

| HTTP | error_code | 状态 | 可重试 | 说明 |
|---:|---|---|---|---|
| 400 | `INVALID_JSON` | FAILED | 否 | JSON 无法解析 |
| 400 | `INVALID_BODY` | FAILED | 否 | body 必须为 object；legacy import 的 `mode`/`letters` 也必须符合只读导入契约 |
| 400 | `MISSING_FIELD` | FAILED | 否 | 缺少契约必填字段 |
| 400 | `INVALID_FIELD_TYPE` | FAILED | 否 | 字段类型不符 |
| 400 | `INVALID_CONTENT` | FAILED | 否 | 内容为空 |
| 400 | `CONTENT_TOO_LONG` | FAILED | 否 | 超出 10000 字符边界 |
| 400 | `INVALID_SCOPE` / `INVALID_PROFILE` | FAILED | 否 | 未知只读 scope 或 health profile |
| 403 | `CORS_ORIGIN_DENIED` | FAILED | 否 | 非 localhost/127.0.0.1 Origin |
| 403 | `READ_ONLY_SCOPE` | FAILED | 否 | 对 legacy 只读视图尝试写入 |
| 404 | `LETTER_NOT_FOUND` / `MIDI_JOB_NOT_FOUND` | FAILED | 否 | 资源不存在 |
| 405 | `METHOD_NOT_ALLOWED` | FAILED | 否 | 方法不在 route contract 中 |
| 501 | `LETTER_RESEND_NOT_IMPLEMENTED` | NOT_IMPLEMENTED | 否 | 回信重发未实现 |
| 501 | `LETTER_SHARE_NOT_IMPLEMENTED` | NOT_IMPLEMENTED | 否 | 分享未实现 |
| 501 | `MUSIC_WRITE_NOT_IMPLEMENTED` | NOT_IMPLEMENTED | 否 | 音乐写操作未实现 |
| 501 | `MIDI_*_NOT_IMPLEMENTED` | NOT_IMPLEMENTED | 否 | MIDI 上传/生成/导入未实现 |
| 501 | `WEBSOCKET_UNAVAILABLE` / `ASR_UNAVAILABLE` / `TTS_UNAVAILABLE` / `LIVE_UNAVAILABLE` | UNAVAILABLE | 否 | 原生实时能力不可用 |
| 501 | `ROUTE_NOT_IMPLEMENTED` | NOT_IMPLEMENTED | 否 | 未登记 route |
| 200 detail | `LLM_TIMEOUT` / `LLM_UNAVAILABLE` | FAILED | 是 | 发信先确认 PENDING；provider 超时/不可用后 detail 显示 FAILED |
| 200 detail | `LLM_INTERRUPTED` | FAILED | 是 | 进程在 provider 调用终态落盘前中断；不自动重复生成 |
| 503 | `MEMORY_UNAVAILABLE` | UNAVAILABLE | 是 | legacy import 的 SQLite 存储不可用；不回显正文、路径或密钥 |
| 400 | `INVALID_IDEMPOTENCY_KEY` | FAILED | 否 | 幂等键为空、类型错误或超过长度边界 |
| 409 | `IDEMPOTENCY_CONFLICT` | FAILED | 否 | 同一幂等键重复提交了不同正文 |
| 400 | `VIDEO_REPLY_SETTING_REQUEST_ID_INVALID` / `VIDEO_REPLY_SETTING_PAYLOAD_INVALID` | FAILED | 否 | 视频回信设置 request_id 必须为 `video_reply_setting:<opaque>`；enabled 必须为布尔值 |
| 400 | `MEMORY_CLEAR_CONFIRMATION_REQUIRED` | FAILED | 否 | 原版客户端清空当前用户 Mem0 长期记忆时缺少 body 二次确认；不执行删除 |
| 409 | `VIDEO_REPLY_SETTING_REQUEST_CONFLICT` | FAILED | 否 | 同一视频设置 request_id 重放了不同 enabled |
| 503 | `VIDEO_REPLY_SETTING_UNAVAILABLE` | UNAVAILABLE | 是 | 设置 state root、持久化读取或原子写入不可用；服务端 fail-closed |
| 410 | `LETTER_SUPERSEDED` | SUPERSEDED | 否 | 失败副本已由成功重试替代；仅返回替代信件 ID |
| 200 detail | `LLM_PROVIDER_REJECTED` / `LLM_PROTOCOL_ERROR` | FAILED | 否 | 上游非重试 4xx、坏 JSON 或空响应；不回显 provider body |

## P03 clear mutation registry

| HTTP | error_code | 状态 | 可重试 | 说明 |
|---:|---|---|---|---|
| 400 | `COMPANION_JSON_INVALID` | FAILED | 否 | clear JSON 无效 |
| 400 | `COMPANION_FIELDS_INVALID` | FAILED | 否 | clear 字段集合无效 |
| 400 | `COMPANION_REQUEST_ID_INVALID` | FAILED | 否 | clear request_id 无效 |
| 400 | `COMPANION_REASON_INVALID` | FAILED | 否 | clear reason 无效 |
| 400 | `MEMORY_CLEAR_CONFIRMATION_REQUIRED` | FAILED | 否 | 缺少 body 二次确认 |
| 403 | `COMPANION_HOST_FORBIDDEN` | FAILED | 否 | 非 loopback host |
| 403 | `COMPANION_ORIGIN_FORBIDDEN` | FAILED | 否 | origin 未授权 |
| 403 | `COMPANION_CONFIRMATION_REQUIRED` | FAILED | 否 | 缺少确认 header |
| 413 | `COMPANION_REQUEST_TOO_LARGE` | FAILED | 否 | body 超限 |
| 415 | `COMPANION_CONTENT_TYPE_INVALID` | FAILED | 否 | content type 无效 |
| 409 | `MEMORY_ADMIN_REQUEST_CONFLICT` | FAILED | 否 | request payload 冲突 |
| 503 | `COMPANION_MUTATION_UNAVAILABLE` | UNAVAILABLE | 是 | transport/backend 不可用 |
| 503 | `COMPANION_MUTATION_INVALID` | UNAVAILABLE | 是 | backend result 无效 |
| 503 | `MEMORY_MUTATION_DISABLED` | UNAVAILABLE | 是 | memory mutation 未装配 |
| 503 | `MEMORY_ADMIN_DISABLED` | UNAVAILABLE | 是 | Mem0 禁用 |
| 503 | `MEMORY_ADMIN_UNAVAILABLE` | UNAVAILABLE | 是 | Mem0/Qdrant 不可用 |
| 503 | `MEMORY_ADMIN_READ_FAILED` | UNAVAILABLE | 是 | Mem0 读取失败 |
| 503 | `MEMORY_ADMIN_CLEAR_FAILED` | UNAVAILABLE | 是 | 精确删除或复读失败 |
| 503 | `MEMORY_ADMIN_AUDIT_UNAVAILABLE` | UNAVAILABLE | 是 | 审计不可用 |
| 503 | `MEMORY_ADMIN_AUDIT_INVALID` | UNAVAILABLE | 否 | 审计 intent 无效 |

源实现和机器可读映射在 `http_contract.py`；原版客户端 companion mutation 的映射在
`original_client_companion_mutation_api.py` 并由
`contracts/original_client_companion_mutation_contract.json` 固定。新增错误码必须同时更新映射、schema、测试和本表。

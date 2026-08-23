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
| 503 | `MEMORY_UNAVAILABLE` | UNAVAILABLE | 是 | legacy import 的 SQLite 存储不可用；不回显正文、路径或密钥 |
| 400 | `INVALID_IDEMPOTENCY_KEY` | FAILED | 否 | 幂等键为空、类型错误或超过长度边界 |
| 409 | `IDEMPOTENCY_CONFLICT` | FAILED | 否 | 同一幂等键重复提交了不同正文 |
| 200 detail | `LLM_PROVIDER_REJECTED` / `LLM_PROTOCOL_ERROR` | FAILED | 否 | 上游非重试 4xx、坏 JSON 或空响应；不回显 provider body |

源实现和机器可读映射在 `http_contract.py`；新增错误码必须同时更新映射、schema、测试和本表。

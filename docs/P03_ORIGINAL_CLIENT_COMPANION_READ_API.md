# P03 原版设置页陪伴读取与视频开关接口

## 1. 目标

为原版 Olivia `/settings` 中的“本地陪伴”入口提供最小只读数据，不创建独立管理页面。

接口挂载在已有本机 aiohttp 服务：

```text
GET /toy/companion/status
GET /toy/companion/memory
GET /toy/companion/private-world
GET /toy/companion/private-world/candidates
POST /toy/companion/settings/video-reply
```

## 2. 职责边界

`original_client_companion_api.py` 只负责：

- HTTP 和 CORS；
- Host / Origin 验证；
- 查询参数与数量限制；
- 稳定响应 schema；
- 输出字段白名单；
- 后端故障脱敏。

它不负责：

- 记忆提取或检索算法；
- 直接读取 Qdrant；
- 直接读取或写入 SQLite；
- PrivateWorld Reducer；
- 候选批准或拒绝；
- 任何用户数据修改。

后续运行时 Adapter 必须复用现有 `ConversationMemoryAdminService`、PrivateWorld Ledger / Service 和 Candidate Store。

## 3. 访问边界

请求必须同时满足：

```text
Host: 127.0.0.1 / localhost / ::1
Origin: 原版 Olivia 前端 Origin
        或显式 loopback 开发 Origin
```

原版前端 Origin 由生产运行时在挂载接口时注入，传输模块不硬编码远端主机，也不会主动访问该 Origin。注入值必须是无凭据、无路径、无查询参数的 HTTPS Origin，最多允许 8 个。

缺少 Origin、外部 Origin 或非 loopback Host 均返回 `403`。成功响应只对通过验证的 Origin 设置 `Access-Control-Allow-Origin`。

所有响应：

- `Cache-Control: no-store`；
- `X-Content-Type-Options: nosniff`；
- 不包含凭据、绝对路径、数据库名称、原始异常或完整审计 payload。

## 4. 状态接口

```json
{
  "schema_version": "p03.original-companion-read.v1",
  "status": "READY",
  "capabilities": {
    "memory": {"state": "available", "count": 0},
    "private_world": {"state": "available"},
    "candidates": {"state": "available", "count": 0},
    "video_reply": {"enabled": true, "default_enabled": true}
  }
}
```

能力状态只允许：

```text
available
degraded
unavailable
disabled
```

未配置或故障不能伪造成 `available`。
当持久化根缺失、损坏或不可写时，`video_reply` 改为
`{"state":"unavailable","reason_code":"VIDEO_REPLY_SETTINGS_UNAVAILABLE"}`；
这不等同于合法旧 store 中缺少 `video_reply_enabled`（后者默认开启）。

## 5. 长期记忆读取

支持：

```text
query：可选，最多 500 字符
limit：1–100
```

每条只返回：

```text
memory_id
text
source_id
created_at
score（可选）
```

不返回向量、provider 原始响应、metadata 全量、用户 ID、系统提示词或 PrivateWorld 数据。

## 6. PrivateWorld 读取

只返回 Control View 的受限摘要：

- snapshot version；
- relationship stage；
- familiarity / trust / comfort / closeness / tension 的 `unknown / low / medium / high`；
- 已授权私人称呼；
- 住所权限；
- Local Continuation statement 与 awareness。

不返回 0–100 隐藏分数、数据库路径、command payload、拒绝审计或管理凭据。

## 7. 候选读取

候选类型只允许：

```text
boundary_respected
conflict
repair
```

每条只返回候选 ID、类型、简短说明、创建时间和可选过期时间。批准和拒绝属于后续写接口，不能由本 PR 的 GET 路径触发。

## 8. 视频回信开关 mutation

`POST /toy/companion/settings/video-reply` 使用同一 `p03.original-companion-mutation.v1` schema，必须带 loopback Origin、`X-Olivia-Companion-Action: confirmed`，且请求体只允许：

```json
{"enabled": false, "request_id": "video-toggle-2026-01", "reason": "user preference"}
```

成功响应包含 `status`（`APPLIED`、`NOOP` 或 `DUPLICATE`）、`request_id` 和 `affected_count`。同一 `request_id` 与相同 payload 重放原始结果；payload 不同稳定返回 `409 VIDEO_REPLY_REQUEST_CONFLICT`，不改变设置。稳定错误包括 `VIDEO_REPLY_ENABLED_INVALID`、`VIDEO_REPLY_REQUEST_CONFLICT`、`VIDEO_REPLY_REPLAY_INVALID`、`VIDEO_REPLY_SETTINGS_INVALID`、`VIDEO_REPLY_SETTINGS_UNAVAILABLE` 和 `COMPANION_REQUEST_ID_INVALID`。

开关只冻结后续新信的资格。信件进入服务端接收边界时记录资格；关闭时该信只能生成文字且不进入媒体队列，开启时继续原有路由，之后切换不取消已接收信的媒体任务。默认和缺失旧配置均为开启，历史视频不删除。

## 9. 后续顺序

```text
CLIENT-SETTINGS-03  将现有 Memory / PrivateWorld Service 适配到本接口
CLIENT-SETTINGS-04  在原版设置弹层渲染真实只读数据
CLIENT-SETTINGS-05  增加带明确确认的受控写操作
CLIENT-SETTINGS-06  安装器接线与原版客户端验收
```

读取和 mutation 均由同一原版客户端本机服务挂载；生产设备、真实 provider、renderer 和人工验收仍需单独验证。

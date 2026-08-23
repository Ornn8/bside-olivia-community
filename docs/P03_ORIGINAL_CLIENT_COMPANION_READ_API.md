# P03 原版设置页陪伴只读接口

## 1. 目标

为原版 Olivia `/settings` 中的“本地陪伴”入口提供最小只读数据，不创建独立管理页面。

接口挂载在已有本机 aiohttp 服务：

```text
GET /toy/companion/status
GET /toy/companion/memory
GET /toy/companion/private-world
GET /toy/companion/private-world/candidates
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
    "candidates": {"state": "available", "count": 0}
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

## 8. 后续顺序

```text
CLIENT-SETTINGS-03  将现有 Memory / PrivateWorld Service 适配到本接口
CLIENT-SETTINGS-04  在原版设置弹层渲染真实只读数据
CLIENT-SETTINGS-05  增加带明确确认的受控写操作
CLIENT-SETTINGS-06  安装器接线与原版客户端验收
```

在 CLIENT-SETTINGS-03 前，本模块不会自动挂载到生产服务。

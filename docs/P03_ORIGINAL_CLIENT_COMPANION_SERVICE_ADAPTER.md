# P03 原版设置页陪伴 Service Adapter

## 1. 目标

将仓库中已经存在的长期记忆、PrivateWorld 和候选存储接入原版 Olivia `/settings` 的只读接口，不再实现第二套记忆、关系或存储逻辑。

```text
原版 /settings
  -> original_client_companion_api.py
  -> OriginalClientCompanionServiceBackend
  -> 现有 Service / Ledger / Candidate Store
```

## 2. 复用边界

Adapter 只接受三个可选端口：

- Memory Admin Read Port；
- PrivateWorld Read Port；
- Candidate Read Port。

它不导入或直接操作：

- sqlite3；
- Qdrant；
- Mem0 内部对象；
- PrivateWorld Reducer；
- Command payload；
- 原始模型响应。

长期记忆继续由 `ConversationMemoryAdminService` 管理；PrivateWorld 继续由现有 Ledger / Command Service / Control API 管理；候选继续由 `SQLitePrivateWorldCandidateStore` 管理。

## 3. 状态规则

每个能力独立报告：

```text
available
degraded
unavailable
disabled
```

- 服务未配置：`disabled`；
- 服务状态可读：沿用服务自身状态；
- 单个服务失败：只将该能力标记为 `unavailable`；
- 其他能力不受影响；
- 不以数据库文件存在代替能力可用性。

## 4. 长期记忆映射

只从 `ConversationMemoryRecord` 映射：

```text
memory_id
text
source_id
created_at
score（可选）
```

时间优先使用 provider creation time；缺失时使用 exchange occurrence time。两者都缺失时拒绝该读取，不能伪造时间。

不输出：

- user_id；
- metadata 全量；
- embedding；
- provider 原始对象；
- system prompt；
- PrivateWorld 数据。

## 5. PrivateWorld 映射

Adapter 只接受现有 Control API 的受限 snapshot：

- version；
- relationship stage；
- 五个定性等级；
- nickname permissions；
- home access；
- continuation facts 与 awareness。

即使上游意外附加隐藏分数、路径或调试字段，Adapter 也不会复制这些字段到原版客户端响应。

## 6. 候选映射

只读取：

```text
status = pending
now = timezone-aware current time
```

只输出：

- candidate ID；
- boundary / conflict / repair 类型；
- 简短说明；
- 创建和过期时间。

不输出 confidence、source letter、reply revision 或决策审计。

## 7. 后续顺序

```text
CLIENT-SETTINGS-04  在生产本机服务中装配并挂载只读接口
CLIENT-SETTINGS-05  在原版设置弹层显示真实数据
CLIENT-SETTINGS-06  增加明确确认后的受控写操作
CLIENT-SETTINGS-07  安装器接线与原版客户端验收
```

本 PR 不修改 `local_server.py`，也不启用任何写操作。

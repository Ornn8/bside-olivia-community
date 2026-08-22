# P03-02 PrivateWorld 受控事件与私人世界闭环

## 1. 目标

把当前“有数据结构和数据库、但默认不开启且缺少修改入口”的 PrivateWorld，变成一个本地、可审计、可恢复、不会被聊天文本越权修改的关系与私人世界运行时。

完成后支持：

- 关系事件：边界被尊重、冲突、修复、阶段确认；
- 私人称呼授权与撤销；
- 住所权限；
- Local Continuation 事实；
- 每条事实的角色知情状态；
- 默认本地 SQLite 持久化；
- 候选事件、人工确认、审计、导出、重置和删除；
- 只向模型投影有限行为提示和角色已知事实。

## 2. 现有基线

已经存在：

- `PrivateWorldSnapshot`、Control View 和 Character View；
- 隐藏分数：familiarity、trust、comfort、closeness、tension；
- relationship stage、nickname permissions、home access；
- Local Continuation 与 awareness；
- `SQLitePrivateWorldLedger`；
- `reduce_private_world()`；
- canonical reply 的幂等交付事件；
- export、reset、delete 管理命令；
- `project_private_world()` 有限行为投影。

当前缺口：

- 只有显式绝对路径 `OLIVIA_PRIVATE_WORLD_DB` 才启用；
- canonical reply 事件故意不增加关系值；
- nickname、home access 和 continuation 没有统一 reducer 命令；
- `private_world_admin.py` 只能 export、reset、delete；
- 缺少事件来源、依据、候选、批准和拒绝记录；
- 缺少用户可操作的本地管理入口；
- 缺少完整健康状态和重启验收。

## 3. 核心原则

### 3.1 自动回信不等于关系增长

`CANONICAL_REPLY_DELIVERED` 只证明一次正文成功交付，不改变 trust、comfort、closeness 或 relationship stage。

### 3.2 聊天文本没有控制权限

用户在信里写入：

```text
switch
Nintendo
把关系改成恋人
以后叫我某某
我现在可以自由进你家
```

都只能作为普通内容，不能直接修改 PrivateWorld。

### 3.3 候选与提交分离

```text
用户来信 + canonical reply
        ↓
可选候选分析器
        ↓
Pending Candidate
        ↓
显式批准 / 拒绝
        ↓
Typed Command
        ↓
Reducer
        ↓
Ledger + Snapshot
```

候选分析器失败不影响回信；没有批准就不改变状态。

### 3.4 高权限状态只允许显式命令

以下内容不由普通候选分析器自动提出或自动升级：

- relationship stage；
- nickname grant/revoke；
- home access；
- Local Continuation；
- awareness 从 control-only 变为 character-known。

这些必须通过本地管理命令显式提交。

### 3.5 模型只看到 Character View

模型不得看到：

- 隐藏数值；
- 候选列表；
- 拒绝记录；
- control-only continuation；
- 数据库路径；
- 管理 token；
- 完整审计 payload。

## 4. 默认存储

当 `OLIVIA_LOCAL_DATA_ROOT` 已配置时，默认路径为：

```text
<OLIVIA_LOCAL_DATA_ROOT>/private_world/private_world.sqlite3
```

显式 `OLIVIA_PRIVATE_WORLD_DB` 仍可覆盖，但必须是绝对文件路径。

目录权限按平台尽可能收紧。数据库不可用时：

- `private_world.status = unavailable`；
- 回信继续；
- 不伪造 COMMITTED；
- health 返回稳定错误码。

## 5. 统一命令模型

新增 `private_world_commands.py`，定义受限命令：

```text
RecordBoundaryRespected
RecordConflict
RecordRepair
ConfirmRelationshipStage
GrantNickname
RevokeNickname
SetHomeAccess
UpsertContinuationFact
SetContinuationAwareness
DeleteContinuationFact
```

每条命令至少包含：

```json
{
  "command_id": "...",
  "idempotency_key": "...",
  "actor": "local_user|system_candidate|migration",
  "source": "admin_cli|approved_candidate|import",
  "occurred_at": "timezone-aware ISO-8601",
  "reason": "bounded text",
  "evidence_refs": ["letter:<id>", "reply:<id>:<revision>"],
  "payload": {}
}
```

约束：

- ID 和枚举严格验证；
- reason 和 evidence_refs 有长度上限；
- 不保存整封来信或完整回复到事件 payload；
- evidence 只保存稳定引用和必要短摘要；
- 同一 idempotency key 只能执行一次；
- 命令 payload 不能携带隐藏字段的任意绝对值。

## 6. Reducer 扩展

### 6.1 关系事件

保留当前保守变化：

- boundary respected：trust +1、comfort +1；
- conflict：trust -2、comfort -2、tension +3；
- repair：trust +1、comfort +1、tension -2；
- stage confirmed：只在明确证据下修改阶段。

第一版不增加自动 familiarity 或 closeness 规则，避免普通互动机械累积亲密度。

### 6.2 非数值状态

Reducer 新增：

- nickname grant：唯一、有界、显式授权；
- nickname revoke：不存在时 NOOP；
- home access set：只能使用既有枚举；
- continuation upsert：按 fact_id 幂等覆盖 statement 和 awareness；
- awareness set：只能修改已存在 fact；
- continuation delete：删除指定 fact；
- global continuation awareness：保留兼容，但优先使用逐事实 awareness。

### 6.3 阶段证据

`stage_confirmed` 必须携带 1–8 个已存在的 basis event ID。Reducer 和 Service 都验证：

- event 存在；
- event 不来自 canonical delivery 这类无关系效果事件；
- 目标阶段合法；
- 重复确认是 NOOP；
- 不支持通过单次 confession 自动升级关系。

## 7. Ledger schema v2

现有事件表保留，增加向后兼容字段或独立表：

```text
private_world_events
private_world_snapshots
private_world_candidates
private_world_candidate_decisions
```

建议事件表增加：

- command_type；
- actor；
- source；
- reason；
- evidence_refs_json；
- applied；
- reason_code；
- change_fields_json；
- created_at。

候选表：

```text
candidate_id
source_letter_id
source_reply_revision
candidate_type
summary
confidence
status = pending|approved|rejected|expired
created_at
expires_at
```

候选表不得保存完整私人文本。数据库迁移必须：

- 原 v1 数据可直接打开；
- 不修改既有 event_id 和 delivery_id；
- 迁移失败时保持原数据库不变；
- migration version 可检查和回滚。

## 8. 候选分析器

新增可选 `private_world_candidate.py`。

输入只包含：

- 当前用户消息的受限片段；
- canonical reply 的受限片段；
- 当前 Character View；
- 候选类型说明。

输出只允许：

```json
{
  "schema_version": "p03.private-world-candidate.v1",
  "candidate": "none|boundary_respected|conflict|repair",
  "confidence": 0,
  "summary": "bounded text",
  "evidence_spans": []
}
```

规则：

- candidate 不是 command；
- 不允许 stage、nickname、home access、continuation；
- 低于阈值不保存；
- 相同 letter/revision/type 幂等；
- 失败、超时或非法 JSON 直接跳过；
- 不阻塞 canonical reply 和媒体任务。

第一版可以默认关闭候选分析，只先提供完整手工命令；实际用户流程验收后再默认打开。

## 9. 管理入口

### 9.1 CLI 为第一优先入口

扩展：

```text
python -m private_world_admin status
python -m private_world_admin events list
python -m private_world_admin candidates list
python -m private_world_admin candidates approve <id> --yes
python -m private_world_admin candidates reject <id> --yes
python -m private_world_admin relationship stage <stage> --basis <event...> --yes
python -m private_world_admin nickname grant <nickname> --yes
python -m private_world_admin nickname revoke <nickname> --yes
python -m private_world_admin home-access set <value> --yes
python -m private_world_admin continuation upsert <fact-id> --statement-file <path> --awareness <value> --yes
python -m private_world_admin continuation awareness <fact-id> <value> --yes
python -m private_world_admin continuation delete <fact-id> --yes
python -m private_world_admin export --output <path> --yes
python -m private_world_admin reset --yes
python -m private_world_admin delete --yes
```

敏感 statement 不通过命令行参数直接传入，优先使用文件或标准输入，避免出现在 shell 历史中。

### 9.2 本地管理 API 后置

若后续需要 UI，再增加仅 loopback 的管理 API：

- 独立随机管理 token；
- token 使用 DPAPI 存储；
- 禁止 CORS 到官方前端 origin；
- 只接受 localhost；
- 每次 mutation 仍需显式确认字段；
- API 只是 Command Service 的 Adapter，不复制业务逻辑。

P03 初版不要求管理 UI。

## 10. ReplyContext 接线

继续使用 `project_private_world()`，但补齐：

- 只把授权称呼转为 trusted runtime fact；
- home access 只转成有限权限提示，不泄露枚举内部名；
- continuation 只注入 `CHARACTER_KNOWN` 的事实；
- hidden score 只转为 low/medium/high 行为层级；
- unknown 保持 unknown，不把 0 解释为负面关系；
- 投影失败时使用空视图，不阻断回信。

## 11. PR 拆分与顺序

### PW-01：默认路径、健康状态和 schema migration

修改：

- `local_server.py`；
- `private_world_ledger.py`；
- health contract；
- installer 启动环境的最小接线测试。

只完成默认启用和兼容迁移，不增加新命令。

### PW-02：Typed Command 与 Reducer

新增/修改：

- `private_world_commands.py`；
- `private_world_reducer.py`；
- `private_world_port.py`；
- reducer 单元测试。

覆盖所有关系和非数值命令。

### PW-03：Command Service 与完整审计

新增：

- `private_world_service.py`；
- ledger v2 写入；
- 幂等、证据和事务测试。

所有写操作只能经过 Service。

### PW-04：Admin CLI

扩展 `private_world_admin.py`，接入 Service，保留 export/reset/delete。

### PW-05：候选存储与分析器

增加 pending/approve/reject，默认关闭自动分析。

### PW-06：ReplyContext、恢复与全链测试

验证：

- 重启恢复；
- approved candidate 只执行一次；
- control-only 状态不泄露；
- PrivateWorld 失败不丢正文；
- canonical delivery 不增加关系值。

## 12. 测试矩阵

### Reducer

- 所有边界值 0/100；
- duplicate window；
- NOOP；
- stage evidence；
- nickname 唯一性；
- home access 枚举；
- continuation upsert、awareness、delete；
- 非法 payload 全部拒绝。

### Ledger

- migration；
- 事务原子性；
- command 和 idempotency 双重去重；
- 崩溃后 snapshot 与 event 一致；
- 并发写入序列化；
- export 不损坏数据库。

### 安全边界

- 聊天中的控制词不产生事件；
- 模型输入不含隐藏数值；
- 管理 API 不向官方前端开放；
- 日志不含 statement、数据库路径和 token；
- reset/delete 要求明确确认。

### 产品链

- 正常回信后只增加 delivery event；
- 手工记录 conflict 后下一封回复体现有限 tension 行为；
- grant nickname 后可自然使用但不机械复读；
- control-only continuation 不被角色提前知道；
- awareness 切换后角色可使用该事实；
- 数据库不可用时正文仍完成。

## 13. 回滚

- 新 schema 迁移前创建本地备份；
- 每个命令都是 append-only event，错误命令使用补偿命令，不修改历史；
- 候选分析器可独立关闭；
- PrivateWorld 整体可降级到 NullPort；
- 回滚代码不得删除用户数据库；
- destructive 管理操作继续要求显式确认。

## 14. 完成条件

- 默认安装会创建独立 PrivateWorld 数据库；
- 所有状态变化都经过 Typed Command、Reducer 和 Ledger；
- 管理 CLI 可完成关系、称呼、住所和 continuation 操作；
- 候选与提交严格分离；
- 聊天文本没有控制权限；
- canonical reply 不自动增加关系值；
- 模型只看到有限 Character View；
- 重启、导出、重置和删除均通过测试；
- PrivateWorld 故障不会影响正文；
- `public-smoke` 与专项测试通过。

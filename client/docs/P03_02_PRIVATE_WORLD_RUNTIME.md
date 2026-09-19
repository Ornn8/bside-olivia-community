# P03-02 PrivateWorld 受控事件与私人世界闭环

## 1. 目标

把当前“有数据结构和数据库、但默认不开启且缺少完整修改入口”的 PrivateWorld，变成一个本地、可审计、可恢复、不会被聊天文本越权修改，并且普通用户能通过图形界面管理的关系与私人世界运行时。

完成后支持：

- 关系事件：边界被尊重、冲突、修复、阶段确认；
- 私人称呼授权与撤销；
- 住所权限；
- Local Continuation 事实；
- 每条事实的角色知情状态；
- 默认本地 SQLite 持久化；
- 候选事件、显式确认、拒绝、审计、导出、重置和删除；
- 在本地 Companion Control Center 中完成全部用户操作；
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
- export、reset、delete 基础管理代码；
- `project_private_world()` 有限行为投影。

当前缺口：

- 只有显式绝对路径 `OLIVIA_PRIVATE_WORLD_DB` 才启用；
- canonical reply 事件故意不增加关系值；
- nickname、home access 和 continuation 没有统一 reducer 命令；
- `private_world_admin.py` 只能 export、reset、delete；
- 缺少事件来源、依据、候选、批准和拒绝记录；
- 没有适合陪伴产品的用户界面；
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
Control Center 中批准 / 拒绝
        ↓
Typed Command
        ↓
Command Service
        ↓
Reducer
        ↓
Ledger + Snapshot
```

候选分析器失败不影响回信；没有用户确认就不改变状态。

### 3.4 高权限状态不自动升级

以下内容不能由普通候选分析器自动提交：

- relationship stage；
- nickname grant/revoke；
- home access；
- Local Continuation；
- awareness 从 control-only 变为 character-known。

这些只能由用户在 Control Center 中明确操作并确认。

### 3.5 模型只看到 Character View

模型不得看到：

- 隐藏数值；
- 候选列表；
- 拒绝记录；
- control-only continuation；
- 数据库路径；
- 管理会话密钥；
- 完整审计 payload。

### 3.6 不把关系做成数值游戏

默认界面不展示可刷取的原始分数，也不允许用户直接输入 trust=100 之类的值。

普通界面只显示：

- 当前关系阶段；
- 低／中／高或未知的定性状态；
- 最近发生并已确认的关系事件；
- 哪些称呼、住所权限和世界事实已授权。

原始分数只允许在本地“高级诊断”中只读查看，不提供任意编辑。

## 4. 默认存储

当 `OLIVIA_LOCAL_DATA_ROOT` 已配置时，默认路径为：

```text
<OLIVIA_LOCAL_DATA_ROOT>/private_world/private_world.sqlite3
```

显式 `OLIVIA_PRIVATE_WORLD_DB` 仍可覆盖，但必须是绝对文件路径。

数据库不可用时：

- `private_world.status = unavailable`；
- 回信继续；
- 不伪造 COMMITTED；
- Control Center 显示明确故障；
- health 返回稳定错误码。

## 5. 统一命令模型

新增 `private_world_commands.py`，定义受限命令：

```text
RecordBoundaryRespected
RecordConflict
RecordRepair
ConfirmRelationshipStage
GrantIntimacy
GrantNickname
RevokeNickname
SetHomeAccess
UpsertContinuationFact
SetContinuationAwareness
DeleteContinuationFact
ApplyHistoricalRelationshipEvidence
```

每条命令至少包含：

```json
{
  "command_id": "...",
  "idempotency_key": "...",
  "actor": "local_user|system_candidate|migration",
  "source": "control_center|approved_candidate|import|migration",
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
- 命令 payload 不能携带隐藏字段任意绝对值；
- 所有 mutation 必须经过 Command Service，UI 不得直接写 SQLite。
- `ApplyHistoricalRelationshipEvidence` 只允许 `migration + import`，payload
  只携带有证据的 familiarity/closeness 评估下限；Service/Reducer 在当前
  canonical snapshot 上做逐轴 `max`，不得由调用方选择 pristine/existing 分支；
- Command Service 的 command lookup 只返回有界执行元数据，不返回命令 payload、
  reason、evidence refs 或 fingerprint，也不产生 event 或 snapshot。

## 6. Reducer 扩展

### 6.1 关系事件

2026-08-29 的 intimacy model 工单在保留显式授权边界的前提下，取代本节早期
“不增加 familiarity / closeness”的第一版限制。当前受控变化为：

- boundary respected：trust +1、comfort +1、familiarity +1；
- conflict：trust -2、comfort -2、tension +3；
- repair：trust +1、comfort +1、tension -2；
- stage confirmed：只在明确证据下修改阶段，并增加 closeness +5、familiarity +3；
- intimacy granted：只允许 `LOCAL_USER + CONTROL_CENTER` 的 `GrantIntimacy`
  命令，追加 grant 并增加 closeness +2。

boundary 与 intimacy grant 的生长受七天窗口和 6 点配额约束；stage confirmation
不消耗该配额。`CANONICAL_REPLY_DELIVERED`、普通聊天、高频通信、礼物、告白与
不活跃事件仍不产生关系效果。所有 mutation 继续必须经过 Command Service；
canonical delivery 不得携带上述命令或直接写入关系状态。

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

`stage_confirmed` 必须携带 1–8 个已存在的 basis event ID。Service 验证：

- event 存在；
- event 不来自 canonical delivery 这类无关系效果事件；
- 目标阶段合法；
- 重复确认是 NOOP；
- 单次 confession、普通高频通信或成功回信不能自动升级关系。

## 7. Ledger schema v2

保留现有表并兼容迁移：

```text
private_world_events
private_world_snapshots
private_world_candidates
private_world_candidate_decisions
```

事件审计至少记录：

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

候选表不得保存完整私人文本。迁移必须：

- 原 v1 数据可直接打开；
- 不修改既有 event_id 和 delivery_id；
- 迁移前创建本地备份；
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
- 不阻塞 canonical reply 和媒体任务；
- 候选分析默认关闭，完成 UI 验收后再决定是否默认开启。

## 9. 用户管理入口：Companion Control Center

用户不需要也不应被要求运行 CLI。

PrivateWorld 的正式产品入口是本地 Companion Control Center，详见：

```text
docs/P03_03A_COMPANION_CONTROL_CENTER.md
```

PrivateWorld 页面至少包含：

### 9.1 待确认建议

- 显示 boundary respected、conflict、repair 候选；
- 显示简短原因、来源时间和关联信件引用；
- 支持批准、拒绝、稍后处理；
- 批准前显示将影响哪些定性状态；
- 同一候选只能提交一次。

### 9.2 关系与事件时间线

- 当前关系阶段；
- 定性关系状态；
- 最近确认事件；
- 冲突与修复成对显示；
- 不将“分数增长”设计成奖励动画。

### 9.3 称呼与边界

- 授权、撤销私人称呼；
- 设置住所权限；
- 显示变更历史；
- mutation 需要明确确认。

### 9.4 私人世界线

- 新增、修改和删除 Local Continuation；
- 分别设置 `control_only / pending / character_known`；
- 清楚显示“系统知道”和“林离知道”的区别；
- 将 control-only 改为 character-known 时再次确认。

### 9.5 数据与隐私

- 导出；
- 创建备份；
- 在测试副本中重置；
- 删除 PrivateWorld；
- destructive 操作需要二次确认并明确数据范围。

底层可以保留非公开的开发测试命令，但：

- 不作为用户完成条件；
- 不创建桌面快捷方式；
- 不要求用户记命令；
- 只能调用同一个 Command Service，不能形成第二套业务逻辑。

## 10. 本地管理 API

Control Center 使用独立 loopback 管理站点，不复用原版客户端兼容 API 的权限边界。

建议：

```text
Compatibility API: 127.0.0.1:8899
Control Center:    127.0.0.1:<独立端口>
```

要求：

- 只绑定 loopback；
- 无外网监听；
- 不允许原版前端 origin 调用管理 mutation；
- 独立短期管理会话；
- HttpOnly、SameSite=Strict session cookie；
- mutation 要求 CSRF token；
- bootstrap token 一次性使用并立即轮换；
- Content-Security-Policy 禁止外部脚本、字体和媒体；
- 日志不记录 token、statement、候选摘要和完整请求体；
- API 只是 Command Service Adapter。

## 11. ReplyContext 接线

继续使用 `project_private_world()`，但补齐：

- 只把授权称呼转为 trusted runtime fact；
- home access 只转成有限权限提示，不泄露枚举内部名；
- continuation 只注入 `CHARACTER_KNOWN` 的事实；
- hidden score 只转为 low/medium/high 行为层级；
- unknown 保持 unknown，不把 0 解释为负面关系；
- 投影失败时使用空视图，不阻断回信。

## 12. PR 拆分与顺序

### PW-01：默认路径、健康状态和 schema migration

只完成默认启用、迁移、备份和稳定 health，不增加新命令。

### PW-02：Typed Command 与 Reducer

新增命令模型，覆盖关系事件、称呼、住所和 continuation。

### PW-03：Command Service 与完整审计

所有写操作只能经过 Service；覆盖幂等、证据和事务。

### PW-04：Loopback 管理 API

新增只读查询和 mutation Adapter、安全会话、CSRF、错误码与测试。

### PW-05：Control Center PrivateWorld 页面

实现待确认建议、关系时间线、称呼、边界和私人世界线页面。

### PW-06：候选存储与分析器

增加 pending/approve/reject，默认关闭自动分析。

### PW-07：ReplyContext、恢复与全链测试

验证：

- 重启恢复；
- approved candidate 只执行一次；
- control-only 状态不泄露；
- PrivateWorld 失败不丢正文；
- canonical delivery 不增加关系值；
- 用户可不接触终端完成所有操作。

## 13. 测试矩阵

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

### Control Center

- 未登录页面不返回私人数据；
- 一次性 bootstrap token 不可复用；
- CSRF 缺失时 mutation 拒绝；
- 原版客户端 origin 无法调用管理 API；
- 批准、拒绝、撤销和二次确认；
- 页面刷新和重启后状态一致；
- 错误提示不泄露路径和 payload；
- 键盘和屏幕阅读器可完成主要操作。

### 产品链

- 正常回信后只增加 delivery event；
- 记录 conflict 后下一封回复体现有限 tension 行为；
- grant nickname 后可自然使用但不机械复读；
- control-only continuation 不被角色提前知道；
- awareness 切换后角色可使用该事实；
- 数据库不可用时正文仍完成。

## 14. 回滚

- schema 迁移前创建本地备份；
- 每个命令都是 append-only event，错误命令使用补偿命令，不修改历史；
- 候选分析器可独立关闭；
- Control Center 可只读降级；
- PrivateWorld 整体可降级到 NullPort；
- 回滚代码不得删除用户数据库；
- destructive 操作继续要求显式确认。

## 15. 完成条件

- 默认安装创建独立 PrivateWorld 数据库；
- 所有状态变化都经过 Typed Command、Service、Reducer 和 Ledger；
- 用户可在 Control Center 完成关系、称呼、住所和 continuation 操作；
- 用户不需要运行 CLI；
- 候选与提交严格分离；
- 聊天文本没有控制权限；
- canonical reply 不自动增加关系值；
- 模型只看到有限 Character View；
- 重启、导出、备份、重置和删除通过测试；
- PrivateWorld 故障不会影响正文；
- `public-smoke` 与专项测试通过。

# P03 原版客户端陪伴读取与视频开关运行时

## 1. 目标

把已经合入的原版设置弹窗、读取/mutation HTTP Contract 和现有 Memory / PrivateWorld Service 真正接到同一个本机进程中。

发布运行结构：

```text
原版 Olivia 客户端
  -> /toy/letter/* 等既有本机接口
  -> /toy/companion/* 原版设置读取接口
  -> 同一个 127.0.0.1 aiohttp 进程
```

不启动独立浏览器 Control Center，不增加第二个用户服务器，也不要求用户运行 CLI。

## 2. 入口

安装后的启动器不再直接执行：

```text
local_server.py
```

而是执行：

```text
original_client_server.py
```

`original_client_server.py` 会：

1. 导入既有 `local_server` 业务运行时；
2. 复用同一个 Memory Adapter、PrivateWorld Ledger 和候选数据库；
3. 先注册 `/toy/companion/*`；
4. 最后注册原有 catch-all toy API handler；
5. 在同一个 `127.0.0.1:8899` 监听器上运行。

原有 `/health`、信箱、生成、媒体和其他 toy API 仍由 `local_server.handler` 处理。

## 3. 长期记忆接线

运行时从现有 `MemoryPromptBuilder` 读取已经用于 Persona 检索的 `ConversationMemoryPort`，再构造：

```text
ConversationMemoryAdminService
```

审计文件位于本机数据根目录：

```text
<data>/memory/memory_admin_audit.sqlite3
```

该审计不记录记忆正文。当前 PR 只把读取能力挂进原版设置接口；纠正、删除等写操作仍是后续独立 PR。

缺少本机数据根、Mem0 未启用或管理服务无法初始化时，长期记忆能力显示为 disabled/unavailable，不影响信箱和普通回信。

## 4. PrivateWorld 接线

运行时复用 `local_server` 已经持有的同一个 `PrivateWorldPort`，只把 `PrivateWorldSnapshot` 投影为：

- 关系阶段；
- familiarity / trust / comfort / closeness / tension 的 low / medium / high；
- 已授权称呼；
- 住所权限；
- Local Continuation 与 awareness。

不会返回隐藏的 0–100 数值。

候选读取复用同一个 PrivateWorld SQLite 文件中的 `SQLitePrivateWorldCandidateStore`。不会复制第二份候选数据库，也不会在本 PR 中批准或拒绝候选。

## 5. 路由优先级

既有 `local_server` 使用 catch-all 路由，因此挂载顺序必须固定：

```text
1. /toy/companion/status
2. /toy/companion/memory
3. /toy/companion/private-world
4. /toy/companion/private-world/candidates
5. /toy/companion/settings/video-reply (POST)
6. /{tail:.*} 既有 toy API
```

如果顺序反过来，catch-all 会截获原版设置请求。本顺序由测试锁定。

## 6. 安全边界

- 只监听 loopback；
- 只接受运行时注入的原版前端 HTTPS Origin，或显式 loopback 开发 Origin；
- 不硬编码或联系外部原版服务器；
- 返回 `Cache-Control: no-store`；
- 后端错误不返回路径、数据库名、provider payload 或凭据；
- Memory、PrivateWorld 和候选读取分别降级；
- 任一可选能力失败时，原有 toy API 继续工作。

## 7. 本 PR 不包含

- 在原版弹窗中渲染记忆列表和 PrivateWorld 详情；
- 记忆纠正、删除；
- 称呼、住所权限和 Local Continuation 修改；
- 候选批准、拒绝；
- 原版信箱 camelCase Contract 的生产接线；
- 安装器应用 `patch_companion_settings.py`；
- 删除 standalone Control Center 代码。

## 8. 验收

- 原版设置四个 GET 读取路由和视频开关 POST 在同一进程可访问；
- `/health` 和既有 toy API 仍进入原 handler；
- Memory、PrivateWorld、候选复用已有 Service/Store；
- PrivateWorld 输出没有隐藏数值；
- 可选服务缺失时只显示 disabled/unavailable；
- 启动器执行 `original_client_server.py`；
- 缺少该入口时安装运行明确失败为 `PATCH_PAYLOAD_INCOMPLETE`；
- Windows public-smoke、hardening scan 和 whitespace check 通过。

## 9. 后续顺序

```text
CLIENT-SETTINGS-05  原版设置弹窗渲染真实只读数据
CLIENT-SETTINGS-06  原版内受控写操作（含视频开关 replay/conflict）
CLIENT-INSTALL-01   安装器自动应用设置与播放器补丁
CLIENT-CONTRACT-02  原版 Collection 数据格式生产接线
```

# B04：本地记忆与旧信件只读资料库

状态：`REVIEW`。本文件描述 Issue #3 的实现边界；实现尚未合入 `main`。

## 数据边界

- 仓库默认仍是 session-only：未显式启用 `memory_config.json` 或 `OLIVIA_MEMORY_ENABLED=true` 时，不创建数据库，也不保存个人聊天。隔离 Windows 安装默认选择本地 Mem0，并提供可选运行依赖安装；用户可用 `OLIVIA_MEMORY_ENABLED=0` 明确关闭。Embedding 只会在安装器或原版 Settings 中得到用户确认后下载；依赖、模型或初始化失败时，写信继续走现有无记忆降级路径。
- `legacy_letters` 是独立的只读角色经历资料库。导入内容保留原文、来源标签、来源记录 ID、时间、内容哈希和完整 metadata；它不会写入新聊天集合，也不会改写原始文本。
- `conversation_memory` 只在 opt-in profile 启用后保存新聊天摘要/事实，支持 TTL 和单独清空。
- `persona_evidence` 仅保存配置引用、版本和哈希，不会被当作记忆事实写入 prompt。

SQLite 表、FTS 索引和配置都属于本机数据，位于被 `.gitignore` 忽略的目录。仓库只提交代码、schema、空配置模板和 synthetic tests；不提交真实信件、数据库、索引、embedding、模型、凭据或本机绝对路径。

## 端口与 provider

`memory_port.py` 定义 B03 唯一依赖的 `MemoryPort`。SQLite adapter 只使用 Python 标准库；FTS5 不可用时退回参数化的 `LIKE` 查询。`provider=mem0` 仅记录真实 `UNAVAILABLE` 边界，不导入 Mem0、不下载模型、不联网、不伪造可用。

## 导入、去重与导出

`tools.memory_import.LegacyLetterImporter` 支持 JSON、JSONL、CSV 和逐行文本。支持显式映射、BOM/UTF-8/常见本地编码、路径根限制、dry-run 和 checkpoint；格式、编码、JSON 行和字段错误返回稳定代码，不把正文或路径放入报告。默认 `atomic=true`，坏行或存储失败整批回滚；内容 SHA-256 用于幂等重复检测。

metadata 先规范化为 JSON 数据模型，再作为完整 JSON 文本存储；超过安全上限会拒绝整条记录，绝不会截断序列化后的 JSON。`export_records(domains=...)` 要求调用方显式选择域，并在返回前校验可以生成有效 JSON；未选择域会拒绝。

## Prompt 与删除语义

检索内容只作为 `<MEMORY_CONTEXT_UNTRUSTED_DATA>` 内的引用资料。正文在渲染时转义 `<`、`>`、方括号、下划线和反斜杠；角色伪装、命令、重复 delimiter 都不会成为 system 指令。资料区分 `CONVERSATION_MEMORY_CURRENT` 与 `LEGACY_LETTERS_REFERENCE_ONLY`，并携带有限 provenance。

聊天清空只调用 `clear_conversation()`，不会触碰 `legacy_letters`。旧信件没有单条删除 API；显式 `uninstall(delete_legacy=true)` 才会在一个事务中整库删除，并返回实际删除计数和 `whole_library` 范围。HTTP `/toy/letter/legacy/import` 只接受显式 `mode=read_only` 的内存 JSON 记录；它把原文、来源和 metadata 原样交给现有 SQLite adapter 的原子导入和 SHA-256 去重逻辑，响应只返回计数而不回显正文或本地路径。`/toy/letter/legacy/official-import` 由设置页用户确认触发，复用同一只读导入边界，只导入用户原信和官方文字回信，忽略视频；官方凭证只在本次请求内存中使用。一个本地 data-root 只对应一个用户档案：首个含文字回信的官方账号写入稳定账号标记，后续不同账号会在任何归档或记忆写入前被拒绝；需要切换账号时必须使用独立 data-root。导入前必须确认真实 Mem0 与 PrivateWorld 可用，否则在采集官方历史前失败。采集并校验后，系统按 `occurred_at` 从早到晚逐封执行 `remember_exchange`；只有写入或稳定判重成功才会继续，`SKIPPED` 视为失败。全部记忆完成后才允许一次幂等的 PrivateWorld 历史基线初始化；它不会覆盖已有 PrivateWorld。最后一步才原子写入 SQLite 只读归档并发布到默认信箱时间线。历史信件保持 `scope=legacy`、`read_only=true`、`is_read=true`，因此可在真实信箱查看，但不增加未读数，也不会生成新回信或视频。

## 健康检查与验证

`/health?profile=memory` 只返回状态、域计数、FTS5/vector 配置边界和 `network_called=false`，不返回数据根、正文或密钥；memory 不可用不会让 core health 变成假成功或假失败。

```text
rtk pytest -q tests/memory tests/llm/test_b04_memory_integration.py
rtk python tools/healthcheck.py --profile memory
```

验证使用 synthetic records，覆盖 metadata 长值、坏编码/JSON、幂等、只读隔离、prompt injection、导入原子性、导出 round-trip、session-only 默认、删除范围、重启恢复和无网络状态。Issue #3 的 PR 正文使用 `Closes #3`。

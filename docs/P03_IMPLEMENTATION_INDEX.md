# P03 实施文档总索引

## 1. 阶段目标

P03 的唯一目标是先交付一个可以长期使用的本地书信陪伴版本：

```text
来信
  -> 三模式表达决策
  -> Persona / ReplyContext 装配
  -> 主模型生成
  -> 审校、最多一次修复、复审
  -> canonical reply 持久化
  -> 文字 / 说话视频 / 音乐视频
  -> 长期记忆
  -> 受控 PrivateWorld
  -> 重启恢复与原版客户端展示
```

P03 不包含 IM、主动发消息、自主生活状态机、多模态输入、实时数字人或通用工具 Agent。

## 2. 当前基线

已经完成并合入 `main`：

- Persona 2.0 和林离核心人格；
- `ReplyContext`、Prompt 装配和预算；
- 确定性质量门、模型审校、最多一次修复；
- `text_letter`、`spoken_video`、`musical_video` 三模式路由；
- 音乐请求、高情绪和音乐话题只建立候选资格，不自动触发音乐视频；
- 说话视频和音乐视频的现有渲染链；
- PrivateWorld 的数据结构、SQLite ledger、Reducer 和有限投影；
- 可选的旧信与本地记忆基础实现。

仍未闭环：

- MiniMax Music 3 的纯钢琴抒情风格稳定性；
- PrivateWorld 的默认启用、受控修改与审计入口；
- 成熟长期记忆框架接入；
- 原版客户端终态兼容和旧 PR 清理；
- 默认配置、安装器和真实全链验收。

## 3. 文档清单

| 顺序 | 文档 | 目标 |
| --- | --- | --- |
| 0 | `P03_00_REPOSITORY_GOVERNANCE.md` | 先稳定主干、清理重复工作和规定合并纪律 |
| 1 | `P03_01C_MINIMAX_MUSIC_STABILIZATION.md` | 稳定传统、克制的普通话女声与钢琴音乐回复 |
| 2 | `P03_02_PRIVATE_WORLD_RUNTIME.md` | 建立默认持久化、受控事件、权限与审计闭环 |
| 3 | `P03_03_LONG_TERM_MEMORY_MEM0.md` | 使用 Mem0 OSS 编排新对话长期记忆，不重复造轮子 |
| 4 | `P03_04_CLIENT_COMPAT_AND_CLEANUP.md` | 修正原版客户端终态，迁移验收资产并清理旧 PR |
| 5 | `P03_05_INSTALLATION_AND_DEFAULTS.md` | 让 Windows 安装后的默认数据、配置和健康检查可用 |
| 6 | `P03_06_END_TO_END_ACCEPTANCE.md` | 用真实模型和本机媒体环境验收完整链路 |

## 4. 强制执行顺序

```text
P03-00 仓库治理
      ↓
P03-01C MiniMax 音频稳定化
      ↓
P03-02 PrivateWorld 受控闭环
      ↓
P03-03 Mem0 长期记忆
      ↓
P03-04 客户端兼容与旧工作清理
      ↓
P03-05 默认配置与安装
      ↓
P03-06 真实全链验收
```

PrivateWorld 与 Mem0 的底层开发理论上可以并行，但本项目当前优先降低并行分支和范围污染风险，因此默认按上述顺序推进。只有在两个工作包的文件范围完全不重叠、主干已保护、CI 稳定时，才允许并行。

## 5. Issue 与 PR 原则

用户审阅这些实施文档后，再创建执行 Issue。每个文档对应一个 Tracker Issue，Tracker 下按单职责 PR 拆分。

所有 PR 必须满足：

- 从最新受保护 `main` 创建；
- 一个 PR 只解决一个可独立回滚的问题；
- 不直接 push `main`；
- 不 force-push 已公开分支；
- 必须通过 `public-smoke` 和该工作包的专项测试；
- 不把本机模型、媒体、原始信件、私有世界线、路径或凭据提交到仓库；
- 外部 provider 不可用时必须诚实降级，不能伪造 READY；
- canonical reply 已完成后，记忆、PrivateWorld 或媒体失败不得抹掉正文。

## 6. 总体验收门槛

P03 只有同时满足下列条件才能关闭：

- 普通日常、高情绪、音乐讨论和明确音乐请求均按文档自然路由；
- MiniMax 音乐回复在约定样本集上达到人工风格验收门槛；
- 新对话长期记忆由成熟框架编排；
- Archive、长期记忆和 PrivateWorld 三个域不混写；
- PrivateWorld 只能通过受控、可审计命令变化；
- 原版客户端能显示正确的等待、完成和失败终态；
- Windows clean install 后可完成真实文字回复；
- 已配置本机媒体环境时可完成说话视频和音乐视频；
- 重启后当前信件、记忆和 PrivateWorld 能恢复；
- 任一可选 provider 失败时，系统保留 canonical text 并返回明确状态；
- 所有用户数据均可导出和按域删除。

## 7. 后续阶段边界

P03 完成后才进入：

- P04：世界时间、课程、练琴、作品与短期生活状态；
- P05：IM、短消息、多轮会话和跨媒介同步；
- P06：主动发消息和生活事件；
- P07：语音、图片、音乐理解等多模态输入；
- P08：长期评测、发布和迁移体系。

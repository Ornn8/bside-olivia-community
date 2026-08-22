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
  -> Companion Control Center
  -> 重启恢复与原版客户端展示
```

P03 不包含 IM、主动发消息、自主生活状态机、多模态输入、实时数字人或通用工具 Agent。

## 2. 已确认决策

- 长期记忆采用 Mem0 OSS Python Library、本地 Qdrant 和本地中文 embedding；
- 不复制 Mem0 内部算法；
- PrivateWorld、长期记忆、音乐校准和数据管理必须有图形化 Control Center；
- CLI 仅用于 CI 和内部维护，不是用户完成路径；
- MiniMax 方案依据官方 README、官方 Caption Skill、官方 ComfyUI 工作流和本机听感证据；
- MiniMax Caption 只写正向目标，不发送自然语言禁止项；
- 音乐视频保留仓库既有链：说话视频＋可选静音转场＋演唱视频；
- 视频生成失败不得删除已经持久化的 canonical text；
- 所有工作按文档和小 PR 顺序推进。

## 3. 当前基线

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

- MiniMax Music 3 人声＋钢琴抒情风格稳定性；
- PrivateWorld 默认启用、受控修改与审计入口；
- 成熟长期记忆框架接入；
- 图形化本地管理；
- 原版客户端终态兼容和旧 PR 清理；
- 默认配置、安装器和真实全链验收。

## 4. 文档清单

| 顺序 | 文档 | 目标 |
| --- | --- | --- |
| 0 | `P03_00_REPOSITORY_GOVERNANCE.md` | 稳定主干、清理重复工作和规定合并纪律 |
| 1 | `P03_01C_MINIMAX_MUSIC_STABILIZATION.md` | 稳定普通话女声与钢琴抒情音频，并保护既有拼接链 |
| 2 | `P03_02_PRIVATE_WORLD_RUNTIME.md` | 建立默认持久化、受控事件、权限与审计闭环 |
| 3 | `P03_03_LONG_TERM_MEMORY_MEM0.md` | 使用 Mem0 OSS 编排新对话长期记忆，不重复造轮子 |
| 3A | `P03_03A_COMPANION_CONTROL_CENTER.md` | 提供 PrivateWorld、Memory、音乐校准和数据管理 UI |
| 4 | `P03_04_CLIENT_COMPAT_AND_CLEANUP.md` | 修正原版客户端终态，迁移验收资产并清理旧 PR |
| 5 | `P03_05_INSTALLATION_AND_DEFAULTS.md` | 图形化安装、Setup Wizard、默认数据和健康检查 |
| 6 | `P03_06_END_TO_END_ACCEPTANCE.md` | 用真实模型和本机媒体环境验收完整链路 |

## 5. 强制执行顺序

```text
P03-00 仓库治理
      ↓
P03-01C MiniMax 音频稳定化
      ↓
P03-02 PrivateWorld 后端闭环
      ↓
P03-03 Mem0 长期记忆
      ↓
P03-03A Companion Control Center
      ↓
P03-04 客户端兼容与旧工作清理
      ↓
P03-05 图形化安装与默认配置
      ↓
P03-06 真实全链验收
```

说明：

- Control Center shell 和安全会话可以在 PrivateWorld 后端完成后提前开发；
- Memory 页面必须等 Mem0 Adapter 与管理 Service 稳定；
- 音乐校准页可以在 MiniMax 规划和 Worker 完成后接入；
- 默认不同时开启大量范围重叠的 PR；
- 只有主干保护和 CI 稳定后，才允许文件范围完全不重叠的并行工作。

## 6. 音乐视频链边界

当前目标链必须保持：

```text
canonical reply
  -> 说话视频 normal_video_path
  -> MiniMax 完整歌曲
  -> RoFormer vocals
  -> 演唱视频 song_video_path
  -> 可选官方静音转场
  -> concat_videos(normal, transition?, performance)
  -> 最终 MP4
```

P03-01C 优化音频 Caption、参数和 seed，不重构 RoFormer、LatentSync 和拼接顺序。P03-06 单独验收最终视频。

## 7. Issue 与 PR 原则

用户已审阅并确认实施方向。下一步为每份文档创建 Tracker Issue，Tracker 下按单职责 PR 拆分。

所有 PR 必须满足：

- 从最新受保护 `main` 创建；
- 一个 PR 只解决一个可独立回滚的问题；
- 不直接 push `main`；
- 不 force-push 已公开分支；
- 必须通过 `public-smoke` 和该工作包专项测试；
- 不把本机模型、媒体、原始信件、私有世界线、路径或凭据提交到仓库；
- 外部 provider 不可用时诚实降级，不能伪造 READY；
- canonical reply 已完成后，记忆、PrivateWorld 或媒体失败不得抹掉正文；
- 用户功能必须提供 UI，不得以 CLI 代替。

## 8. 总体验收门槛

P03 只有同时满足下列条件才能关闭：

- 普通日常、高情绪、音乐讨论和明确音乐请求均按文档自然路由；
- MiniMax 音乐回复在固定样本集上达到人工风格验收门槛；
- 音乐视频按说话＋可选静音转场＋演唱顺序拼接；
- 新对话长期记忆由 Mem0 OSS 编排；
- Archive、长期记忆和 PrivateWorld 三个域不混写；
- PrivateWorld 只能通过受控、可审计命令变化；
- 用户可在 Control Center 管理状态和数据，不需要终端；
- 原版客户端能显示正确等待、完成和失败终态；
- Windows clean install 和图形化 Setup Wizard 通过；
- 已配置本机媒体环境时可完成说话视频和音乐视频；
- 重启后当前信件、记忆和 PrivateWorld 能恢复；
- 任一可选 provider 失败时保留 canonical text 并返回明确状态；
- 所有用户数据均可导出和按域删除。

## 9. 后续阶段边界

P03 完成后才进入：

- P04：世界时间、课程、练琴、作品与短期生活状态；
- P05：IM、短消息、多轮会话和跨媒介同步；
- P06：主动发消息和生活事件；
- P07：语音、图片、音乐理解等多模态输入；
- P08：长期评测、发布和迁移体系。
# STATUS

本文件只提供仓库入口、队列链接和状态语义，不复制任何工作项的动态状态、分支、PR、SHA 或进行中事实。GitHub Issues 与 Milestones 是工作队列及进展事实的唯一 source of truth。允许的状态只有：`BACKLOG`、`IMPLEMENTING`、`REVIEW`、`READY`、`MERGED`、`BLOCKED`。

仓库：[`Ornn8/bside-olivia-community`](https://github.com/Ornn8/bside-olivia-community)；治理规则入口：[`PROJECT_MANAGEMENT.md`](PROJECT_MANAGEMENT.md)。

队列入口：

- [GitHub Issues](https://github.com/Ornn8/bside-olivia-community/issues)
- [GitHub Milestones](https://github.com/Ornn8/bside-olivia-community/milestones)
- [P02 intimacy model contract](P02_17_INTIMACY_MODEL.md)

## 状态语义

- `BACKLOG`：已排队，尚未开始。
- `IMPLEMENTING`：已开始实施，尚未交付审阅。
- `REVIEW`：实施结果等待独立审阅或证据复核。
- `READY`：审阅通过，等待总控合并决策。
- `MERGED`：已由真实 GitHub PR 合并到目标分支。
- `BLOCKED`：存在未解决的权限、依赖、数据、证据或外部服务阻塞。

## P01 人格 evidence 边界

Issue #4 的 persona evidence 仅作为候选、可审计的证据包，不是最终人格定稿或现实人物身份声明。observed、inferred、uncertainty 必须分离；B04 只读 `persona_evidence` 元数据；原始信件、歌词、字幕、私信、媒体和用户资料不进入仓库。来源、评估器和回滚边界见 [`P01_PERSONA_EVIDENCE.md`](P01_PERSONA_EVIDENCE.md)；Issue、PR 和 commit 仍是动态状态与精确事实的唯一来源。

# Project management / 项目治理

本文件是项目治理规则的唯一入口；仓库是
[`Ornn8/bside-olivia-community`](https://github.com/Ornn8/bside-olivia-community)。
GitHub [Issues](https://github.com/Ornn8/bside-olivia-community/issues) 与 [Milestones](https://github.com/Ornn8/bside-olivia-community/milestones) 是工作队列、scope、进展和精确提交事实的唯一事实源；
[`STATUS.md`](STATUS.md) 只保留状态语义、仓库/队列入口和链接，不复制逐项动态状态。

B11 的逐门禁记录格式与冻结审查协议见 [`B11_ACCEPTANCE_STANDARD.md`](B11_ACCEPTANCE_STANDARD.md)；本文件仍是治理规则的唯一入口。

## 1. 基线与变更权限

- GitHub `main` 是唯一开发基线。旧本地历史只能作为取材，禁止直接推送旧历史或旧分支。
- 所有变更必须通过 PR；总控和执行者都禁止直接更新 `main`。PR 目标固定为 `main`，不得绕过审阅合并。
- 分支必须从 `origin/main` 创建，命名为 `codex/<milestone>-<slug>`。实现完成且审阅 `PASS` 后，才可提交并推送分支，由总控合并；每次合并后必须对远端 `main` 回归。
- GitHub Issues 与 Milestones 是工作队列；Issue、PR 和 commit 页面维护精确 SHA、进行中事实和验收证据。不要把这些事实手工复制到 `STATUS.md` 或其它文档。
- `docs/STATUS.md` 只记录状态语义、仓库/队列入口和链接；其它计划、派工和迁移文档只能提供背景或批次细节，不得另立 active 状态。

## 2. 角色与实施窗口

架构硬约束是“只组装，不自己造”：优先复用经过验证、较新且活跃维护的 GitHub/Hugging Face 上游；本仓库只允许必要的薄 adapter/connector、接口契约、配置、编排、生命周期、可装卸机制和验收测试。reviewer 必须把该项列为 blocking 检查，并拒绝重写上游核心能力、平替引擎、自研框架化或缺少 docs/manifest/provenance 中 upstream、固定 version/commit/license、替换边界和卸载路径的变更。

- 根任务/总控只负责宏观编排、任务布置、Git 运输、门禁验收以及最终视觉/听觉/文字多模态验收；不亲自承担具体开发、实现或修复。
- 所有具体开发与修复必须放在本项目独立的 Codex 任务中，由 `gpt-5.6-luna/xhigh` 实施窗口执行；根任务/总控不得直接修改具体实现来替代该任务。
- 每个实施任务冻结最终 diff 后且仅在此时，必须恰好创建一名独立的 `gpt-5.6-luna/xhigh` reviewer；reviewer 只读检查变更、测试、隐私/许可证边界和所需证据，并明确给出 verdict。
- reviewer `FAIL` 时，只能退回同一实施任务修复，再由同一 reviewer 复核更新后的冻结 diff；不得创建第二名 reviewer。审阅超时、未返回 verdict 或证据不完整都不是 `PASS`。
- 只有明确的审阅 `PASS` 才能交总控验收；总控不能用其它功能的通过记录替代当前功能的证据。

## 3. 依赖与并行上限

- 当前功能合并前，不得开启依赖它的功能。
- 只允许真正独立且基线一致的并行项；WIP 上限为 2：一个关键功能，加一个独立研究/资产项。
- 依赖、基线或证据不一致时，保持阻塞，不以并行实现掩盖缺口。

## 4. 状态机

项目工作项只能使用以下状态：`BACKLOG`、`IMPLEMENTING`、`REVIEW`、`READY`、`MERGED`、`BLOCKED`。
状态枚举只在本文件和 [`STATUS.md`](STATUS.md) 定义；工作项动态状态、事实和下一步由对应 GitHub Issue/Milestone 维护，不在文档中复制第二份。

## 5. 验收门槛

交总控前，所有适用管线必须无阻塞，并且所有相关的 targeted、full、collect、compile、scanner、scope 和 diff 检查都必须有真实全绿证据。每个适用的完整测试套件都必须实际收集测试（`collect > 0`），并报告 `failed=0`、`errors=0`、`skipped=0`；环境型 skip、未收集测试、单项冒烟或命令退出成功都不能冒充通过。还必须同时满足：

- 运行时健康检查真实通过，管线没有阻塞、假成功、永久等待或未解释的降级；
- 需要真实视觉、听觉或文字判断的项目，由根任务亲自观看、听取或阅读验收；自动指标不能替代人工验收；
- 失败、缺权重、缺权限、缺证据和外部服务不可用必须标为 `BLOCKED` 或保留未通过；对适用管线而言，真实的 `UNAVAILABLE` 也是未就绪事实，不得写成 `READY`/`MERGED`。只有明确标注为不适用的管线才可排除，不能用 `UNAVAILABLE` 冒充就绪。

## 6. PR 交付协议

标准顺序固定为：GitHub Issue → 从最新 `main` 建 `codex/<milestone>-<slug>` 分支 → 独立 Codex 实施任务（由 `gpt-5.6-luna/xhigh` 执行）→ 冻结最终 diff → 恰好一名独立 reviewer 明确 `PASS` → 创建 PR → 远端 required CI 全绿 → 总控门禁与实际视觉/听觉/文字多模态验收 → merge → 远端 `main` 回归。提交、push、PR 创建、merge 和回归的 Git 运输由总控负责；任何环节都不得绕过 reviewer 或远端 CI。PR 正文必须写清 scope、tests、reviewer 和 boundary；精确链接以 GitHub Issue/PR 为准。

## 7. 数据与提交边界

上游复用必须记录 upstream、固定 version/commit、license、替换边界和卸载路径；缺少这些 provenance 或发现“为门禁而重造”时保持 `BLOCKED`，不得以临时代码冒充完成。

- 模型、媒体、私有信件、本机绝对路径和 secrets 不得提交；旧信只进入独立只读资料库，不进入代码、测试 fixture 或 PR 证据正文。
- 本仓库只提交可复现的源码、测试、正式文档、模板、schema 和脱敏证据；本机资产通过外部路径或受控 manifest 引用。
- 许可证、隐私、权利、路径和删除风险未闭合时，功能不得宣称完成。

批次输入/输出与证据模板见 [`DELEGATION_BOARD.md`](DELEGATION_BOARD.md)，技术计划见 [`MASTER_PLAN.md`](MASTER_PLAN.md)，总验收矩阵见 [`ACCEPTANCE.md`](ACCEPTANCE.md)。这些文档不改变本文件和 `STATUS.md` 的治理权威。

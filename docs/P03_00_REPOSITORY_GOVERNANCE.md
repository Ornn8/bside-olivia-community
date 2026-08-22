# P03-00 仓库治理、主干保护与旧工作收口

## 1. 目标

在继续修改运行时前，先消除主干可被直接写入、旧 PR 长期漂移、重复实现并存和范围门禁失真的风险。

本工作包不增加产品功能，只建立后续 P03 的可信开发基线。

## 2. 当前问题

- `main` 尚未启用保护规则；
- 历史上已经发生 worker 绕过 PR 直接写入主干；
- PR #18、#25、#26 均基于明显落后的主干；
- #25 与当前 `reply_model_quality.py` 属于平行 Reviewer/Rewriter 实现；
- #26 的来源覆盖和验收资产有价值，但不能直接合并旧分支；
- scope 检查依赖正确基线，主干污染会让无关 PR 失败。

## 3. 主干保护规则

对 `main` 启用：

- 只能通过 Pull Request 合并；
- 必须通过 `public-smoke`；
- 合并前分支必须与最新 `main` 同步；
- 禁止直接 push；
- 禁止删除 `main`；
- 禁止 force-push；
- 管理员和自动化默认不得绕过必需检查；
- 至少保留一个独立审阅结果或显式维护者合并决策；
- 合并后自动删除分支。

如果 GitHub 套餐或仓库设置无法提供某项规则，应记录实际缺失项，不得把“约定禁止”宣称成平台强制门禁。

## 4. 分支与提交纪律

- 新工作统一使用 `codex/p03-<work-package>-<purpose>`；
- 文档准备分支只包含文档；
- 每个 PR 只修改其列明的文件范围；
- 不在已公开 PR 分支上 rebase 后 force-push；
- 需要重建旧 PR 时，从最新 `main` 建新分支并 cherry-pick 必要提交；
- 发现主干出现旁路提交时，使用 revert PR 恢复，不重写公开历史；
- 本机验收证据进入 `.evidence/` 或用户数据目录，不进入 Git。

## 5. 旧 PR 处理

### PR #18：原版客户端终态兼容

处理方式：

1. 从最新 `main` 创建新分支；
2. 重新实现公开响应的状态映射；
3. 保持内部状态机为字符串；
4. 补齐当前三模式和延迟发布测试；
5. 新 PR 合并后关闭 #18，并注明 superseded。

不得直接把落后分支 merge 进当前主干。

### PR #25：重复 Reviewer/Rewriter

处理方式：

1. 将 #25 与当前 `reply_model_quality.py`、`reply_quality_gate.py` 对比；
2. 只迁移当前实现缺失的测试或隐私断言；
3. 不保留第二套运行时模块；
4. 有价值内容合并后关闭 #25，并注明 superseded by current runtime。

### PR #26：来源覆盖与陪伴验收

处理方式：

从最新 `main` 迁移并适配：

```text
docs/P02_PERSONA_SOURCE_COVERAGE.md
linli_character/persona_source_coverage_v2.json
tests/persona/test_companion_acceptance.py
tools/persona_companion_acceptance.py
```

验收工具必须认识当前三模式路由和最新 Persona 文件，不得照搬旧基线假设。

## 6. 拆分 PR

### GOV-01：启用主干保护

外部仓库设置变更，不修改代码。

验收：

- 直接 push `main` 被拒绝；
- 失败的 `public-smoke` 阻止合并；
- 设置页面和测试 PR 提供证据。

### GOV-02：建立 P03 scope 清单

新增或更新：

- P03 工作包文件所有权；
- 历史基线与当前 diff 的比较规则；
- scope 工具的错误说明。

验收：无关文件不会被错误归入当前 PR。

### GOV-03：旧 PR 审计报告

只提交一份短报告，列明 #18、#25、#26：

- 可迁移内容；
- 已被主干替代内容；
- 新 PR 目标；
- 最终关闭理由。

该报告完成后，具体功能迁移在 P03-04 执行。

## 7. 测试与证据

- 创建一个只改注释的测试 PR，确认必需检查；
- 创建一个故意失败的临时检查分支，确认不能合并；
- 验证机器人凭据不能直接写 `main`；
- 记录 branch protection 实际生效项；
- 不在仓库中保存访问令牌、完整设置响应或账号信息。

## 8. 回滚

- 代码文档可通过普通 revert PR 回滚；
- branch protection 若阻塞紧急修复，使用临时、可审计的规则变更，修复完成后立即恢复；
- 不以关闭保护规则作为日常故障处理方式。

## 9. 完成条件

- `main` 已受保护；
- 后续 worker 无法直接 push 主干；
- 旧 PR 的去向明确；
- P03 各工作包文件范围已登记；
- 所有后续实现均从同一可信主干开始。

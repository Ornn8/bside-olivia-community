# Experimental modules

本文件不是功能承诺，而是把当前研发中的模块与公开基线隔离开，避免误用。

| 模块 | 当前状态 | 进入默认公开门禁前必须满足 |
| --- | --- | --- |
| B05 ASR | 本机 provider/fallback 试验 | 去除机器路径；补齐无模型 fixture；clean clone 可收集并通过 |
| B06 TTS | 本机 CosyVoice 适配试验 | 运行时、音频资产和 provider 配置全部外置；缺失时返回 `UNAVAILABLE` |
| B07/B08 Live | 编排与协议试验 | 明确官方协议边界；不把 health/contract 当成真实 provider 成功 |
| B10A/B10B | 安装与模块组合试验 | child scope、历史基线和当前 diff 语义统一；全量组合门禁稳定 |
| B11 visual | 视觉运行时试验 | 不把原图 fallback 宣称为口型/面部重绘；资源和 evidence 私有化 |

## 工作树与公开提交

当前工作树可能同时存在上述实验代码、测试和本机 evidence。公开提交前应逐项检查：

1. `git ls-files` 中的每个实验文件都没有凭据、绝对路径、官方抓包或媒体/模型二进制；
2. 默认安装只拉取公开依赖，不会隐式扫描 Steam、模型或用户数据目录；
3. 没有 provider 时，状态为 `UNAVAILABLE`、`DEGRADED` 或等价的明确失败，而不是伪造 `READY`；
4. 实验 scope 不会阻塞公开 HTTP/media 基线，也不会被 `public-smoke` 误报为已验收；
5. 新模块拥有单独的 README、fixture 测试、依赖清单和回滚说明。

默认 `pytest` 只覆盖公开 HTTP/media 基线；实验测试不会被删除，而是必须按目录显式运行。这样可以避免未完成的 scope 组合阻塞公开基线，同时保留维护者继续推进实验模块所需的回归入口。

在这些条件完成前，维护者应把实验模块视为可选分支或本机集成层，而不是稳定 API。

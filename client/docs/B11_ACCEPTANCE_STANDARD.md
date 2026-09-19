# B11 验收标准与项目流程

状态：规范性文档；本文件把 [`ACCEPTANCE.md`](ACCEPTANCE.md) 的门槛整理为可执行的验收记录格式。它不代表当前产品已经通过验收。

## 1. 总结论规则

B11 只允许在以下条件全部满足时写 `PASS`：

1. 验收清单已声明所有管线的 `applicable` 状态；所有适用管线均无阻塞，且有真实健康证据。未明确声明为不适用的管线，不得因缺少实现、权重、权限、证据或外部服务而排除。
2. 所有适用的自动化完整测试套件都真实收集到测试，并满足 `collect > 0`、`failed = 0`、`errors = 0`、`skipped = 0`、退出码为 `0`。
3. `compile`、`scanners`、`scopes`、`health`、`lifecycle` 五类门禁全部为 `PASS`；任何一类为 `BLOCKED`、`UNAVAILABLE` 或 `UNVERIFIED`，总结果只能是 `BLOCKED` 或 `UNVERIFIED`。
4. LLM 通过配置的 API 运行；除 LLM API 外的运行时组件在本机运行。不能用本机 mock、单项冒烟或 API 可达性替代完整管线证据。
5. 代码只组装已验证的 GitHub/Hugging Face 上游，不自造引擎、模型或框架；每个上游都有完整 provenance。
6. `legacy_letters` 是独立、只读的旧信件经历资料库；原信不可改，新聊天的读写数据不得写入或混入该库。
7. 最终 diff 冻结后，恰好由一名独立同级 `gpt-5.6-luna/xhigh` reviewer 审查并明确写出 `PASS` 或 `FAIL`。超时、配额不足、`UNAVAILABLE`、没有 verdict 或证据不全都不是 `PASS`。
8. 总控只负责 Git transport、门禁和真实视觉/听觉/文字验收；总控不得以自动指标、另一批次证据或 reviewer 缺席替代实际验收。
9. 安装、停用、卸载、回滚和重装可逆，不删除原版资产、用户数据或未登记路径。

## 2. 机器可执行门禁记录

每个门禁写一行 JSON 或等价表格，至少包含：`gate_id`、`applicable`、`command`、`exit_code`、`status`、`evidence_path`、`source_commit`。适用性为 `false` 时必须同时写 `reason` 和批准边界；空白、缺字段和“未运行”都不是通过。

| gate_id | 必须执行与通过条件 | 失败状态 |
|---|---|---|
| `tests` | 每个适用完整套件实际 `collect > 0`，且 `failed=0`、`errors=0`、`skipped=0`、退出码 `0`；保存完整日志或 JUnit/XML | `BLOCKED`/`UNVERIFIED` |
| `compile` | 对所有 source roots 运行 `rtk python -m compileall -q <source-roots>`，无编译错误 | `BLOCKED` |
| `scanners` | secrets、原版资产/模型/用户数据、大文件、禁止外联和 provenance 扫描均有命令、退出码和脱敏报告；未解释命中为失败 | `BLOCKED` |
| `scopes` | `rtk git status --short`、`rtk git diff --check`、基线到 HEAD 的文件 allowlist 和逐文件 diff 检查全绿；不得出现产品实现越界或受保护数据 | `BLOCKED` |
| `health` | 每个适用组件和端到端管线真实 healthcheck 为 `READY`/`HEALTHY`；`UNAVAILABLE` 不得升级；错误路径必须返回真实可行动状态 | `BLOCKED`/`UNVERIFIED` |
| `lifecycle` | 干净环境 dry-run、安装、启动/停用、卸载、回滚、重装全部成功；preserve 清单、前后 hash 和删除清单证明用户数据/原版资产未被删除 | `BLOCKED` |

推荐的最小入口如下；它们不是完整套件的替代品：

```powershell
rtk pytest -q
rtk python -m compileall -q <source-roots>
rtk python <scanner.py> --evidence-dir <local-evidence>
rtk python <healthcheck.py> --profile all-applicable
rtk python <lifecycle-harness.py> --evidence-dir <local-evidence>
```

不得把“命令退出码为 0 但收集为 0”、环境 skip、缺权重导致的 skip、单个接口冒烟、单个模型生成或 README 自述写成测试通过。

## 3. 架构、运行时与 provenance

- LLM 只通过配置的 API 接入；真实 key 只在运行时注入，不写入仓库、日志、截图或证据正文。
- ASR、TTS、记忆、视觉驱动、会话编排和生命周期运行时必须在本机；外部下载只属于显式安装步骤，不能成为运行时隐式依赖。
- 每个 GitHub/Hugging Face 上游都必须登记：项目/模型 URL、固定 version 或 commit、许可证标识和证据、下载包或权重 SHA-256、集成边界、替换路径、卸载路径及审查范围。
- 本仓库只保留薄 adapter/connector、接口契约、配置、编排、生命周期、可装卸支持和验收测试。重写上游核心能力、制造平替引擎/模型/框架，或缺少上述 provenance，均为 blocking finding。
- 原版程序、解包资产、模型权重、生成媒体、真实密钥、玩家资料和本机绝对路径不进入 Git 或分发包；只可由本机 manifest 和脱敏证据引用。

## 4. `legacy_letters` 数据边界

`legacy_letters` 只读保存旧信件作为角色经历参考，不是聊天记录，也不是玩家记忆。验收必须证明：

- 原始信件保留来源记录、时间、内容 hash 和 metadata；导入后仍不可编辑、覆盖或删除原文。
- 新聊天消息、用户记忆、清空聊天和删除请求使用独立 store/API；任何新聊天写入都不能落到 `legacy_letters`。
- 检索只作为明确标记的只读、不可信上下文；不得把旧信自动当成新事实、用户记忆或当前聊天内容。
- `legacy_letters` 的只读健康检查、越权写入拒绝、聊天清空隔离、重启后 hash 不变和卸载保留测试均有证据。

## 5. 冻结、reviewer 与总控流程

标准顺序固定为：GitHub Issue → 从权威 `main` 基线创建 `codex/<milestone>-<slug>` → 独立实施任务 → 文档/代码自检 → 冻结最终 diff → 创建恰好一名独立 `gpt-5.6-luna/xhigh` reviewer → reviewer 明确 `PASS`/`FAIL` → 交总控做门禁与真实多模态验收 → 由总控负责后续 Git transport。

冻结记录必须固定 `base_commit`、`head_commit`、分支、文件清单、`git diff --check` 结果和 diff hash。reviewer 只能审查该冻结范围，并记录 reviewer ID、模型、审查时间、测试/扫描结果、许可证/隐私边界、证据路径和明确 verdict。

若 verdict 为 `FAIL`，只能由同一实施任务修复并冻结新 diff，再由同一 reviewer 复审；禁止创建第二 reviewer。`PASS` 只表示 reviewer 对冻结 diff 的审查结论，不替代总控实际观看、听取和阅读。未完成的视觉、听觉或文字验收必须保持 `UNVERIFIED`，不得升级为 `READY` 或 `PASS`。

## 6. 证据与审查边界

证据目录必须位于本机且被忽略，至少包含完整测试结果、门禁记录、环境版本、health/lifecycle 日志、脱敏扫描、manifest/provenance、必要的视觉/听觉/文字人工记录和 `review.md`。每项证据注明来源文件或上游 URL、对应 commit、许可证、hash、生成命令、退出码、人工/自动性质及未覆盖边界；证据缺失、无法复现或只证明局部能力时，只能报告局部结果，不得宣称总验收通过。

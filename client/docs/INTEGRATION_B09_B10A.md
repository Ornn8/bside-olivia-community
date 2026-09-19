# B09/B10A canonical main integration

状态：候选分支已 rebase 到最新治理 main，代码与验证已完成；同一独立 `gpt-5.6-luna / max` reviewer 对更新后的最终 diff 判定 `PASS`。

## 基线与源 refs

- canonical base：`github/main` 固定为 `63017963851ade6784c1d10a971de99fbcdfbca0`（治理 PR #1 merge）；其第一父提交为 sanitized main `8e8191666f1b812a48e436afd27713a0b73a789b`。
- 上游仓库的 PRIVATE、默认分支为 main、远端 clean 状态由总控核验；本轮使用总控已 fetch 的 `github/main`，不访问网络。
- 候选分支从旧 sanitized main 的集成结果 rebase 到 `github/main`，保留治理提交与 B09/B10A 功能，不直接更新 main。

| 源 ref / 提交 | 集成提交 | 处置 |
| --- | --- | --- |
| `github/main` `63017963851ade6784c1d10a971de99fbcdfbca0` | rebase base | 保留治理 PR #1，不重放治理历史 |
| `feature/b09` `93a80e59e005118f9cd1ef331709e4d08d7b1845` | `7998028`（重放 `a2ea192`） | B09 tip 中的 sanitized fixture 提交与 main 已有内容重复，不重复导入 |
| B09 功能 `a2ea192f443606b23671b766059e2211401bd826` | `7998028` | rebase 后的 media state engine |
| B10A 功能 `7507aedf28b39c542462ed156b54771b0f0f0202` | `d037d14` | rebase 后的 packaging/runtime skeleton |
| B10A fixture `16f22291ed4786e87e003c86b988aedc6c638679` | 无（no-op） | 与 main 的 sanitized fixture 内容重复 |
| B10A 修复 `e713f80e6a9d4486fa44b7573a90ada580577975` | `b478dbb` | rebase 后保留本地 B10A control endpoints |

提交 SHA 因 rebase 到治理 main 而变化；没有重复治理/B02 基线历史，也没有带入原始资产、媒体、模型、私密数据或本机绝对路径。

## 集成内容与冲突

- B09 新增 `media_state/`、media state schemas/examples、B09 contract 文档和 synthetic media tests。它只接受 B01 private manifest 的本机 path-reference，状态事件脱敏，不修改 `local_server.py`、B03 LLM、人格或视觉基线。
- B10A 新增声明式 `runtime/packaging/` skeleton、config/schema/manifest、mock service、CLI、scope verifier、packaging tests 和文档。它只管理自己的 marker/process/log ownership，不安装模型、不复制原版媒体、不读取用户数据、不调用 provider API。
- 共享 `.gitignore` 冲突已保留双方完整规则：B09 文档、B10A 文档、`.b10a/` 和 local config 忽略规则，同时保留 main 的 B03 文档放行规则。
- B10A scope allowlist 已纳入本集成审计文档，避免提交前的预期文档被误报为越界；未放宽任何功能目录边界。
- 没有修改 B03 LLM 逻辑、B01 原版视觉/private assets 或 main 中与本批无关的文件；`verify_b02_scope.py` 对此作了 exact baseline 检查。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| `rtk python -m pytest -q tests/media` | `8 passed`, `0 skipped` |
| `rtk python -m pytest -q tests/packaging` | `15 passed`, `0 skipped`；包含运行中卸载、异常/不健康进程卸载、重复启动与端口冲突生命周期断言 |
| `rtk python -m pytest -q -p no:cacheprovider tests/media tests/packaging` | `23 passed`, `0 skipped` |
| `rtk python -m pytest -q` | `111 passed`, `0 skipped`（含治理 PR #1 新增测试） |
| `rtk python -m pytest --collect-only -q` | `111 tests collected` |
| `rtk python -m compileall -q local_server.py http_contract.py media_state runtime tools tests` | exit 0 |
| `rtk python baseline_hardening_scan.py --root .` 及 6 个单模式扫描 | exit 0；tracked_files=89、runtime_python_files_checked=32；comments、runtime dependencies、secrets、sensitive paths、large files、evidence ignore 全部 `matches=0` |
| 候选新增/修改 tracked 文件绝对路径扫描 | `0 matches` |
| `rtk python tools/verify_b02_scope.py` | `status=PASS` |
| `rtk python tools/verify_B10A_scope.py` | `status=PASS` |
| `rtk python tools/verify_project_status.py` | `PASS`；links/statuses/absolute-paths/secrets/duplicate-active 全部通过 |
| `rtk python tools/healthcheck.py --profile core` | code 0，core `HEALTHY` |
| B10A `manifest` / `doctor` | code 0；manifest 正常，doctor 正确报告 skeleton `DEGRADED`（6 个 pending 模块未伪装为 healthy） |
| `rtk git diff --check github/main...HEAD` | exit 0 |

## 风险与边界

- 测试只使用 pytest synthetic fixtures 和 B10A mock process；没有播放真实原版音频/视频，没有人工听审、视觉逐帧验收、音画同步、真实 provider health、模型安装或 GPU/延迟验收。
- B09 的 manifest、媒体路径、哈希和运行时 asset reference 仍是本机 `.evidence/` 边界；B10A 的 pending modules 仍由后续批次实现。core health 绿不代表整产品完成。
- 本轮未修改或删除原版程序、资产、模型、用户数据、真实配置或原始信件；没有直接更新 `main`。发布动作仅针对本候选分支和指向 `main` 的非 draft PR。

## Reviewer verdict

`PASS` — the original independent `gpt-5.6-luna / max` reviewer re-checked the rebased final diff and confirmed governance PR #1 retention, the three replay mappings, no duplicate B02 baseline, `.gitignore`/docs/scope behavior, B09 cancellation/manifest/privacy boundaries, B10A lifecycle/localhost control behavior, and no sensitive leakage. The reviewer accepted the serial evidence: targeted `23 passed`, full `111 passed`, collect `111`, with compile, scanner, scope, health, and diff checks green.

# Baseline hardening evidence index

状态：`PASS`（本批 B00 hardening；不代表整产品验收通过）。

分支：`feature/baseline-hardening`

基线 `main`：`SANITIZED_ID`

本轮起点：`SANITIZED_ID`

`reviewed_range`：`SANITIZED_ID..SELF`

`implementation_head`：`SELF`（本轮实现与本索引同属待提交的原子变更，不在索引内自指最终 commit hash）。

`evidence_commit`：`SELF`（由包含本索引的 Git history 证明）。

合并状态：未合并，未推送远端。

## 本地证据位置

证据目录：`.evidence/baseline-hardening/run-20260812T133134/`

该目录由 `.gitignore` 排除，不进入 Git、分发包或远端。目录内只保留 UTF-8 测试日志、JUnit XML、收集清单、编译清单、staged diff 检查、脱敏扫描输出和 SHA-256 manifest；不含原版资产、模型、玩家数据、密钥、原始信件或正文。`manifest.sha256` 覆盖目录内除自身以外的 6 个文件。

`manifest.sha256` 的 SHA-256：`SANITIZED_ID`

pytest 临时目录：`.evidence/baseline-hardening/pytest-tmp-20260812T133134/`，仅为 synthetic test artifacts，可保留，不纳入上述 6 文件 manifest。

索引文件：

- `.evidence/baseline-hardening/run-20260812T133134/pytest.log`
- `.evidence/baseline-hardening/run-20260812T133134/pytest.junit.xml`
- `.evidence/baseline-hardening/run-20260812T133134/pytest-collect.log`
- `.evidence/baseline-hardening/run-20260812T133134/compile.txt`
- `.evidence/baseline-hardening/run-20260812T133134/git-diff-check.txt`
- `.evidence/baseline-hardening/run-20260812T133134/scan-commands.txt`
- `.evidence/baseline-hardening/run-20260812T133134/manifest.sha256`

## 本轮实现与扫描边界

- `local_server.py` 的注释明确为仅本地 toy API 兼容层、本地模型适配和脱机样例 fixture；不暗示官方服务、官方回复来源或历史官方快照。
- `test_cosyvoice3.py` 的注释改为模型输入格式，不再把输入格式称作官方格式。
- `baseline_hardening_scan.py` 提供 `all`、`comments`、`runtime-dependencies`、`secrets`、`sensitive-paths`、`large-files`、`evidence-ignore` 多模式；输出只含相对路径、行号、模式名、计数和退出状态。
- 误导性注释扫描覆盖中文/英文官方词及 `offical` 拼写变体、历史/快照词、中文和英文在线依赖（含 `dependency`）以及人格蒸馏完成语义；同时扫描 Python `#` 注释和模块/类/函数三引号 docstring。
- 官方运行时依赖扫描覆盖已知 host，以及 `official`/`capture official`/`download reply video`/`x token` 的下划线、连字符和空格分隔 request/poll/download/token 标记。
- 秘密扫描只报告相对路径、行号和模式名；敏感路径扫描覆盖 secret/token/credential、密钥、模型、媒体和压缩包路径；tracked large-file 阈值为 50,000,000 bytes。

## 命令与结果摘要

| 命令/产物 | 结果 |
|---|---|
| `rtk python -m pytest -q -p no:cacheprovider --basetemp .evidence/baseline-hardening/pytest-tmp-20260812T133134 --junitxml=...` | exit code 0；collected=29；passed=29；skipped=0；failed=0 |
| `rtk python -m pytest --collect-only -q -p no:cacheprovider --basetemp ...` | exit code 0；collected=29 |
| `rtk python -c "compile_all_tracked_python"` | exit code 0；compiled_files=7 |
| `rtk git diff --cached --check` | exit code 0；stdout/stderr 均为空 |
| `rtk python baseline_hardening_scan.py --root .` 及 6 个单模式命令 | 全部 exit code 0；误导注释、官方运行时依赖、秘密、敏感路径、大文件和 `.evidence/` ignore 均 matches=0 |
| `manifest.sha256` | 覆盖 6 个 UTF-8 证据文件；manifest hash=`SANITIZED_ID` |

四项扫描器回归已纳入 pytest：英文 `external dependency`、三引号 docstring 中的风险语义、空格分隔的 `official request/poll/download/token`、`capture official reply`、`download reply video`、`x token` 标记，以及进程级退出码 0/1/2 和脱敏命中输出。扫描器实际运行结果只保留脱敏后的零命中记录，不把合成源文本写入证据。

## 执行边界与未验证范围

本轮没有下载模型、启动外部服务、访问外部写接口或修改真实原版程序、配置、玩家数据、原始信件和原版资产；patch/extract 测试仍只使用 pytest 临时 synthetic fixture。没有合并 `main`、没有 push，也没有提交原版资产、模型、媒体、秘密或 `.evidence` 原始包。

AIRI、Mem0、Nemotron Streaming ASR、VoxCPM2、MOSS-TTS、LiveTalking/MuseTalk 的正式安装/接入/运行/卸载，原版视觉逐状态像素/SSIM/LPIPS/身份/闪烁/背景/色差/帧率/音画同步，RTX 3080 10 GB 显存与 P50/P95 延迟，Live 全链路、数据导入删除、版权/隐私权利、公开资料人格蒸馏和总控多模态观看均保持 `UNVERIFIED`。本批 hardening 测试全绿不等于整产品全绿。

## 审查结论

独立 Luna Max：`PASS`（gpt-5.6-luna / max；findings=none；允许提交）。本结论只覆盖本批 staged diff 与列明证据，不升级整产品的 `UNVERIFIED` 范围。

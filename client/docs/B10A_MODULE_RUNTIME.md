# B10A 模块装卸与本地运行骨架

状态：`SKELETON / NOT FINAL PACKAGE`（2026-08-12）。本批只提供声明式模块目录、配置/秘密边界、可逆本地状态、精确卸载 dry-run、进程生命周期契约和 mock service；B10B 才负责最终整包、锁版本、实机安装与总验收。

## 边界

`runtime/packaging/manifests/b10a.modules.json` 声明以下模块：

| 模块 | 本批状态 | 实际范围 |
|---|---|---|
| `core/http` | `available` | 发现既有 B02 HTTP 文件；可登记 marker，并可启动仅用于测试的 `mock-http` |
| `llm-api` | `pending` | 只有 OpenAI-compatible provider slot；B03 实现 gateway，B10A 不调用 API |
| `memory-local` | `pending` | 只有本地 slot；不读取/迁移/删除 `memory_store.json` |
| `asr-local` | `pending` | 不安装模型、不访问麦克风 |
| `tts-local` | `pending` | 不安装权重、不复制参考音频或生成媒体 |
| `visual-driver` | `pending` | 不安装 LiveTalking/MuseTalk，不复制原版视觉资产 |
| `media-original` | `pending` | 只声明未来 path-reference slot，不打包或删除原版媒体 |

`pending`/`unavailable` 永远不会被 `install` 当作成功；请求它们返回稳定错误。`doctor` 聚合状态会保留 `DEGRADED`，不会把未来模块伪装成健康。

## 配置与路径

命令默认使用当前项目目录和 `<project-root>/.b10a`。只有显式 `--data-root` 才能选择外部数据根；B10A 不创建 C: 缓存、不搜索用户目录、不改写原版安装目录。配置按以下层级合并：内置默认值 -> 项目根 `b10a.config.json` -> 指定数据根下的 `config/local.json` -> provider 环境槽位。

提交的示例与 schema 在 `runtime/packaging/config/` 和 `runtime/packaging/schemas/`。项目配置被忽略；本地配置位于数据根的 `config/local.json`（仓库规则也显式忽略该文件）。LLM 密钥优先且默认只从 `B10A_LLM_API_KEY` 读取；CLI、doctor、state、marker 和 mock 日志都不会输出秘密值。项目层直接写 `api_key`/`token`/`secret` 会被拒绝。

## 命令

以下命令均可在 Windows PowerShell 运行：

```powershell
rtk python tools/B10A_cli.py --project-root . manifest
rtk python tools/B10A_cli.py --project-root . install --module core/http
rtk python tools/B10A_cli.py --project-root . doctor
rtk python tools/B10A_cli.py --project-root . start --service mock-http --port 8780
rtk python tools/B10A_cli.py --project-root . stop --service mock-http
rtk python tools/B10A_cli.py --project-root . uninstall --module core/http
rtk python tools/B10A_cli.py --project-root . uninstall --module core/http --apply
rtk python tools/B10A_cli.py --project-root . rollback --module core/http
```

`uninstall` 默认只输出 dry-run 计划。`--apply` 也只删除 manifest 的 `ownership.owned_paths` 下的常规文件，并要求对应进程已停止；它不删除项目源代码、原版客户端/媒体、导入信件、用户数据、外部模型或缓存。路径必须是相对数据根的普通文件；绝对路径、`..`、symlink/reparse point 和 ownership 冲突都会被拒绝。

安装和升级只写 module marker 与 B10A 状态。升级先保存状态/owned files 快照；`rollback` 会在发现 managed file 被用户改动时拒绝覆盖。卸载同样写可回滚事务，重新 `install` 或 `rollback` 可恢复 B10A marker；B10A 不承诺恢复用户在其 ownership 边界外的文件。

## 生命周期契约

本批只允许声明的内置 `mock-http` 进程：不接受 shell command，不拼接用户命令，不使用任意 `taskkill`。启动前检查端口，启动后检查 `/health`；重复启动返回 `ALREADY_RUNNING`，端口占用返回 `PORT_CONFLICT`。state 只记录 B10A 自己生成的 PID、nonce、identity path 和 log path；停止前必须匹配 identity，异常退出会显示 `ABNORMAL_EXIT`，PID 不匹配则拒绝控制。

这不是 B02 `local_server.py` 的替代品，也不是 LLM/ASR/TTS/Live 实机服务。mock service 只用于验证本地进程、端口、健康聚合、重复启动、异常退出与脱敏日志契约。

## 验收边界

自动化测试覆盖：安装/卸载 roundtrip、idempotency、dry-run、upgrade/rollback、依赖、dirty config、路径逃逸、Windows 空格/Unicode 路径、端口冲突、重复启动、异常退出、健康聚合、秘密脱敏和 scoped ownership。测试只在 pytest 临时目录启动 mock service。

范围检查：`rtk python tools/verify_B10A_scope.py`。它只允许本批声明的 runtime/packaging、测试、工具、模板、文档和 `.gitignore` 行，发现越界路径即失败。

本批视觉证据：`N/A`；没有读取、复制或生成原版视觉媒体。听觉证据：`N/A`；没有安装或运行 ASR/TTS。B10B 仍需在干净环境执行锁版本安装、真实 provider health、设备/模型资源检查、回滚/卸载实机验收，并由总控依据 `docs/ACCEPTANCE.md` 放行。

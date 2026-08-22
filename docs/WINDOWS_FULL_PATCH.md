# Windows 完整版补丁（隔离安装）

本补丁只复制并修改用户自己的正版 Steam 文件副本，不写入正版目录，也不分发原版游戏、模型、运行时或媒体。安装目标默认是 `%LOCALAPPDATA%\BSideOliviaLocal\install`，用户数据和未来外部缓存保留在同一产品目录的 `data` / `third-party` 下。

## 使用

1. 将发布 ZIP 解压到任意目录。
2. 双击 `INSTALL.cmd`。它始终使用 `%LOCALAPPDATA%` 下受管的 Python 3.12 embeddable runtime；首次使用会显示 Python.org 固定来源、SHA-256 和 PSF 许可证，并在明确同意后下载。安装器从 Steam AppID `4532590` 的 appmanifest 自动发现正版目录。
3. 安装成功后双击 `START.cmd`。它只启动隔离副本的本地 HTTP server，再直接启动隔离副本的 `0.0.9.615\Olivia.exe`，并使用安装目录下的独立 profile。
4. 首次配置可双击 `CONFIGURE.cmd`：API key 输入不回显，并以当前 Windows 用户 DPAPI 加密保存到 `data\config`；也可选择自己的参考音频/视频导入 `data\third-party\reference`。这些文件不进入补丁包。
5. 卸载双击 `UNINSTALL.cmd`。受控卸载只删除安装器自己写入的 `app`、`local_backend`、启动脚本和 marker，保留 `data`、`logs`、`third-party`。

安装前会校验客户端版本 `0.0.9.615` 与 `feapp.dat` SHA-256；校验不匹配或目标目录已有未知内容时停止。补丁会在隔离副本中保留 `feapp.dat.orig`，正版 Steam 目录不会产生备份、补丁或写入。

## 当前能力边界

- 文字回信：接入 main 的本地 HTTP 契约；LLM 未配置时返回真实 `UNAVAILABLE`，不伪造成功。
- 视频回信、音乐视频：已迁入情绪分流、普通视频 delivery、LatentSync、音乐内容/渲染与 MiniMax Music 3 worker 的调用边界；实际 TTS、视觉、分离和音乐模型均为外部依赖。高情绪来信进入串行后台媒体任务，依赖缺失时明确返回 `UNAVAILABLE`，不会伪造媒体 URL。
- Live：暂停，manifest 标记为 `UNAVAILABLE_PAUSED`，不会注入 Live 入口。
- 第三方 Python/runtime/model：不随补丁分发。请使用 `docs/THIRD_PARTY_DOWNLOADS.md` 的官方来源页；下载清单默认空、下载器默认 dry-run，必须明确接受许可证后才允许写入仓库外 data root。

embeddable Python 仅解决解释器获取；`aiohttp`、TTS、视觉、音频分离和音乐模型仍需按第三方下载清单单独准备。缺失时 `START.cmd` / 本地 health 会报告不可用原因；不会自动下载未固定来源、许可证或 SHA-256 的内容。用户 API key 只从启动进程环境或 `CONFIGURE.cmd` 生成的当前用户 DPAPI 文件读取，不写入安装包或日志；DPAPI 解密值只存在于后端子进程环境中。
启动时若没有 `OLIVIA_LLM_API_KEY`、`DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`，命令行会明确提示未配置，不能把 safe-static 回退误认为真实模型回信。

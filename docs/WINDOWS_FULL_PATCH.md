# Windows 完整版补丁（隔离安装）

本补丁只复制并修改用户自己的正版 Steam 文件副本，不写入正版目录，也不分发原版游戏、模型、运行时或媒体。安装目标默认是 `%LOCALAPPDATA%\BSideOliviaLocal\install`，用户数据和未来外部缓存保留在同一产品目录的 `data` / `third-party` 下。

## 使用

1. 将发布 ZIP 解压到任意目录。
2. 双击 `INSTALL.cmd`。它始终使用 `%LOCALAPPDATA%` 下受管的 Python 3.12 embeddable runtime；首次使用会显示 Python.org 固定来源、SHA-256 和 PSF 许可证，并在明确同意后下载。安装器从 Steam AppID `4532590` 的 appmanifest 自动发现正版目录。
3. 安装成功后双击 `START.cmd`。它只启动一个监听 `127.0.0.1` 的本机服务，再直接启动隔离副本的 `0.0.9.615\Olivia.exe`，并使用安装目录下的独立 profile。长期记忆、PrivateWorld 和媒体接口都挂在同一个进程中。
4. 首次配置可双击 `CONFIGURE.cmd`：API key 输入不回显，并以当前 Windows 用户 DPAPI 加密保存到 `data\config`；也可选择自己的参考音频/视频导入 `data\third-party\reference`。这些文件不进入补丁包。
5. 卸载双击 `UNINSTALL.cmd`。受控卸载只删除安装器自己写入的 `app`、`local_backend`、启动脚本和 marker，保留 `data`、`logs`、`third-party`。

## 启动器健康检查

`installer/start_local.py --health-only` 输出一行符合 [`contracts/launcher_health.schema.json`](../contracts/launcher_health.schema.json) 的 JSON。`READY` 的退出码为 0；`UNAVAILABLE` 与 `PORT_CONFLICT` 的退出码为 2。`PORT_CONFLICT` 表示端口已有非本契约监听器或返回了无效健康契约，启动器不会尝试启动第二个后端。

## 原版客户端补丁

安装前同时校验：

```text
客户端版本：0.0.9.615
feapp.dat SHA-256：
53babcf288c7679a57eb4a2647397d951ec450d5fdaea634498286b2ebb8136e

webplayer.dat SHA-256：
504b59876af2f04c4902f8c8e6811018d36a2da4394e20cf74f22d13d394b636
```

任一文件缺失或哈希不匹配时，安装在写入目标目录前停止。

安装器只在隔离副本中按顺序执行：

```text
feapp.dat
  1. toyApiUrl / toyWsUrl 指向本机服务
  2. 默认进入原版 Collection
  3. 在原版 Settings 内加入“本地陪伴”界面

webplayer.dat
  4. 保留原版 uid 播放路径
  5. 只为明确的 loopback /toy/media/ 地址启用本机视频播放
```

原版主包之外，设置界面只增加一个仓库自有 bootstrap；原版 `assets/main-917d29fc.js` 的业务代码除既有端点和 Collection 锚点外不做模糊替换。播放器的普通原版路径继续加载未修改的原模块。

隔离副本保留：

```text
feapp.dat.orig
feapp.dat.companion.orig
webplayer.dat.orig
```

正版 Steam 目录不会产生备份、补丁或写入。任何补丁、验证或重打包步骤失败时，整个未完成安装目录会被删除，不留下部分可用的客户端。

已有旧 marker 但缺少原版设置或 webplayer 本机媒体标记时，不会伪装成“已经安装”；安装器会停止并要求先按受控流程处理旧安装。

## 当前能力边界

- 原版 UI：用户日常只打开原版 Olivia。原版 Settings 内可读取长期记忆、PrivateWorld 和待确认建议；不发布独立浏览器 Control Center 或第二个桌面入口。
- 文字回信：接入 main 的本地 HTTP 契约；LLM 未配置时返回真实 `UNAVAILABLE`，不伪造成功。
- 视频回信、音乐视频：已迁入情绪分流、普通视频 delivery、LatentSync、音乐内容/渲染与 MiniMax Music 3 worker 的调用边界；实际 TTS、视觉、分离和音乐模型均为外部依赖。依赖缺失时明确返回 `UNAVAILABLE`，不会伪造媒体 URL。完成的本机 MP4 通过原版 webplayer 播放。
- Live：暂停，manifest 标记为 `UNAVAILABLE_PAUSED`，不会注入 Live 入口。
- 第三方 Python/runtime/model：不随补丁分发。请使用 `docs/THIRD_PARTY_DOWNLOADS.md` 的官方来源页；下载清单默认空、下载器默认 dry-run，必须明确接受许可证后才允许写入仓库外 data root。

embeddable Python 仅解决解释器获取；`aiohttp`、TTS、视觉、音频分离和音乐模型仍需按第三方下载清单单独准备。缺失时 `START.cmd` / 本地 health 会报告不可用原因；不会自动下载未固定来源、许可证或 SHA-256 的内容。用户 API key 只从启动进程环境或 `CONFIGURE.cmd` 生成的当前用户 DPAPI 文件读取，不写入安装包或日志；DPAPI 解密值只存在于后端子进程环境中。

启动时若没有 `OLIVIA_LLM_API_KEY`、`DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`，命令行会明确提示未配置，不能把 safe-static 回退误认为真实模型回信。

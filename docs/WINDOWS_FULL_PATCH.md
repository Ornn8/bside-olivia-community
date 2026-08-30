# Windows 完整版补丁（隔离安装）

本补丁只复制并修改用户自己的正版 Steam 文件副本，不写入正版目录，也不分发原版游戏、可选模型或媒体。`Olivia-Setup-x64.exe` 内含固定版本的核心 Python 运行时和依赖，使首次安装不依赖 Python.org、PyPI 或 Hugging Face。安装目标默认是 `%LOCALAPPDATA%\BSideOliviaLocal\install`，用户数据和未来外部缓存保留在同一产品目录的 `data` / `third-party` 下。

## 使用

1. 下载 `Olivia-Setup-x64.exe` 及同目录的 `.sha256` 文件，并先核对 SHA-256。
2. 双击 EXE，选择产品目录和正版 Steam 游戏目录。安装器按当前用户运行，不要求管理员权限；它在产品目录内分别创建 `install` 与 `runtime`，从 EXE 内置的离线资产安装受管 Python 3.12 runtime 和固定 wheel，不联网下载。正版目录可留空并按 Steam AppID `4532590` 自动发现。
3. 安装成功后可从开始菜单的“Olivia 本地版”快捷方式启动；安装时勾选“创建桌面快捷方式”后也可从桌面启动。两个快捷方式都由 `%WINDIR%\System32\wscript.exe //B //Nologo "<安装目录>\START.vbs"` 隐藏启动，不会显示可被误关的命令行窗口；工作目录固定为安装目录。点击后会立即显示“Olivia 正在启动，请稍候”的短暂提示，实际启动同时开始，不会等待提示关闭。只有隐藏启动器最终返回非零退出码时才显示中文失败对话框；正常关闭返回 `0` 时不会弹出错误。`START.cmd` 仍保留为兼容入口。它只启动一个监听 `127.0.0.1` 的本机服务，再直接启动隔离副本的 `0.0.9.627\Olivia.exe`，并使用安装目录下的独立 profile。
4. 首次登录后在原版客户端内完成 LLM key 与按需能力设置；后续在 Settings 的“本地陪伴”中管理。`CONFIGURE.cmd` 仍可用于管理参考音频/视频。这些文件不进入补丁包。
5. 卸载双击 `UNINSTALL.cmd`。受控卸载只删除安装器自己写入的 `app`、`local_backend`、启动脚本和 marker，保留 `data`、`logs`、`third-party`。

兼容旧流程：源码/调试场景仍可解压发布内容后双击 `INSTALL.cmd`。EXE 只是图形化外壳，最终仍调用同一份 `installer/Install.ps1`，不会形成第二套安装逻辑。安装阶段不填写 API key，也不会下载 Mem0、BGE 或其他可选模型。

## 离线核心资产

发布包的 `offline/offline-core-assets.json` 固定 Python 3.12.10、pip 25.2 和 `installer/runtime-requirements.txt` 的 14 个 Windows x64 / CPython 3.12 wheel。安装前逐项校验路径、大小和 SHA-256，并要求 wheelhouse 与清单完全一致；缺失、多余或被修改的 wheel 都会使安装失败。pip 只使用 `--no-index --find-links` 读取本地 wheelhouse。

Python ZIP 内含 PSF `LICENSE.txt`；pip 和各依赖 wheel 内含各自的 `.dist-info/licenses` 或等价许可证文件，安装后也保留在受管 runtime 中。发布人员联网生成资产目录：

```powershell
python installer/build_offline_core_assets.py --output offline
```

构建器只接受哈希锁定的二进制 wheel；pip 源仍兼容标准 `PIP_INDEX_URL` / pip 配置，因此发布构建可使用可信镜像，最终产物仍按仓库固定 SHA-256 验证。公开清单契约见 `contracts/offline_core_assets.schema.json` 和 `contracts/offline_core_assets.example.json`。

## 构建单文件安装器

构建机需要 Python 3.12、`jsonschema` 和 Inno Setup 6.7.1 或兼容的新版本。先生成离线核心资产，再执行：

```powershell
python installer/build_windows_setup.py `
  --offline offline `
  --output dist `
  --version 0.1.0 `
  --iscc 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

构建器只复制 Git 已跟踪且相对 `HEAD` 未修改的发布文件，排除 `.github`、`docs`、测试和构建/CI 元数据，并在编译前复验离线 manifest、requirements 哈希、每个资产的大小与 SHA-256，以及实际资产集合。输出为 `Olivia-Setup-x64.exe` 和对应 `.sha256` 文件。

GitHub Actions 会在 PR 和 `main` 更新时生成同样的可下载 artifact。当前产物未做商业代码签名；正式面向普通用户发布前应增加受信任的 Authenticode 签名，但签名不替代随包 SHA-256 校验。

## 启动器健康检查

`installer/start_local.py --health-only` 输出一行符合 [`contracts/launcher_health.schema.json`](../contracts/launcher_health.schema.json) 的 JSON。`READY` 的退出码为 0；`UNAVAILABLE` 与 `PORT_CONFLICT` 的退出码为 2。`PORT_CONFLICT` 表示端口已有非本契约监听器或返回了无效健康契约，启动器不会尝试启动第二个后端。正常启动还会核对 health 中不含路径的 `backend_id`，它同时绑定活动组件与当前安装实例；版本或安装实例不匹配时不会复用该服务。首个组件补丁遇到旧版遗留服务时，只有在 Windows 确认监听进程是当前产品目录自带 runtime 的 Python 后才会终止并启动新版本；无法证明归属时以 `STALE_BACKEND_RUNNING` 停止，不会结束其他程序。启动器自己创建的后端随 Olivia 客户端退出而结束。

客户端启动协议以整个隔离 profile 目录为边界：启动器在创建 `profile/` 前记录该目录是否已存在，只有此前不存在才视为全新 profile。客户端始终以安装目录下的 `app/` 为工作目录。若且仅若全新 profile 的第一次退出码为 `0x0E000003`，启动器会在同一后端生命周期内重启客户端一次，因此客户端最多启动两次；已有 profile 或任何其他退出码均不重试，第二次退出码原样透传。每次启动和退出分别记录不含路径的 `client_start` 与 `client_exit`，并以 `attempt` 标记第 1 或第 2 次，退出记录只附整数退出码；触发重试时另记 `client_retry`，其固定原因为 `known_fresh_profile_exit`。这些诊断不记录安装路径、profile 路径、环境变量或凭据。同一安装目录的稳定启动器只允许一个启动生命周期；首个 Olivia 尚在运行时重复点击会记录 `launch_already_running` 并立即结束第二个启动器，不会重复启动后端或客户端。不同安装目录互不阻塞，进程异常退出后自动释放该启动占用。若启动器无法创建该占用，则记录 `launch_lock_unavailable`，输出稳定错误码 `START_LOCK_UNAVAILABLE` 并以退出码 `2` 结束，不会继续启动后端或客户端。Windows 稳定启动器还使用 Job Object 归属本次启动的后端、客户端及其子进程；启动器正常或异常退出时由系统结束整棵子进程树，不进行宽泛进程清理。若无法建立该归属，则记录 `launch_job_unavailable`，输出稳定错误码 `START_JOB_UNAVAILABLE` 并以退出码 `2` 结束。

## 原版客户端补丁

安装前同时校验：

```text
客户端版本：0.0.9.627
feapp.dat SHA-256：
c88f1dd4cb7c95e4902d74dd0c247962ffd65559e3907497b416078d3a6698b5

webplayer.dat SHA-256：
565b5e3e113c2a9dfb90d5fa4f2a0ccda9b0151c118ae3365e6ee0c8624a451d
```

文件缺失时安装在写入目标目录前停止。自动发现多份 Steam 副本时，安装器优先选择上述双 SHA-256 精确匹配的第一份；仅发现一份非精确匹配副本时，由 Python 补丁器继续校验 ZIP 结构与补丁锚点。多份副本均不精确匹配时返回 `OFFICIAL_INSTALL_AMBIGUOUS`，用户必须返回上一步明确选择正版目录。

安装器只在隔离副本中按顺序执行：

```text
feapp.dat
  1. toyApiUrl / toyWsUrl 指向本机服务
  2. 默认进入原版 Collection
  3. 在原版 Settings 内加入“本地陪伴”界面

webplayer.dat
4. 保留原版 uid 播放路径
5. 提供可选的显式 `uid` 本机回退，只为明确的 loopback `/toy/media/` 地址启用本机视频播放
```

原版主包之外，设置界面只增加一个仓库自有 bootstrap；原版 `assets/main-31595bd3.js` 的业务代码除既有端点和 Collection 锚点外不做模糊替换。播放器的普通原版路径继续加载未修改的原模块。

隔离副本保留：

```text
feapp.dat.orig
feapp.dat.companion.orig
webplayer.dat.orig
```

正版 Steam 目录不会产生备份、补丁或写入。任何补丁、验证或重打包步骤失败时，整个未完成安装目录会被删除，不留下部分可用的客户端。

已有旧 marker 但缺少原版设置或 webplayer 本机媒体标记时，不会伪装成“已经安装”；安装器会停止并要求先按受控流程处理旧安装。

## 安装源诊断契约

单文件安装器失败时，会把 `olivia.setup-source-diagnostic.v1` JSON 写入本机安装日志。该诊断只包含 `selection_mode`、`candidate_count`、`client_version`、`manifest_feapp_sha256`、`manifest_webplayer_sha256`，以及 `observed_feapp_size`、`observed_feapp_sha256`、`observed_webplayer_size`、`observed_webplayer_sha256`。选中目录仅以 `selected_official_id`（规范化目录路径 SHA-256 的前 16 位）标识；日志不得包含原始正版目录路径。自动发现歧义时，`candidates` 按发现顺序记录 `candidate_index` 与上述观测 size/hash，无法读取的值为 `null`。

稳定安装错误码 `OFFICIAL_INSTALL_AMBIGUOUS` 表示自动发现了多份候选，但没有任何候选同时匹配内嵌 manifest 的 `feapp.dat` 与 `webplayer.dat` SHA-256。该错误不可自动重试；应返回上一步显式选择 Steam 正版目录。

## 当前能力边界

- 原版 UI：用户日常只打开原版 Olivia。原版 Settings 内可读取长期记忆、PrivateWorld 和待确认建议；不发布独立浏览器 Control Center 或第二个桌面入口。
- 文字回信：接入 main 的本地 HTTP 契约；LLM 未配置时返回真实 `UNAVAILABLE`，不伪造成功。
- 视频回信、音乐视频：已迁入情绪分流、普通视频 delivery、LatentSync、音乐内容/渲染与 MiniMax Music 3 worker 的调用边界；实际 TTS、视觉、分离和音乐模型均为外部依赖。依赖缺失时明确返回 `UNAVAILABLE`，不会伪造媒体 URL。完成的本机 MP4 默认通过 Collection 内的 `BaseVideo` 展示；`webplayer` 仅保留为可选的显式 `uid` 本机回退，不替代书信编排路线。
- Live：暂停，manifest 标记为 `UNAVAILABLE_PAUSED`，不会注入 Live 入口。
- 核心 Python/runtime：随发布包离线分发并在安装前完整校验，不在用户安装时访问外网。
- 可选模型与扩展依赖：不随核心安装器分发。长期记忆的 Mem0 runtime 和 BGE 模型已从首装移除，后续由登录后的初始设置按需安装，并可在客户端 Settings 中管理。当前版本缺失时明确降级为 `UNAVAILABLE`。

离线核心包含本地服务启动所需的 `aiohttp`、`jsonschema` 及其锁定依赖；TTS、视觉、音频分离、音乐和长期记忆模型仍需按第三方下载清单单独准备。缺失时 `START.cmd` / 本地 health 会报告不可用原因；不会自动下载未固定来源、许可证或 SHA-256 的内容。用户 API key 只从启动进程环境或登录后的初始设置生成的当前用户 DPAPI 文件读取，不写入安装包或日志；DPAPI 解密值只存在于后端子进程环境中。初始设置 API 仅接受原版客户端明确配置的 HTTPS Origin，所有写操作还要求本次成功登录生成的随机 session token；任意本机端口网页不能调用。留空 key 只可复用当前已保存地址和模型的密钥，修改目标地址时必须重新输入。每次保存先写入新的版本化 DPAPI 文件，再用一次原子替换发布同时绑定 provider、模型、密钥文件名和密文 SHA-256 的 `llm.json`；配置损坏或绑定不匹配时启动器关闭 provider，不把旧 key 发送到默认服务。

保存或删除 LLM 设置后，当前版本会明确返回 `restart_required: true`。原因是回复、情绪分流和私人世界分析在进程启动时共同捕获同一 provider graph；进程内热替换会让并发任务跨两个 provider 状态运行。关闭并重新打开 Olivia 后，稳定启动器会加载新的公开配置与 DPAPI 密钥。公开路由、状态及错误码契约见 `contracts/initial_setup_api_contract.json` 和对应 schema。

启动时若没有 `OLIVIA_LLM_API_KEY`、`DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`，命令行会明确提示未配置，不能把 safe-static 回退误认为真实模型回信。

## 本地组件补丁与回滚

安装后的 `START.cmd`、`CONFIGURE.cmd` 和 `UNINSTALL.cmd` 只调用安装根目录中的稳定启动器 `launcher/version_launcher.py`。稳定启动器读取一次 `.olivia-update-state.json` 原子指针，从 `versions/local_backend/<version>-<manifest-sha256>` 选择完整后端；没有更新状态时继续使用初装的 `local_backend`。因此更新期间不会把正在使用的后端目录临时移走，也不会要求用户重新运行完整安装器。

当前版本只提供经过外部渠道取得的本地 `.oliviapatch` 文件安装，不联网检查或下载更新。包必须是 ZIP，并且只能包含一个 `manifest.json` 和清单声明的 `payload/...` 普通文件。`local_backend` 包必须包含 `installer/start_local.py`、`installer/configure.py` 和 `installer/uninstall.py` 三个普通文件入口，否则不会激活。公开契约及示例见：

普通用户在客户端 Settings 的“补丁更新”中选择已下载的 `.oliviapatch`，粘贴发布页提供的 Manifest SHA-256 后安装；成功后关闭并重新打开 Olivia。该页面也提供回滚到上一版本的入口。命令行入口继续保留用于维护和故障排查。

状态指针成功切换后，更新器会以 best-effort 方式发现桌面和开始菜单中仍然存在的快捷方式，并把图标刷新到当前激活版本。属于当前安装、仍指向旧 `START.cmd` 的快捷方式会同时迁移为 `%WINDIR%\System32\wscript.exe //B //Nologo "<安装目录>\START.vbs"` 隐藏启动；已经采用该隐藏入口的当前安装快捷方式只刷新图标。其他安装或无关的 wscript 快捷方式不会被改写。路径发现失败、PowerShell 不可用或启动失败、执行超时、非零退出以及单个快捷方式保存失败，都不会撤销已经完成的补丁激活，也不会把成功更新改报为失败。

客户端补丁入口的鉴权、请求、响应、重启语义和稳定错误码见 `contracts/local_update_api_contract.json` 及其 schema。

- `contracts/component_update_package.schema.json`
- `contracts/component_update_package.example.json`
- `contracts/component_update_state.schema.json`
- `contracts/component_update_state.example.json`

维护者应从干净、已冻结的 Git 提交生成补丁及两个独立摘要文件：

```powershell
python -m installer build-update --source <源码目录> --output <发布目录>\olivia-local-backend-<版本>.oliviapatch --version <版本> --source-commit <40位提交SHA>
```

命令拒绝 tracked 文件有改动或 HEAD 与 `--source-commit` 不一致的源码，并生成 `.manifest.sha256`（供客户端安装页粘贴）和 `.sha256`（用于核对整个补丁文件）。相同源码提交和版本会生成字节一致的包。

安装命令：

```powershell
python -m installer apply-update --installation <安装目录> --package <补丁.oliviapatch> --manifest-sha256 <64位小写SHA-256>
```

`--manifest-sha256` 是包外信任锚，必须来自经过认证的发布元数据或用户已验证的官方发布页面，不能从同一个补丁包内自行读取后当作可信值。当前实现不包含签名验证或在线发布元数据获取；自动下载器接入前必须保持这条边界。

每个 payload 文件还会按清单校验大小和 SHA-256。路径会按 Windows 规则拒绝目录穿越、大小写别名、尾随点/空格、ADS、设备名、符号链接和 reparse point；解包完成后会重新枚举暂存目录并逐文件复验。校验成功后先发布不可变版本目录，最后仅用一次原子替换切换状态指针。指针替换失败时，旧指针或初装后端仍可启动，新目录只是未激活版本。

第一次更新会把初装 `local_backend` 记录为 `0.0.0+legacy` 回滚基线；后续更新记录上一个活动版本。重复应用当前活动版本不会覆盖真正的上一版本。可原子交换活动/上一版本指针：

```powershell
python -m installer rollback-update --installation <安装目录>
```

CLI 成功时输出一行 JSON 并返回 `0`；失败时输出 `{"status":"ERROR","code":"..."}` 并返回 `2`。稳定错误码包括：`UPDATE_INSTALLATION_INVALID`、`UPDATE_MANIFEST_DIGEST_INVALID`、`UPDATE_MANIFEST_DIGEST_MISMATCH`、`UPDATE_MANIFEST_INVALID`、`UPDATE_PACKAGE_INVALID`、`UPDATE_PAYLOAD_DIGEST_MISMATCH`、`UPDATE_STAGED_TREE_MISMATCH`、`UPDATE_COMPONENT_UNSUPPORTED`、`UPDATE_COMPONENT_UNAVAILABLE`、`UPDATE_VERSION_CONFLICT`、`UPDATE_STATE_INVALID`、`UPDATE_ROLLBACK_UNAVAILABLE` 和 `UPDATE_ACTIVATION_FAILED`。

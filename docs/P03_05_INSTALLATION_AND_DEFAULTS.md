# P03-05 Windows 安装、默认配置与健康检查

## 1. 目标

让普通用户在一台干净 Windows 机器上完成安装和配置后，不需要打开 CMD、PowerShell、Python 或手工拼接环境变量，就能启动稳定的文字书信陪伴系统；已安装完整本机媒体组件时，可继续使用包含自然说话段和约 60 秒音乐段的视频回信。

安装器必须区分：

- 核心必需能力；
- 默认本地持久化；
- 可选长期记忆；
- 可选完整视频回信；
- 本地 Companion Control Center；
- 实验模块。

任何可选组件缺失都不能伪造 READY，也不能阻止核心文字回复。

## 2. 用户体验决策

正式用户流程全部图形化：

```text
双击 INSTALL.cmd
  -> 安装核心文件
  -> 启动本地 Setup Wizard
  -> 浏览器打开本地界面
  -> 配置主模型、记忆和媒体
  -> 运行检查
  -> 创建 Windows 快捷方式
```

用户日常只需要：

- `Start Olivia`；
- `Olivia Control Center`；
- `Olivia Diagnostics`；
- `Uninstall Olivia`。

不得把 CLI、JSON 文件或环境变量作为普通用户的完成路径。`--non-interactive` 等参数只保留给 CI、自动安装和维护测试。

## 3. 现有问题

当前 `installer/start_local.py`：

- 会创建 `data` 和 memory root；
- 会设置本地 HTTP、模型和回复延迟；
- 默认配置较多依赖进程环境；
- 没有默认设置 PrivateWorld DB；
- 没有 Mem0 安装和模型缓存流程；
- 媒体配置主要依赖外部环境变量；
- core health 只证明 HTTP 基线，不证明真实模型、记忆和媒体可运行；
- 配置向导主要处理单个 DeepSeek key 和参考文件；
- 配置过程仍偏命令行，不适合最终用户。

## 4. 默认目录布局

```text
<install-root>/
├─ app/                         # 隔离复制的原版客户端
├─ local_backend/               # 本仓库运行时代码
├─ profile/                     # 隔离客户端用户目录
└─ data/
   ├─ state/
   │  ├─ letters.json
   │  └─ backups/
   ├─ archive/
   │  └─ legacy_letters.sqlite3
   ├─ memory/
   │  ├─ mem0/
   │  │  ├─ qdrant/
   │  │  └─ history/
   │  └─ model-cache/
   ├─ private_world/
   │  └─ private_world.sqlite3
   ├─ media/
   ├─ config/
   │  ├─ runtime.json
   │  ├─ memory.json
   │  ├─ media.json
   │  ├─ control-center.json
   │  └─ secrets/
   ├─ control/
   │  ├─ sessions/
   │  └─ calibration/
   ├─ acceptance/
   ├─ logs/
   ├─ third-party/
   └─ exports/
```

旧版本使用 `data/state.json` 和旧 memory SQLite 时，首次启动执行无损迁移。迁移前保留备份。

## 5. 配置优先级

```text
自动化显式参数
    > 环境变量覆盖
    > data/config/*.json
    > 安全默认值
```

普通用户通过 Setup Wizard 和 Control Center 编辑配置。配置文件不保存明文 API key；凭据使用 Windows DPAPI，运行时只在当前进程内解密。

## 6. 核心配置

`runtime.json` 示例：

```json
{
  "schema_version": 1,
  "port": 8899,
  "control_port": 8900,
  "reply_delay": {
    "enabled": true,
    "minimum_minutes": 5,
    "maximum_minutes": 10
  },
  "llm": {
    "provider": "openai_compatible",
    "base_url": "https://api.deepseek.com",
    "model": "configured-model",
    "api_key_secret": "llm-primary",
    "timeout_seconds": 30,
    "max_retries": 0,
    "stream": true
  },
  "persona_v2_enabled": true,
  "private_world_enabled": true,
  "memory_enabled": true,
  "control_center_enabled": true
}
```

安装器不永久写死可能变化的模型名称。Setup Wizard 可以提供建议值，但最终保存用户选择。

## 7. 安装模式

### 7.1 Core

必需：

- Python 3.12 venv；
- 本仓库 runtime；
- aiohttp 和核心依赖；
- 原版客户端隔离复制与 patch；
- Persona 文件；
- 当前信件持久化；
- PrivateWorld SQLite；
- Companion Control Center 静态资源；
- health、start、uninstall。

Core 不下载大型模型。

### 7.2 Memory

可选安装 extra：

```text
mem0ai
qdrant-client
sentence-transformers
锁定的 embedding 模型
```

Setup Wizard 必须：

- 展示下载内容和预计体积；
- 下载到 `data/memory/model-cache`；
- 固定 revision 或校验哈希；
- 允许跳过；
- 下载失败时保持 Core 可用；
- 不在服务首次启动时隐式联网下载。

### 7.3 说话与口型基础组件

检测：

- TTS config；
- CosyVoice runtime；
- 参考声音；
- LatentSync Python；
- LatentSync root、config 和 checkpoint；
- morning/day/dusk/night 场景视频；
- FFmpeg。

这只是完整视频回信的内部基础阶段，不是用户可选或可独立投递的回复模式。

### 7.4 完整视频回信

在说话与口型基础组件上额外检测：

- MiniMax Music 3 ComfyUI runtime；
- MiniMax worker；
- MiniMax model 文件；
- 已选择的 Caption/CFG/seed profile；
- RoFormer executable；
- performance base video；
- 可选官方转场；
- `concat_videos()` 拼接链依赖。

完整能力定义为：

```text
自然说话段
  + MiniMax 音频
  + RoFormer vocals
  + 演唱视频 LatentSync
  + 可选静音转场
  + FFmpeg 顺序拼接
```

只有完整链可运行才报告 `musical_video=available`；缺少音乐段必需组件时不退化为纯说话视频。

## 8. 图形化 Setup Wizard

Setup Wizard 运行在本地 Control Center 的 setup 模式，不连接外部 UI 服务。

页面顺序：

```text
1. 欢迎与数据目录
2. 主模型 endpoint、模型和 API key
3. Persona READY 检查
4. PrivateWorld 数据库创建
5. Mem0 与 embedding 安装选择
6. 说话视频组件检查
7. MiniMax / RoFormer / 演唱视频组件检查
8. 音乐校准状态
9. 数据和隐私摘要
10. 静态验证
11. 可选真实 provider probe
12. 完成与快捷方式
```

要求：

- API key 输入不回显；
- probe 不输出 provider 原始响应；
- 所有文件路径通过文件选择器指定并验证；
- 不自动扫描整个磁盘、Steam 目录或用户文档；
- 只复制用户显式选择的第三方参考文件；
- 重新运行时保留未修改项；
- 每一步可返回修改；
- 可选组件可跳过；
- 页面明确解释“可用、降级、未配置”的区别；
- 自动化保留 `--non-interactive --config <file>`，但不暴露给普通用户。

## 9. 启动器

`installer/start_local.py` 调整：

1. 读取并验证配置；
2. 创建所有默认数据目录；
3. 设置进程级环境；
4. 解密凭据；
5. 执行轻量 migration；
6. 启动兼容 API 和 Control Center 管理站点；
7. 等待 `/health?profile=core`；
8. 可选检查 `/health?profile=companion`；
9. 启动隔离客户端；
10. 客户端退出后不误杀仍在处理的媒体任务；
11. 提供图形化“安全停止”操作。

使用 PID/lock 文件防止同一数据目录启动两个后端实例。

## 10. Windows 快捷方式

安装后创建开始菜单项：

### Start Olivia

- 使用无控制台窗口启动；
- 启动后端和原版客户端；
- 不自动打开 Control Center。

### Olivia Control Center

- 检查后端；
- 创建一次性管理 bootstrap token；
- 打开本地管理页面；
- 不显示终端窗口。

### Olivia Diagnostics

- 打开 Control Center 的诊断页；
- 不直接弹出日志文件或命令行。

### Uninstall Olivia

- 图形化选择保留或删除哪些数据域；
- 默认保留 `data/`。

## 11. 健康检查分层

### `profile=core`

验证：

- HTTP 服务；
- store 可读写；
- Persona 文件可加载；
- Control Center shell 可加载；
- 不调用外部网络。

### `profile=llm`

验证配置完整性；只有显式 `probe=true` 才调用一次最小模型请求。

### `profile=memory`

验证：

- Mem0 Adapter；
- Qdrant path；
- embedding 已安装；
- 不默认写测试记忆；
- 显式 probe 使用临时 collection 并清理。

### `profile=private-world`

验证：

- SQLite 可打开；
- schema version；
- snapshot 可读取；
- 不输出隐藏值。

### `profile=control-center`

验证：

- 独立 loopback listener；
- 静态资源完整；
- session store 可用；
- 不创建真实管理 session；
- 不输出 token。

### `profile=spoken-video`

只做本地文件和 executable 检查。真实生成属于 acceptance。

### `profile=musical-video`

检查 MiniMax、RoFormer、performance base、转场和拼接 profile；不在 health 中启动大型模型。

### `profile=companion`

汇总：

```text
core
llm
persona
memory
private-world
control-center
spoken-video
musical-video
```

每项返回：

```json
{
  "status": "available|degraded|unavailable",
  "provider": "sanitized-name",
  "reason_code": "stable_code",
  "probe": "not-run|filesystem|in-process|network"
}
```

## 12. 数据迁移

首次启动按版本迁移：

- `data/state.json` -> `data/state/letters.json`；
- 旧 memory SQLite 保留为 Archive/迁移源；
- 创建 PrivateWorld 默认 DB；
- Mem0 只在显式安装后初始化；
- 配置文件写入 schema version；
- 每次迁移创建时间戳备份；
- 迁移失败时不删除原文件；
- 不自动把旧 conversation memory 灌入 Mem0；
- Control Center 提供迁移预览和确认。

## 13. 卸载与数据保留

默认卸载：

- 删除 runtime 和隔离客户端；
- 保留 `data/`；
- 在图形界面显示保留路径和数据域。

可选择删除：

```text
当前信件
Archive
长期记忆
PrivateWorld
媒体
凭据
全部本地数据
```

每个 destructive 选项都需要确认。删除 PrivateWorld 和 memory 时使用正式 Service/API，清理 SQLite/Qdrant 附属文件。

## 14. PR 拆分与顺序

### INSTALL-01：配置 schema 与目录布局

新增纯配置模型和迁移计划，不改启动行为。

### INSTALL-02：Setup Wizard 后端与 UI

扩展 key、LLM、PrivateWorld、Memory 和 Media 配置。

### INSTALL-03：启动器与单实例锁

接入配置读取、默认路径、migration 和 PID lock。

### INSTALL-04：Control Center 快捷方式与无控制台启动

完成 Windows 用户入口。

### INSTALL-05：健康检查分层

更新 HTTP contract、health 和测试。

### INSTALL-06：可选 Mem0 安装

增加 optional extra、模型下载和离线启动验证。

### INSTALL-07：媒体能力检测

检测两种视频和音乐拼接链，不修改渲染参数。

### INSTALL-08：图形化卸载和数据保留

按域删除、确认和回归测试。

## 15. 测试矩阵

- Windows 路径包含空格和中文；
- 无管理员权限安装到用户目录；
- 首次安装、重复安装和升级；
- Setup Wizard 全流程；
- DPAPI key 保存与读取；
- 无 key 启动；
- 错误 base URL、模型和超时；
- 无 Mem0；
- Mem0 已安装但 embedding 缺失；
- 无媒体；
- 只有说话与口型基础组件、完整视频链未就绪；
- 完整音乐视频拼接；
- 旧目录迁移；
- 损坏 config 和 state；
- 双实例启动；
- 开始菜单快捷方式；
- 启动时不出现终端窗口；
- 默认卸载保留数据；
- 分域删除；
- 仓库外 wheel 安装和导入。

## 16. 回滚

- 配置和数据迁移都有 schema version；
- 新启动器失败时可使用旧配置兼容读取；
- 回滚代码不得删除新数据目录；
- optional components 可单独禁用；
- Control Center 不可用时原版客户端和文字回复继续；
- 媒体检测错误不影响 core；
- 安装器失败时恢复 patch 前客户端副本。

### 视频回信运行环境包

设置页的“一键下载并安装”负责下载公开可分发的语音、音乐、口型和媒体工具，并优先选择国内源。私有完整安装器同时携带只含四套隔离 Python 的 `Olivia-video-runtime-*.zip` 与受管林离音色；组件下载完成后会自动发现、解压、逐文件校验、补齐受管 MiniMax worker、应用到当前服务进程并立即重新检测。状态契约为 `olivia.video-capability-status.v2`；设置页只保留一个手动选择 ZIP 的断网恢复入口。

私有视频运行时的归档、manifest、哈希、路径、reparse point、worker 和配置错误必须硬失败并回滚。归档和逐文件校验已通过、但 portable Python、readiness probe 或 Windows loader 在当前宿主不可用时，安装仍以 exit code 0 完成：`runtime_import.state=ready` 只表示运行时归档已安装，整体状态为 `UNAVAILABLE`，两个视频 bundle 均为 `prerequisites_required`，原因固定为 `VIDEO_RUNTIME_HOST_UNAVAILABLE`。安装器会保存已验证运行时 profile 和不含路径的宿主状态；重启只恢复该状态，不重新解压、重哈希或重新 probe，核心、语音及视频字节仍提交。兼容宿主保持 `READY`。

运行时归档解压使用 `data/capabilities/.video-runtime-import-cache` 下的内部断点状态；它绑定 ZIP 中央目录指纹而非 inode、mtime 或路径，每 256 个条目原子记录进度。安装中断或外层事务删除 `video` 目录后，同一归档可复用安全 staging，最终仍必须完整 manifest、逐文件哈希、路径集合和 reparse 校验；每次候选重试都会再次完整校验。该 checkpoint 不是公开状态/API，不出现在 `contracts/video_capability_status.schema.json`，成功完成 TTS、readiness、profile 和状态发布后才清理。

## 17. 完成条件

- clean Windows 用户可以完成 Core 安装；
- 用户不需要终端、JSON 或环境变量；
- Setup Wizard、Start Olivia 和 Control Center 快捷方式可用；
- 默认数据目录、当前信件和 PrivateWorld 可持久化；
- Mem0 可通过图形化步骤安装，服务启动不隐式下载；
- health 准确区分 Core、LLM、Memory、PrivateWorld、Control Center 和两种视频；
- musical-video health 覆盖说话＋演唱＋转场＋拼接完整链；
- 可选 provider 缺失不阻断文字回复；
- 升级和卸载不丢用户数据；
- 所有凭据使用 DPAPI 或进程环境，不进入仓库和日志；
- Windows 安装测试、`public-smoke` 和 hardening scan 通过。

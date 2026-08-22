# P03-05 Windows 安装、默认配置与健康检查

## 1. 目标

让普通维护者在一台干净 Windows 机器上完成安装和配置后，不需要手工拼接大量环境变量，就能启动稳定的文字书信陪伴系统；已安装本机媒体组件时，可继续使用说话视频和音乐视频。

安装器必须区分：

- 核心必需能力；
- 默认本地持久化；
- 可选长期记忆；
- 可选说话视频；
- 可选音乐视频；
- 实验模块。

任何可选组件缺失都不能伪造 READY，也不能阻止核心文字回复。

## 2. 现有问题

当前 `installer/start_local.py`：

- 会创建 `data` 和 memory root；
- 会设置本地 HTTP、模型和回复延迟；
- 默认配置较多依赖进程环境；
- 没有默认设置 PrivateWorld DB；
- 没有 Mem0 组件安装和模型缓存流程；
- 媒体配置主要依赖外部环境变量；
- core health 只证明 HTTP 基线，不证明真实模型、记忆和媒体可运行；
- 配置向导主要处理单个 DeepSeek key 和参考文件。

## 3. 默认目录布局

安装后使用：

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
   │  └─ secrets/
   ├─ logs/
   ├─ third-party/
   └─ exports/
```

旧版本使用 `data/state.json` 和 `data/memory` 时，首次启动执行无损迁移。迁移前保留备份。

## 4. 配置优先级

```text
显式命令行参数
    > 环境变量
    > data/config/*.json
    > 安全默认值
```

配置文件不保存明文 API key。凭据使用 Windows DPAPI，运行时只在当前进程内解密。

## 5. 核心配置

`runtime.json` 示例：

```json
{
  "schema_version": 1,
  "port": 8899,
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
  "memory_enabled": true
}
```

安装器不应永久写死一个可能变化的模型名称。首次配置向导提供默认建议，但最终保存用户选择。

## 6. 安装模式

### 6.1 Core

必需：

- Python 3.12 venv；
- 本仓库 runtime；
- aiohttp 和核心依赖；
- 原版客户端隔离复制与 patch；
- Persona 文件；
- 当前信件持久化；
- PrivateWorld SQLite；
- health、start、uninstall。

Core 不下载大型模型。

### 6.2 Memory

可选安装 extra：

```text
mem0ai
qdrant-client
sentence-transformers
锁定的 embedding 模型
```

安装器必须：

- 显式展示下载内容和预计体积；
- 下载到 `data/memory/model-cache`；
- 固定 revision 或校验哈希；
- 允许跳过；
- 下载失败时保持 Core 可用；
- 不在服务首次启动时隐式联网下载。

### 6.3 Spoken Video

检测：

- TTS config；
- CosyVoice runtime；
- 参考声音；
- LatentSync Python；
- LatentSync root、config 和 checkpoint；
- morning/day/dusk/night 场景视频；
- FFmpeg。

只有全部满足才报告 `spoken_video=available`。

### 6.4 Musical Video

在 Spoken Video 基础上额外检测：

- MiniMax Music 3 ComfyUI runtime；
- MiniMax worker；
- MiniMax model 文件；
- RoFormer executable；
- performance base video；
- 可选官方转场；
- 音频实验已经选择生产参数。

只有完整链可运行才报告 `musical_video=available`。

## 7. 首次配置向导

扩展 `installer/configure.py`，使用分阶段向导：

```text
1. 选择并验证主模型 endpoint
2. 安全保存 API key
3. 检查 Persona READY
4. 创建 PrivateWorld DB
5. 选择是否安装 Mem0
6. 检查或配置 embedding
7. 检查说话视频组件
8. 检查音乐视频组件
9. 写入配置文件
10. 运行无外部调用的静态验证
11. 可选运行真实 provider probe
```

要求：

- 输入 key 不回显；
- probe 不输出 provider 原始响应；
- 所有文件路径在保存前验证；
- 不自动扫描整个磁盘、Steam 目录或用户文档；
- 只复制用户显式选择的第三方参考文件；
- 重新运行向导时保留未修改项；
- 提供 `--non-interactive --config <file>` 供自动化验收。

## 8. 启动器

`installer/start_local.py` 调整：

1. 读取并验证配置；
2. 创建所有默认数据目录；
3. 设置进程级环境；
4. 解密凭据；
5. 执行轻量 migration；
6. 启动本地后端；
7. 等待 `/health?profile=core`；
8. 可选检查 `/health?profile=companion`；
9. 启动隔离客户端；
10. 客户端退出后不误杀仍在处理的媒体任务，提供明确停止命令。

需要 PID/lock 文件防止同一数据目录启动两个后端实例。

## 9. 健康检查分层

### `profile=core`

验证：

- HTTP 服务；
- store 可读写；
- Persona 文件可加载；
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

### `profile=spoken-video`

只做本地文件和 executable 检查。真实生成属于 acceptance，不属于普通 health。

### `profile=musical-video`

只检查本地依赖和生产参数配置；不在 health 中启动大型模型。

### `profile=companion`

汇总：

```text
core
llm
persona
memory
private-world
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

## 10. 数据迁移

首次启动按版本迁移：

- `data/state.json` -> `data/state/letters.json`；
- 旧 memory SQLite 保留为 Archive/迁移源；
- 创建 PrivateWorld 默认 DB；
- Mem0 只在显式安装后初始化；
- 配置文件写入 schema version；
- 每次迁移创建时间戳备份；
- 迁移失败时不删除原文件；
- 不自动把旧 conversation memory 灌入 Mem0。

## 11. 卸载与数据保留

默认卸载：

- 删除 runtime 和隔离客户端；
- 保留 `data/`；
- 输出保留路径。

可选：

```text
--delete-current-letters
--delete-archive
--delete-memory
--delete-private-world
--delete-media
--delete-secrets
--delete-all-data
```

每个 destructive 选项都需要确认。删除 PrivateWorld 和 memory 时使用其正式管理入口，清理 SQLite/Qdrant 附属文件。

## 12. PR 拆分与顺序

### INSTALL-01：配置 schema 与目录布局

新增纯配置模型和迁移计划，不改启动行为。

### INSTALL-02：首次配置向导

扩展 key、LLM、PrivateWorld、Memory 和 Media 配置。

### INSTALL-03：启动器与单实例锁

接入配置读取、默认路径、migration 和 PID lock。

### INSTALL-04：健康检查分层

更新 HTTP contract、health 和测试。

### INSTALL-05：可选 Mem0 安装

增加 optional extra、模型下载和离线启动验证。

### INSTALL-06：媒体能力检测

只检测并报告，不修改已经调好的渲染参数。

### INSTALL-07：卸载和数据保留

按域删除、确认和回归测试。

## 13. 测试矩阵

- Windows 路径包含空格和中文；
- 无管理员权限安装到用户目录；
- 首次安装、重复安装和升级；
- DPAPI key 保存与读取；
- 无 key 启动；
- 错误 base URL、模型和超时；
- 无 Mem0；
- Mem0 已安装但 embedding 缺失；
- 无媒体；
- 只有说话视频；
- 完整音乐视频；
- 旧目录迁移；
- 损坏 config 和 state；
- 双实例启动；
- 默认卸载保留数据；
- 分域删除；
- 仓库外 wheel 安装和导入。

## 14. 回滚

- 配置和数据迁移都有 schema version；
- 新启动器失败时可使用旧配置兼容读取；
- 回滚代码不得删除新数据目录；
- optional components 可单独禁用；
- 媒体检测错误不影响 core；
- 安装器失败时恢复 patch 前客户端副本。

## 15. 完成条件

- clean Windows 用户可以完成 Core 安装；
- 默认数据目录、当前信件和 PrivateWorld 可持久化；
- Mem0 可通过明确步骤安装，且服务启动不隐式下载；
- 配置不依赖手工拼接几十个环境变量；
- health 能准确区分 Core、LLM、Memory、PrivateWorld 和两种视频；
- 可选 provider 缺失不阻断文字回复；
- 升级和卸载不丢用户数据；
- 所有凭据使用 DPAPI 或环境变量，不进入仓库和日志；
- Windows 安装测试、`public-smoke` 和 hardening scan 通过。

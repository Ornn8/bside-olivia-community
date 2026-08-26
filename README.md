# BSide Olivia Community

[![Public smoke](https://github.com/Ornn8/bside-olivia-community/actions/workflows/public-smoke.yml/badge.svg)](https://github.com/Ornn8/bside-olivia-community/actions/workflows/public-smoke.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/Code-MIT-yellow.svg)](LICENSE)

一个面向 Windows 的非官方、非商业 Olivia 本地陪伴复刻项目。

项目尽量保留原版的写信、等待回信和视频回信体验，并在本机加入可配置的 LLM、Persona、长期记忆、PrivateWorld 与媒体生成能力。它不是官方服务的替代入口，也不包含原版游戏、官方素材、模型权重、用户数据或访问凭据。

> 当前阶段：文字回信、人格、记忆和原版客户端接入已经形成可运行主链；视频与音乐回信具备编排代码，但跨机器安装和最终人工效果验收仍在进行；Live 实时对话暂停开发。

## 已实现

- 原版客户端与本机 HTTP 后端联动，服务默认只监听 `127.0.0.1:8899`；
- Persona 2.0、上下文装配、固定分层审校和最多一次正文修复；
- 文字回信、普通说话视频、音乐视频三种表达路径；
- Mem0 长期记忆与 PrivateWorld 私有关系状态，按用户隔离并提供本地管理入口；
- 后台媒体任务、重启恢复和“仅影响新信”的视频回信开关；
- Windows 隔离安装、DPAPI 密钥保存、启动与保留用户数据的卸载流程；
- provider、模型或原版资源缺失时明确返回 `UNAVAILABLE` / `DEGRADED`，不伪造成功。

## 仍在完善

| 能力 | 当前边界 |
| --- | --- |
| 文字回信 | 主链可用，仍需更多长期人格与记忆盲测 |
| 普通视频回信 | 编排已接入；TTS、口型和场景效果仍需真实设备人工验收 |
| 音乐视频回信 | 支持说话视频、转场、演唱视频组合；生成耗时和人物稳定性仍是主要问题 |
| 本地模型安装 | 核心运行时可由安装器准备；大型 TTS、视觉和音乐模型仍需按文档单独配置 |
| Live 实时对话 | 暂停，不属于当前发布范围 |
| 正式发行包 | 尚未发布 GitHub Release；目前面向源码安装与开发者测试 |

动态进度以 [GitHub Issues](https://github.com/Ornn8/bside-olivia-community/issues) 和 [Milestones](https://github.com/Ornn8/bside-olivia-community/milestones) 为准。

## 普通用户安装

### 要求

- Windows 10/11 x64；
- 合法取得的原版客户端 `0.0.9.615`；
- 可用的 DeepSeek API；其他 OpenAI-compatible 接口目前仅支持开发者通过环境变量配置；
- 仅使用文字回信时不要求本地独立显卡；视频、TTS 和音乐模型有各自的显存与磁盘要求。

### 安装与启动

```powershell
git clone https://github.com/Ornn8/bside-olivia-community.git
cd bside-olivia-community
.\INSTALL.cmd
.\START.cmd
```

首次安装会在获得同意后下载经过哈希校验的 Python 3.12 嵌入式运行时和固定依赖，并自动查找 Steam AppID `4532590`。如果没有自动发现，可以按提示选择原版安装目录。

首次启动后可在原版客户端的本地设置页管理记忆和视频回信开关；安装目录中的 `CONFIGURE.cmd` 用于保存 DeepSeek API key 和可选参考文件。API key 使用当前 Windows 用户的 DPAPI 加密保存，不写入仓库、安装包或日志。

安装、升级、目录和卸载边界详见 [Windows 完整版补丁说明](docs/WINDOWS_FULL_PATCH.md)。第三方模型请从 [官方或维护方下载入口](docs/THIRD_PARTY_DOWNLOADS.md) 获取。

## 工作方式

```text
原版客户端
  -> 本机 HTTP 服务
  -> Persona + 当前来信 + 历史记忆 + PrivateWorld 行为投影
  -> LLM 生成与审校
  -> canonical reply 持久化
  -> 文字展示
  -> 可选的后台说话视频 / 音乐视频任务
```

正文一旦成为 canonical reply，记忆、PrivateWorld 或媒体 provider 的失败都不能删除它。媒体只是正文的可选投影，不是文字回信成功的前置条件。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| 根目录 Python 模块 | 当前产品运行时；为兼容已有导入暂时保持扁平结构 |
| `linli_character/` | 可公开的人格配置、风格特征与 provenance |
| `control_center/` | 本地记忆和 PrivateWorld 管理界面 |
| `installer/` | Windows 隔离安装、启动、配置和卸载 |
| `contracts/` | JSON Schema 与公开接口契约 |
| `tts/`, `asr/`, `live/`, `visual_driver/` | 可替换的媒体 provider 适配层 |
| `runtime/` | 可选模块和视觉运行时装配 |
| `tests/` | 合成 fixture 与回归测试，不包含私人数据 |
| `tools/` | 审计、健康检查和维护工具 |
| `docs/` | 用户、架构、验收和治理文档；入口见 [文档索引](docs/README.md) |

更详细的公开边界见 [仓库结构说明](docs/REPOSITORY_LAYOUT.md)。

## 开发

需要 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest -q
python baseline_hardening_scan.py --mode all
git diff --check
```

本地开发服务：

```powershell
python local_server.py
```

测试通过只证明对应代码和合成 fixture 通过，不代表第三方模型、原版客户端、真实 GPU 或人工视听验收已经完成。

## 隐私、版权与分发边界

本仓库不会分发：

- 原版程序、`feapp.dat`、解包前端、角色视频、背景或音乐；
- CosyVoice、LiveTalking、MiniMax 等第三方运行时、模型权重和缓存；
- 私人信件、声音参考、生成媒体、用户数据库、抓包、Token 或 API key；
- 任何开发者机器专属的绝对路径和私有配置。

公开代码只提供适配器、配置接口、哈希校验、合成测试和失败状态。使用者必须自行确认原版内容、第三方模型和生成内容在所在地及具体用途下的许可。完整规则见 [公开仓库边界](docs/PUBLIC_REPOSITORY.md) 与 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 参与维护

提交前请阅读 [贡献指南](CONTRIBUTING.md)、[安全政策](SECURITY.md) 与 [行为准则](CODE_OF_CONDUCT.md)。优先提交小而可回滚的 PR，并明确测试结果、外部依赖和未验证边界。

## 许可

本仓库自有代码与文档采用 [MIT License](LICENSE)。该许可证不覆盖原版游戏、官方素材、第三方模型、第三方运行时或用户生成内容。本项目与原作者、发行方及相关权利方没有隶属或授权关系。

# Repository layout

仓库当前采用“扁平产品运行时＋分目录适配层”的结构。根目录 Python 模块较多，但它们已经被安装器、测试和公开导入路径使用；在形成兼容迁移计划前，不做仅为美观的大规模搬家。

## 产品入口

| 路径 | 职责 |
| --- | --- |
| `local_server.py` | 本机 HTTP 服务、信件主流程和后台媒体任务入口 |
| `reply_context.py`、`reply_pipeline.py`、`reply_reviewer.py`、`reply_delivery.py`、`reply_media.py` | 原有导入兼容别名，映射到 `runtime/reply/` 中的同一模块对象 |
| `reply_model_quality.py`、`reply_orchestrator.py`、`runtime/reply/*.py` | canonical reply 装配、审校、持久化和媒体投影 |
| `llm_gateway.py`、`voice_direction.py` | 可配置模型调用与语音导演计划 |
| `runtime/memory/conversation_memory_{delivery,outbox}.py`（根目录保留兼容别名）、其余 `conversation_memory_*.py`、`mem0_memory.py` | 长期记忆、outbox、管理和运行时适配 |
| `private_world_*.py` | 私有关系事件、ledger、reducer、投影和管理 |
| `original_client_*.py`、`patch_*.py` | 原版客户端兼容接口、设置页和本机补丁 |
| `runtime/media/`、`runtime/reply/reply_media.py`（根目录兼容别名保留） | 视频、音乐和媒体编排 |

## 目录

| 路径 | 内容 | 默认发布边界 |
| --- | --- | --- |
| `installer/` | Windows 隔离安装、启动、配置、升级和卸载 | 核心入口 |
| `linli_character/` | 公开人格配置、风格特征和 provenance | 核心数据 |
| `control_center/` | Memory 与 PrivateWorld 本地管理界面 | 核心入口 |
| `contracts/` | JSON Schema 和接口契约 | 公共契约 |
| `tts/`, `asr/` | 语音 provider 适配器 | provider 可选 |
| `live/`, `visual_driver/` | Live 与视觉驱动接口 | 实验性、默认暂停 |
| `runtime/` | 可选模块和视觉运行时装配 | 按 profile 启用 |
| `media_state/` | 媒体状态与资源引用边界 | 公共契约 |
| `tests/` | 合成 fixture、契约和回归测试 | 不含真实数据 |
| `tools/` | 审计、健康检查、provider worker 和维护命令 | 维护入口 |
| `docs/` | 用户、实现、验收、治理与历史文档 | 入口见 `docs/README.md` |

## 本机内容

以下内容永不进入公开提交：

- 原版安装目录、`feapp.dat`、提取后的前端和官方媒体；
- `CosyVoice/`、`LiveTalking/`、MiniMax 等第三方运行时、权重和缓存；
- API key、抓包、协议秘密、真实信件、用户数据库和声音参考；
- `.evidence/`、生成音视频、日志和机器专属绝对路径。

需要这些内容的功能只能提交配置接口、逻辑 ID、哈希校验、脱敏 manifest 和合成 fixture。详细规则见 [`PUBLIC_REPOSITORY.md`](PUBLIC_REPOSITORY.md)。

## 未来整理原则

根目录模块只有在满足下列条件时才迁入正式 package：

1. 有明确的模块边界和单一归属；
2. 保留旧导入路径的兼容层或提供版本化迁移；
3. 安装器、provider worker、测试和第三方调用方可在同一个小 PR 中验证；
4. 迁移能删除旧代码，而不是同时长期维护两套目录。

当前优先级是完成功能和真实验收，不为目录观感进行大规模重构。

# Root Python inventory

本账本冻结于 `main@f99a3054d214bccf4170a001fec6d8745c5930ac`。整理开始时根目录有 66 个 `.py` 文件；当前有 55 个，净减少 11 个。另有 9 个原根实现已迁入职责包，根文件缩成发布中的薄兼容别名。本文只记录布局与兼容边界，不改变代码、打包或运行协议。

## 必须保留的根入口

| 根文件 | 保留证据 |
| --- | --- |
| `local_server.py` | README 的公开命令 `python local_server.py` |
| `original_client_server.py` | `installer/start_local.py` 的启动目标，并属于 full-patch 必需根文件 |
| `letter_status.py`、`original_client_media_http.py`、`original_client_settings_ui.py`、`video_reply_settings.py` | `installer/full_patch.py` 的必需根文件 |
| `patch_companion_settings.py`、`patch_feapp.py`、`patch_webplayer.py` | `installer/full_patch.py` 的必需补丁入口 |
| `mem0_embedding_install.py` | `installer/provision_mem0_embedding.py` 的直接导入依赖 |
| `baseline_hardening_scan.py`、`extract_player.py`、`private_world_admin.py` | 独立 `__main__` CLI；其中 hardening scan 另有 README 命令 |

## 已发布兼容映射

下列 9 个根模块已作为 `pyproject.toml` 的 wheel 顶层模块发布，打包 smoke 也验证旧导入与 canonical 模块为同一对象。本轮严格审计将它们判定为 **KEEP**，不能在文档收口 PR 中删除。

| 根兼容模块 | Canonical 模块 |
| --- | --- |
| `reply_context` | `runtime.reply.reply_context` |
| `reply_pipeline` | `runtime.reply.reply_pipeline` |
| `reply_reviewer` | `runtime.reply.reply_reviewer` |
| `reply_delivery` | `runtime.reply.reply_delivery` |
| `reply_media` | `runtime.reply.reply_media` |
| `latentsync_reply` | `runtime.media.latentsync_reply` |
| `song_content` | `runtime.media.song_content` |
| `conversation_memory_delivery` | `runtime.memory.conversation_memory_delivery` |
| `conversation_memory_outbox` | `runtime.memory.conversation_memory_outbox` |

## 当前 55 个根文件

每个根 `.py` 在以下六类中恰好出现一次。`KEEP` 表示有现行入口或兼容证据；`RETAIN/DEFER` 表示仍是 canonical 根实现，本轮受迁移批次上限约束而明确延期，不代表已经完成收拢。

### 正式入口 / 兼容壳（15）

| 文件 | 处置 |
| --- | --- |
| `local_server.py`、`original_client_server.py` | `KEEP`：公开或安装启动入口 |
| `conversation_memory_delivery.py`、`conversation_memory_outbox.py` | `KEEP`：已发布 wheel 兼容别名 |
| `latentsync_reply.py`、`song_content.py` | `KEEP`：已发布 wheel 兼容别名 |
| `reply_context.py`、`reply_delivery.py`、`reply_media.py`、`reply_pipeline.py`、`reply_reviewer.py` | `KEEP`：已发布 wheel 兼容别名 |
| `letter_status.py`、`original_client_media_http.py`、`video_reply_settings.py` | `KEEP`：安装器要求的 canonical 包兼容入口 |
| `memory_isolation_case01.py` | `KEEP`：现有 synthetic isolation 调用兼容入口 |

### 后端模块（29）

| 文件 | 主要职责 / 处置 |
| --- | --- |
| `companion_memory_context.py` | Memory prompt 投影；`RETAIN/DEFER` |
| `conversation_memory_admin.py`、`conversation_memory_port.py`、`conversation_memory_runtime.py` | Conversation Memory 管理、端口和运行时；`RETAIN/DEFER` |
| `http_contract.py`、`letter_triage.py`、`llm_gateway.py` | HTTP 契约、回信分流和模型网关；`RETAIN/DEFER` |
| `local_memory.py`、`mem0_memory.py`、`memory.py`、`memory_port.py`、`memory_prompt.py` | Memory 存储、provider、端口和 prompt；`RETAIN/DEFER` |
| `original_client_companion_api.py`、`original_client_companion_backend.py` | 原版设置页只读适配；`RETAIN/DEFER` |
| `original_client_companion_mutation_api.py`、`original_client_companion_mutation_backend.py` | 原版设置页变更适配；`RETAIN/DEFER` |
| `original_client_letter_contract.py` | 原版 mailbox wire contract；`RETAIN/DEFER` |
| `persona_assembly.py`、`persona_loader.py`、`persona_provider.py` | Persona 装配、加载和 provider；`RETAIN/DEFER` |
| `private_world_candidate.py`、`private_world_candidates.py` | PrivateWorld 候选分析与存储；`RETAIN/DEFER` |
| `private_world_commands.py`、`private_world_ledger.py`、`private_world_port.py`、`private_world_reducer.py`、`private_world_service.py` | PrivateWorld 命令、账本、端口、reducer 和服务；`RETAIN/DEFER` |
| `reply_model_quality.py`、`reply_orchestrator.py` | 回信模型审校和状态编排；`RETAIN/DEFER` |

### 媒体模块（2）

| 文件 | 主要职责 / 处置 |
| --- | --- |
| `music_reply.py` | 普通视频与歌曲回复拼接；`RETAIN/DEFER` |
| `voice_direction.py` | 冻结回复的语音表演方向；`RETAIN/DEFER` |

### 安装器相关（5）

| 文件 | 主要职责 / 处置 |
| --- | --- |
| `mem0_embedding_install.py` | 可选 embedding 安装实现；`KEEP`：安装器直接依赖 |
| `original_client_settings_ui.py` | 原版设置页注入脚本；`KEEP`：full-patch 必需文件 |
| `patch_companion_settings.py`、`patch_feapp.py`、`patch_webplayer.py` | 原版客户端补丁入口；`KEEP`：full-patch 必需文件 |

### 工具（3）

| 文件 | 主要职责 / 处置 |
| --- | --- |
| `baseline_hardening_scan.py` | 仓库扫描 CLI；`KEEP` |
| `extract_player.py` | 受控归档解包 CLI；`KEEP` |
| `private_world_admin.py` | PrivateWorld 本地管理 CLI；`KEEP` |

### 测试残留（1）

| 文件 | 处置 |
| --- | --- |
| `test_cosyvoice3.py` | `RETAIN/DEFER`：根目录手工 provider smoke；应在独立、明确验证边界的后续任务中决定迁移或删除 |

## 收口边界

- 本轮没有把 `RETAIN/DEFER` 宣称为已迁移，也没有扩大到功能、安全或治理整改。
- 后续迁移必须重新核对安装器、原版客户端、wheel 顶层导入、公开命令和仓库外调用；不能仅按文件名机械删除。
- 任何兼容别名退役都需要单独的版本化决定、打包验证和迁移说明。

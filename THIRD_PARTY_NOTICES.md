# Third-party notices

本文件只记录公开基础安装与开发测试依赖。第三方项目的许可证不被根目录 MIT 许可证替代；发布包含这些依赖的环境时，应同时遵守各自许可证和 NOTICE 要求。

| 依赖 | 用途 | 许可证边界 |
| --- | --- | --- |
| `aiohttp` | 本地 HTTP 服务 | Apache-2.0 / MIT（以安装版本随附文本为准） |
| `pytest` | 开发测试 | MIT |
| `numpy` | 可选开发/媒体测试依赖 | BSD-3-Clause |
| `opencv-python-headless` | 可选媒体测试依赖 | Apache-2.0 |

模型、官方游戏、CosyVoice、LiveTalking、ASR/TTS provider、生成媒体和用户数据不属于公开依赖清单；它们必须由使用者在本机按各自来源和许可证提供，不能因为本项目使用接口或适配器就被重新授权。

版本化发布前，维护者应从实际锁定环境重新生成依赖清单，并核对每个发行包中的 LICENSE/NOTICE 文件。

# Third-party notices

本文件只记录公开基础安装与开发测试依赖。第三方项目的许可证不被根目录 Apache-2.0 许可证替代；发布包含这些依赖的环境时，应同时遵守各自许可证和 NOTICE 要求。

| 依赖 | 用途 | 上游来源 | 许可证边界 |
| --- | --- | --- | --- |
| `aiohttp` | 本地 HTTP 服务 | [aio-libs/aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 / MIT（以安装版本随附文本为准） |
| `imageio-ffmpeg` | 运行时 FFmpeg 包装器 | [imageio/imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Python 包为 BSD-2-Clause；PyPI wheel 可能附带 FFmpeg 可执行文件，发行时必须按所用二进制的实际来源、许可证和 NOTICE 另行核对。 |
| `jsonschema` | 运行时 JSON Schema 校验 | [python-jsonschema/jsonschema](https://github.com/python-jsonschema/jsonschema) | MIT |
| `pytest` | 开发测试 | [pytest-dev/pytest](https://github.com/pytest-dev/pytest) | MIT |
| `numpy` | 可选开发/媒体测试依赖 | [numpy/numpy](https://github.com/numpy/numpy) | BSD-3-Clause |
| `opencv-python-headless` | 可选媒体测试依赖 | [opencv/opencv-python](https://github.com/opencv/opencv-python) | Apache-2.0 |

模型、官方游戏、CosyVoice、LiveTalking、ASR/TTS provider、生成媒体和用户数据不属于公开依赖清单；它们必须由使用者在本机按各自来源和许可证提供，不能因为本项目使用接口或适配器就被重新授权。

版本化发布前，维护者应从实际锁定环境重新生成依赖清单，并核对每个发行包中的 LICENSE/NOTICE 文件。

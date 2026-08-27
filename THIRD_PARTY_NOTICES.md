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
| Inno Setup 6.7.1 | 仅用于构建 Windows 单文件安装器；编译器本身不进入发布包，生成的 Setup runtime 进入 EXE | [jrsoftware/issrc `is-6_7_1`](https://github.com/jrsoftware/issrc/tree/is-6_7_1) | Inno Setup License；构建时验证 `ISCC.exe` 的有效 Authenticode 签名及发布者 `Pyrsys B.V.` |
| `ChineseSimplified.isl` | Windows 安装向导简体中文消息，编译后进入 EXE | [Inno Setup `is-6_7_1` 固定标签](https://github.com/jrsoftware/issrc/blob/is-6_7_1/Files/Languages/Unofficial/ChineseSimplified.isl) | 随 Inno Setup 源码发布并适用其许可证；构建锁定 SHA-256 `7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278` |

模型、官方游戏、CosyVoice、LiveTalking、ASR/TTS provider、生成媒体和用户数据不属于公开依赖清单；它们必须由使用者在本机按各自来源和许可证提供，不能因为本项目使用接口或适配器就被重新授权。

版本化发布前，维护者应从实际锁定环境重新生成依赖清单，并核对每个发行包中的 LICENSE/NOTICE 文件。

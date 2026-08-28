# Third-party notices

本文件记录公开基础安装、安装器构建和开发测试依赖。第三方项目的许可证不被根目录 Apache-2.0 许可证替代；发布包含这些依赖的环境时，应同时遵守各自许可证和 NOTICE 要求。

| 依赖 | 用途 | 上游来源 | 许可证边界 |
| --- | --- | --- | --- |
| `aiohttp` | 本地 HTTP 服务 | [aio-libs/aiohttp](https://github.com/aio-libs/aiohttp) | Apache-2.0 / MIT（以安装版本随附文本为准） |
| `imageio-ffmpeg` | 运行时 FFmpeg 包装器 | [imageio/imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Python 包为 BSD-2-Clause；PyPI wheel 可能附带 FFmpeg 可执行文件，发行时必须按所用二进制的实际来源、许可证和 NOTICE 另行核对。 |
| LatentSync source and declared checkpoints | Optional local lip-sync provider | [bytedance/LatentSync at `a229c394`](https://github.com/bytedance/LatentSync/tree/a229c3948406bc2cf6eaf4873e662e70c6a04746) | Apache-2.0 |
| MiniMax Music 3 repackaged weights | Optional local music provider | [Comfy-Org/MiniMax-Music-3 at `6444666`](https://huggingface.co/Comfy-Org/MiniMax-Music-3/tree/6444666eb6edfb2c7fcab5f8b81da8b84b4b17b6) | Apache-2.0 model-card metadata |
| Mel-Band RoFormer inference source | Optional local vocal-separation provider | [openmirlab/melband-roformer-infer at `a21cb300`](https://github.com/openmirlab/melband-roformer-infer/tree/a21cb300e7637b878f46c500a68737aeb5aa2226) | MIT |
| MelBandRoformer checkpoint | Optional local vocal-separation checkpoint; downloaded directly to the user's machine and not bundled | [KimberleyJSN/melbandroformer at `ac9b0614`](https://huggingface.co/KimberleyJSN/melbandroformer/tree/ac9b0614ab3cd7f77219e18ba494dfd93956c348) | CC-BY-NC-SA-4.0 repository metadata |
| `jsonschema` | 运行时 JSON Schema 校验 | [python-jsonschema/jsonschema](https://github.com/python-jsonschema/jsonschema) | MIT |
| `pytest` | 开发测试 | [pytest-dev/pytest](https://github.com/pytest-dev/pytest) | MIT |
| `numpy` | 可选开发/媒体测试依赖 | [numpy/numpy](https://github.com/numpy/numpy) | BSD-3-Clause |
| `opencv-python-headless` | 可选媒体测试依赖 | [opencv/opencv-python](https://github.com/opencv/opencv-python) | Apache-2.0 |
| Inno Setup 6.7.1 | 仅用于构建 Windows 单文件安装器；编译器本身不进入发布包，生成的 Setup runtime 进入 EXE | [jrsoftware/issrc `is-6_7_1`](https://github.com/jrsoftware/issrc/tree/is-6_7_1) | Inno Setup License；构建时验证 `ISCC.exe` 的有效 Authenticode 签名及发布者 `Pyrsys B.V.` |
| `ChineseSimplified.isl` | Windows 安装向导简体中文消息，编译后进入 EXE | [Inno Setup `is-6_7_1` 固定标签](https://github.com/jrsoftware/issrc/blob/is-6_7_1/Files/Languages/Unofficial/ChineseSimplified.isl) | 随 Inno Setup 源码发布并适用其许可证；构建锁定 SHA-256 `7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278` |

## Windows 离线核心的锁定发行闭包

以下内容是 `installer/runtime-requirements.txt`、`build_offline_core_assets.py` 和实际下载 wheel 元数据共同确定的 Windows / CPython 3.12 发行闭包。wheel 保持上游文件原样，其中的 `.dist-info/licenses/`、`LICENSE`、`COPYING` 和 `NOTICE` 随 wheel 一起进入离线安装包。

| 发行内容 | 锁定版本 | 许可证 |
| --- | --- | --- |
| Python 3.12.10 embeddable package | 3.12.10 | PSF License Version 2；运行时 ZIP 内含 `LICENSE.txt` |
| `pip` 25.2 | 25.2 | MIT；wheel 内同时保留所有 vendored dependency 许可证 |
| `aiohappyeyeballs` | 2.7.1 | PSF-2.0 |
| `aiohttp` | 3.14.1 | Apache-2.0 AND MIT；wheel 内含 aiohttp 与 vendored llhttp 许可证 |
| `aiosignal` | 1.4.0 | Apache-2.0 |
| `attrs` | 26.1.0 | MIT |
| `frozenlist` | 1.8.0 | Apache-2.0 |
| `idna` | 3.18 | BSD-3-Clause |
| `jsonschema` | 4.26.0 | MIT |
| `jsonschema-specifications` | 2025.9.1 | MIT |
| `multidict` | 6.7.1 | Apache-2.0 |
| `propcache` | 0.5.2 | Apache-2.0；wheel 内含 NOTICE |
| `referencing` | 0.37.0 | MIT |
| `rpds-py` | 2026.6.3 | MIT |
| `typing_extensions` | 4.16.0 | PSF-2.0 |
| `yarl` | 1.24.2 | Apache-2.0；wheel 内含 NOTICE |

模型、官方游戏、CosyVoice、LiveTalking、ASR/TTS provider、生成媒体和用户数据不属于公开依赖清单；它们必须由使用者在本机按各自来源和许可证提供，不能因为本项目使用接口或适配器就被重新授权。

版本化发布前，维护者仍应重新构建离线目录，确认 manifest、wheel 集合和 SHA-256 与锁文件一致；依赖版本变化时必须同步更新本节。

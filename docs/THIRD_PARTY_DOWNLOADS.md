# 第三方内容下载

本仓库只发布项目代码和下载清单，不打包第三方模型、运行时、原版游戏资源或生成媒体。项目代码采用 MIT；MIT **不覆盖**第三方内容，使用者必须在下载前自行阅读并接受每个清单条目的许可证和上游条款。

## 准备清单

复制 [`third_party_manifest.example.json`](../contracts/third_party_manifest.example.json)，它默认是空清单，不会形成可安装的假条目。维护者新增条目时，必须填写真实上游的 `source_url`、`version` 或 `revision`、`license`、`target_path`、`sha256`（以及可选 `size_bytes`）。URL 必须是 HTTPS 且不能包含账号、密钥、token、查询参数或 fragment；本地测试才允许 loopback HTTP。

## Windows 用法

默认命令只校验清单并显示计划，不联网、不写文件：

```powershell
$thirdPartyRoot = Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\third-party'
.\tools\Install-ThirdParty.ps1 `
  -DataRoot $thirdPartyRoot `
  -Manifest '.\third_party_manifest.json'
```

确认每个条目的许可证后，显式添加 `-Install -AcceptLicenses` 才会下载：

```powershell
$thirdPartyRoot = Join-Path $env:LOCALAPPDATA 'BSideOliviaLocal\third-party'
.\tools\Install-ThirdParty.ps1 `
  -DataRoot $thirdPartyRoot `
  -Manifest '.\third_party_manifest.json' `
  -Install -AcceptLicenses
```

也可以只安装一个条目：`-Item '<manifest-item-id>'`。`DataRoot` 必须位于仓库外；下载器会先写临时文件，再校验大小和 SHA-256，任何来源、许可证、路径、哈希或大小缺失/不匹配都会 fail closed，仓库不会被写入。

下载清单中的 URL 和日志不会打印查询参数或凭据。不要把带 token 的 URL 写入 manifest、PowerShell 历史或 issue；需要认证的上游内容应使用其官方登录/下载流程后，再提供无凭据、可校验的公开 artifact 地址。已存在且校验通过的目标文件会复用；已存在但校验失败时会停止，绝不覆盖。

## 清单字段

机器校验规则见 [`third_party_manifest.schema.json`](../contracts/third_party_manifest.schema.json)。`id` 必须唯一；`target_path` 是相对于外部 `DataRoot` 的路径，不能是绝对路径或包含 `..`；`sha256` 必须是 64 位十六进制；`version` 与 `revision` 至少提供一个。

本地 fixture 测试使用 loopback HTTP，不代表项目会替用户下载任何真实第三方内容。

## 已固定的上游来源目录

以下目录来自仓库已跟踪的 [`runtime/packaging/manifests/b10b.modules.json`](../runtime/packaging/manifests/b10b.modules.json)，仅提供官方上游来源页、固定 revision 和许可证，供用户按上游说明手动下载。它们不是自动下载 artifact URL，也没有在本仓库伪造 SHA-256：

| 内容 | 官方上游来源页 | 固定 revision | 许可证 | 方式 |
| --- | --- | --- | --- | --- |
| NeMo-Speech.cpp | [NVIDIA/NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) | `1118951337094db3b362fbf1b27e871696f10590` | Apache-2.0 | 上游来源页/手动下载 |
| Nemotron 3.5 ASR Streaming 0.6B | [nvidia/nemotron-3.5-asr-streaming-0.6b](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b) | `1c8deaecc64b91f034d73e08dd8b64625eb3395d` | OpenMDW-1.1 | 上游来源页/手动下载 |
| CosyVoice runtime | [FunAudioLLM/CosyVoice](https://github.com/FunAudioLLM/CosyVoice) | `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc` | Apache-2.0 | 上游来源页/手动下载 |
| Fun-CosyVoice3-0.5B-2512 | [FunAudioLLM/Fun-CosyVoice3-0.5B-2512](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512) | `29e01c4e8d000f4bcd70751be16fa94bf3d85a18` | Apache-2.0 | 上游来源页/手动下载 |
| openai-whisper runtime | [openai/whisper](https://github.com/openai/whisper) | `31243bad24cc746f07d4c8bfdd2d974872cb1803` (`v20250625`) | MIT | 安装到外部 CosyVoice Python 环境 |
| Whisper base.pt | [OpenAI official artifact](https://openaipublic.azureedge.net/main/whisper/models/ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e/base.pt) | `ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e` (SHA-256) | MIT | 手动下载到显式配置的外部 cache |
| LiveTalking runtime | [lipku/LiveTalking](https://github.com/lipku/LiveTalking) | `a97f01ba366e55eeed94e88d6bae38ed77b3a1b9` | Apache-2.0 | 上游来源页/手动下载 |

这些条目不会带入原版游戏素材、私有参考音频、模型权重或任何生成媒体。只有维护者取得稳定、无凭据的直接 artifact URL，并完成许可证确认和 SHA-256 固定后，才允许把条目加入自动下载 manifest。

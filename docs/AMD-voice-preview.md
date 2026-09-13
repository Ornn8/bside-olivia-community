# AMD 语音独立测试包（实验版 1）

这个包自带 Python、Breeze INT8 模型、参考音色和林离 2250 LoRA。
不需要安装社区版或 CUDA 语音包，不修改社区版设置，不读取私人信件、记忆、API 密钥。
它是独立验收工具，尚未接入正式应用的语音按钮，也没有经过 AMD 实机验收。

## 如何测试

1. 解压到短路径，例如 `D:\AMDVoice`。不要在压缩包内直接运行，也不要覆盖旧的测试目录。
2. Windows 11、AMD 显卡、建议 12GB 以上显存。先关闭游戏和其他占用显存的软件。
3. 双击 `1-Start-AMD-Voice-Test.cmd`。首次联网下载 AMD 官方 ROCm 7.2.1 / PyTorch 2.9.1 依赖，约数 GB；解压后所在磁盘至少留 18GiB 空间。安装失败可以重跑。
4. 自动生成两条固定中文测试语音，包含现有 2250 LoRA，每条最多等待 15 分钟；第一条失败就停止，保留诊断。
5. 将 `results\日期时间\AMD-voice-diagnostic.zip` 发给开发者，并说明听感是否正常。生成的 `voice-1.wav`、`voice-2.wav` 可直接播放。

如果不方便下载依赖，双击 `2-Diagnose-Only.cmd`，先发送硬件诊断。Windows 10 或未发现 AMD 显卡会停止推理并生成诊断包。
ROCm 官方 7.2.1 Windows 表列出部分 RX 9000、RX 7900 XTX、RX 7700 和专业型号；检测到 AMD 不代表型号受支持。
请按官方文档准备兼容驱动（该版本文档列 26.2.2），工具不会自动安装或降级显卡驱动。

## 结果与隐私

诊断 ZIP 只包含硬件/运行版本、算子和生成阶段、错误类别及无路径堆栈位置、峰值显存/耗时、两条固定测试文本及成功生成的音频。
不包含配置、参考音频、私人信件、账号、密钥、完整本机路径或原始日志，不自动上传。
`local-*.log` 仅保存在本地，可能有本机路径，不要直接公开发送整个 `results` 文件夹。
显示 completed 只说明两条语音生成完成，不代表听感合格或其他型号兼容。

第一版固定 `bf16` 计算、INT8 hybrid 权重、eager 注意力和量化后端，不启用 CUDA Graph、FlashAttention 或 Triton。
这版用于定位兼容性，eager 量化可能较慢；AMD 专用加速内核在收到实机结果后再验证。
不更换模型、不重训 LoRA。模型许可随 `assets/model` 提供；保留其研究及非商业使用限制。
删除整个解压目录即可移除测试环境；不会修改现有应用。

## 上游依据

- https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2.1/docs/install/installrad/windows/install-pytorch.html
- https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2.1/docs/compatibility/compatibilityrad/windows/windows_compatibility.html
- https://github.com/Saganaki22/ComfyUI-Breeze-TTS-2
- https://github.com/Comfy-Org/comfy-kitchen

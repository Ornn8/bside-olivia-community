# B07 原版视觉驱动契约

状态：实现候选，尚未通过独立 Luna Max 审阅或总控多模态验收。

## 边界

B07 提供一个可替换的本地视觉 driver 接口。输入必须是 B01 私有 manifest 选出的原版 Olivia 帧：运行时只接收逻辑 asset reference 和已解码的原版帧，不接收源路径，不读取仓库内媒体，也不安装 LiveTalking/MuseTalk 权重。

driver 后端只能返回内存中的候选帧；compositor 只把显式 `speaking_mask` 内、且未被保护区域覆盖的像素写入结果。脸部、发型、服装、肤色、构图、背景、光照和清晰度区域始终从原版帧复制；脸部 mask 可以提供不含说话区域的轮廓，若覆盖说话区域则安全回退。缺少后端、mask、合法输出或后端抛错时，结果为 `FALLBACK`，返回原版帧的精确副本；不会返回黑帧、空帧或近似替代。

代码不生成、编码、保存或提交媒体。测试中的 numpy 数组是 synthetic fixture，不是原版视觉证据；实际原版输入和逐帧证据只能留在本机被忽略的 `.evidence/`。

## 公开接口

- `visual_driver.OriginalVisualFrame`：校验 B01 `private_asset_manifest` 中唯一的 image/video asset reference、状态、帧时间和帧格式；公开元数据不包含 manifest 路径、哈希、任意 metadata 或帧字节。
- `visual_driver.VisualDriverRequest`：携带原版帧、说话区域 mask 和可选的 `face`、`hair`、`clothing`、`skin`、`framing`、`background`、`lighting`、`clarity` 保护区域。
- `visual_driver.VisualDriver`：调用注入的 backend，并对候选帧做尺寸、dtype、保护区域和 fallback 检查；可选 `quality_guard` 返回明确的布尔拒绝时也回原版；`coverage()` 固定覆盖 B01 的十个状态。
- `visual_driver.VisualDriverResult`：只在内存返回帧；`public_dict()` 只输出脱敏状态、fallback 原因和边界标记。
- `visual_driver.unavailable_av_sync()`：B05/B06 尚未提供真实音频时间戳时的稳定接口，固定返回 `status=UNAVAILABLE`、`value=null` 和 `reason=b05_b06_runtime_unavailable`，不猜测零偏移。

状态覆盖为 `day`、`dusk`、`night`、`idle`、`piano_performance`、`letter_reply`、`letter_reading`、`live`、`outfit_variants`、`scene_transitions`。缺少某个原版输入时，该状态标为 `FALLBACK_ONLY`，fallback 来源仍为 `original_frame`。

## 可复现证据

`tools/visual_driver.py report` 复用 B01 的私有输入边界，只从 `.evidence/` 读取经 manifest 校验的原版/候选帧、对应 source metadata、mask 和序列，并只在 `.evidence/` 写 JSON 报告；`coverage` 子命令生成不含路径的十状态覆盖报告。

报告的 `required_metrics` 固定提供：

| 字段 | 来源 | 缺失时 |
|---|---|---|
| `pixel` | exact pixel diff | `UNVERIFIED` |
| `ssim` | SSIM | `UNAVAILABLE` 或 `UNVERIFIED` |
| `lpips` | 显式 provider hook | `UNAVAILABLE` |
| `identity` | 显式 provider 或人工复核 | 未配置时 `UNVERIFIED` |
| `flicker` | 同长度同时间序列 | `UNVERIFIED` |
| `background_drift` | 显式 background mask | `UNVERIFIED` |
| `color` | LAB 色差 | `UNVERIFIED` |
| `fps` | reference/candidate metadata | `UNVERIFIED` |
| `av_sync` | B05/B06 contract | 固定 `UNAVAILABLE` |

`region_integrity` 另外记录 face、hair、clothing、skin、framing、background、lighting、clarity 八个区域；没有对应 mask 就保持 `UNVERIFIED`。所有阈值保持 `UNFROZEN`，`acceptance_status` 保持 `UNVERIFIED`；自动指标不替代总控在同分辨率同时间点实际观看原版与候选。

示例命令：

```text
rtk python tools/visual_driver.py coverage --available-state live --output .evidence/b07/coverage.json
rtk python tools/visual_driver.py report --state-id live --manifest .evidence/b07/manifest.json --reference .evidence/b07/reference.png --candidate .evidence/b07/candidate.png --reference-metadata .evidence/b07/reference.json --candidate-metadata .evidence/b07/candidate.json --output .evidence/b07/report.json
```

路径、哈希、原版帧、候选帧和 mask 不进入提交；提交内容只包含接口、schema、测试、文档和证据生成器。

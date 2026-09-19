# B09 原版音乐、演奏与状态切换契约

状态：实现候选，尚未通过独立 Luna Max 审阅或总控多模态验收。

## 边界

B09 只提供本地、可装卸的媒体状态层，不改 `local_server.py`、LLM、人格或回信编排。曲目由调用方在运行时组装；每个音频/视频选择器必须来自 B01 的私有 `private_asset_manifest`，源路径、媒体、哈希和运行时 asset reference 只能留在本机 `.evidence/`。

代码没有网络下载、替代曲生成、MIDI 生成或默认播放器。没有注入 `MediaProvider` 时，播放请求返回 `PLAYBACK_PROVIDER_UNAVAILABLE`；没有 manifest 命中的音频/视频时，返回带错误码的失败，不创建假播放或黑屏成功。

## 公开接口

- `ManifestAssetResolver`：读取外部 B01 manifest 和显式本机 root 映射；每次解析重新检查 root containment、文件存在、字节数和 SHA-256。`probe_status=unavailable` 只表示探测工具不可用，仍会按实际文件和哈希校验；`probe_status=error` 拒绝使用。
- `TrackDefinition` / `MusicCatalog`：只保存运行时 `track_id`、音频 manifest reference、可选的状态视频 reference 和显式 fallback reference。提交的 schema/example 不包含任何实际 reference。
- `MediaStateMachine`：接收 `play`、`pause`、`stop`、`seek`、`switch_track`、`switch_state`、`recover` 命令，返回可轮询的 `OperationResult`。通过 `OperationHandle.wait()`、`status()`、`cancel()` 和 `retry()` 管理任务。
- `MediaProvider`：由后续本地音频/视频后端注入 `set_source`、`pause`、`stop`、`seek`、`set_visual`。provider 必须对取消保持原子性；状态机只在 provider 成功后提交状态。
- `MediaEvent`：事件只有 operation、命令、状态、脱敏 snapshot 和错误码，不含源路径、manifest reference 或媒体内容，可由 B08 订阅。

## 状态与幂等

时间状态为 `day`、`dusk`、`night`；演奏状态为 `idle`、`piano_performance`；播放状态为 `stopped`、`playing`、`paused`、`error`。所有修改在单一异步锁下按提交顺序执行，并由 revision 单调递增标记。

重复的 `play`、`pause`、`stop`、相同 `seek` 或相同状态切换返回 `NOOP`，不会重复调用 provider。相同 `request_id` 重复提交返回同一 operation；相同 request id 搭配不同命令返回 `REQUEST_ID_REUSED`。失败任务可用新 operation `retry()` 重试；provider 失败先进入 `error`，只能通过成功的 `recover`（停止 provider 并回到 `stopped`）清除。

取消不会提交半成品状态；播放 provider 的取消安全由接口契约负责。缺资产的默认策略为 `error`。只有运行时 catalog 明确声明 fallback 且启动 `use_declared_fallback` 时才使用原版 fallback；`silent` 只会保持当前视觉而跳过不可用的目标视觉，不会伪造媒体已播放。

## 可执行证据边界

自动化测试只使用 pytest 临时目录中的 synthetic fixture，验证 manifest 解析、缺文件、坏 hash、正常控制、状态切换、fallback、重复请求、并发串行、取消、恢复、provider 缺失和事件隐私。测试不是原版音乐听审，也不是实际播放器验收。

- 视觉证据：`UNVERIFIED`。当前窗口没有播放/查看私有 B01 原版片段，也没有生成候选对照；没有宣称弹琴画面、服装、场景或白天/黄昏/夜晚视觉等价。
- 听觉证据：`N/A`。本批没有在真实音频设备上播放原版音轨，没有采样率、起止、无缝拼接、音画同步或人工听审记录；synthetic fixture 不构成听觉证据。
- 权利/分发边界：`UNVERIFIED`。实现只约束资产来源为本机 B01 manifest，不替总控确认原版权利；原版媒体、manifest、路径和 `.evidence` 不进入 Git 或分发包。

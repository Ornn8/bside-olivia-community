# 林离本地陪伴与 Live 扩展主计划

本文件是长期技术计划与验收背景，不是当前状态板；截至 2026-08-12，产品能力仍不可宣称完成。当前工作项状态唯一见 [`STATUS.md`](STATUS.md)，治理流程唯一见 [`PROJECT_MANAGEMENT.md`](PROJECT_MANAGEMENT.md)。

B11 的可执行验收 ledger 与冻结 reviewer 流程见 [`B11_ACCEPTANCE_STANDARD.md`](B11_ACCEPTANCE_STANDARD.md)。

## 0. 目标与不可降级边界

目标是在 Windows + RTX 3080 10 GB 上，尽量复现原版 BSide: Olivia Lin 的交互，并增加可切换的 Live 实时对话。LLM 通过可配置 API；其余组件尽量本地；只组装成熟开源技术，不训练新模型。

不可降级要求：

1. 原版角色视频、立绘、背景、镜头、光照、服装和 UI 叠层是唯一视觉基准。禁止通用 Live2D/VRM、AI 重绘、风格近似角色或重设计背景。
2. 原版资产只允许通过本机绝对路径或受校验 manifest 引用；原版程序、解包资产、模型权重、音视频和生成媒体不进入 Git 或任何分发包。
3. LiveTalking/MuseTalk 只能作为实时说话时的可替换驱动层，不得改变脸型、发型、服装、肤色、构图、背景、光照、清晰度或整体身份。无法满足时必须回退原版片段组合或无损方案。
4. 人格只按 `PERSONA_DISTILLATION_TASK.md` 后续执行；当前 DRAFT 不得作为官方最终人设，不训练新模型，不复制受版权保护的大段原文。

## 1. 当前事实（以文件和命令为准）

- 当前只有本地 HTTP 兼容原型，没有可运行的原版 WebSocket、Live、ASR、TTS 或完整降级链路。
- `local_server.py` 中运行时代码的官方转发路径已移除；发信先确认 PENDING，LLM 失败随后写入可查询终态，MIDI 生成和未实现写操作返回实际 HTTP 501；`NOT_IMPLEMENTED` 是终态。
- 当前 baseline hardening pytest 收集 25 项；这只是本批安全/状态回归，不代表整产品全绿。根目录 `test_cosyvoice3.py` 仍是历史一次性脚本，旧收集曾为 0。
- 本机目录快照：`0.0.9.615` 为 3,711,516,860 bytes / 183 文件；`CosyVoice` 为 15,484,055,181 / 58,681；`LiveTalking` 为 9,928,926,801 / 67,626；`olivia_assets` 为 5,004,535,016 / 145；`assets_extracted` 为 15,159,776 / 24；`output_audio` 为 112,759,722 / 12。它们均不进入提交。
- 本机有 5 对 `letter_pairs` 原文，仅作本地审计输入，不跟踪原文；真实配置、记忆、日志、模型、媒体和 `.evidence/` 均被忽略。
- 现状详见 [`CURRENT_STATE_AUDIT.md`](CURRENT_STATE_AUDIT.md)，完整本批证据索引详见 [`docs/reviews/baseline-hardening-evidence.md`](reviews/baseline-hardening-evidence.md)。

## 2. 模块边界

| 模块 | 职责 | 可替换边界 | 计划/验证边界（非工作项状态） |
|---|---|---|---|
| 原版资产 manifest | 本机路径、SHA-256、状态/时间点和权利登记 | 只替换 manifest/片段组合，不替换视觉身份 | 未验证 |
| 原版 UI/本地兼容层 | 保持 `/toy/*` schema 和原版页面契约 | HTTP schema 与 provider 解耦；不外联官方路径 | 仅原型 |
| LLM Gateway | 文本生成、超时、脱敏错误和 provider 切换 | Ollama/OpenAI-compatible API；密钥只运行时注入 | 仅原型 |
| Companion/Live 编排 | 会话、取消、背压、时间戳和降级 | AIRI 或窄接口编排器 | 缺失/未验证 |
| MemoryProvider | 用户授权范围内的本地记忆、查看、清空、导入/删除 | `MemoryPort` + SQLite/FTS5；未来 Mem0 只能作为可选 local adapter | B04 已实现，默认关闭 |
| ASR | 麦克风音频到 partial/final 文本 | Nemotron 3.5 ASR Streaming 0.6B | 缺失 |
| TTS | 文本到带时间戳音频 | VoxCPM2；MOSS-TTS 仅离线可选 profile | 缺失 |
| Visual driver | 在原版帧/背景/叠层内做局部说话驱动 | LiveTalking/MuseTalk；失败回原版片段 | 缺失/未验证 |
| MIDI/音乐 | 原版音乐状态和片段契约 | 未实现时明确 501，不伪造 Processing/成功 | `NOT_IMPLEMENTED` |
| 安装 profile | 依赖、版本、SHA-256、healthcheck、停用/卸载/回滚 | 每组件独立 profile，保留原版资产和用户数据 | 未验证 |

## 3. 分阶段实施

### Phase 0 — 治理与安全边界

保持严格 `.gitignore`、安全配置模板、第三方清单、manifest schema、数据删除策略和本地 `.evidence/` 证据包。每次运行只保存不含密钥/正文/用户数据的日志、JUnit XML、编译输出、扫描退出码和 SHA-256 manifest。

### Phase 1 — 原版视觉/UI 基线

为白天、黄昏、夜晚、待机、弹琴、回信/阅读、Live 以及原版服装/场景切换建立同分辨率、同时间点截图/参考片段。执行静态区域像素误差、SSIM、LPIPS、面部身份、边缘闪烁、背景漂移、色差、帧率和音画同步测量；阈值先由执行者基线测量，再由独立 Luna Max 批准冻结。缺证据保持 `UNVERIFIED`。

### Phase 2 — 本地兼容层与文字回信

覆盖 `/toy/*` 的成功、缺字段、未知路由、LLM 超时/失败、CORS、取消和删除语义。禁止 placeholder 回信、假成功、官方转发和未实现写操作返回 200。当前 MIDI 只允许明确 `HTTP 501 + NOT_IMPLEMENTED`。

### Phase 3 — LLM、AIRI 与记忆

接入 LLM Gateway 后再评估 AIRI companion 内核；B04 先使用无框架依赖的 SQLite/FTS5 本地 adapter，满足数据权限、查看、清空、导入/整库删除和离线 fallback。AIRI Memory Alaya WIP 不在第一版依赖内；Mem0 只有在锁定版本、许可证、本地模式和无网络证据后，才可通过 `MemoryPort` 作为可选替换。

### Phase 4 — ASR/TTS 音频链路

先测 Nemotron Streaming ASR 的 partial/final 稳定性、WER 和首字延迟，再测 VoxCPM2 首包、持续延迟、显存和音色边界；MOSS-TTS v1.5 只作为离线高质量 profile，不以它掩盖 Live 指标未达标。

### Phase 5 — Live 视觉驱动

把 LiveTalking/MuseTalk 包在独立 driver 接口中，只接受原版参考帧/视频、背景和叠层。任何身份、构图、背景、光照、清晰度或同步退化都失败，自动回退原版视频片段/静态立绘；总控必须实际观看对比，不能只用自动指标放行。

### Phase 6 — 状态整合与降级

串联信件、Live、回信/阅读、待机、弹琴、音乐和场景切换。所有 provider/资产不可用时返回明确错误或原版片段/文本降级；不得黑屏、卡死、永久 Processing 或把单项冒烟当全链路通过。

### Phase 7 — 可装卸 profile

为 `core`、`llm-*`、`memory-mem0`、`asr-nemotron`、`tts-voxcpm2`、`tts-moss`、`live-driver`、`music/original-clips` 建立独立 manifest。安装前 dry-run，失败原子回滚；停用只改路由；卸载只删除 manifest 登记的依赖，保留原版资产、用户数据和可回滚信息。禁止 vendoring、Git LFS 收纳原版资产或模型。

## 4. 依赖与替换路径

| 候选 | 首选用途 | 替换/失败路径 |
|---|---|---|
| AIRI | companion 编排 | 上游不适用时保留窄接口编排器 |
| Mem0 local | 本地记忆 | session-only context 或 legacy 迁移层 |
| Nemotron ASR Streaming 0.6B | Live ASR | 文本输入 fallback |
| VoxCPM2 | Live TTS | 明确错误或原版片段；MOSS 只离线 |
| LiveTalking/MuseTalk | 可替换视觉 driver | 原版视频片段/静态立绘，不降低视觉标准 |
| 可配置 LLM API | 文本生成 | 脱敏失败状态，不伪造回信 |

## 5. 风险与放行

权利/隐私、原版视觉身份、10 GB 显存、延迟、模型可用性、API/安装失败和数据清除都是阻塞风险。任何缺少测试收集项、完整日志、人工视觉/听觉证据、独立 Luna Max diff 审查或总控多模态观看的批次均只能标为 `BLOCKED/UNVERIFIED`。

后续每批必须从 `origin/main` 创建 `codex/<milestone>-<slug>`，使用带测试/文档的原子提交；同一实施窗口创建的独立 `gpt-5.6-luna/xhigh` 审查测试、diff、资产 manifest、许可证/隐私和视觉/听觉证据；`PASS` 后通过指向 `main` 的非 draft PR 交总控合并，禁止直接更新 `main`。合并后必须回归远端 `main`。

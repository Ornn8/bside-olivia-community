# 后续开发派工板

状态：可独立派发的小批次输入/输出与证据模板；本文件不记录当前工作项状态（2026-08-12）。
治理规则唯一入口见 [`PROJECT_MANAGEMENT.md`](PROJECT_MANAGEMENT.md)；工作队列和动态进展以 [GitHub Issues](https://github.com/Ornn8/bside-olivia-community/issues) 与 [Milestones](https://github.com/Ornn8/bside-olivia-community/milestones) 为准；[`STATUS.md`](STATUS.md) 只保留状态语义和入口链接。本文件只保留批次细节，不得替代治理入口或队列事实源。

## 0. 每批通用交付格式

架构硬约束（blocking）：只组装，不自己造。优先复用经过验证、较新且活跃维护的 GitHub/Hugging Face 上游；本仓库只实现必要的薄 adapter/connector、接口契约、配置、编排、生命周期管理、可装卸机制和验收测试。若发现现有实现越界，先收缩为 adapter/connector，并在 docs、manifest、provenance 中记录 upstream、固定 version/commit/license、替换边界和卸载路径。

唯一独立 `gpt-5.6-luna/xhigh` reviewer 必须把 ARCH-01“只组装，不自己造”列为 blocking 检查项；发现重写上游核心能力、平替引擎、自研框架化或缺少 upstream/provenance/卸载证据时只能 `FAIL`。

本板中的“独立 Luna”统一指唯一的 `gpt-5.6-luna/xhigh` reviewer。根任务/总控只负责宏观编排、任务布置、Git 运输、门禁验收以及最终视觉/听觉/文字多模态验收，不承担具体实现或修复；所有具体开发与修复必须在本项目独立 Codex 任务中由 `gpt-5.6-luna/xhigh` 实施窗口完成。实施任务冻结最终 diff 后必须恰好创建这一名 reviewer，明确 `PASS` 后才可交总控；`FAIL` 只能退回同一实施任务并由同一 reviewer 复核，超时或无 verdict 不是 `PASS`。

每个批次必须附：

- 输入清单、输出清单、依赖版本和许可证；
- 实际执行的完整测试命令、完整退出码、实际测试收集数量和脱敏日志；每个适用完整套件必须 `collect > 0` 且 `failed=0`、`errors=0`、`skipped=0`；
- 视觉证据：适用时给出原版基准/候选同分辨率同时间点对比；不适用时明确写 `N/A`，不能留空；
- 听觉证据：适用时给出原版参考/候选音频、采样率、时间戳、听审；不适用时明确写 `N/A`；
- 文字证据：适用时给出实际文本输出、输入/输出边界和总控阅读记录；不适用时明确写 `N/A`；自动文本指标不能替代总控阅读；
- 安全边界：没有真实密钥、玩家数据、原版媒体、模型权重进入提交；
- 唯一独立 `gpt-5.6-luna/xhigh` reviewer 的 review 记录和“可交总控/退回修改”结论。

所有适用正常管线必须无阻塞；任何批次发现上游下载、权利、显存、路径或数据缺口，只能标为 `BLOCKED/UNVERIFIED`。真实 `UNAVAILABLE` 只能作为未就绪事实，不能冒充 `READY`/完成；不能用 placeholder 冒充完成。

## 1. 批次清单

### B00 — 治理基线与秘密边界

- 输入：当前目录、现有源码/文档/原版资产、敏感扫描结果。
- 输出：`.gitignore`、`contracts/llm_config.example.json`、正式文档、初始 Git 基线和跟踪体积清单。
- 依赖：无；不需要模型或官方服务。
- 测试命令：`rtk git status --short`、`rtk git diff --check`、脱敏 secrets scan、`rtk git ls-files`、大文件体积扫描。
- 视觉证据：记录原版资产目录和未来 manifest 边界，媒体不复制；当前批次 `N/A`。
- 听觉证据：记录本机音频输出目录被忽略；当前批次 `N/A`。
- 完成定义：无秘密、无原版/权重/用户数据进入提交；独立 Luna Max 审查 diff 后才交总控。
- 阶段说明：本轮基线批次；实施任务冻结后必须由唯一独立 `gpt-5.6-luna/xhigh` reviewer 审查通过；当前工作项状态以对应 GitHub Issue/Milestone 为准，`STATUS.md` 只提供状态语义和入口链接。

### B01 — 原版资产本机 manifest 与视觉基线

- 输入：原版程序、解包前端、原版角色视频/立绘/背景/镜头/UI 叠层路径；全部只读。
- 输出：本机 manifest、SHA-256、分辨率/帧率/时间码、状态枚举和视觉基线截图/参考片段索引；不提交媒体。
- 依赖：B00；原版资产本机可读；合法使用边界已登记。
- 测试命令：`rtk python <asset-manifest-check.py> --root <local-original-root>`、路径存在/哈希复核、重复资产检查。
- 视觉证据：白天/黄昏/夜晚、待机、弹琴、回信/阅读、Live、原版服装/场景切换逐项对照；测量像素、SSIM、LPIPS、身份、闪烁、背景漂移、色差。
- 听觉证据：为有音轨片段记录原版音频哈希、时长、采样率和音画同步；无音轨项标 `N/A`。
- 完成定义：基线阈值由实施任务先测量并写入证据；最终 diff 冻结后由唯一独立 `gpt-5.6-luna/xhigh` reviewer 连同阈值一起审查并明确 `PASS`；缺基线/阈值不得交总控。
- 阶段说明：候选批次模板；实施任务冻结后必须由唯一独立 `gpt-5.6-luna/xhigh` reviewer 审查通过；当前工作项状态以对应 GitHub Issue/Milestone 为准，`STATUS.md` 只提供状态语义和入口链接。

### B02 — 原版 UI 与本地 HTTP 契约

- 输入：`local_server.py`、已确认的前端调用路径和脱敏 schema；不使用官方写接口。
- 输出：本地 route schema、fixture、错误码表、健康检查和覆盖矩阵；明确未实现接口。
- 依赖：B00；B01 的资产 ID；本地 aiohttp。
- 测试命令：`rtk pytest -q tests/http`、`rtk python <healthcheck.py> --profile core`、`rtk python -m compileall -q <source-roots>`。
- 视觉证据：原版信箱/回信/阅读/音乐页面截图与本地对照；证明 UI 叠层和状态不被替换。
- 听觉证据：页面无音频时写 `N/A`；有原版片段时给音轨/播放状态证据。
- 完成定义：正常、缺字段、错误、重试、空数据路径均有测试；placeholder 不得被标为完成；Luna Max 审查后交总控。

### B03 — LLM Gateway 与回信编排

- 输入：OpenAI 兼容 API/Ollama 配置模板、消息 schema、人格配置接口；不提供真实 key。
- 输出：可取消/有界超时的 Gateway、后端切换、脱敏日志和文本断言测试。
- 依赖：B02；后续人格任务书的配置 schema；本地或测试 mock LLM。
- 测试命令：`rtk pytest -q tests/llm`、无 key 启动、mock timeout/cancel/error、双后端 contract test。
- 视觉证据：回信生成后原版阅读/回信 UI 对照；无 UI 改动时仍提供截图或 `N/A` 说明。
- 听觉证据：当前只生成文本时为 `N/A`，不得把文本通过冒烟当成 TTS 证据。
- 完成定义：LLM 不可用时明确错误/文本降级，不调用官方 endpoint；Luna Max 复核 prompt 注入和 secrets diff 后交总控。

### B04 — Mem0 本地记忆适配器

- 输入：现有 `memory.py` 的迁移样本（只用脱敏 schema）、玩家同意/删除规则。
- 输出：`MemoryProvider`、Mem0 local profile、字段来源/保留期/删除/导入导出和迁移报告。
- 依赖：B03；本地数据库/加密与备份策略；不得提交 `memory_store.json`。
- 测试命令：`rtk pytest -q tests/memory`、新增/检索/重启/删除/导入导出/串用户测试、敏感字段扫描。
- 视觉证据：回信/Live 多轮上下文在原版 UI 中显示正确；不得截图出真实玩家信息。
- 听觉证据：若记忆只影响文本，写 `N/A`；接入 Live 时需证明上下文切换不造成重复/串音。
- 完成定义：玩家可查看和删除；Mem0 不可用时只保留明确 session-only context；Luna Max 审查数据流后交总控。

### B05 — Nemotron 流式 ASR

- 输入：本机麦克风/授权测试音频、事件 schema、RTX 3080 profile。
- 输出：固定 Nemotron/NeMo-Speech.cpp provenance、partial/final 转写服务、单调时间戳、断句、取消/静音/断连/背压处理和 CPU/无模型诊断；native 未经 Windows CUDA + RTX 3080 原生 WebSocket 实跑保持 `UNAVAILABLE`。
- 依赖：B03/B04；Nemotron 3.5 ASR Streaming 0.6B 权重和许可证；不得提交权重。
- 测试命令：`rtk pytest -q tests/asr`、`rtk python tools/asr_healthcheck.py`、`rtk python tools/healthcheck.py --profile asr`、延迟/WER 测试、断网/断模型测试；默认不联网/不下载大模型。
- 视觉证据：只需证明 partial/final 状态在原版 Live UI 中正确，截图不得含原始用户身份。
- 听觉证据：原始音频哈希、partial/final 时间戳、保留集 WER/人工听辨；测试音频不能用于模型选择/校准。
- 完成定义：达到已批准的延迟/准确率预算；未达到就保留文本输入回退；Luna Max 审查日志和音频证据后交总控。

### B06 — VoxCPM2 TTS 与 MOSS 离线 profile

- 输入：B03 文本事件、已登记的本机参考音频、VoxCPM2；MOSS-TTS 仅可选离线 profile。
- 输出：TTS provider、首包/结束时间戳、采样率/峰值检查、取消与无声错误；不复制参考音频。
- 依赖：B05 的时间戳；显存预算；权重和许可证清单。
- 测试命令：`rtk pytest -q tests/tts`、短/长文本、打断/连续、爆音/截断检查、`rtk nvidia-smi` 资源记录。
- 视觉证据：原版画面/Live 画面保持构图和清晰度，附同时间点视频对照。
- 听觉证据：参考/候选采样率、时长、峰值、静音段、音画同步和人工听审；单个 wav 生成不算通过。
- 完成定义：VoxCPM2 Live 和 MOSS offline 边界明确；显存/延迟未达标时保留文本或原版片段降级；Luna Max 审查后交总控。

### B07 — LiveTalking/MuseTalk 可替换视觉 driver

- 输入：B01 原版基准帧/片段、B05 ASR、B06 音频、driver 接口。
- 输出：只驱动说话局部的 driver、静态区域保护、失败自动切片回退和逐状态指标报告。
- 依赖：B01、B05、B06；LiveTalking/MuseTalk 运行环境；不得使用通用角色资产。
- 测试命令：`rtk pytest -q tests/live_driver`、同分辨率同时间点指标、长会话帧率/同步测试、故障注入。
- 视觉证据：脸型、发型、服装、肤色、构图、背景、光照、整体清晰度逐项人工与自动确认；覆盖所有 VIS 状态。
- 听觉证据：同一输入音频在原版片段/driver 输出中同步；记录首帧、稳定帧和漂移。
- 完成定义：指标阈值和审查者已批准；任何身份/背景/清晰度退化都切原版片段，不能调宽阈值；Luna Max 复核后交总控。

### B08 — Live 会话编排与降级矩阵

- 输入：B03/B04/B05/B06/B07 事件和错误契约。
- 输出：会话状态机、打断/取消/背压、超时、用户可见状态和逐组件降级矩阵。
- 依赖：所有前置 provider 只能通过接口接入；不得把外部官方服务作为 fallback。
- 测试命令：`rtk pytest -q tests/live`、LLM/记忆/ASR/TTS/driver/资产逐项故障注入、长会话 trace。
- 视觉证据：Live、回信/阅读、待机、原版片段回退状态的同分辨率对照。
- 听觉证据：打断、重播、静音、TTS 错误和原版片段音轨均有时间戳/听审。
- 完成定义：没有黑屏、假成功、静默死等或串用户；所有降级保留原版视觉标准；Luna Max 复核后交总控。

### B09 — 原版音乐、演奏与状态切换

- 输入：原版曲库/片段/镜头 manifest、前端状态契约；本地 MIDI 只作输入 schema。
- 输出：原版片段组合或经视觉验收的无损方案、任务状态、取消/重试和播放回退。
- 依赖：B01/B02；权利和本机资产路径确认。
- 测试命令：`rtk pytest -q tests/music`、空/非法 MIDI、任务轮询、取消/重复、片段边界和音画同步测试。
- 视觉证据：弹琴、曲目切换、原版服装/场景、白天/黄昏/夜晚同时间点对照；拒绝通用/近似数字人替代。
- 听觉证据：原版音轨或授权本地音轨的起止、采样率、无缝拼接和同步；没有音频就不能宣称演奏完成。
- 完成定义：未有无损方案时明确 `BLOCKED`，不把“处理中”当成生成完成；Luna Max 审查后交总控。

### B10 — 安装、停用、卸载与回滚

- 输入：各 profile manifest、锁版本、许可证、安装根目录和 preserve 列表。
- 输出：干净环境安装器/健康检查/dry-run/卸载清单/回滚策略；只下载到用户指定外部根目录。
- 依赖：B00 规则；各组件已通过单独健康检查；网络/离线缓存边界明确。
- 测试命令：`rtk pytest -q tests/packaging`、干净环境安装、单组件停用/卸载/重装、路径/哈希/误删检查。
- 视觉证据：安装前后原版画面和 UI 截图一致；卸载不能删除或改写原版资产。
- 听觉证据：停用 TTS/Live 后文本或原版片段的明确回退；记录音频设备未被错误占用。
- 完成定义：任何组件可卸载，core 和用户数据仍可用；Luna Max 审查删除清单和回滚后交总控。

### B11 — 总验收与多模态审查

- 输入：B00-B10 的分支、审查记录、证据包、`ACCEPTANCE.md`。
- 输出：完整测试报告、视觉/听觉指标和人工观看记录、显存/延迟报告、风险清单与最终结论。
- 依赖：所有适用批次均由唯一独立 `gpt-5.6-luna/xhigh` reviewer 明确 `PASS`；所有适用正常管线无阻塞；holdout 与校准数据分离；无适用 `BLOCKED` 或 `UNAVAILABLE`。
- 测试命令：`rtk pytest -q`、全量 component healthcheck、E2E harness、安装卸载回归和秘密/大文件扫描；完整测试必须实际收集且 `failed=0`、`errors=0`、`skipped=0`。
- 视觉证据：总控实际观看所有规定状态的原版/候选同分辨率对比；自动指标不能替代观看。
- 听觉证据：总控实际听取 ASR/TTS/原版片段关键样本并签署同步/质量结论；自动听觉指标不能替代听审。
- 文字证据：总控实际阅读规定文本输入/输出和边界案例并签署结论；自动文本指标不能替代阅读。
- 完成定义：[`ACCEPTANCE.md`](ACCEPTANCE.md) 与 [`B11_ACCEPTANCE_STANDARD.md`](B11_ACCEPTANCE_STANDARD.md) 的所有适用行 PASS，唯一独立 `gpt-5.6-luna/xhigh` reviewer 明确 PASS，总控完成实际多模态验收后才合并 main；否则退回对应批次。

## 2. 分支、PR、提交与合并协议

标准顺序固定为：GitHub Issue → 从最新 `main` 创建 `codex/<milestone>-<slug>` 分支 → 独立 Codex 实施任务 → 冻结最终 diff → 恰好一名独立 `gpt-5.6-luna/xhigh` reviewer 明确 `PASS` → 创建 PR → 远端 required CI 全绿 → 总控门禁与实际视觉/听觉/文字多模态验收 → merge → 远端 `main` 回归。总控负责提交、push、PR、merge 和回归的 Git 运输；只允许提交与该批输入/输出/测试相关的文件，禁止总控或执行者直接更新 `main`。提交信息建议：

```text
codex/<milestone>-<slug>
feat(<milestone>): <one atomic change>
test(<milestone>): <evidence or regression coverage>
docs(<milestone>): <contract or acceptance update>
```

实施任务完成后先自检并冻结最终 diff，再由该实施任务创建恰好一名独立 `gpt-5.6-luna/xhigh` reviewer；审查者必须查看 `git diff --check`、完整测试收集结果、实际变更范围、资产/秘密扫描和视觉/听觉/文字证据。FAIL 只回同一实施任务修复并由同一 reviewer 复核；不得创建第二名 reviewer，超时不是 PASS。总控合并前再次运行适用验收项；总控不能用“另一个批次已通过”覆盖本批缺失证据。

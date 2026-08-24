# P03-01C MiniMax Music 3 人声＋钢琴抒情风格稳定化

## 1. 目标

在保留现有音乐视频装配链的前提下，提高 MiniMax Music 3 音频输出的稳定性，使音乐回复更接近：

- 普通话成年女声；
- 一台原声三角钢琴承担完整伴奏；
- 克制、清晰、自然的抒情表达；
- 少量而明确的情绪与力度变化；
- 不因宽泛风格词而滑向 R&B、Soul、影视配乐、民族器乐融合或大编制；
- 歌词与来信、canonical reply 和林离人格一致；
- 音乐生成失败时不影响已经完成并持久化的 canonical text。

本工作包只重构 `song_content.py -> MiniMax Music 3` 的内容规划、正向 Caption、参数基线、实验与音频验收。现有说话视频、RoFormer、LatentSync、转场和 FFmpeg 拼接链不在本工作包中重新设计，但必须做回归验证。

## 2. 上游依据与证据优先级

设计只以一手资料为主要依据：

1. MiniMax Music 3 官方 README：Lyrics 与 Music Description 分离；推荐 `Global Metadata / Vocal Details / Arrangement`；同时明确节奏、调性、配器、歌词和结构只是生成控制，并非严格符号保证。
2. MiniMax 官方 `music-caption-rewriter` Skill：具体说明配器角色、演奏方法、段落生命周期和人声表现，比宽泛情绪词更重要。
3. Comfy-Org 官方 MiniMax Music 3 工作流：
   - `MiniMaxMusic3TextEncode` 的官方示例为 CFG 1.7、top_k 50；
   - KSampler 为 Euler、30 steps、CFG 1.7、simple scheduler、denoise 1.0；
   - negative conditioning 使用 `ConditioningZeroOut`，没有自然语言负向 Caption 输入。
4. MiniMax 官方 Issue：段落标签、目标时长与歌词唱完仍可能漂移，验收不能只检查请求结构。

社区提示词和论坛经验只用于提出实验候选，不直接写成生产事实。任何生产参数都必须经过本机固定样本和盲听证据。

上游参考：

- `https://github.com/MiniMax-AI/MiniMax-Music3`
- `https://github.com/MiniMax-AI/MiniMax-Music3/tree/main/skills/music-caption-rewriter`
- `https://github.com/Comfy-Org/workflow_templates/blob/main/templates/audio_minimax_music_3.json`
- `https://github.com/MiniMax-AI/MiniMax-Music3/issues/1`

## 3. 已确认的现有问题

### 3.1 当前没有可写自然语言的负面提示通道

仓库当前 ComfyUI 图与官方工作流一致：KSampler 的 negative 输入来自 `ConditioningZeroOut`。因此不能把下列文字写入正向 Caption：

```text
no R&B
without drums
no strings
avoid soul
```

它们仍会进入正向文本编码，可能反而强化对应概念。

实现纪律：

- 发送给 MiniMax 的 Caption 只描述希望出现的声音；
- 禁止项只存在于程序结构验证、测试和人工评分表；
- 最终 Caption 中不出现被排除风格和乐器的名称。

### 3.2 自由 Caption 权限过大

当前 `song_content.py` 让文本模型自由生成完整 Caption，只检查少量必需字段。模型可以在保留钢琴、女声、普通话等词的同时加入额外配器、groove、背景和声或宽泛风格标签，仍然通过验证。

### 3.3 风格词会带入训练先验

`heritage-leaning`、`cinematic`、`ambient pop`、`modern ballad` 等词可能把模型路由到民族器乐、弦乐、合成器、鼓组或 R&B 邻近风格。官方模板库也说明，即使“钢琴是基础”，具体风格模板仍可能继续加入 pad、strings、bass 或 percussion。

生产链不再从宽泛风格库自由选模板。模板库只用于研究与对照，不能覆盖本项目固定配器契约。

### 3.4 Worker 兜底主动要求额外乐器

`tools/minimax_music3_worker.py` 的现有 fallback Caption 包含 strings、cello 和 percussion。这会直接要求额外乐器出现，必须移除。

### 3.5 当前参数偏离官方基线

仓库当前 Text Encode CFG 和 KSampler CFG 均为 1.5；官方 ComfyUI 工作流示例均为 1.7。参数差异不一定是 R&B 或额外乐器的主因，因此必须与 Caption 改造分阶段验证，不能一次修改全部变量后凭主观判断归因。

### 3.6 固定单 seed 无法证明稳定性

生产链固定 `seed=200717`。单个 seed 可能持续放大特定节奏、唱腔或配器偏向，也无法判断问题来自 Caption、参数还是采样随机性。

## 4. 核心设计

### 4.1 文本模型只输出受限语义计划

文本模型不再编写最终 Caption，只返回严格 JSON：

```json
{
  "schema_version": "p03.song-semantic-plan.v1",
  "emotion_arc": "gentle_reassurance",
  "piano_texture": "transparent_broken_chords",
  "vocal_delivery": "clear_legato",
  "dynamic_arc": "soft_gentle_rise_settle",
  "ending": "complete_soft_cadence",
  "lyrics": "[Intro]\n..."
}
```

除歌词外，所有字段均为白名单枚举。模型不能返回：

- 自由流派；
- 自由配器；
- 制作插件或效果器；
- 额外演唱者；
- 自由 BPM、调性或拍号；
- 任意负面提示词。

### 4.2 程序构造唯一的正向 Structured Caption

新增 `music_caption.py`，由程序把枚举渲染为固定三段式 Caption。核心基线：

```text
### Global Metadata
An intimate Mandarin vocal-and-acoustic-grand-piano lyrical song at 68 BPM
in B-flat major, straight 4/4. The emotional movement is restrained,
clear and personal, rising gently in the middle and settling into a complete ending.
The recording has close, natural small-room acoustics and a transparent tonal balance.

### Vocal Details
One adult female Mandarin lead sings a clear centered melody with natural diction,
mostly syllabic legato phrases, a moderate range, smooth breath support and measured
phrase endings. The performance remains intimate and emotionally contained.

### Arrangement
The complete instrumental arrangement consists of one acoustic grand piano from the
opening through the final cadence. The left hand supplies simple tonal foundation and
harmonic movement; the right hand carries transparent broken chords, sustained voicings
and short lyrical responses. Every section change is created through register, voicing,
note density, dynamics, pedaling, phrasing and silence. The ending resolves with a
complete, quiet piano cadence.
```

注意：

- 不写被排除风格或乐器的名称；
- 不使用 `no / without / avoid` 句式；
- 不使用 `heritage`、`cinematic`、`ambient` 等容易扩大编制的路由词；
- “一位主唱＋一台钢琴”通过正向、完整的角色描述表达；
- 段落发展只能来自钢琴演奏参数，不允许文本模型增加新声部。

### 4.3 受限枚举

#### `emotion_arc`

- `quiet_longing`
- `gentle_reassurance`
- `restrained_sadness`
- `warm_gratitude`
- `soft_reconciliation`
- `calm_affection`

#### `piano_texture`

- `transparent_broken_chords`
- `lyrical_arpeggios`
- `measured_chordal_voicing`
- `sparse_counterline`

#### `vocal_delivery`

- `clear_legato`
- `gentle_narrative`
- `quiet_songful`
- `contained_intimate`

#### `dynamic_arc`

- `soft_gentle_rise_settle`
- `soft_steady_settle`
- `quiet_gradual_warmth`

#### `ending`

- `complete_soft_cadence`
- `lingering_piano_cadence`
- `short_settled_cadence`

新增枚举必须有本机样本证据，不因单封来信临时扩展。

### 4.4 歌词结构

正式产品只保留 40 秒和 60 秒短歌时长：

```text
[Intro]
[Verse]
...
[Chorus]
...
[Outro]
```

约束：

- Intro、Outro 不含歌词行；
- 40 秒为主歌 6 行加副歌 6 行，60 秒为主歌 8 行加副歌 8 行；
- 不生成第二主歌或中间奏，最终音视频同步渐暗收尾；
- 每行以自然可唱的中文短句为主；
- 不复制用户原文或现有歌曲；
- 不发明林离的过去；
- 不把安慰写成强迫乐观或心理咨询；
- 歌词和 canonical reply 来自同一来信，但歌词不是正文逐句改写；
- 生成后检查标签顺序、行数、空段和总字符范围；
- 本机验收检查歌词是否唱完，不能只依赖目标时长。

### 4.5 校准 provenance

40/60 秒主歌加副歌契约是独立于旧长歌证据的 v2 校准批次。每次盲听运行的
`manifest.json` 必须记录 `caseset_version: p03.music-cases.v2` 和
`caption_version: p03.minimax-caption.v2`；私有映射文件重复这两个版本，方便在
脱敏评分与私有音频之间核对。缺少这两个版本，或仍标记为 v1 的运行，不得与新
契约的结果混合、回填或宣称可比。

## 5. 目标代码结构

```text
song_content.py
  ├─ SongSemanticPlan
  ├─ 受限 LLM JSON 生成
  ├─ 枚举、歌词与标签验证
  └─ 不再接受自由 Caption

music_caption.py（新增）
  ├─ 固定正向核心模板
  ├─ 枚举 -> 正向片段
  ├─ render_minimax_caption(plan)
  └─ 快照测试

music_reply.py
  ├─ plan_song_content()
  ├─ render_minimax_caption()
  ├─ MiniMaxMusic3Worker.generate(...)
  └─ 保留既有视频拼接接口

tools/minimax_music3_worker.py
  ├─ 要求合法 lyrics + caption
  ├─ 删除错误自由 fallback
  ├─ 参数配置化
  └─ 支持离线批量 seed 实验

Control Center / 音乐校准页
  ├─ 创建固定实验队列
  ├─ 播放本机生成音频
  ├─ 填写人工评分
  ├─ 对比 seed / CFG /模板版本
  └─ 保存脱敏结果摘要
```

用户不需要运行 CLI。底层批量 runner 可以保留给自动化与开发测试，但正式校准入口必须在本地 Control Center 中完成。

## 6. 参数与实验顺序

### Phase A：只验证 Caption 架构

保持当前参数，隔离 Caption 改造效果：

- Text Encode CFG：1.5；
- top_k：50；
- KSampler CFG：1.5；
- Euler；
- 30 steps；
- simple scheduler；
- denoise 1.0。

比较：

```text
旧自由 Caption
vs
新固定正向 Caption
```

### Phase B：对齐官方参数候选

固定新 Caption 后，比较：

```text
1.5 / 1.5
vs
1.7 / 1.7（官方 ComfyUI 示例）
```

只有听感与稳定性证据支持时，才把生产默认改为 1.7 / 1.7。

### Phase C：seed 稳定性

使用至少 8 个 seed，所有输入和参数保持一致。依据盲听结果选出 3–5 个稳定 seed，组成生产 seed pool。

生产时：

```text
seed = stable_seed_pool[hash(letter_id) % len(stable_seed_pool)]
```

这样不同回信有变化，但不会在未经验证的全随机空间中漂移。没有可靠自动音频风格判别器时，不做在线多候选“自动挑最好”。

## 7. 固定验收样本

使用六类合成来信，不提交真实私人通信：

1. 普通安慰；
2. 失落与孤独；
3. 亲密日常；
4. 冲突后的修复；
5. 用户明确请求演奏；
6. 林离主动形成旋律构想。

每类至少：

- Phase A：4 个 seed；
- Phase B：2 组 CFG × 4 个 seed；
- Phase C：最终候选 seed 池复测。

评分：

| 指标 | 评分 |
| --- | --- |
| 钢琴是否承担完整伴奏 | 0 / 1 / 2 |
| 额外乐器明显程度 | 0 / 1 / 2，越高越严重 |
| R&B / Soul / groove 倾向 | 0 / 1 / 2，越高越严重 |
| 转音、ad-lib、背景和声倾向 | 0 / 1 / 2，越高越严重 |
| 普通话清晰度 | 0 / 1 / 2 |
| 克制抒情感 | 0 / 1 / 2 |
| 情绪与来信匹配 | 0 / 1 / 2 |
| 歌词可唱性与完整度 | 0 / 1 / 2 |
| 结尾完整度 | 0 / 1 / 2 |

初始门槛：

- 额外乐器平均严重度 <= 0.5；
- R&B / Soul / groove 平均严重度 <= 0.5；
- 钢琴完整伴奏、普通话、抒情感和情绪匹配平均 >= 1.5；
- 歌词明显未唱完或音频突然截断的样本为硬失败；
- 至少两轮盲听结论一致；
- 所有失败样本保留在本机证据中，不得只展示最佳样本。

## 8. 现有音乐视频拼接链

音乐回复并不是单独生成一段演唱视频。仓库当前链路是：

```text
canonical reply
  -> render_reply_video(...)
     生成说话视频 normal_video_path
  -> SongSemanticPlan + 固定正向 Caption
  -> MiniMax Music 3
     生成完整歌曲
  -> RoFormer
     分离 vocals
  -> render_latentsync_video(...)
     使用 vocals 驱动 performance base video
     生成 song_video_path
  -> concat_videos(...)
     normal video
     + 可选官方转场（原音频被静音）
     + performance video
  -> 最终 MP4
```

现有 `concat_videos()` 明确按 `说话视频 -> 可选静音转场 -> 演唱视频` 顺序拼接，并按各段帧数校验最终时长。

本工作包要求：

- 不改变该顺序；
- 不绕过说话视频开场；
- 不把 MiniMax 音频直接覆盖整段最终视频；
- 转场继续静音，避免旧素材音频泄漏；
- 音频改造后必须跑一次完整拼接回归；
- 最终口型、动作、转场和整体观感在用户本机验收。

## 9. PR 拆分与顺序

### MUSIC-01：受限语义计划 Contract

只增加 `SongSemanticPlan`、枚举、歌词结构和非法 JSON 测试，不切换生产链。

### MUSIC-02：固定正向 Caption Renderer

新增 `music_caption.py`、模板快照和枚举组合测试。最终 Caption 必须完全由程序生成。

### MUSIC-03：切换生产链

生产链改为：

```text
LLM -> SongSemanticPlan -> 程序 Caption -> MiniMax
```

同步更新 `song_content.py`、`music_reply.py` 和媒体边界测试。

### MUSIC-04：Worker 清理与官方参数入口

- 删除含额外配器的 fallback；
- 缺失合法 Caption 时明确失败；
- 暴露 Text CFG、KSampler CFG、top_k、steps、seed；
- 默认先保持旧参数，实验后再决定生产值。

### MUSIC-05：Control Center 音乐校准页

- 固定样本队列；
- 批量生成；
- 音频播放器；
- 盲听评分；
- 结果导出；
- 不显示真实私人来信。

### MUSIC-06：本机 Phase A / B / C 实验

没有本机生成和人工评分证据不得完成。

### MUSIC-07：确定生产 Caption、CFG 和 seed pool

写入生产配置、证据摘要和回滚点。

### MUSIC-08：完整视频拼接回归

只验证音频改造没有破坏：

```text
spoken -> optional transition -> performance
```

不在该 PR 中修改 LatentSync、RoFormer 或 FFmpeg 视觉参数。

## 10. 回滚

- 旧 Caption 仅保留一个发行周期的对照开关；
- 新 Caption 或参数退化时，文字和说话视频继续可用；
- 音乐失败不回滚 canonical reply；
- Caption 版本、参数和 seed pool 集中配置；
- 回滚不删除本机实验音频和评分记录；
- 不通过重调视频参数掩盖音频风格问题。

## 11. 完成条件

- MiniMax 不再接收文本模型自由生成的完整 Caption；
- 所有 Caption 来自固定正向模板；
- 发送给 MiniMax 的 Caption 不包含禁止项名称或负向句式；
- Worker 删除 strings、cello、percussion 等错误兜底；
- Phase A / B / C 已在本机完成；
- 生产 Caption、CFG 和 seed pool 有盲听证据；
- 用户可在 Control Center 完成校准，不需要终端；
- 歌词唱完率和结尾完整度达到门槛；
- 说话视频＋可选转场＋演唱视频拼接链保持可用；
- canonical reply 在任何音频或视频失败时均保留；
- `public-smoke` 与专项测试通过。

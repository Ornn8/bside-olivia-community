# P03-01C MiniMax Music 3 传统抒情风格稳定化

## 1. 目标

在不改动现有视频渲染、RoFormer、LatentSync 和拼接链的前提下，提高音乐回复的音频风格稳定性：

- 普通话成年女声；
- 原声三角钢琴作为完整伴奏；
- 传统、克制、清晰的华语抒情表达；
- 降低 R&B 唱腔、切分 groove 和额外乐器出现率；
- 保持歌词、情绪和来信内容之间的相关性；
- 失败时不影响已经完成的 canonical text。

本工作包只优化 `song_content.py -> MiniMax Music 3` 的音频输入与验收，不宣称自动解决最终视频观感。

## 2. 已确认的模型边界

MiniMax Music 3 接收两类输入：

- Lyrics：歌词和段落标签；
- Music description / Structured Caption：风格、人声、配器、段落发展和制作质感。

官方推荐 `Global Metadata`、`Vocal Details`、`Arrangement` 三段式 Structured Caption，但也明确说明这些描述属于生成控制，不是严格符号保证。即使 Caption 正确，实际配器、调性、节奏和结构仍可能漂移。

当前 ComfyUI 图没有文本负向提示通道：负条件是 `ConditioningZeroOut`。因此不能把 `no R&B`、`without drums`、`no strings` 等禁止词写进正向 Caption；这些词仍可能强化相关概念。

## 3. 当前实现问题

### 3.1 自由 Caption 权限过大

当前 `song_content.py` 让文本模型自由生成完整 Caption，只验证时长、BPM、调性、女声、普通话和钢琴等少数必需词。文本模型可以在保留这些词的同时加入 R&B、弦乐、贝斯、合成器或背景和声，仍然通过验证。

### 3.2 风格表述存在歧义

`East Asian heritage-leaning` 容易被模型解释为东亚传统器乐融合，而用户需要的是经典、克制的华语抒情写法，并非古筝、二胡、笛子等民族器乐。

### 3.3 Worker 兜底直接要求额外配器

`tools/minimax_music3_worker.py` 的 fallback Caption 当前包含 strings、cello 和 percussion。只要上游 Caption 缺失，就会主动要求这些乐器出现。

### 3.4 单 seed 无法评估稳定性

生产链固定 `seed=200717`。单个 seed 可能长期放大某种节奏或配器偏向，也无法区分问题来自 Caption、seed 还是 CFG。

## 4. 设计原则

### 4.1 文本模型只做语义规划

文本模型不再自由编写最终 Caption，只返回受限结构：

```json
{
  "schema_version": "p03.song-semantic-plan.v1",
  "emotion_arc": "quiet_longing",
  "piano_texture": "sparse_arpeggiated",
  "vocal_delivery": "restrained_legato",
  "dynamic_arc": "soft_gentle_peak_soft",
  "ending": "resolved_soft_cadence",
  "lyrics": "..."
}
```

所有非歌词字段均为白名单枚举。模型不能添加流派、乐器、制作插件或新的自由文本配器说明。

### 4.2 程序生成最终正向 Caption

程序根据枚举拼装固定 Structured Caption。发送给 MiniMax 的内容只描述想要的结果，不出现禁止词。

固定核心应表达：

```text
Global Metadata
A classic Mandarin lyrical song presented as an intimate voice-and-piano recital,
at 68 BPM in Bb major, straight 4/4. The emotional shape is restrained, songful,
clear and personal, with a gentle central rise and a settled ending. The recording
feels close, natural and small-room.

Vocal Details
One adult female Mandarin singer uses a clear centered tone, natural diction,
mostly syllabic legato phrasing, a moderate range and quietly contained emotion.
Phrase endings are clean and the melodic line remains simple and singable.

Arrangement
One acoustic grand piano provides the complete instrumental performance from the
opening to the final cadence. The right hand carries transparent lyrical figures;
the left hand supplies low-register foundation and harmonic movement. Section
changes come from register, voicing, note density, dynamics, pedaling and silence.
The final section returns to a simple, complete piano cadence.
```

具体情绪、钢琴织体和动态变化由白名单片段替换。模板中不写任何 `no / without / avoid` 禁止句。

### 4.3 禁止项只存在于程序验证和人工评分

代码可以检查内部生成计划是否含有未授权字段，但这些禁止词不得进入 MiniMax Caption。

例如：

- 计划 JSON 只允许固定键；
- 枚举之外的值直接拒绝；
- Caption 必须由程序生成，不接受模型原样 Caption；
- Worker 不再拥有自由配器 fallback；
- 人工验收单独记录 R&B、额外乐器和唱腔漂移。

### 4.4 不自动用文本规则判断最终音频

关键词扫描只能约束输入，不能证明输出音频没有额外乐器。第一阶段不自研音频风格分类器，也不把不可靠的自动分数当作质量事实。

最终风格是否合格由固定样本集的本机盲听决定。

## 5. 目标代码结构

```text
song_content.py
  ├─ SongSemanticPlan
  ├─ 受限 LLM JSON 生成
  ├─ 枚举和歌词结构验证
  └─ 不再返回自由 Caption

music_caption.py（新增）
  ├─ 正向核心模板
  ├─ 枚举 -> Caption 片段
  ├─ render_minimax_caption(plan)
  └─ 纯函数测试

music_reply.py
  ├─ plan_song_content()
  ├─ render_minimax_caption()
  └─ MiniMaxMusic3Worker.generate(...)

tools/minimax_music3_worker.py
  ├─ 要求上游提供合法 lyrics + caption
  ├─ 正向、安全的最小 fallback 或明确失败
  ├─ 参数配置化
  └─ 批量 seed 实验支持

tools/evaluate_minimax_music.py（新增）
  ├─ 生成实验清单
  ├─ 保存 request、seed、参数和输出路径
  ├─ 不自动上传音频
  └─ 汇总人工评分
```

## 6. 语义计划枚举

第一版只开放足够覆盖书信音乐回复的少量选项。

### `emotion_arc`

- `quiet_longing`
- `gentle_reassurance`
- `restrained_sadness`
- `warm_gratitude`
- `soft_reconciliation`
- `calm_affection`

### `piano_texture`

- `sparse_arpeggiated`
- `lyrical_broken_chords`
- `measured_chordal`
- `transparent_counterline`

### `vocal_delivery`

- `restrained_legato`
- `clear_narrative`
- `gentle_songful`
- `quiet_intimate`

### `dynamic_arc`

- `soft_gentle_peak_soft`
- `soft_steady_soft`
- `quiet_gradual_warmth`

### `ending`

- `resolved_soft_cadence`
- `lingering_piano_cadence`
- `short_settled_cadence`

枚举增加必须通过实际样本和听感理由，不因单封来信临时扩展。

## 7. 歌词约束

保留现有 90 秒和 118 秒产品时长，但歌词结构应与 MiniMax 的段落标签一致：

```text
[Intro]
[Verse]
...
[Interlude]
[Verse]
...
[Outro]
```

要求：

- Intro、Interlude、Outro 不含歌词行；
- 90 秒 12 行，118 秒 16 行；
- 每行以自然可唱的中文短句为主；
- 不复制用户原文，不复制现有歌曲；
- 不发明林离经历；
- 不把普通安慰写成强迫乐观或心理咨询；
- 歌词只承载本次回复已经允许表达的内容；
- canonical reply 与歌词均来自同一来信，但歌词不是正文的逐句改写。

## 8. 参数实验

第一轮只比较 Caption 架构和 seed，不同时修改所有参数。

### 固定基线

- Text encode CFG：1.5；
- top_k：50；
- KSampler：Euler；
- steps：30；
- scheduler：simple；
- denoise：1.0；
- 时长：90 秒为主，118 秒补测。

### Seed 池

初始实验使用：

```text
200717
1247
2702
202608
```

这些 seed 只用于对照，不代表最终生产默认。每个 seed 在相同输入和 Caption 下生成，避免把来信差异误认为 seed 差异。

### 第二轮可选实验

只有第一轮仍不稳定时，再单独比较：

- KSampler CFG：1.5 / 1.7；
- Text encode CFG：1.5 / 1.7；
- 两种钢琴织体模板。

每轮只改变一个变量。

## 9. 固定验收样本集

使用合成来信，不提交真实私人通信：

1. 普通安慰；
2. 失落与孤独；
3. 亲密日常；
4. 冲突后的修复；
5. 用户明确请求演奏；
6. 林离主动形成旋律构想。

每个样本至少跑 4 个 seed。

人工评分表：

| 指标 | 评分 |
| --- | --- |
| 钢琴是否持续承担完整伴奏 | 0 / 1 / 2 |
| 额外乐器是否明显出现 | 0 / 1 / 2，越高越严重 |
| R&B / soul / groove 倾向 | 0 / 1 / 2，越高越严重 |
| 转音、ad-lib 或背景和声倾向 | 0 / 1 / 2，越高越严重 |
| 女声普通话清晰度 | 0 / 1 / 2 |
| 传统、克制的抒情感 | 0 / 1 / 2 |
| 情绪与来信匹配 | 0 / 1 / 2 |
| 歌词可唱性 | 0 / 1 / 2 |
| 结尾是否完整 | 0 / 1 / 2 |

生产候选必须满足：

- 额外乐器严重度平均不高于 0.5；
- R&B 倾向平均不高于 0.5；
- 钢琴完整伴奏、普通话清晰度、抒情感和情绪匹配平均不低于 1.5；
- 无明显失败样本被隐藏；
- 至少两名听者或同一听者的两轮盲听结论一致。

门槛可在首次实验后调整，但必须先记录原始结果。

## 10. PR 拆分与顺序

### MUSIC-01：受限语义计划 Contract

修改：

- `song_content.py`；
- 新增 Contract 测试。

只增加 `SongSemanticPlan`、枚举和 JSON 验证，不改变生产调用。

### MUSIC-02：固定正向 Caption Renderer

新增：

- `music_caption.py`；
- 模板快照测试；
- 每个枚举组合的合法性测试。

不调用 MiniMax。

### MUSIC-03：切换生产链

修改：

- `song_content.py`；
- `music_reply.py`；
- 相关媒体测试。

生产链从自由 Caption 切换为：

```text
LLM -> SongSemanticPlan -> 程序 Caption -> MiniMax
```

### MUSIC-04：清理 Worker 兜底与参数入口

修改：

- `tools/minimax_music3_worker.py`；
- worker 单元测试。

删除含额外配器和否定句的 fallback。缺少合法 Caption 时优先明确失败；如保留兜底，只能调用同一固定正向模板。

### MUSIC-05：本机批量实验工具

新增：

- `tools/evaluate_minimax_music.py`；
- 合成测试清单；
- 评分表 schema；
- 使用文档。

工具只在本机运行，不进入默认服务。

### MUSIC-06：确定生产 seed 和参数

依据听感证据更新：

- 默认 seed 策略；
- 生产参数；
- 验收记录摘要；
- 回滚说明。

没有本机证据不得合并 MUSIC-06。

## 11. 与视频链的边界

冻结：

- RoFormer 人声分离；
- LatentSync 参数；
- 官方动作和转场素材；
- 说话视频生成；
- 最终 FFmpeg 拼接。

允许修改：

- 音乐生成请求；
- 音频产物 metadata；
- 音频失败码；
- 音频实验工具；
- 将选定音频交给后续视频链的接口。

最终 MP4 的口型、动作、画面和整体观感必须在用户本机验收，本工作包不能用代码单测替代视觉判断。

## 12. 回滚

- 保留旧 Caption 架构的单一 feature flag 仅用于对照，最多跨一个发行周期；
- 新架构出现严重退化时，普通文字回复和说话视频仍可用；
- 音乐生成失败不回滚 canonical reply；
- 生产参数和 seed 均集中配置，回滚不需要修改视频模块。

## 13. 完成条件

- MiniMax 不再接收自由生成的完整 Caption；
- MiniMax Caption 全部由正向固定模板构造；
- Worker 不包含 strings、cello、percussion 等错误兜底配器；
- 没有禁止句被发送到 MiniMax；
- 固定样本集和 seed 对照已经在本机跑完；
- 人工评分达到门槛；
- 生产参数有证据、有回滚点；
- 现有视频链未被重构；
- `public-smoke` 与专项测试通过。

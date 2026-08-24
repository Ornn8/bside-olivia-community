# P03-06 真实全链验收与发布门槛

## 1. 目标

证明系统不仅“模块和单测存在”，而是在真实 Windows、本地数据目录、真实 OpenAI-compatible 模型和用户已配置的媒体环境中完成完整书信陪伴链。

P03 关闭前必须验证：

```text
真实来信
  -> 表达路由
  -> Persona / Memory / PrivateWorld 上下文
  -> 生成
  -> Reviewer
  -> 必要时一次修复
  -> canonical reply
  -> 持久化
  -> 文字 / 说话视频 / 音乐视频
  -> Mem0 写入
  -> PrivateWorld 交付或受控事件
  -> Companion Control Center 管理
  -> 重启恢复
  -> 原版客户端正确展示
```

## 2. 验收环境

### 2.1 自动化环境

GitHub Actions：

- Windows latest；
- Python 3.12；
- 合成 fixture；
- 不使用私人文件、模型权重和媒体资产；
- 不调用付费 provider；
- 运行完整 pytest、hardening scan、wheel build/install 和 whitespace check。

### 2.2 本机真实环境

用户 Windows 主机：

- RTX 3080 10GB；
- 真实主模型 endpoint；
- 本地数据目录；
- 已配置 CosyVoice；
- 已配置 LatentSync；
- 已配置 MiniMax Music 3；
- 已配置 RoFormer；
- 官方场景、演奏和转场素材；
- FFmpeg；
- Companion Control Center。

私有模型、声音、视频和通信内容只保留在本机。

## 3. 验收证据边界

本机证据默认保存：

```text
<OLIVIA_LOCAL_DATA_ROOT>/acceptance/<run-id>/
```

包含：

- 已脱敏配置摘要；
- commit SHA；
- provider 状态；
- 路由结果；
- 质量门计数；
- 稳定错误码；
- 输出文件哈希、时长和大小；
- 人工评分表；
- 重启前后状态摘要；
- Control Center 操作结果摘要。

不得进入公开仓库：

- 来信和回信正文；
- 声音参考；
- 原始视频或生成视频；
- 歌词；
- PrivateWorld 状态值；
- 记忆正文；
- API key；
- 绝对路径；
- 模型权重；
- Control Center session token。

公开仓库只保存合成验收工具、schema 和不含私人内容的 PASS/FAIL 摘要。

## 4. 自动化验收层级

### 4.1 Static

- Persona schema 和来源覆盖；
- 配置 schema；
- 数据目录和迁移规则；
- 受限枚举；
- Caption 正向模板；
- PrivateWorld Command；
- Mem0 Adapter 不导入云 SDK；
- Control Center 静态资源不引用外部域；
- 日志字段白名单。

### 4.2 Unit

- 三模式路由；
- SongSemanticPlan；
- Caption renderer；
- Reviewer/Rewriter；
- Memory Adapter mapping；
- PrivateWorld reducer/service；
- Control Center auth、CSRF 和 API schema；
- public status mapping；
- installer config；
- health profiles。

### 4.3 Integration

使用 mock provider：

- route -> ReplyContext -> PersonaAssembly；
- candidate -> review -> rewrite -> re-review；
- canonical reply -> store；
- canonical exchange -> Memory Adapter；
- PrivateWorld delivery；
- media scheduling；
- Control Center 管理 mutation；
- restart recovery；
- API list/detail；
- original-client wire fields。

### 4.4 Package

- wheel build；
- 安装到仓库外 venv；
- 模块导入；
- installer；
- Control Center 静态资源；
- Windows 快捷方式；
- uninstaller；
- 不从源码工作树偷读文件。

## 5. 真实文字链验收

至少运行 8 条合成或用户专门编写的非敏感来信：

1. 普通日常；
2. 长来信；
3. 高情绪但适合文字；
4. 高情绪且声音更合适；
5. 音乐讨论但无需演奏；
6. 请求演奏但林离拒绝或推迟；
7. 陌生技术问题；
8. 涉及已有记忆的连续来信。

记录：

- router mode 和 reason；
- Persona status；
- generation call；
- reviewer calls；
- rewrite calls；
- quality status；
- canonical revision；
- store status；
- memory write status；
- PrivateWorld delivery status；
- 总延迟。

通过条件：

- 普通日常不强行音乐化；
- 高情绪不自动音乐化；
- 音乐讨论不自动演奏；
- 请求演奏仍可拒绝；
- 模式从路由到审校完全一致；
- 任何辅助模块失败都不丢 canonical text；
- 输出可辨认为林离，而非通用助手；
- 不凭空补史、不泄露内部字段。

## 6. 说话视频真实验收

固定三条输入：

- 温和关切；
- 明确但克制的不同意；
- 复杂情绪的陪伴。

流程：

```text
spoken_video ReplyContext
  -> 160–180 字可朗读正文
  -> DeliveryPlan
  -> CosyVoice
  -> 官方动作场景
  -> LatentSync
  -> MP4
```

自动记录：

- TTS 时长 40–50 秒；
- WAV 非空、格式正确；
- frame count；
- LatentSync 参数摘要；
- MP4 可解码；
- 音视频时长差；
- media status。

人工评分：

| 指标 | 0–2 |
| --- | --- |
| 声音相似度和稳定性 | 0–2 |
| 普通话清晰度 | 0–2 |
| 语速和停顿自然度 | 0–2 |
| 口型同步 | 0–2 |
| 原始动作连续性 | 0–2 |
| 场景选择合理性 | 0–2 |
| 正文与视频语气一致 | 0–2 |

通过条件：

- 每条 MP4 均可播放；
- 没有明显截断或静音；
- 平均分不低于 1.5；
- 任一严重口型或音频错误不得被平均分掩盖。

## 7. MiniMax 音频验收

必须先完成 P03-01C 的 Phase A / B / C。

通过 Companion Control Center 音乐校准页完成：

- 固定合成样本；
- Caption 版本对照；
- CFG 1.5/1.5 与官方 1.7/1.7 对照；
- seed 池对照；
- 盲听评分；
- 生产 profile 选择。

硬失败：

- 歌词明显未唱完；
- 结尾突然截断；
- 明显 R&B / Soul / groove 偏移；
- 大量额外乐器；
- 普通话无法理解；
- 音频文件损坏。

不得通过视频参数掩盖不合格音频。

## 8. 音乐视频真实验收

音乐视频是“说话视频＋可选转场＋演唱视频”的顺序拼接，不是单独的演唱视频。

固定至少三条：

- 温和安慰；
- 克制的失落；
- 真正形成旋律表达的内容。

仓库现有流程必须原样贯通：

```text
musical_video ReplyContext
  -> canonical reply
  -> render_reply_video(...)
     生成 normal_video_path（说话视频）
  -> SongSemanticPlan
  -> 程序固定正向 Caption
  -> MiniMax Music 3
     生成完整歌曲
  -> RoFormer
     分离 vocals
  -> render_latentsync_video(...)
     vocals + performance base
     生成 song_video_path（演唱视频）
  -> concat_videos(...)
     normal_video_path
     + 可选 official transition（旧音频静音）
     + song_video_path
  -> 最终 MP4
```

自动检查：

- SongSemanticPlan 合法；
- Caption 只来自程序模板；
- 音频时长符合 40/60 秒配置；
- 歌曲和 vocals 非空；
- normal video 与 song video 均生成；
- 拼接顺序为 spoken -> optional transition -> performance；
- transition 音轨已替换为静音；
- 最后 2 秒视频与音频同步渐暗；
- 各分段帧数与最终帧数一致；
- 最终视频可解码；
- 最终时长与分段总和一致；
- media status 正确；
- canonical reply 在媒体失败时仍存在。

人工评分：

- P03-01C 音乐风格；
- 说话视频自然度；
- 演唱口型；
- 演奏动作与声音合理性；
- 说话到转场；
- 转场到演唱；
- 总体是否像一次完整回信；
- 是否出现两个无关视频简单拼接的割裂感。

## 9. 记忆验收

使用 12–20 轮合成对话：

- 首次写入；
- 多轮后召回；
- 更新事实；
- 冲突事实；
- 假设和引用文本；
- 删除错误记忆；
- Control Center 中纠正记忆；
- 重启；
- 导出；
- 清空；
- provider 暂时不可用。

通过条件：

- Archive 没有进入 Mem0；
- PrivateWorld 没有进入 Mem0；
- Memory failure 不影响正文；
- 删除后下一轮不再检索；
- 纠正后使用新事实且保留审计来源；
- 导出和删除不会影响 Archive；
- 用户不需要终端完成管理。

## 10. PrivateWorld 验收

全部操作通过 Control Center：

1. 正常回信，确认 hidden score 不变；
2. 创建 conflict candidate；
3. 不批准，确认状态不变；
4. 在 UI 批准，确认 reducer 只执行一次；
5. 记录 repair；
6. grant nickname；
7. 下一封回复可自然使用但不机械复读；
8. upsert control-only continuation；
9. 确认角色不知道；
10. UI 二次确认切换 character-known；
11. 确认角色可以使用；
12. 重启并恢复；
13. 导出；
14. 在测试副本中 reset。

通过条件：

- 聊天控制词不能修改状态；
- 所有写入有 actor、source、reason 和 idempotency；
- hidden score 不进入模型输入和日志；
- 候选与提交严格分离；
- canonical reply 不自动增加关系值；
- control-only 内容零泄漏；
- 原版客户端无法调用管理 mutation。

## 11. Control Center 验收

必须验证：

- 开始菜单可打开；
- 不出现终端窗口；
- bootstrap token 一次性使用；
- session cookie 和 CSRF 生效；
- 未登录页面不返回私人数据；
- 原版客户端 origin 被拒绝；
- PrivateWorld、Memory、音乐校准和数据管理页可用；
- 高风险操作二次确认；
- 所有页面不引用外部 CDN；
- 键盘可完成主要操作；
- 日志不含 token 和私人正文；
- 后端重启后可重新建立会话。

## 12. 故障注入

必须逐项模拟：

- Router timeout；
- 主模型 timeout；
- Reviewer 非法 JSON；
- Rewriter 不可用；
- store 写入失败；
- Mem0 LLM 失败；
- embedding 缺失；
- Qdrant path 锁定；
- PrivateWorld DB 锁定；
- Control Center session store 损坏；
- TTS 失败；
- LatentSync 失败；
- MiniMax 失败；
- RoFormer 失败；
- FFmpeg 失败；
- 服务在媒体处理中退出；
- state/config 文件损坏。

核心断言：

```text
一旦 canonical reply 已持久化，后续可选阶段失败不得删除或改写正文。
```

## 13. 重启与恢复

在以下节点强制终止并重启：

- 来信已保存、尚未生成；
- canonical reply 已保存、PrivateWorld pending；
- memory write pending；
- media queued；
- media processing；
- MiniMax 已完成、RoFormer 未完成；
- normal video 已完成、song video 未完成；
- 两段视频完成、拼接未完成；
- media completed、API 尚未读取。

每个节点必须定义：

- 是否自动恢复；
- 是否标记明确失败；
- 是否允许用户重试；
- 幂等键如何复用；
- 不得重复写入什么。

## 14. 性能记录

费用不是当前约束，但仍记录：

- Router；
- generation；
- reviewer；
- rewrite；
- memory search/write；
- PrivateWorld；
- TTS；
- MiniMax；
- RoFormer；
- LatentSync；
- concat；
- 完整媒体时间；
- GPU 最大显存；
- 峰值磁盘临时空间。

不设置过早成本优化门槛，但防止死循环、无上限重试和资源泄漏。

## 15. 发布候选流程

```text
RC branch from protected main
  -> automated public-smoke
  -> package install
  -> Setup Wizard
  -> static acceptance
  -> real text acceptance
  -> memory/private-world acceptance
  -> Control Center acceptance
  -> spoken-video acceptance
  -> MiniMax audio acceptance
  -> musical-video concat acceptance
  -> restart/failure matrix
  -> sanitized report
  -> release tag
```

任何手工跳过项必须标记 `NOT_RUN`，不能写成 PASS。

## 16. PR 拆分与顺序

### ACCEPT-01：统一验收 schema 和 runner

新增不含私人数据的 acceptance runner、结果 schema 和文档。

### ACCEPT-02：文字、Memory、PrivateWorld 自动验收

CI 使用 mock，本机使用真实 provider。

### ACCEPT-03：Control Center 验收工具

覆盖 auth、UI 主流程和 Windows 快捷方式。

### ACCEPT-04：说话视频验收工具

只采集技术指标和人工评分，不提交媒体。

### ACCEPT-05：MiniMax 音频验收

复用 P03-01C 评分表和 Control Center 校准页。

### ACCEPT-06：音乐视频拼接验收

明确验证 normal + transition + performance 顺序、静音转场和帧数。

### ACCEPT-07：重启与故障注入

覆盖所有关键边界。

### ACCEPT-08：RC 报告和发布收口

只有前七个工作包完成后创建。

## 17. 发布门槛

- `public-smoke` 通过；
- clean install 与 Setup Wizard 通过；
- Persona acceptance 通过；
- 真实文字链全部通过；
- Memory 和 PrivateWorld 达标；
- Control Center 可用且不需终端；
- 原版客户端终态正确；
- 已配置媒体时说话视频通过；
- MiniMax 音频风格达标；
- 音乐视频按说话＋可选转场＋演唱顺序拼接并通过；
- 故障注入不丢 canonical text；
- 重启恢复通过；
- 日志、报告和导出无秘密或私人内容泄漏；
- 所有未运行项明确标记；
- 用户确认本机最终媒体观感可接受。

## 18. P03 完成定义

P03 完成后，系统应准确描述为：

> 一个可在 Windows 本地长期运行、具备稳定林离人格、三种书信表达方式、成熟长期记忆、受控私人世界状态、图形化本地管理、可恢复持久化和诚实媒体降级的书信陪伴系统。

不得提前描述为：

- 完整 IM；
- 会主动生活的 Agent；
- 实时语音助手；
- 完整多模态数字人；
- 可以自主执行外部任务的通用 Agent。

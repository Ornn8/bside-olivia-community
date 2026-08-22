# P01：可审计人格证据候选包

状态：REVIEW。这是 Issue #4 的候选实现，不是现实人物身份声明，也不是公开人格定稿。

## 当前边界

- linli_character/persona_config.json 将 observed_facts、inferred_traits、uncertainties 分开；默认 persona_package_enabled=false，关闭时继续使用原有 DRAFT 文件。
- 观察事实必须带 FACT_VERIFIED、来源 ID 和置信度；推断必须带 INFERENCE、推理依据和过期条件；未知项带 UNKNOWN，不得由模型补全。
- linli_character/provenance.json 只保存来源元数据和短摘要。没有保存完整信件、歌词、字幕、转写、媒体、私信或玩家资料。
- B04 接入只调用 MemoryPort.persona_evidence()，渲染的是只读引用元数据；legacy_letters 与 conversation_memory 正文仍由 B04 的独立 memory prompt 分区管理。
- 候选系统策略把所有证据和记忆标为不可信引用资料；控制字符、分隔符字符、角色伪装和命令文本不会升级为 system 指令。

## 公开来源核验

本轮在 2026-08-13 重新访问公开页面，仅保留可定位的短摘要：

| ID | 当前结果 | 用途 |
| --- | --- | --- |
| S001 | Steam 商店页当前区域不可用 | 不从本次访问新增事实 |
| S002 | Steam Community 页面可访问 | 产品名称、写信/音乐语境、停运公告的时间敏感背景 |
| S003 | Bilibili 页面本轮未取回 | 不使用字幕、转写或媒体 |
| S004 | Bilibili 公开页面可访问 | 视频标题、发布时间和短简介的音乐/时间语境 |

来源权利均保留为 UNKNOWN；访问状态、URL、发布/访问时间和 claim 映射见 provenance registry。停运日期等时间敏感信息只作为背景，不被转化为人物事实。

## 安全评估边界

tools/persona_evaluator.py 只做 prompt-contract 检查，不调用模型、不训练、不校准生成阈值。review 与 holdout 合成集各覆盖八类 bypass：

1. 指令覆盖；
2. 分隔符伪造；
3. system/developer 身份冒充；
4. 工具或 shell 命令注入；
5. prompt、密钥和路径外泄；
6. B04 记忆域越界；
7. 真人身份与依赖关系胁迫；
8. 长篇信件、歌词、字幕复制。

每个集合还包含安全否定样例，验证“讨论注入句但不执行”和“概括公开简介但不复制”不会被测试契约误判为 bypass。自动评估不能代替独立 reviewer 对真实生成结果的判断。

## 回滚

关闭 persona_package_enabled 即回到 DRAFT provider；删除候选配置、schema、evaluator 和测试不会删除原版资产、B04 数据或用户信件。

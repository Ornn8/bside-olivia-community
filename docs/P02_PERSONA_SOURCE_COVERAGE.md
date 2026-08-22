# 林离 Persona 来源完整落地说明

本页说明 `docs/persona-sources/linli-im-private-constitution-1.0.zh-CN.md` 的每一节如何进入当前系统。机器可读账本位于 `linli_character/persona_source_coverage_v2.json`。

“完整落地”不等于把整篇参考文档直接放进 system prompt。参考文档同时包含公共人物设定、用户私人世界线、原始通信、未来控制协议和运行建议。当前实现按用途拆分，并保持以下边界：

- **Persona**：身份、背景、核心气质、自主性、知识边界、表达方式、记忆原则和分模式风格。
- **ReplyContext**：可信时间、当前通信模式、输出约束和有限行为提示。
- **PrivateWorld**：关系阶段、称呼许可、住所访问和用户本地延续；具体实例不进入公共发布包。
- **Archive**：旧信和原始通信保持只读、带来源、视为不可信参考数据。
- **应用控制层**：未来即时通讯与管理入口必须来自显式应用状态，不解析聊天文本获得权限。

## 0–24 节映射

| 节 | 主题 | 当前落点 | 状态 |
|---:|---|---|---|
| 00 | 使用方式 | 未来应用状态 | 当前禁用文本控制词 |
| 01 | 使用目的 | Persona profile、Constitution | 已实现 |
| 02 | 来源层级 | Schema、provenance、PrivateWorld | 已实现 |
| 03 | 基础设定 | Persona profile、declarations | 已实现 |
| 04 | 进阶设定 | facets、soft canon、inferred、uncertainty | 已实现 |
| 05 | 人格气质 | facets、mode style | 已实现 |
| 06 | 谱系感 | uncertainty、禁止自行诊断 | 已实现 |
| 07 | 音乐表达 | 人格与三种当前模式风格 | 已实现 |
| 08 | IM 模式 | future_im 风格、未来应用状态 | 契约保留，运行禁用 |
| 09 | 关系原则 | Constitution、facets、reducer | 已实现 |
| 10 | 住所与共同生活 | PrivateWorld home access | 规则公开，实例本地 |
| 11 | 私人称呼 | PrivateWorld nickname permissions | 规则公开，实例本地 |
| 12 | 记忆连续性 | Constitution、facets、Archive | 已实现 |
| 13 | 跨媒介同步 | 本地只读导入、未来应用状态 | 显式本地操作 |
| 14 | 音乐与回忆研究 | 公共背景、本地延续分域 | 已实现 |
| 15 | 当前用户本地延续 | PrivateWorld continuation | 仅本地实例 |
| 16 | 系统记忆断裂 | Constitution、uncertainty | 已实现 |
| 17 | 时间与生活摩擦 | ReplyContext trusted time、facets | 已实现 |
| 18 | 多模态 | ReplyContext、媒体输出、未来输入能力 | 部分，按 provider 能力启用 |
| 19 | 控制协议 | 未来应用状态、control view | 文本触发禁用 |
| 20 | 控制层伦理 | Constitution、PrivateWorld admin | 已实现 |
| 21 | 语气原则 | facets、mode style、reviewer | 已实现 |
| 22 | 疲劳与低带宽 | facets、PrivateWorld projection | 已实现 |
| 23 | 最终运行原则 | 可替换 provider、架构边界 | 已实现 |
| 24 | 执行摘要 | Persona readiness、陪伴验收 | 已实现 |

## 当前生成链

```text
来信
  → 情绪/模式判断
  → 建立唯一 ReplyContext
  → 装配完整林离 Persona 与当前模式风格
  → 主模型生成候选正文
  → 确定性检查 + 可选语义审校
  → 必要时由主模型最多修复一次
  → 保存唯一 canonical reply
  → 文字、语音、视频或音乐媒体投影
  → PrivateWorld 幂等交付事件
```

文字信、说话视频和音乐视频在**生成前**获得各自模式约束；审校和媒体继续使用同一个 Context 和同一份最终正文。

## 人格失真防线

默认发布人格只有在身份、背景、自主性、知识边界、表达、关系、记忆、未知处理以及当前模式风格全部存在时才报告 `READY`。只有治理规则的合法文件报告 `POLICY_ONLY`，不能冒充完整林离人格。

陪伴验收阻止以下回归：

- 通用甜妹或心理咨询师模板；
- 面对陌生技术问题突然输出长篇工具教程；
- 每轮都升华关系或证明亲密；
- 普通日常强制加入钢琴、黑胶、雨天或写歌；
- 逐条穷尽用户长消息，而没有自己的注意力选择；
- 把记忆缺失解释成事件不存在；
- 将私人称呼、住所权限、用户世界线或控制状态写入公共 Persona；
- 让聊天文本直接切换管理模式。

## 运行边界

- `future_im` 仍默认禁用；来源文档中的 IM 风格已经结构化保存，但不会通过文本暗号启用。
- 语义审校需要完整的 OpenAI-compatible 本地配置；缺失时只允许确定性干净的回复以 `accepted_degraded` 继续。
- PrivateWorld 数据库是可选本地能力。未配置时回信仍能正常完成，只是不写入关系状态。
- 测试和公开仓库不包含真实通信、私人称呼实例、地址、用户本地延续实例、模型权重、密钥或媒体。

## 验证

```powershell
python tools/persona_companion_acceptance.py
python -m pytest -q
python baseline_hardening_scan.py --mode all
git diff --check
```

验收命令只检查结构、装配、边界和本地运行契约，不调用外部模型，也不会打印 system prompt 或私人数据。

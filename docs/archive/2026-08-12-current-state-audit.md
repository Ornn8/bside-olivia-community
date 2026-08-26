# CURRENT STATE AUDIT

> 历史快照：本文件记录 2026-08-12 的初始公开基线，已不代表当前产品状态。当前入口见 [`README.md`](../../README.md) 与 [`STATUS.md`](../STATUS.md)。

审计分支：`feature/baseline-hardening`。本文件只按实际文件、可运行命令和不回显内容的扫描记录判断；README 自述、单项冒烟或存在目录都不能自动升级为完成。

## 状态定义

- **已存在**：文件或结构实际存在，但不代表产品能力完成。
- **仅原型**：能看到局部代码/脚本，缺少完整管线或验收证据。
- **缺失**：没有可供本地运行的实现、入口或必要资产。
- **未验证**：可能存在实现或本机资产，但本批没有安全、完整、可复现证据。

## 代码与仓库审计

| 项目 | 状态 | 实际证据与边界 |
|---|---|---|
| `local_server.py` | 仅原型 | HTTP 路由和可配置 LLM 适配存在；运行时代码中的官方转发路径已移除，包含官方 host、token 日志读取、官方请求/轮询/下载的标记均未命中；没有 WS/ASR/TTS/Live。 |
| `memory.py` | 仅原型 | 明确标为 legacy；默认 `persist=False`，支持清空；Mem0 未接入。 |
| `patch_feapp.py` | 仅原型 | 只接受显式本地 WS，已移除 API 注入参数；输入/备份 hash、临时目录、原子替换和失败回滚只在合成压缩包测试。 |
| `tools/extract_player.py` | 仅原型 | zip-slip 和输出根约束存在；只在临时恶意 zip 测试，未处理真实资产。 |
| `tests/test_baseline_hardening.py` | 已存在 | 25 项本批回归测试，覆盖实际 handler HTTP 状态、CORS、未实现音乐写操作、MIDI 终态、超时/事件循环、日志隐私、ignore、patch、zip-slip 和人格 fallback。 |
| `linli_character/system_prompt.md` | 仅原型 | 明确 `DRAFT`、未完成公开资料蒸馏、不得作为最终官方人格。 |
| `llm_config.example.json` | 已存在 | 只含安全模板，不含真实 provider 值或官方采集开关。 |
| `.gitignore` | 已存在 | 显式允许正式文档和唯一证据文件；`docs/reviews/*` 先整体排除，后只放行 `baseline-hardening-evidence.md`；`.evidence/` 全部本地忽略；末尾再次排除秘密、日志、模型和媒体。 |
| `PROTOCOL.md` | 已存在但忽略 | 历史协议材料含敏感认证/签名/令牌记录，不作为运行路径或提交内容。 |

## 本机目录事实

| 路径 | 实际大小 / 文件数 | 状态 | 结论 |
|---|---:|---|---|
| `0.0.9.615` | 3,711,516,860 bytes / 183 | 已存在 | 原版程序/解包资产；只允许未来本机 manifest 引用，禁止 Git/分发。 |
| `CosyVoice` | 15,484,055,181 bytes / 58,681 | 仅原型 | 历史 TTS 运行目录，未完成 GPU、声音权利、同步和实时验收。 |
| `LiveTalking` | 9,928,926,801 bytes / 67,626 | 未验证 | 本机候选目录，未完成安装或原版视觉驱动验收。 |
| `olivia_assets` | 5,004,535,016 bytes / 145 | 已存在 | 本机归档；版权、隐私和唯一视觉基线未验收。 |
| `assets_extracted` | 15,159,776 bytes / 24 | 已存在 | 解包导出物；没有正式 manifest 和逐状态视觉证据。 |
| `output_audio` | 112,759,722 bytes / 12 | 未验证 | 生成音频不能证明 TTS、同步或声音权利。 |
| `letter_pairs` | 5 对本地原文 | 已存在但忽略 | 原文不进入版本库；只允许 schema/无版权示例。 |

## 产品候选状态

| 能力 | 状态 |
|---|---|
| AIRI 陪伴内核 | 缺失/未验证 |
| Mem0 本地记忆 | 缺失/未验证 |
| Nemotron 3.5 ASR Streaming 0.6B | 缺失 |
| VoxCPM2 | 缺失 |
| MOSS-TTS v1.5 | 缺失/可选离线候选 |
| LiveTalking/MuseTalk | 缺失/未验证 |
| 原版视觉逐状态保真 | 未验证 |
| 原版 WebSocket、Live、安装卸载 | 缺失/未验证 |

## 本批可运行证据

单一命令：

```text
rtk python -m pytest -q
```

本批实际收集 25 项，最终结果、退出码、编译文件数、Git diff 检查和安全扫描记录在 `docs/reviews/baseline-hardening-evidence.md`。编译和测试均只使用仓库源码及临时 synthetic fixture；没有下载模型、启动官方服务、访问外部写接口或修改真实原版资产/配置。

## 未验证结论

视觉唯一基准的同分辨率同时间点截图/片段、像素/SSIM/LPIPS、面部身份、闪烁/背景漂移/色差、帧率和音画同步；ASR/TTS 真实运行；RTX 3080 10 GB 显存和 P50/P95 延迟；Live 多轮对话；安装、停用、卸载、回滚、数据导入/删除；版权/隐私权利；公开资料人格蒸馏；最终总控多模态观看，全部保持 `UNVERIFIED`。本批 hardening 测试全绿不等于整产品全绿。

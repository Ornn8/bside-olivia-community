# Changelog

## Unreleased

仓库已公开，当前仍处于开发者预览阶段。GitHub 已发布 [v0.1.0](https://github.com/Ornn8/bside-olivia-community/releases/tag/v0.1.0) 与 [v0.1.2](https://github.com/Ornn8/bside-olivia-community/releases/tag/v0.1.2) 两个预发布版；以下主干改动尚未形成新的 Release。

### 已进入主干

- 原版客户端、本机 HTTP 服务与隔离安装、更新和回滚；Windows 启动支持单实例、无控制台快捷方式、子进程回收及长期记忆后台初始化；
- Persona 2.0、1200 字信件边界与五层独立审校；文字回信的硬结论使用候选绑定证据，在全局预算内完成裁决、重写和完整复审；
- PrivateWorld 已支持五级关系阶段、分级亲密授权与七日有界增长；阶段和授权只能经类型化命令显式变更，普通回信投递不能越权；
- 官方历史信件按用户和林离分角色写入长期记忆，并幂等应用历史熟悉度和亲近度下限；不会自动改变关系阶段、信任、舒适度或亲密授权；
- 视频回信统一准备语音、音乐、口型和媒体工具，支持可恢复下载；媒体任务在回信质量通过后异步执行并公开失败状态；
- 当前项目原创代码与技术文档采用 Apache-2.0；原版内容、第三方模型与运行时、用户数据和生成媒体不属于项目许可证，历史已按 MIT 取得的权利不追溯撤回。

### 发布前仍需完成

- 跨机器媒体 provider 配置和真实 Windows 全链验收；
- TTS、口型、音乐视频的人工视听验收；
- 下一预览版安装包、升级路径和回滚路径的跨机器验证；
- 对仍在实验中的 ASR、Live 和视觉运行时保持明确边界。

发布判断以 [`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md) 为准，动态进度以 GitHub Issues 与 Milestones 为准。

## v0.1.2 - 2026-08-28

Windows 社区预览版的客户端交互、provider 连接与组件补丁生命周期修复。完整安装、升级、校验值和验证边界见 [`docs/releases/v0.1.2.md`](docs/releases/v0.1.2.md)。

## v0.1.0 - 2026-08-28

首个 Windows 社区预览版。完整安装方式、已验证能力和已知限制见 [`docs/releases/v0.1.0.md`](docs/releases/v0.1.0.md)。

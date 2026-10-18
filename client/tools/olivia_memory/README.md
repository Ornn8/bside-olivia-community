# Olivia 记忆迁移工具（社区维护）

把灵离（`.soul` 文件）里的旧信搬进本地 Olivia（月离）的信件库，并保证时间显示与信箱排序正确；以前导入过、但显示为 `NaN-NaN-NaN` 或「时间未知」的信，也能就地修复。完整使用说明（带图）见 [`说明.docx`](说明.docx)。

设置页中的信件对比、时间修复和重复信整理，见 [信件对比与整理指南](letter-maintenance-guide.md)（English: [Compare and tidy letters](letter-maintenance-guide.en.md)）。本目录随安装包分发，安装后也能直接阅读这份指南。

## 文件

| 文件 | 职责 |
| --- | --- |
| `olivia_memory.py` | 全部逻辑：解析 `.soul` / json、体检、并排对比、写库修复、隐藏重复（含近似重复判定）、版本回退 |
| `运行.bat` | Windows 启动器：定位本机 Python 后启动 `olivia_memory.py`（纯 ASCII，CRLF） |
| `说明.docx` | 面向用户的带图详细说明书（正文、用法、回滚） |
| `letter-maintenance-guide.md` | 设置页中的信件对比、时间修复和重复信整理指南 |
| `letter-maintenance-guide.en.md` | 同一份指南的英文版（界面英文模式下的按钮和提示用语） |

## 边界

- 只读 `.soul` 源文件；Olivia 自己写的信（`state.json`）一个字节不动。
- 写库前先 `VACUUM INTO` 备份到 `install\data\memory\_backups\`，写库过程在单个事务里，中途失败整体回滚。
- 诊断日志（`olivia_memory.log`）只记数量、时间和内部标识，不落信件正文；正文只出现在控制台，以及用户主动生成的对比报告里。
- 命令行用法与回滚步骤见 `说明.docx` 及 `olivia_memory.py` 内注释；交互菜单默认不猜、要改库的步骤先展示再确认。

诊断日志保存为 `olivia_memory_diagnostic.log`，不沿用旧版可能含私人内容的 `olivia_memory.log`。需要排查时只提供新日志；旧日志不会被本工具删除。路径只在控制台展示，含路径的诊断行不落盘，异常堆栈只记录类型、函数名和行号。

## 依赖

- Python 3.10+（标准库；在 Olivia 自带的嵌入式 Python 3.12.10 上验证过）。
- 不依赖本仓库其它模块，也不依赖模型、网络 provider 或官方资源。

## 设计要点（给后续维护者）

- `letter-maintenance-guide.md` 是 `client/docs/letter-maintenance-guide.md` 的随包副本；更新说明时，两处内容应保持一致。英文版 `letter-maintenance-guide.en.md` 同样在两处各有一份，随中文版一起更新。
- `created_at` 必须落库为数字（epoch 秒）；ISO 字符串只修排序不修显示，`None`（走 `letter_pairs.json` 导入）两样都错。
- 同一分钟的多封信按 `.soul` `exchanges` 数组顺序定先后，通过可排序的 `memory_id` / 秒数承载；`uuid4` 不可用。
- 判重按「去信 + 回信」忽略空白全等，跨 `legacy_letters` 与 `state.json` 两个存储；近似重复（正文很像但不逐字相同）与精确判重共用同一套相似度口径与门槛，`compare` 与 `apply` 共用同一套索引与分类实现。
- `运行.bat` 必须保持纯 ASCII、CRLF、不含 `chcp`；控制台中文输出全部由 `olivia_memory.py` 承担。

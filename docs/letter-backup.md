# Letter backups

Settings provides a text-only JSON backup (`olivia.letters.v1`) containing both sides of each mailbox letter, titles, source identities, recorded timestamps, original reply modes and statuses. Audio/video files, private-world controls, credentials and arbitrary internal metadata are not exported. Unknown timestamps remain unknown; numeric legacy timestamps are converted to ISO form. Imported voice/video letters are displayed as read-only text, retaining the original mode in the backup. This is a mailbox backup, not a full installation or personal-chat backup.

Import validates the complete document before atomic archive insertion. A stable hash of the normalized individual record makes repeat import and export/import roundtrips idempotent without relying on the file hash or current export date. Existing live letters are compared too. Imports never queue replies, change relation scores, or require model extraction. Original text is searchable through the read-only Archive prompt path and is automatically indexed by the memory worker when enabled and ready, including previously imported records. Extracted Mem0 facts and their management list remain separate; background original indexing does not call the extraction model.

The current UI submits `originals_only: true` for the legacy `letter_pairs.json` importer, so original persistence no longer waits for Mem0 initialization or a provider call. The previous API behavior remains available to callers that explicitly use the old request shape. No existing relation scores or previously extracted facts are cleared. Limits are 16 MiB per document, 10,000 records and 50,000 characters per side; oversized/invalid input is rejected, not truncated.

Memory-panel reads now have a longer timeout than the ordinary status UI and preserve successful record results when an auxiliary status refresh fails. List/search timings and fixed errors are added to diagnostic history, without queries or memory text. This fixes a reproduced display failure, but the supplied 1.2.5 diagnostic bundle only reported available service status and cannot establish the user's specific list/search failure cause.

The memory management panel counts extracted Mem0 records, not imported letters or original-text index sources. An archive can contain 84 indexed letters while the extracted-record count is zero. The panel labels that distinction explicitly; duplicate-import counts alone do not establish index readiness on a user's device. Management searches propagate provider errors rather than presenting them as zero matches, undated records display an unknown date, and stale responses cannot overwrite a newer search.
# 设置页查看原文接入

长期记忆页面分别显示“信件原文”和提取记忆。原文搜索直接查询本地索引，
不会为搜索调用聊天模型；显示说话人、原信时间和命中片段，不提供提取记忆的编辑/删除按钮。
关键词搜索最多显示 5 个片段，空查询显示最近原文最多 20 段；长内容明确标为节选。

后台扫描将可接入历史信件的来源登记到索引，页面显示最近扫描时间、已接入/扫描总数和已移除数。
尚未扫描时总数为未知，不能当作零封；读取失败单独提示，不能当作接入完成。
点击“搜索 / 刷新接入进度”刷新。数字基于最近扫描快照，不代表信件事实已被模型提取。

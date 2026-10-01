# 长期记忆浏览器

设置页的长期记忆列表每页显示 20 条，支持在全部本地记录中按关键词、最近 7/30/90 天和时间顺序筛选。列表与详情分别滚动；窄窗口切换到单独的详情页。管理操作折叠收纳，修正、删除和清空继续使用现有确认与审计流程。

## 本机读取接口

`GET /toy/companion/memory?browse=1` 显式启用分页；不带 `browse` 的旧调用保持原有响应。接口沿用本机 Host 与 Origin 校验，返回 `Cache-Control: no-store`，仅读取当前用户的数据。

| 参数 | 含义与范围 | 默认值 |
| --- | --- | --- |
| `collection` | `memories` 或 `originals` | `memories` |
| `query` | 本地关键词匹配，最多 500 字符 | 空 |
| `page` | 1–1000000，超过最后一页时回落到最后一页 | 1 |
| `limit` | 1–100；设置页明确发送 20 | 50 |
| `days` | 0、7、30、90；0 表示不限时间 | 0 |
| `sort` | `new` 或 `old` | `new` |
| `source_id` | 仅原文模式可用；包含该来源的别名 | 无 |
| `full` | 仅原文模式可用；1 必须同时指定来源 | 0 |

成功响应遵循 [`original_client_memory_browser.schema.json`](../contracts/original_client_memory_browser.schema.json)，包含 `total`（筛选后的记录数）、`page` 和 `limit`。记忆响应额外包含未筛选的 `total_count`，修正过的记录可带 `updated_at`。原文以用户/林离的文本片段分页，同一封信可以对应两条；`indexed_letters` 是来源数，不等于片段数。

原文预览最多 4000 字符，指定来源且 `full=1` 时最多 50000 字符；被截断的片段设置 `excerpt=true`。无结果时返回空列表、`total=0`、`page=1`。非法选项返回 400，读取不可用返回 503，详情保留已显示的内容并提供重试。

## 存储与兼容

记忆浏览读取 Qdrant 现有 payload，原文浏览读取本地 SQLite 索引，不调用 JEV、嵌入或远程 provider，也不改变回信的记忆检索策略。支持 scroll 的产品适配器可浏览和管理超过 1000 条的旧记录；没有 scroll 的旧自定义适配器仍受其原有列表上限限制。

无需配置或存储迁移。回滚代码恢复原设置页；用户记录及现有管理审计保持原格式。

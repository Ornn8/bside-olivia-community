# P03 原版 Olivia 信箱响应契约

## 1. 目的

本文件把本地陪伴后端的内部状态映射到原版 Olivia `0.0.9.615` 的既有信箱契约。它不增加第二套客户端、不新增独立播放器页面，也不要求原版客户端理解本项目自定义字段。

目标链路：

```text
本地内部状态
  -> 原版 camelCase 信件字段
  -> Collection 既有文字／视频回信组件
```

## 2. 已确认的原版枚举

### 信件状态

```text
PENDING        = 1
AUDITING       = 2
LLM_PROCESSING = 3
REPLIED        = 4
FAILED         = 5
```

### 审核状态

```text
PENDING  = 1
PASSED   = 2
REJECTED = 3
```

### 回复类型

```text
NONE     = 0
TEXT     = 1
SPEECH   = 2
MIX_PLAY = 3
MIX_SVS  = 4
```

原版 Collection 将 `TEXT` 显示为文字回复，将其他非零回复类型显示为视频回复。

## 3. 已确认的原版字段

### 列表项

```text
letterId
isRead
letterStatus
auditStatus
summary
createdAt
repliedAt（可选）
replyType
```

### 信件详情

```text
letterId
isRead
letterStatus
auditStatus
material.stampId（可选）
material.paperId（可选）
content
createdAt
repliedAt（可选）
replyText
replyType
replyVideoUrl
```

### 列表响应

```text
list
hasMore
total
nextCursor
remainingToday
```

### 未读响应

```text
unreadCount
```

旧的 snake_case 字段只作为本仓库现有测试或内部调用者的兼容别名保留，不作为原版客户端契约。

## 4. 本地回复映射

| 本地模式 | 媒体状态 | 原版 replyType | 原版表现 |
| --- | --- | ---: | --- |
| `text_letter` | 任意 | `TEXT` | 文字回复 |
| `musical_video` | `COMPLETED` 且有合法本机 MP4 | `MIX_SVS` | 视频回复 |
| `musical_video` | 等待、处理中或失败 | `TEXT` | 先保留文字正文 |

`video` 与历史 `spoken_video` 只作为输入兼容值，序列化前统一升级为
`musical_video`。`musical_video -> MIX_SVS` 是本项目的语义映射决定：
每个视频回信都包含生成演唱；`SPEECH` 不再由本项目的新回复产出，
`MIX_PLAY` 保留给纯演奏语义。

## 5. 正文耐久性

原版只在 `replyType` 为视频类型时使用 `replyVideoUrl`。因此本地后端必须遵守：

```text
canonical text 完成
  -> letterStatus = REPLIED
  -> 媒体尚未完成时 replyType = TEXT
  -> 媒体完成后 replyType 切为对应视频类型
```

媒体失败不得把信件状态改回 `FAILED`，也不得清空 `replyText`。原版 Collection 的轮询在媒体完成后会重新读取详情，从文字投影切换到视频投影。

## 6. 媒体 URL 边界

公开给原版客户端的 `replyVideoUrl` 只允许：

```text
http://127.0.0.1:<port>/toy/media/<safe-name>.mp4
http://localhost:<port>/toy/media/<safe-name>.mp4
```

禁止：

- 外部域名；
- 用户名或密码；
- query token；
- fragment；
- 路径穿越；
- 非 MP4 文件；
- 任意本机文件路径。

无合法本机 URL 时降级为文字回复。

## 7. 原版播放器边界

原版信箱视频回复使用 Collection 内现有的 `BaseVideo` 路径，作为默认书信编排路线。`webplayer` 是另一套与 `uid`、下载进度和播放控制相关的播放器；安装器提供可选的显式 `uid` 本机回退，只接受明确的 loopback `/toy/media/` URL。该回退不替代 `BaseVideo` 或改变书信路由；`feplayer` 使用 `file` 参数，也不应被强行替换为信件详情组件。

## 8. 实施顺序

### CLIENT-CONTRACT-01

- 新增纯枚举和序列化模块；
- 锁定字段、状态、回复类型和安全媒体 URL；
- 不修改运行时。

### CLIENT-CONTRACT-02

- 将列表、详情、未读和发送响应接入纯序列化模块；
- 同时保留必要的 snake_case 兼容别名；
- 设置 canonical `repliedAt`；
- 验证媒体完成前后 `TEXT -> MIX_SVS`。

### CLIENT-CONTRACT-03

- 使用原版客户端真实打开文字回复和完整视频回信；
- 验证轮询、详情切换、播放、失败降级和重启恢复。

## 9. 完成条件

- 原版字段全部使用正确 camelCase；
- 原版状态和回复类型使用正确数值；
- 文字正文始终可恢复；
- 视频只有在本机 MP4 完成后才公开；
- 原版 Collection 不依赖 `media_status`、`reply_mode` 等自定义字段；
- 没有新增独立客户端、网页或播放器；
- 公共 CI、hardening scan 和 whitespace check 通过。

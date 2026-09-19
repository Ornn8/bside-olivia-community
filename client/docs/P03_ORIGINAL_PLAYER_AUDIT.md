# P03 原版播放器归档审计

## 1. 目的

`feapp.dat` 的首轮脱敏审计已经确认：

- 原版信箱命名路由包含 `Collection`；
- 用户信息候选入口包含 `UserInfo`；
- 主包中存在 `letter_status` 和 `reply_content`；
- 主包中没有证据证明 `reply_video_url`、`media_status`、`media_error_code` 或精确回复模式字段被直接读取；
- 原版前端还包含独立的 `feplayer.dat` 和 `webplayer.dat`。

因此，在修改原版信件详情或视频播放方式之前，必须先确认两个播放器归档如何接收媒体地址、如何与外层客户端通信、读取哪些字段，以及是否已经存在可复用播放器。

## 2. 产品边界

该审计仍遵循：

```text
原版 Olivia 客户端是唯一用户产品外壳
```

审计不能被用来建立：

- 独立浏览器播放器；
- 第二套视频页面；
- 绕开原版信件详情的媒体入口；
- 独立 Control Center；
- 直接打开生成 MP4 代替原版客户端验收。

## 3. 新增只读工具

```text
tools/audit_original_players.py
```

输入是原版版本目录下的：

```text
resources/feplayer.dat
resources/webplayer.dat
```

工具只在内存中读取 ZIP 元数据和有大小上限的 HTML/JavaScript，不会：

- 解压到磁盘；
- 修改或重打包原版归档；
- 访问网络；
- 输出 HTML 或 JavaScript 源码；
- 输出任意原版文案；
- 输出绝对路径；
- 输出完整 URL；
- 输出凭据或用户内容。

## 4. 报告内容

每个播放器只报告：

- archive SHA-256；
- 成员数、压缩前后大小和扩展名计数；
- 直接包含的媒体文件类型计数；
- 安全的 HTML 入口文件名；
- 引用的相对 JS/CSS 文件名；
- JS bundle 文件名、大小和 SHA-256；
- 直接出现的 `/toy/...` 本地路径；
- 保守识别的 action 名；
- allowlist 中的 query key；
- allowlist 媒体字段出现次数；
- `postMessage`、`message`、`URLSearchParams` 等通信标记；
- HLS、DPlayer、Video.js 等已知播放器标记；
- 外部 URL 只保留 hostname，不保留完整地址。

计数只证明标记存在，不能单独证明具体运行方式。

## 5. 本机执行

在拉取包含该工具的最新 `main` 后运行：

```powershell
python tools/audit_original_players.py `
  "<Steam安装目录>\0.0.9.615\resources" `
  --output original-player-audit.json
```

只上传生成的：

```text
original-player-audit.json
```

不要上传：

- `feplayer.dat`；
- `webplayer.dat`；
- 解包文件；
- HTML/JavaScript 源码；
- 原版媒体文件。

## 6. 审计后的判断顺序

拿到报告后：

1. 确认 `feplayer` 与 `webplayer` 的 HTML 入口和 JS bundle；
2. 确认是否使用 query string、`postMessage`、本地 API 或其他 transport；
3. 确认现有播放器是否原生支持 MP4；
4. 确认媒体 URL 所需字段；
5. 再结合原版 `Collection` 内真实点击行为定位信件详情；
6. 最后才定义最小补丁锚点。

没有报告时，不允许：

- 在后端继续新增未经客户端读取的媒体字段；
- 猜测 `LetterDetail` 路由；
- 新增独立播放器页面；
- 声称原版客户端已经能播放生成视频。

## 7. 完成条件

- 两个播放器归档均生成脱敏报告；
- 报告不包含源码、绝对路径、私人内容或完整 URL；
- 播放器入口、transport 和媒体字段的证据已分类为“确认／推断／未知”；
- 原版播放器接线方案基于证据，而不是关键词猜测；
- 在确认最小锚点前，不修改原版 UI；
- `public-smoke`、hardening scan 和 whitespace check 通过。

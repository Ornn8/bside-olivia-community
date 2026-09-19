# P03 原版播放器审计结果（0.0.9.615）

## 1. 证据来源

本结论只基于用户在受支持原版客户端 `0.0.9.615` 上生成的脱敏报告：

```text
docs/evidence/original-player-audit.0.0.9.615.json
```

报告没有包含 `feplayer.dat`、`webplayer.dat`、HTML/JavaScript 源码、原版文案、绝对路径或用户数据。

## 2. 可以确认的事实

### 2.1 两个播放器包不是同一个实现

| 项目 | `feplayer.dat` | `webplayer.dat` |
| --- | ---: | ---: |
| ZIP 成员 | 1 | 7 |
| HTML | 1 | 1 |
| JavaScript | 0 个独立文件 | 4 个独立文件 |
| CSS | 0 个独立文件 | 2 个独立文件 |
| 解压后体积 | 5,012 bytes | 596,546 bytes |

两者只共同包含 `index.html`。没有发现共同的本地 API 路径或已识别 query key，因此不能将它们视为可互换播放器，也不能为二者编造一个统一调用契约。

### 2.2 `feplayer.dat` 是内联的原生视频页面

`feplayer.dat` 只包含一个约 5 KB 的 `index.html`，没有独立 JavaScript 或 CSS 文件。

脱敏标记证明其中存在：

- 一个 `<video>` 元素；
- `URLSearchParams`；
- `location.search`；
- autoplay；
- currentTime；
- duration；
- muted。

没有证据证明其中存在：

- 第三方播放器库；
- `postMessage` 或 message listener；
- localStorage、sessionStorage、hash 或 window.name 传参；
- 明文 `/toy/...` API；
- `reply_video_url`、`video_url`、`videoUrl` 等已知字段；
- `.mp4`、`.m3u8` 等明文扩展名。

**合理推断：** 它很可能通过 query string 接收某种播放参数，并直接使用浏览器原生视频能力。但报告没有识别出参数名和赋值关系，因此这个推断不能作为补丁锚点。

### 2.3 `webplayer.dat` 是独立的 Vue 前端包

`webplayer.dat` 包含：

```text
assets/main-752b9fc4.js
assets/vendor-f724cb1c.js
assets/vendor-vue-832b0c73.js
assets/zh-cn-deff0b22.js
2 个 CSS
index.html
```

脱敏标记证明其中存在：

- `URLSearchParams`；
- `location.search`；
- autoplay；
- currentTime；
- duration；
- loop；
- muted；
- Vue 与 Element 相关资源。

没有发现 Artplayer、DPlayer、Hls.js、Video.js、Plyr、dash.js、flv.js 等已知播放器库标记。也没有发现已知视频字段、本地 API 路径、message transport 或媒体扩展名。

**合理推断：** 它使用 Vue 自行封装播放界面，媒体参数也可能来自 query string。但不能仅因 vendor 名称和 token 计数，断言具体组件、传参键或播放协议。

### 2.4 当前没有证据支持 `reply_video_url`

三份审计现在一致表明：

- `feapp.dat` 主包没有 `reply_video_url`；
- `feplayer.dat` 没有 `reply_video_url`、`video_url` 或 `videoUrl`；
- `webplayer.dat` 也没有这些字段。

因此后端返回一个新 `reply_video_url` 字段，不会自动使原版客户端播放视频。必须找到原版主包如何打开播放器，以及它实际构造了什么参数。

### 2.5 无 transport 标记不等于没有外部启动

两个播放器包都没有发现 `postMessage`、message listener、localStorage、sessionStorage 或 window.name。

这排除了若干明显的浏览器内通信候选，但仍不能排除：

- 原版主进程创建播放器窗口并拼接 query string；
- CEF/Electron/native 层通过启动参数注入 URL；
- minified 代码间接读取 query 参数；
- 参数值被编码、缩写或在运行时组装。

## 3. 当前不能做的事

在取得下一层证据前，禁止：

- 新增 `reply_video_url` 后宣称播放器已接通；
- 猜测 query key 为 `video_url`、`url`、`src` 或其他名称；
- 修改 `feplayer.dat` 或 `webplayer.dat`；
- 将 `feplayer` 和 `webplayer` 合并成同一个产品契约；
- 新建独立视频页面；
- 绕开原版 Collection 和原版播放器做产品验收。

## 4. 下一步证据

下一项应是**主包与播放器的交叉绑定审计**，只回答：

1. `feapp.dat` 是否引用 `feplayer.dat`、`webplayer.dat` 或对应窗口名称；
2. 原版主包在什么事件后打开播放器；
3. 播放器 URL 或启动参数如何构造；
4. query string 中有哪些受限、可公开的参数键；
5. 播放器是否由信箱 `Collection` 中的当前信件触发；
6. 原版正文终态 `letter_status = 4` 与播放器入口之间有什么关系。

该审计仍必须：

- 只读；
- 不解包到持久目录；
- 不修改原版文件；
- 不输出 JavaScript 源码或任意字符串表；
- 只输出 allowlist 字段、计数、hash 和经过限制的结构证据。

## 5. 当前产品接线结论

现在可冻结为：

```text
原版 Collection
  -> 原版信件数据（letter_status + reply_content 已证实）
  -> 未知的原版播放器启动桥
  -> feplayer 或 webplayer
```

下一个补丁目标不是播放器本身，而是先找到并验证“原版信箱如何打开原版播放器”的唯一、可回滚锚点。

# P03 原版客户端 0.0.9.615 脱敏审计结果

## 1. 证据状态

本结果来自用户本机原版 `feapp.dat`，由 `tools/audit_original_client.py` 只读生成。公开证据文件：

```text
docs/evidence/original-client-audit-0.0.9.615.json
```

已核验：

- `status = AUDITED`；
- 客户端版本目录提示为 `0.0.9.615`；
- archive SHA-256 与支持清单完全一致；
- 主包为 `assets/main-917d29fc.js`；
- 当前补丁需要的四类锚点均处于预期状态；
- `safe_to_apply_existing_patch = true`。

因此当前支持清单、隔离复制安装和既有 `toyApiUrl / toyWsUrl + Home -> Collection` 补丁可以继续作为客户端集成基线。

## 2. 已确认的原版导航表面

脱敏报告发现以下命名路由引用：

```text
Collection
Feedback
Home
Login
ModeSelect
Share
Studio
Survey
UserInfo
```

其中可直接确认：

- `Collection` 是原版信箱入口；
- `Home` 是原版默认首页；
- `UserInfo` 是现有用户信息表面；
- `Studio`、`Survey`、`Feedback` 等属于原版既有页面，不能为了方便擅自挪作陪伴设置页。

`Settings`、`Setting` 和 `Profile` 只在主包中出现为关键词，没有被当前审计识别为命名路由。它们可能是组件、字段、第三方库文本或动态路由，不能据此声明原版存在独立设置页。

## 3. 信件详情结论

报告没有发现名为 `LetterDetail` 的路由引用。

这不等于原版没有信件详情。更可能的情况包括：

- 详情嵌在 `Collection` 页面内部；
- 使用动态路由或未命名子路由；
- 详情组件通过状态切换而不是命名导航打开；
- minified 形式未被当前保守正则识别。

因此 CLIENT-UI-02 不得凭空添加 `LetterDetail` 路由。下一步必须在本机真实打开一封信，记录从列表点击到详情显示过程中使用的原版请求、状态字段和播放器行为。

## 4. 当前信件 wire contract 证据

主包中已发现：

```text
letter_status  × 4
reply_content  × 1
```

未发现：

```text
reply_text
reply_video_url
reply_mode
media_status
media_error_code
```

结论：

1. 原版客户端确实读取 `letter_status` 和 `reply_content`，因此现有后端数值终态映射和 canonical text 字段有真实客户端依据。
2. 当前不能声称原版主包已经理解独立 `media_status`、精确 `reply_mode` 或 `reply_video_url`。
3. 视频可能使用其他字段、动态对象、单独播放器包或原版未被正则识别的绑定方式。
4. 在信件详情接线前，必须先审计 `feplayer.dat`、`webplayer.dat` 以及真实视频信件的网络/播放器行为。

## 5. API 和 action 扫描结论

当前报告中的：

```text
action_names = []
toy_api_paths = []
```

只说明保守扫描没有发现直接字符串形式，不说明原版没有这些调用。主包可能：

- 动态拼接路径；
- 通过统一 request wrapper 传递 action；
- 使用压缩后的常量表；
- 从运行时配置注入地址和接口名。

现有 `getClientConfig` 锚点来自已验证的精确补丁代码，仍然有效；不能用空扫描结果推翻它。

## 6. 原版资源结构

`feapp.dat` 共 36 个成员，其中包括：

```text
5 个 JavaScript
3 个 CSS
1 个 HTML
8 个 MP4
16 个 WEBP
2 个 TTF
1 个 SVG
```

这证明原版前端包自身包含页面资源和媒体素材，但不能证明其中 8 个 MP4 是回信播放器素材。文件名和具体内容没有进入脱敏报告，仍需本机人工查看。

## 7. 当前可确定的最小信息架构

### 7.1 `Collection`

优先保持原版信箱主流程：

```text
写信
等待状态
完成状态
打开原版详情/展开区域
显示 canonical reply
显示最终视频
```

PrivateWorld 候选若与某封信直接相关，应放在该封信的原版详情或紧邻回复的区域，而不是单独管理产品。

### 7.2 `UserInfo`

`UserInfo` 是目前唯一被真实路由证据支持的用户设置候选入口，但其页面结构和可扩展组件尚未确认。

在完成真实页面审计后，才决定是否在其内部增加：

- 长期记忆的查看、纠正和删除；
- 私人称呼、住所权限和 Local Continuation；
- 本地模型与媒体状态；
- 必要配置。

没有确认组件和锚点前，不修改 `UserInfo`。

### 7.3 MiniMax 校准

MiniMax 校准不是日常陪伴能力。即使最终需要图形入口，也只允许放在原版客户端内部的隐藏开发/高级模式，并且必须在真实音频实验开始后再接线。

## 8. 下一步证据顺序

### CLIENT-UI-01C：播放器与详情只读审计

本机审计：

- `feplayer.dat`；
- `webplayer.dat`；
- 原版信箱列表点击一封信后的实际页面；
- 真实或合成视频信件的播放器字段和请求；
- `UserInfo` 页面结构。

输出继续保持脱敏，不提交原版资源或源码。

### CLIENT-UI-02：最小补丁契约

只有上述证据完成后，确定：

- `Collection` 内的详情扩展点；
- 原版播放器复用方式；
- `UserInfo` 内的设置扩展点；
- 唯一锚点、修改后验证和回滚条件。

## 9. 当前禁止事项

- 不新增独立 Control Center 发布入口；
- 不猜测 `LetterDetail` 路由；
- 不把关键词计数当成可复用组件证据；
- 不假设原版已支持 `media_status` 或 `reply_video_url`；
- 不在缺少播放器审计时重写视频展示；
- 不修改 `Studio`、`Survey`、`Feedback` 等无关原版页面；
- 不用直接 HTTP、CLI 或外部浏览器页面代替产品验收。
